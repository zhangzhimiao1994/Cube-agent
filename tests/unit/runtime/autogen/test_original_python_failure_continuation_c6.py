"""Real original-Python gateway failure fixture and fail-closed evidence checks."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from autogen_core import CancellationToken

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runs.project_inputs import python_original_context_metadata
from agent_hub.runs.python_preparation import validate_python_preparation
from agent_hub.runtime.autogen.adapter import (
    GatewayCapabilityTool,
    ToolOutcomeUncertain,
    _DiscussionDurability,
    _ExecutionObservationError,
    _recoverable_tool_failure,
)
from agent_hub.runtime.autogen.engineering_continuation import (
    engineering_continuation_selected,
    writer_can_continue,
)
from agent_hub.runtime.autogen.engineering_delivery import engineering_progress
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent
from agent_hub.runtime.generated_project_history import generated_history_selected
from agent_hub.runtime.python_project_history import python_failure_current
from tests.unit.capabilities.test_project_verification import Executor
from tests.unit.harness.test_tool_gateway import FakePolicyGateway
from tests.unit.runtime.test_python_project_history import history_case

TOOLS = {'engineer': ('project.inspect', 'project.verify', 'workspace.write_text', 'workspace.bundle')}


async def actual_chain(tmp_path: Path) -> tuple[Any, ...]:
    verifier, repository, executor, original, context = await history_case(tmp_path)
    route = dict(context.routing_decision)
    route.update(engineering_action_version=1,
                 engineering_writer_continuation_version=1)
    baseline = context.artifacts[0]
    metadata = Artifact(id=uuid4(), type='original_python_project_context',
        producer='original_project_preparer', source_ids=(str(baseline.id),),
        content=python_original_context_metadata(original))
    context = context.model_copy(update={'routing_decision': route,
        'artifacts': (*context.artifacts, metadata), 'execution_token': uuid4(),
        'actor_id': repository.record.actor_id, 'actor_role': Role.OPERATOR}).validated_internal_clone()
    validate_python_preparation(context)
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path/'skills',
        project_workspace_dir=verifier.root, run_repository=repository,
        project_executor=Executor())
    backend._project_verification = verifier
    gateway = HarnessToolGateway(backend,
        policy_gateway=FakePolicyGateway(CapabilityStatus.ALLOWED), raise_backend_errors=True)
    stored: list[Path] = []
    checkpoints: list[Any] = []
    finalized: list[Any] = []

    async def publish(artifacts: Any, models: Any, tools: Any) -> None:
        checkpoints.append(json.loads(json.dumps(tools, default=dict)))

    async def store(artifact: Artifact) -> Any:
        p = tmp_path / (str(artifact.id)+'.artifact.json')
        p.write_text(json.dumps(artifact.to_payload()), encoding='utf-8')
        stored.append(p)
        return uuid4()

    async def abort(write_id: Any) -> bool:
        pytest.fail('known completed observation must not abort')

    durable = _DiscussionDurability(publish, store, abort, finalized.append,
        run_id=context.run_id, tenant_id=context.tenant_id)
    records: list[Any] = []

    def tool(name: str) -> GatewayCapabilityTool:
        return GatewayCapabilityTool(backend, tenant_id=context.tenant_id,
            run_id=context.run_id, actor='engineer', name=name, records=records,
            durability=durable, harness_tool_gateway=gateway,
            user_id=repository.record.actor_id, role=Role.OPERATOR,
            server_context=context, sandbox_profile='workspace_write', serialize_tool_calls=True)

    await tool('project.inspect').run_json({}, CancellationToken(), call_id='local-inspection')
    calls_before = len(executor.calls)
    with pytest.raises(_ExecutionObservationError):
        await tool('project.verify').run_json({}, CancellationToken(), call_id='local-failed-check')
    assert len(executor.calls) == calls_before + 1
    assert len(stored) == len(finalized) == 2 and checkpoints
    failure = durable.artifacts[-1]
    persisted = json.loads(stored[-1].read_bytes())
    assert persisted['content_sha256'] == failure.content_sha256
    assert persisted['content']['result']['execution_diagnostic']['exit_code'] != 0
    assert 'python_verification_failure' in persisted['content']['result']
    assert durable.tool_entries[-1]['status'] == 'failed'
    assert durable.tool_entries[-1]['artifact_id'] == str(failure.id)
    assert records[-1].kind is EventKind.TOOL_FAILED
    assert records[-1].reason == 'python_project_checks_failed'
    event = RunEvent(kind=records[-1].kind, sequence=74, run_id=context.run_id,
        actor='engineer', tool_name='project.verify', tool_call_id=records[-1].call_id, payload=records[-1].payload,
        reason=records[-1].reason)
    assert _recoverable_tool_failure(event, context=context, artifacts=durable.artifacts)
    return context, durable, failure, executor, tool


async def test_original_python_failure_is_current_after_real_gateway(tmp_path: Path) -> None:
    context, durable, failure, _executor, _tool = await actual_chain(tmp_path)
    assert not generated_history_selected(context), 'original and generated identities must not be mixed'
    assert python_failure_current(context, failure.content['result']), 'current failure binding was lost before scheduler'
    progress = engineering_progress(context, tool_entries=durable.tool_entries,
        artifacts=durable.artifacts, allowed_tools=TOOLS)
    assert progress.inspected and not progress.uncertain and progress.verification_failed_current
    assert not engineering_continuation_selected(context), 'current selector is generated-only'
    # Writer scheduling is outside the checkpoint-context fix. Its independent
    # RED and original source are retained under .tmp, not pytest discovery.


@pytest.mark.parametrize('defect', ['artifact_hash', 'scope', 'environment', 'UNKNOWN'])
async def test_drift_or_unknown_cannot_unlock_writer(tmp_path: Path, defect: str) -> None:
    context, durable, failure, executor, tool = await actual_chain(tmp_path)
    artifacts = list(durable.artifacts)
    rows = [dict(r) for r in durable.tool_entries]
    if defect == 'artifact_hash':
        rows[-1]['artifact_sha256'] = '0'*64
    elif defect == 'scope':
        route = json.loads(json.dumps(context.routing_decision, default=dict))
        route['python_project_history']['scope']['run_id'] = str(uuid4())
        context = context.model_copy(update={'routing_decision': route}).validated_internal_clone()
    elif defect == 'environment':
        content = json.loads(json.dumps(failure.content, default=dict))
        content['result']['python_environment_reference']['environment_sha256'] = '0'*64
        forged = Artifact(id=failure.id, type=failure.type, producer=failure.producer, content=content)
        artifacts[-1] = forged
        rows[-1]['artifact_sha256'] = forged.content_sha256
    else:
        rows[-1]['status'] = 'running'
    progress = engineering_progress(context, tool_entries=rows, artifacts=artifacts, allowed_tools=TOOLS)
    assert progress.uncertain, defect
    assert not writer_can_continue(context, 'engineer', rows, artifacts, TOOLS)
    if defect == 'UNKNOWN':
        durable.tool_entries[-1]['status'] = 'running'
        durable._tool_cursor = len(durable.tool_entries)-1
        before = len(executor.calls)
        with pytest.raises(ToolOutcomeUncertain):
            await tool('project.verify').run_json({}, CancellationToken(), call_id='local-unknown-no-replay')
        assert len(executor.calls) == before
