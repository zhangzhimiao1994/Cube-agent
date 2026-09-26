from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.conversation_queue import ConversationQueueRepository
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.registry import RuntimeRegistry


class QueueServiceRepository:
    def __init__(self, predecessor: RunRecord) -> None:
        self.predecessor = predecessor
        self.cancelled: list[tuple[object, object, RunStatus]] = []

    async def list_conversation(self, tenant_id, conversation_id):
        del tenant_id, conversation_id
        return (self.predecessor,)

    async def update_control_status(self, tenant_id, run_id, status):
        self.cancelled.append((tenant_id, run_id, status))
        return self.predecessor

    async def completed_step_ids(self, tenant_id, run_id):
        del tenant_id, run_id
        return ()

    async def artifact_ids(self, tenant_id, run_id):
        del tenant_id, run_id
        return ()

    async def usage_cost(self, tenant_id, run_id):
        del tenant_id, run_id
        return Decimal(0)


class FailingConversationQueue:
    async def enqueue(self, command):
        del command
        raise RuntimeError("queue write failed")


class TrackingConversationQueue:
    def __init__(self) -> None:
        self.released: list[tuple[object, object]] = []

    async def release_next_for_terminal_run(self, tenant_id, run_id):
        self.released.append((tenant_id, run_id))


@pytest.mark.asyncio
async def test_queue_message_cancels_blocked_run_when_queue_record_fails() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    predecessor = RunRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        actor_id=actor_id,
        request="running",
        mode=TaskMode.DIRECT,
        status=RunStatus.RUNNING,
        version=1,
        created_at=datetime.now(UTC),
        routing_decision={"conversation_id": "conv-queue"},
    )
    submitted = RunRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        actor_id=actor_id,
        request="queued",
        mode=TaskMode.DIRECT,
        status=RunStatus.QUEUED,
        version=1,
        created_at=datetime.now(UTC),
        routing_decision={"conversation_id": "conv-queue"},
        blocked_by_run_id=predecessor.id,
    )
    repository = QueueServiceRepository(predecessor)
    service = RunService(
        cast(RunRepository, repository),
        runtime_registry=MagicMock(spec=RuntimeRegistry),
        router=None,
        task_queue=MagicMock(),
        conversation_queue_repository=cast(ConversationQueueRepository, FailingConversationQueue()),
    )
    service.submit = AsyncMock(return_value=submitted)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="queue write failed"):
        await service.queue_message(
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_role=None,
            conversation_id="conv-queue",
            message="queued",
            mode=TaskMode.DIRECT,
            idempotency_key="queue-idempotency",
        )

    assert repository.cancelled == [(tenant_id, submitted.id, RunStatus.CANCELLED)]


@pytest.mark.asyncio
async def test_cancel_releases_the_next_conversation_queue_item() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    active = RunRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        actor_id=actor_id,
        request="running",
        mode=TaskMode.DIRECT,
        status=RunStatus.RUNNING,
        version=1,
        created_at=datetime.now(UTC),
        routing_decision={"conversation_id": "conv-queue"},
    )
    repository = QueueServiceRepository(active)
    queue = TrackingConversationQueue()
    service = RunService(
        cast(RunRepository, repository),
        runtime_registry=MagicMock(spec=RuntimeRegistry),
        router=None,
        task_queue=MagicMock(),
        conversation_queue_repository=cast(ConversationQueueRepository, queue),
    )

    await service.cancel(tenant_id, active.id)

    assert repository.cancelled == [(tenant_id, active.id, RunStatus.CANCELLED)]
    assert queue.released == [(tenant_id, active.id)]
