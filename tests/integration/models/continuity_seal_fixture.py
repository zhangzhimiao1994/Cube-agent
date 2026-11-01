"""Exclusive continuity fixtures. Memory SQL delivery is not PostgreSQL proof."""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.engine import Connection, ExecutionContext, Result, make_url
from sqlalchemy.engine.result import IteratorResult, SimpleResultMetaData
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import ORMExecuteState, Session, UOWTransaction
from sqlalchemy.sql import Select, operators, visitors
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import BinaryExpression, BindParameter

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.models import (
    RunAdmissionEventRow,
    RunAdmissionPoolRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunRow,
)
from agent_hub.db.session import Database, build_database
from agent_hub.models.run_admission_repository import (
    AdmissionConflict,
    PoolSnapshot,
    RunAdmissionRepository,
    _OpenArguments,
    _WorkerLease,
    continuity_profile_fingerprint,
)
from agent_hub.runs.authorization import _capture_authenticated_input, _persist_authenticated_input


def profile_value(expiry: datetime) -> dict[str, object]:
    return {'version': 2, 'config_sha256': 'c'*64, 'catalog_version': 11,
            'catalog_sha256': 'd'*64, 'activation': {'valid_until': expiry.isoformat()},
            'timeout_seconds': 600.0, 'modes': ['direct', 'dispatch', 'discuss', 'hybrid']}


@dataclass
class PendingRows:
    pool: RunAdmissionPoolRow | None = None
    events: list[RunAdmissionEventRow] = field(default_factory=list)


class SealStore:
    """Actual DAO/fence/ORM rows; SQL delivery and commits are memory-only seams."""
    def __init__(self) -> None:
        self.now = datetime(2030, 1, 1, tzinfo=UTC)
        self.expiry = self.now+timedelta(seconds=30)
        user = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
        self.evidence = _capture_authenticated_input(user, 'original continuity request', {'mode': 'direct'})
        self.lease = _WorkerLease('continuity-worker', uuid4())
        self.run = RunRow(id=uuid4(), tenant_id=user.tenant_id, actor_id=user.user_id,
            actor_role='operator', mode='direct', status='running', blocked_by_run_id=None,
            request=self.evidence.message, routing_decision={
                'runtime_token_absolute_tokens': 100, 'runtime_plan_token_budget': 100,
                'runtime_timeout_absolute_seconds': 600, 'approval_review_profile': profile_value(self.expiry)},
            worker_id=self.lease.worker_id, worker_lease_token=self.lease.token,
            worker_lease_expires_at=self.now+timedelta(minutes=10))
        self.link = RunInputAuthorizationRow(run_id=self.run.id, tenant_id=self.run.tenant_id,
                                              turn_id=self.evidence.turn_id)
        self.turn = RunAuthenticatedInputRow(run_id=self.run.id, tenant_id=self.run.tenant_id,
            turn_id=self.evidence.turn_id, actor_id=user.user_id, actor_role='operator',
            message=self.evidence.message, message_sha256=self.evidence.message_sha256,
            input_scope=self.evidence.input_scope, scope_sha256=self.evidence.scope_sha256)
        self.pool: RunAdmissionPoolRow | None = None
        self.events: list[RunAdmissionEventRow] = []
        self.timeline: list[str] = []
        self.after_pool_read: Callable[[], None] | None = None
        self.fail_event_flush = False
        self.factory = async_sessionmaker(expire_on_commit=False, sync_session_class=SealSession,
                                          info={'seal_store': self})
        self.repository = RunAdmissionRepository(self.factory)

    def opening(self) -> _OpenArguments:
        return {'tenant_id': self.run.tenant_id, 'run_id': self.run.id,
                'authorization_turn_id': self.evidence.turn_id, 'lease': self.lease,
                'config_sha256': 'c'*64, 'original_token_grant': 100,
                'original_deadline': datetime(2030, 1, 1, tzinfo=UTC)+timedelta(minutes=5),
                'review_token_limit': 80, 'review_count_limit': 3, 'review_cost_limit': Decimal(1)}


class SealSession(Session):
    pass


def store_for(session: Session) -> SealStore:
    value = session.info['seal_store']
    assert isinstance(value, SealStore)
    return value


def matches(row: object, statement: Select[tuple[object]]) -> bool:
    for clause in visitors.iterate(statement.whereclause):
        if isinstance(clause, BinaryExpression) and clause.operator is operators.eq:
            key: object = getattr(clause.left, 'key', None)
            assert isinstance(key, str) and isinstance(clause.right, BindParameter)
            if getattr(row, key) != clause.right.value:
                return False
    return True


def rows[T](values: Sequence[T]) -> IteratorResult[tuple[T]]:
    return IteratorResult(SimpleResultMetaData(['fixture_row']), iter((value,) for value in values))


@event.listens_for(SealSession, 'do_orm_execute')
def deliver(state: ORMExecuteState) -> (
    Result[tuple[RunRow]] | Result[tuple[RunAdmissionPoolRow]] | Result[tuple[RunAdmissionEventRow]]
    | Result[tuple[RunInputAuthorizationRow, RunAuthenticatedInputRow]] | Result[tuple[datetime]]
):
    store = store_for(state.session)
    statement = state.statement
    assert isinstance(statement, Select)
    entity: object = statement.column_descriptions[0].get('entity')
    if entity is RunRow:
        store.timeline.append('run_lock')
        assert statement._for_update_arg is not None
        return rows([store.run] if matches(store.run, statement) else [])
    if entity is RunAdmissionPoolRow:
        store.timeline.append('pool_lock')
        assert statement._for_update_arg is not None
        if store.after_pool_read is not None:
            store.after_pool_read()
        return rows([store.pool] if store.pool is not None and matches(store.pool, statement) else [])
    if entity is RunInputAuthorizationRow:
        store.timeline.append('authorization')
        values = [(store.link, store.turn)] if matches(store.link, statement) else []
        return IteratorResult(SimpleResultMetaData(['link', 'turn']), iter(values))
    if entity is RunAdmissionEventRow:
        store.timeline.append('opened_read')
        return rows([item for item in store.events if matches(item, statement)])
    assert str(statement.column_descriptions[0]['expr']) == 'clock_timestamp()'
    store.timeline.append('clock_timestamp')
    return rows([store.now])


@event.listens_for(SealSession, 'before_flush')
def stage(session: Session, flush_context: UOWTransaction, instances: Sequence[object] | None) -> None:
    store = store_for(session)
    pending = session.info.setdefault('pending_rows', PendingRows())
    assert isinstance(pending, PendingRows)
    for item in list(session.new):
        if isinstance(item, RunAdmissionPoolRow):
            store.timeline.append('pool_flush')
            pending.pool = item
        else:
            assert isinstance(item, RunAdmissionEventRow)
            store.timeline.append('event_flush')
            if store.fail_event_flush:
                from sqlalchemy.exc import SQLAlchemyError
                raise SQLAlchemyError('PRIVATE_FLUSH_DETAILS')
            pending.events.append(item)
    session.expunge_all()


@event.listens_for(SealSession, 'after_commit')
def commit(session: Session) -> None:
    store = store_for(session)
    pending = session.info.pop('pending_rows', PendingRows())
    assert isinstance(pending, PendingRows)
    if pending.pool is not None:
        store.pool = pending.pool
    store.events.extend(pending.events)


@event.listens_for(SealSession, 'after_rollback')
def rollback(session: Session) -> None:
    session.info.pop('pending_rows', None)


async def untrusted_open(repository: RunAdmissionRepository, arguments: Mapping[str, object]) -> PoolSnapshot:
    """Bad pair inputs still reach the actual production method unchanged."""
    method: object = repository.open_pool
    assert callable(method)
    pending: object = method(**arguments)
    assert isinstance(pending, Awaitable)
    value: object = await pending
    assert isinstance(value, PoolSnapshot)
    return value


def dedicated_pg_url() -> str:
    value = os.environ.get('AGENT_HUB_TEST_DATABASE_URL')
    if not value:
        pytest.skip('explicit dedicated PG required; no provisioning or migration')
    url = make_url(value)
    assert url.drivername == 'postgresql+asyncpg' and url.username == url.database == 'agent_hub_test'
    assert set(url.query) <= {'host'}
    socket = url.query.get('host')
    assert ((socket is None and url.host in {'127.0.0.1','localhost','::1'} and url.port is not None)
            or (url.host is None and isinstance(socket, str) and socket.startswith('/')
                and '..' not in socket.split('/') and '\x00' not in socket))
    return value


@pytest.fixture(scope='session')
def migrated_database() -> Iterator[None]:
    """Override the broad migration fixture: native must supply an existing test DB."""
    dedicated_pg_url()
    yield


@dataclass
class PgSealCase:
    database: Database
    repository: RunAdmissionRepository
    opening: _OpenArguments
    route: dict[str, object]


@pytest_asyncio.fixture
async def pg_seal_case() -> AsyncIterator[PgSealCase]:
    database = build_database(dedicated_pg_url())
    ids: list[UUID] = []
    try:
        async with database.session_factory() as session, session.begin():
            identity = (await session.execute(select(func.current_database(), func.current_user()))).one()
            assert tuple(identity) == ('agent_hub_test','agent_hub_test')
            assert (await session.execute(text('SELECT version_num FROM alembic_version'))).scalar_one() == '0036_review_ledger'
            now: object = await session.scalar(select(func.clock_timestamp()))
            assert isinstance(now, datetime)
            expiry = now+timedelta(seconds=30)
            principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
            original = _capture_authenticated_input(principal, 'dedicated PG continuity', {'mode':'direct'})
            lease = _WorkerLease('pg-continuity-worker', uuid4())
            route: dict[str, object] = {'runtime_token_absolute_tokens':100,'runtime_plan_token_budget':100,
                'runtime_timeout_absolute_seconds':600,'approval_review_profile':profile_value(expiry)}
            run = RunRow(id=uuid4(), tenant_id=principal.tenant_id, actor_id=principal.user_id,
                actor_role='operator', mode='direct', status='running', request=original.message,
                routing_decision=route, worker_id=lease.worker_id, worker_lease_token=lease.token,
                worker_lease_expires_at=now+timedelta(minutes=10))
            ids.append(run.id)
            session.add(run)
            await session.flush()
            await _persist_authenticated_input(session, run, original)
            opening: _OpenArguments = {'tenant_id':run.tenant_id,'run_id':run.id,
                'authorization_turn_id':original.turn_id,'lease':lease,'config_sha256':'c'*64,
                'original_token_grant':100,'original_deadline':now+timedelta(minutes=5),
                'review_token_limit':80,'review_count_limit':3,'review_cost_limit':Decimal(1),
                'continuity_profile_sha256':continuity_profile_fingerprint(route['approval_review_profile']),
                'activation_valid_until':expiry}
        yield PgSealCase(database, RunAdmissionRepository(database.session_factory), opening, route)
    finally:
        if ids:
            async with database.session_factory() as session, session.begin():
                await session.execute(delete(RunRow).where(RunRow.id.in_(ids)))
        await database.dispose()


async def assert_pg_pool_and_opened_count(case: PgSealCase, expected: int) -> None:
    async with case.database.session_factory() as session:
        for row in (RunAdmissionPoolRow, RunAdmissionEventRow):
            query = select(func.count()).select_from(row).where(row.run_id == case.opening['run_id'])
            if row is RunAdmissionEventRow:
                query = query.where(RunAdmissionEventRow.kind == 'opened')
            assert (await session.execute(query)).scalar_one() == expected


@pytest.mark.integration
async def test_pg_opened_seal_and_pool_rollback_atomically(pg_seal_case: PgSealCase) -> None:
    case = pg_seal_case
    pool_inserted = False
    fault_hit = False

    def observe_insert(connection: Connection, cursor: object, statement: str, params: object,
                       context: ExecutionContext, many: bool) -> None:
        nonlocal pool_inserted
        compiled = context.compiled
        if (compiled is not None and isinstance(compiled.statement, Insert)
                and compiled.statement.table.name == RunAdmissionPoolRow.__tablename__):
            # after_cursor_execute proves PostgreSQL executed this INSERT;
            # merely seeing an ORM pending row is insufficient rollback proof.
            pool_inserted = True

    def fail_opened(session: Session, flush_context: UOWTransaction, instances: Sequence[object] | None) -> None:
        nonlocal fault_hit
        if any(isinstance(row, RunAdmissionEventRow) and row.run_id == case.opening['run_id']
               and row.kind == 'opened' for row in session.new):
            assert pool_inserted, 'opened fault must follow the actual pool INSERT'
            fault_hit = True
            raise SQLAlchemyError('owned event failure')
    event.listen(case.database.engine.sync_engine, 'after_cursor_execute', observe_insert)
    event.listen(Session, 'before_flush', fail_opened)
    try:
        with pytest.raises(AdmissionConflict):
            await case.repository.open_pool(**case.opening)
    finally:
        event.remove(Session, 'before_flush', fail_opened)
        event.remove(case.database.engine.sync_engine, 'after_cursor_execute', observe_insert)
    assert pool_inserted and fault_hit
    await assert_pg_pool_and_opened_count(case, 0)
    assert await case.repository.get_continuity_seal(case.opening['tenant_id'], case.opening['run_id']) is None


@pytest.mark.integration
async def test_pg_concurrent_first_open_has_one_original_seal(pg_seal_case: PgSealCase) -> None:
    case = pg_seal_case
    results = await asyncio.gather(*(case.repository.open_pool(**case.opening) for _ in range(4)))
    assert all(result == results[0] for result in results)
    await assert_pg_pool_and_opened_count(case, 1)
    seal = await case.repository.get_continuity_seal(case.opening['tenant_id'], case.opening['run_id'])
    assert seal is not None and seal[0] == case.opening['continuity_profile_sha256']
    assert seal[1] < case.opening['activation_valid_until']


@pytest.mark.integration
async def test_pg_expiry_while_waiting_for_run_lock_creates_neither_pool_nor_seal(
    pg_seal_case: PgSealCase, record_property: Callable[[str, object], None],
) -> None:
    case = pg_seal_case
    waiting = asyncio.Event()
    waiter_pid: int | None = None
    task: asyncio.Task[PoolSnapshot] | None = None
    def lock_query(connection: Connection, cursor: object, statement: str, params: object,
                   context: ExecutionContext, many: bool) -> None:
        nonlocal waiter_pid
        if 'FOR UPDATE' in statement and 'agent_hub_runs' in statement:
            # Identify the very connection about to wait. This client event
            # only enables observation; it is never itself blocking evidence.
            pid: object = connection.scalar(select(func.pg_backend_pid()))
            assert type(pid) is int
            waiter_pid = pid
            waiting.set()
    try:
        async with case.database.session_factory() as holder, holder.begin():
            row = await holder.scalar(select(RunRow).where(RunRow.id == case.opening['run_id']).with_for_update())
            assert row is not None
            holder_pid: object = await holder.scalar(select(func.pg_backend_pid()))
            assert type(holder_pid) is int
            clock: object = await holder.scalar(select(func.clock_timestamp()))
            assert isinstance(clock, datetime)
            # Test qualification only: give server-observed lock polling room.
            # The immutable product/task deadline in case.opening is unchanged.
            expiry = clock+timedelta(seconds=5)
            value = profile_value(expiry)
            route = {**case.route,'approval_review_profile':value}
            await holder.execute(update(RunRow).where(RunRow.id == row.id).values(routing_decision=route))
            args: _OpenArguments = {**case.opening,'continuity_profile_sha256':continuity_profile_fingerprint(value),
                                    'activation_valid_until':expiry}
            event.listen(case.database.engine.sync_engine,'before_cursor_execute',lock_query)
            try:
                task = asyncio.create_task(case.repository.open_pool(**args))
                await asyncio.wait_for(waiting.wait(), timeout=3)
                assert waiter_pid is not None and waiter_pid != holder_pid
                assert not task.done()
                async with case.database.session_factory() as observer:
                    async with asyncio.timeout(4):
                        while True:
                            # Avoid a cached statistics snapshot inside the
                            # observer transaction masking a newly blocked PID.
                            await observer.execute(text('SELECT pg_stat_clear_snapshot()'))
                            blocked = (await observer.execute(text('''
                                SELECT pid, xact_start, clock_timestamp() AS observed_at,
                                       pg_blocking_pids(pid) AS blockers, wait_event_type
                                FROM pg_stat_activity
                                WHERE pid = :waiter AND :holder = ANY(pg_blocking_pids(pid))
                                  AND datname = current_database() AND usename = current_user
                            '''), {'waiter': waiter_pid, 'holder': holder_pid})).one_or_none()
                            if blocked is not None:
                                xact_start: object = blocked.xact_start
                                observed_at: object = blocked.observed_at
                                blockers: object = blocked.blockers
                                assert isinstance(xact_start, datetime)
                                assert isinstance(observed_at, datetime)
                                assert isinstance(blockers, list) and holder_pid in blockers
                                assert blocked.pid == waiter_pid and blocked.wait_event_type == 'Lock'
                                assert xact_start < expiry and observed_at < expiry
                                assert not task.done()
                                record_property('continuity_lock_observed', json.dumps({
                                    'holder_pid': holder_pid, 'waiter_pid': waiter_pid,
                                    'waiter_xact_start': xact_start.isoformat(),
                                    'server_observed_at': observed_at.isoformat(),
                                    'activation_valid_until': expiry.isoformat(),
                                    'blocked_by_holder': True}, sort_keys=True))
                                break
                            assert not task.done()
                            await asyncio.sleep(0.02)
                    async with asyncio.timeout(6):
                        while True:
                            actual: object = await observer.scalar(select(func.clock_timestamp()))
                            assert isinstance(actual, datetime)
                            if actual >= expiry:
                                assert not task.done()
                                record_property('continuity_lock_release_clock', actual.isoformat())
                                break
                            await asyncio.sleep(0.02)
            finally:
                event.remove(case.database.engine.sync_engine,'before_cursor_execute',lock_query)
        assert task is not None
        with pytest.raises(AdmissionConflict):
            await asyncio.wait_for(task, timeout=5)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await assert_pg_pool_and_opened_count(case, 0)
