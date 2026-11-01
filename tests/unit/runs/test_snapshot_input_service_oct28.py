"""Real service input/codec boundary; synthetic rows/files, zero model/tool execution."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import subprocess
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.runs import service as service_module
from agent_hub.runs.conversation_inputs import STATE, VERSION, ConversationInputs
from agent_hub.runs.repository import ConversationContextItem, RunRecord, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.action_continuation import read_action_continuation
from agent_hub.runtime.action_direct import _binding, _tighten_budgets
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.continuation_artifacts import _encode, load_continuation, save_continuation
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.direct import RuntimeExecutionError
from agent_hub.runtime.instruction_context import InstructionContextLoader
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.conversation_authenticated_input_typed_fixture import (
    LockSession,
    UnexpectedQueue,
    mutable,
    present,
)
from tests.unit.runs.delivery_context_revision_typed_fixture import DeliveryRecord
from tests.unit.runs.project_auto_repair_typed_fixture import RepairRepository
from tests.unit.runs.snapshot_approval_types_fixture import number, record
from tests.unit.runs.test_conversation_inputs_oct28 import MemoryRuns
from tests.unit.runs.test_delivery_context_oct28 import Repository as DeliveryRepository
from tests.unit.runs.test_delivery_context_oct28 import fixture as delivery_fixture
from tests.unit.runs.test_delivery_context_oct28 import view

OLD = '8c79e1e987cc3a1f743130c2403ef61bf4967717'
CHANGED = b'# Updated usage\npython -m demo --format ndjson\n'


class HistoryRuns(RepairRepository):
    """Reuse Boole row locks and existing execute transport; no SQL/network."""
    def __init__(self, delivery: DeliveryRepository, current: DeliveryRecord | RunRecord, source: DeliveryRecord) -> None:
        super().__init__(routing_decision={})
        memory = MemoryRuns()
        self.row, self.lock = memory.row, memory.lock
        self.row.id, self.row.tenant_id, self.row.actor_id = current.id, current.tenant_id, current.actor_id
        self.row.mode, self.row.request, self.row.created_at = 'direct', current.request, current.created_at
        self.row.routing_decision = {**present(current.routing_decision), VERSION: 1, 'permission_mode': 'read_only'}
        self.run_id, self.delivery, self.source = current.id, delivery, source
        self.history_reads = 0
        self.raw_reads: list[UUID] = []

    async def run_transaction(self) -> AsyncSession:
        return LockSession(self.lock)

    async def get(self, tenant_id: UUID, run_id: UUID, *, actor_id: UUID | None = None) -> RunRecord:
        assert tenant_id == self.row.tenant_id
        return RunRepository._record(self.row) if run_id == self.row.id else record(await self.delivery.get(tenant_id, run_id))

    async def raw_events(self, tenant_id: UUID, run_id: UUID) -> tuple[RunEvent, ...]:
        self.raw_reads.append(run_id)
        assert tenant_id == self.row.tenant_id
        return tuple(self.event_log) if run_id == self.row.id else await self.delivery.raw_events(tenant_id, run_id)

    async def conversation_context(self, tenant_id: UUID, conversation_id: str, *,
        before_run_id: UUID, limit: int | None = 32, query: str | None = None,
        candidate_limit: int | None = None,
    ) -> tuple[ConversationContextItem, ...]:
        assert tenant_id == self.row.tenant_id and conversation_id == mutable(self.row.routing_decision)['conversation_id']
        assert before_run_id == self.row.id
        self.history_reads += 1
        return (ConversationContextItem(self.source.id, self.source.request,
            ({'type': 'text', 'producer': 'main', 'content': {'text': 'Prior report.'}},)),)


def service(runs: HistoryRuns, artifacts: InMemoryArtifactRepository, root: Path) -> RunService:
    return RunService(runs, runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None, task_queue=UnexpectedQueue(), worker_id=runs.row.worker_id,
        conversation_artifact_repository=artifacts,
        instruction_context_loader=InstructionContextLoader(repository=runs, project_root=root))


@dataclass
class Case:
    runs: HistoryRuns
    artifacts: InMemoryArtifactRepository
    service: RunService
    root: Path
    readme: Path


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Case:
    delivery, current, source, _ = delivery_fixture(tmp_path)
    runs = HistoryRuns(delivery, current, source)
    # Only the inherited in-memory execute transport uses this fixed fixture constant.
    monkeypatch.setattr('tests.unit.runs.test_terminal_hooks.TENANT_ID', current.tenant_id)
    monkeypatch.setattr('tests.unit.runs.project_auto_repair_typed_fixture.TENANT_ID', current.tenant_id)
    artifacts = InMemoryArtifactRepository()
    return Case(runs, artifacts, service(runs, artifacts, tmp_path), tmp_path,
        tmp_path / str(current.tenant_id) / 'projects/demo/sessions/session-a/README.md')


async def inputs(case: Case, checkpoint: RuntimeCheckpoint | None = None) -> ConversationInputs:
    row = case.runs.row
    return await case.service._runtime_context_inputs(tenant_id=row.tenant_id, run_id=row.id,
        actor_id=row.actor_id, actor_role=Role.OPERATOR, mode=TaskMode.DIRECT,
        execution_token=row.worker_lease_token, current_request=row.request,
        routing_decision=dict(mutable(row.routing_decision)), runtime_token_budget=1_000_000, checkpoint=checkpoint)


def context(case: Case, prepared: ConversationInputs, checkpoint: RuntimeCheckpoint | None = None) -> TaskContext:
    row = case.runs.row
    route = dict(mutable(row.routing_decision))
    if prepared.snapshot is not None:
        route[STATE] = prepared.snapshot
    return TaskContext(tenant_id=row.tenant_id, run_id=row.id, actor_id=row.actor_id,
        actor_role=Role.OPERATOR, mode=TaskMode.DIRECT, request=row.request,
        execution_token=row.worker_lease_token, artifacts=prepared.artifacts, checkpoint=checkpoint,
        routing_decision=cast(Mapping[str, JsonValue], route))


def historical(items: tuple[Artifact, ...]) -> Artifact:
    return next(a for a in items if a.producer == 'historical_delivery_evidence')


async def action_checkpoint(case: Case, original: TaskContext, raw_source: str = 'request',
                            inspection: Artifact | None = None) -> RuntimeCheckpoint:
    pending = HarnessToolCallRequest(run_id=original.run_id, actor='main_agent', tool_name='workspace.read',
        arguments={'path': 'README.md'}, approval_required=False, sandbox='read_only', idempotency_key='read-next')
    raw = pending.to_payload() if raw_source == 'request' else {
        'name': pending.tool_name, 'arguments': pending.arguments, 'key': pending.idempotency_key}
    snapshot = original.model_copy(update={'execution_token': None, 'checkpoint': None})
    reference = await save_continuation(case.artifacts, tenant_id=original.tenant_id, run_id=original.run_id,
        value=cast(JsonValue, {'context': snapshot.to_payload(), 'models': [], 'actions': [],
            'pending': {'raw_sha256': hashlib.sha256(_encode(raw)).hexdigest(), 'request': pending.to_payload()},
            'emitted': 0, 'elapsed': 0.0, 'inspection': None if inspection is None else inspection.to_payload(),
            'preparation': {}}))
    return RuntimeCheckpoint(id=uuid4(), tenant_id=original.tenant_id, run_id=original.run_id,
        mode=TaskMode.DIRECT, runtime_type='direct_action', runtime_version='2',
        state={'phase': 'concrete_action', 'completed': False, 'approval_id': 'approval_snapshot1',
            'tool_name': pending.tool_name, 'context_sha256': _binding(original, 'component-fixture'),
            'reference': reference, 'elapsed_seconds': 0.0, 'timeout_seconds': original.timeout_seconds})


def publish_checkpoint(case: Case, checkpoint: RuntimeCheckpoint, *observations: Artifact) -> None:
    case.runs.event_log = [RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=i, run_id=case.runs.row.id,
        actor='main_agent', artifact=a) for i, a in enumerate(observations, 1)]
    case.runs.event_log.append(RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=len(observations) + 1,
        run_id=case.runs.row.id, checkpoint=checkpoint))


@pytest.mark.parametrize('raw_source', ['request', 'action'])
async def test_same_run_keeps_first_readme_history_and_real_action_sources(case: Case, raw_source: str) -> None:
    first = await inputs(case)
    original = context(case, first)
    before = historical(first.artifacts)
    assert mutable(view((before,))['readme'])['status'] == 'same_as_delivery'
    assert _encode(original.routing_decision[STATE]) == _encode(first.snapshot) == _encode(mutable(case.runs.row.routing_decision)[STATE])
    checkpoint = await action_checkpoint(case, original, raw_source)
    case.readme.write_bytes(CHANGED)
    fresh = await case.service._conversation_artifacts(tenant_id=original.tenant_id, run_id=original.run_id,
        current_request=original.request, routing_decision=original.routing_decision, runtime_token_budget=1_000_000)
    assert historical(fresh).id != before.id and historical(fresh).content != before.content
    assert mutable(view((historical(fresh),))['readme'])['status'] == 'changed'
    reads = case.runs.history_reads
    publish_checkpoint(case, checkpoint)
    case.runs.row.worker_id, case.runs.row.worker_lease_token = 'worker-b', uuid4()
    case.service = service(case.runs, case.artifacts, case.root)
    restored = await inputs(case, checkpoint)
    resumed = context(case, restored, checkpoint)
    assert resumed.routing_decision[STATE] == original.routing_decision[STATE]
    assert _binding(original, 'component-fixture') == _binding(resumed, 'component-fixture')
    assert [a.to_payload() for a in restored.artifacts] == [a.to_payload() for a in first.artifacts]
    assert case.runs.history_reads == reads
    decoded = await read_action_continuation(case.artifacts, resumed)
    assert decoded.snapshot.artifacts == original.artifacts and decoded.value['preparation'] == {}


def inspection(case: Case, suffix: str) -> Artifact:
    row = case.runs.row
    return Artifact(id=uuid4(), type='tool_result', producer='main_agent', content={
        'agent_id': 'main_agent', 'tool_name': 'project.inspect', 'call_id': 'inspect_' + suffix,
        'arguments_sha256': hashlib.sha256(b'{}').hexdigest(), 'result': {'scope': {
            'tenant_id': str(row.tenant_id), 'run_id': str(row.id), 'project_id': 'demo',
            'workspace_session_id': 'session-a'}, 'summary': suffix}})


async def test_own_inspections_are_live_validated_and_merged_outside_history_snapshot(case: Case) -> None:
    first = await inputs(case)
    one, two, unpublished = (inspection(case, label) for label in ('one', 'two', 'suffix'))
    checkpoint = await action_checkpoint(case, context(case, first), inspection=one)
    publish_checkpoint(case, checkpoint, one, two)
    case.runs.event_log.append(RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=4,
        run_id=case.runs.row.id, actor='main_agent', artifact=unpublished))
    case.readme.write_bytes(CHANGED)
    restored = await inputs(case, checkpoint)
    assert restored.artifacts == (*first.artifacts, one, two)
    assert restored.snapshot == first.snapshot and case.runs.history_reads == 1
    assert case.runs.row.id in case.runs.raw_reads
    private = await load_continuation(case.artifacts, tenant_id=case.runs.row.tenant_id,
        run_id=case.runs.row.id, reference=cast(Any, first.snapshot)['reference'])
    saved = json.loads(_encode(private))['artifacts']
    assert all(a['producer'] != 'main_agent' for a in saved)
    decoded = await read_action_continuation(case.artifacts, context(case, restored, checkpoint))
    assert decoded.value['inspection'] is not None and decoded.snapshot.artifacts == first.artifacts


async def test_frozen_history_does_not_bypass_current_inspection_event_scope_checks(case: Case) -> None:
    first = await inputs(case)
    checkpoint = await action_checkpoint(case, context(case, first))
    one = inspection(case, 'foreign')
    payload = json.loads(_encode(one.to_payload()))
    payload['content']['result']['scope']['run_id'] = str(uuid4())
    payload['content_sha256'] = ''
    publish_checkpoint(case, checkpoint, Artifact.from_payload(payload))
    with pytest.raises(ValueError, match='integrity or scope'):
        await inputs(case, checkpoint)
    assert case.runs.history_reads == 1


async def test_legacy_unmarked_wrapper_reads_live_history_each_time(case: Case) -> None:
    mutable(case.runs.row.routing_decision).pop(VERSION)
    first = await inputs(case)
    assert first.snapshot is None and STATE not in mutable(case.runs.row.routing_decision)
    case.readme.write_bytes(CHANGED)
    row = case.runs.row
    fresh = await case.service._runtime_context_artifacts(tenant_id=row.tenant_id, run_id=row.id,
        current_request=row.request, routing_decision=mutable(row.routing_decision), runtime_token_budget=1_000_000,
        checkpoint=None)
    assert historical(fresh).id != historical(first.artifacts).id and case.runs.history_reads == 2


async def test_new_run_captures_current_readme_instead_of_previous_run_snapshot(case: Case) -> None:
    first = await inputs(case)
    case.readme.write_bytes(CHANGED)
    next_row = await case.runs.get(case.runs.row.tenant_id, case.runs.row.id)
    runs = HistoryRuns(case.runs.delivery, next_row, case.runs.source)
    runs.row.id = runs.run_id = uuid4()
    mutable(runs.row.routing_decision).pop(STATE, None)
    next_case = Case(runs, case.artifacts, service(runs, case.artifacts, case.root), case.root, case.readme)
    fresh = await inputs(next_case)
    assert fresh.snapshot != first.snapshot and runs.history_reads == 1
    assert mutable(view((historical(fresh.artifacts),))['readme'])['status'] == 'changed'


class ContextProbe:
    mode = TaskMode.DIRECT
    seen: TaskContext | None = None
    async def run(self, ctx: TaskContext) -> AsyncIterator[RunEvent]:
        self.seen = ctx
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=ctx.run_id)
    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError('fresh execute must not restore')
    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError('no runtime checkpoint')
    async def cancel(self) -> None:
        pass


async def test_actual_execute_first_task_context_already_contains_persisted_snapshot(case: Case) -> None:
    probe = ContextProbe()
    case.service._runtime_registry = RuntimeRegistry((probe,))
    result = await case.service.execute(case.runs.row.id)
    assert result.status is RunStatus.COMPLETED
    assert probe.seen is not None and STATE in probe.seen.routing_decision
    assert _encode(probe.seen.routing_decision[STATE]) == _encode(mutable(case.runs.row.routing_decision)[STATE])
    assert historical(probe.seen.artifacts).producer == 'historical_delivery_evidence'


async def test_snapshot_restore_time_is_charged_after_saved_total_cap(
    case: Case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutable(case.runs.row.routing_decision)['runtime_timeout_seconds'] = 3600
    first = await inputs(case)
    original = context(case, first).model_copy(update={'timeout_seconds': 3500.0})
    saved = await action_checkpoint(case, original)
    saved = saved.model_copy(update={'state': {**saved.state, 'elapsed_seconds': 1000.0}})
    saved = RuntimeCheckpoint.from_payload({**saved.to_payload(),
        'state_sha256': saved.recompute_state_sha256()})
    publish_checkpoint(case, saved)
    claim = case.runs.claim_for_execution

    async def restore_claim(*args: Any, **kwargs: Any) -> Any:
        claimed = await claim(*args, **kwargs)
        assert not isinstance(claimed, RunRecord)
        row, _ = claimed
        return row, saved

    class RestoreProbe(ContextProbe):
        async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
            assert checkpoint == saved

    probe = RestoreProbe()
    case.service._runtime_registry = RuntimeRegistry((probe,))
    monkeypatch.setattr(case.runs, 'claim_for_execution', restore_claim)
    clock = [100.0]
    fake_loop = SimpleNamespace(time=lambda: clock[0])
    monkeypatch.setattr(service_module, 'asyncio', SimpleNamespace(
        **{k: v for k, v in vars(asyncio).items() if k != 'get_running_loop'},
        get_running_loop=lambda: fake_loop))
    read_inputs = case.service._runtime_context_inputs

    async def timed_inputs(**kwargs: Any) -> ConversationInputs:
        result = await read_inputs(**kwargs)
        clock[0] += 10.0
        return result

    monkeypatch.setattr(case.service, '_runtime_context_inputs', timed_inputs)
    result = await case.service.execute(case.runs.row.id)
    assert result.status is RunStatus.COMPLETED and probe.seen is not None
    assert probe.seen.timeout_seconds == 3490.0
    narrowed = _tighten_budgets(original, probe.seen)
    assert narrowed.timeout_seconds - number(saved.state['elapsed_seconds']) == 2490.0
    assert probe.seen.checkpoint == saved and original.timeout_seconds == 3500.0
    assert probe.seen.token_budget == 1_000_000
    assert _encode(probe.seen.routing_decision[STATE]) == _encode(first.snapshot)
    assert probe.seen.routing_decision['requested_permissions'] == original.routing_decision['requested_permissions']


@pytest.fixture
def old_inputs() -> Any:
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(['git', '--no-optional-locks', 'cat-file', 'blob',
        OLD + ':src/agent_hub/runs/service.py'], cwd=root, capture_output=True, text=True,
        encoding='utf-8', check=True, timeout=20)  # Fixed local revision, read-only.
    old_class = next(n for n in ast.parse(result.stdout).body if isinstance(n, ast.ClassDef) and n.name == 'RunService')
    method = next(n for n in old_class.body if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == '_runtime_context_artifacts')
    module = ModuleType('_snapshot_input_service_8c_component')
    module.__dict__.update(vars(service_module))
    exec(  # noqa: S102 - exact committed method, isolated in-memory namespace.
        compile(ast.Module(body=[method], type_ignores=[]), '<8c input method>', 'exec'), module.__dict__)
    return module.__dict__['_runtime_context_artifacts']


async def test_8c_negative_control_readme_drift_rejects_real_continuation_sources(case: Case, old_inputs: Any) -> None:
    row = case.runs.row
    kwargs = {'tenant_id': row.tenant_id, 'run_id': row.id, 'current_request': row.request,
        'routing_decision': dict(mutable(row.routing_decision)), 'runtime_token_budget': 1_000_000, 'checkpoint': None}
    first = await old_inputs(case.service, **kwargs)
    original = context(case, ConversationInputs(first, None))
    checkpoint = await action_checkpoint(case, original)
    publish_checkpoint(case, checkpoint)
    case.readme.write_bytes(CHANGED)
    fresh = await old_inputs(case.service, **{**kwargs, 'checkpoint': checkpoint})
    assert historical(fresh).id != historical(first).id
    with pytest.raises(RuntimeExecutionError, match='sources changed'):
        await read_action_continuation(case.artifacts, original.model_copy(update={
            'checkpoint': checkpoint, 'artifacts': fresh}))
