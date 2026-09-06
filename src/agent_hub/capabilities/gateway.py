from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalRecord,
    ApprovalService,
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.policy import CapabilityPolicy
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect


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
    ) -> None:
        self._policy = policy
        self._approvals = approvals
        self._run_repository = run_repository
        self._approval_reviewer = approval_reviewer
        approvals.bind_run_repository(run_repository)  # type: ignore[arg-type]

    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        decision = self._policy.evaluate(request, role)
        if decision.effect is PolicyEffect.DENY:
            return CapabilityResult(
                CapabilityStatus.DENIED,
                request.run_id,
                reason="capability denied",
            )
        if decision.effect is PolicyEffect.ALLOW:
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)

        review = await self._review_required_request(request)
        if review is not None:
            if review.outcome is ApprovalReviewOutcome.ALLOW:
                if not await self._record_review_decision(
                    request,
                    review,
                    status="approved",
                ):
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
                )
                return CapabilityResult(
                    CapabilityStatus.DENIED,
                    request.run_id,
                    reason=review.reason,
                    review=review,
                )

        async def begin_wait(approval: ApprovalRecord) -> None:
            await self._run_repository.begin_capability_approval(  # type: ignore[attr-defined]
                request.tenant_id,
                request.run_id,
                approval_id=approval.id,
                approval_fingerprint=approval.request_fingerprint,
                approval_scope=capability_approval_scope(request),
            )

        approval = await self._approvals.start_waiting(
            request,
            begin_wait,
        )
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
    ) -> bool:
        recorder = getattr(self._run_repository, "record_capability_approval_review", None)
        if not callable(recorder):
            return True
        fingerprint = fingerprint_capability_request(request)
        approval_id = f"auto_review_{hashlib.sha256(fingerprint.encode()).hexdigest()[:24]}"
        try:
            await recorder(
                request.tenant_id,
                request.run_id,
                approval_id=approval_id,
                approval_fingerprint=fingerprint,
                reviewer=review.reviewer,
                reason=review.reason,
                status=status,
            )
        except Exception:  # noqa: BLE001 - failed audit must not auto-approve execution.
            return False
        return True


def _bounded_printable_text(value: str, *, field_name: str, max_length: int) -> None:
    if (
        not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be nonempty bounded printable text")
