"""Typed, in-memory IO fixture for the project-repair fence tests only.

The real RunRepository transaction methods execute unchanged. A real
AsyncSession/Session supplies the nominal contract; ORM execute and flush events
provide the same explicitly scoped in-memory rows as the original fixture.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import TypeGuard, overload
from uuid import UUID, uuid4

from sqlalchemy import event
from sqlalchemy.engine import Result
from sqlalchemy.engine.result import IteratorResult, SimpleResultMetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.orm.unitofwork import UOWTransaction
from sqlalchemy.sql import Select, operators, visitors
from sqlalchemy.sql.elements import BinaryExpression, BindParameter

from agent_hub.db.models import RunCheckpointRow, RunOutboxRow, RunRow, RuntimeArtifactRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import ConversationContextItem, RunConflict, RunRecord, RunRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    _freeze_object,
)
from tests.unit.runs.test_terminal_hooks import ACTOR_ID, TENANT_ID


def present[T](value: T | None) -> T:
    assert value is not None
    return value


def _string_keys(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def mutable(value: object) -> dict[str, object]:
    assert _string_keys(value)
    return value


@overload
def mapping[T](value: Mapping[str, T]) -> Mapping[str, T]: ...
@overload
def mapping(value: object) -> Mapping[str, object]: ...
def mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    assert all(isinstance(key, str) for key in value)
    return value


@overload
def sequence[T](value: tuple[T, ...] | list[T]) -> Sequence[T]: ...
@overload
def sequence(value: object) -> Sequence[object]: ...
def sequence(value: object) -> Sequence[object]:
    assert isinstance(value, (tuple, list))
    return value


def integer(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def number(value: object) -> int | float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return value


def string(value: object) -> str:
    assert isinstance(value, str)
    return value


def json_map(value: object) -> Mapping[str, JsonValue]:
    return _freeze_object(value, name="project repair fixture")


def ledger(value: object) -> tuple[dict[str, JsonValue], ...]:
    return tuple(dict(json_map(item)) for item in sequence(value))


def registry(value: object) -> dict[str, str]:
    return {key: string(pin) for key, pin in mapping(value).items()}


def result_rows[T](values: Sequence[T]) -> IteratorResult[tuple[T]]:
    return IteratorResult(SimpleResultMetaData(["fixture_row"]), iter((value,) for value in values))


class UnexpectedTaskQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        raise AssertionError("repair continuation must use the transactional outbox")


class RepairSession(Session):
    pass


@event.listens_for(RepairSession, "do_orm_execute")
def in_memory_rows(state: ORMExecuteState) -> Result[tuple[RunRow]] | Result[tuple[RuntimeArtifactRow]] | Result[tuple[RunCheckpointRow]]:
    repository = state.session.info["repository"]
    assert isinstance(repository, RepairRepository)
    statement = state.statement
    assert isinstance(statement, Select)
    entity = statement.column_descriptions[0].get("entity")
    if entity is RunRow:
        return result_rows([repository.row])
    if entity is RunCheckpointRow:
        predicates = [clause for clause in visitors.iterate(statement.whereclause)
            if isinstance(clause, BinaryExpression) and clause.operator is operators.eq
            and clause.left.shares_lineage(RunCheckpointRow.__table__.c.state_sha256)
            and isinstance(clause.right, BindParameter)]
        assert len(predicates) == 1
        predicate = predicates[0]
        assert isinstance(predicate.right, BindParameter)
        pin = string(predicate.right.value)
        candidates = [entry.checkpoint for entry in repository.event_log
                      if entry.checkpoint is not None and entry.checkpoint.state_sha256 == pin]
        return result_rows([RunCheckpointRow(id=checkpoint.id, run_id=repository.run_id,
            tenant_id=repository.row.tenant_id, sequence=index, runtime_type=checkpoint.runtime_type,
            runtime_version=checkpoint.runtime_version, mode=checkpoint.mode.value,
            state=mutable(checkpoint.to_payload()["state"]), state_sha256=checkpoint.state_sha256,
            payload=checkpoint.to_payload()) for index, checkpoint in enumerate(candidates[-1:])])
    assert entity is RuntimeArtifactRow, "fixture must not service another SQL query"
    id_filters = [
        clause for clause in visitors.iterate(statement.whereclause)
        if isinstance(clause, BinaryExpression) and clause.operator is operators.in_op
        and clause.left.shares_lineage(RuntimeArtifactRow.__table__.c.id)
        and isinstance(clause.right, BindParameter)
    ]
    assert len(id_filters) == 1, "private artifact query must select explicit artifact ids"
    clause = id_filters[0]
    assert isinstance(clause.right, BindParameter)
    requested = sequence(clause.right.value)
    assert all(isinstance(identifier, UUID) for identifier in requested)
    return result_rows([row for row in repository.private_rows if row.id in requested])


@event.listens_for(RepairSession, "before_flush")
def retain_outbox(session: Session, flush_context: UOWTransaction, instances: Sequence[object] | None) -> None:
    repository = session.info["repository"]
    assert isinstance(repository, RepairRepository)
    for value in session.new:
        assert isinstance(value, RunOutboxRow), "only the real repair transaction may add outbox rows"
        repository.outbox.append(value)
    session.expunge_all()


class RepairRepository(RunRepository):
    def __init__(self, *, routing_decision: dict[str, object]) -> None:
        super().__init__(async_sessionmaker(
            expire_on_commit=False, sync_session_class=RepairSession, info={"repository": self}))
        self.outbox: list[RunOutboxRow] = []
        self.private_rows: list[RuntimeArtifactRow] = []
        self.run_id = uuid4()
        self.row = RunRow(
            blocked_by_run_id=None,
            id=self.run_id,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            actor_role=None,
            request="run evolution round",
            mode=TaskMode.DISPATCH.value,
            status=RunStatus.QUEUED.value,
            version=1,
            created_at=datetime.now(UTC),
            routing_decision=routing_decision,
        )
        self.event_log: list[RunEvent] = []
        self.persisted_artifacts: list[Artifact] = []
        self.conversation_context_calls: list[dict[str, object]] = []
        self.lease_renewed = asyncio.Event()
        self.lease_renewals: list[datetime] = []

    async def run_transaction(self) -> AsyncSession:
        return self._session_factory()

    async def claim_for_execution(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        allow_running_recovery: bool,
        worker_id: str | None = None,
        worker_lease_token: UUID | None = None,
        worker_lease_expires_at: datetime | None = None,
    ) -> tuple[RunRow, RuntimeCheckpoint | None] | RunRecord:
        del session, allow_running_recovery
        assert run_id == self.run_id
        if self.row.status in {
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELLED.value,
        }:
            return self._record(self.row)
        self.row.status = RunStatus.RUNNING.value
        self.row.worker_id = worker_id
        self.row.worker_lease_token = worker_lease_token
        self.row.worker_lease_expires_at = worker_lease_expires_at
        self.row.worker_heartbeat_at = datetime.now(UTC)
        self.row.version += 1
        return self.row, None

    async def get_for_update(self, session: AsyncSession, run_id: UUID) -> RunRow:
        del session
        assert run_id == self.run_id
        return self.row

    async def renew_active_worker_lease(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        worker_id: str,
        worker_lease_token: UUID,
        worker_lease_expires_at: datetime,
    ) -> bool:
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        if self.row.status != RunStatus.RUNNING.value:
            return False
        if self.row.worker_id != worker_id or self.row.worker_lease_token != worker_lease_token:
            return False
        self.row.worker_lease_expires_at = worker_lease_expires_at
        self.row.worker_heartbeat_at = datetime.now(UTC)
        self.lease_renewals.append(worker_lease_expires_at)
        self.lease_renewed.set()
        return True

    async def release_worker_execution(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        worker_id: str,
        worker_lease_token: UUID,
        complete_if_running: bool = False,
    ) -> RunRecord:
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        if self.row.worker_id != worker_id or self.row.worker_lease_token != worker_lease_token:
            raise RuntimeError("worker execution ownership changed")
        if complete_if_running and self.row.status == RunStatus.RUNNING.value:
            self.row.status = RunStatus.COMPLETED.value
        self.row.worker_id = None
        self.row.worker_lease_token = None
        self.row.worker_lease_expires_at = None
        self.row.worker_heartbeat_at = None
        self.row.version += 1
        return self._record(self.row)

    async def persist_event(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
        routing_decision: Mapping[str, JsonValue] | None = None,
    ) -> None:
        del session, routing_decision
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        self.event_log.append(event)
        if event.artifact is not None:
            self.persisted_artifacts.append(event.artifact)

    async def next_event_sequence(self, session: AsyncSession, run_id: UUID) -> int:
        del session
        assert run_id == self.run_id
        return max((event.sequence for event in self.event_log), default=0) + 1

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
        return self._record(self.row)

    async def fail_run(
        self,
        run_id: UUID,
        *,
        reason: str,
        worker_id: str | None = None,
        worker_lease_token: UUID | None = None,
    ) -> RunRecord:
        del worker_id, worker_lease_token
        assert run_id == self.run_id
        if self.row.status in {
            RunStatus.WAITING_APPROVAL.value,
            RunStatus.PAUSED.value,
            RunStatus.CANCELLED.value,
        }:
            return self._record(self.row)
        sequence = max((event.sequence for event in self.event_log), default=0) + 1
        self.event_log.append(
            RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=sequence,
                run_id=run_id,
                reason=reason,
            )
        )
        self.row.status = RunStatus.FAILED.value
        self.row.version += 1
        return self._record(self.row)

    async def mark_worker_execution_exited(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        worker_id: str,
        worker_lease_token: UUID,
        exited_at: datetime,
    ) -> RunRecord:
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        if self.row.worker_id != worker_id or self.row.worker_lease_token != worker_lease_token:
            raise RunConflict("run worker execution ownership changed")
        self.row.worker_lease_expires_at = exited_at
        self.row.worker_heartbeat_at = None
        self.row.version += 1
        return self._record(self.row)

    async def record_self_repair_decision(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
        proposal: dict[str, object] | None,
        decision_token: str | None,
    ) -> None:
        await self.persist_event(session, tenant_id=tenant_id, run_id=run_id, event=event)
        if proposal is not None:
            routing_decision = (
                {} if self.row.routing_decision is None else dict(self.row.routing_decision)
            )
            self.row.routing_decision = {
                **routing_decision,
                "approval_kind": "self_repair",
                "decision_token": decision_token,
                "repair_proposal": proposal,
            }
            if proposal.get("pauses_run") is True:
                self.row.status = RunStatus.WAITING_APPROVAL.value
            self.row.version += 1

    async def get(self, tenant_id: UUID, run_id: UUID, *, actor_id: UUID | None = None) -> RunRecord:
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        return self._record(self.row)

    async def conversation_context(
        self,
        tenant_id: UUID,
        conversation_id: str,
        *,
        before_run_id: UUID,
        limit: int | None = 32,
        query: str | None = None,
        candidate_limit: int | None = None,
    ) -> tuple[ConversationContextItem, ...]:
        assert tenant_id == TENANT_ID
        assert before_run_id == self.run_id
        assert conversation_id
        self.conversation_context_calls.append(
            {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "before_run_id": before_run_id,
                "query": query,
                "candidate_limit": candidate_limit,
            }
        )
        return ()

    async def events(self, tenant_id: UUID, run_id: UUID, *, actor_id: UUID | None = None) -> tuple[dict[str, object], ...]:
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        return tuple(
            {**event.to_payload(), "created_at": datetime.now(UTC).isoformat()}
            for event in self.event_log
        )

    async def raw_events(self, tenant_id: UUID, run_id: UUID) -> tuple[RunEvent, ...]:
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        return tuple(self.event_log)



def present_checkpoint(value: object) -> RuntimeCheckpoint:
    assert isinstance(value, RuntimeCheckpoint)
    return value


def present_artifact(value: object) -> Artifact:
    assert isinstance(value, Artifact)
    return value


def closable(stream: AsyncIterator[RunEvent]) -> AsyncGenerator[RunEvent, None]:
    assert isinstance(stream, AsyncGenerator)
    return stream


def mutable_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: mutable_json(item) for key, item in mapping(value).items()}
    if isinstance(value, (tuple, list)):
        return [mutable_json(item) for item in value]
    assert value is None or isinstance(value, (str, bool, int, float))
    return value
