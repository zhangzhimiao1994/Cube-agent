"""Source-free terminal recovery must retain original-project delivery binding."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.capabilities.project_verification import _validate_report
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runtime import direct
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.hybrid import HybridRuntime
from tests.unit.capabilities.project_original_fixture import failed_observation
from tests.unit.runtime.test_direct_original_project import batch, fixture
from tests.unit.runtime.test_direct_project_verification import Model
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime, RecordingHarnessToolGateway

SOURCE, PROTECTED, ENVELOPE, CANDIDATE = (letter * 64 for letter in 'abcd')


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, default=dict, sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()


def data(value: object) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], json.loads(json.dumps(value, default=dict)))


def original_route() -> dict[str, JsonValue]:
    observation = failed_observation(SOURCE, PROTECTED)
    return {
        'project_input_contract_version': 1,
        'project_input': {'schema_version': 1, 'kind': 'managed_workspace', 'editable_paths': ('src',)},
        'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle',
        'project_input_preparation': {
            'schema_version': 1, 'phase': 'observed',
            'reference': {'schema_version': 1, 'contract_sha256': ENVELOPE,
                          'input_source_sha256': SOURCE, 'file_count': 3},
            'observation': observation, 'observation_sha256': digest(observation),
        },
    }


def delivery() -> dict[str, JsonValue]:
    receipt = _validate_report(json.dumps({
        'schema_version': 1, 'status': 'passed', 'source_sha256': CANDIDATE,
        'runtime': {'node': 'v22.12.0', 'npm': '10.9.0'},
        'checks': [{'phase': phase, 'status': 'passed', 'exit_code': 0, 'timed_out': False,
                    'stdout': '', 'stderr': ''} for phase in ('install', 'build', 'test')],
    }), expected_source=CANDIDATE)
    file_id = str(uuid4())
    return {
        'artifact_id': file_id, 'presentation': 'final_attachment',
        'file': {'artifact_id': file_id, 'filename': 'project.zip', 'mime_type': 'application/zip',
                 'download_url': '/fixture/project.zip', 'sha256': 'e' * 64, 'size_bytes': 100},
        'verification': receipt, 'verification_source_sha256': CANDIDATE,
        'verification_receipt_sha256': digest(receipt),
        'original_check_contract': {
            'schema_version': 1, 'contract_sha256': PROTECTED, 'protected_sha256': PROTECTED,
            'baseline_envelope_sha256': ENVELOPE, 'baseline_source_sha256': SOURCE,
            'baseline_observation_sha256': digest(failed_observation(SOURCE, PROTECTED)),
            'candidate_source_sha256': CANDIDATE,
        },
    }


@dataclass
class Replay:
    runtime: direct.DirectRuntime | HybridRuntime
    context: TaskContext
    checkpoint: RuntimeCheckpoint
    model: Model
    children: tuple[MultiArtifactRuntime, ...]
    gateway: RecordingHarnessToolGateway

    async def events(self) -> list[RunEvent]:
        await self.runtime.restore_checkpoint(self.checkpoint)
        events: list[RunEvent] = []
        try:
            async for event in self.runtime.run(self.context):
                events.append(event)
        except direct.RuntimeExecutionError:
            pass
        return events

    def assert_no_execution(self) -> None:
        assert self.model.requests == []
        assert all(child.contexts == [] for child in self.children)
        assert self.gateway.calls == []


async def replay(mode: str, mutation: str = '', *, legacy: bool = False,
                 bundle_tool: bool = False) -> Replay:
    route, result = original_route(), delivery()
    proof = data(result['original_check_contract'])
    state = data(route['project_input_preparation'])
    if mutation == 'missing_proof':
        result.pop('original_check_contract')
    elif mutation == 'old_proof':
        result['original_check_contract'] = {key: proof[key] for key in
            ('schema_version', 'contract_sha256', 'protected_sha256')}
    elif mutation == 'candidate_mismatch':
        proof['candidate_source_sha256'] = 'f' * 64
    elif mutation == 'baseline_mismatch':
        proof['baseline_envelope_sha256'] = 'f' * 64
    elif mutation == 'unknown_proof_key':
        proof['permission'] = True
    elif mutation == 'boolean_schema':
        proof['schema_version'] = True
    elif mutation == 'missing_preparation':
        route.pop('project_input_preparation')
    elif mutation == 'missing_observation':
        state.pop('observation')
    elif mutation == 'missing_reference':
        state.pop('reference')
    elif mutation == 'receipt_mismatch':
        result['verification_receipt_sha256'] = 'f' * 64
    elif mutation == 'failed_receipt':
        receipt = data(result['verification'])
        receipt['status'] = 'failed'
        result['verification'] = receipt
        result['verification_receipt_sha256'] = digest(receipt)
    if mutation not in {'missing_proof', 'old_proof'}:
        result['original_check_contract'] = proof
    if mutation != 'missing_preparation':
        route['project_input_preparation'] = state
    if legacy:
        route = {'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle'}
    content: Mapping[str, JsonValue] = (
        {'tool_name': 'workspace.bundle', 'result': result} if bundle_tool
        else {'workspace_delivery': result})
    if mutation == 'no_bundle':
        content = {'text': 'All fixed.'}
    artifact = Artifact(id=uuid4(), type='tool_result', producer='worker' if bundle_tool else 'main',
                        content=content)
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(),
        mode=TaskMode.DIRECT if mode == 'direct' else TaskMode.HYBRID, request='Fix it.',
        token_budget=1000, timeout_seconds=60, routing_decision=route,
        artifacts=() if mutation == 'missing_artifact' else (artifact,))
    model = Model()
    gateway = RecordingHarnessToolGateway()
    children = tuple(MultiArtifactRuntime(task_mode, ()) for task_mode in
                     (TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.DIRECT))
    if mode == 'direct':
        runtime: direct.DirectRuntime | HybridRuntime = direct.DirectRuntime(model, logical_model='main',
            harness_tool_gateway=cast(HarnessToolGateway, gateway))
        checkpoint = RuntimeCheckpoint(id=uuid4(), runtime_type='direct', runtime_version='1',
            run_id=context.run_id, tenant_id=context.tenant_id, mode=context.mode,
            state={'completed': True, 'artifact_id': str(artifact.id),
                   'artifact_sha256': 'f' * 64 if mutation == 'artifact_hash' else artifact.content_sha256,
                   'next_sequence': 4})
    else:
        repository = InMemoryArtifactRepository()
        if mutation != 'missing_artifact':
            await repository.put(context.tenant_id, context.run_id, artifact)
        runtime = HybridRuntime(*children, artifact_repository=repository)
        checkpoint = runtime._checkpoint(context, artifacts=(artifact,), next_sequence=4,
            next_stage=2, terminal=True, reason='explicit_completion', remaining_tokens=1000,
            token_budget_grant=1000, token_budget_spent=0, remaining_timeout_seconds=60,
            remaining_absolute_timeout_seconds=60, timeout_progress_units=0,
            last_child_progress_fingerprint=None, child_checkpoint=None)
        if mutation == 'artifact_hash':
            changed = data(checkpoint.state)
            changed['artifact_registry'] = {str(artifact.id): 'f' * 64}
            checkpoint = RuntimeCheckpoint(id=checkpoint.id, runtime_type=checkpoint.runtime_type,
                runtime_version=checkpoint.runtime_version, run_id=context.run_id,
                tenant_id=context.tenant_id, mode=context.mode, state=changed)
    context = context.model_copy(update={'checkpoint': checkpoint})
    assert checkpoint.state_sha256 == checkpoint.recompute_state_sha256()
    assert artifact.content_sha256 == artifact.recompute_content_sha256()
    return Replay(runtime, context, checkpoint, model, children, gateway)


@pytest.mark.parametrize('mode', ['direct', 'hybrid'])
@pytest.mark.parametrize('mutation', [
    'missing_proof', 'old_proof', 'candidate_mismatch', 'baseline_mismatch', 'unknown_proof_key',
    'boolean_schema', 'missing_preparation', 'missing_observation', 'missing_reference',
    'no_bundle', 'missing_artifact', 'artifact_hash',
])
async def test_original_terminal_replay_rejects_unbound_delivery_before_execution(mode: str, mutation: str) -> None:
    configured = await replay(mode, mutation)
    events = await configured.events()
    configured.assert_no_execution()
    assert not any(event.kind in {EventKind.RUNTIME_COMPLETED, EventKind.CHECKPOINT_SAVED,
                                 EventKind.ARTIFACT_CREATED} for event in events)


@pytest.mark.parametrize('mode', ['direct', 'hybrid'])
async def test_original_terminal_replay_accepts_rich_delivery_without_rewriting_checkpoint(mode: str) -> None:
    configured = await replay(mode)
    before = configured.checkpoint.to_payload()
    events = await configured.events()
    configured.assert_no_execution()
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert configured.checkpoint.to_payload() == before


@pytest.mark.parametrize('mutation', ['receipt_mismatch', 'failed_receipt'])
async def test_direct_terminal_replay_requires_matching_successful_receipt(mutation: str) -> None:
    configured = await replay('direct', mutation)
    events = await configured.events()
    configured.assert_no_execution()
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


async def test_hybrid_terminal_replay_accepts_rich_bundle_tool_artifact() -> None:
    configured = await replay('hybrid', bundle_tool=True)
    events = await configured.events()
    configured.assert_no_execution()
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.parametrize('mutation', ['missing_proof', 'old_proof', 'missing_artifact', 'artifact_hash'])
async def test_hybrid_invalid_recovery_cannot_complete_partial_from_context_attachment(mutation: str) -> None:
    configured = await replay('hybrid', mutation, bundle_tool=True)
    events = await configured.events()
    configured.assert_no_execution()
    assert not any(event.kind in {EventKind.RUNTIME_COMPLETED, EventKind.CHECKPOINT_SAVED,
                                 EventKind.ARTIFACT_CREATED} for event in events)


@pytest.mark.parametrize('prior_sorts_first', [False, True])
async def test_hybrid_terminal_replay_does_not_mix_bound_and_unbound_bundles(prior_sorts_first: bool) -> None:
    configured = await replay('hybrid', 'old_proof', bundle_tool=True)
    assert isinstance(configured.runtime, HybridRuntime)
    prior = Artifact(id=UUID(int=1 if prior_sorts_first else 2**128 - 1), type='tool_result', producer='worker',
        content={'tool_name': 'workspace.bundle', 'result': delivery()})
    await configured.runtime._repository.put(configured.context.tenant_id, configured.context.run_id, prior)
    state = data(configured.checkpoint.state)
    state['artifact_registry'] = {str(prior.id): prior.content_sha256,
                                  **data(state['artifact_registry'])}
    configured.checkpoint = RuntimeCheckpoint(id=uuid4(), runtime_type='hybrid', runtime_version='3',
        run_id=configured.context.run_id, tenant_id=configured.context.tenant_id, mode=TaskMode.HYBRID, state=state)
    configured.context = configured.context.model_copy(update={'checkpoint': configured.checkpoint})
    events = await configured.events()
    configured.assert_no_execution()
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


def test_terminal_artifact_validator_never_inherits_shared_helpers_context_none_bypass() -> None:
    artifact = Artifact(id=uuid4(), type='tool_result', producer='main',
                        content={'workspace_delivery': delivery()})
    assert not direct._original_project_terminal_artifact_matches(None, artifact)


@pytest.mark.parametrize('mode', ['direct', 'hybrid'])
async def test_real_produced_delivery_replays_without_new_provider_or_workspace_calls(tmp_path: Path, mode: str) -> None:
    configured = await fixture(tmp_path)
    live, _, _ = configured.runtime(Model(batch(files={'src/index.js': 'module.exports = () => 12;\n'},
                                               complete=True)))
    produced = [event async for event in live.run(configured.context)]
    assert produced[-1].kind is EventKind.RUNTIME_COMPLETED
    output = next(event.artifact for event in produced if event.kind is EventKind.ARTIFACT_CREATED)
    assert output is not None and 'workspace_delivery' in output.content
    calls = list(configured.backend.calls)
    model = Model()
    children = tuple(MultiArtifactRuntime(task_mode, ()) for task_mode in
                     (TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.DIRECT))
    if mode == 'direct':
        runtime: direct.DirectRuntime | HybridRuntime
        runtime, _, _ = configured.runtime(model)
        checkpoint = await live.save_checkpoint()
        context = configured.context.model_copy(update={'artifacts': (output,), 'checkpoint': checkpoint})
    else:
        context = configured.context.model_copy(update={'mode': TaskMode.HYBRID, 'artifacts': (output,)})
        repository = InMemoryArtifactRepository()
        await repository.put(context.tenant_id, context.run_id, output)
        runtime = HybridRuntime(*children, artifact_repository=repository)
        checkpoint = runtime._checkpoint(context, artifacts=(output,), next_sequence=4,
            next_stage=2, terminal=True, reason='explicit_completion', remaining_tokens=1000,
            token_budget_grant=1000, token_budget_spent=0, remaining_timeout_seconds=60,
            remaining_absolute_timeout_seconds=60, timeout_progress_units=0,
            last_child_progress_fingerprint=None, child_checkpoint=None)
        context = context.model_copy(update={'checkpoint': checkpoint})
    before = checkpoint.to_payload()
    await runtime.restore_checkpoint(checkpoint)
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert configured.backend.calls == calls
    assert model.requests == [] and all(child.contexts == [] for child in children)
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize('mode', ['direct', 'hybrid'])
async def test_legacy_terminal_checkpoint_keeps_existing_replay_behavior_and_hash(mode: str) -> None:
    configured = await replay(mode, 'old_proof', legacy=True)
    before = configured.checkpoint.to_payload()
    events = await configured.events()
    configured.assert_no_execution()
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert configured.checkpoint.to_payload() == before


@pytest.mark.parametrize('mode', ['direct', 'hybrid'])
async def test_original_terminal_replay_rejects_absent_task_context(mode: str) -> None:
    configured = await replay(mode)
    await configured.runtime.restore_checkpoint(configured.checkpoint)
    with pytest.raises((direct.RuntimeExecutionError, RuntimeError), match='context'):
        _ = [event async for event in configured.runtime.run(cast(TaskContext, None))]
    configured.assert_no_execution()
