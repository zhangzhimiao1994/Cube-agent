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
from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataPlanningRequest
from agent_hub.skills.sandbox.python_registry_protocol import DependencyNeed, digest, need
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionClaim,
    ResolutionPlan,
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
    ) -> None:
        self.current = current
        self.approved = approved
        self.transport = transport
        self._original: ResolutionClaim | None = None

    async def _claim(self, planning: PythonMetadataPlanningRequest) -> ResolutionClaim:
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
        live = generated_authority(record, scope.tenant_id, scope.run_id)
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
        need(
            (live.project_id, live.session_id) == (scope.project_id, scope.session_id),
            "resolution_scope_changed",
        )
        from agent_hub.capabilities.python_metadata_preparation import metadata_scope

        need(
            metadata_scope(record, actor=scope.actor, live=True) == scope,
            "resolution_scope_changed",
        )
        need(hashlib.sha256(record.request.encode()).hexdigest() == scope.request_sha256)
        if self._original is None:
            # Conservatively include queue time. This cannot restart the run's budget.
            maximum = min(float(context.timeout_seconds), float(live.timeout_seconds))
            original_deadline = record.created_at + timedelta(seconds=maximum)
            remaining = (original_deadline - datetime.now(UTC)).total_seconds()
            deadline = _budget_deadline(time.monotonic() + remaining)
        else:
            original_deadline = self._original.original_deadline
            deadline = _budget_deadline(self._original.monotonic_deadline)
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
        planning: PythonMetadataPlanningRequest,
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
        async with asyncio.timeout_at(current.deadline):
            response = await transport(request)
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
        planning: PythonMetadataPlanningRequest,
    ) -> ResolutionStatus:
        return await self._send(
            "resolve_python_inputs", needs=needs, environment=environment, planning=planning
        )

    async def freeze(
        self,
        *,
        needs: tuple[DependencyNeed, ...],
        environment: dict[str, str],
        planning: PythonMetadataPlanningRequest,
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
    ) -> ResolutionStatus:
        return await self._send(
            "publish_python_resolution",
            needs=needs,
            environment=environment,
            planning=plan.claim.planning,
            plan=plan,
        )

    async def inspect(
        self,
        *,
        needs: tuple[DependencyNeed, ...],
        environment: dict[str, str],
        planning: PythonMetadataPlanningRequest,
        plan: ResolutionPlan | None = None,
        expected_plan_sha256: str | None = None,
    ) -> ResolutionStatus:
        return await self._send(
            "inspect_python_resolution", needs=needs, environment=environment, planning=planning, plan=plan, expected_plan_sha256=expected_plan_sha256
        )
