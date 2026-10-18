from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from asyncpg.pgproto.pgproto import UUID as DriverUUID
from sqlalchemy.dialects import postgresql

from agent_hub.db.models import RunOutboxRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRepository


def test_postgres_driver_uuid_identity_is_canonical_at_repository_boundary():
    identifiers={key:DriverUUID(str(uuid4())) for key in (
        'id','tenant_id','actor_id','blocked_by_run_id','worker_lease_token')}
    assert all(isinstance(value,UUID) and type(value) is not UUID for value in identifiers.values())
    row=SimpleNamespace(**identifiers,actor_role='operator',request='Owned diagnostic',mode='direct',
        status='running',version=1,created_at=datetime.now(UTC),routing_decision={},worker_id='worker',
        worker_lease_expires_at=None,worker_heartbeat_at=None)
    record=RunRepository._record(row)
    for key,value in identifiers.items():
        assert type(getattr(record,key)) is UUID
        assert getattr(record,key)==value
    assert record.status is RunStatus.RUNNING and record.mode is TaskMode.DIRECT


def test_nullable_database_uuid_fields_remain_none():
    row=SimpleNamespace(id=uuid4(),tenant_id=uuid4(),actor_id=None,actor_role=None,request='Read',
        mode=None,status='queued',version=1,created_at=datetime.now(UTC),routing_decision=None,
        blocked_by_run_id=None,worker_lease_token=None)
    record=RunRepository._record(row)
    assert record.actor_id is None and record.blocked_by_run_id is None and record.worker_lease_token is None


def _repository_session() -> tuple[RunRepository, MagicMock]:
    session = MagicMock()
    session.__aenter__.return_value = session
    session.scalar = AsyncMock()
    session.scalars = AsyncMock()
    session.flush = AsyncMock()
    return RunRepository(cast(Any, lambda: session)), session


def _outbox_row() -> RunOutboxRow:
    return RunOutboxRow(
        id=DriverUUID(str(uuid4())),
        tenant_id=DriverUUID(str(uuid4())),
        run_id=DriverUUID(str(uuid4())),
        idempotency_key="outbox-run-1",
        delivered=False,
    )


@pytest.mark.asyncio
async def test_pending_outbox_canonicalizes_real_driver_uuid_identities() -> None:
    repository, session = _repository_session()
    row = _outbox_row()
    session.scalars.return_value = SimpleNamespace(all=lambda: [row])

    (pending,) = await repository.pending_outbox(limit=7)

    assert type(pending.id) is UUID and pending.id == row.id
    assert type(pending.run_id) is UUID and pending.run_id == row.run_id
    assert pending.idempotency_key == row.idempotency_key
    statement = session.scalars.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "delivered IS false" in str(statement)
    assert statement.params["param_1"] == 7


@pytest.mark.asyncio
async def test_deliver_outbox_canonicalizes_reread_driver_uuid_before_callback() -> None:
    repository, session = _repository_session()
    row = _outbox_row()
    session.scalar.return_value = row
    deliveries = []

    async def deliver(run_id: UUID, idempotency_key: str) -> None:
        assert type(run_id) is UUID
        assert row.delivered is False
        assert session.flush.await_count == 0
        deliveries.append((run_id, idempotency_key))

    assert await repository.deliver_outbox(UUID(str(row.id)), deliver) is True
    assert deliveries == [(row.run_id, row.idempotency_key)]
    assert row.delivered is True and row.delivered_at is not None
    assert session.flush.await_count == 1
    statement = session.scalar.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "FOR UPDATE SKIP LOCKED" in str(statement)
    assert statement.params["id_1"] == row.id


@pytest.mark.asyncio
async def test_recovery_candidates_canonicalize_real_driver_uuids_without_changing_lease_query() -> None:
    repository, session = _repository_session()
    identifiers = [DriverUUID(str(uuid4())), DriverUUID(str(uuid4()))]
    session.scalars.return_value = SimpleNamespace(all=lambda: identifiers)
    now = datetime.now(UTC)

    candidates = await repository.running_for_recovery(5, now=now)

    assert candidates == tuple(identifiers)
    assert all(type(run_id) is UUID for run_id in candidates)
    statement = session.scalars.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "worker_lease_expires_at IS NOT NULL" in str(statement)
    assert "worker_lease_expires_at <=" in str(statement)
    assert statement.params["worker_lease_expires_at_1"] == now
    assert statement.params["status_1"] == "running"
    assert statement.params["param_1"] == 5


@pytest.mark.asyncio
async def test_failed_outbox_delivery_does_not_acknowledge_row() -> None:
    repository, session = _repository_session()
    row = _outbox_row()
    session.scalar.return_value = row

    async def deliver(run_id: UUID, idempotency_key: str) -> None:
        raise RuntimeError("queue unavailable")

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await repository.deliver_outbox(UUID(str(row.id)), deliver)

    assert row.delivered is False and row.delivered_at is None
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_missing_or_delivered_outbox_is_not_delivered_again(missing: bool) -> None:
    repository, session = _repository_session()
    row = _outbox_row()
    row.delivered = True
    session.scalar.return_value = None if missing else row
    deliver = AsyncMock()

    assert await repository.deliver_outbox(UUID(str(row.id)), deliver) is False
    deliver.assert_not_awaited()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1])
async def test_nonpositive_recovery_limit_does_not_query_database(limit: int) -> None:
    repository, session = _repository_session()

    assert await repository.running_for_recovery(limit, now=datetime.now(UTC)) == ()
    session.scalars.assert_not_awaited()
