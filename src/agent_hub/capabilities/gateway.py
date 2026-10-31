from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalRecord,
    ApprovalService,
    capability_approval_scope,
    exact_capability_approval,
    fingerprint_capability_request,
    plugin_approval_identity,
)
from agent_hub.capabilities.permission_modes import (
    read_only_capability_allowed,
    run_permission_mode,
)
from agent_hub.capabilities.policy import CapabilityPolicy, normalize_resource
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.runtime.action_approval_capture import (
    action_approval_capture_active,
    before_action_approval,
)

if TYPE_CHECKING:
    from agent_hub.capabilities.review_context import _ConcreteReviewCall
    from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority


class CapabilityStatus(StrEnum):
    ALLOWED = "allow"
    WAITING_APPROVAL = "waiting_approval"
    DENIED = "deny"


class ApprovalReviewOutcome(StrEnum):
    ALLOW = "allow"
    REQUIRE_USER_APPROVAL = "require_user_approval"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ApprovalReviewDecision:
    outcome: ApprovalReviewOutcome
    reviewer: str
    reason: str

    def __post_init__(self) -> None:
        _bounded_printable_text(self.reviewer, field_name="reviewer", max_length=64)
        _bounded_printable_text(self.reason, field_name="reason", max_length=256)


class ApprovalReviewer(Protocol):
    async def review(self, request: CapabilityRequest) -> ApprovalReviewDecision: ...


class ApprovalReviewRepository(Protocol):
    async def record_capability_approval_review(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        approval_id: str,
        approval_fingerprint: str,
        reviewer: str,
        reason: str,
        status: str,
        toolchain_authority: ToolchainSupplyAuthority | None = None,
        approval_scope: str | None = None,
        go_recipe_authority: ToolchainSupplyAuthority | None = None,
        expected_go_recipe: Mapping[str, object] | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    status: CapabilityStatus
    run_id: UUID
    approval_id: str | None = None
    reason: str | None = None
    review: ApprovalReviewDecision | None = None


class CapabilityGateway:
    def __init__(
        self,
        policy: CapabilityPolicy,
        approvals: ApprovalService,
        run_repository: object,
        *,
        approval_reviewer: ApprovalReviewer | None = None,
        plugin_policy_source: object | None = None,
        toolchain_authority: ToolchainSupplyAuthority | None = None,
        go_recipe_authority: ToolchainSupplyAuthority | None = None,
    ) -> None:
        self._policy = policy
        self._approvals = approvals
        self._run_repository = run_repository
        self._approval_reviewer = approval_reviewer
        self._plugin_policy_source = plugin_policy_source
        self._toolchain_authority = toolchain_authority
        self._go_recipe_authority = go_recipe_authority
        approvals.bind_run_repository(run_repository)  # type: ignore[arg-type]

    async def invoke(
        self, request: CapabilityRequest, *, role: Role,
        concrete_review: _ConcreteReviewCall | None = None,
        submission_authorized: bool = False,
    ) -> CapabilityResult:
        try:
            permission_mode = await run_permission_mode(self._run_repository, request, role=role)
        except Exception:  # noqa: BLE001 - a missing trusted policy never grants execution.
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason="capability denied")
        if permission_mode == "read_only" and not read_only_capability_allowed(request):
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason="read_only capability denied")
        go_recipe = _is_go_recipe_request(request)
        expected_go_recipe: Mapping[str, object] | None = None
        if go_recipe:
            from agent_hub.runs.go_project_admission import (
                APPROVAL_REQUIRED,
                RECIPE_KEY,
                require_recipe_request,
            )
            from agent_hub.runs.repository import RunConflict
            from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority
            from agent_hub.runtime.contracts import _freeze_object

            authority = self._go_recipe_authority
            if type(authority) is not ToolchainSupplyAuthority or authority.actor_role is not role:
                return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason='capability denied')
            try:
                record = await self._run_repository.get(request.tenant_id, request.run_id)  # type: ignore[attr-defined]
                authority.require_current(record, request.tenant_id, request.run_id)
                require_recipe_request(record, request)
                expected_go_recipe = _freeze_object(record.routing_decision[RECIPE_KEY], name='Go recipe admission')
            except Exception:  # noqa: BLE001 - public request fields cannot establish private authority.
                return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason='capability denied')
        if _is_toolchain_supply_request(request) and self._toolchain_authority is None:
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason='capability denied')
        decision = self._policy.evaluate(request, role)
        if decision.effect is PolicyEffect.DENY:
            return CapabilityResult(
                CapabilityStatus.DENIED,
                request.run_id,
                reason="capability denied",
            )
        if concrete_review is not None:
            async def human_wait() -> CapabilityResult:
                # This instance has no legacy reviewer in the fresh path. A model
                # allow never enters the ordinary approved-grant audit/upsert.
                gateway = CapabilityGateway(self._policy, self._approvals, self._run_repository,
                    plugin_policy_source=self._plugin_policy_source)
                with exact_capability_approval(request):
                    return await gateway.invoke(request, role=role)

            return await concrete_review.authorize(request, role, self._policy,
                inbound=submission_authorized, human_wait=human_wait)
        if go_recipe:
            try:
                await self._run_repository.require_go_project_recipe_approval(  # type: ignore[attr-defined]
                    request.tenant_id, request.run_id, authority=self._go_recipe_authority,
                    request=request, expected_go_recipe=expected_go_recipe)
            except RunConflict as error:
                if str(error) != APPROVAL_REQUIRED:
                    raise
            else:
                return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        if decision.effect is PolicyEffect.ALLOW and not go_recipe:
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)

        review = await self._review_required_request(request)
        if review is not None:
            if review.outcome is ApprovalReviewOutcome.ALLOW:
                if not await self._record_review_decision(
                    request,
                    review,
                    status="approved",
                    expected_go_recipe=expected_go_recipe,
                ):
                    if go_recipe:
                        return CapabilityResult(CapabilityStatus.DENIED, request.run_id,
                                                reason='Go recipe approval audit unavailable')
                    review = ApprovalReviewDecision(
                        outcome=ApprovalReviewOutcome.REQUIRE_USER_APPROVAL,
                        reviewer="auto_review",
                        reason="approval reviewer audit unavailable",
                    )
                else:
                    return CapabilityResult(
                        CapabilityStatus.ALLOWED,
                        request.run_id,
                        reason=f"approved by {review.reviewer}",
                        review=review,
                    )
            if review.outcome is ApprovalReviewOutcome.DENY:
                await self._record_review_decision(
                    request,
                    review,
                    status="denied",
                    expected_go_recipe=expected_go_recipe,
                )
                return CapabilityResult(
                    CapabilityStatus.DENIED,
                    request.run_id,
                    reason=review.reason,
                    review=review,
                )

        async def begin_wait(approval: ApprovalRecord) -> None:
            identity = plugin_approval_identity(self._plugin_policy_source, request)
            identity_args: dict[str, object] = (
                {"plugin_identity": identity}
                if identity is not None else {}
            )
            if self._toolchain_authority is not None:
                identity_args['toolchain_authority'] = self._toolchain_authority
            if go_recipe:
                identity_args['go_recipe_authority'] = self._go_recipe_authority
                identity_args['expected_go_recipe'] = expected_go_recipe
            if getattr(self._run_repository, "supports_conversation_capability_approval", False) is True:
                identity_args["approval_request"] = request
            await before_action_approval(request, approval.id)
            await self._run_repository.begin_capability_approval(  # type: ignore[attr-defined]
                request.tenant_id,
                request.run_id,
                approval_id=approval.id,
                approval_fingerprint=approval.request_fingerprint,
                approval_scope=capability_approval_scope(request),
                actor_id=request.user_id,
                **identity_args,
            )

        if self._toolchain_authority is not None or go_recipe or action_approval_capture_active():
            # A rejected DB fence must not leave even an in-memory SDK approval.
            approval = await self._approvals.start_waiting(request, begin_wait, publish_after_begin=True)
        else:
            approval = await self._approvals.start_waiting(request, begin_wait)
        return CapabilityResult(
            CapabilityStatus.WAITING_APPROVAL,
            request.run_id,
            approval_id=approval.id,
            reason="capability requires approval",
            review=review,
        )

    async def _review_required_request(
        self,
        request: CapabilityRequest,
    ) -> ApprovalReviewDecision | None:
        if self._approval_reviewer is None:
            return None
        try:
            return await self._approval_reviewer.review(request)
        except Exception:  # noqa: BLE001 - approval review must fail closed to user approval.
            return ApprovalReviewDecision(
                outcome=ApprovalReviewOutcome.REQUIRE_USER_APPROVAL,
                reviewer="auto_review",
                reason="approval reviewer unavailable",
            )

    async def _record_review_decision(
        self,
        request: CapabilityRequest,
        review: ApprovalReviewDecision,
        *,
        status: str,
        expected_go_recipe: Mapping[str, object] | None = None,
    ) -> bool:
        recorder = getattr(self._run_repository, "record_capability_approval_review", None)
        resolution = request.resource.startswith('workspace/current/project-resolution/')
        if not callable(recorder):
            return (self._toolchain_authority is None and self._go_recipe_authority is None
                    and not _is_metadata_preparation_request(request) and not resolution)
        fingerprint = fingerprint_capability_request(request)
        approval_id = f"auto_review_{hashlib.sha256(fingerprint.encode()).hexdigest()[:24]}"
        try:
            toolchain_options: dict[str, object] = {}
            if _is_metadata_preparation_request(request):
                toolchain_options['approval_scope'] = capability_approval_scope(request)
            if resolution:
                toolchain_options['approval_scope'] = capability_approval_scope(request)
                toolchain_options['approval_request'] = request
            if self._toolchain_authority is not None:
                toolchain_options = {'toolchain_authority': self._toolchain_authority,
                                     'approval_scope': capability_approval_scope(request)}
            if self._go_recipe_authority is not None:
                toolchain_options = {'go_recipe_authority': self._go_recipe_authority,
                                     'expected_go_recipe': expected_go_recipe,
                                     'approval_scope': capability_approval_scope(request)}
            await recorder(
                request.tenant_id,
                request.run_id,
                approval_id=approval_id,
                approval_fingerprint=fingerprint,
                reviewer=review.reviewer,
                reason=review.reason,
                status=status,
                **toolchain_options,
            )
        except Exception:  # noqa: BLE001 - failed audit must not auto-approve execution.
            return False
        return True


def _is_metadata_preparation_request(request: CapabilityRequest) -> bool:
    resource = normalize_resource(request.resource)
    prefix = 'workspace/current/project-metadata'
    return resource is not None and (resource == prefix or resource.startswith(prefix + '/'))


def _is_toolchain_supply_request(request: CapabilityRequest) -> bool:
    resource = normalize_resource(request.resource)
    prefix = 'workspace/current/project-inputs/toolchain'
    return resource is not None and (resource == prefix or resource.startswith(prefix + '/'))


def _is_go_recipe_request(request: CapabilityRequest) -> bool:
    resource = normalize_resource(request.resource)
    prefix = 'workspace/current/project-verification/go'
    return resource is not None and (resource == prefix or resource.startswith(prefix + '/'))


def _bounded_printable_text(value: str, *, field_name: str, max_length: int) -> None:
    if (
        not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be nonempty bounded printable text")
