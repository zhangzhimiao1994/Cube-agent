"""Local delivery reference records and fail-closed service boundary fixtures."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict, TypeGuard
from unittest.mock import AsyncMock
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest
from agent_hub.runs.repository import ConversationContextItem, RunRepository
from agent_hub.runtime.contracts import Artifact
from agent_hub.runtime.instruction_context import InstructionContextLoader


class DeliveryCommon(TypedDict):
    tenant_id: UUID
    actor_id: UUID | None
    actor_role: Role
    routing_decision: dict[str, object]
    request: str
    mode: TaskMode
    worker_id: None
    worker_lease_token: None
    worker_lease_expires_at: None
    worker_heartbeat_at: None


@dataclass
class DeliveryRecord:
    tenant_id: UUID
    actor_id: UUID | None
    actor_role: Role
    routing_decision: dict[str, object]
    request: str
    mode: TaskMode
    worker_id: str | None
    worker_lease_token: UUID | None
    worker_lease_expires_at: datetime | None
    worker_heartbeat_at: datetime | None
    id: UUID
    status: RunStatus
    created_at: datetime


def is_artifacts(value: object) -> TypeGuard[tuple[Artifact, ...]]:
    return isinstance(value, tuple) and all(isinstance(item, Artifact) for item in value)


class DeliveryConversationRepository(RunRepository):
    def __init__(self, items: tuple[ConversationContextItem, ...]) -> None:
        self.items = items

    async def conversation_context(
        self, tenant_id: UUID, conversation_id: str, *, before_run_id: UUID,
        limit: int | None = 32, query: str | None = None,
        candidate_limit: int | None = None,
    ) -> tuple[ConversationContextItem, ...]:
        return self.items


class DeliveryEvidenceLoader(InstructionContextLoader):
    def __init__(self, artifacts: tuple[Artifact, ...], *, failure: bool = False) -> None:
        # Retain the original AsyncMock recording and exception behavior behind
        # the exact production loader signature, with no filesystem/provider IO.
        self.calls = AsyncMock(return_value=artifacts, side_effect=OSError if failure else None)

    async def load_delivery_evidence(
        self, *, tenant_id: UUID, run_id: UUID, source_run_ids: tuple[UUID, ...],
        token_budget: int,
    ) -> tuple[Artifact, ...]:
        result: object = await self.calls(
            tenant_id=tenant_id, run_id=run_id, source_run_ids=source_run_ids,
            token_budget=token_budget,
        )
        assert is_artifacts(result)
        return result


class NoModelGateway:
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        raise AssertionError("prompt construction must not call a model")


def read_map(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping) and all(isinstance(key, str) for key in value)
    return value
