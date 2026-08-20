from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRecord
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")


@dataclass(slots=True)
class FakeRunRow:
    id: UUID
    tenant_id: UUID
    actor_id: UUID | None
    request: str
    mode: str | None
    status: str
    version: int
    created_at: datetime
    routing_decision: dict[str, object] | None


class FakeTransaction:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def begin(self) -> FakeTransaction:
        return self


class ExecutableFakeRepository:
    def __init__(self, *, routing_decision: dict[str, object]) -> None:
        self.run_id = uuid4()
        self.row = FakeRunRow(
            id=self.run_id,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            request="run evolution round",
            mode=TaskMode.DISPATCH.value,
            status=RunStatus.QUEUED.value,
            version=1,
            created_at=datetime.now(UTC),
            routing_decision=routing_decision,
        )
        self.events: list[RunEvent] = []

    async def run_transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def claim_for_execution(
        self,
        session: FakeTransaction,
        run_id: UUID,
        *,
        allow_running_recovery: bool,
    ) -> tuple[FakeRunRow, RuntimeCheckpoint | None] | RunRecord:
        del session, allow_running_recovery
        assert run_id == self.run_id
        self.row.status = RunStatus.RUNNING.value
        self.row.version += 1
        return self.row, None

    async def get_for_update(self, session: FakeTransaction, run_id: UUID) -> FakeRunRow:
        del session
        assert run_id == self.run_id
        return self.row

    async def persist_event(
        self,
        session: FakeTransaction,
        *,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
    ) -> None:
        del session
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        self.events.append(event)

    async def next_event_sequence(self, session: FakeTransaction, run_id: UUID) -> int:
        del session
        assert run_id == self.run_id
        return max((event.sequence for event in self.events), default=0) + 1

    async def update_status(
        self,
        tenant_id: UUID,
        run_id: UUID,
        status: RunStatus,
        *,
        mode: TaskMode | None = None,
    ) -> RunRecord:
        del mode
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        self.row.status = status.value
        self.row.version += 1
        return self._record()

    async def fail_run(self, run_id: UUID, *, reason: str) -> RunRecord:
        del reason
        assert run_id == self.run_id
        self.row.status = RunStatus.FAILED.value
        self.row.version += 1
        return self._record()

    async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        return self._record()

    def _record(self) -> RunRecord:
        return RunRecord(
            id=self.row.id,
            tenant_id=self.row.tenant_id,
            actor_id=self.row.actor_id,
            request=self.row.request,
            mode=None if self.row.mode is None else TaskMode(self.row.mode),
            status=RunStatus(self.row.status),
            version=self.row.version,
            created_at=self.row.created_at,
            routing_decision=None
            if self.row.routing_decision is None
            else dict(self.row.routing_decision),
        )


class RuntimeCompletes:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=context.run_id)

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RuntimeReportsCapacityPressure:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.STEP_FAILED,
            sequence=1,
            run_id=context.run_id,
            actor="planner",
            step_id="planner_step",
            reason="model gateway failed: model capacity unavailable",
        )
        yield RunEvent(
            kind=EventKind.RUNTIME_FAILED,
            sequence=2,
            run_id=context.run_id,
            reason="model gateway failed: model capacity unavailable",
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RecordingHook:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID | None,
        run_id: UUID,
        status: RunStatus,
        mode: TaskMode | None,
        routing_decision: dict[str, object],
    ) -> None:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "run_id": run_id,
                "status": status,
                "mode": mode,
                "routing_decision": routing_decision,
            }
        )
        if self.fail:
            raise RuntimeError("hook failed")


@pytest.mark.asyncio
async def test_execute_notifies_terminal_hooks_after_completed_run() -> None:
    repository = ExecutableFakeRepository(
        routing_decision={"source": "evolution", "evolution_run_id": "evolution_1"}
    )
    hook = RecordingHook()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
        terminal_run_hooks=(hook,),
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.COMPLETED
    assert hook.calls == [
        {
            "tenant_id": TENANT_ID,
            "actor_id": ACTOR_ID,
            "run_id": repository.run_id,
            "status": RunStatus.COMPLETED,
            "mode": TaskMode.DISPATCH,
            "routing_decision": {"source": "evolution", "evolution_run_id": "evolution_1"},
        }
    ]


@pytest.mark.asyncio
async def test_terminal_hook_failure_does_not_fail_completed_run() -> None:
    repository = ExecutableFakeRepository(
        routing_decision={"source": "evolution", "evolution_run_id": "evolution_1"}
    )
    hook = RecordingHook(fail=True)
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
        terminal_run_hooks=(hook,),
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.COMPLETED
    assert hook.calls


@pytest.mark.asyncio
async def test_execute_persists_observer_notice_for_capacity_pressure() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeReportsCapacityPressure(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.FAILED
    notices = [event for event in repository.events if event.kind == "observer.notice"]
    assert len(notices) == 1
    notice = notices[0]
    assert [event.kind for event in repository.events] == [
        EventKind.STEP_FAILED,
        EventKind.RUNTIME_FAILED,
        "observer.notice",
    ]
    assert notice.sequence == 3
    assert notice.payload["trigger"] == "model_capacity_pressure"
    assert notice.payload["action"] == "reschedule_or_reassign_model"
    assert notice.payload["source_sequence"] == 1
    assert "message" not in notice.payload
    assert "prompt" not in notice.payload
