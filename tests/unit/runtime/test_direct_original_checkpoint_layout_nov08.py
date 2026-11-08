"""Original-workspace publication/recovery, using scripted models and real gates.

The existing ContractExecutor supplies report fixtures, not native execution
evidence. No server, real provider, approval or historical user run is invoked.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runs.service import _main_delivery_publication_batches
from agent_hub.runtime import direct, hybrid_delivery_report
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
    validate_publication_checkpoint,
)
from agent_hub.runtime.generated_project_history import generated_history_selected
from tests.unit.runtime.crew import test_delivery_report_verification_tail as tail_fixtures
from tests.unit.runtime.test_direct_original_entry_protocol_regression_nov07 import PricedModel
from tests.unit.runtime.test_direct_original_project import Fixture, batch, fixture
from tests.unit.runtime.test_direct_project_verification import Model, Policy

LAYOUT = 'original_workspace_event_layout'
REPORT = '已修复原项目并保留原测试和配置；验证结果见交付记录。未进行生产部署验证。'
native_case = tail_fixtures.native_case
native_node_case = tail_fixtures.native_node_case
node_case = tail_fixtures.node_case


async def completed(
    tmp_path: Path, *, corrected: bool, report: bool,
) -> tuple[Fixture, TaskContext, direct.DirectRuntime, PricedModel, HarnessToolGateway,
           list[RunEvent], RuntimeCheckpoint, TaskContext]:
    configured = await fixture(tmp_path)
    context = configured.context
    if report:
        context = context.model_copy(update={'routing_decision': {
            **context.routing_decision, 'engineering_direct_report_version': 1}})
    protected = {p: (configured.source / p).read_bytes() for p in ('tests/original.js', 'package.json')}
    texts = [batch(reads=['src/detail.js']),
             batch(files={'src/index.js': 'module.exports = () => 12;\n'}, complete=True)]
    if corrected:
        texts.insert(0, json.dumps({'workspace_batch': {'read_paths': ['src/detail.js']}}))
    if report:
        texts.append(REPORT)
    model = PricedModel(*texts)
    policy = Policy(context, CapabilityStatus.ALLOWED)
    harness = HarnessToolGateway(configured.backend, policy_gateway=policy, require_actor_identity=True)
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=configured.backend,
                                   harness_tool_gateway=harness)
    events = [e async for e in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert all((configured.source / p).read_bytes() == raw for p, raw in protected.items())
    checkpoint = await runtime.save_checkpoint()
    artifacts = tuple(e.artifact for e in events if e.artifact is not None)
    recovery = context.model_copy(update={'checkpoint': checkpoint,
                                         'artifacts': (*context.artifacts, *artifacts)})
    return configured, context, runtime, model, harness, events, checkpoint, recovery


@pytest.mark.parametrize('corrected', [False, True])
@pytest.mark.parametrize('report', [False, True])
async def test_original_completion_publishes_and_recovers_without_replay(tmp_path: Path, corrected: bool, report: bool) -> None:
    configured, context, runtime, model, harness, events, cp, recovery = await completed(
        tmp_path, corrected=corrected, report=report)
    # RED on56bafa: completed original runtime rejects its own checkpoint here.
    assert runtime._is_completed_checkpoint_state(cp)
    validate_publication_checkpoint(runtime, cp, recovery)
    layout = cp.state[LAYOUT]
    assert isinstance(layout, Mapping)
    assert context.actor_role is not None
    assert 'generated_event_layout' not in cp.state
    assert layout['schema_version'] == 1
    assert layout['scope'] == {
        'tenant_id': str(context.tenant_id), 'run_id': str(context.run_id),
        'actor_id': str(context.actor_id), 'actor_role': context.actor_role.value,
        'project_id': context.routing_decision['project_id'],
        'session_id': context.routing_decision['workspace_session_id'],
    }
    assert layout['model_started_event_count'] == sum(e.kind is EventKind.MODEL_STARTED for e in events)
    assert layout['model_completed_event_count'] == sum(e.kind is EventKind.MODEL_COMPLETED for e in events)
    assert layout['model_cost_event_count'] == sum(e.kind is EventKind.COST_RECORDED for e in events)
    assert layout['protocol_feedback_event_count'] == int(corrected)
    assert layout['read_progress_event_count'] == 1
    assert layout['verification_failure_event_count'] == 0
    assert cp.state['next_sequence'] == events[-1].sequence
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    final = next(a for a in recovery.artifacts if str(a.id) == cp.state['artifact_id'])
    assert final.content[LAYOUT] == layout and final.content_sha256 == cp.state['artifact_sha256']
    requests_before, calls_before = len(model.requests), tuple(configured.backend.calls)
    async def stored_events() -> AsyncIterator[RunEvent]:
        for event in events:
            yield event
    published = [group async for group in _main_delivery_publication_batches(
        stored_events(), context, runtime=runtime, recovery_events=())]
    assert published[-1][-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == requests_before and tuple(configured.backend.calls) == calls_before
    fresh_model = Model()
    fresh = direct.DirectRuntime(fresh_model, logical_model='main', capability_gateway=configured.backend,
                                 harness_tool_gateway=harness)
    await fresh.restore_checkpoint(cp)
    resumed = [e async for e in fresh.run(recovery)]
    assert len(resumed) == 1 and resumed[0].kind is EventKind.RUNTIME_COMPLETED
    assert resumed[0].sequence == cp.state['next_sequence']
    assert not fresh_model.requests and tuple(configured.backend.calls) == calls_before


async def test_original_layout_tampering_never_authorizes_publication_or_recovery(tmp_path: Path) -> None:
    configured, _, runtime, _, harness, _, cp, recovery = await completed(tmp_path, corrected=True, report=True)
    assert runtime._is_completed_checkpoint_state(cp)
    final = next(a for a in recovery.artifacts if str(a.id) == cp.state['artifact_id'])
    before = tuple(configured.backend.calls)
    defects = ('schema_bool', 'count_bool', 'count_negative', 'lifecycle_mismatch', 'cost_overflow',
        'protocol_read_overlap', 'context_overflow', 'scope_count_mismatch', 'prefix', 'layout_only',
        'self_consistent_counts', 'tenant', 'run', 'actor', 'role', 'project', 'session',
        'protected', 'artifact_hash', 'report_text', 'missing_layout', 'mixed_generated', 'plain_chat')
    for defect in defects:
        state = json.loads(json.dumps(cp.state, default=dict))
        content = json.loads(json.dumps(final.content, default=dict))
        layout = state[LAYOUT]
        context = recovery
        mirror = False
        if defect == 'schema_bool':
            layout['schema_version'] = True
        elif defect == 'count_bool':
            layout['protocol_feedback_event_count'] = True
        elif defect == 'count_negative':
            layout['verification_failure_event_count'] = -1
        elif defect == 'lifecycle_mismatch':
            layout['model_completed_event_count'] -= 1
        elif defect == 'cost_overflow':
            layout['model_cost_event_count'] = layout['model_started_event_count'] + 1
        elif defect == 'protocol_read_overlap':
            layout['read_progress_event_count'] = layout['model_started_event_count']
        elif defect == 'context_overflow':
            layout['context_injection_event_count'] = 2
        elif defect == 'scope_count_mismatch':
            layout['model_scope_event_count'] += 1
        elif defect == 'prefix':
            state['next_sequence'] += 1
        elif defect in ('layout_only', 'self_consistent_counts'):
            layout['model_started_event_count'] += 1
            layout['model_completed_event_count'] += 1
            if defect == 'self_consistent_counts':
                state['next_sequence'] += 2
        elif defect in ('tenant', 'run', 'actor', 'role', 'project', 'session'):
            key = {'tenant': 'tenant_id', 'run': 'run_id', 'actor': 'actor_id',
                   'role': 'actor_role', 'project': 'project_id', 'session': 'session_id'}[defect]
            layout['scope'][key] = ('admin' if defect == 'role' else 'different-scope'
                                   if defect in ('project', 'session') else str(uuid4()))
            mirror = True  # Matching artifact/layout still must match trusted context.
        elif defect == 'protected':
            content['workspace_delivery']['original_check_contract']['protected_sha256'] = '0' * 64
        elif defect == 'artifact_hash':
            state['artifact_sha256'] = '0' * 64
        elif defect == 'report_text':
            content['text'] = 'Unbound report.'
        elif defect == 'missing_layout':
            state.pop(LAYOUT)
            content.pop(LAYOUT)
            state['next_sequence'] = 4 + state.get('model_scope_event_count', 0)
        elif defect == 'mixed_generated':
            state['generated_event_layout'] = deepcopy(layout)
        elif defect == 'plain_chat':
            context = recovery.model_copy(update={'routing_decision': {}})
        if mirror:
            content[LAYOUT] = deepcopy(layout)
        if mirror or defect in ('protected', 'report_text', 'missing_layout'):
            replacement = Artifact(id=final.id, version=final.version, type=final.type,
                producer=final.producer, content=content, source_ids=final.source_ids,
                provenance=final.provenance)
            state['artifact_sha256'] = replacement.content_sha256
            context = context.model_copy(update={'artifacts': tuple(
                replacement if a.id == final.id else a for a in recovery.artifacts)})
        bad = RuntimeCheckpoint(id=cp.id, runtime_type=cp.runtime_type, runtime_version=cp.runtime_version,
            run_id=cp.run_id, tenant_id=cp.tenant_id, mode=cp.mode, state=state)
        context = context.model_copy(update={'checkpoint': bad})
        model = Model()
        fresh = direct.DirectRuntime(model, logical_model='main', capability_gateway=configured.backend,
                                     harness_tool_gateway=harness)
        with pytest.raises(direct.RuntimeExecutionError):
            validate_publication_checkpoint(fresh, bad, context)
        with pytest.raises(direct.RuntimeExecutionError):
            await fresh.restore_checkpoint(bad)
            _ = [e async for e in fresh.run(context)]
        assert not model.requests and tuple(configured.backend.calls) == before, defect


async def test_plain_chat_keeps_legacy_small_prefix_and_has_no_workspace_layout() -> None:
    from tests.unit.runtime.test_direct_project_verification import context

    runtime = direct.DirectRuntime(Model('A plain explanation.'), logical_model='main')
    current = context(routing_decision={})
    events = [e async for e in runtime.run(current)]
    cp = await runtime.save_checkpoint()
    assert LAYOUT not in cp.state and 'generated_event_layout' not in cp.state
    assert runtime._is_completed_checkpoint_state(cp)
    state = json.loads(json.dumps(cp.state, default=dict))
    state['next_sequence'] = 100
    bad = RuntimeCheckpoint(id=cp.id, runtime_type=cp.runtime_type, runtime_version=cp.runtime_version,
        run_id=cp.run_id, tenant_id=cp.tenant_id, mode=cp.mode, state=state)
    assert not runtime._is_completed_checkpoint_state(bad)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


def framed_original_context(case: tail_fixtures.Case) -> TaskContext:
    report, _, models = tail_fixtures.report(case)
    artifacts = (*case.artifacts.values(), *case.sources.values(), *models.values(), report)
    _, required = hybrid_delivery_report.delivery(case.context, artifacts,
                                                  output_ids={a.id for a in artifacts})
    return case.context.model_copy(update={'mode': TaskMode.DIRECT, 'artifacts': required,
        'routing_decision': {**case.context.routing_decision, 'engineering_hybrid_report_version': 1,
            'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle',
            hybrid_delivery_report.INPUT: {str(a.id): a.content_sha256 for a in required}}})


async def test_original_framed_report_only_consumer_keeps_legacy_publication_and_recovery(node_case: tail_fixtures.Case) -> None:
    context = framed_original_context(node_case)
    assert direct._original_project_policy(context)
    assert direct._is_workspace_project_delivery(context)
    assert not generated_history_selected(context)
    model = Model(REPORT)
    runtime = direct.DirectRuntime(model, logical_model='main')
    events = [e async for e in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    cp = await runtime.save_checkpoint()
    assert LAYOUT not in cp.state and 'generated_event_layout' not in cp.state
    artifacts = tuple(e.artifact for e in events if e.artifact is not None)
    final = next(a for a in artifacts if str(a.id) == cp.state['artifact_id'])
    delivery = final.content['workspace_delivery']
    assert isinstance(delivery, Mapping)
    assert delivery[direct._ORIGINAL_REPORT_SCOPE] == {
        'schema_version': 1, 'tenant_id': str(context.tenant_id), 'run_id': str(context.run_id),
        'actor_id': None if context.actor_id is None else str(context.actor_id),
        'actor_role': None if context.actor_role is None else context.actor_role.value,
        'project_id': context.routing_decision['project_id'],
        'session_id': context.routing_decision['workspace_session_id']}
    recovery = context.model_copy(update={'artifacts': (*context.artifacts, *artifacts), 'checkpoint': cp})
    # RED7037: inherited original flags falsely require an executor's layout.
    validate_publication_checkpoint(runtime, cp, recovery)
    assert len(model.requests) == 1 and all(not r.tools for r in model.requests)
    async def stored_events() -> AsyncIterator[RunEvent]:
        for event in events:
            yield event
    published = [group async for group in _main_delivery_publication_batches(
        stored_events(), context, runtime=runtime, recovery_events=())]
    assert published[-1][-1].kind is EventKind.RUNTIME_COMPLETED
    cold_model = Model()
    cold = direct.DirectRuntime(cold_model, logical_model='main')
    await cold.restore_checkpoint(cp)
    recovered = [e async for e in cold.run(recovery)]
    assert len(recovered) == 1 and recovered[0].kind is EventKind.RUNTIME_COMPLETED
    assert not cold_model.requests


async def test_report_consumer_exception_cannot_replace_frame_scope_or_executor_layout(node_case: tail_fixtures.Case) -> None:
    context = framed_original_context(node_case)
    runtime = direct.DirectRuntime(Model(REPORT), logical_model='main')
    events = [e async for e in runtime.run(context)]
    cp = await runtime.save_checkpoint()
    artifacts = tuple(e.artifact for e in events if e.artifact is not None)
    recovery = context.model_copy(update={'artifacts': (*context.artifacts, *artifacts), 'checkpoint': cp})
    validate_publication_checkpoint(runtime, cp, recovery)
    final = next(a for a in artifacts if str(a.id) == cp.state['artifact_id'])
    from tests.unit.runtime.test_direct_original_entry_protocol_regression_nov07 import EntryBackend

    for defect in ('missing_frame', 'wrong_ref', 'policy_only', 'protected', 'report',
                   'mirror_project', 'mirror_session', 'mirror_run', 'mirror_tenant',
                   'mirror_actor', 'mirror_role',
                   'missing_scope', 'rewrite_scope', 'executor'):
        route = json.loads(json.dumps(recovery.routing_decision, default=dict))
        state = json.loads(json.dumps(cp.state, default=dict))
        chosen = recovery.artifacts
        run_id, tenant_id = cp.run_id, cp.tenant_id
        actor_id, actor_role = recovery.actor_id, recovery.actor_role
        if defect == 'missing_frame':
            route.pop(hybrid_delivery_report.INPUT)
        elif defect == 'wrong_ref':
            route[hybrid_delivery_report.INPUT][next(iter(route[hybrid_delivery_report.INPUT]))] = '0' * 64
        elif defect == 'policy_only':
            route[hybrid_delivery_report.INPUT] = {}
        elif defect == 'mirror_project':
            route['project_id'] = 'different-project'
        elif defect == 'mirror_run':
            run_id = uuid4()
        elif defect == 'mirror_tenant':
            tenant_id = uuid4()
        elif defect == 'mirror_session':
            route['workspace_session_id'] = 'different-session'
        elif defect == 'mirror_actor':
            actor_id = uuid4()
        elif defect == 'mirror_role':
            actor_role = Role.ADMIN if actor_role is not Role.ADMIN else Role.VIEWER
        elif defect in ('protected', 'report', 'missing_scope', 'rewrite_scope'):
            content = json.loads(json.dumps(final.content, default=dict))
            if defect == 'protected':
                content['workspace_delivery']['original_check_contract']['protected_sha256'] = '0' * 64
            elif defect == 'report':
                content['text'] = 'Unbound report.'
            elif defect == 'missing_scope':
                del content['workspace_delivery'][direct._ORIGINAL_REPORT_SCOPE]
            else:
                # Even recomputing the final artifact/checkpoint hashes cannot
                # move the scope under the original report binding.
                route['project_id'] = 'different-project'
                content['workspace_delivery'][direct._ORIGINAL_REPORT_SCOPE]['project_id'] = 'different-project'
            changed = Artifact(id=final.id, type=final.type, producer=final.producer, version=final.version,
                content=content, provenance=final.provenance, source_ids=final.source_ids)
            state['artifact_sha256'] = changed.content_sha256
            chosen = tuple(changed if a.id == final.id else a for a in chosen)
        bad = RuntimeCheckpoint(id=cp.id, runtime_type=cp.runtime_type, runtime_version=cp.runtime_version,
            run_id=run_id, tenant_id=tenant_id, mode=cp.mode, state=state)
        subject = recovery.model_copy(update={'checkpoint': bad, 'routing_decision': route, 'artifacts': chosen,
                                             'run_id': run_id, 'tenant_id': tenant_id,
                                             'actor_id': actor_id, 'actor_role': actor_role})
        unused = Model()
        backend = EntryBackend() if defect == 'executor' else None
        receiver = direct.DirectRuntime(unused, logical_model='main', capability_gateway=backend)
        try:
            validate_publication_checkpoint(receiver, bad, subject)
        except direct.RuntimeExecutionError:
            pass
        else:
            pytest.fail('accepted invalid framed consumer: ' + defect)
        with pytest.raises(direct.RuntimeExecutionError):
            await receiver.restore_checkpoint(bad)
            _ = [e async for e in receiver.run(subject)]
        assert not unused.requests, defect
