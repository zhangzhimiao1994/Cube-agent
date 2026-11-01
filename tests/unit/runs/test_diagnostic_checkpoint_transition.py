"""The diagnostic checkpoint guard uses the locked DB predecessor and private bytes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from sqlalchemy import (
    JSON,
    Column,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    delete,
    event,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.engine import Connection, ExecutionContext
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent_hub.auth.models import Role
from agent_hub.db.models import (
    RunCheckpointRow,
    RunEventRow,
    RunOutboxRow,
    RunRow,
    RuntimeArtifactRow,
    RuntimeArtifactWriteRow,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunConflict, RunRepository
from agent_hub.runtime.autogen import project_repair_phase
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from tests.unit.runs.checkpoint_project_failure_typed_fixture import (
    ProofCalls,
    checkpoint_state,
    model_table,
    number,
    present,
)

POLICY = 'engineering_diagnostic_allowance_version'
WATERMARK = 'engineering_diagnostic_consumed_through'


@dataclass
class Checkpoints:
    factory: async_sessionmaker[AsyncSession]
    repository: RunRepository
    tenant: UUID = field(default_factory=uuid4)
    run: UUID = field(default_factory=uuid4)
    actor: UUID = field(default_factory=uuid4)
    route: dict[str, object] = field(default_factory=lambda: {
        POLICY: 1, 'project_id': 'project', 'workspace_session_id': 'session',
        'sandbox_profile': 'workspace_write', 'requested_permissions': ['workspace.read'],
        'engineering_diagnostic_submission_version': 1,
        'engineering_delivery_contract_version': 1,
        'engineering_writer_continuation_version': 1,
        'engineering_action_version': 1,
        'engineering_progress_budget_version': 1,
        'engineering_checkpoint_ack_version': 1,
        'engineering_soft_admission_version': 1,
        'runtime_timeout_source': 'project_scale_soft_budget',
        'runtime_timeout_seconds': 40.0,
        'runtime_timeout_soft_seconds': 30.0,
        'runtime_timeout_absolute_seconds': 40.0,
        'runtime_plan_token_budget': 1_000,
        'runtime_token_absolute_tokens': 2_000,
    })

    def checkpoint(self, *, soft: float = 1, watermark: int = 0,
                   artifacts: tuple[Artifact, ...] = (), state_overrides: Mapping[str, object] | None = None, **state: object) -> RuntimeCheckpoint:
        assert state_overrides is None or not set(state_overrides) & {"soft", "watermark", "artifacts", "state_overrides"}
        return RuntimeCheckpoint(id=uuid4(), runtime_type='autogen', runtime_version='2',
            tenant_id=self.tenant, run_id=self.run, mode=TaskMode.DISCUSS, state=checkpoint_state({
                POLICY: 1, WATERMARK: watermark, 'remaining_timeout_seconds': soft,
                'engineering_writer_continuation_version': 1,
                'engineering_action_version': 1,
                'engineering_progress_budget_version': 1,
                'engineering_checkpoint_ack_version': 1,
                'engineering_soft_admission_version': 1,
                'engineering_tool_prefix': 0,
                'remaining_absolute_timeout_seconds': 40.0,
                'usage': {'tokens': 10, 'cost_usd': '0.01'},
                'model_ledger': tuple({'status': 'succeeded', 'artifact_id': str(a.id),
                    'artifact_sha256': a.content_sha256} for a in artifacts if a.type == 'model_result'),
                'tool_ledger': tuple({'status': 'succeeded', 'artifact_id': str(a.id),
                    'artifact_sha256': a.content_sha256} for a in artifacts if a.type == 'tool_result'),
                'artifact_registry': {str(a.id): a.content_sha256 for a in artifacts}, **(state_overrides or {}), **state,
            }))

    def event(self, checkpoint: RuntimeCheckpoint, sequence: int) -> RunEvent:
        return RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=sequence,
                        run_id=self.run, checkpoint=checkpoint)

    async def seed(self, checkpoint: RuntimeCheckpoint, sequence: int) -> None:
        saved = self.event(checkpoint, sequence)
        assert isinstance(saved.kind, EventKind)
        async with self.factory.begin() as session:
            session.add(RunEventRow(id=uuid4(), tenant_id=self.tenant, run_id=self.run,
                sequence=sequence, kind=saved.kind.value, payload=saved.to_payload()))
            session.add(RunCheckpointRow(id=checkpoint.id, tenant_id=self.tenant, run_id=self.run,
                sequence=sequence, runtime_type='autogen', runtime_version='2', mode='discuss',
                state=checkpoint.to_payload()['state'], state_sha256=checkpoint.state_sha256,
                payload=checkpoint.to_payload()))

    async def private(self, artifact: Artifact, *, status: str = 'written', permanent: bool = False,
                      tenant: UUID | None = None, run: UUID | None = None) -> None:
        tenant, run = tenant or self.tenant, run or self.run
        payload = PostgresArtifactRepository._encode_payload(artifact.to_payload())
        async with self.factory.begin() as session:
            session.add(RuntimeArtifactRow(tenant_id=tenant, run_id=run, id=artifact.id,
                content_sha256=artifact.content_sha256, payload=payload,
                byte_size=len(payload.encode()), permanent=permanent))
            session.add(RuntimeArtifactWriteRow(tenant_id=tenant, run_id=run, write_id=uuid4(),
                artifact_id=artifact.id, content_sha256=artifact.content_sha256, status=status))

    async def counts(self) -> tuple[int, int]:
        async with self.factory() as session:
            events = await session.scalar(select(func.count()).select_from(RunEventRow))
            checkpoints = await session.scalar(select(func.count()).select_from(RunCheckpointRow))
            assert isinstance(events, int) and isinstance(checkpoints, int)
            return events, checkpoints


@pytest.fixture
async def db() -> AsyncIterator[Checkpoints]:
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    metadata = MetaData()
    for model in (RunRow, RunEventRow, RunCheckpointRow, RunOutboxRow,
                  RuntimeArtifactRow, RuntimeArtifactWriteRow):
        table = model_table(model)
        columns = [Column(c.name, JSON() if isinstance(c.type, JSONB)
            else String(32) if isinstance(c.type, PGUUID) else c.type,
            primary_key=c.primary_key, nullable=c.nullable, server_default=c.server_default)
            for c in table.columns]
        constraints = [UniqueConstraint(*(c.name for c in constraint.columns))
            for constraint in table.constraints if isinstance(constraint, UniqueConstraint)]
        Table(model.__tablename__, metadata, *columns, *constraints)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        fixture = Checkpoints(factory, RunRepository(factory))
        async with factory.begin() as session:
            session.add(RunRow(id=fixture.run, tenant_id=fixture.tenant, actor_id=fixture.actor,
                actor_role='operator', request='Repair within the original scope.', mode='discuss',
                status='running', routing_decision=fixture.route, version=1,
                worker_id='test-worker', worker_lease_token=uuid4()))
        yield fixture
    finally:
        await engine.dispose()


@pytest.fixture
def proof(monkeypatch: pytest.MonkeyPatch) -> ProofCalls:
    calls: ProofCalls = []

    def validate(context: TaskContext, previous: RuntimeCheckpoint | None, checkpoint: RuntimeCheckpoint, artifacts: Sequence[Artifact]) -> None:
        calls.append((context, previous, checkpoint, tuple(artifacts)))
        if (checkpoint.state.get(POLICY) != 1 or type(checkpoint.state.get(POLICY)) is not int
                or previous is not None and checkpoint.state[WATERMARK] == previous.state[WATERMARK]
                and number(checkpoint.state['remaining_timeout_seconds']) > number(previous.state['remaining_timeout_seconds'])):
            raise ValueError('no new diagnostic grant')

    monkeypatch.setattr(project_repair_phase, 'validate_diagnostic_transition', validate, raising=False)
    return calls


async def test_validator_rejection_rolls_back_event_and_checkpoint(db: Checkpoints, proof: ProofCalls) -> None:
    previous = db.checkpoint(soft=0, watermark=4)
    await db.seed(previous, 1)
    candidate = db.checkpoint(soft=30, watermark=4)
    with pytest.raises(RunConflict):
        async with db.factory.begin() as session:
            await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                                              event=db.event(candidate, 2))
    assert await db.counts() == (1, 1)
    assert present(proof[0][1]).id == previous.id


async def test_guard_uses_latest_db_checkpoint_and_locked_route_identity(db: Checkpoints, proof: ProofCalls) -> None:
    await db.seed(db.checkpoint(soft=0), 1)
    latest = db.checkpoint(soft=2, watermark=4)
    await db.seed(latest, 3)
    candidate = db.checkpoint(soft=1, watermark=4)
    async with db.factory.begin() as session:
        await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                                          event=db.event(candidate, 4))
    assert await db.counts() == (3, 3)
    context, predecessor, saved, artifacts = proof[0]
    assert present(predecessor).id == latest.id and saved.id == candidate.id
    assert context.actor_id == db.actor and context.actor_role is Role.OPERATOR
    assert context.request == 'Repair within the original scope.'
    assert context.routing_decision['workspace_session_id'] == 'session' and artifacts == ()


async def test_all_historical_and_pending_private_model_tool_refs_are_loaded(db: Checkpoints, proof: ProofCalls) -> None:
    historical = Artifact(id=uuid4(), type='model_result', producer='agent_hub', content={'text': 'old'})
    pending = Artifact(id=uuid4(), type='model_result', producer='agent_hub', content={'text': 'new'})
    read = Artifact(id=uuid4(), type='tool_result', producer='implementer', content={
        'tool_name': 'workspace.read', 'result': {'path': 'main.py', 'text': 'x', 'truncated': False}})
    for artifact in (historical, pending, read):
        await db.private(artifact)
    previous = db.checkpoint(artifacts=(historical,))
    await db.seed(previous, 1)
    candidate = db.checkpoint(artifacts=(historical, pending, read),
                              project_repair_phase={'model_floor': 1})
    async with db.factory.begin() as session:
        await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                                          event=db.event(candidate, 2))
    assert {a.id for a in proof[0][3]} == {historical.id, pending.id, read.id}


@pytest.mark.parametrize('defect', [
    'reserved', 'aborted', 'tenant', 'run', 'hash', 'registry', 'owner_hash', 'missing', 'payload',
])
async def test_unowned_foreign_or_hash_mismatched_private_ref_is_atomic_failure(
    db: Checkpoints, proof: ProofCalls, defect: str,
) -> None:
    artifact = Artifact(id=uuid4(), type='model_result', producer='agent_hub', content={'text': 'private'})
    await db.private(artifact, status=defect if defect in {'reserved', 'aborted'} else 'written',
                     tenant=uuid4() if defect == 'tenant' else None,
                     run=uuid4() if defect == 'run' else None)
    if defect in {'owner_hash', 'missing', 'payload'}:
        async with db.factory.begin() as session:
            if defect == 'owner_hash':
                owner = await session.scalar(select(RuntimeArtifactWriteRow))
                assert owner is not None
                owner.content_sha256 = 'f' * 64
            elif defect == 'missing':
                await session.execute(delete(RuntimeArtifactRow))
            else:
                stored = await session.scalar(select(RuntimeArtifactRow))
                assert stored is not None
                stored.payload = '{}'
    candidate = db.checkpoint(artifacts=(artifact,))
    if defect == 'hash':
        state = dict(candidate.state)
        state['model_ledger'] = ({'status': 'succeeded', 'artifact_id': str(artifact.id),
                                  'artifact_sha256': 'f' * 64},)
        state['artifact_registry'] = {str(artifact.id): 'f' * 64}
        candidate = db.checkpoint(state_overrides=state)
    elif defect == 'registry':
        candidate = db.checkpoint(artifacts=(artifact,), artifact_registry={})
    with pytest.raises(RunConflict):
        async with db.factory.begin() as session:
            await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                                              event=db.event(candidate, 1))
    assert await db.counts() == (0, 0) and proof == []


async def test_permanent_private_artifact_does_not_require_live_write_owner(db: Checkpoints, proof: ProofCalls) -> None:
    artifact = Artifact(id=uuid4(), type='model_result', producer='agent_hub', content={'text': 'private'})
    await db.private(artifact, status='aborted', permanent=True)
    async with db.factory.begin() as session:
        await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
            event=db.event(db.checkpoint(artifacts=(artifact,)), 1))
    assert proof[0][3][0].id == artifact.id and await db.counts() == (1, 1)


async def test_missing_checkpoint_flag_cannot_downgrade_trusted_route(db: Checkpoints, proof: ProofCalls) -> None:
    checkpoint = db.checkpoint()
    state = dict(checkpoint.state)
    state.pop(POLICY)
    candidate = RuntimeCheckpoint(id=checkpoint.id, runtime_type='autogen', runtime_version='2',
        tenant_id=db.tenant, run_id=db.run, mode=TaskMode.DISCUSS, state=state)
    with pytest.raises(RunConflict):
        async with db.factory.begin() as session:
            await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                event=db.event(candidate, 1), routing_decision=checkpoint_state(db.route))
    assert await db.counts() == (0, 0)


@pytest.mark.parametrize('version', [True, 1.0, 2, None])
async def test_checkpoint_policy_must_be_exact_integer_version(db: Checkpoints, proof: ProofCalls, version: object) -> None:
    with pytest.raises(RunConflict):
        async with db.factory.begin() as session:
            await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                event=db.event(db.checkpoint(state_overrides={POLICY: version}), 1))
    assert await db.counts() == (0, 0)


async def test_checkpoint_cannot_activate_policy_absent_from_locked_route(db: Checkpoints, proof: ProofCalls) -> None:
    async with db.factory.begin() as session:
        row = await session.get(RunRow, db.run)
        assert row is not None
        row.routing_decision = {}
    with pytest.raises(RunConflict):
        async with db.factory.begin() as session:
            await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                                              event=db.event(db.checkpoint(), 1))
    assert await db.counts() == (0, 0) and proof == []


@pytest.mark.parametrize('defect', ['tenant', 'run', 'mode', 'event_run'])
async def test_checkpoint_claim_scope_must_match_locked_run(db: Checkpoints, proof: ProofCalls, defect: str) -> None:
    candidate = db.checkpoint()
    candidate = RuntimeCheckpoint(id=candidate.id, runtime_type='autogen', runtime_version='2',
        tenant_id=uuid4() if defect == 'tenant' else db.tenant,
        run_id=uuid4() if defect == 'run' else db.run,
        mode=TaskMode.DIRECT if defect == 'mode' else TaskMode.DISCUSS, state=candidate.state)
    checkpoint_event = RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=1,
                               run_id=candidate.run_id, checkpoint=candidate)
    if defect == 'event_run':
        checkpoint_event = checkpoint_event.model_copy(update={'run_id': uuid4()})
    with pytest.raises(RunConflict):
        async with db.factory.begin() as session:
            await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                                              event=checkpoint_event)
    assert await db.counts() == (0, 0) and proof == []


async def test_conflicting_duplicate_sequence_is_not_acknowledged(db: Checkpoints, proof: ProofCalls) -> None:
    candidate = db.checkpoint()
    async with db.factory.begin() as session:
        await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                                          event=db.event(candidate, 1))
    with pytest.raises(RunConflict):
        async with db.factory.begin() as session:
            await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                event=db.event(db.checkpoint(soft=30, watermark=1), 1))
    assert await db.counts() == (1, 1)


async def test_identical_duplicate_is_idempotent_without_revalidating_old_grant(db: Checkpoints, proof: ProofCalls) -> None:
    candidate = db.event(db.checkpoint(), 1)
    for _ in range(2):
        async with db.factory.begin() as session:
            await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run, event=candidate)
    assert await db.counts() == (1, 1) and len(proof) == 1


async def test_identical_event_cannot_ack_a_corrupt_persisted_checkpoint(db: Checkpoints, proof: ProofCalls) -> None:
    candidate = db.event(db.checkpoint(), 1)
    async with db.factory.begin() as session:
        await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run, event=candidate)
    async with db.factory.begin() as session:
        row = await session.scalar(select(RunCheckpointRow))
        assert row is not None
        row.payload = {**row.payload, 'run_id': str(uuid4())}
    with pytest.raises(RunConflict):
        async with db.factory.begin() as session:
            await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run, event=candidate)
    assert await db.counts() == (1, 1) and len(proof) == 1


async def test_trusted_hint_cannot_override_locked_actor_or_scope(db: Checkpoints, proof: ProofCalls) -> None:
    hint = {**db.route, 'project_id': 'foreign', 'actor_id': str(uuid4())}
    async with db.factory.begin() as session:
        await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
            event=db.event(db.checkpoint(), 1), routing_decision=checkpoint_state(hint))
    assert proof[0][0].actor_id == db.actor
    assert proof[0][0].routing_decision['project_id'] == 'project'


async def test_legacy_checkpoint_keeps_insert_only_query_behavior(db: Checkpoints, proof: ProofCalls) -> None:
    candidate = db.checkpoint()
    state = dict(candidate.state)
    state.pop(POLICY)
    state.pop(WATERMARK)
    checkpoint = RuntimeCheckpoint(id=candidate.id, tenant_id=db.tenant, run_id=db.run,
        runtime_type='autogen', runtime_version='2', mode=TaskMode.DISCUSS, state=state)
    statements: list[str] = []
    bind = db.factory.kw['bind']
    assert isinstance(bind, AsyncEngine)
    engine = bind.sync_engine

    def observe(connection: Connection, cursor: object, statement: str, parameters: object, context: ExecutionContext, executemany: bool) -> None:
        statements.append(statement)

    event.listen(engine, 'before_cursor_execute', observe)
    try:
        async with db.factory.begin() as session:
            await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                                              event=db.event(checkpoint, 1))
    finally:
        event.remove(engine, 'before_cursor_execute', observe)
    assert not any(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    assert proof == [] and await db.counts() == (1, 1)


async def test_real_validator_accepts_initial_zero_watermark(db: Checkpoints) -> None:
    async with db.factory.begin() as session:
        await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
            event=db.event(db.checkpoint(), 1), routing_decision=checkpoint_state(db.route))
    assert await db.counts() == (1, 1)


async def test_first_checkpoint_can_retain_initial_timeout_above_soft_runway(db: Checkpoints) -> None:
    async with db.factory.begin() as session:
        await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
            event=db.event(db.checkpoint(soft=39), 1), routing_decision=checkpoint_state(db.route))
    assert await db.counts() == (1, 1)


@pytest.mark.parametrize('mutation', [
    {'remaining_timeout_seconds': 41.0},
    {'remaining_timeout_seconds': -1.0},
    {'remaining_absolute_timeout_seconds': 41.0},
    {'remaining_absolute_timeout_seconds': -1.0},
    {'usage': {'tokens': -1, 'cost_usd': '0.01'}},
    {'usage': {'tokens': 2_001, 'cost_usd': '0.01'}},
    {'usage': {'tokens': 10, 'cost_usd': '-0.01'}},
])
async def test_first_checkpoint_cannot_establish_invalid_budget_baseline(
    db: Checkpoints, mutation: dict[str, object],
) -> None:
    with pytest.raises(RunConflict):
        async with db.factory.begin() as session:
            await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                event=db.event(db.checkpoint(state_overrides=mutation), 1), routing_decision=checkpoint_state(db.route))
    assert await db.counts() == (0, 0)


async def test_real_validator_preserves_cold_remaining_without_new_grant(db: Checkpoints) -> None:
    await db.seed(db.checkpoint(soft=9), 1)
    candidate = db.event(db.checkpoint(soft=7, remaining_absolute_timeout_seconds=38.0), 2)
    for _ in range(2):
        async with db.factory.begin() as session:
            await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                event=candidate, routing_decision=checkpoint_state(db.route))
    async with db.factory() as session:
        saved = await db.repository.latest_checkpoint(session, tenant_id=db.tenant, run_id=db.run)
    assert saved is not None and saved.state['remaining_timeout_seconds'] == 7
    assert await db.counts() == (2, 2)


@pytest.mark.parametrize('mutation', [
    {'remaining_timeout_seconds': 30.0},
    {'remaining_timeout_seconds': 8.0, 'remaining_absolute_timeout_seconds': 38.0},
    {'remaining_absolute_timeout_seconds': 41.0},
    {'usage': {'tokens': 9, 'cost_usd': '0.01'}},
    {'usage': {'tokens': 10, 'cost_usd': '0.00'}},
    {WATERMARK: 1},
])
async def test_real_validator_rejects_regrant_and_budget_or_watermark_forgery(
    db: Checkpoints, mutation: dict[str, object],
) -> None:
    await db.seed(db.checkpoint(soft=9), 1)
    candidate = db.checkpoint(state_overrides=mutation)
    with pytest.raises(RunConflict):
        async with db.factory.begin() as session:
            await db.repository.persist_event(session, tenant_id=db.tenant, run_id=db.run,
                event=db.event(candidate, 2), routing_decision=checkpoint_state(db.route))
    assert await db.counts() == (1, 1)


@pytest.mark.parametrize('diagnostic', [False, True])
@pytest.mark.parametrize('floor', [1, 2])
async def test_continuation_hydrates_historical_models_only_for_diagnostic_policy(
    db: Checkpoints, monkeypatch: pytest.MonkeyPatch, diagnostic: bool, floor: int,
) -> None:
    models = tuple(Artifact(id=uuid4(), type='model_result', producer='agent_hub',
                            content={'text': text}) for text in ('historical', 'latest'))
    for artifact in models:
        await db.private(artifact)
    checkpoint = db.checkpoint(artifacts=models, project_repair_phase={'model_floor': floor})
    await db.seed(checkpoint, 1)
    async with db.factory.begin() as session:
        row = await session.get(RunRow, db.run)
        assert row is not None
        row.status = 'failed'
        RunRepository.clear_worker_lease(row)
        if not diagnostic:
            row.routing_decision = {k: v for k, v in db.route.items() if k != POLICY}
    observed: list[Artifact] = []

    def boundary(context: TaskContext, candidate: RuntimeCheckpoint, artifacts: Sequence[Artifact]) -> dict[str, int]:
        observed.extend(artifacts)
        return {'tool_count': 0}

    monkeypatch.setattr(project_repair_phase, 'productive_boundary', boundary)
    record = await db.repository.continue_project_repair_and_enqueue(
        tenant_id=db.tenant, run_id=db.run, version=1,
        checkpoint_sha256=checkpoint.state_sha256)
    expected = models if diagnostic else models[floor:]
    assert {a.id for a in observed} == {a.id for a in expected}
    assert record.status.value == 'queued'


@pytest.mark.parametrize('defect', ['reserved', 'foreign', 'corrupt'])
async def test_continuation_cannot_use_unavailable_historical_private_model(
    db: Checkpoints, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    model = Artifact(id=uuid4(), type='model_result', producer='agent_hub', content={'text': 'old'})
    await db.private(model, status='reserved' if defect == 'reserved' else 'written',
                     tenant=uuid4() if defect == 'foreign' else None)
    checkpoint = db.checkpoint(artifacts=(model,), project_repair_phase={'model_floor': 1})
    await db.seed(checkpoint, 1)
    async with db.factory.begin() as session:
        row = await session.get(RunRow, db.run)
        assert row is not None
        row.status = 'failed'
        RunRepository.clear_worker_lease(row)
        if defect == 'corrupt':
            stored = await session.scalar(select(RuntimeArtifactRow))
            assert stored is not None
            stored.payload = '{}'

    def boundary(context: TaskContext, candidate: RuntimeCheckpoint, artifacts: Sequence[Artifact]) -> dict[str, int]:
        if {a.id for a in artifacts} != {model.id}:
            raise ValueError('historical diagnostic proof is unavailable')
        return {'tool_count': 0}

    monkeypatch.setattr(project_repair_phase, 'productive_boundary', boundary)
    with pytest.raises(RunConflict):
        await db.repository.continue_project_repair_and_enqueue(
            tenant_id=db.tenant, run_id=db.run, version=1,
            checkpoint_sha256=checkpoint.state_sha256)
    async with db.factory() as session:
        row = await session.get(RunRow, db.run)
        assert row is not None
        assert row.status == 'failed' and row.version == 1
        assert await session.scalar(select(func.count()).select_from(RunOutboxRow)) == 0
