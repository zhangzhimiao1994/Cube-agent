from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.conversation_queue import ConversationQueueRepository
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.service import RunService, SubmittedRun
from agent_hub.runtime.contracts import RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry


def test_recover_running_recovers_candidates_and_continues_after_failure() -> None:
    async def scenario() -> None:
        recovered: list[UUID] = []
        first = uuid4()
        second = uuid4()
        third = uuid4()
        service = RecoverRunningService(
            recoverable_run_repository=RecoverableRunRepository((first, second, third)),
            recovered=recovered,
            failing_run_id=second,
        )

        count = await service.recover_running(limit=10)

        assert count == 2
        assert recovered == [first, third]

    asyncio.run(scenario())


def test_recover_running_does_not_count_still_active_race() -> None:
    async def scenario() -> None:
        recovered: list[UUID] = []
        first = uuid4()
        second = uuid4()
        service = RecoverRunningService(
            recoverable_run_repository=RecoverableRunRepository((first, second)),
            recovered=recovered,
            failing_run_id=uuid4(),
            active_run_id=first,
        )

        count = await service.recover_running(limit=10)

        assert count == 1
        assert recovered == [second]

    asyncio.run(scenario())


def test_recover_running_does_not_starve_terminal_worker_recovery() -> None:
    async def scenario() -> None:
        recovered: list[UUID] = []
        service = RecoverRunningService(
            recoverable_run_repository=RecoverableRunRepository((uuid4(), uuid4())),
            recovered=recovered,
            failing_run_id=uuid4(),
            terminal_recovery_count=1,
        )

        count = await service.recover_running(limit=2)

        assert count == 3
        assert service.terminal_recovery_limits == [2]

    asyncio.run(scenario())


def test_recover_conversation_queue_releases_retries_each_terminal_predecessor() -> None:
    async def scenario() -> None:
        tenant_id = uuid4()
        first, second, third = uuid4(), uuid4(), uuid4()
        queue = RecoverableConversationQueue(
            ((tenant_id, first), (tenant_id, second), (tenant_id, third)),
            failing_run_id=second,
        )
        service = RunService(
            cast(RunRepository, RecoverableRunRepository(())),
            runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
            router=None,
            task_queue=UnusedQueue(),
            conversation_queue_repository=cast(ConversationQueueRepository, queue),
        )

        assert await service.recover_conversation_queue_releases(limit=10) == 2
        assert queue.released == [first, second, third]

    asyncio.run(scenario())


def test_recover_terminal_worker_execution_cancels_runtime_then_releases_lease() -> None:
    async def scenario() -> None:
        now = datetime.now(UTC)
        record = RunRecord(
            id=uuid4(),
            tenant_id=uuid4(),
            actor_id=uuid4(),
            request="crashed after terminal commit",
            mode=TaskMode.DISPATCH,
            status=RunStatus.CANCELLED,
            version=3,
            created_at=now - timedelta(minutes=1),
            routing_decision=None,
            worker_id="worker-crashed",
            worker_lease_token=uuid4(),
            worker_lease_expires_at=now - timedelta(seconds=1),
        )
        repository = TerminalWorkerRecoveryRepository(record)
        runtime = RecoverableTerminalRuntime()
        service = RunService(
            cast(RunRepository, repository),
            runtime_registry=RuntimeRegistry((runtime,)),
            router=None,
            task_queue=UnusedQueue(),
            worker_id="worker-crashed",
        )

        assert await service.recover_terminal_worker_executions(limit=10) == 1
        assert runtime.cancelled == [record.id]
        assert repository.record.worker_id is None
        assert await service.recover_terminal_worker_executions(limit=10) == 0

    asyncio.run(scenario())


def test_recover_terminal_worker_execution_keeps_lease_when_runtime_cancel_fails() -> None:
    async def scenario() -> None:
        now = datetime.now(UTC)
        record = RunRecord(
            id=uuid4(),
            tenant_id=uuid4(),
            actor_id=uuid4(),
            request="runtime still uncertain",
            mode=TaskMode.DISPATCH,
            status=RunStatus.FAILED,
            version=3,
            created_at=now - timedelta(minutes=1),
            routing_decision=None,
            worker_id="worker-crashed",
            worker_lease_token=uuid4(),
            worker_lease_expires_at=now - timedelta(seconds=1),
        )
        repository = TerminalWorkerRecoveryRepository(record)
        runtime = RecoverableTerminalRuntime(fail=True)
        service = RunService(
            cast(RunRepository, repository),
            runtime_registry=RuntimeRegistry((runtime,)),
            router=None,
            task_queue=UnusedQueue(),
            worker_id="worker-crashed",
        )

        assert await service.recover_terminal_worker_executions(limit=10) == 0
        assert repository.record.worker_id == "worker-crashed"

    asyncio.run(scenario())


def test_recover_terminal_worker_execution_clears_expired_other_process_lease() -> None:
    async def scenario() -> None:
        now = datetime.now(UTC)
        record = RunRecord(
            id=uuid4(),
            tenant_id=uuid4(),
            actor_id=uuid4(),
            request="owned by another process",
            mode=TaskMode.DISPATCH,
            status=RunStatus.CANCELLED,
            version=3,
            created_at=now - timedelta(minutes=1),
            routing_decision=None,
            worker_id="worker-other",
            worker_lease_token=uuid4(),
            worker_lease_expires_at=now - timedelta(seconds=1),
        )
        repository = TerminalWorkerRecoveryRepository(record)
        runtime = RecoverableTerminalRuntime()
        service = RunService(
            cast(RunRepository, repository),
            runtime_registry=RuntimeRegistry((runtime,)),
            router=None,
            task_queue=UnusedQueue(),
            worker_id="worker-local",
        )

        assert await service.recover_terminal_worker_executions(limit=10) == 1
        assert runtime.cancelled == []
        assert repository.record.worker_id is None
        assert repository.record.worker_lease_token is None
        assert repository.record.worker_lease_expires_at is None

    asyncio.run(scenario())


def test_recover_paused_other_process_lease_without_releasing_successor() -> None:
    async def scenario() -> None:
        now = datetime.now(UTC)
        record = RunRecord(
            id=uuid4(),
            tenant_id=uuid4(),
            actor_id=uuid4(),
            request="paused after old worker exited",
            mode=TaskMode.DISPATCH,
            status=RunStatus.PAUSED,
            version=3,
            created_at=now - timedelta(minutes=1),
            routing_decision=None,
            worker_id="worker-old-process",
            worker_lease_token=uuid4(),
            worker_lease_expires_at=now - timedelta(seconds=1),
        )
        repository = TerminalWorkerRecoveryRepository(record)
        runtime = RecoverableTerminalRuntime()
        queue = RecoverableConversationQueue(
            ((record.tenant_id, record.id),),
            failing_run_id=uuid4(),
        )
        service = RunService(
            cast(RunRepository, repository),
            runtime_registry=RuntimeRegistry((runtime,)),
            router=None,
            task_queue=UnusedQueue(),
            conversation_queue_repository=cast(ConversationQueueRepository, queue),
            worker_id="worker-new-process",
        )

        assert await service.recover_terminal_worker_executions(limit=10) == 1
        assert runtime.cancelled == []
        assert repository.record.status is RunStatus.PAUSED
        assert repository.record.worker_id is None
        assert repository.record.worker_lease_token is None
        assert repository.record.worker_lease_expires_at is None
        assert queue.released == []

    asyncio.run(scenario())


class RecoverableRunRepository:
    def __init__(self, candidates: tuple[UUID, ...]) -> None:
        self._candidates = candidates

    async def running_for_recovery(self, limit: int, *, now: datetime) -> tuple[UUID, ...]:
        del now
        return self._candidates[:limit]

    async def terminal_worker_executions_for_recovery(
        self,
        limit: int,
        *,
        now: datetime,
    ) -> tuple[RunRecord, ...]:
        del limit, now
        return ()


class TerminalWorkerRecoveryRepository:
    def __init__(self, record: RunRecord) -> None:
        self.record = record

    async def terminal_worker_executions_for_recovery(
        self,
        limit: int,
        *,
        now: datetime,
    ) -> tuple[RunRecord, ...]:
        if (
            limit <= 0
            or self.record.worker_id is None
            or self.record.worker_lease_expires_at is None
            or self.record.worker_lease_expires_at > now
        ):
            return ()
        return (self.record,)

    async def release_expired_worker_execution(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        worker_id: str,
        worker_lease_token: UUID,
        now: datetime,
    ) -> RunRecord:
        assert tenant_id == self.record.tenant_id
        assert run_id == self.record.id
        assert worker_id == self.record.worker_id
        assert worker_lease_token == self.record.worker_lease_token
        assert self.record.worker_lease_expires_at is not None
        assert self.record.worker_lease_expires_at <= now
        self.record = replace(
            self.record,
            worker_id=None,
            worker_lease_token=None,
            worker_lease_expires_at=None,
        )
        return self.record


class RecoverableConversationQueue:
    def __init__(
        self,
        candidates: tuple[tuple[UUID, UUID], ...],
        *,
        failing_run_id: UUID,
    ) -> None:
        self.candidates = candidates
        self.failing_run_id = failing_run_id
        self.released: list[UUID] = []

    async def terminal_predecessors_for_recovery(
        self, limit: int
    ) -> tuple[tuple[UUID, UUID], ...]:
        return self.candidates[:limit]

    async def release_next_for_terminal_run(
        self, tenant_id: UUID, run_id: UUID
    ) -> None:
        assert any(candidate_tenant == tenant_id for candidate_tenant, _ in self.candidates)
        self.released.append(run_id)
        if run_id == self.failing_run_id:
            raise RuntimeError("synthetic queue release failure")


class RecoverRunningService(RunService):
    def __init__(
        self,
        *,
        recoverable_run_repository: RecoverableRunRepository,
        recovered: list[UUID],
        failing_run_id: UUID,
        active_run_id: UUID | None = None,
        terminal_recovery_count: int = 0,
    ) -> None:
        super().__init__(
            cast(RunRepository, recoverable_run_repository),
            runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
            router=None,
            task_queue=UnusedQueue(),
        )
        self._recovered = recovered
        self._failing_run_id = failing_run_id
        self._active_run_id = active_run_id
        self._terminal_recovery_count = terminal_recovery_count
        self.terminal_recovery_limits: list[int] = []

    async def recover(self, run_id: UUID) -> SubmittedRun:
        if run_id == self._failing_run_id:
            raise RuntimeError("synthetic recovery failure")
        if run_id == self._active_run_id:
            return SubmittedRun(
                id=run_id,
                tenant_id=uuid4(),
                status=RunStatus.RUNNING,
                mode=TaskMode.DISPATCH,
                decision_token=None,
                version=1,
            )
        self._recovered.append(run_id)
        return SubmittedRun(
            id=run_id,
            tenant_id=uuid4(),
            status=RunStatus.COMPLETED,
            mode=TaskMode.DISPATCH,
            decision_token=None,
            version=1,
        )

    async def recover_terminal_worker_executions(self, limit: int = 100) -> int:
        self.terminal_recovery_limits.append(limit)
        return self._terminal_recovery_count if limit > 0 else 0


class UnusedRuntime:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        del context
        raise AssertionError("recover_running should call the patched recover method")
        yield  # pragma: no cover

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint
        raise AssertionError("not used")

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RecoverableTerminalRuntime(UnusedRuntime):
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.cancelled: list[UUID] = []

    async def cancel_run(self, run_id: UUID) -> None:
        self.cancelled.append(run_id)
        if self.fail:
            raise RuntimeError("runtime cancel failed")


class UnusedQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        del run_id, idempotency_key
        raise AssertionError("not used")
