"""Typed catalog/cold-read observations; no extra policy or execution authority.

Policy is an exact local copy of the original missing cold-verification test
helper. No primary-worktree module is imported at execution time.
"""
from collections.abc import Mapping
from typing import TypedDict
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runtime.contracts import JsonValue


class ExecuteArgs(TypedDict):
    tenant_id: UUID
    run_id: UUID
    actor: str
    name: str
    arguments: Mapping[str, JsonValue]
    idempotency_key: str


class CallTrace(ExecuteArgs):
    lease: UUID | None


type BudgetWindow = tuple[float, float, int, tuple[tuple[int, int, int], ...]]


class Policy:
    def __init__(self) -> None:
        self.allowed: set[str] = set()

    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        if request.operation in self.allowed:
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        return CapabilityResult(CapabilityStatus.WAITING_APPROVAL, request.run_id,
            approval_id='approval_'+request.operation, reason='capability requires approval')
