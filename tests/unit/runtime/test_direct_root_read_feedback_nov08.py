"""Root-shaped read syntax is feedback-only, never an executable read.

These structural fixtures do not reconstruct the unretained paid response.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from agent_hub.models.types import ModelResponse, ToolCall
from agent_hub.runtime import direct
from agent_hub.runtime.contracts import EventKind
from tests.unit.runtime.test_direct_original_entry_protocol_regression_nov07 import (
    EntryBackend,
    PricedModel,
    entry_context,
)
from tests.unit.runtime.test_direct_original_project import batch, fixture
from tests.unit.runtime.test_direct_read_protocol_feedback_nov08 import feedback


def root_read(kind: str = 'read', **updates: object) -> str:
    operation = ({'read_paths': ['src/detail.js']} if kind == 'read' else
                 {'python_catalog': {'catalog_sha256': 'a' * 64, 'offset': 0}})
    return json.dumps({'files': {}, **operation, 'complete': False,
                       'continuation': 'inspect then repair', **updates})


@pytest.mark.parametrize('kind', ['read', 'catalog'])
@pytest.mark.parametrize('controls', [{}, {'complete': True}, {'continuation': ''}])
async def test_root_read_shape_buys_only_one_bounded_correction(
    kind: str, controls: dict[str, object],
) -> None:
    raw = root_read(kind, **controls)
    assert direct._workspace_batch_from_model_text(raw, allow_reads=True, allow_catalog=True) is None
    model = PricedModel(raw, raw)
    context = entry_context()
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    events = []
    with pytest.raises(direct.RuntimeExecutionError, match='workspace batch is invalid'):
        async for event in runtime.run(context):
            events.append(event)
    assert len(model.requests) == 2
    assert all(r.logical_model == 'main' and not r.tools for r in model.requests)
    assert all(0 < r.timeout_seconds <= context.timeout_seconds for r in model.requests)
    notes = feedback(events)
    assert [n['retryable'] for n in notes] == [True, False]
    assert all(n['rejected_response_tool_io'] is False for n in notes)
    assert all(n['reason'] == kind + '_batch_controls' for n in notes)
    assert 'src/detail.js' not in json.dumps(notes, default=dict)
    assert sum(e.kind is EventKind.MODEL_COMPLETED for e in events) == 2
    costs = [e for e in events if e.kind is EventKind.COST_RECORDED]
    assert len(costs) == 2 and all(e.payload['total_tokens'] == 180 for e in costs)
    assert sum(e.cost_usd for e in costs if e.cost_usd is not None) == Decimal('0.002')
    assert not any(e.kind is EventKind.RUNTIME_COMPLETED for e in events)


@pytest.mark.parametrize('raw', [
    root_read(read_paths=['../private']), root_read(read_paths=['/etc/passwd']),
    root_read(read_paths=['src/a.py', 'SRC/a.py']), root_read(read_paths=[]),
    root_read(read_paths=['x.py'] * 9), root_read(files={'src/a.py': 'never write'}),
    root_read(command='never execute'), root_read(python_catalog={'catalog_sha256': 'a' * 64}),
    root_read('catalog', python_catalog={'catalog_sha256': 'invalid'}),
    root_read(summary={'extra': 'operation'}),
    '{"files":{},"read_paths":["src/a.py"],"complete":false,"complete":false,"continuation":"read"}',
    '{"files":{},"read_paths":["src/a.py"],"complete":false,"continuation":"read","extra":NaN}',
    'prefix ' + root_read() + ' suffix',
    json.dumps({'workspace_bundle': json.loads(root_read())}),
])
def test_root_read_hazards_remain_unretryable_and_unexecutable(raw: str) -> None:
    rejected = direct._workspace_read_batch_rejection(
        ModelResponse(text=raw), allow_reads=True, allow_catalog=True)
    assert rejected is not None and rejected[1] is False
    assert direct._workspace_batch_from_model_text(raw, allow_reads=True, allow_catalog=True) is None


@pytest.mark.parametrize('kind', ['read', 'catalog'])
def test_root_shape_does_not_enable_disabled_reads(kind: str) -> None:
    raw = root_read(kind)
    assert direct._workspace_read_batch_rejection(
        ModelResponse(text=raw), allow_reads=False, allow_catalog=False) == (
            'read_not_enabled' if kind == 'read' else 'catalog_not_enabled', False)


def test_root_read_with_native_call_stays_hard_rejected() -> None:
    response = ModelResponse(text=root_read(), tool_calls=(ToolCall('attempt', 'read_context', {}),))
    assert direct._workspace_read_batch_rejection(response, allow_reads=True, allow_catalog=True) == (
        'native_read_tools_not_executed', False)


async def test_root_feedback_never_restarts_exhausted_budget() -> None:
    model = PricedModel(root_read(), root_read(), usage=50_001)
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    with pytest.raises(direct.RuntimeExecutionError):
        _ = [event async for event in runtime.run(entry_context())]
    assert len(model.requests) == 1


async def test_final_defensive_consumer_keeps_disabled_reads_unretryable() -> None:
    context = entry_context()
    context = context.model_copy(update={'routing_decision': {
        **context.routing_decision, 'project_input_contract_version': None, 'project_input': None,
        'engineering_diagnostic_reads_version': None, 'engineering_catalog_reads_version': None}})
    assert direct._is_workspace_project_delivery(context)
    assert not direct._original_project_policy(context)
    model = PricedModel(root_read(), root_read())
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    events = []
    with pytest.raises(direct.RuntimeExecutionError, match='workspace batch is invalid'):
        async for event in runtime.run(context):
            events.append(event)
    assert len(model.requests) == 1
    notes = feedback(events)
    assert len(notes) == 1 and notes[0]['reason'] == 'read_not_enabled'
    assert notes[0]['retryable'] is False and notes[0]['rejected_response_tool_io'] is False


@pytest.mark.skipif(os.name != 'posix', reason='real original baseline requires POSIX filesystem evidence')
@pytest.mark.parametrize('later', [False, True])
async def test_root_format_correction_then_real_read_repair_verify_bundle(
    tmp_path: Path, later: bool,
) -> None:
    configured = await fixture(tmp_path)
    protected = {p: (configured.source / p).read_bytes() for p in ('tests/original.js', 'package.json')}
    good_read = batch(reads=['src/detail.js'])
    patch = batch(files={'src/index.js': 'module.exports = () => 12;\n'}, complete=True)
    responses = ([good_read, root_read(), patch] if later else [root_read(), good_read, patch])
    model = PricedModel(*responses)
    runtime, _, policy = configured.runtime(model)
    events = [event async for event in runtime.run(configured.context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 3
    names = [n for n, _ in configured.backend.calls]
    assert names.count('read_context') == 1
    assert names[-2:] == ['project.verify', 'workspace.bundle']
    assert policy.calls[0].operation == 'read'
    assert len(feedback(events)) == 1
    assert feedback(events)[0]['retryable'] is True
    costs = [e for e in events if e.kind is EventKind.COST_RECORDED]
    assert len(costs) == 3 and all(e.payload['total_tokens'] == 180 for e in costs)
    assert sum(e.cost_usd for e in costs if e.cost_usd is not None) == Decimal('0.003')
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert (configured.source / 'src/index.js').read_text() == 'module.exports = () => 12;\n'
    assert all((configured.source / p).read_bytes() == value for p, value in protected.items())
