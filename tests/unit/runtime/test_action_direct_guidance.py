"""Stable guidance is persisted identity, never a grant of tool authority."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.runtime.action_direct import _binding
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, TaskContext
from agent_hub.runtime.direct import RuntimeExecutionError
from agent_hub.runtime.project_preflight_context import project_preflight_context_text
from agent_hub.runtime.self_repair_context import self_repair_context_text
from tests.unit.runtime.test_action_direct import Backend, Policy
from tests.unit.runtime.test_action_direct_recovery_regressions import pause_at_write, runtime
from tests.unit.runtime.test_direct_project_verification import Model, batch, context

Kind = Literal['preflight', 'repair']
KINDS: tuple[Kind, ...] = ('preflight', 'repair')
make_backend = cast(Callable[[Path, TaskContext, InMemoryArtifactRepository], Backend], Backend)


def guidance(kind: Kind) -> dict[str, Any]:
    if kind == 'preflight':
        return {'project_preflight_approved': True, 'project_preflight_proposal': {
            'schema_version': 1, 'kind': 'project_architecture_preflight',
            'capability': 'project.preflight_architecture',
            'plan_path': 'PROJECT_ARCHITECTURE_PLAN.md', 'graph_path': 'architecture-map.html',
            'requires_constraints_and_skills_reading': True, 'summary': 'Keep staged evidence.',
        }}
    return {'source': 'self_repair', 'self_repair_accepted': True, 'self_repair_context': {
        'schema_version': 1, 'source': 'self_repair', 'source_run_id': 'original-run',
        'source_event_sequence': 7, 'failure_kind': 'runtime_failure',
        'repair_action': 'draft_repair_proposal', 'attempt': 1, 'max_attempts': 1,
        'instruction': 'Run one bounded repair attempt, then stop.',
        'requires_approval': True, 'automatic_execution': False,
    }}


def scoped(kind: Kind) -> TaskContext:
    ctx = context()
    return ctx.model_copy(update={'routing_decision': {**ctx.routing_decision, **guidance(kind)}})


@pytest.mark.parametrize('kind', KINDS)
async def test_unchanged_guidance_survives_cold_approval_recovery(
    tmp_path: Path, kind: Kind,
) -> None:
    ctx, policy, repo = scoped(kind), Policy(), InMemoryArtifactRepository()
    before = make_backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Stable approved guidance'}))
    checkpoint = await pause_at_write(runtime(before, policy, repo, model), ctx)
    assert before.calls == [] and not before.root.exists()
    text = '\n'.join(message.content for message in model.requests[0].messages
                     if isinstance(message.content, str))
    renderer = project_preflight_context_text if kind == 'preflight' else self_repair_context_text
    assert renderer(ctx.routing_decision) in text
    policy.allowed.update({'write', 'create'})
    cold = make_backend(tmp_path, ctx, repo)
    resumed = runtime(cold, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)

    events = [event async for event in resumed.run(ctx.model_copy(update={'checkpoint': checkpoint}))]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert (cold.root / 'README.md').read_text() == 'Stable approved guidance'
    assert cold.calls.count('workspace.bundle') == 1
    assert len(model.requests) == 1


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('changed', [False, True])
async def test_guidance_stays_bound_across_write_and_bundle_approval_workers(
    tmp_path: Path, kind: Kind, changed: bool,
) -> None:
    ctx, policy, repo = scoped(kind), Policy(), InMemoryArtifactRepository()
    model = Model(batch({'README.md': 'One write across fresh workers'}))
    first = make_backend(tmp_path, ctx, repo)
    write_checkpoint = await pause_at_write(runtime(first, policy, repo, model), ctx)
    assert first.calls == []

    policy.allowed.add('write')
    writer = make_backend(tmp_path, ctx, repo)
    writing = runtime(writer, policy, repo, model)
    await writing.restore_checkpoint(write_checkpoint)
    events = [event async for event in writing.run(ctx.model_copy(update={
        'checkpoint': write_checkpoint}))]
    bundle_checkpoint = events[-1].checkpoint
    assert bundle_checkpoint is not None
    assert bundle_checkpoint.state['tool_name'] == 'workspace.bundle'
    assert writer.calls.count('workspace.write_text') > 0
    assert writer.calls.count('workspace.bundle') == 0
    files = {path: path.read_bytes() for path in writer.root.rglob('*') if path.is_file()}

    policy.allowed.add('create')
    current = ctx.model_copy(update={'checkpoint': bundle_checkpoint})
    if changed:
        route = json.loads(json.dumps(ctx.routing_decision, default=dict))
        key = 'project_preflight_proposal' if kind == 'preflight' else 'self_repair_context'
        route[key]['ignored_by_renderer'] = {'future_guidance': True}
        current = current.model_copy(update={'routing_decision': route})
    bundler = make_backend(tmp_path, current, repo)
    resumed = runtime(bundler, policy, repo, model)
    await resumed.restore_checkpoint(bundle_checkpoint)
    if changed:
        with pytest.raises(RuntimeExecutionError, match='context changed'):
            _ = [event async for event in resumed.run(current)]
        assert bundler.calls == []
    else:
        completed = [event async for event in resumed.run(current)]
        assert completed[-1].kind is EventKind.RUNTIME_COMPLETED
        assert bundler.calls.count('workspace.bundle') == 1
        assert bundler.calls.count('workspace.write_text') == 0
    assert len(model.requests) == 1
    assert {path: path.read_bytes() for path in writer.root.rglob('*') if path.is_file()} == files


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('mutation', ['changed', 'removed', 'added', 'schema', 'schema_removed',
                                      'schema_bool', 'source', 'flag'])
async def test_changed_guidance_is_rejected_before_model_or_pending_tool(
    tmp_path: Path, kind: Kind, mutation: str,
) -> None:
    ctx, policy, repo = scoped(kind), Policy(), InMemoryArtifactRepository()
    backend = make_backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Original guidance'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    route = json.loads(json.dumps(ctx.routing_decision, default=dict))
    key = 'project_preflight_proposal' if kind == 'preflight' else 'self_repair_context'
    if mutation == 'changed':
        route[key]['summary' if kind == 'preflight' else 'instruction'] = 'Changed guidance'
    elif mutation == 'removed':
        for field in guidance(kind):
            route.pop(field)
    elif mutation == 'added':
        route[key]['future_context'] = {'mode': 'changed'}
    elif mutation == 'schema':
        route[key]['schema_version'] = 2
    elif mutation == 'schema_removed':
        route[key].pop('schema_version')
    elif mutation == 'schema_bool':
        route[key]['schema_version'] = True
    elif mutation == 'source':
        route['source'] = 'manual'
    else:
        route['project_preflight_approved' if kind == 'preflight' else 'self_repair_accepted'] = False
    changed = ctx.model_copy(update={'checkpoint': checkpoint, 'routing_decision': route})
    policy.allowed.update({'write', 'create'})
    cold = make_backend(tmp_path, changed, repo)
    resumed = runtime(cold, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)

    with pytest.raises(RuntimeExecutionError, match='(?:guidance|context)'):
        _ = [event async for event in resumed.run(changed)]
    assert not cold.root.exists() and cold.calls == []
    assert len(model.requests) == 1


@pytest.mark.parametrize('kind', KINDS)
async def test_added_guidance_cannot_replace_checkpoint_with_no_guidance(
    tmp_path: Path, kind: Kind,
) -> None:
    ctx, policy, repo = context(), Policy(), InMemoryArtifactRepository()
    backend = make_backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'No guidance at initial execution'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    changed = ctx.model_copy(update={'checkpoint': checkpoint,
        'routing_decision': {**ctx.routing_decision, **guidance(kind)}})
    policy.allowed.update({'write', 'create'})
    cold = make_backend(tmp_path, changed, repo)
    resumed = runtime(cold, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match='context changed'):
        _ = [event async for event in resumed.run(changed)]
    assert cold.calls == [] and not cold.root.exists()
    assert len(model.requests) == 1


@pytest.mark.parametrize('kind', KINDS)
def test_full_values_remain_bound_when_guidance_renderer_uses_same_defaults(kind: Kind) -> None:
    ctx = scoped(kind)
    first = json.loads(json.dumps(ctx.routing_decision, default=dict))
    second = json.loads(json.dumps(ctx.routing_decision, default=dict))
    key = 'project_preflight_proposal' if kind == 'preflight' else 'self_repair_context'
    field = 'capability' if kind == 'preflight' else 'recovery_strategy'
    first[key][field], second[key][field] = 'unknown-first', 'unknown-second'
    renderer = project_preflight_context_text if kind == 'preflight' else self_repair_context_text
    assert renderer(first) == renderer(second)
    assert _binding(ctx.model_copy(update={'routing_decision': first}), 'main') != _binding(
        ctx.model_copy(update={'routing_decision': second}), 'main')


@pytest.mark.parametrize('key', ['project_preflight_approved', 'project_preflight_proposal',
    'self_repair_context', 'self_repair_accepted', 'source'])
def test_guidance_presence_is_not_collapsed_into_explicit_null(key: str) -> None:
    ctx = context()
    assert _binding(ctx, 'main') != _binding(ctx.model_copy(update={
        'routing_decision': {**ctx.routing_decision, key: None}}), 'main')


@pytest.mark.parametrize('key', ['native_assistant', 'reasoning_content', 'credentials', 'raw_prompt'])
async def test_nonpersistable_guidance_is_rejected_before_model(tmp_path: Path, key: str) -> None:
    ctx = scoped('repair')
    route = json.loads(json.dumps(ctx.routing_decision, default=dict))
    route['self_repair_context'][key] = {'text': 'Never persist this'}
    ctx = ctx.model_copy(update={'routing_decision': route})
    policy, repo = Policy(), InMemoryArtifactRepository()
    backend = make_backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Must not run'}))
    with pytest.raises(RuntimeExecutionError, match='guidance'):
        _ = [event async for event in runtime(backend, policy, repo, model).run(ctx)]
    assert model.requests == [] and backend.calls == []
    assert not backend.root.exists()


async def test_self_repair_source_and_automatic_hint_never_authorize_pending_write(tmp_path: Path) -> None:
    ctx = scoped('repair')
    route = json.loads(json.dumps(ctx.routing_decision, default=dict))
    route['self_repair_context'].update(automatic_execution=True, requires_approval=False)
    ctx = ctx.model_copy(update={'routing_decision': route})
    policy, repo = Policy(), InMemoryArtifactRepository()
    backend = make_backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Still requires policy'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    cold = make_backend(tmp_path, ctx, repo)
    resumed = runtime(cold, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)
    next_checkpoint = await pause_at_write(resumed, ctx.model_copy(update={'checkpoint': checkpoint}))
    assert next_checkpoint.state['approval_id'] == 'approval_write'
    assert len(policy.requests) == 2
    assert len(model.requests) == 1 and cold.calls == []
    assert not cold.root.exists()


async def test_stable_guidance_does_not_bypass_current_policy_denial(tmp_path: Path) -> None:
    ctx, policy, repo = scoped('repair'), Policy(), InMemoryArtifactRepository()
    backend = make_backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Still requires current policy'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)

    class Denied(Policy):
        async def invoke(self, request: Any, *, role: Any) -> CapabilityResult:
            self.requests.append(request)
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id, reason='capability denied')

    denied = Denied()
    cold = make_backend(tmp_path, ctx, repo)
    resumed = runtime(cold, denied, repo, model)
    await resumed.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match='incremental workspace delivery failed'):
        _ = [event async for event in resumed.run(ctx.model_copy(update={'checkpoint': checkpoint}))]
    assert len(denied.requests) == 1 and cold.calls == []
    assert len(model.requests) == 1 and not cold.root.exists()
