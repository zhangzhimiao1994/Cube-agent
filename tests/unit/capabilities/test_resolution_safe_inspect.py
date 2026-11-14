"""Real preparer -> portable broker -> exact durable receipt reconciliation.

Only native unit/process/Root-file IO is replaced. No Root, provider, network,
clock-store test, approval write or real business run is executed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

import pytest
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    event,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.sqlite.base import SQLiteCompiler
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.sql.elements import BinaryExpression
from sqlalchemy.sql.operators import custom_op

from agent_hub.capabilities.approvals import (
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.execution_authority import (
    ExecutionAuthorityError,
    bind_execution_authority,
)
from agent_hub.capabilities.generated_project_service import GeneratedProjectError
from agent_hub.capabilities.python_resolution_authorization import resolution_approval_binding
from agent_hub.capabilities.python_resolution_preparation import (
    PythonResolutionPreparation,
    existing_resolution_publication_approved,
    resolution_action,
)
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.db.models import RunApprovalRow, RunRow
from agent_hub.runs import execution_clock as clock
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.skills.sandbox import python_metadata_preparation_broker as broker_api
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import ProjectExecutionBroker, _Execution
from agent_hub.skills.sandbox.python_registry_protocol import DependencyNeed, ResolutionError
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionFailureDiagnostic,
    ResolutionPlan,
    ResolutionRequest,
    ResolutionStatus,
    resolution_key,
)
from agent_hub.skills.sandbox.python_resolution_store import ResolutionLeaseWitness, ResolutionStore
from tests.unit.capabilities import test_python_resolution_preparation as fixtures
from tests.unit.skills import test_python_registry_read as registry
from tests.unit.skills.test_python_dependency_index_store import (
    root_configuration,
    supply_configuration,
)

live = fixtures.live
offline_network = registry.offline_network


@dataclass
class Case:
    client: PythonResolutionPreparation
    plan: ResolutionPlan
    live: fixtures.Live
    broker: ProjectExecutionBroker
    receipt_dir: Path
    requests: list[ResolutionRequest] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    existing_checks: int = 0
    effectful_post_checks: int = 0
    committed: bool = True
    cleanup_incomplete: bool = False
    root_timer: bool = False
    revoke_before: bool = False
    revoke_after: bool = False
    changed_source: bool = False
    thrown: BaseException | None = None
    inspect_thrown: BaseException | None = None
    corrupt_plan: bool = False
    clock_stop: str = ''
    ordinary_failure: bool = False
    expired_lease: bool = False
    forged_quiet: bool = False
    ledger_reader: Callable[[CapabilityRequest], Awaitable[bool]] | None = None
    after_publication: Callable[[], Awaitable[None]] | None = None

    async def publish(self) -> ResolutionStatus:
        return await self.client.publish(self.plan, needs=(DependencyNeed('demo', 'a' * 64),),
                                         environment=registry.environment(), planning=self.live.planning)


@pytest.fixture
async def case(live: fixtures.Live, tmp_path: Path,
               monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Case]:
    # Real split-clock policy, fixed H and remaining active allowance. No SQL or
    # clock tests are run; the actual claim/physical clamp consumes this scope.
    live.row = replace(live.row, execution_clock_sha256='e' * 64)
    live.planning = live.planning.model_copy(update={
        'invocation': live.planning.invocation.model_copy(update={'timeout_seconds': 1})})
    start = datetime.now(UTC)
    policy = clock.ClockPolicy(100., 1000., 80., 10., 'f' * 64)
    broker = ProjectExecutionBroker(BrokerPolicy(runtime_root=tmp_path / 'broker'))
    receipts = tmp_path / 'receipts'
    receipts.mkdir()
    current: Case

    async def row() -> RunRecord:
        if current.requests and current.clock_stop:
            assert live.row.worker_lease_token is not None
            clock.activate(clock.ClockSnapshot(policy,
                start - timedelta(seconds=1) if current.clock_stop == 'H' else start + timedelta(seconds=1000),
                100. if current.clock_stop == 'A' else 10., start, 'worker',
                live.row.worker_lease_token, 0, 1), start,
                run_id=live.row.id, worker_token=live.row.worker_lease_token)
        if current.requests and current.expired_lease:
            return replace(live.row, worker_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        if current.requests and current.changed_source:
            return replace(live.row, request='changed source request')
        return live.row

    async def approved(request: CapabilityRequest) -> bool:
        if current.requests:
            current.effectful_post_checks += 1
        return True

    async def existing(request: CapabilityRequest) -> bool:
        current.existing_checks += 1
        assert request.run_id == live.row.id
        assert request.arguments['key'] == current.plan.resolution_key
        assert request.arguments['plan_sha256'] == current.plan.sha256
        if current.ledger_reader is not None:
            return await current.ledger_reader(request)
        return not current.revoke_before and not (current.revoke_after and current.existing_checks > 1)

    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        current.requests.append(request)
        if request.action == 'publish_python_resolution' and current.thrown is not None:
            raise current.thrown
        if request.action == 'inspect_python_resolution' and current.inspect_thrown is not None:
            raise current.inspect_thrown
        return await broker_api.handle_resolution_preparation(broker, request,
                                                              peer_uid=broker.policy.allowed_uid)

    async def admit(actual: ProjectExecutionBroker, job: _Execution) -> None:
        assert actual is broker

    async def stop(job: _Execution) -> None:
        current.stops.append(job.unit)
        if current.cleanup_incomplete:
            raise RuntimeError('offline cleanup deliberately incomplete')

    stage_count = 0

    def stage(*, prefix: str, dir: Path) -> str:
        nonlocal stage_count
        stage_count += 1
        result = tmp_path / f'stage-{stage_count}'
        result.mkdir()
        return str(result)

    def read(store: ResolutionStore, name: str, deadline: float) -> object:
        assert deadline > time.monotonic()
        return json.loads((receipts / hashlib.sha256(name.encode()).hexdigest()).read_text(encoding='utf-8'))

    def write(suffix: str, value: object) -> None:
        name = ResolutionStore._name(current.plan.resolution_key, suffix)
        (receipts / hashlib.sha256(name.encode()).hexdigest()).write_text(json.dumps(value), encoding='utf-8')

    async def subprocess(command: tuple[str, ...], limits: object, **kwargs: object) -> SkillResult:
        request = current.requests[-1]
        if request.action == 'publish_python_resolution':
            write('publish-' + current.plan.sha256,
                  {'state': 'unknown', 'plan_sha256': current.plan.sha256})
            if current.committed:
                write('result-' + current.plan.sha256,
                      {'plan_sha256': ('0' * 64 if current.corrupt_plan else current.plan.sha256),
                       'index_sha256': 'a' * 64})
            if current.after_publication is not None:
                await current.after_publication()
            if current.forged_quiet:
                forged = ResolutionStatus(state='unknown', key=request.key,
                    diagnostic=ResolutionFailureDiagnostic(phase='publication',
                        code='resolution_publication_timeout_quiet', timed_out=True, exit_code=2))
                return SkillResult(exit_code=2, timed_out=False, stdout=forged.model_dump_json(), stderr='')
            if current.root_timer:
                await asyncio.sleep(2)  # Actual broker's one-second operation timer interrupts this.
            return SkillResult(exit_code=7 if current.ordinary_failure else 137,
                               timed_out=not current.ordinary_failure, stdout='', stderr='withheld')
        assert request.action == 'inspect_python_resolution'
        assert request.expected_plan_sha256 == current.plan.sha256
        inspected = ResolutionStore(supply_configuration()).inspect_publication(
            request.key, request.claim, request.expected_plan_sha256)
        return SkillResult(exit_code=0, timed_out=False, stdout=inspected.model_dump_json(), stderr='')

    monkeypatch.setattr(broker_api, '_systemd_supported', lambda: True)
    monkeypatch.setattr(broker_api, 'read_root_configuration', root_configuration)
    monkeypatch.setattr(broker_api, 'read_supply_configuration', supply_configuration)
    monkeypatch.setattr(broker_api, 'validate_configuration', lambda *args: None)
    monkeypatch.setattr(broker_api, '_admit_unit', admit)
    monkeypatch.setattr(broker_api, '_private_project_runtime', lambda path: None)
    monkeypatch.setattr(tempfile, 'mkdtemp', stage)
    monkeypatch.setattr(broker_api, '_write_request', lambda path, request: None)
    monkeypatch.setattr(ResolutionLeaseWitness, 'initialize', lambda self, claim=None: None)
    monkeypatch.setattr(broker_api, 'build_metadata_preparation_root_command', lambda *args, **kwargs:
                        ('agent_hub.skills.sandbox.python_metadata_preparation_worker',))
    monkeypatch.setattr(broker_api, 'run_subprocess_with_limits', subprocess)
    monkeypatch.setattr(broker, '_kill_and_confirm', stop)
    monkeypatch.setattr(ResolutionStore, '_read', read)

    # ContextVar token restoration is confined to this fixture; never carry a
    # clock between cases or use a checkpoint as authority.
    token = clock._EXECUTION.set(None)
    try:
        assert live.row.worker_lease_token is not None
        clock.activate(clock.ClockSnapshot(policy, start + timedelta(seconds=1000), 10., start,
            'worker', live.row.worker_lease_token, 0, 1), start,
            run_id=live.row.id, worker_token=live.row.worker_lease_token)
        client = PythonResolutionPreparation(current=row, approved=approved,
                                             existing_approved=existing, transport=transport)
        # current() is used by _claim; initialise the fixture before deriving the plan.
        placeholder = object.__new__(Case)
        placeholder.requests, placeholder.changed_source = [], False
        placeholder.clock_stop, placeholder.expired_lease = '', False
        current = placeholder
        with bind_execution_authority(live.context, worker_id='worker', actor='implementer'):
            original = await client._claim(live.planning)
            plan = ResolutionPlan(claim=original,
                resolution_key=resolution_key(original, (DependencyNeed('demo', 'a' * 64),), registry.environment()),
                descriptor_sha256='b' * 64, overlay_sha256='c' * 64,
                artifacts=(('demo-1-py3-none-any.whl', 'd' * 64, 100),))
            current = Case(client, plan, live, broker, receipts)
            write('plan-' + plan.sha256, plan.model_dump(mode='json'))
            write('intent', original.model_dump(mode='json'))
            yield current
    finally:
        clock._EXECUTION.reset(token)


@pytest.mark.parametrize('timer', [False, True])
async def test_actual_broker_timeout_quiet_then_exact_receipt_continues(case: Case, timer: bool) -> None:
    case.root_timer = timer
    result = await case.publish()
    assert result.state == 'published' and result.plan == case.plan
    assert [r.action for r in case.requests] == ['publish_python_resolution', 'inspect_python_resolution']
    assert case.existing_checks == 2 and case.effectful_post_checks == 0
    assert len(case.stops) == 4  # Both units drained for each existing read/write transport.
    assert all(job.quiet and job.finished.is_set() and not job.slot_acquired
               for job in case.broker.executions.values())
    assert not list(case.receipt_dir.parent.glob('stage-*'))
    assert case.requests[1].key == case.requests[0].key == case.plan.resolution_key
    case.requests[1].claim.require_continuity(case.requests[0].claim)


async def test_unknown_receipt_is_only_inspected_once_and_never_republished(case: Case) -> None:
    case.committed = False
    result = await case.publish()
    assert result.state == 'unknown'
    assert [r.action for r in case.requests] == ['publish_python_resolution', 'inspect_python_resolution']
    assert case.effectful_post_checks == 0


@pytest.mark.parametrize('unsafe', ['cleanup', 'missing_grant', 'revoked_after', 'changed_source', 'corrupt_receipt'])
async def test_no_success_without_quiet_existing_grant_and_exact_receipt(case: Case, unsafe: str) -> None:
    case.cleanup_incomplete = unsafe == 'cleanup'
    case.revoke_before = unsafe == 'missing_grant'
    case.revoke_after = unsafe == 'revoked_after'
    case.changed_source = unsafe == 'changed_source'
    case.corrupt_plan = unsafe == 'corrupt_receipt'
    if unsafe == 'changed_source':
        with pytest.raises(ExecutionAuthorityError):
            await case.publish()
    else:
        assert (await case.publish()).state == 'unknown'
    assert sum(r.action == 'publish_python_resolution' for r in case.requests) == 1
    assert sum(r.action == 'inspect_python_resolution' for r in case.requests) <= 1
    if unsafe in {'cleanup', 'missing_grant', 'changed_source'}:
        assert len(case.requests) == 1


@pytest.mark.parametrize('failure', [TimeoutError('ordinary'), asyncio.CancelledError('caller')])
async def test_unproven_timeout_and_caller_cancellation_propagate_without_inspect(case: Case, failure: BaseException) -> None:
    case.thrown = failure
    with pytest.raises(type(failure)) as caught:
        await case.publish()
    assert caught.value is failure
    assert len(case.requests) == 1 and case.existing_checks == 0


async def test_inspect_timeout_is_not_retried(case: Case) -> None:
    failure = TimeoutError('inspection interrupted')
    case.inspect_thrown = failure
    with pytest.raises(TimeoutError) as caught:
        await case.publish()
    assert caught.value is failure
    assert len(case.requests) == 2


@pytest.mark.parametrize('stop', ['A', 'H', 'lease'])
async def test_remaining_authority_and_active_clock_must_survive_the_timeout(case: Case, stop: str) -> None:
    case.clock_stop = stop if stop != 'lease' else ''
    case.expired_lease = stop == 'lease'
    with pytest.raises(GeneratedProjectError if stop == 'lease' else ResolutionError):
        await case.publish()
    assert len(case.requests) == 1 and case.existing_checks == 0


@pytest.mark.parametrize('missing', ['reader', 'timeout_proof'])
async def test_quiet_alone_or_missing_existing_reader_cannot_reconcile(case: Case, missing: str) -> None:
    if missing == 'reader':
        case.client.existing_approved = None
    else:
        case.ordinary_failure = True
    assert (await case.publish()).state == 'unknown'
    assert len(case.requests) == 1 and case.existing_checks == 0


async def test_worker_stdout_cannot_attest_brokers_cleanup_or_local_timer(case: Case) -> None:
    case.forged_quiet = True
    result = await case.publish()
    assert result.state == 'unknown'
    assert len(case.requests) == 1 and case.existing_checks == 0


@pytest.fixture
async def ledger(case: Case, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[RunRepository, async_sessionmaker[AsyncSession]]]:
    """Actual approval SQL and adapter reader; only SQLite types/operators adapt.

    No generic reader/revalidation/approval matcher is replaced. These are local
    synthetic rows, not PG evidence or a new runtime approval.
    """
    class OperatorCompiler(Protocol):
        def __call__(self, compiler: SQLiteCompiler, binary: BinaryExpression[object],
                     operator: custom_op[object], **kwargs: object) -> str: ...

    # SQLAlchemy leaves this dialect hook untyped; state its actual callback
    # contract at the test-only adapter boundary, without ignoring any imports.
    original_operator = cast(OperatorCompiler, SQLiteCompiler.visit_custom_op_binary)

    def json_operator(compiler: SQLiteCompiler, binary: BinaryExpression[object],
                      operator: custom_op[object], **kwargs: object) -> str:
        if isinstance(binary.left.type, JSONB) and operator.opstring == '?':
            return (f"(json_type({compiler.process(binary.left, **kwargs)}, '$.' || "
                    f"{compiler.process(binary.right, **kwargs)}) IS NOT NULL)")
        return original_operator(compiler, binary, operator, **kwargs)

    monkeypatch.setattr(SQLiteCompiler, 'visit_custom_op_binary', json_operator)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    metadata = MetaData()
    for model in (RunRow, RunApprovalRow):
        source = model.__table__
        assert isinstance(source, Table)
        columns = [Column(c.name, JSON() if isinstance(c.type, JSONB)
            else String(32) if isinstance(c.type, PGUUID) else c.type,
            primary_key=c.primary_key, nullable=c.nullable, server_default=c.server_default)
            for c in source.columns]
        constraints = [UniqueConstraint(*(c.name for c in item.columns))
            for item in source.constraints if isinstance(item, UniqueConstraint)]
        Table(model.__tablename__, metadata, *columns, *constraints)

    def mapper_utc(row: RunRow, context: object) -> None:
        for column in RunRow.__table__.columns:
            value = getattr(row, column.name)
            if isinstance(column.type, DateTime) and isinstance(value, datetime) and value.tzinfo is None:
                setattr(row, column.name, value.replace(tzinfo=UTC))

    event.listen(RunRow, 'load', mapper_utc)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = RunRepository(factory)
        live = case.live.row
        async with factory.begin() as session:
            session.add(RunRow(id=live.id, tenant_id=live.tenant_id, actor_id=live.actor_id,
                actor_role='operator', request=live.request, mode='direct', status='running', version=live.version,
                created_at=live.created_at, routing_decision=live.routing_decision,
                worker_id=live.worker_id, worker_lease_token=live.worker_lease_token,
                worker_lease_expires_at=live.worker_lease_expires_at,
                execution_clock_sha256=live.execution_clock_sha256))

        async def real_reader(request: CapabilityRequest) -> bool:
            return await existing_resolution_publication_approved(repository, request)

        case.ledger_reader = real_reader
        yield repository, factory
    finally:
        event.remove(RunRow, 'load', mapper_utc)
        await engine.dispose()


@pytest.mark.parametrize('kind', ['manual_once', 'resolution_auto', 'conversation', 'recipe', 'automatic_recipe',
                                  'revoked', 'other_source', 'other_identity', 'other_permissions', 'A', 'H'])
async def test_sql_ledger_adapter_accepts_only_actual_resolution_grant(case: Case,
    ledger: tuple[RunRepository, async_sessionmaker[AsyncSession]], kind: str) -> None:
    repository, factory = ledger
    action = resolution_action(case.plan.claim, (DependencyNeed('demo', 'a' * 64),),
                               registry.environment(), plan=case.plan)
    fingerprint = fingerprint_capability_request(action)
    scope = capability_approval_scope(action)
    assert scope is not None
    if kind == 'resolution_auto':
        # Actual production recorder writes the same exact resolution binding
        # consumed by the original reader; no fake boolean or injected matcher.
        await repository.record_capability_approval_review(action.tenant_id, action.run_id,
            approval_id='auto_review_' + hashlib.sha256(fingerprint.encode()).hexdigest()[:24],
            approval_fingerprint=fingerprint, reviewer='offline_review_fixture', reason='synthetic',
            status='approved', approval_scope=scope, approval_request=action)
    else:
        payload: dict[str, object] = {'approval_fingerprint': fingerprint,
            'approval_scope': scope, 'approval_origin': 'builtin', 'decision_scope': 'once',
            'python_resolution_binding': resolution_approval_binding(case.live.row, action)}
        async with factory.begin() as session:
            session.add(RunApprovalRow(id=uuid4(), tenant_id=action.tenant_id, run_id=action.run_id,
                approval_id='approval_sql_existing', action='capability_tool',
                status='approved', payload=payload))

    assert case.ledger_reader is not None
    case.client.approved = case.ledger_reader  # Real SQL checks before the one original publication too.
    statements: list[str] = []
    external_change = False
    after_dispatch: list[tuple[object, str, dict[str, object]]] = []

    async def capture_rows() -> list[tuple[object, str, dict[str, object]]]:
        async with factory() as session:
            return [(row.id, row.status, dict(row.payload))
                    for row in (await session.scalars(select(RunApprovalRow))).all()]

    async def changed_after_dispatch() -> None:
        # Simulate revocation/scope drift by a separate actor after the original
        # approved effect; the reconciliation reader itself never performs DML.
        nonlocal external_change, after_dispatch
        external_change = True
        try:
            if kind in {'A', 'H'}:
                case.clock_stop = kind
            async with factory.begin() as session:
                approval = await session.scalar(select(RunApprovalRow).where(RunApprovalRow.run_id == action.run_id))
                assert approval is not None
                changed = dict(approval.payload)
                if kind == 'conversation':
                    changed['decision_scope'] = 'conversation'
                if kind == 'recipe':
                    changed['python_recipe_review'] = {'resource': 'workspace/current/project-verification/python/' + 'a' * 64}
                if kind == 'automatic_recipe':
                    changed['automatic_review_id'] = 'python_recipe_' + 'a' * 32
                approval.payload = changed
                if kind == 'revoked':
                    approval.status = 'denied'
                row = await session.get(RunRow, action.run_id)
                assert row is not None
                if kind == 'other_source':
                    row.request = 'a different original request'
                if kind == 'other_identity':
                    row.actor_id = uuid4()
                if kind == 'other_permissions':
                    row.routing_decision = {**(row.routing_decision or {}), 'requested_permissions': ['workspace.read']}
            after_dispatch = await capture_rows()
        finally:
            external_change = False

    case.after_publication = changed_after_dispatch

    def capture_sql(connection: object, cursor: object, statement: str, parameters: object,
                    context: object, executemany: bool) -> None:
        if not external_change:
            statements.append(statement.lstrip().split()[0].upper())

    engine = factory.kw['bind']
    assert isinstance(engine, AsyncEngine)
    event.listen(engine.sync_engine, 'before_cursor_execute', capture_sql)
    try:
        if kind in {'A', 'H'}:
            with pytest.raises(ResolutionError):
                await case.publish()
            assert len(case.requests) == 1 and case.existing_checks == 0
        else:
            result = await case.publish()
            if kind in {'manual_once', 'resolution_auto'}:
                assert result.state == 'published' and result.plan == case.plan
                assert len(case.requests) == 2 and case.existing_checks == 2
            else:
                assert result.state == 'unknown' and len(case.requests) == 1
        assert case.effectful_post_checks == 0
        assert statements and set(statements) == {'SELECT'}, 'reconciliation must only read existing ledger'
    finally:
        event.remove(engine.sync_engine, 'before_cursor_execute', capture_sql)
    assert await capture_rows() == after_dispatch


async def test_clock_final_protected_h7200_preserves_exact_authority_and_active_clamp() -> None:
    """Exercise the actual merged _claim branch, not clock/store arithmetic tests."""
    from agent_hub.capabilities.python_resolution_authorization import original_resolution_scope
    from agent_hub.runs.original_python_resolution import _approval_window
    from agent_hub.runtime.contracts import TaskContext, _freeze_object
    from agent_hub.skills.sandbox.project import ProjectInvocation
    from agent_hub.skills.sandbox.python_resolution_protocol import (
        ProtectedOriginalResolutionPlanning,
        ProtectedOriginalResolutionSource,
    )
    from tests.unit.capabilities.test_protected_original_resolution_authorization import (
        SOURCE,
        original,
    )

    record = replace(original(), execution_clock_sha256='e' * 64)
    assert record.actor_role is not None and record.worker_lease_token is not None
    assert record.mode is not None and record.worker_id is not None and record.routing_decision is not None
    source = ProtectedOriginalResolutionSource.model_validate(SOURCE, strict=True)
    scope = original_resolution_scope(record, source=source, actor='main_agent')
    invocation = ProjectInvocation(execution_id='protected_h7200_overlap', tenant_id=record.tenant_id,
        run_id=record.id, project_id=scope.project_id, session_id=scope.session_id,
        sandbox_profile='restricted', timeout_seconds=30)
    planning = ProtectedOriginalResolutionPlanning(invocation=invocation, scope=scope, draft=source,
        expected_root_config_sha256='a' * 64, expected_catalog_sha256='b' * 64)
    context = TaskContext(run_id=record.id, tenant_id=record.tenant_id, actor_id=record.actor_id,
        actor_role=record.actor_role, execution_token=record.worker_lease_token, mode=record.mode,
        request=record.request, routing_decision=_freeze_object(record.routing_decision, name='routing decision'),
        timeout_seconds=3600)

    async def current() -> RunRecord:
        return record

    def protected(live: RunRecord, value: ProtectedOriginalResolutionPlanning) -> int:
        assert original_resolution_scope(live, source=value.draft, actor='main_agent', live=True) == value.scope
        return _approval_window(live)

    token = clock._EXECUTION.set(None)
    try:
        now = datetime.now(UTC)
        clock.activate(clock.ClockSnapshot(clock.ClockPolicy(3600., 7200., 3600., 10., 'f' * 64),
            record.created_at + timedelta(seconds=7200), 20., now, record.worker_id,
            record.worker_lease_token, 0, 1), now, run_id=record.id, worker_token=record.worker_lease_token)
        with bind_execution_authority(context, worker_id=record.worker_id, actor='main_agent'):
            client = PythonResolutionPreparation(current=current, approved=None, protected_original=protected)
            claim = await client._claim(planning)
            assert claim.original_deadline == record.created_at + timedelta(seconds=7200)
            assert claim.task_deadline <= time.monotonic() + 3580
            assert claim.deadline <= time.monotonic() + 60
            wrong = PythonResolutionPreparation(current=current, approved=None,
                protected_original=lambda live, value: protected(live, value) - 1)
            with pytest.raises(ResolutionError, match='resolution_deadline'):
                await wrong._claim(planning)
    finally:
        clock._EXECUTION.reset(token)
