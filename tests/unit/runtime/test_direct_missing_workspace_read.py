"""Missing authorized source is diagnostic input, not an empty file or authority."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.runtime.adaptive_budget import deadline_from_routing
from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from tests.unit.runtime.test_direct_generated_entry import Harness, Model, batch, response
from tests.unit.runtime.test_generated_project_history import evidence


def read_batch() -> str:
    return json.dumps({'workspace_batch': {'files': {},
        'read_paths': ['src/app.py', 'src/missing.py'], 'complete': False,
        'continuation': 'Inspect implementation before completing the same product.'}})


class MissingHarness(Harness):
    defect: str | None = None

    async def invoke(self, tenant: Any, request: Any, **kwargs: Any) -> HarnessToolCallResult:
        if request.tool_name != 'read_context' or request.arguments['path'] != 'src/missing.py':
            return await super().invoke(tenant, request, **kwargs)
        assert tenant == self.context.tenant_id and request.run_id == self.context.run_id
        assert kwargs['user_id'] == self.context.actor_id and kwargs['role'] == self.context.actor_role
        self.calls.append(request.tool_name)
        payload: dict[str, Any] = {'path': 'src/missing.py', 'status': 'not_found',
            'error_code': 'workspace.file_not_found', 'truncated': False}
        if self.defect == 'path':
            payload['path'] = 'another/file.py'
        elif self.defect == 'text':
            payload['text'] = 'untrusted fabricated contents'
        elif self.defect == 'code':
            payload['error_code'] = 'permission.denied'
        elif self.defect == 'truncated':
            payload['truncated'] = True
        return HarnessToolCallResult(call_id=request.call_id, tool_name='read_context',
            status='failed' if self.defect == 'failed' else 'succeeded', payload=payload)


async def test_missing_read_continues_same_delivery_with_explicit_negative_observation(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000, 'timeout_seconds': 60})
    harness = MissingHarness(context, verified, bundle)
    initial = json.loads(batch())
    initial['workspace_batch'].update(complete=False, continuation='Implement remaining files and verify.')
    repaired = json.loads(batch())
    repaired['workspace_batch']['files']['src/missing.py'] = 'def supporting_total(): return 12\n'
    model = Model(response(json.dumps(initial)), response(read_batch()), response(json.dumps(repaired)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 3
    prompt = '\n'.join(str(m.content) for m in model.requests[-1].messages)
    raw = prompt.split('<UNTRUSTED_WORKSPACE_READ_JSON>')[-1].split('</UNTRUSTED_WORKSPACE_READ_JSON>')[0]
    reads = json.loads(raw)
    assert reads[0]['path'] == 'src/app.py' and reads[0]['text']
    assert reads[1] == {'path': 'src/missing.py', 'status': 'not_found',
        'error_code': 'workspace.file_not_found', 'truncated': False}
    assert 'not empty' in prompt
    assert 'project.verify' in harness.calls and harness.calls[-1] == 'workspace.bundle'
    assert all(not r.tools and 0 < r.timeout_seconds <= context.timeout_seconds for r in model.requests)
    assert sum(e.kind is EventKind.MODEL_COMPLETED for e in events) == 3


async def test_repeated_missing_observation_cannot_buy_another_model_round(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    harness = MissingHarness(context, verified, bundle)
    model = Model(response(read_batch()), response(read_batch()), response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    with pytest.raises(RuntimeExecutionError, match='no progress'):
        _ = [e async for e in runtime.run(context)]
    assert len(model.requests) == 2
    assert 'workspace.write_text' not in harness.calls and 'workspace.bundle' not in harness.calls


async def test_fresh_missing_can_inform_repair_without_extending_productive_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.runtime import direct

    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000, 'timeout_seconds': 60})
    harness = MissingHarness(context, verified, bundle)
    missing = json.dumps({'workspace_batch': {'files': {}, 'read_paths': ['src/missing.py'],
        'complete': False, 'continuation': 'Correct this missing source assumption, then implement.'}})
    model = Model(response(missing), response(batch()))
    progress: list[int] = []
    limits: list[dict[str, Any]] = []
    original_deadline, original_limit = deadline_from_routing, direct._workspace_delivery_token_limit

    class ObservedDeadline:
        def __init__(self, target: Any) -> None:
            self.target = target

        @property
        def deadline(self) -> float:
            return cast(float, self.target.deadline)

        def observe(self, *, progress_units: int, now: float) -> Any:
            progress.append(progress_units)
            return self.target.observe(progress_units=progress_units, now=now)

    def deadline(*args: Any, **kwargs: Any) -> Any:
        return ObservedDeadline(original_deadline(*args, **kwargs))

    def limit(*args: Any, **kwargs: Any) -> int:
        limits.append(dict(kwargs))
        return original_limit(*args, **kwargs)

    monkeypatch.setattr(direct, 'deadline_from_routing', deadline)
    monkeypatch.setattr(direct, '_workspace_delivery_token_limit', limit)
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED and len(model.requests) == 2
    assert progress[0] == 0
    assert all(row['completed_batches'] > 0 or row['consumed_tokens'] == 0 for row in limits)


@pytest.mark.parametrize('defect', ['path', 'text', 'code', 'truncated', 'failed'])
async def test_invalid_or_denied_read_is_never_promoted_to_missing_observation(tmp_path: Path, defect: str) -> None:
    context, verified, bundle = await evidence(tmp_path)
    harness = MissingHarness(context, verified, bundle)
    harness.defect = defect
    model = Model(response(read_batch()), response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    with pytest.raises(RuntimeExecutionError, match='workspace read'):
        _ = [e async for e in runtime.run(context)]
    assert len(model.requests) == 1 and 'workspace.write_text' not in harness.calls
