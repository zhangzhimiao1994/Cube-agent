"""Typed local repair seams. Real DAOs run; no PG/transaction-lock proof."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TypedDict
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.engine.result import IteratorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.orm.unitofwork import UOWTransaction
from sqlalchemy.sql import Select

from agent_hub.db.models import RunOutboxRow, RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runtime.contracts import RuntimeCheckpoint
from tests.integration.runtime._model_review_repair_typed_fixture import (
    RepairDB,
    RepairFixture,
    checkpoint_row,
    claim,
    matches,
    pool_values,
    productive_checkpoint,
    repair_fixture,
    service_runtime_context,
)
from tests.unit.harness.review_typed_fixture import StreamRuntimeStub, result_rows
from tests.unit.runs.project_auto_repair_typed_fixture import present

__all__ = ['RepairDB', 'RepairFixture', 'checkpoint_row', 'claim', 'matches', 'pool_values', 'productive_checkpoint', 'repair_fixture', 'service_runtime_context']


class ContinuityRuntime(StreamRuntimeStub):
    mode = TaskMode.DISCUSS


class AsyncValue[T]:
    def __init__(self, value: T) -> None:
        self.return_value = value
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def __call__(self, *args: object, **kwargs: object) -> T:
        self.calls.append((args, kwargs))
        return self.return_value

    def assert_not_called(self) -> None:
        assert not self.calls


@dataclass
class AddedRows:
    rows: list[RunOutboxRow] = field(default_factory=list)

    def __call__(self, value: RunOutboxRow) -> None:
        self.rows.append(value)

    def assert_called_once(self) -> None:
        assert len(self.rows) == 1

    def assert_not_called(self) -> None:
        assert not self.rows


@dataclass
class SessionWitness:
    """Observe actual AsyncSession.add events, not a fabricated SQL session."""
    add: AddedRows = field(default_factory=AddedRows)


class AutoRepairSession(Session):
    pass


@event.listens_for(AutoRepairSession, 'do_orm_execute')
def selected_run(state: ORMExecuteState) -> IteratorResult[tuple[object, ...]]:
    owner = state.session.info['repair_case']
    assert isinstance(owner, AutoRepairRepository)
    assert isinstance(state.statement, Select)
    assert state.statement.column_descriptions[0].get('entity') is RunRow
    return result_rows([(owner.row,)])


@event.listens_for(AutoRepairSession, 'after_attach')
def observe_add(session: Session, value: object) -> None:
    owner = session.info['repair_case']
    assert isinstance(owner, AutoRepairRepository)
    assert isinstance(value, RunOutboxRow)
    owner.witness.add(value)


@event.listens_for(AutoRepairSession, 'before_flush')
def no_database_write(session: Session, context: UOWTransaction, objects: object) -> None:
    session.expunge_all()


class AutoRepairRepository(RunRepository):
    def __init__(self, record: RunRecord, checkpoint: RuntimeCheckpoint, *, row: RunRow | None = None) -> None:
        self.record = record
        self.row = row if row is not None else RunRow(
            id=record.id, tenant_id=record.tenant_id, actor_id=record.actor_id,
            actor_role=None if record.actor_role is None else record.actor_role.value,
            request=record.request, mode=present(record.mode).value, status=record.status.value,
            version=record.version, created_at=record.created_at,
            routing_decision=record.routing_decision,
        )
        self.witness = SessionWitness()
        self.checkpoint_lookup = AsyncValue[RuntimeCheckpoint | None](checkpoint)
        self.recovery_lookup = AsyncValue(False)
        self.sequence_lookup = AsyncValue(5)
        super().__init__(async_sessionmaker(
            expire_on_commit=False, sync_session_class=AutoRepairSession,
            info={'repair_case': self},
        ))

    async def get(self, tenant_id: UUID, run_id: UUID, *, actor_id: UUID | None = None) -> RunRecord:
        return self.record

    async def latest_checkpoint(self, session: AsyncSession, *, tenant_id: UUID, run_id: UUID) -> RuntimeCheckpoint | None:
        return await self.checkpoint_lookup(session, tenant_id=tenant_id, run_id=run_id)

    async def next_event_sequence(self, session: AsyncSession, run_id: UUID) -> int:
        return await self.sequence_lookup(session, run_id)

    async def _recovery_blocked_after_checkpoint(
        self, session: AsyncSession, *, run_id: UUID, minimum_event_sequence: int = 0,
        pre_model_original_capture: bool = False,
    ) -> bool:
        return await self.recovery_lookup(session, run_id=run_id,
            minimum_event_sequence=minimum_event_sequence,
            pre_model_original_capture=pre_model_original_capture)




class RecordChange(TypedDict, total=False):
    worker_id: str | None
    worker_heartbeat_at: datetime | None
    worker_lease_token: UUID | None
    worker_lease_expires_at: datetime | None
    status: RunStatus
    blocked_by_run_id: UUID | None
    routing_decision: dict[str, object] | None
