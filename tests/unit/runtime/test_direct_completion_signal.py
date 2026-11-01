"""A completion control verifies prior writes; it never supplies delivery evidence."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from tests.unit.runtime.test_direct_generated_entry import Harness, Model, batch, response
from tests.unit.runtime.test_generated_project_history import evidence


async def test_empty_completion_after_written_batch_runs_verification_before_bundle(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    harness = Harness(context, verified, bundle)
    first = json.loads(batch())
    first['workspace_batch'].update(complete=False, continuation='All files written; verify next.')
    terminal = json.dumps({'workspace_batch': {'files': {}, 'complete': True, 'continuation': ''},
        'summary': 'Ready for actual verification.'})
    model = Model(response(json.dumps(first)), response(terminal))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert harness.calls == ['project.inspect', 'workspace.write_text', 'workspace.write_text',
        'project.verify', 'workspace.bundle']
    assert len(model.requests) == 2
    assert sum(event.kind is EventKind.MODEL_COMPLETED for event in events) == 2


async def test_empty_initial_completion_cannot_deliver_an_empty_project(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    harness = Harness(context, verified, bundle)
    terminal = json.dumps({'workspace_batch': {'files': {}, 'complete': True, 'continuation': ''}})
    model = Model(response(terminal), response(terminal))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(context)]
    assert harness.calls == ['project.inspect']
