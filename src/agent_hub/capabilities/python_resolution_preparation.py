"""Fresh bound-worker/action fences around the concrete Root resolution IPC."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from agent_hub.capabilities.execution_authority import (
    _trusted_review_context,
    get_verification_authority,
)
from agent_hub.capabilities.generated_project_service import generated_authority
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.models.physical_budget import _budget_deadline, _run_budget
from agent_hub.runs.repository import RunRecord
from agent_hub.skills.sandbox.python_registry_protocol import DependencyNeed, digest, need
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ProtectedOriginalResolutionPlanning,
    ResolutionClaim,
    ResolutionPlan,
    ResolutionPlanning,
    ResolutionRequest,
    ResolutionStatus,
    resolution_key,
)

PREFIX = "workspace/current/project-resolution/python/"


def resolution_action(
    claim: ResolutionClaim,
    needs: tuple[DependencyNeed, ...],
    environment: dict[str, str],
    *,
    plan: ResolutionPlan | None = None,
) -> CapabilityRequest:
    scope = claim.planning.scope
    key = resolution_key(claim, needs, environment)
    purpose = "python_dependency_resolution" if plan is None else "python_dependency_publication"
    binding = {
        "purpose": purpose,
        "key": key,
        "source_sha256": claim.planning.draft.source_sha256,
        "root_sha256": claim.planning.expected_root_config_sha256,
        "catalog_sha256": claim.planning.expected_catalog_sha256,
        "permissions_sha256": scope.permissions_sha256,
        "original_deadline": claim.original_deadline.isoformat(),
        "registry": "https://pypi.org",
        "artifact_host": "files.pythonhosted.org",
        "install_allowed": False,
        "requirements": [n.requirement for n in needs],
        "environment": environment,
        "max_wheel_bytes": 64 * 1024**2,
    }
    if plan is not None:
        need(plan.claim.approval_payload() == claim.approval_payload(), "resolution_scope_changed")
        binding["plan_sha256"] = plan.sha256
        binding["artifacts"] = [list(a) for a in plan.artifacts]
    if isinstance(claim.planning, ProtectedOriginalResolutionPlanning):
        binding["protected_original"] = claim.planning.draft.model_dump(mode="json")
    return CapabilityRequest(
        tenant_id=scope.tenant_id,
        run_id=scope.run_id,
        user_id=scope.user_id,
        agent_id=scope.actor,
        capability="command",
        operation="run",
        resource=PREFIX + digest(binding),
        arguments=binding,
        idempotency_key="resolution-" + key,
    )


class ResolutionApprovalRequired(ValueError):
    def __init__(self, request: CapabilityRequest) -> None:
        self.request = request
        super().__init__("python_resolution_approval_required")


class PythonResolutionPreparation:
    def __init__(
        self,
        *,
        current: Callable[[], Awaitable[RunRecord | None]],
        approved: Callable[[CapabilityRequest], Awaitable[bool]] | None,
        transport: Callable[[ResolutionRequest], Awaitable[ResolutionStatus]] | None = None,
        original_deadline: datetime | None = None,
        protected_original: Callable[[RunRecord, ProtectedOriginalResolutionPlanning], int] | None = None,
    ) -> None:
        self.current = current
        self.approved = approved
        self.transport = transport
        self._original: ResolutionClaim | None = None
        self._original_deadline = original_deadline
        self._protected_original = protected_original

    async def _claim(self, planning: ResolutionPlanning) -> ResolutionClaim:
        budget = _run_budget.get()
        if budget is not None:
            await budget.require_current()
        record = await self.current()
        need(type(record) is RunRecord, "resolution_authority_unavailable")
        assert record is not None
        scope = planning.scope
        assert record.actor_role is not None
        authority = get_verification_authority(
            tenant_id=scope.tenant_id,
            run_id=scope.run_id,
            user_id=scope.user_id,
            role=record.actor_role,
            current_record=record,
        )
        context, worker = _trusted_review_context(
            tenant_id=scope.tenant_id,
            run_id=scope.run_id,
            user_id=scope.user_id,
            role=record.actor_role,
            current_record=record,
        )
        need(
            (
                authority.actor,
                authority.worker_id,
                authority.execution_token,
                authority.request_sha256,
                authority.actor_role.value,
            )
            == (
                scope.actor,
                worker,
                record.worker_lease_token,
                scope.request_sha256,
                scope.actor_role,
            ),
            "resolution_scope_changed",
        )
        if isinstance(planning, ProtectedOriginalResolutionPlanning):
            need(self._protected_original is not None, "resolution_authority_unavailable")
            assert self._protected_original is not None
            allowed_timeout = self._protected_original(record, planning)
            need(type(allowed_timeout) is int and 0 < allowed_timeout <= 3600, "resolution_deadline")
        else:
            live = generated_authority(record, scope.tenant_id, scope.run_id)
            need(
                (live.project_id, live.session_id) == (scope.project_id, scope.session_id),
                "resolution_scope_changed",
            )
            from agent_hub.capabilities.python_metadata_preparation import metadata_scope

            need(metadata_scope(record, actor=scope.actor, live=True) == scope, "resolution_scope_changed")
            allowed_timeout = live.timeout_seconds
        need(hashlib.sha256(record.request.encode()).hexdigest() == scope.request_sha256)
        if self._original is None:
            # The route retains the original total; context timeout is remaining
            # time and tightens the monotonic deadline below from the current time.
            original_deadline = self._original_deadline or record.created_at + timedelta(
                seconds=allowed_timeout)
        else:
            original_deadline = self._original.original_deadline
        # Registered data can retain an earlier exact cutoff across workers. It
        # cannot extend the durable authority or this worker's remaining budget.
        need(original_deadline.tzinfo is not None and record.created_at < original_deadline
             <= record.created_at + timedelta(seconds=allowed_timeout), "resolution_deadline")
        remaining = (original_deadline - datetime.now(UTC)).total_seconds()
        deadline = _budget_deadline(time.monotonic() + remaining if self._original is None
            else self._original.monotonic_deadline)
        deadline = min(deadline, time.monotonic() + min(
            float(context.timeout_seconds), float(allowed_timeout),
            (original_deadline - datetime.now(UTC)).total_seconds()))
        if budget is not None:
            budget.check()
            deadline = min(deadline, budget.deadline)
        assert record.worker_lease_expires_at is not None
        result = ResolutionClaim(
            planning=planning,
            worker_id=worker,
            lease_token=authority.execution_token,
            lease_expires_at=record.worker_lease_expires_at,
            original_deadline=original_deadline,
            monotonic_deadline=deadline,
        )
        _ = result.deadline
        if self._original is None:
            self._original = result
        else:
            if (result.worker_id, result.lease_token) == (self._original.worker_id, self._original.lease_token):
                result.require_continuity(self._original)
            else:
                # Both authority accessors above verified the fresh private binding.
                result.require_data_origin(self._original)
            # Keep each tighter monotonic bound, including on the same worker;
            # subsequent wall-clock conversions must never regain microseconds.
            self._original = result
        return result

    async def _send(
        self,
        action: Literal[
            "resolve_python_inputs",
            "freeze_python_resolution",
            "publish_python_resolution",
            "inspect_python_resolution",
        ],
        *,
        needs: tuple[DependencyNeed, ...],
        environment: dict[str, str],
        planning: ResolutionPlanning,
        plan: ResolutionPlan | None = None,
        expected_plan_sha256: str | None = None,
    ) -> ResolutionStatus:
        if expected_plan_sha256 is not None:
            from agent_hub.skills.sandbox.python_registry_protocol import require_sha
            require_sha(expected_plan_sha256)
            need(action == "inspect_python_resolution" and (plan is None or plan.sha256 == expected_plan_sha256),"resolution_request_invalid")
        claim = await self._claim(planning)
        action_request = resolution_action(claim, needs, environment, plan=plan)
        if action in {"resolve_python_inputs", "publish_python_resolution"} and (
            self.approved is None or await self.approved(action_request) is not True
        ):
            raise ResolutionApprovalRequired(action_request)
        # Approval reads may yield; never dispatch under the earlier lease/grant snapshot.
        current = await self._claim(planning)
        current.require_continuity(claim)
        need(current.planning == claim.planning, "resolution_source_changed")
        if action in {"resolve_python_inputs", "publish_python_resolution"}:
            assert self.approved is not None
            if await self.approved(action_request) is not True:
                raise ResolutionApprovalRequired(action_request)
            current = await self._claim(planning)
            current.require_continuity(claim)
        from uuid import uuid4

        transport_planning = planning.model_copy(
            update={
                "invocation": planning.invocation.model_copy(
                    update={"execution_id": "resolution_" + uuid4().hex}
                )
            }
        )
        request = ResolutionRequest(
            action=action,
            claim=current.model_copy(update={"planning": transport_planning}),
            requirements=tuple((n.requirement, n.evidence_sha256) for n in needs),
            environment=environment,
            expected_plan_sha256=plan.sha256 if plan else expected_plan_sha256,
        )
        transport: Callable[[ResolutionRequest], Awaitable[ResolutionStatus]]
        if self.transport is None:
            from agent_hub.skills.sandbox.python_resolution_worker import request_resolution

            transport = request_resolution
        else:
            transport = self.transport
        from agent_hub.skills.sandbox.python_resolution_worker import _await_resolution_with_renewal

        async def refresh() -> ResolutionClaim:
            # Stay in the original authority-owning task. Refreshing routing
            # never permits adopting another worker, token or approval scope.
            fresh = await self._claim(planning)
            fresh.require_continuity(current)
            need(fresh.planning == planning, "resolution_source_changed")
            if action in {"resolve_python_inputs", "publish_python_resolution"}:
                assert self.approved is not None
                if await self.approved(action_request) is not True:
                    raise ResolutionApprovalRequired(action_request)
                fresh = await self._claim(planning)
                fresh.require_continuity(current)
                need(fresh.planning == planning, "resolution_source_changed")
            return fresh.model_copy(update={"planning": transport_planning})

        async with asyncio.timeout_at(current.task_deadline):
            response = await _await_resolution_with_renewal(request,
                operation=transport(request), refresh=refresh, renew=transport)
            need(
                type(response) is ResolutionStatus and response.key == request.key,
                "resolution_response_invalid",
            )
            final = await self._claim(planning)
            final.require_continuity(current)
            if action in {"resolve_python_inputs", "publish_python_resolution"}:
                assert self.approved is not None
                if await self.approved(action_request) is not True:
                    raise ResolutionApprovalRequired(action_request)
                await self._claim(planning)
            return response

    async def resolve(
        self,
        *,
        needs: tuple[DependencyNeed, ...],
        environment: dict[str, str],
        planning: ResolutionPlanning,
    ) -> ResolutionStatus:
        return await self._send(
            "resolve_python_inputs", needs=needs, environment=environment, planning=planning
        )

    async def freeze(
        self,
        *,
        needs: tuple[DependencyNeed, ...],
        environment: dict[str, str],
        planning: ResolutionPlanning,
    ) -> ResolutionStatus:
        return await self._send(
            "freeze_python_resolution", needs=needs, environment=environment, planning=planning
        )

    async def publish(
        self,
        plan: ResolutionPlan,
        *,
        needs: tuple[DependencyNeed, ...],
        environment: dict[str, str],
        planning: ResolutionPlanning | None = None,
    ) -> ResolutionStatus:
        plan = ResolutionPlan.model_validate(plan, strict=True)
        current_planning = plan.claim.planning if planning is None else planning
        need(plan.resolution_key == resolution_key(plan.claim, needs, environment),
             "resolution_scope_changed")
        transport_losses = (
            ConnectionError, ConnectionAbortedError, ConnectionRefusedError,
            ConnectionResetError, BrokenPipeError, EOFError, asyncio.IncompleteReadError,
        )
        try:
            return await self._send(
                "publish_python_resolution",
                needs=needs,
                environment=environment,
                planning=current_planning,
                plan=plan,
            )
        except (ConnectionError, EOFError) as error:
            # Only known transport losses permit a read, never another effect.
            # Unknown subclasses, timeouts, cancellation, authority/approval,
            # physical-budget stops and incomplete cleanup propagate unchanged.
            if type(error) not in transport_losses:
                raise
            async with asyncio.timeout_at(plan.claim.task_deadline) as timeout:
                current = await self._claim(current_planning)
                if isinstance(current_planning, ProtectedOriginalResolutionPlanning):
                    current.require_data_origin(plan.claim)
                    need(current.approval_payload() == plan.claim.approval_payload(), "resolution_scope_changed")
                else:
                    current.require_continuity(plan.claim)
                timeout.reschedule(current.task_deadline)
                action = resolution_action(current, needs, environment, plan=plan)
                if self.approved is None or await self.approved(action) is not True:
                    raise ResolutionApprovalRequired(action) from error
                fresh = await self._claim(current_planning)
                fresh.require_continuity(current)
                timeout.reschedule(fresh.task_deadline)
                try:
                    status = await self.inspect(
                        needs=needs, environment=environment, planning=current_planning,
                        plan=plan, expected_plan_sha256=plan.sha256,
                    )
                except (ConnectionError, EOFError) as inspect_error:
                    if type(inspect_error) not in transport_losses:
                        raise
                    raise error
                status = ResolutionStatus.model_validate(status, strict=True)
                need(status.key == plan.resolution_key, "resolution_response_invalid")
                if status.state != "published":
                    raise
                need(status.plan is None or status.plan.sha256 == plan.sha256,
                     "resolution_response_invalid")
                inspected = await self._claim(current_planning)
                inspected.require_continuity(fresh)
                timeout.reschedule(inspected.task_deadline)
                # Inspect is data-only. Recheck the concrete publication grant
                # before letting its receipt complete the original operation.
                if await self.approved(action) is not True:
                    raise ResolutionApprovalRequired(action) from error
                final = await self._claim(current_planning)
                final.require_continuity(inspected)
                # Opaque Root inspections intentionally omit plan bytes; only
                # the exact validated, requested plan can fill that field.
                return ResolutionStatus(state="published", key=plan.resolution_key, plan=plan)

    async def inspect(
        self,
        *,
        needs: tuple[DependencyNeed, ...],
        environment: dict[str, str],
        planning: ResolutionPlanning,
        plan: ResolutionPlan | None = None,
        expected_plan_sha256: str | None = None,
    ) -> ResolutionStatus:
        return await self._send(
            "inspect_python_resolution", needs=needs, environment=environment, planning=planning, plan=plan, expected_plan_sha256=expected_plan_sha256
        )
