"""Real PostgreSQL proofs for the shared original-run storage slice only."""
import asyncio
import importlib.util
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Unpack
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import ArgumentError, DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunAdmissionEventRow,
    RunAdmissionPoolRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunReviewEffectRow,
    RunReviewEventRow,
    RunReviewIntentRow,
    RunRow,
)
from agent_hub.db.session import build_database
from agent_hub.domain.runs import RunStatus
from agent_hub.models.run_admission_repository import (
    AdmissionConflict,
    AttemptSnapshot,
    DeploymentIdentity,
    FinishOutcome,
    PoolSnapshot,
    Purpose,
    RunAdmissionRepository,
    UsageObservation,
    _WorkerLease,
)
from agent_hub.models.run_admission_repository import (
    _ExecutionArguments as Binding,
)
from agent_hub.models.run_admission_repository import (
    _OpenArguments as Opening,
)
from agent_hub.models.run_admission_repository import (
    _ReserveArguments as ReservationArguments,
)
from agent_hub.runs.authorization import _capture_authenticated_input, _persist_authenticated_input
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent
from tests.integration.models.run_admission_typed_fixture import (
    BindingChanges,
    EventReason,
    LockWaitStarted,
    MigrationModule,
    PoolLimits,
    ReopenChanges,
    ReservationOverrides,
    Storage,
    present,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope='module')]
CONFIG = 'c' * 64
DEPLOYMENT = DeploymentIdentity('synthetic', 'deployment', 'model')


def _require_test_database_url(database_url: str) -> None:
    message = 'dedicated test database URL required'
    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError):
        raise AssertionError(message) from None
    assert (url.drivername == 'postgresql+asyncpg' and url.username == 'agent_hub_test'
            and url.database == 'agent_hub_test' and set(url.query) <= {'host'}), message
    if url.port is not None:
        assert 1 <= url.port <= 65535, message
    socket = url.query.get('host')
    if socket is None:
        assert url.host in {'127.0.0.1', 'localhost', '::1'} and url.port is not None, message
    else:
        assert (url.host is None and type(socket) is str and socket.startswith('/')
                and not socket.startswith('//') and '..' not in socket.split('/')
                and '\x00' not in socket), message


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def storage(database_url: str) -> AsyncIterator[Storage]:
    _require_test_database_url(database_url)
    database = build_database(database_url)
    ids: list[UUID] = []
    try:
        async with database.session_factory() as session:
            identity = (await session.execute(select(func.current_database(), func.current_user()))).one()
            assert tuple(identity) == ('agent_hub_test', 'agent_hub_test'), 'dedicated test DB identity required'
        yield database.session_factory, ids
    finally:
        if ids:
            async with database.session_factory() as session, session.begin():
                await _cleanup_owned_runs(session, ids)
        await database.dispose()


async def _cleanup_owned_runs(session: AsyncSession, ids: Sequence[UUID]) -> None:
    """Remove only this fixture's run IDs; callers own the cleanup transaction."""
    if ids:
        for row in (RunReviewEventRow, RunReviewEffectRow, RunReviewIntentRow):
            await session.execute(delete(row).where(row.run_id.in_(ids)))
        await session.execute(delete(RunRow).where(RunRow.id.in_(ids)))


async def prepare(
    storage: Storage, *, open_now: bool = True, route: dict[str, JsonValue] | None = None,
    **limits: Unpack[PoolLimits],
) -> tuple[RunAdmissionRepository, Binding, Opening]:
    factory, ids = storage
    user = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    evidence = _capture_authenticated_input(user, 'synthetic original', {'mode': 'direct'})
    run_id = uuid4()
    ids.append(run_id)
    lease = _WorkerLease('synthetic-worker', uuid4())
    async with factory() as session, session.begin():
        now = await session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime)
        run = RunRow(id=run_id, tenant_id=user.tenant_id, actor_id=user.user_id,
            actor_role=user.role.value, request='synthetic original', mode='direct',
            status='running', routing_decision=route if route is not None else {
                'runtime_token_absolute_tokens': 100, 'runtime_plan_token_budget': 100,
                'runtime_timeout_absolute_seconds': 3600}, worker_id=lease.worker_id,
            worker_lease_token=lease.token, worker_lease_expires_at=now + timedelta(hours=1))
        session.add(run)
        await session.flush()
        await _persist_authenticated_input(session, run, evidence)
    repository = RunAdmissionRepository(factory)
    binding: Binding = {'tenant_id': user.tenant_id, 'run_id': run_id,
        'authorization_turn_id': evidence.turn_id, 'lease': lease, 'config_sha256': CONFIG}
    opening: Opening = {
        'tenant_id': binding['tenant_id'], 'run_id': binding['run_id'],
        'authorization_turn_id': binding['authorization_turn_id'],
        'lease': binding['lease'], 'config_sha256': binding['config_sha256'],
        'original_token_grant': limits.get('original_token_grant', 100),
        'original_deadline': limits.get('original_deadline', now + timedelta(minutes=30)),
        'review_token_limit': limits.get('review_token_limit', 80),
        'review_count_limit': limits.get('review_count_limit', 3),
        'review_cost_limit': limits.get('review_cost_limit', Decimal(1)),
    }
    if open_now:
        await repository.open_pool(**opening)
    return repository, binding, opening


async def reserve(
    repository: RunAdmissionRepository, binding: Binding, *, tokens: int = 60,
    purpose: Purpose = 'primary', attempt_id: UUID | None = None,
    **extra: Unpack[ReservationOverrides],
) -> AttemptSnapshot:
    arguments: ReservationArguments = {**binding, 'attempt_id': attempt_id or uuid4(),
        'purpose': purpose, 'request_sha256': 'a' * 64, 'deployment': DEPLOYMENT,
        'token_hold': tokens, 'review_cost_hold': Decimal('.5') if purpose == 'review' else None,
        **extra}
    return await repository.reserve(**arguments)


async def issue(
    repository: RunAdmissionRepository, binding: Binding, attempt: AttemptSnapshot,
    physical: UUID | None = None,
) -> bool:
    return await repository.mark_issued(**binding, attempt_id=attempt.attempt_id,
        physical_transport_id=physical or uuid4())


async def finish(
    repository: RunAdmissionRepository, binding: Binding, attempt: AttemptSnapshot,
    outcome: FinishOutcome = 'completed', usage: UsageObservation | None = None,
) -> AttemptSnapshot:
    return await repository.finish(tenant_id=binding['tenant_id'], run_id=binding['run_id'],
        attempt_id=attempt.attempt_id, lease=binding['lease'], deployment=DEPLOYMENT,
        outcome=outcome, usage=usage)


async def pool(repository: RunAdmissionRepository, binding: Binding) -> PoolSnapshot | None:
    return await repository.get_pool(binding['tenant_id'], binding['run_id'])


async def test_primary_review_contend_for_same_original_grant(storage: Storage) -> None:
    repo, binding, _ = await prepare(storage)
    results = await asyncio.gather(reserve(repo, binding),
        reserve(repo, binding, purpose='review'), return_exceptions=True)
    assert sum(isinstance(item, AdmissionConflict) for item in results) == 1
    snapshot = present(await pool(repo, binding))
    assert snapshot.token_charged == 60 and snapshot.tokens_remaining == 40
    async with storage[0]() as session:
        assert await session.scalar(select(func.count()).select_from(RunAdmissionAttemptRow)
            .where(RunAdmissionAttemptRow.run_id == binding['run_id'])) == 1


async def test_partial_known_refund_and_immutable_reopen(storage: Storage) -> None:
    repo, binding, opening = await prepare(storage)
    attempt = await reserve(repo, binding, purpose='review')
    assert await issue(repo, binding, attempt)
    usage = UsageObservation(10, 10, 20, Decimal('.1'))
    terminal = await finish(repo, binding, attempt, usage=usage)
    assert terminal.state == 'completed'
    assert len(present(terminal.receipt_sha256)) == 64
    assert json.loads(present(terminal.receipt_bytes))['usage']['total_tokens'] == 20
    snapshot = await repo.open_pool(**opening)
    assert (snapshot.token_charged, snapshot.review_token_charged,
        snapshot.review_count_charged, snapshot.review_cost_charged) == (20, 20, 1, Decimal('.1'))
    assert await finish(repo, binding, attempt, usage=usage) == terminal
    reopen_changes: tuple[ReopenChanges, ...] = ({'original_token_grant': 99},
                    {'original_deadline': opening['original_deadline'] + timedelta(seconds=1)},
                    {'review_count_limit': 4}, {'config_sha256': 'b' * 64})
    for changed in reopen_changes:
        with pytest.raises(AdmissionConflict):
            reopened: Opening = {**opening, **changed}
            await repo.open_pool(**reopened)
    assert (present(await pool(repo, binding))).token_charged == 20


async def test_exact_duplicate_no_second_issue_and_terminal_conflict(storage: Storage) -> None:
    repo, binding, _ = await prepare(storage)
    attempt_id = uuid4()
    first = await reserve(repo, binding, attempt_id=attempt_id)
    assert await reserve(repo, binding, attempt_id=attempt_id) == first
    with pytest.raises(AdmissionConflict):
        await reserve(repo, binding, attempt_id=attempt_id, tokens=59)
    physical = uuid4()
    assert await issue(repo, binding, first, physical)
    assert not await issue(repo, binding, first, physical)
    with pytest.raises(AdmissionConflict):
        await issue(repo, binding, first)
    terminal = await finish(repo, binding, first, usage=UsageObservation(10, 10, 20, Decimal('.1')))
    with pytest.raises(AdmissionConflict):
        await finish(repo, binding, first, usage=UsageObservation(10, 11, 21, Decimal('.1')))
    assert await repo.get_attempt(binding['tenant_id'], binding['run_id'], attempt_id) == terminal
    async with storage[0]() as session:
        kinds = list(await session.scalars(select(RunAdmissionEventRow.kind)
            .where(RunAdmissionEventRow.run_id == binding['run_id'])))
    assert sorted(kinds) == ['completed', 'issued', 'opened', 'prepared']


async def test_cold_unknown_retains_hold_and_blocks_late_refund(storage: Storage) -> None:
    repo, binding, _ = await prepare(storage)
    attempt = await reserve(repo, binding, purpose='review')
    await issue(repo, binding, attempt)
    cold = RunAdmissionRepository(storage[0])
    unknown = await cold.recover_issued(binding['tenant_id'], binding['run_id'], attempt.attempt_id)
    assert unknown.state == 'unknown'
    assert await cold.recover_issued(binding['tenant_id'], binding['run_id'],
        attempt.attempt_id) == unknown
    snapshot = present(await pool(cold, binding))
    assert not snapshot.closed and snapshot.token_charged == 60
    assert snapshot.review_cost_charged == Decimal('.5')
    with pytest.raises(AdmissionConflict):
        await finish(cold, binding, attempt, usage=UsageObservation(1, 1, 2, Decimal('.01')))
    assert not await issue(cold, binding, unknown, unknown.physical_transport_id)
    alternate = await reserve(cold, binding, tokens=40, request_sha256='b' * 64)
    assert alternate.attempt_id != unknown.attempt_id and alternate.purpose == 'primary'
    assert await cold.get_attempt(binding['tenant_id'], binding['run_id'],
        unknown.attempt_id) == unknown
    snapshot = present(await pool(cold, binding))
    assert snapshot.original_token_grant == 100 and snapshot.token_charged == 100
    assert snapshot.review_token_charged == 60 and snapshot.review_count_charged == 1
    assert snapshot.review_cost_charged == Decimal('.5')
    with pytest.raises(AdmissionConflict):
        await reserve(cold, binding, tokens=1)


async def test_prepared_cancellation_refunds_but_issued_cannot_cancel(storage: Storage) -> None:
    repo, binding, _ = await prepare(storage)
    attempt = await reserve(repo, binding, purpose='review')
    terminal = await finish(repo, binding, attempt, outcome='not_issued')
    assert terminal.state == 'cancelled'
    snapshot = present(await pool(repo, binding))
    assert (snapshot.token_charged, snapshot.review_token_charged,
        snapshot.review_count_charged, snapshot.review_cost_charged) == (0, 0, 0, Decimal(0))
    with pytest.raises(AdmissionConflict):
        await issue(repo, binding, attempt)
    issued = await reserve(repo, binding, purpose='review')
    await issue(repo, binding, issued)
    with pytest.raises(AdmissionConflict):
        await finish(repo, binding, issued, outcome='not_issued')


@pytest.mark.parametrize('usage', [None, UsageObservation(True, 0, 1, Decimal(0)),
    UsageObservation(1, 2, 4, Decimal(0)), UsageObservation(1, 2, 3, Decimal('NaN')),
    UsageObservation(60, 1, 61, Decimal('.1')), UsageObservation(1, 1, 2, Decimal('.6'))])
async def test_invalid_or_over_bound_usage_retains_hold_with_distinct_pool_fences(storage: Storage, usage: UsageObservation | None) -> None:
    repo, binding, _ = await prepare(storage)
    attempt = await reserve(repo, binding, purpose='review')
    await issue(repo, binding, attempt)
    invalid = await finish(repo, binding, attempt, usage=usage)
    over_bound = usage is not None and (usage.total_tokens == 61 or usage.cost_usd == Decimal('.6'))
    assert invalid.state == ('invalid' if over_bound else 'unknown')
    assert invalid.receipt_bytes
    if usage is not None and usage.total_tokens == 61:
        assert json.loads(present(invalid.receipt_bytes))['usage']['total_tokens'] == 61
    snapshot = present(await pool(repo, binding))
    assert snapshot.closed is over_bound and snapshot.token_charged == 60
    assert snapshot.review_cost_charged == Decimal('.5')
    if over_bound:
        with pytest.raises(AdmissionConflict):
            await reserve(repo, binding, tokens=1)
    else:
        await reserve(repo, binding, tokens=40, request_sha256='b' * 64)
        assert (present(await pool(repo, binding))).token_charged == 100


@pytest.mark.parametrize('change', ['expired', 'replaced', 'worker', 'role', 'actor', 'missing',
    'cancelled', 'link', 'hash'])
async def test_live_fences_on_reopen_reserve_and_issue(storage: Storage, change: str) -> None:
    repo, binding, opening = await prepare(storage)
    attempt = await reserve(repo, binding, tokens=10)
    async with storage[0]() as session, session.begin():
        values: dict[str, dict[str, object]] = {
            'expired': {'worker_lease_expires_at': datetime(2000, 1, 1, tzinfo=UTC)},
            'replaced': {'worker_lease_token': uuid4()}, 'worker': {'worker_id': 'other'},
            'missing': {'worker_id': None, 'worker_lease_token': None, 'worker_lease_expires_at': None},
            'role': {'actor_role': 'viewer'}, 'actor': {'actor_id': uuid4()},
            'cancelled': {'status': 'cancelled'}}
        if change in values:
            await session.execute(update(RunRow).where(RunRow.id == binding['run_id'])
                .values(**values[change]))
        elif change == 'link':
            await session.execute(delete(RunInputAuthorizationRow)
                .where(RunInputAuthorizationRow.run_id == binding['run_id']))
        else:
            await session.execute(update(RunAuthenticatedInputRow)
                .where(RunAuthenticatedInputRow.run_id == binding['run_id'])
                .values(message_sha256='b' * 64))
    for operation in [repo.open_pool(**opening), reserve(repo, binding, tokens=1),
                      issue(repo, binding, attempt)]:
        with pytest.raises(AdmissionConflict):
            await operation
    assert (present(await pool(repo, binding))).token_charged == 10


async def test_cross_tenant_current_turn_config_and_missing_state_fail_closed(storage: Storage) -> None:
    repo, binding, opening = await prepare(storage)
    attempt = await reserve(repo, binding, tokens=1)
    binding_changes: tuple[BindingChanges, ...] = (
        {'tenant_id': uuid4()}, {'authorization_turn_id': uuid4()}, {'config_sha256': 'b' * 64})
    for changed in binding_changes:
        with pytest.raises(AdmissionConflict):
            await reserve(repo, {**binding, **changed}, tokens=1)
    assert await repo.get_pool(uuid4(), binding['run_id']) is None
    assert await repo.get_attempt(uuid4(), binding['run_id'], attempt.attempt_id) is None
    async with storage[0]() as session, session.begin():
        evidence = _capture_authenticated_input(AuthenticatedPrincipal(
            tenant_id=binding['tenant_id'],
            user_id=UUID(str((present(await session.get(RunRow, binding['run_id']))).actor_id)),
            role=Role.OPERATOR), 'changed original', {'mode': 'direct'})
        run = present(await session.get(RunRow, binding['run_id']))
        await _persist_authenticated_input(session, run, evidence, submitted_message='changed original')
    with pytest.raises(AdmissionConflict):
        await repo.open_pool(**opening)
    with pytest.raises(AdmissionConflict):
        await reserve(repo, {**binding, 'authorization_turn_id': evidence.turn_id}, tokens=1)
    missing, fresh, _ = await prepare(storage, open_now=False)
    with pytest.raises(AdmissionConflict):
        await reserve(missing, fresh, tokens=1)


@pytest.mark.parametrize('route', [{}, {'runtime_token_absolute_tokens': 100},
    {'runtime_token_absolute_tokens': True, 'runtime_plan_token_budget': 100,
        'runtime_timeout_absolute_seconds': 3600},
    {'runtime_token_absolute_tokens': 99, 'runtime_plan_token_budget': 100,
        'runtime_timeout_absolute_seconds': 3600},
    {'runtime_token_absolute_tokens': 100, 'runtime_plan_token_budget': 100},
    {'runtime_token_absolute_tokens': 100, 'runtime_plan_token_budget': 100,
        'runtime_timeout_absolute_seconds': 1}])
async def test_missing_or_insufficient_server_envelope_does_not_open(storage: Storage, route: dict[str, JsonValue]) -> None:
    repo, binding, opening = await prepare(storage, route=route, open_now=False)
    with pytest.raises(AdmissionConflict):
        await repo.open_pool(**opening)
    assert await pool(repo, binding) is None


async def test_review_sublimits_and_primary_cannot_gain_review_tokens(storage: Storage) -> None:
    repo, binding, _ = await prepare(storage, review_count_limit=1, review_token_limit=10,
        review_cost_limit=Decimal('.1'))
    for tokens, cost in [(11, Decimal('.1')), (1, Decimal('.2'))]:
        with pytest.raises(AdmissionConflict):
            await reserve(repo, binding, purpose='review', tokens=tokens, review_cost_hold=cost)
    a = await reserve(repo, binding, purpose='review', tokens=10, review_cost_hold=Decimal('.1'))
    await issue(repo, binding, a)
    await finish(repo, binding, a, usage=UsageObservation(0, 0, 0, Decimal(0)))
    with pytest.raises(AdmissionConflict):
        await reserve(repo, binding, purpose='review', tokens=1, review_cost_hold=Decimal('.01'))
    await reserve(repo, binding, tokens=100)
    assert (present(await pool(repo, binding))).tokens_remaining == 0


async def test_accounting_after_cancellation_and_recovery_does_not_replace_terminal(storage: Storage) -> None:
    repo, binding, _ = await prepare(storage)
    attempt = await reserve(repo, binding)
    await issue(repo, binding, attempt)
    async with storage[0]() as session, session.begin():
        await session.execute(update(RunRow).where(RunRow.id == binding['run_id'])
            .values(status='cancelled', worker_lease_token=None))
    terminal = await finish(repo, binding, attempt, usage=UsageObservation(1, 2, 3, Decimal('.1')))
    assert terminal.state == 'completed'
    assert await repo.recover_issued(binding['tenant_id'], binding['run_id'],
        attempt.attempt_id) == terminal
    with pytest.raises(AdmissionConflict):
        await reserve(repo, binding, tokens=1)


async def test_event_failure_rolls_back_hold_and_count(storage: Storage) -> None:
    repo, binding, _ = await prepare(storage)
    # Force a real database failure after counter/attempt writes, at the event INSERT.
    async with storage[0]() as session, session.begin():
        await session.execute(text('ALTER TABLE agent_hub_run_admission_events ADD CONSTRAINT '
            "task2b_rollback_probe CHECK (kind <> 'prepared') NOT VALID"))
    try:
        with pytest.raises(AdmissionConflict):
            await reserve(repo, binding, purpose='review')
    finally:
        async with storage[0]() as session, session.begin():
            await session.execute(text('ALTER TABLE agent_hub_run_admission_events '
                'DROP CONSTRAINT task2b_rollback_probe'))
    snapshot = present(await pool(repo, binding))
    assert (snapshot.token_charged, snapshot.review_count_charged) == (0, 0)
    async with storage[0]() as session:
        assert await session.scalar(select(func.count()).select_from(RunAdmissionAttemptRow)
            .where(RunAdmissionAttemptRow.run_id == binding['run_id'])) == 0
        assert await session.scalar(select(func.count()).select_from(RunAdmissionEventRow)
            .where(RunAdmissionEventRow.run_id == binding['run_id'])) == 1


async def test_deadline_blocks_even_prepared_issue_and_no_extension(storage: Storage) -> None:
    repo, binding, opening = await prepare(storage)
    attempt = await reserve(repo, binding)
    # A shortened synthetic stored deadline models expiration without sleeping or clock mocks.
    async with storage[0]() as session, session.begin():
        await session.execute(update(RunAdmissionPoolRow).where(
            RunAdmissionPoolRow.run_id == binding['run_id'])
            .values(original_deadline=datetime(2000, 1, 1, tzinfo=UTC)))
    with pytest.raises(AdmissionConflict):
        await issue(repo, binding, attempt)
    with pytest.raises(AdmissionConflict):
        await repo.open_pool(**opening)


async def test_populated_downgrade_refuses_evidence_loss(storage: Storage) -> None:
    repo, binding, _ = await prepare(storage)
    await reserve(repo, binding)
    path = Path(__file__).resolve().parents[3] / 'alembic/versions/0035_run_admission.py'
    spec = importlib.util.spec_from_file_location('task2b_migration', path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert isinstance(migration, MigrationModule)
    async with storage[0]() as session:
        with pytest.raises(DBAPIError, match='admission evidence exists'):
            async with session.begin():
                connection = await session.connection()
                def downgrade(sync: Connection) -> None:
                    with Operations.context(MigrationContext.configure(sync)):
                        migration.downgrade()
                await connection.run_sync(downgrade)
    assert (present(await pool(repo, binding))).token_charged == 60


async def test_caller_transaction_composes_reserve_issue_and_settlement(storage: Storage) -> None:
    repo, binding, _ = await prepare(storage)
    attempt_id = uuid4()
    reservation_args: ReservationArguments = {**binding, 'attempt_id': attempt_id, 'purpose': 'primary',
        'request_sha256': 'a' * 64, 'deployment': DEPLOYMENT, 'token_hold': 60, 'review_cost_hold': None}
    async with storage[0]() as session:
        with pytest.raises(RuntimeError, match='caller rollback'):
            async with session.begin():
                await repo._reserve_in_session(session, **reservation_args)
                # Stand-in for a caller-owned domain event, in exactly the same transaction.
                await session.execute(update(RunRow).where(RunRow.id == binding['run_id'])
                    .values(version=RunRow.version + 1))
                raise RuntimeError('caller rollback')
    assert (present(await pool(repo, binding))).token_charged == 0
    assert await repo.get_attempt(binding['tenant_id'], binding['run_id'], attempt_id) is None
    attempt = await repo.reserve(**reservation_args)
    physical = uuid4()
    async with storage[0]() as session:
        with pytest.raises(RuntimeError, match='caller rollback'):
            async with session.begin():
                assert await repo._mark_issued_in_session(session, **binding,
                    attempt_id=attempt_id, physical_transport_id=physical)
                raise RuntimeError('caller rollback')
    assert (present(await repo.get_attempt(binding['tenant_id'], binding['run_id'], attempt_id))).state == 'prepared'
    assert await issue(repo, binding, attempt, physical)
    async with storage[0]() as session:
        with pytest.raises(RuntimeError, match='caller rollback'):
            async with session.begin():
                await repo._finish_in_session(session, tenant_id=binding['tenant_id'],
                    run_id=binding['run_id'], attempt_id=attempt_id, lease=binding['lease'],
                    deployment=DEPLOYMENT, outcome='completed',
                    usage=UsageObservation(1, 1, 2, Decimal('.01')))
                raise RuntimeError('caller rollback')
    assert (present(await pool(repo, binding))).token_charged == 60
    assert (present(await repo.get_attempt(binding['tenant_id'], binding['run_id'], attempt_id))).state == 'issued'
    async with storage[0]() as session:
        assert (present(await session.get(RunRow, binding['run_id']))).version == 1


@pytest.mark.parametrize('expires', ['lease', 'deadline'])
async def test_lock_wait_rechecks_database_wall_clock_not_transaction_start(storage: Storage, expires: str) -> None:
    repo, binding, _ = await prepare(storage)
    attempt = await reserve(repo, binding)
    started = LockWaitStarted()
    async with storage[0]() as locker, locker.begin():
        await locker.scalar(select(RunRow).where(RunRow.id == binding['run_id']).with_for_update())
        blocker_pid = await locker.scalar(select(func.pg_backend_pid()))
        async def blocked_issue() -> bool:
            async with storage[0]() as waiter, waiter.begin():
                transaction_start = await waiter.scalar(select(func.now()))
                assert isinstance(transaction_start, datetime)
                started.transaction_start = transaction_start
                started.set()
                return await repo._mark_issued_in_session(waiter, **binding,
                    attempt_id=attempt.attempt_id, physical_transport_id=uuid4())
        task = asyncio.create_task(blocked_issue())
        try:
            await started.wait()
            async with asyncio.timeout(10):
                while not await locker.scalar(text('SELECT EXISTS (SELECT 1 FROM '
                    'pg_stat_activity WHERE :blocker = ANY(pg_blocking_pids(pid)))'),
                    {'blocker': blocker_pid}):
                    await asyncio.sleep(.01)
            expiry = present(started.transaction_start) + timedelta(microseconds=1)
            wall_clock = await locker.scalar(select(func.clock_timestamp()))
            assert isinstance(wall_clock, datetime)
            assert present(started.transaction_start) < expiry < wall_clock
            if expires == 'lease':
                await locker.execute(update(RunRow).where(RunRow.id == binding['run_id'])
                    .values(worker_lease_expires_at=expiry))
            else:
                await locker.execute(update(RunAdmissionPoolRow).where(
                    RunAdmissionPoolRow.run_id == binding['run_id']).values(original_deadline=expiry))
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
    with pytest.raises(AdmissionConflict):
        await task
    assert (present(await repo.get_attempt(binding['tenant_id'], binding['run_id'], attempt.attempt_id))).state == 'prepared'


async def test_physical_identity_cannot_be_shared_and_prepared_is_not_recoverable(storage: Storage) -> None:
    repo, binding, _ = await prepare(storage)
    first = await reserve(repo, binding, tokens=10)
    second = await reserve(repo, binding, tokens=10)
    with pytest.raises(AdmissionConflict):
        await repo.recover_issued(binding['tenant_id'], binding['run_id'], first.attempt_id)
    physical = uuid4()
    await issue(repo, binding, first, physical)
    with pytest.raises(AdmissionConflict):
        await issue(repo, binding, second, physical)
    assert (present(await repo.get_attempt(binding['tenant_id'], binding['run_id'], second.attempt_id))).state == 'prepared'


async def test_malformed_usage_is_persisted_without_jsonb_or_encoding_escape(storage: Storage) -> None:
    for raw in ['\ud800' * 1025, '\ud800', '\x00']:
        repo, binding, _ = await prepare(storage)
        attempt = await reserve(repo, binding)
        await issue(repo, binding, attempt)
        malformed_usage = UsageObservation(1, 1, 2, None)
        object.__setattr__(malformed_usage, 'cost_usd', raw)
        result = await finish(repo, binding, attempt, usage=malformed_usage)
        assert result.state == 'unknown'
        receipt = json.loads(present(result.receipt_bytes))['usage']['cost_usd']
        if len(raw) <= 1024:
            assert bytes.fromhex(receipt['invalid_string_utf8_hex']) == raw.encode('utf-8', 'surrogatepass')
        else:
            assert receipt['invalid_length'] == 1025
        assert not (present(await pool(repo, binding))).closed
        assert (present(await pool(repo, binding))).token_charged == 60


async def test_current_worker_status_boundaries_do_not_infer_authority_from_lease(storage: Storage) -> None:
    repo, binding, opening = await prepare(storage)
    attempt = await reserve(repo, binding, tokens=1)
    for status in RunStatus:
        if status is RunStatus.RUNNING:
            continue
        async with storage[0]() as session, session.begin():
            await session.execute(update(RunRow).where(RunRow.id == binding['run_id'])
                .values(status=status.value))
        # Keep the valid lease deliberately: even that cannot make a waiting,
        # unclaimed or terminal RunRow executable in the current worker lifecycle.
        for operation in [repo.open_pool(**opening), reserve(repo, binding, tokens=1),
                          issue(repo, binding, attempt)]:
            with pytest.raises(AdmissionConflict):
                await operation
    assert (present(await pool(repo, binding))).token_charged == 1


async def test_actual_worker_retry_claim_and_final_synthesis_events_remain_admissible(storage: Storage) -> None:
    repo, binding, opening = await prepare(storage)
    runs = RunRepository(storage[0])
    next_lease = _WorkerLease('next-worker', uuid4())
    async with storage[0]() as session, session.begin():
        await session.execute(update(RunRow).where(RunRow.id == binding['run_id'])
            .values(status=RunStatus.RETRYING.value))
        claim_result = await runs.claim_for_execution(session, binding['run_id'],
            allow_running_recovery=False, worker_id=next_lease.worker_id,
            worker_lease_token=next_lease.token,
            worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=30))
        assert isinstance(claim_result, tuple)
        claimed, _ = claim_result
        assert claimed.status == RunStatus.RUNNING.value
        for sequence, kind in enumerate((EventKind.STEP_RETRYING, EventKind.STEP_STARTED), 1):
            reason_fields: EventReason = (
                {'reason': 'synthetic model retry'} if kind is EventKind.STEP_RETRYING else {})
            await runs.persist_event(session, tenant_id=binding['tenant_id'], run_id=binding['run_id'],
                event=RunEvent(kind=kind, sequence=sequence, run_id=binding['run_id'],
                    step_id='final_synthesis', actor='final_synthesizer',
                    **reason_fields))
        assert claimed.status == RunStatus.RUNNING.value
    binding = {**binding, 'lease': next_lease}
    reopened: Opening = {**opening, 'lease': next_lease}
    assert (await repo.open_pool(**reopened)).token_charged == 0
    attempt = await reserve(repo, binding, tokens=1)
    assert await issue(repo, binding, attempt)


@pytest.mark.parametrize('corruption', ['route', 'mode', 'deployment'])
async def test_corrupted_persisted_prerequisites_cannot_issue(storage: Storage, corruption: str) -> None:
    repo, binding, _ = await prepare(storage)
    attempt = await reserve(repo, binding, tokens=10)
    async with storage[0]() as session, session.begin():
        if corruption == 'deployment':
            await session.execute(update(RunAdmissionAttemptRow)
                .where(RunAdmissionAttemptRow.attempt_id == attempt.attempt_id).values(deployment={}))
        else:
            values = {'routing_decision': ['not-a-server-envelope']} if corruption == 'route' else {'mode': None}
            await session.execute(update(RunRow).where(RunRow.id == binding['run_id']).values(**values))
    with pytest.raises(AdmissionConflict, match='^admission state conflict$'):
        await issue(repo, binding, attempt)
    async with storage[0]() as session:
        row = present(await session.get(RunAdmissionAttemptRow, attempt.attempt_id))
        assert row.state == 'prepared' and row.physical_transport_id is None
    assert (present(await pool(repo, binding))).token_charged == 10


@pytest.mark.parametrize('usage', [UsageObservation(60, 1, 61, None),
    UsageObservation(None, None, None, Decimal('.6')),
    UsageObservation(2**63, 0, 2**63, None)])
async def test_valid_partial_bound_break_closes_pool_despite_other_missing_fields(storage: Storage, usage: UsageObservation) -> None:
    repo, binding, _ = await prepare(storage)
    attempt = await reserve(repo, binding, purpose='review')
    await issue(repo, binding, attempt)
    result = await finish(repo, binding, attempt, usage=usage)
    assert result.state == 'invalid'
    snapshot = present(await pool(repo, binding))
    assert snapshot.closed and snapshot.token_charged == 60
    assert snapshot.review_cost_charged == Decimal('.5')
    assert json.loads(present(result.receipt_bytes))['usage']['total_tokens'] == usage.total_tokens
    with pytest.raises(AdmissionConflict):
        await reserve(repo, binding, tokens=1)


@pytest.mark.parametrize('cost,broken', [
    (Decimal('0.6000000'), True), (Decimal(1_000_001), True),
    (Decimal('0.1000000'), False),
])
async def test_actual_cost_outside_hold_storage_limits_preserves_receipt_and_pool_fence(storage: Storage, cost: Decimal, broken: bool) -> None:
    repo, binding, _ = await prepare(storage)
    attempt = await reserve(repo, binding, purpose='review')
    await issue(repo, binding, attempt)
    result = await finish(repo, binding, attempt, usage=UsageObservation(1, 1, 2, cost))
    assert result.state == ('invalid' if broken else 'unknown')
    assert json.loads(present(result.receipt_bytes))['usage']['cost_usd'] == {'decimal': str(cost)}
    snapshot = present(await pool(repo, binding))
    assert snapshot.closed is broken
    assert (snapshot.token_charged, snapshot.review_token_charged,
        snapshot.review_count_charged, snapshot.review_cost_charged) == (60, 60, 1, Decimal('.5'))
    if broken:
        with pytest.raises(AdmissionConflict):
            await reserve(repo, binding, tokens=1)
    else:
        await reserve(repo, binding, tokens=40, request_sha256='b' * 64)
        assert (present(await pool(repo, binding))).token_charged == 100
        assert await repo.get_attempt(binding['tenant_id'], binding['run_id'], attempt.attempt_id) == result


async def test_in_session_duplicate_physical_is_private_conflict_and_caller_rolls_back(storage: Storage) -> None:
    repo, binding, _ = await prepare(storage)
    first = await reserve(repo, binding, tokens=10)
    second = await reserve(repo, binding, tokens=10)
    physical = uuid4()
    await issue(repo, binding, first, physical)
    additional_id = uuid4()
    async with storage[0]() as session:
        with pytest.raises(AdmissionConflict, match='^admission state conflict$'):
            async with session.begin():
                await repo._reserve_in_session(session, **binding, attempt_id=additional_id,
                    purpose='primary', request_sha256='c' * 64, deployment=DEPLOYMENT,
                    token_hold=10, review_cost_hold=None)
                await session.execute(update(RunRow).where(RunRow.id == binding['run_id'])
                    .values(version=RunRow.version + 1))
                try:
                    await repo._mark_issued_in_session(session, **binding,
                        attempt_id=second.attempt_id, physical_transport_id=physical)
                except AdmissionConflict as error:
                    assert str(binding['lease'].token) not in str(error)
                    # Repository translates the failure but never ends the caller's
                    # transaction; its context manager must perform the full rollback.
                    assert session.in_transaction() and not session.is_active
                    raise
    assert (present(await pool(repo, binding))).token_charged == 20
    assert await repo.get_attempt(binding['tenant_id'], binding['run_id'], additional_id) is None
    current = present(await repo.get_attempt(binding['tenant_id'], binding['run_id'], second.attempt_id))
    assert current.state == 'prepared' and current.physical_transport_id is None
    assert (present(await repo.get_attempt(binding['tenant_id'], binding['run_id'], first.attempt_id))).state == 'issued'
    async with storage[0]() as session:
        assert (present(await session.get(RunRow, binding['run_id']))).version == 1
        assert await session.scalar(select(func.count()).select_from(RunAdmissionEventRow)
            .where(RunAdmissionEventRow.run_id == binding['run_id'])) == 4
