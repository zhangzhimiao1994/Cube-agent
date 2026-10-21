"""Real capability/Harness/store integration; native executor responses are fixtures."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.skills.sandbox.base import SkillResult
from tests.unit.capabilities.test_generated_project_delivery import configured
from tests.unit.capabilities.test_scoped_read import project_path
from tests.unit.runtime.test_direct_generated_entry import Model, response
from tests.unit.runtime.test_direct_project_verification import Policy


def batch(files: Mapping[str, str]) -> str:
    return json.dumps({'workspace_batch': {'files': files, 'complete': True, 'continuation': ''}})


@pytest.mark.parametrize('protected_attempt', [False, True])
async def test_real_gateway_failed_verify_preserves_controls_and_allows_implementation_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protected_attempt: bool,
) -> None:
    backend, repository = await configured(tmp_path)
    verifier = backend._project_verification
    assert verifier is not None and verifier.executor is not None
    native = verifier.executor
    reports: list[Mapping[str, Any]] = []

    class FailOnce:
        async def run(self, invocation: Any) -> SkillResult:
            result = await native.run(invocation)
            raw = json.loads(result.stdout)
            if not reports:
                raw['status'] = 'failed'
                raw['checks'][-1].update(status='failed', exit_code=1,
                    stderr='ActualFailure: expected total 12, got 0\n')
                raw.pop('generated_test_execution', None)
            reports.append(raw)
            return SkillResult(exit_code=0,
                timed_out=False, stdout=json.dumps(raw), stderr='')

    verifier.executor = FailOnce()

    async def profile(*args: Any) -> Mapping[str, JsonValue]:
        return {'status': 'unavailable', 'reason': 'native_not_run_in_unit_test'}

    monkeypatch.setattr(backend, '_python_build_profile', profile)
    row = repository.row
    route = {**row.routing_decision, 'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle'}
    row.routing_decision = route
    context = TaskContext(tenant_id=row.tenant_id, run_id=row.id, actor_id=row.actor_id,
        actor_role=row.actor_role, mode=TaskMode.DIRECT, request=row.request, routing_decision=route,
        token_budget=50_000, timeout_seconds=60)
    policy = Policy(context, CapabilityStatus.ALLOWED)
    harness = HarnessToolGateway(backend, policy_gateway=policy, require_actor_identity=True,
        raise_backend_errors=True)
    root = cast(Path, backend._project_workspace_dir) / project_path()
    protected = {name: (root / name).read_bytes() for name in
        ('ACCEPTANCE.json', 'package.json', 'test/business.test.cjs')}
    texts = [batch({'src/index.js': 'exports.add=(a,b)=>0;\n'})]
    if protected_attempt:
        texts.append(batch({'package.json': '{"scripts":{"build":"echo skip","test":"echo skip"}}'}))
    texts.append(batch({'src/index.js': 'exports.add=(a,b)=>a+b;\n'}))
    model = Model(*(response(text) for text in texts))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=backend,
        harness_tool_gateway=harness)
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(reports) == 2 and reports[0]['status'] == 'failed' and reports[1]['status'] == 'passed'
    failures = [event.artifact for event in events if event.artifact is not None
        and event.artifact.content.get('tool_name') == 'project.verify'
        and event.artifact.content.get('status') == 'failed']
    assert len(failures) == 1
    failure = failures[0].content['result']
    assert isinstance(failure, Mapping) and 'verification_failure' in failure
    assert 'ActualFailure: expected total 12, got 0' in json.dumps(failure, default=dict)
    failure_index = next(index for index, event in enumerate(events) if event.artifact == failures[0])
    assert sum(event.kind is EventKind.MODEL_STARTED for event in events[:failure_index]) == 1
    assert any(event.kind is EventKind.MODEL_STARTED for event in events[failure_index + 1:])
    assert len(model.requests) == (3 if protected_attempt else 2)
    assert all((root / name).read_bytes() == value for name, value in protected.items())
    assert not (root / 'package-lock.json').exists()  # npm install recipe, not a required source lock.
    assert (root / 'DELIVERY_MANIFEST.json').is_file()
    assert row.routing_decision['generated_project_preparation']['phase'] == 'captured'
    repair_prompt = '\n'.join(cast(str, item.content) for item in model.requests[-1].messages)
    assert 'ActualFailure: expected total 12, got 0' in repair_prompt
    if protected_attempt:
        assert 'protected_control' in repair_prompt and 'implementation' in repair_prompt
    checkpoint = await runtime.save_checkpoint()
    layout = checkpoint.state['generated_event_layout']
    assert isinstance(layout, Mapping) and layout['verification_failure_event_count'] == 1
    fresh_model = Model()
    fresh = DirectRuntime(fresh_model, logical_model='fixture', capability_gateway=backend,
        harness_tool_gateway=harness)
    await fresh.restore_checkpoint(checkpoint)
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    replayed = [event async for event in fresh.run(context.model_copy(update={
        'checkpoint': checkpoint, 'artifacts': artifacts}))]
    assert len(replayed) == 1 and replayed[0].kind is EventKind.RUNTIME_COMPLETED
    assert not fresh_model.requests and len(reports) == 2
