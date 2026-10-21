from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.runtime.contracts import EventKind, JsonValue
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from tests.unit.runtime.test_direct_generated_entry import Harness, Model, response
from tests.unit.runtime.test_generated_draft_feedback import case
from tests.unit.runtime.test_generated_project_history import evidence


@pytest.mark.parametrize('defect', [None, 'same-input', 'scope', 'truncated', 'generic', 'mixed-passed'])
async def test_direct_corrects_draft_before_real_verification_or_fails_closed(
    tmp_path: Path, defect: str | None,
) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000, 'routing_decision': {
        **context.routing_decision, 'sandbox_profile': 'workspace_write'}})

    class DraftHarness(Harness):
        attempts = 0

        async def invoke(self, tenant: Any, request: Any, **kw: Any) -> HarnessToolCallResult:
            if request.tool_name != 'project.verify':
                return await super().invoke(tenant, request, **kw)
            self.calls.append(request.tool_name)
            self.attempts += 1
            if self.attempts > 1 and defect != 'same-input':
                return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                    status='succeeded', payload=verified)
            _, _, example = case()
            payload = dict(example.payload)
            rejection = dict(cast(Mapping[str, JsonValue], payload['generated_draft_rejection']))
            rejection.update(tenant_id=str(tenant), user_id=str(context.actor_id),
                run_id=str(context.run_id), project_id=context.routing_decision['project_id'],
                session_id=context.routing_decision['workspace_session_id'],
                request_sha256=hashlib.sha256(context.request.encode()).hexdigest())
            payload.update(generated_draft_rejection=rejection, call_id=request.call_id)
            if defect == 'scope':
                rejection['request_sha256'] = 'b' * 64
            if defect == 'mixed-passed':
                return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                    status='succeeded', payload={**verified, **payload})
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name, status='failed',
                failure_reason='generated_acceptance_unavailable_or_changed' if defect == 'generic'
                    else 'generated project draft requires correction',
                payload=payload, truncated=defect == 'truncated')

    def batch(version: int) -> str:
        return json.dumps({'workspace_batch': {'files': {'ACCEPTANCE.json': 'draft-' + str(version),
            'src/app.py': 'def total(): return 12\n'}, 'complete': True, 'continuation': ''}})

    harness = DraftHarness(context, verified, bundle)
    model = Model(response(batch(1)), response(batch(2)), response(batch(3)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events = []
    if defect is None:
        events = [event async for event in runtime.run(context)]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(model.requests) == 2 and harness.attempts == 2
        assert harness.calls[-1] == 'workspace.bundle'
        prompt = '\n'.join(cast(str, m.content) for m in model.requests[1].messages)
        assert 'protected_editable_paths' in prompt and 'before_capture' in prompt
        draft_artifacts = [e.artifact for e in events if e.artifact is not None
            and e.artifact.content.get('status') == 'failed']
        assert len(draft_artifacts) == 1
        assert 'verification' not in cast(Mapping[str, JsonValue], draft_artifacts[0].content['result'])
    else:
        with pytest.raises(RuntimeExecutionError):
            _ = [event async for event in runtime.run(context)]
        assert 'workspace.bundle' not in harness.calls
        assert len(model.requests) == (2 if defect == 'same-input' else 1)
