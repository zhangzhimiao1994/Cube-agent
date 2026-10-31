"""Unambiguous control-field placement must not discard a completed batch."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.direct import DirectRuntime, _workspace_batch_from_model_text
from tests.unit.runtime.test_direct_generated_entry import Harness, Model, response
from tests.unit.runtime.test_generated_project_history import evidence


def envelope(**updates: Any) -> dict[str, Any]:
    return {'workspace_batch': {'files': {'src/app.py': 'def total(): return 12\n'}},
        'complete': True, 'continuation': '', 'summary': 'Implementation ready.', **updates}


@pytest.mark.parametrize('complete', [False, True])
def test_unambiguous_outer_controls_preserve_files_and_completion(complete: bool) -> None:
    value = envelope(complete=complete, continuation='' if complete else 'Implement the remaining files.')
    batch = _workspace_batch_from_model_text(json.dumps(value))
    assert batch is not None
    assert batch.files == value['workspace_batch']['files']
    assert batch.complete is complete
    assert batch.continuation == value['continuation']
    assert batch.summary == value['summary']


@pytest.mark.parametrize('defect', ['duplicate', 'split', 'missing', 'boolean', 'continuation',
    'extra_root', 'extra_batch', 'traversal', 'read_without_permission', 'read_complete'])
def test_outer_control_recovery_rejects_ambiguous_or_unsafe_batches(defect: str) -> None:
    value = envelope()
    if defect == 'duplicate':
        value['workspace_batch'].update(complete=False, continuation='Do something else.')
    elif defect == 'split':
        value['workspace_batch']['complete'] = True
        value.pop('complete')
    elif defect == 'missing':
        value.pop('continuation')
    elif defect == 'boolean':
        value['complete'] = 1
    elif defect == 'continuation':
        value['continuation'] = {'commands': ['do not execute']}
    elif defect == 'extra_root':
        value['tool_calls'] = [{'name': 'command.run'}]
    elif defect == 'extra_batch':
        value['workspace_batch']['command'] = 'do not execute'
    elif defect == 'traversal':
        value['workspace_batch']['files'] = {'../outside.py': 'bad'}
    else:
        value['workspace_batch'].update(files={}, read_paths=['src/app.py'])
        if defect == 'read_without_permission':
            value.update(complete=False, continuation='Inspect source.')
    assert _workspace_batch_from_model_text(json.dumps(value), allow_reads=defect == 'read_complete') is None


@pytest.mark.parametrize('text', [
    '{"workspace_batch":{"files":{"a.py":"x"}},"complete":false,"complete":true,"continuation":""}',
    '{"workspace_batch":{"files":{"a.py":"first","a.py":"second"}},"complete":true,"continuation":""}',
    '{"workspace_batch":{"files":{"a.py":"x"}},"workspace_batch":{"files":{"b.py":"y"}},"complete":true,"continuation":""}',
    json.dumps(envelope()) + '\n{"tool_calls":[{"name":"command.run"}]}',
    'Plan before JSON: ' + json.dumps(envelope()),
    json.dumps(envelope()) + '\nAdditional instructions.',
], ids=['duplicate_control', 'duplicate_file', 'duplicate_batch', 'trailing_action', 'leading_prose', 'trailing_prose'])
def test_recovery_requires_one_complete_json_object_without_duplicate_keys(text: str) -> None:
    assert _workspace_batch_from_model_text(text) is None


@pytest.mark.parametrize('fenced', [False, True])
def test_complete_json_envelope_retains_read_authorization(fenced: bool) -> None:
    value = envelope(complete=False, continuation='Inspect implementation.')
    value['workspace_batch'] = {'files': {}, 'read_paths': ['src/app.py']}
    text = json.dumps(value)
    if fenced:
        text = '```json\n' + text + '\n```'
    batch = _workspace_batch_from_model_text(text, allow_reads=True)
    assert batch is not None and batch.read_paths == ('src/app.py',)
    assert batch.complete is False and not batch.files
    assert _workspace_batch_from_model_text(text, allow_reads=False) is None


async def test_subsequent_batch_uses_unambiguous_controls_without_extra_model_call(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    harness = Harness(context, verified, bundle)
    read = json.dumps({'workspace_batch': {'files': {}, 'read_paths': ['src/app.py'],
        'complete': False, 'continuation': 'Inspect source and implement totals.'}})
    model = Model(response(read), response(json.dumps(envelope())))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 2 and not model.responses
    assert harness.calls == ['project.inspect', 'read_context', 'workspace.write_text',
        'workspace.write_text', 'project.verify', 'workspace.bundle']
    assert sum(event.kind is EventKind.MODEL_COMPLETED for event in events) == 2
    assert sum(event.kind is EventKind.COST_RECORDED for event in events) == 2
