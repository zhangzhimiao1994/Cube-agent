"""Persist finite read progress before the next model call or terminal error."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.direct import DirectRunStream, DirectRuntime, RuntimeExecutionError
from tests.unit.runtime.test_direct_generated_entry import Model, batch, response
from tests.unit.runtime.test_direct_read_context_retention import ReadHarness
from tests.unit.runtime.test_generated_project_history import evidence

PATH = 'src/PRIVATE_READ_PATH_225.py'
TEXT = 'PRIVATE_READ_BODY_225\n'
SUMMARY = 'PRIVATE_MODEL_SUMMARY_225'
COUNTS = {'known_file_count', 'batch_file_count', 'changed_file_count', 'read_count',
    'read_gain', 'retained_read_count', 'omitted_read_count', 'cache_bytes', 'max_cache_bytes'}
FLAGS = {'no_progress', 'correction_exhausted', 'entries_complete'}


def read() -> str:
    return json.dumps({'workspace_batch': {'files': {}, 'read_paths': [PATH],
        'complete': False, 'continuation': 'Use the read result to repair.'}, 'summary': SUMMARY})


def is_feedback(event: RunEvent) -> bool:
    return (event.kind == 'runtime.control_feedback'
        and event.payload.get('stage') == 'workspace_read_context'
        and event.payload.get('phase') == 'batch_progress')


def finite(event: RunEvent) -> None:
    data = event.payload
    assert event.message is None and event.reason is None and event.artifact is None
    assert set(data) <= {'schema_version', 'stage', 'phase'} | COUNTS | FLAGS
    assert data['stage'] == 'workspace_read_context' and data['phase'] == 'batch_progress'
    if 'schema_version' in data:
        assert type(data['schema_version']) is int and data['schema_version'] == 1
    for key in COUNTS & set(data):
        assert type(data[key]) is int and cast(int, data[key]) >= 0
    for key in FLAGS & set(data):
        assert type(data[key]) is bool
    for key in ['known_file_count', 'read_count', 'read_gain', 'retained_read_count']:
        assert key in data
    assert data['known_file_count'] == 0 and data['read_count'] == 1
    assert data['retained_read_count'] == 1
    encoded = json.dumps(dict(data), ensure_ascii=False, sort_keys=True)
    assert all(secret not in encoded for secret in [PATH, TEXT.strip(), SUMMARY])
    assert not {'path', 'text', 'summary', 'approval', 'authority', 'binding', 'source'} & set(data)


async def test_repeated_read_feedback_is_finite_and_precedes_next_model_or_failure(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000, 'timeout_seconds': 60})
    harness = ReadHarness(context, verified, bundle, {PATH: TEXT})
    model = Model(response(read()), response(read()), response(read()), response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match='workspace batch made no progress'):
        async for event in runtime.run(context):
            events.append(event)
    assert len(model.requests) == 3
    assert harness.calls == ['project.inspect', *(['read_context'] * 3)]
    feedback = [event for event in events if is_feedback(event)]
    assert feedback, 'Repeated reads must persist finite runtime.control_feedback before failing.'
    for event in feedback:
        finite(event)
    zero_gain = [event for event in feedback if event.payload['read_gain'] == 0]
    assert len(zero_gain) == 2
    assert zero_gain[0].payload['no_progress'] is True
    assert zero_gain[0].payload['correction_exhausted'] is False
    assert zero_gain[1].payload['no_progress'] is True
    assert zero_gain[1].payload['correction_exhausted'] is True
    starts = [event for event in events if event.kind is EventKind.MODEL_STARTED]
    received = [event for event in events if event.kind is EventKind.MODEL_COMPLETED]
    assert events.index(received[1]) < events.index(zero_gain[0]) < events.index(starts[2])
    assert events.index(received[2]) < events.index(zero_gain[1])
    assert not any(event.kind is EventKind.MODEL_STARTED for event in events[events.index(zero_gain[1]) + 1:])
    for terminal in [event for event in events if event.kind is EventKind.RUNTIME_FAILED]:
        assert events.index(zero_gain[1]) < events.index(terminal)


async def test_read_feedback_is_an_ack_boundary_before_model_transport(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000, 'timeout_seconds': 60})
    harness = ReadHarness(context, verified, bundle, {PATH: TEXT})
    model = Model(response(read()), response(read()), response(read()), response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    stream = runtime.run(context)
    assert isinstance(stream, DirectRunStream)
    try:
        try:
            async with asyncio.timeout(10):
                while True:
                    event = await anext(stream)
                    if is_feedback(event) and event.payload.get('read_gain') == 0:
                        break
        except RuntimeExecutionError:
            pytest.fail('No zero-gain persisted control boundary was yielded before no-progress failure.')
        finite(event)
        assert event.payload['correction_exhausted'] is False
        assert len(model.requests) == 2
        await asyncio.sleep(0)
        # Consumer has not acknowledged the yielded event; producer may not
        # enter a third model transport while this durable boundary is pending.
        assert len(model.requests) == 2
        assert harness.calls == ['project.inspect', 'read_context', 'read_context']
    finally:
        await stream.aclose()
    assert len(model.requests) == 2
    assert 'workspace.write_text' not in harness.calls and 'project.verify' not in harness.calls
