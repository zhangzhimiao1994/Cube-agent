from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRepository
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


class RecoverableRunRepository:
    def __init__(self, candidates: tuple[UUID, ...]) -> None:
        self._candidates = candidates

    async def running_for_recovery(self, limit: int, *, now: datetime) -> tuple[UUID, ...]:
        del now
        return self._candidates[:limit]


class RecoverRunningService(RunService):
    def __init__(
        self,
        *,
        recoverable_run_repository: RecoverableRunRepository,
        recovered: list[UUID],
        failing_run_id: UUID,
        active_run_id: UUID | None = None,
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


class UnusedQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        del run_id, idempotency_key
        raise AssertionError("not used")
