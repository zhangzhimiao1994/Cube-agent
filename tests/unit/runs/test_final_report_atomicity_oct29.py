"""Final Main publication boundaries; all models/tools are owned offline fixtures."""
from __future__ import annotations

import types
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import JSON, Column, MetaData, String, Table, UniqueConstraint, func, select
from sqlalchemy import event as sql_event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.engine import Connection, ExecutionContext
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent_hub.db.models import RunArtifactRow, RunCheckpointRow, RunEventRow, RunRow
from agent_hub.domain.runs import TaskMode
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import _delivery_publication_batches
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    EventKind,
    ExecutionRuntime,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.hybrid import HybridRuntime
from tests.unit.harness.review_typed_fixture import UnusedConfigs, UnusedSecrets, unused_capacity
from tests.unit.runs.final_report_boundaries_typed_fixture import REPORT, SQLCase, report_context
from tests.unit.runs.reviewed_report_revision_typed_fixture import (
    integer_value,
    object_map,
    read_map,
    string_value,
)
from tests.unit.runtime.crew.test_framework_parser_boundary import (
    installed_parser,  # noqa: F401
    offline_only,  # noqa: F401
)
from tests.unit.runtime.test_direct_project_verification import FIXED, Model, batch, runner


@dataclass
class HybridCase:
    context: TaskContext
    events: tuple[RunEvent, ...]
    runtime: HybridRuntime
    repository: InMemoryArtifactRepository
    reporter: Model

    @property
    def main_index(self) -> int:
        return next(i for i, event in enumerate(self.events)
                    if event.artifact is not None and event.artifact.producer == 'main'
                    and 'delivery_report_binding' in event.artifact.content)


@pytest.fixture
async def hybrid_case(tmp_path: Path, request: pytest.FixtureRequest) -> HybridCase:
    from agent_hub.runtime.direct import DirectRuntime
    from tests.unit.capabilities.test_project_verification import Executor
    from tests.unit.runs.final_report_boundaries_typed_fixture import context, responses, subject
    from tests.unit.runtime.crew.test_adapter_failure_reason import WorkspaceDeliverySequenceGateway
    from tests.unit.runtime.crew.test_engineering_delivery_execution import ActualCapabilityHarness
    from tests.unit.runtime.test_hybrid import MultiArtifactRuntime, artifact

    parser = request.getfixturevalue('installed_parser')
    assert isinstance(parser, types.ModuleType)
    base = context()
    ctx = base.model_copy(update={'mode': TaskMode.HYBRID, 'routing_decision': {
        **base.routing_decision, 'engineering_delivery_contract_version': 1,
        'engineering_hybrid_report_version': 1,
    }})
    repository = InMemoryArtifactRepository()
    reporter = Model(REPORT)
    native = HybridRuntime(
        subject(WorkspaceDeliverySequenceGateway(responses()),
                ActualCapabilityHarness(tmp_path, Executor()), repository, parser),
        MultiArtifactRuntime(TaskMode.DISCUSS, (artifact('reviewer', 'Reviewed.'),)),
        DirectRuntime(reporter, logical_model='main'), artifact_repository=repository,
    )
    events = tuple([event async for event in native.run(ctx)])
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    return HybridCase(ctx, events, native, repository, reporter)


def changed_checkpoint(value: object) -> RuntimeCheckpoint:
    payload = object_map(value)
    payload.pop('state_sha256', None)
    for name in ('id', 'run_id', 'tenant_id'):
        payload[name] = UUID(str(payload[name]))
    payload['mode'] = TaskMode(string_value(payload['mode']))
    return RuntimeCheckpoint.model_validate(payload)


async def stream(events: tuple[RunEvent, ...]) -> AsyncIterator[RunEvent]:
    for event in events:
        yield event


async def test_direct_main_without_checkpoint_is_never_published(tmp_path: Path) -> None:
    context = report_context()
    runtime, _, _, _ = runner(tmp_path, context, Model(batch({'main.py': FIXED}), REPORT))
    events = tuple([event async for event in runtime.run(context)])
    main_index = next(i for i, event in enumerate(events)
                      if event.kind is EventKind.ARTIFACT_CREATED
                      and event.artifact is not None and event.artifact.producer == 'main')
    main = events[main_index].artifact
    published: list[RunEvent] = []
    with pytest.raises(RuntimeError, match='publication.*checkpoint'):
        async for group in _delivery_publication_batches(stream(events[:main_index + 1]), context, runtime=runtime):
            published.extend(group)
            assert not any(event.artifact == main for event in published), (
                'Main was publicly emitted before its recovery checkpoint'
            )


async def test_direct_main_and_checkpoint_are_one_publication(tmp_path: Path) -> None:
    context = report_context()
    runtime, _, _, _ = runner(tmp_path, context, Model(batch({'main.py': FIXED}), REPORT))
    events = tuple([event async for event in runtime.run(context)])
    groups = [group async for group in _delivery_publication_batches(stream(events), context, runtime=runtime)]
    main_group = next(group for group in groups if any(
        event.artifact is not None and event.artifact.producer == 'main' for event in group))
    assert [event.kind for event in main_group] == [
        EventKind.ARTIFACT_CREATED, EventKind.CHECKPOINT_SAVED,
    ]


@pytest.mark.parametrize('defect', ['run', 'tenant', 'hash', 'policy', 'runtime'])
async def test_direct_checkpoint_mismatch_never_publishes_main(tmp_path: Path, defect: str) -> None:
    from uuid import uuid4

    context = report_context()
    runtime, _, _, _ = runner(tmp_path, context, Model(batch({'main.py': FIXED}), REPORT))
    events = [event async for event in runtime.run(context)]
    main_index = next(i for i, event in enumerate(events)
                      if event.artifact is not None and event.artifact.producer == 'main')
    main_event = events[main_index]
    checkpoint_event = events[main_index + 1]
    checkpoint = checkpoint_event.checkpoint
    assert checkpoint is not None
    payload = checkpoint.to_payload()
    if defect in {'run', 'tenant'}:
        payload[defect + '_id'] = str(uuid4())
    elif defect == 'hash':
        object_map(payload['state'])['artifact_sha256'] = 'f' * 64
    elif defect == 'policy':
        object_map(object_map(payload['state'])['delivery_report_policy'])['engineering_direct_report_version'] = True
    else:
        payload['runtime_type'] = 'unrelated'
    bad = changed_checkpoint(payload)
    changed = checkpoint_event.model_copy(update={'checkpoint': bad})
    published: list[RunEvent] = []
    with pytest.raises(RuntimeError, match='publication'):
        async for group in _delivery_publication_batches(
            stream((*events[:main_index + 1], changed)), context, runtime=runtime,
        ):
            published.extend(group)
    assert main_event not in published


async def test_hybrid_main_and_terminal_parent_checkpoint_are_one_publication(
    hybrid_case: HybridCase,
) -> None:
    case = hybrid_case
    groups = [group async for group in _delivery_publication_batches(
        stream(case.events), case.context, runtime=case.runtime,
    )]
    selected = next(group for group in groups if case.events[case.main_index] in group)
    assert [event.kind for event in selected] == [
        EventKind.ARTIFACT_CREATED, EventKind.CHECKPOINT_SAVED,
        EventKind.CHECKPOINT_SAVED, EventKind.CHECKPOINT_SAVED,
    ]
    terminal = selected[-1].checkpoint
    assert terminal is not None and terminal.runtime_type == 'hybrid'
    assert terminal.state['terminal'] is True
    assert terminal.state['next_stage'] == 3
    assert terminal.state['child_checkpoint'] is None


@pytest.mark.parametrize('checkpoint_count', [0, 1, 2])
async def test_hybrid_main_without_terminal_checkpoint_is_never_published(
    hybrid_case: HybridCase, checkpoint_count: int,
) -> None:
    case = hybrid_case
    published: list[RunEvent] = []
    with pytest.raises(RuntimeError, match='publication.*checkpoint'):
        async for group in _delivery_publication_batches(
            stream(case.events[:case.main_index + 1 + checkpoint_count]), case.context, runtime=case.runtime,
        ):
            published.extend(group)
            assert case.events[case.main_index] not in published


@pytest.mark.parametrize('defect', [
    'run', 'tenant', 'child_run', 'child_tenant', 'child_hash', 'missing_lineage',
    'output_hash', 'main_sources',
])
async def test_hybrid_checkpoint_requires_current_child_and_lineage(
    hybrid_case: HybridCase, defect: str,
) -> None:
    from uuid import uuid4

    case = hybrid_case
    main_event = case.events[case.main_index]
    main = main_event.artifact
    checkpoint_event = case.events[case.main_index + 1]
    assert main is not None and checkpoint_event.checkpoint is not None
    payload = checkpoint_event.checkpoint.to_payload()
    state = object_map(payload['state'])
    if defect in {'run', 'tenant'}:
        payload[defect + '_id'] = str(uuid4())
    elif defect.startswith('child_'):
        child = object_map(state['child_checkpoint'])
        assert child is not None
        if defect == 'child_hash':
            object_map(child['state'])['artifact_sha256'] = 'f' * 64
        else:
            child[defect.removeprefix('child_') + '_id'] = str(uuid4())
        state['child_checkpoint'] = changed_checkpoint(child).to_payload()
    elif defect == 'missing_lineage':
        reference = next(key for key in main.source_ids if key != str(main.id))
        object_map(state['artifact_registry']).pop(reference, None)
        object_map(state['output_artifact_registry']).pop(reference, None)
    elif defect == 'output_hash':
        object_map(state['output_artifact_registry'])[str(main.id)] = 'f' * 64
    else:
        main_event = main_event.model_copy(update={'artifact': main.model_copy(update={'source_ids': ()})})
    changed = checkpoint_event.model_copy(update={'checkpoint': changed_checkpoint(payload)})
    published: list[RunEvent] = []
    with pytest.raises(RuntimeError, match='publication'):
        async for group in _delivery_publication_batches(
            stream((*case.events[:case.main_index], main_event, changed,
                    *case.events[case.main_index + 2:])), case.context, runtime=case.runtime,
        ):
            published.extend(group)
    assert main_event not in published


async def test_hybrid_atomic_terminal_checkpoint_recovers_at_zero_budget(
    hybrid_case: HybridCase,
) -> None:
    from tests.unit.runtime.test_hybrid import UnusedRuntime

    case = hybrid_case
    groups = [group async for group in _delivery_publication_batches(
        stream(case.events), case.context, runtime=case.runtime,
    )]
    group = next(group for group in groups if case.events[case.main_index] in group)
    checkpoint = group[-1].checkpoint
    assert checkpoint is not None and checkpoint.state['terminal'] is True
    payload = checkpoint.to_payload()
    object_map(payload['state']).update(remaining_token_budget=0, remaining_timeout_seconds=0.0,
                            remaining_absolute_timeout_seconds=0.0,
                            token_budget_spent=object_map(payload['state'])['token_budget_grant'])
    zero = changed_checkpoint(payload)
    restored = HybridRuntime(
        UnusedRuntime(TaskMode.DISPATCH, 'no tools'),
        UnusedRuntime(TaskMode.DISCUSS, 'no models'),
        UnusedRuntime(TaskMode.DIRECT, 'no synthesis'), artifact_repository=case.repository,
    )
    await restored.restore_checkpoint(zero)
    recovered = [event async for event in restored.run(
        case.context.model_copy(update={'checkpoint': zero}),
    )]
    assert [event.kind for event in recovered] == [EventKind.RUNTIME_COMPLETED], [
        (event.reason, dict(event.payload)) for event in recovered
    ]
    assert len(case.reporter.requests) == 1


@dataclass
class PublicationCase:
    context: TaskContext
    events: tuple[RunEvent, ...]
    main_index: int
    hybrid: HybridCase | None
    native: DirectRuntime | HybridRuntime | None = None


@pytest.fixture(params=[TaskMode.DIRECT, TaskMode.HYBRID])
async def publication_case(
    request: pytest.FixtureRequest, tmp_path: Path, hybrid_case: HybridCase,
) -> PublicationCase:
    if request.param is TaskMode.HYBRID:
        case = hybrid_case
        return PublicationCase(case.context, case.events, case.main_index, case, case.runtime)
    ctx = report_context()
    native, _, _, _ = runner(tmp_path, ctx, Model(batch({'main.py': FIXED}), REPORT))
    events = tuple([item async for item in native.run(ctx)])
    main_index = next(i for i, item in enumerate(events)
                      if item.artifact is not None and item.artifact.producer == 'main')
    return PublicationCase(ctx, events, main_index, None, native)


@pytest.fixture
async def session_repository(publication_case: PublicationCase) -> AsyncIterator[SQLCase]:
    # Same real SQLAlchemy/SQLite type adaptation as test_diagnostic_checkpoint_transition.py.
    # PostgreSQL lease locking/concurrent reader isolation is deliberately not claimed here.
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    metadata = MetaData()
    for model in (RunRow, RunEventRow, RunArtifactRow, RunCheckpointRow):
        table = model.__table__
        assert isinstance(table, Table)
        columns = [Column(c.name, JSON() if isinstance(c.type, JSONB)
                          else String(32) if isinstance(c.type, PGUUID) else c.type,
                          primary_key=c.primary_key, nullable=c.nullable,
                          server_default=c.server_default) for c in table.columns]
        constraints = [UniqueConstraint(*(c.name for c in constraint.columns))
                       for constraint in table.constraints
                       if isinstance(constraint, UniqueConstraint)]
        Table(model.__tablename__, metadata, *columns, *constraints)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        context = publication_case.context
        async with factory.begin() as session:
            session.add(RunRow(id=context.run_id, tenant_id=context.tenant_id,
                actor_id=context.actor_id, actor_role='operator', request=context.request,
                mode=context.mode.value, status='running', version=1,
                routing_decision=dict(context.routing_decision)))
        yield RunRepository(factory), factory, engine
    finally:
        await engine.dispose()


async def persist_delivery_tail(repository: RunRepository, case: PublicationCase) -> None:
    # Exercise the exact existing per-batch session.begin/persist_event transaction boundary.
    # Ignore preceding native activity: only the Main publication and its checkpoints are under test.
    started = False
    async for group in _delivery_publication_batches(stream(case.events), case.context, runtime=case.native):
        if case.events[case.main_index] in group:
            started = True
        if not started or group[0].kind is EventKind.RUNTIME_COMPLETED:
            continue
        async with await repository.run_transaction() as session, session.begin():
            await repository.get_for_update(session, case.context.run_id)
            for item in group:
                sequence = await repository.next_event_sequence(session, case.context.run_id)
                await repository.persist_event(session, tenant_id=case.context.tenant_id,
                    run_id=case.context.run_id, event=item.model_copy(update={'sequence': sequence}))


async def test_real_repository_checkpoint_insert_failure_rolls_back_main(
    publication_case: PublicationCase, session_repository: SQLCase,
) -> None:
    repository, factory, engine = session_repository
    checkpoints = 0

    class SaveFailed(RuntimeError):
        pass

    def fail_terminal_insert(connection: Connection, cursor: object, statement: str, parameters: object,
                             execution_context: ExecutionContext, many: bool) -> None:
        nonlocal checkpoints
        if statement.startswith('INSERT INTO ' + RunCheckpointRow.__tablename__):
            checkpoints += 1
            if checkpoints == (3 if publication_case.hybrid else 1):
                raise SaveFailed('injected terminal checkpoint INSERT failure')

    sql_event.listen(engine.sync_engine, 'before_cursor_execute', fail_terminal_insert)
    try:
        with pytest.raises(SaveFailed):
            await persist_delivery_tail(repository, publication_case)
    finally:
        sql_event.remove(engine.sync_engine, 'before_cursor_execute', fail_terminal_insert)
    async with factory() as session:
        counts = tuple([await session.scalar(select(func.count()).select_from(model))
                        for model in (RunEventRow, RunArtifactRow, RunCheckpointRow)])
    assert counts == (0, 0, 0), 'SQL transaction leaked a partial Main publication'


async def test_real_repository_committed_envelope_cold_recovers_without_work(
    publication_case: PublicationCase, session_repository: SQLCase,
) -> None:
    from agent_hub.runtime.direct import DirectRuntime
    from tests.unit.runtime.test_hybrid import UnusedRuntime

    case = publication_case
    repository, factory, _ = session_repository
    if case.hybrid is not None:
        events = list(case.events)
        terminal = events[case.main_index + 3]
        assert terminal.checkpoint is not None
        payload = terminal.checkpoint.to_payload()
        object_map(payload['state']).update(remaining_token_budget=0, remaining_timeout_seconds=0.0,
            remaining_absolute_timeout_seconds=0.0,
            token_budget_spent=object_map(payload['state'])['token_budget_grant'])
        events[case.main_index + 3] = terminal.model_copy(update={
            'checkpoint': changed_checkpoint(payload),
        })
        case = PublicationCase(case.context, tuple(events), case.main_index, case.hybrid, case.native)
    await persist_delivery_tail(repository, case)
    # New repository and new sessions reconstruct actual stored event/artifact/checkpoint bytes.
    cold = RunRepository(factory)
    raw = await cold.raw_events(case.context.tenant_id, case.context.run_id)
    async with factory() as session:
        checkpoint = await cold.latest_checkpoint(session, tenant_id=case.context.tenant_id,
                                                  run_id=case.context.run_id)
    assert checkpoint is not None
    assert len(raw) == (4 if case.hybrid else 2)
    assert not any(item.kind is EventKind.RUNTIME_COMPLETED for item in raw)
    artifacts = tuple(item.artifact for item in raw if item.artifact is not None)
    assert len(artifacts) == 1
    unused = Model()
    restored: DirectRuntime | HybridRuntime
    if case.hybrid is None:
        restored = DirectRuntime(unused, logical_model='main')
    else:
        restored = HybridRuntime(UnusedRuntime(TaskMode.DISPATCH, 'no tools'),
            UnusedRuntime(TaskMode.DISCUSS, 'no models'), UnusedRuntime(TaskMode.DIRECT, 'no main'),
            artifact_repository=case.hybrid.repository)
    await restored.restore_checkpoint(checkpoint)
    recovered = [item async for item in restored.run(case.context.model_copy(update={
        'checkpoint': checkpoint, 'artifacts': artifacts,
    }))]
    assert [item.kind for item in recovered] == [EventKind.RUNTIME_COMPLETED]
    assert not unused.requests


@pytest.mark.parametrize('version', [None, True, 2])
async def test_legacy_direct_publication_is_not_retrofitted(tmp_path: Path, version: int | None) -> None:
    context = report_context()
    runtime, _, _, _ = runner(tmp_path, context, Model(batch({'main.py': FIXED}), REPORT))
    events = tuple([item async for item in runtime.run(context)])
    route = dict(context.routing_decision)
    if version is None:
        route.pop('engineering_direct_report_version')
    else:
        route['engineering_direct_report_version'] = version
    legacy = context.model_copy(update={'routing_decision': route})
    groups = [group async for group in _delivery_publication_batches(stream(events), legacy)]
    assert groups == [(item,) for item in events]


async def test_discuss_existing_atomic_envelope_is_unchanged() -> None:
    from tests.unit.runs.final_report_boundaries_typed_fixture import make_case

    case = await make_case()
    envelope = case.events[case.report_index - 1:case.report_index + 2]
    previous = next(item.checkpoint for item in reversed(case.events[:case.report_index - 1])
                    if item.checkpoint is not None)
    ctx = case.task.model_copy(update={'checkpoint': previous})
    groups = [group async for group in _delivery_publication_batches(
        stream(envelope), ctx, runtime=case.runtime,
    )]
    assert groups == [envelope]


async def test_hybrid_resume_uses_only_exact_claimed_checkpoint_event_prefix(
    hybrid_case: HybridCase,
) -> None:
    case = hybrid_case
    boundary = max(i for i, item in enumerate(case.events[:case.main_index])
                   if item.checkpoint is not None)
    checkpoint = case.events[boundary].checkpoint
    assert checkpoint is not None
    resumed = case.context.model_copy(update={'checkpoint': checkpoint})
    groups = [group async for group in _delivery_publication_batches(
        stream(case.events[boundary + 1:]), resumed, runtime=case.runtime,
        recovery_events=case.events[:boundary + 1],
    )]
    assert len(next(group for group in groups if case.events[case.main_index] in group)) == 4


async def test_historical_context_cannot_replace_current_hybrid_lineage(
    hybrid_case: HybridCase,
) -> None:
    case = hybrid_case
    history = tuple(item.artifact for item in case.events[:case.main_index]
                    if item.artifact is not None)
    context = case.context.model_copy(update={'artifacts': history})
    published: list[RunEvent] = []
    with pytest.raises(RuntimeError, match='publication.*lineage'):
        async for group in _delivery_publication_batches(
            stream(case.events[case.main_index:]), context, runtime=case.runtime,
        ):
            published.extend(group)
    assert not published


async def test_legacy_hybrid_is_not_retrofitted(hybrid_case: HybridCase) -> None:
    case = hybrid_case
    route = dict(case.context.routing_decision)
    route.pop('engineering_hybrid_report_version')
    legacy = case.context.model_copy(update={'routing_decision': route})
    groups = [group async for group in _delivery_publication_batches(stream(case.events), legacy)]
    assert groups == [(item,) for item in case.events]


async def test_unrestorable_hybrid_terminal_budget_never_publishes_main(
    hybrid_case: HybridCase,
) -> None:
    case = hybrid_case
    events = list(case.events)
    terminal = events[case.main_index + 3]
    assert terminal.checkpoint is not None
    payload = terminal.checkpoint.to_payload()
    # The failed zero-budget fixture exposed this native-invalid state: no new budget is granted.
    object_map(payload['state'])['remaining_token_budget'] = 0
    assert integer_value(object_map(payload['state'])['token_budget_grant']) > integer_value(object_map(payload['state'])['token_budget_spent'])
    events[case.main_index + 3] = terminal.model_copy(update={
        'checkpoint': changed_checkpoint(payload),
    })
    published: list[RunEvent] = []
    with pytest.raises(RuntimeError, match='publication.*checkpoint'):
        async for group in _delivery_publication_batches(
            stream(tuple(events)), case.context, runtime=case.runtime,
        ):
            published.extend(group)
    assert case.events[case.main_index] not in published


async def test_conflicting_recovery_artifact_identity_is_not_hidden_by_equal_content_hash(
    hybrid_case: HybridCase,
) -> None:
    from uuid import uuid4

    case = hybrid_case
    boundary = max(i for i, item in enumerate(case.events[:case.main_index])
                   if item.checkpoint is not None)
    checkpoint = case.events[boundary].checkpoint
    assert checkpoint is not None
    prefix = list(case.events[:boundary + 1])
    original = next(item for item in prefix if item.artifact is not None
                    and item.artifact.type == 'model_response'
                    and str(item.artifact.id) in read_map(checkpoint.state['artifact_registry']))
    assert original.artifact is not None
    changed = original.artifact.model_copy(update={'source_ids': (str(uuid4()),)})
    prefix.insert(prefix.index(original) + 1, original.model_copy(update={'artifact': changed}))
    prefix = [item.model_copy(update={'sequence': i + 1}) for i, item in enumerate(prefix)]
    with pytest.raises(RuntimeError, match='publication.*ambiguous'):
        _ = [group async for group in _delivery_publication_batches(
            stream(case.events[boundary + 1:]), case.context.model_copy(update={'checkpoint': checkpoint}),
            runtime=case.runtime, recovery_events=tuple(prefix),
        )]


async def test_native_invalid_direct_next_sequence_never_publishes_main(tmp_path: Path) -> None:
    from agent_hub.runtime.direct import DirectRuntime

    ctx = report_context()
    native, _, _, _ = runner(tmp_path, ctx, Model(batch({'main.py': FIXED}), REPORT))
    events = [item async for item in native.run(ctx)]
    index = next(i for i, item in enumerate(events)
                 if item.artifact is not None and item.artifact.producer == 'main')
    checkpoint_event = events[index + 1]
    assert checkpoint_event.checkpoint is not None
    payload = checkpoint_event.checkpoint.to_payload()
    object_map(payload['state'])['next_sequence'] = 0
    bad = changed_checkpoint(payload)
    assert not DirectRuntime._is_completed_checkpoint_state(bad)
    events[index + 1] = checkpoint_event.model_copy(update={'checkpoint': bad})
    published: list[RunEvent] = []
    with pytest.raises(RuntimeError, match='publication.*checkpoint'):
        async for group in _delivery_publication_batches(stream(tuple(events)), ctx, runtime=native):
            published.extend(group)
    assert events[index] not in published


async def test_native_invalid_hybrid_terminal_timeout_never_publishes_main(
    hybrid_case: HybridCase,
) -> None:
    from agent_hub.runtime.hybrid import RuntimeExecutionError

    case = hybrid_case
    events = list(case.events)
    terminal = events[case.main_index + 3]
    assert terminal.checkpoint is not None
    payload = terminal.checkpoint.to_payload()
    object_map(payload['state'])['remaining_timeout_seconds'] = -1.0
    bad = changed_checkpoint(payload)
    with pytest.raises(RuntimeExecutionError, match='checkpoint is incompatible'):
        case.runtime._validate_checkpoint(bad, case.context)
    events[case.main_index + 3] = terminal.model_copy(update={'checkpoint': bad})
    published: list[RunEvent] = []
    with pytest.raises(RuntimeError, match='publication.*checkpoint'):
        async for group in _delivery_publication_batches(
            stream(tuple(events)), case.context, runtime=case.runtime,
        ):
            published.extend(group)
    assert case.events[case.main_index] not in published


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.HYBRID])
async def test_live_configured_wrapper_binds_original_tenant_run_and_execution(mode: TaskMode) -> None:
    from uuid import uuid4

    from agent_hub.runtime.contracts import validate_publication_checkpoint
    from agent_hub.runtime.defaults import (
        ConfigBackedDirectRuntime,
        ConfigBackedHybridRuntime,
        _PlannedRuntime,
    )
    from agent_hub.runtime.direct import DirectRuntime
    from agent_hub.runtime.engineering_admission import EngineeringAdmissionRuntime
    from tests.unit.runtime.test_hybrid import MultiArtifactRuntime, artifact

    context = report_context().model_copy(update={
        'mode': mode, 'request': 'Explain this result.', 'routing_decision': {},
        'execution_token': uuid4(),
    })
    model = Model(REPORT)
    direct = DirectRuntime(model, logical_model='main')
    native = direct if mode is TaskMode.DIRECT else HybridRuntime(
        MultiArtifactRuntime(TaskMode.DISPATCH, ()),
        MultiArtifactRuntime(TaskMode.DISCUSS, (artifact('reviewer', 'Reviewed.'),)), direct,
    )
    planned = _PlannedRuntime(native, mode=mode, main_agent_model='main', roles=(), steps=())
    admission = EngineeringAdmissionRuntime(planned, None)
    with pytest.raises(RuntimeError, match='child checkpoint unavailable'):
        admission.validate_publication_checkpoint(RuntimeCheckpoint(
            id=uuid4(), runtime_type='unused', runtime_version='1', run_id=context.run_id,
            tenant_id=context.tenant_id, mode=mode, state={},
        ), context)

    class ConfiguredDirect(ConfigBackedDirectRuntime):
        builds = 0

        async def _runtime_for(self, observed: TaskContext) -> ExecutionRuntime:
            self.builds += 1
            return admission

    class ConfiguredHybrid(ConfigBackedHybridRuntime):
        builds = 0

        async def _runtime_for(self, observed: TaskContext) -> ExecutionRuntime:
            self.builds += 1
            return admission

    configured_type = ConfiguredDirect if mode is TaskMode.DIRECT else ConfiguredHybrid
    configured = configured_type(config_service=UnusedConfigs(), secret_service=UnusedSecrets(),
                                 capacity_factory=unused_capacity, transport=LiteLLMClient())
    saved = None
    # Validation runs while the real configured/admission/planned/native generators are suspended.
    async for event in configured.run(context):
        if event.checkpoint is None:
            continue
        saved = event.checkpoint
        validate_publication_checkpoint(configured, saved, context)
        foreign = uuid4()
        with pytest.raises((RuntimeError, ValueError)):
            validate_publication_checkpoint(configured, saved.model_copy(update={'tenant_id': foreign}),
                                            context.model_copy(update={'tenant_id': foreign}))
        for identity in ({'run_id': uuid4()}, {'execution_token': None}, {'execution_token': uuid4()}):
            with pytest.raises((RuntimeError, ValueError)):
                validate_publication_checkpoint(configured, saved, context.model_copy(update=identity))
        payload = saved.to_payload()
        if mode is TaskMode.DIRECT:
            object_map(payload['state'])['next_sequence'] = 0
        else:
            object_map(payload['state'])['remaining_timeout_seconds'] = -1.0
        with pytest.raises((RuntimeError, ValueError)):
            validate_publication_checkpoint(configured, changed_checkpoint(payload), context)
    assert saved is not None
    with pytest.raises((RuntimeError, ValueError)):
        validate_publication_checkpoint(configured, saved, context)
    assert configured.builds == 1
    assert len(model.requests) == 1


async def test_generated_direct_native_validation_keeps_inspection_from_original_context(
    tmp_path: Path,
) -> None:
    from agent_hub.runtime.direct import DirectRuntime
    from tests.unit.runs.final_report_boundaries_typed_fixture import GeneratedHarness
    from tests.unit.runtime.test_direct_generated_entry import Harness, response
    from tests.unit.runtime.test_direct_generated_entry import Model as GeneratedModel
    from tests.unit.runtime.test_direct_generated_entry import batch as generated_batch
    from tests.unit.runtime.test_generated_project_history import evidence

    context, verified, bundle = await evidence(tmp_path)
    bundle = {**bundle, 'artifact_id': 'fixture-generated-bundle'}
    context = context.model_copy(update={'token_budget': 50_000, 'routing_decision': {
        **context.routing_decision, 'engineering_direct_report_version': 1,
    }})
    harness = Harness(context, verified, bundle)
    model = GeneratedModel(response(generated_batch()), response(REPORT))
    native = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
                           harness_tool_gateway=GeneratedHarness(harness))
    events = tuple([item async for item in native.run(context)])
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await native.save_checkpoint()
    layout = read_map(checkpoint.state['generated_event_layout'])
    inspection = next(item.artifact for item in events if item.artifact is not None
                      and str(item.artifact.id) == layout['inspection_artifact_id'])
    resumed = context.model_copy(update={'artifacts': (inspection,)})
    tail = tuple(item for item in events if item.artifact is None or item.artifact.id != inspection.id)
    groups = [group async for group in _delivery_publication_batches(stream(tail), resumed, runtime=native)]
    main = next(group for group in groups if any(item.artifact is not None
                and item.artifact.producer == 'main' for item in group))
    assert len(main) == 2
    assert len(model.requests) == 2


async def test_hybrid_native_validator_receives_original_inputs_without_promoting_them(
    hybrid_case: HybridCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    from agent_hub.runtime.contracts import Artifact

    case = hybrid_case
    original = Artifact(id=uuid4(), type='text', producer='source_context',
                        content={'text': 'Original read-only project context.'})
    context = case.context.model_copy(update={'artifacts': (original,)})
    validate = case.runtime.validate_publication_checkpoint
    calls: list[UUID] = []

    def observe(checkpoint: RuntimeCheckpoint, observed: TaskContext) -> None:
        assert original in observed.artifacts
        calls.append(checkpoint.id)
        validate(checkpoint, observed)

    monkeypatch.setattr(case.runtime, 'validate_publication_checkpoint', observe)
    groups = [group async for group in _delivery_publication_batches(
        stream(case.events), context, runtime=case.runtime,
    )]
    assert len(next(group for group in groups if case.events[case.main_index] in group)) == 4
    assert len(calls) == 3


async def test_original_context_and_new_event_artifact_identity_conflict_is_rejected(
    tmp_path: Path,
) -> None:

    context = report_context()
    native, _, _, _ = runner(tmp_path, context, Model(batch({'main.py': FIXED}), REPORT))
    events = tuple([item async for item in native.run(context)])
    main = next(item.artifact for item in events if item.artifact is not None
                and item.artifact.producer == 'main')
    conflicting = type(main)(id=main.id, version=main.version, type=main.type,
        producer=main.producer, source_ids=main.source_ids, provenance=main.provenance,
        content={**main.content, 'text': REPORT + ' A conflicting original input.'})
    assert conflicting.content_sha256 != main.content_sha256
    context = context.model_copy(update={'artifacts': (conflicting,)})
    published: list[RunEvent] = []
    with pytest.raises(RuntimeError, match='publication.*ambiguous'):
        async for group in _delivery_publication_batches(stream(events), context, runtime=native):
            published.extend(group)
    assert not any(item.artifact == main for item in published)
