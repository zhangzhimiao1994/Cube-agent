"""Real local SQL transactions for the opt-in split execution clock.

Only database time, SQLite UTC decoding and PostgreSQL JSON operators are adapted. No
repository, approval/checkpoint guard, clock mutation or worker API is mocked.
SQLite proves transaction/rollback behaviour, not PostgreSQL row-lock concurrency.
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.sqlite import DATETIME
from sqlalchemy.dialects.sqlite.base import SQLiteCompiler
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import Constraint
from sqlalchemy.sql.elements import BinaryExpression
from sqlalchemy.sql.operators import custom_op

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.base import Base
from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunAdmissionEventRow,
    RunAdmissionPoolRow,
    RunApprovalRow,
    RunArtifactRow,
    RunAuthenticatedInputRow,
    RunCheckpointRow,
    RunEventRow,
    RunExecutionClockRow,
    RunInputAuthorizationRow,
    RunOutboxRow,
    RunRow,
    RunStepRow,
    RunUsageRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs import execution_clock_store as store
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runs.execution_clock import (
    ClockSnapshot,
    bind_submission,
    submission_boundary,
)
from agent_hub.runs.repository import RunConflict, RunRecord, RunRepository
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    _freeze_object,
)

APPROVAL = "approval_clock_transaction"
REQUEST = "Offline transaction fixture: preserve the original execution budget."


@dataclass
class SQLClock:
    factory: async_sessionmaker[AsyncSession]
    repository: RunRepository
    now: datetime = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0))
    tenant: UUID = field(default_factory=uuid4)
    actor: UUID = field(default_factory=uuid4)
    token: UUID = field(default_factory=uuid4)
    worker: str = "clock-worker"
    profile: dict[str, object] = field(default_factory=lambda: {
        "config_sha256": "a" * 64, "catalog_sha256": "b" * 64,
        "timeout_seconds": 1000.0,
    })

    @submission_boundary
    async def submit(self, *, legacy: bool = False, bind: bool = True) -> RunRecord:
        route: dict[str, object] = {}
        if not legacy:
            route.update(execution_clock_version=2, runtime_timeout_absolute_seconds=100.0,
                         runtime_timeout_soft_seconds=80.0, approval_review_profile=self.profile)
            if bind:
                async def approved() -> bool:
                    return True
                bind_submission(tenant_id=self.tenant, actor_id=self.actor, request=REQUEST,
                    profile=self.profile, active_seconds=100.0, initial_soft_seconds=80.0,
                    extension_per_unit=10.0, recheck=approved)
        original = _capture_authenticated_input(
            AuthenticatedPrincipal(self.actor, self.tenant, Role.OPERATOR), REQUEST,
            {"mode": "discuss", "execution_clock_version": 1 if legacy else 2})
        return await self.repository.create_run(tenant_id=self.tenant, actor_id=self.actor,
            actor_role=Role.OPERATOR, request=REQUEST, mode=TaskMode.DISCUSS,
            status=RunStatus.QUEUED, idempotency_key=None, enqueue=True,
            routing_decision=route, authenticated_input=original)

    async def claim(self, run: UUID, *, recovery: bool = False) -> RuntimeCheckpoint | None:
        async with self.factory.begin() as session:
            result = await self.repository.claim_for_execution(session, run,
                allow_running_recovery=recovery, worker_id=self.worker,
                worker_lease_token=self.token,
                worker_lease_expires_at=self.now + timedelta(days=1))
            assert isinstance(result, tuple), "claim must execute, not return a terminal record"
            row, checkpoint = result
            assert row.status == RunStatus.RUNNING.value
            return checkpoint

    def checkpoint(self, run: UUID, *, unknown: bool = False) -> RuntimeCheckpoint:
        arguments_hash = hashlib.sha256(b"{}").hexdigest()
        receipt: dict[str, JsonValue] = {
            "status": "waiting_approval", "approval_id": APPROVAL, "replay_safe": False,
            "actor": "implementer", "name": "project.verify", "position": 0,
            "arguments_hash": arguments_hash,
            "idempotency_key": hashlib.sha256(
                f"{run}:implementer:0:project.verify:{arguments_hash}".encode()).hexdigest(),
        }
        ledger: list[JsonValue] = [receipt]
        if unknown:
            # An unresolved earlier operation cannot become quiet just because a
            # later approval receipt and a boolean claim of quiescence exist.
            ledger.insert(0, {"status": "unknown", "name": "workspace.write_text"})
            receipt["position"] = 1
            receipt["idempotency_key"] = hashlib.sha256(
                f"{run}:implementer:1:project.verify:{arguments_hash}".encode()).hexdigest()
        return RuntimeCheckpoint(id=uuid4(), runtime_type="autogen", runtime_version="2",
            tenant_id=self.tenant, run_id=run, mode=TaskMode.DISCUSS,
            state=_freeze_object({"terminal": False, "tool_ledger": ledger}, name="clock checkpoint state"))

    async def save(self, value: RuntimeCheckpoint, *, sequence: int = 1) -> None:
        async with self.factory.begin() as session:
            await self.repository.persist_event(session, tenant_id=self.tenant,
                run_id=value.run_id, event=RunEvent(kind=EventKind.CHECKPOINT_SAVED,
                    sequence=sequence, run_id=value.run_id, checkpoint=value))

    async def waiting(self, run: UUID) -> RunRecord:
        return await self.repository.begin_capability_approval(self.tenant, run,
            approval_id=APPROVAL, approval_fingerprint="c" * 64, actor_id=self.actor)

    async def park(self, run: UUID, *, quiet: bool = True, token: UUID | None = None) -> None:
        await self.repository.park_execution_clock(self.tenant, run,
            worker_id=self.worker, worker_lease_token=self.token if token is None else token,
            quiescent=quiet)

    async def approve(self, run: UUID) -> None:
        async with self.factory() as session:
            row = await session.get(RunRow, run)
            assert row is not None
            version = row.version
        await self.repository.approve_capability_and_enqueue(tenant_id=self.tenant,
            run_id=run, approval_id=APPROVAL, version=version, actor_id=self.actor)

    async def clock(self, run: UUID) -> ClockSnapshot:
        async with self.factory() as session:
            value = await session.get(RunExecutionClockRow, run)
            assert value is not None, "tagged submission must create a durable clock"
            return store.snapshot(value)

    async def count(self, model: type[Base]) -> int:
        async with self.factory() as session:
            value = await session.scalar(select(func.count()).select_from(model))
            assert isinstance(value, int)
            return value


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SQLClock]:
    # PostgreSQL TIMESTAMPTZ returns aware values; SQLite DATETIME drops tzinfo.
    # Adapt decoding at the test dialect boundary, leaving all lease comparisons
    # and repository guards unchanged. monkeypatch restores it after this fixture.
    original_datetime_processor = DATETIME.result_processor

    def utc_datetime_processor(sql_type: DATETIME, dialect: Dialect,
                               coltype: object) -> Callable[[object], object]:
        decode = original_datetime_processor(sql_type, dialect, coltype)

        def as_utc(value: object) -> object:
            decoded = decode(value) if decode is not None else value
            if isinstance(decoded, datetime):
                return (decoded.replace(tzinfo=UTC) if decoded.tzinfo is None
                        else decoded.astimezone(UTC))
            return decoded

        return as_utc

    monkeypatch.setattr(DATETIME, "result_processor", utc_datetime_processor)
    # Keep actual SQL INSERT/UPDATE and WHERE clauses, translating only the two
    # PostgreSQL JSONB operators absent from SQLite. No approval rows are seeded.
    original = cast(Callable[..., str], SQLiteCompiler.visit_custom_op_binary)

    def json_operator(compiler: SQLiteCompiler, binary: BinaryExpression[object],
                      operator: custom_op[object], **kwargs: object) -> str:
        left = compiler.process(binary.left, **kwargs)
        right = compiler.process(binary.right, **kwargs)
        if isinstance(binary.left.type, JSONB) and operator.opstring == "||":
            return f"json_patch({left}, {right})"
        if isinstance(binary.left.type, JSONB) and operator.opstring == "?":
            return f"(json_type({left}, '$.' || {right}) IS NOT NULL)"
        return original(compiler, binary, operator, **kwargs)

    monkeypatch.setattr(SQLiteCompiler, "visit_custom_op_binary", json_operator)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    for model in (RunRow, RunAuthenticatedInputRow, RunInputAuthorizationRow,
                  RunExecutionClockRow, RunOutboxRow, RunEventRow, RunCheckpointRow,
                  RunApprovalRow, RunAdmissionPoolRow, RunAdmissionAttemptRow, RunAdmissionEventRow, RunArtifactRow, RunStepRow, RunUsageRow):
        source = model.__table__
        assert isinstance(source, Table)
        columns = [Column(c.name, JSON() if isinstance(c.type, JSONB)
            else String(32) if isinstance(c.type, PGUUID) else c.type,
            primary_key=c.primary_key, nullable=c.nullable, server_default=c.server_default)
            for c in source.columns]
        constraints: list[Constraint] = [UniqueConstraint(*(c.name for c in item.columns))
            for item in source.constraints if isinstance(item, UniqueConstraint)]
        # Retain clock accounting and both composite identity FKs. Other model
        # regex checks/FKs are omitted as in the existing local SQL fixtures.
        if model is RunExecutionClockRow:
            constraints.extend(CheckConstraint(str(item.sqltext)) for item in source.constraints
                               if isinstance(item, CheckConstraint))
            constraints.extend(ForeignKeyConstraint([c.name for c in item.columns],
                [element.target_fullname for element in item.elements], name=item.name)
                for item in source.constraints if isinstance(item, ForeignKeyConstraint)
                and len(item.columns) > 1)
        Table(model.__tablename__, metadata, *columns, *constraints)
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert await connection.scalar(select(func.sqlite_version())) is not None
            assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 1
            await connection.run_sync(metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        fixture = SQLClock(factory, RunRepository(factory))

        async def database_time(session: AsyncSession) -> datetime:
            assert session.in_transaction(), "clock observations must occur inside the SQL transaction"
            return fixture.now

        monkeypatch.setattr(store, "database_now", database_time)
        yield fixture
    finally:
        await engine.dispose()


async def test_submission_checkpoint_quiet_wait_resume_and_duplicate_event_charge_once(db: SQLClock) -> None:
    start = db.now
    run = await db.submit()
    created = await db.clock(run.id)
    assert created.hard_deadline == start + timedelta(seconds=1000)
    assert created.spent_seconds == 0 and created.active_since is None
    # Mill additions on top of Avicenna's 811397... fixture: one closed seal
    # must protect fixed H and its persisted creation anchor before any pool exists.
    for changed in ("hard_deadline", "created_at"):
        with pytest.raises(ValueError, match="execution clock original binding changed"):
            async with db.factory.begin() as session:
                row = await session.get(RunRow, run.id)
                clock = await session.get(RunExecutionClockRow, run.id)
                assert row is not None and clock is not None
                if changed == "hard_deadline":
                    clock.hard_deadline += timedelta(seconds=1)
                else:
                    row.created_at += timedelta(seconds=1)
                await session.flush()
                await store.locked(session, row)
        assert (await db.clock(run.id)).hard_deadline == created.hard_deadline
    async with db.factory() as session:
        clock = await session.get(RunExecutionClockRow, run.id)
        original = await session.get(RunInputAuthorizationRow, run.id)
        assert clock is not None and original is not None
        assert clock.authorization_turn_id == original.turn_id
        marker = clock.binding_sha256
    for forged_marker in (None, "f" * 64):
        with pytest.raises(IntegrityError):
            async with db.factory.begin() as session:
                row = await session.get(RunRow, run.id)
                assert row is not None
                row.execution_clock_sha256 = forged_marker
                await session.flush()
    async with db.factory() as session:
        row = await session.get(RunRow, run.id)
        assert row is not None and row.execution_clock_sha256 == marker

    db.now = start + timedelta(seconds=2)
    assert await db.claim(run.id) is None
    assert (await db.clock(run.id)).active_since == db.now
    db.now = start + timedelta(seconds=12)
    await db.waiting(run.id)
    db.now = start + timedelta(seconds=15)
    checkpoint = db.checkpoint(run.id)
    await db.save(checkpoint)
    db.now = start + timedelta(seconds=20)
    await db.park(run.id)
    parked = await db.clock(run.id)
    assert parked.spent_seconds == 18 and parked.active_since is None
    db.now = start + timedelta(seconds=500)
    await db.park(run.id)
    assert await db.clock(run.id) == parked, "duplicate quiet observation must not change accounting"
    await db.approve(run.id)
    db.now = start + timedelta(seconds=520)
    restored = await db.claim(run.id)
    assert restored is not None and restored.id == checkpoint.id
    resumed = await db.clock(run.id)
    assert resumed.hard_deadline == created.hard_deadline
    assert resumed.spent_seconds == 18 and resumed.active_since == db.now
    assert resumed.remaining(db.now).active_seconds == 82
    assert resumed.remaining(db.now).soft_seconds == 62
    assert resumed.remaining(db.now).authorization_seconds == 480
    db.now = start + timedelta(seconds=527)
    next_checkpoint = db.checkpoint(run.id)
    await db.save(next_checkpoint, sequence=2)
    once = await db.clock(run.id)
    assert once.spent_seconds == 25
    db.now = start + timedelta(seconds=530)
    await db.save(next_checkpoint, sequence=2)
    assert await db.clock(run.id) == once, "duplicate persisted event must not debit a second time"
    assert once.remaining(db.now).active_seconds == 72
    assert await db.count(RunCheckpointRow) == 2
    assert await db.count(RunEventRow) == 2


@pytest.mark.parametrize("unsafe", ["unknown", "not_quiet", "live_worker", "wrong_owner", "missing_checkpoint"])
async def test_unproven_approval_quiescence_never_refunds_execution(db: SQLClock, unsafe: str) -> None:
    start = db.now
    run = await db.submit()
    await db.claim(run.id)
    db.now = start + timedelta(seconds=5)
    await db.waiting(run.id)
    if unsafe != "missing_checkpoint":
        db.now = start + timedelta(seconds=10)
        await db.save(db.checkpoint(run.id, unknown=unsafe == "unknown"))
    if unsafe == "live_worker":
        # Simulate a concurrent/lingering DB worker ownership observation after
        # approval was recorded. A quiet boolean cannot override the real lease.
        async with db.factory.begin() as session:
            row = await session.get(RunRow, run.id)
            assert row is not None
            RunRepository.record_worker_lease(row, worker_id=db.worker,
                worker_lease_token=db.token, worker_lease_expires_at=start + timedelta(days=1))
    before = await db.clock(run.id)
    db.now = start + timedelta(seconds=40)
    with pytest.raises((RunConflict, ValueError)):
        await db.park(run.id, quiet=unsafe != "not_quiet",
                      token=uuid4() if unsafe == "wrong_owner" else None)
    after = await db.clock(run.id)
    assert after == before, "failed park must roll back every clock mutation"
    assert after.active_since is not None
    assert after.remaining(db.now).active_seconds == 60


async def test_legacy_run_cannot_acquire_a_split_clock_on_claim_or_checkpoint(db: SQLClock) -> None:
    run = await db.submit(legacy=True)
    await db.claim(run.id)
    await db.waiting(run.id)
    await db.save(db.checkpoint(run.id))
    await db.park(run.id)
    await db.approve(run.id)
    await db.claim(run.id)
    assert await db.count(RunExecutionClockRow) == 0


async def test_public_tag_without_private_submission_rolls_back_run_and_original(db: SQLClock) -> None:
    with pytest.raises(ValueError, match="execution clock"):
        await db.submit(bind=False)
    assert await db.count(RunRow) == 0
    assert await db.count(RunAuthenticatedInputRow) == 0
    assert await db.count(RunExecutionClockRow) == 0


async def test_parked_wait_never_renews_original_authorization_deadline(db: SQLClock) -> None:
    start = db.now
    run = await db.submit()
    await db.claim(run.id)
    await db.waiting(run.id)
    db.now = start + timedelta(seconds=10)
    await db.save(db.checkpoint(run.id))
    await db.park(run.id)
    parked = await db.clock(run.id)
    await db.approve(run.id)
    db.now = start + timedelta(seconds=1001)
    dispatches = await db.count(RunOutboxRow)
    async with db.factory.begin() as session:
        result = await db.repository.claim_for_execution(session, run.id,
            allow_running_recovery=False, worker_id=db.worker, worker_lease_token=db.token,
            worker_lease_expires_at=db.now + timedelta(days=1))
        assert isinstance(result, RunRecord) and result.status is RunStatus.FAILED
        terminal_version = result.version
    expired = await db.clock(run.id)
    assert expired.spent_seconds == parked.spent_seconds
    assert expired.hard_deadline == parked.hard_deadline
    assert expired.active_since is None
    async with db.factory() as session:
        row = await session.get(RunRow, run.id)
        assert row is not None and row.status == RunStatus.FAILED.value
        assert row.worker_lease_token is None
        failure = await session.scalar(select(RunEventRow).where(
            RunEventRow.run_id == run.id, RunEventRow.kind == EventKind.RUNTIME_FAILED.value))
        assert failure is not None
        assert failure.payload["reason"] == "run execution or authorization budget exhausted"
    async with db.factory.begin() as session:
        again = await db.repository.claim_for_execution(session, run.id,
            allow_running_recovery=True, worker_id=db.worker, worker_lease_token=db.token,
            worker_lease_expires_at=db.now + timedelta(days=1))
        assert isinstance(again, RunRecord) and again.status is RunStatus.FAILED
        assert again.version == terminal_version
    assert await db.count(RunOutboxRow) == dispatches


async def test_unclosed_crashed_interval_is_charged_on_running_recovery(db: SQLClock) -> None:
    start = db.now
    run = await db.submit()
    await db.claim(run.id)
    async with db.factory.begin() as session:
        row = await session.get(RunRow, run.id)
        assert row is not None
        row.worker_lease_expires_at = start - timedelta(seconds=1)
    db.now = start + timedelta(seconds=40)
    db.token = uuid4()
    await db.claim(run.id, recovery=True)
    recovered = await db.clock(run.id)
    assert recovered.spent_seconds == 40 and recovered.active_since == db.now
    assert recovered.remaining(db.now).active_seconds == 60
    assert recovered.hard_deadline == start + timedelta(seconds=1000)
