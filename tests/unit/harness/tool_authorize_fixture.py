"""Private typed policies for static discovery versus concrete invocation tests."""
from __future__ import annotations

from typing import TypedDict
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.types import CapabilityRequest
from tests.unit.capabilities.test_scoped_read import StoredRun, stored_run
from tests.unit.harness.test_tool_gateway import RUN_ID, TENANT_ID, USER_ID


class StaticPolicyGateway:
    def __init__(
        self, status: CapabilityStatus = CapabilityStatus.ALLOWED, *,
        static_status: CapabilityStatus = CapabilityStatus.ALLOWED,
        approval_id: str | None = None,
    ) -> None:
        self.status = status
        self.static_status = static_status
        self.approval_id = approval_id
        self.static_requests: list[tuple[CapabilityRequest, Role]] = []
        self.requests: list[tuple[CapabilityRequest, Role]] = []

    async def authorize_static(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        self.static_requests.append((request, role))
        return CapabilityResult(self.static_status, request.run_id,
            reason="capability denied" if self.static_status is CapabilityStatus.DENIED else None)

    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        self.requests.append((request, role))
        return CapabilityResult(self.status, request.run_id, approval_id=self.approval_id)


class ApprovalWait(TypedDict):
    approval_id: str
    approval_fingerprint: str
    approval_scope: str | None
    actor_id: UUID | None


class AskRepository:
    def __init__(self) -> None:
        self.record = stored_run(tenant=TENANT_ID, run=RUN_ID, actor_id=USER_ID,
            requested_permissions=["workspace.read", "workspace.write", "command.run", "network.read"])
        self.scopes: set[str] = set()
        self.waits: list[ApprovalWait] = []

    async def get(self, tenant_id: UUID, run_id: UUID) -> StoredRun | None:
        return self.record if (tenant_id, run_id) == (TENANT_ID, RUN_ID) else None

    async def is_capability_approval_scope_approved(self, tenant_id: UUID, run_id: UUID, scope: str) -> bool:
        return (tenant_id, run_id) == (TENANT_ID, RUN_ID) and scope in self.scopes

    async def begin_capability_approval(
        self, tenant_id: UUID, run_id: UUID, *, approval_id: str,
        approval_fingerprint: str, approval_scope: str | None, actor_id: UUID | None = None,
    ) -> None:
        assert (tenant_id, run_id) == (TENANT_ID, RUN_ID)
        self.waits.append({"approval_id": approval_id, "approval_fingerprint": approval_fingerprint,
            "approval_scope": approval_scope, "actor_id": actor_id})
