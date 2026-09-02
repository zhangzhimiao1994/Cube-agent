from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalRecord,
    ApprovalService,
    capability_approval_scope,
)
from agent_hub.capabilities.policy import CapabilityPolicy
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect


class CapabilityStatus(StrEnum):
    ALLOWED = "allow"
    WAITING_APPROVAL = "waiting_approval"
    DENIED = "deny"


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    status: CapabilityStatus
    run_id: UUID
    approval_id: str | None = None
    reason: str | None = None


class CapabilityGateway:
    def __init__(
        self,
        policy: CapabilityPolicy,
        approvals: ApprovalService,
        run_repository: object,
    ) -> None:
        self._policy = policy
        self._approvals = approvals
        self._run_repository = run_repository
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
        )
