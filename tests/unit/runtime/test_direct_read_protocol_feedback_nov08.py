"""Structural regressions only; the failed production response was not retained.

Scripted GatewayCompletion fixtures exercise the real Direct entry. No provider,
native execution, approvals, or claim that these bytes reproduce the paid run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.models.types import ModelResponse
from agent_hub.runtime import direct
from agent_hub.runtime.contracts import EventKind
from tests.unit.runtime.test_direct_original_entry_protocol_regression_nov07 import (
    EntryBackend,
    PricedModel,
    entry_context,
)
from tests.unit.runtime.test_direct_original_project import batch, fixture
from tests.unit.runtime.test_direct_project_verification import Policy


def envelope(kind: str = 'read', **controls: object) -> str:
    operation = ({'read_paths': ['src/private_marker.py']} if kind == 'read' else
                 {'python_catalog': {'catalog_sha256': 'a' * 64, 'offset': 0}})
    return json.dumps({'workspace_batch': {'files': {}, **operation, **controls}})


def feedback(events):
    return [e.payload for e in events if e.kind == 'runtime.control_feedback'
            and e.payload.get('stage') == 'workspace_batch_protocol']


@pytest.mark.parametrize('kind', ['read', 'catalog'])
@pytest.mark.parametrize('controls', [{}, {'complete': False},
    {'complete': 'false', 'continuation': 'inspect'}, {'complete': False, 'continuation': ''}])
async def test_safe_read_control_format_gets_one_correction_without_tools(kind, controls):
    rejected = envelope(kind, **controls)
    model = PricedModel(rejected, rejected)
    context = entry_context()
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    events = []
    with pytest.raises(direct.RuntimeExecutionError, match='workspace batch is invalid'):
        async for event in runtime.run(context):
            events.append(event)
    assert len(model.requests) == 2, 'Safe control-format error bypassed bounded protocol correction'
    assert all(not r.tools and r.logical_model == 'main' for r in model.requests)
    assert all(0 < r.timeout_seconds <= context.timeout_seconds for r in model.requests)
    costs = [e for e in events if e.kind is EventKind.COST_RECORDED]
    assert len(costs) == 2 and sum(e.payload['total_tokens'] for e in costs) == 360
    notes = feedback(events)
    assert len(notes) == 2 and notes[0]['retryable'] is True and notes[1]['retryable'] is False
    assert all(n['rejected_response_tool_io'] is False for n in notes)
    public = json.dumps(notes, default=dict)
    assert 'private_marker' not in public and 'a' * 64 not in public
    assert not any(e.kind is EventKind.RUNTIME_COMPLETED for e in events)


@pytest.mark.parametrize('raw', [
    '{"workspace_batch":{"files":{},"read_paths":["src/x.py"],"complete":false,"complete":false,"continuation":"read"}}',
    '{"workspace_batch":{"files":{},"read_paths":["src/x.py"],"complete":false,"continuation":"read","command":"no"}}',
    '{"workspace_batch":{"files":{},"read_paths":["src/x.py"],"complete":false,"continuation":"read","other":NaN}}',
    'prefix {"workspace_batch":{"files":{},"read_paths":["src/x.py"],"complete":false,"continuation":"read"}} suffix',
])
def test_ambiguous_or_extra_read_envelope_never_becomes_a_batch(raw):
    assert direct._workspace_batch_from_model_text(raw, allow_reads=True) is None


@pytest.mark.parametrize('raw', [
    envelope(read_paths=['../private']),
    envelope(read_paths=['/etc/passwd']),
    envelope(read_paths=['src/A.py', 'src/a.py']),
    envelope(read_paths=[]),
    envelope(read_paths=['x.py'] * 9),
    envelope(files={'src/x.py': 'NEVER_WRITE'}),
    envelope(command='never_run'),
    envelope('catalog', python_catalog={'catalog_sha256': 'not-a-digest'}),
    envelope('catalog', read_paths=['src/x.py']),
])
async def test_unsafe_or_mixed_read_stays_hard_rejected(raw):
    model = PricedModel(raw, raw)
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    events = []
    with pytest.raises(direct.RuntimeExecutionError, match='workspace batch is invalid'):
        async for event in runtime.run(entry_context()):
            events.append(event)
    assert len(model.requests) == 1
    notes = feedback(events)
    assert len(notes) == 1 and notes[0]['retryable'] is False
    assert notes[0]['rejected_response_tool_io'] is False


async def test_disabled_catalog_does_not_buy_format_feedback():
    context = entry_context()
    context = context.model_copy(update={'routing_decision': {
        **context.routing_decision, 'engineering_catalog_reads_version': None}})
    model = PricedModel(envelope('catalog'), envelope('catalog'))
    events = []
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    with pytest.raises(direct.RuntimeExecutionError, match='workspace batch is invalid'):
        async for event in runtime.run(context):
            events.append(event)
    assert len(model.requests) == 1
    assert feedback(events)[0]['reason'] == 'catalog_not_enabled'


def test_parser_valid_controls_do_not_imply_read_authority():
    raw = envelope(complete=False, continuation='read')
    assert direct._workspace_batch_from_model_text(raw, allow_reads=False) is None
    assert direct._workspace_batch_from_model_text(raw, allow_reads=True) is not None
    assert direct._workspace_batch_rejection_code(ModelResponse(text='ordinary prose')) == 'invalid_json_envelope'


@pytest.mark.parametrize('kind', ['read', 'catalog'])
async def test_only_absent_files_can_receive_feedback_without_inventing_writes(kind):
    value = json.loads(envelope(kind, complete=False, continuation='inspect'))
    del value['workspace_batch']['files']
    raw = json.dumps(value)
    model = PricedModel(raw, raw)
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    events = []
    with pytest.raises(direct.RuntimeExecutionError, match='workspace batch is invalid'):
        async for event in runtime.run(entry_context()):
            events.append(event)
    assert len(model.requests) == 2
    assert len(feedback(events)) == 2 and not feedback(events)[-1]['retryable']


@pytest.mark.parametrize('later', [False, True])
@pytest.mark.parametrize('denied', [False, True])
async def test_feedback_then_corrected_read_still_uses_real_policy_and_preserves_original(
    tmp_path: Path, later: bool, denied: bool,
):
    configured = await fixture(tmp_path)
    protected = {p: (configured.source / p).read_bytes() for p in ('tests/original.js', 'package.json')}
    malformed = json.dumps({'workspace_batch': {'files': {}, 'read_paths': ['src/detail.js']}})
    good_read = batch(reads=['src/detail.js'])
    patch = batch(files={'src/index.js': 'module.exports = () => 12;\n'}, complete=True)
    # Later recovery follows one accepted read. The rejected read is never run.
    responses = ([good_read, malformed, patch] if later else [malformed, good_read, patch])
    model = PricedModel(*responses)
    policy = Policy(configured.context, CapabilityStatus.DENIED if denied else CapabilityStatus.ALLOWED)
    shared = HarnessToolGateway(configured.backend, policy_gateway=policy, require_actor_identity=True)
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=configured.backend,
                                   harness_tool_gateway=shared)
    events = []
    if denied:
        with pytest.raises(direct.RuntimeExecutionError, match='workspace read'):
            async for event in runtime.run(configured.context):
                events.append(event)
        assert configured.backend.calls == []
        assert len(model.requests) == (1 if later else 2)
    else:
        async for event in runtime.run(configured.context):
            events.append(event)
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(model.requests) == 3
        names = [n for n, _ in configured.backend.calls]
        assert names.count('read_context') == 1
        assert names[-2:] == ['project.verify', 'workspace.bundle']
        assert (configured.source / 'src/index.js').read_text() == 'module.exports = () => 12;\n'
        assert len(feedback(events)) == 1
        assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert all((configured.source / p).read_bytes() == raw for p, raw in protected.items())
