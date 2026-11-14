"""Real PostgreSQL clock accounting and FK protection on the integration test DB.

The existing integration fixture migrates and cleans its private database. No
SQLite compiler, DB-time stub, model call or external worker is used here. The
submission-policy recheck is a deterministic test authority, not qualification.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.models import (
    RunAuthenticatedInputRow,
    RunExecutionClockRow,
    RunInputAuthorizationRow,
    RunRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs import execution_clock_schema as clock_schema
from agent_hub.runs import execution_clock_store as clocks
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runs.execution_clock import (
    ClockSnapshot,
    bind_submission,
    execution_boundary,
    submission_boundary,
)
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint

pytestmark = pytest.mark.integration


async def _stored_clock(
    factory: async_sessionmaker[AsyncSession], run_id: UUID,
) -> tuple[ClockSnapshot, UUID]:
    async with factory() as session:
        row = await session.get(RunExecutionClockRow, run_id)
        assert row is not None
        return clocks.snapshot(row), row.authorization_turn_id


@submission_boundary
@execution_boundary
async def test_postgres_submission_park_resume_preserves_h_and_protected_original(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = auth_session_factory
    async with factory() as session:
        connection = await session.connection()
        assert connection.dialect.name == "postgresql", "this test requires actual PostgreSQL"
        assert str(await session.scalar(text("SELECT version()"))).startswith("PostgreSQL")

    tenant, actor, token = uuid4(), uuid4(), uuid4()
    worker = "postgres-clock-worker"
    request = "Inspect the existing Python project without replaying unknown work."
    profile: dict[str, object] = {
        "config_sha256": "a" * 64, "catalog_sha256": "b" * 64, "timeout_seconds": 600.0,
    }
    rechecks: list[bool] = []

    async def approved() -> bool:
        rechecks.append(True)
        return True

    bind_submission(tenant_id=tenant, actor_id=actor, request=request, profile=profile,
        active_seconds=120.0, initial_soft_seconds=90.0, extension_per_unit=5.0, recheck=approved)
    original = _capture_authenticated_input(AuthenticatedPrincipal(actor, tenant, Role.OPERATOR),
        request, {"mode": "discuss", "execution_clock_version": 2})
    repository = RunRepository(factory)
    run = await repository.create_run(tenant_id=tenant, actor_id=actor, actor_role=Role.OPERATOR,
        request=request, mode=TaskMode.DISCUSS, status=RunStatus.QUEUED, idempotency_key=None,
        enqueue=True, routing_decision={"execution_clock_version": 2,
            "runtime_timeout_absolute_seconds": 120.0, "runtime_timeout_soft_seconds": 90.0,
            "approval_review_profile": profile}, authenticated_input=original)
    created, turn = await _stored_clock(factory, run.id)
    assert rechecks and created.hard_deadline == run.created_at + timedelta(seconds=600)
    assert created.active_since is None and created.spent_seconds == 0

    # PostgreSQL, not an in-memory metadata assertion, rejects witness mutation.
    with pytest.raises(IntegrityError) as witness_error:
        async with factory.begin() as session:
            parent = await session.get(RunRow, run.id)
            assert parent is not None
            parent.execution_clock_sha256 = "f" * 64
            await session.flush()
    assert "fk_execution_clock_witness" in str(witness_error.value.orig)

    # Remove the old authorization child in this rolled-back transaction so the
    # failing constraint specifically proves the new clock -> original FK.
    with pytest.raises(IntegrityError) as original_error:
        async with factory.begin() as session:
            await session.execute(delete(RunInputAuthorizationRow).where(
                RunInputAuthorizationRow.run_id == run.id))
            await session.execute(delete(RunAuthenticatedInputRow).where(
                RunAuthenticatedInputRow.turn_id == turn))
    assert "fk_execution_clock_original" in str(original_error.value.orig)

    with pytest.raises(ValueError, match="execution clock original binding changed"):
        async with factory.begin() as session:
            parent = await session.get(RunRow, run.id)
            clock = await session.get(RunExecutionClockRow, run.id)
            assert parent is not None and clock is not None
            clock.hard_deadline += timedelta(seconds=1)
            await session.flush()
            await clocks.locked(session, parent)
    assert (await _stored_clock(factory, run.id))[0] == created

    async def claim(worker_token: UUID) -> RuntimeCheckpoint | None:
        async with factory.begin() as session:
            now = await clocks.database_now(session)
            result = await repository.claim_for_execution(session, run.id,
                allow_running_recovery=False, worker_id=worker, worker_lease_token=worker_token,
                worker_lease_expires_at=now + timedelta(seconds=120))
            assert isinstance(result, tuple), "claim must acquire a real worker execution"
            row, checkpoint = result
            assert row.status == RunStatus.RUNNING.value
            return checkpoint

    assert await claim(token) is None
    approval = "approval_" + uuid4().hex
    await repository.begin_capability_approval(tenant, run.id, approval_id=approval,
        approval_fingerprint="c" * 64, actor_id=actor)
    arguments_hash = hashlib.sha256(b"{}").hexdigest()
    checkpoint = RuntimeCheckpoint(id=uuid4(), tenant_id=tenant, run_id=run.id,
        mode=TaskMode.DISCUSS, runtime_type="autogen", runtime_version="2",
        state={"terminal": False, "tool_ledger": ({"status": "waiting_approval",
            "approval_id": approval, "replay_safe": False, "actor": "implementer",
            "name": "project.verify", "position": 0, "arguments_hash": arguments_hash,
            "idempotency_key": hashlib.sha256(
                f"{run.id}:implementer:0:project.verify:{arguments_hash}".encode()).hexdigest()},)})
    async with factory.begin() as session:
        await repository.persist_event(session, tenant_id=tenant, run_id=run.id,
            event=RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=1, run_id=run.id,
                           checkpoint=checkpoint))
    await repository.park_execution_clock(tenant, run.id, worker_id=worker,
        worker_lease_token=token, quiescent=True)
    parked, _ = await _stored_clock(factory, run.id)
    assert parked.active_since is None and parked.spent_seconds > 0
    assert parked.hard_deadline == created.hard_deadline
    async with factory() as session:
        before_wait = await clocks.database_now(session)
        await session.execute(text("SELECT pg_sleep(0.01)"))
        after_wait = await clocks.database_now(session)
    assert parked.remaining(after_wait).active_seconds == parked.remaining(before_wait).active_seconds
    assert parked.remaining(after_wait).authorization_seconds < parked.remaining(before_wait).authorization_seconds

    approval_record = await repository.get(tenant, run.id)
    await repository.approve_capability_and_enqueue(tenant_id=tenant, run_id=run.id,
        approval_id=approval, version=approval_record.version, actor_id=actor)
    next_token = uuid4()
    restored = await claim(next_token)
    assert restored is not None and restored.id == checkpoint.id
    resumed, _ = await _stored_clock(factory, run.id)
    assert resumed.active_since is not None and resumed.worker_token == next_token
    assert resumed.hard_deadline == created.hard_deadline
    assert resumed.spent_seconds == parked.spent_seconds
    async with factory() as session:
        rows = list(await session.scalars(select(RunExecutionClockRow).where(
            RunExecutionClockRow.run_id == run.id)))
        assert len(rows) == 1
    # auth_session_factory teardown must delete this clock before Input/Auth.


def _run_clock_ddl(connection: Connection, operation: Callable[[], None]) -> None:
    assert connection.dialect.name == "postgresql", "guard evidence requires real PostgreSQL"
    with Operations.context(MigrationContext.configure(connection)):
        operation()



@submission_boundary
async def _new_downgrade_clock(factory: async_sessionmaker[AsyncSession]) -> UUID:
    tenant, actor = uuid4(), uuid4()
    request = "Synthetic private-database clock downgrade safety check."

    async def approved() -> bool:
        return True

    profile: dict[str, object] = {"config_sha256": "a" * 64,
        "catalog_sha256": "b" * 64, "timeout_seconds": 600.0}
    bind_submission(tenant_id=tenant, actor_id=actor, request=request, profile=profile,
        active_seconds=120., initial_soft_seconds=90., extension_per_unit=5., recheck=approved)
    original = _capture_authenticated_input(AuthenticatedPrincipal(actor, tenant, Role.OPERATOR),
        request, {"mode": "discuss", "execution_clock_version": 2})
    run = await RunRepository(factory).create_run(tenant_id=tenant, actor_id=actor, actor_role=Role.OPERATOR,
        request=request, mode=TaskMode.DISCUSS, status=RunStatus.QUEUED, idempotency_key=None,
        enqueue=True, routing_decision={"execution_clock_version": 2,
            "runtime_timeout_absolute_seconds": 120., "runtime_timeout_soft_seconds": 90.,
            "approval_review_profile": profile}, authenticated_input=original)
    return run.id



@pytest.mark.parametrize("state", ["queued", "parked", "terminal", "witness_only"])
async def test_postgres_downgrade_refuses_recovery_state_without_mutation(
    auth_session_factory: async_sessionmaker[AsyncSession], state: str,
) -> None:
    factory = auth_session_factory
    async with factory() as session:
        connection = await session.connection()
        assert connection.dialect.name == "postgresql"
        assert str(await session.scalar(text("SELECT version()"))).startswith("PostgreSQL")
    run_id = await _new_downgrade_clock(factory)
    if state in {"parked", "terminal"}:
        async with factory.begin() as session:
            parent = await session.get(RunRow, run_id)
            assert parent is not None
            parent.status = "waiting_approval" if state == "parked" else "completed"

    async def snapshot() -> tuple[dict[str, object], dict[str, object]]:
        async with factory() as session:
            clock = (await session.execute(select(RunExecutionClockRow.__table__).where(
                RunExecutionClockRow.run_id == run_id))).mappings().one()
            parent = (await session.execute(select(RunRow.__table__).where(RunRow.id == run_id))).mappings().one()
            return dict(clock), dict(parent)

    before = await snapshot()
    assert before[0]["hard_deadline"] is not None and before[1]["execution_clock_sha256"] is not None
    # Roll back even if the old unsafe implementation returns normally: a RED
    # must not leave the private integration DB's schema/data destroyed.
    async with factory() as session:
        transaction = await session.begin()
        try:
            if state == "witness_only":
                await session.execute(delete(RunExecutionClockRow).where(RunExecutionClockRow.run_id == run_id))
            with pytest.raises(IntegrityError) as refusal:
                connection = await session.connection()
                await connection.run_sync(_run_clock_ddl, clock_schema.downgrade)
            assert getattr(refusal.value.orig, "sqlstate", None) == "23514"
        finally:
            await transaction.rollback()
    assert await snapshot() == before, "H/policy/binding/CAS/witness and every original field must survive"
    async with factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "0037_execution_clock"
        assert await session.scalar(text("SELECT to_regclass('agent_hub_run_execution_clocks')")) is not None
        assert await session.scalar(text("SELECT count(*) FROM pg_constraint WHERE conname IN "
            "('fk_execution_clock_witness', 'fk_execution_clock_original')")) == 2



async def test_postgres_empty_clock_and_null_witness_can_downgrade_then_upgrade(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = auth_session_factory
    async with factory() as session:
        transaction = await session.begin()
        try:
            connection = await session.connection()
            assert connection.dialect.name == "postgresql"
            assert await session.scalar(text("SELECT count(*) FROM agent_hub_run_execution_clocks")) == 0
            assert await session.scalar(text("SELECT count(*) FROM agent_hub_runs "
                "WHERE execution_clock_sha256 IS NOT NULL")) == 0
            await connection.run_sync(_run_clock_ddl, clock_schema.downgrade)
            assert await session.scalar(text("SELECT to_regclass('agent_hub_run_execution_clocks')")) is None
            assert await session.scalar(text("SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='agent_hub_runs' AND column_name='execution_clock_sha256'")) == 0
            await connection.run_sync(_run_clock_ddl, clock_schema.upgrade)
            assert await session.scalar(text("SELECT to_regclass('agent_hub_run_execution_clocks')")) is not None
            assert await session.scalar(text("SELECT count(*) FROM agent_hub_run_execution_clocks")) == 0
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "0037_execution_clock"
        finally:
            await transaction.rollback()
