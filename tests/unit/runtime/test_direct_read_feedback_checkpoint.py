"""Read progress extends generated event layout without enabling any execution replay."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.runtime.contracts import Artifact, EventKind, RuntimeCheckpoint
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from tests.unit.runtime.test_direct_generated_entry import Model, batch, response
from tests.unit.runtime.test_direct_read_context_retention import ReadHarness, read_batch
from tests.unit.runtime.test_generated_project_history import evidence


@pytest.mark.parametrize('legacy_layout', [False, True])
async def test_successful_read_progress_checkpoint_restores_without_model_or_tool_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_layout: bool,
) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000, 'timeout_seconds': 60})
    harness = ReadHarness(context, verified, bundle, {'src/a.py': 'ACKNOWLEDGED_REFERENCE\n'})
    if legacy_layout:
        # Model the historical runtime which did not emit read progress events.
        # Do not strip live events or pretend a nonzero count was historically zero.
        async def historical_no_progress_events(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(DirectRuntime, '_workspace_read_progress', historical_no_progress_events)
    model = Model(response(read_batch('src/a.py')), response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    feedback = [event for event in events if event.kind == 'runtime.control_feedback'
        and event.payload.get('stage') == 'workspace_read_context']
    assert bool(feedback) is not legacy_layout
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state['next_sequence'] == events[-1].sequence
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    if legacy_layout:
        state: Any = json.loads(json.dumps(checkpoint.state, default=dict))
        assert state['generated_event_layout'].pop('read_progress_event_count', 0) == 0
        final = next(item for item in artifacts if str(item.id) == state['artifact_id'])
        legacy_final = Artifact(id=final.id, version=final.version, type=final.type,
            producer=final.producer, content={**final.content,
                'generated_event_layout': state['generated_event_layout']},
            source_ids=final.source_ids, provenance=final.provenance)
        state['artifact_sha256'] = legacy_final.content_sha256
        artifacts = tuple(legacy_final if item.id == final.id else item for item in artifacts)
        checkpoint = RuntimeCheckpoint(id=checkpoint.id, runtime_type=checkpoint.runtime_type,
            runtime_version=checkpoint.runtime_version, run_id=context.run_id,
            tenant_id=context.tenant_id, mode=context.mode, state=state)
    recovered_context = context.model_copy(update={'checkpoint': checkpoint, 'artifacts': artifacts})
    fresh_model = Model()
    fresh = DirectRuntime(fresh_model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    before = tuple(harness.calls)
    await fresh.restore_checkpoint(checkpoint)
    replayed = [event async for event in fresh.run(recovered_context)]
    assert len(replayed) == 1 and replayed[0].kind is EventKind.RUNTIME_COMPLETED
    assert replayed[0].sequence == checkpoint.state['next_sequence']
    assert not fresh_model.requests and tuple(harness.calls) == before
    if not legacy_layout:
        layout = checkpoint.state['generated_event_layout']
        assert isinstance(layout, Mapping)
        assert layout['read_progress_event_count'] == len(feedback)
        for value in [True, -1, len(model.requests) + 1]:
            state = json.loads(json.dumps(checkpoint.state, default=dict))
            state['generated_event_layout']['read_progress_event_count'] = value
            invalid = RuntimeCheckpoint(id=checkpoint.id, runtime_type=checkpoint.runtime_type,
                runtime_version=checkpoint.runtime_version, run_id=context.run_id,
                tenant_id=context.tenant_id, mode=context.mode, state=state)
            rejected = DirectRuntime(fresh_model, logical_model='fixture', capability_gateway=harness,
                harness_tool_gateway=cast(Any, harness))
            with pytest.raises(RuntimeExecutionError):
                await rejected.restore_checkpoint(invalid)
        state = json.loads(json.dumps(checkpoint.state, default=dict))
        state['next_sequence'] += 1
        invalid = RuntimeCheckpoint(id=checkpoint.id, runtime_type=checkpoint.runtime_type,
            runtime_version=checkpoint.runtime_version, run_id=context.run_id,
            tenant_id=context.tenant_id, mode=context.mode, state=state)
        with pytest.raises(RuntimeExecutionError):
            await fresh.restore_checkpoint(invalid)
        assert not fresh_model.requests and tuple(harness.calls) == before
