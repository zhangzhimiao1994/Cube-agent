from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.conversation_queue import (
    ConversationQueueItem,
    ConversationQueueRepository,
    EnqueueConversationMessage,
)
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.registry import RuntimeRegistry


class CancellableRuntime:
    mode = TaskMode.DIRECT

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.cancelled: list[UUID] = []

    async def cancel_run(self, run_id: UUID) -> None:
        self.cancelled.append(run_id)
        if self.fail:
            raise RuntimeError("runtime cancel failed")

    async def cancel(self) -> None:
        raise AssertionError("run-scoped cancellation is required")

    async def run(self, context: object) -> object:
        del context
        raise AssertionError("not used")

    async def save_checkpoint(self) -> object:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: object) -> None:
        del checkpoint


class QueueServiceRepository:
    def __init__(self, predecessor: RunRecord) -> None:
        self.predecessor = predecessor
        self.cancelled: list[tuple[object, object, RunStatus]] = []

    async def list_conversation(
        self, tenant_id: UUID, conversation_id: str
    ) -> tuple[RunRecord, ...]:
        del tenant_id, conversation_id
        return (self.predecessor,)

    async def update_control_status(
        self, tenant_id: UUID, run_id: UUID, status: RunStatus
    ) -> RunRecord:
        self.cancelled.append((tenant_id, run_id, status))
        self.predecessor = replace(self.predecessor, status=status)
        return self.predecessor

    async def release_expired_worker_execution(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        worker_id: str,
        worker_lease_token: UUID,
        now: datetime,
    ) -> RunRecord:
        assert tenant_id == self.predecessor.tenant_id
        assert run_id == self.predecessor.id
        assert worker_id == self.predecessor.worker_id
        assert worker_lease_token == self.predecessor.worker_lease_token
        if (
            self.predecessor.worker_lease_expires_at is not None
            and self.predecessor.worker_lease_expires_at <= now
        ):
            self.predecessor = replace(
                self.predecessor,
                worker_id=None,
                worker_lease_token=None,
                worker_lease_expires_at=None,
            )
        return self.predecessor

    async def completed_step_ids(self, tenant_id: UUID, run_id: UUID) -> tuple[str, ...]:
        del tenant_id, run_id
        return ()

    async def artifact_ids(self, tenant_id: UUID, run_id: UUID) -> tuple[UUID, ...]:
        del tenant_id, run_id
        return ()

    async def usage_cost(self, tenant_id: UUID, run_id: UUID) -> Decimal:
        del tenant_id, run_id
        return Decimal(0)


class FailingConversationQueue:
    async def enqueue(self, command: EnqueueConversationMessage) -> ConversationQueueItem:
        del command
        raise RuntimeError("queue write failed")


class TrackingConversationQueue:
    def __init__(self) -> None:
        self.released: list[tuple[object, object]] = []

    async def release_next_for_terminal_run(
        self, tenant_id: UUID, run_id: UUID
    ) -> ConversationQueueItem | None:
        self.released.append((tenant_id, run_id))
        return None


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
async def test_cancel_keeps_successor_blocked_until_active_worker_releases() -> None:
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
        worker_id="worker-active",
        worker_lease_token=uuid4(),
        worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    repository = QueueServiceRepository(active)
    queue = TrackingConversationQueue()
    runtime = CancellableRuntime()
    service = RunService(
        cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((runtime,)),  # type: ignore[arg-type]
        router=None,
        task_queue=MagicMock(),
        conversation_queue_repository=cast(ConversationQueueRepository, queue),
        worker_id="worker-active",
    )

    await service.cancel(tenant_id, active.id)

    assert repository.cancelled == [(tenant_id, active.id, RunStatus.CANCELLED)]
    assert queue.released == []
    assert runtime.cancelled == [active.id]


@pytest.mark.asyncio
async def test_cancel_failure_keeps_active_worker_and_successor_blocked() -> None:
    tenant_id = uuid4()
    active = RunRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        actor_id=uuid4(),
        request="running",
        mode=TaskMode.DIRECT,
        status=RunStatus.RUNNING,
        version=1,
        created_at=datetime.now(UTC),
        routing_decision={"conversation_id": "conv-queue"},
        worker_id="worker-active",
        worker_lease_token=uuid4(),
        worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    repository = QueueServiceRepository(active)
    queue = TrackingConversationQueue()
    runtime = CancellableRuntime(fail=True)
    service = RunService(
        cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((runtime,)),  # type: ignore[arg-type]
        router=None,
        task_queue=MagicMock(),
        conversation_queue_repository=cast(ConversationQueueRepository, queue),
        worker_id="worker-active",
    )

    with pytest.raises(RuntimeError, match="runtime cancel failed"):
        await service.cancel(tenant_id, active.id)

    assert queue.released == []
    assert active.worker_lease_token is not None


@pytest.mark.asyncio
async def test_pause_preserves_worker_and_drives_run_scoped_runtime_stop() -> None:
    tenant_id = uuid4()
    active = RunRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        actor_id=uuid4(),
        request="running",
        mode=TaskMode.DIRECT,
        status=RunStatus.RUNNING,
        version=1,
        created_at=datetime.now(UTC),
        routing_decision={"conversation_id": "conv-queue"},
        worker_id="worker-active",
        worker_lease_token=uuid4(),
        worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    repository = QueueServiceRepository(active)
    runtime = CancellableRuntime()
    service = RunService(
        cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((runtime,)),  # type: ignore[arg-type]
        router=None,
        task_queue=MagicMock(),
        worker_id="worker-active",
    )

    summary = await service.pause(tenant_id, active.id)

    assert summary.status is RunStatus.PAUSED
    assert summary.execution_quiescent is False
    assert repository.predecessor.worker_id == "worker-active"
    assert runtime.cancelled == [active.id]


@pytest.mark.asyncio
async def test_successful_cancel_retry_releases_expired_worker_and_successor() -> None:
    tenant_id = uuid4()
    active = RunRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        actor_id=uuid4(),
        request="running",
        mode=TaskMode.DIRECT,
        status=RunStatus.RUNNING,
        version=1,
        created_at=datetime.now(UTC),
        routing_decision={"conversation_id": "conv-queue"},
        worker_id="worker-active",
        worker_lease_token=uuid4(),
        worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    repository = QueueServiceRepository(active)
    queue = TrackingConversationQueue()
    runtime = CancellableRuntime(fail=True)
    service = RunService(
        cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((runtime,)),  # type: ignore[arg-type]
        router=None,
        task_queue=MagicMock(),
        conversation_queue_repository=cast(ConversationQueueRepository, queue),
        worker_id="worker-active",
    )

    with pytest.raises(RuntimeError, match="runtime cancel failed"):
        await service.cancel(tenant_id, active.id)
    assert repository.predecessor.status is RunStatus.CANCELLED
    assert repository.predecessor.worker_id == "worker-active"
    assert queue.released == []

    repository.predecessor = replace(
        repository.predecessor,
        worker_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    runtime.fail = False
    summary = await service.cancel(tenant_id, active.id)

    assert summary.status is RunStatus.CANCELLED
    assert summary.execution_quiescent is True
    assert repository.predecessor.worker_id is None
    assert runtime.cancelled == [active.id, active.id]
    assert queue.released == [(tenant_id, active.id)]
