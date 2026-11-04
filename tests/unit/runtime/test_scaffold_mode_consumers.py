"""Schema-4 missing paths reach actual adapter prompts as data, not authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.generated_project_draft import GeneratedDraftRejection
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent, TaskContext
from agent_hub.runtime.crew.adapter import RuntimeExecutionError
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.generated_draft_feedback import generated_draft_feedback
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.self_repair_context import project_repair_failure_text
from tests.unit.capabilities.test_python_lock_declaration_mismatch import (
    MISMATCH_HINT,
    PYTHON_HINT,
    TOML_HINT,
)
from tests.unit.runtime import test_generated_draft_autogen as discuss
from tests.unit.runtime import test_generated_draft_crew as crew
from tests.unit.runtime.crew.test_adapter_failure_reason import WorkspaceDeliverySequenceGateway
from tests.unit.runtime.test_direct_generated_entry import Model, response
from tests.unit.runtime.test_generated_draft_feedback import case
from tests.unit.runtime.test_generated_project_history import evidence
from tests.unit.runtime.test_resolution_pin_mode_consumers import PinDirectHarness, UnusedChild

MISSING = ('requirements.lock', 'tests/test_flask_app.py')
GUIDANCE = {'python_lock_declaration_mismatch': MISMATCH_HINT,
            'python_requires_python_unsupported': PYTHON_HINT, 'python_pyproject_toml_invalid': TOML_HINT}
DIAGNOSES = ('scaffold', *GUIDANCE)


def scaffold_feedback(ctx: TaskContext, request: HarnessToolCallRequest,
                      diagnosis: str = 'scaffold') -> HarnessToolCallResult:
    project, session = (ctx.routing_decision[key] for key in ('project_id', 'workspace_session_id'))
    assert isinstance(project, str) and isinstance(session, str)
    draft = GeneratedDraftRejection(
        tenant_id=str(ctx.tenant_id), user_id=str(ctx.actor_id), run_id=str(ctx.run_id),
        project_id=project, session_id=session, request_sha256=hashlib.sha256(ctx.request.encode()).hexdigest(),
        proposer_actor=request.actor, observation_sha256='a' * 64,
        reason='generated_acceptance_scaffold_incomplete' if diagnosis == 'scaffold'
            else 'generated_acceptance_python_declaration_invalid',
        issues=('scaffold_incomplete',) if diagnosis == 'scaffold' else (diagnosis, GUIDANCE[diagnosis]),
        schema_version=4 if diagnosis == 'scaffold' else 1,
        missing_files=MISSING if diagnosis == 'scaffold' else None,
    )
    raw = draft.to_payload()
    payload: dict[str, JsonValue] = json.loads(json.dumps(raw))
    return HarnessToolCallResult(
        call_id=request.call_id, tool_name=request.tool_name, status='failed',
        failure_reason='generated project draft requires correction', payload={
            'generated_draft_rejection': payload, 'call_id': request.call_id,
            'arguments_sha256': hashlib.sha256(b'{}').hexdigest(),
        },
    )


def assert_next_prompt(request: ModelRequest, diagnosis: str) -> None:
    text = '\n'.join(message.content for message in request.messages if isinstance(message.content, str))
    text = text.replace('\\u003c', '<').replace('\\u003e', '>')
    required = ('missing_files', *MISSING, 'scaffold_incomplete') if diagnosis == 'scaffold' else (
        diagnosis, GUIDANCE[diagnosis])
    for value in (*required, 'before_capture', 'failed', 'untrusted'):
        assert value in text, value
    assert not any('missing_files' in message.content for message in request.messages
                   if message.role == 'system' and isinstance(message.content, str))
    assert 'secret://' not in text and 'approval_id' not in text


class ScaffoldDirectHarness(PinDirectHarness):
    diagnosis = 'scaffold'

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        result = await super().invoke(tenant_id, request, user_id=user_id, role=role)
        return scaffold_feedback(self.ctx, request, self.diagnosis) if 'generated_draft_rejection' in result.payload else result


@pytest.mark.parametrize('diagnosis', DIAGNOSES)
async def test_direct_corrects_scaffold_with_missing_paths_on_next_request(tmp_path: Path, diagnosis: str) -> None:
    ctx, verified, bundle = await evidence(tmp_path)
    ctx = ctx.model_copy(update={'token_budget': 50_000, 'routing_decision': {
        **ctx.routing_decision, 'sandbox_profile': 'workspace_write',
    }})
    route = dict(ctx.routing_decision)
    harness = ScaffoldDirectHarness(ctx, verified, bundle)
    harness.diagnosis = diagnosis

    def batch(version: int) -> str:
        return json.dumps({'workspace_batch': {'files': {
            'ACCEPTANCE.json': f'draft-{version}', 'src/app.py': 'def total(): return 12\n',
        }, 'complete': True, 'continuation': ''}})

    model = Model(response(batch(1)), response(batch(2)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness, harness_tool_gateway=harness)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 2 and harness.attempts == 2
    assert_next_prompt(model.requests[1], diagnosis)
    assert harness.calls.count('project.inspect') == 1 and harness.calls[-1] == 'workspace.bundle'
    assert ctx.routing_decision == route and ctx.token_budget == 50_000


class ScaffoldCrewHarness(crew.DraftHarness):
    diagnosis = 'scaffold'

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        result = await super().invoke(tenant_id, request, user_id=user_id, role=role)
        return scaffold_feedback(self.context, request, self.diagnosis) if 'generated_draft_rejection' in result.payload else result


@pytest.mark.parametrize('mode', [TaskMode.DISPATCH, TaskMode.HYBRID])
@pytest.mark.parametrize('diagnosis', DIAGNOSES)
async def test_crew_missing_paths_keep_denial_and_original_scope(mode: TaskMode, diagnosis: str) -> None:
    ctx = crew.context().model_copy(update={'mode': mode})
    route = dict(ctx.routing_decision)
    harness = ScaffoldCrewHarness(ctx)
    harness.diagnosis = diagnosis
    model = WorkspaceDeliverySequenceGateway((crew.verify(), crew.correction(), crew.verify('fresh')))
    dispatch = crew.runtime(model, harness)
    events: list[RunEvent] = []
    if mode is TaskMode.HYBRID:
        runtime = HybridRuntime(dispatch, UnusedChild(TaskMode.DISCUSS), UnusedChild(TaskMode.DIRECT))
        events = [event async for event in runtime.run(ctx)]
        assert events[-1].kind is EventKind.RUNTIME_FAILED
    else:
        with pytest.raises(RuntimeExecutionError, match='capability execution failed'):
            async for event in dispatch.run(ctx):
                events.append(event)
    assert len(model.requests) == 3
    assert_next_prompt(model.requests[1], diagnosis)
    assert [call.tool_name for call in harness.calls] == ['project.verify', 'workspace.write_text', 'project.verify']
    assert harness.calls[0].call_id != harness.calls[2].call_id
    assert all(call.run_id == ctx.run_id and call.sandbox == 'workspace_write' for call in harness.calls)
    assert not any(event.kind in {EventKind.STEP_COMPLETED, EventKind.RUNTIME_COMPLETED} for event in events)
    assert ctx.routing_decision == route and ctx.token_budget == 10_000


class ScaffoldDiscussGateway(discuss.DraftGateway):
    diagnosis = 'scaffold'

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        result = await super().invoke(tenant_id, request, user_id=user_id, role=role)
        if 'generated_draft_rejection' in result.payload:
            result = replace(result, payload=scaffold_feedback(self.context, request, self.diagnosis).payload)
            self.rows[-1] = result
        return result


@pytest.mark.parametrize('diagnosis', DIAGNOSES)
async def test_discuss_checkpoint_preserves_missing_paths_without_replaying_verify(
    monkeypatch: pytest.MonkeyPatch, diagnosis: str,
) -> None:
    ctx = discuss.context()
    route = dict(ctx.routing_decision)
    gateway = ScaffoldDiscussGateway(ctx)
    gateway.diagnosis = diagnosis
    previous, checkpoint = await discuss.paused_checkpoint(monkeypatch, ctx, discuss.DraftModel(), gateway)
    assert len(gateway.calls) == 1
    model = discuss.DraftModel(stage=1)
    runtime = discuss.runtime(model, gateway)
    runtime._repository = previous._repository
    await runtime.restore_checkpoint(checkpoint)
    events = await discuss.collect(runtime, ctx.model_copy(update={'checkpoint': checkpoint}))
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert_next_prompt(next(request for request in model.requests if request.logical_model != 'selector'), diagnosis)
    assert [call.tool_name for call in gateway.calls] == ['project.verify', 'workspace.write_text', 'project.verify']
    assert gateway.calls[0].call_id != gateway.calls[2].call_id
    assert not any(event.kind is EventKind.TOOL_COMPLETED and event.tool_call_id == 'draft-0' for event in events)
    assert model.feedback
    for feedback in model.feedback:
        draft = feedback['generated_draft_rejection']
        assert draft['execution_started'] is False
        if diagnosis == 'scaffold':
            assert draft['schema_version'] == 4 and draft['missing_files'] == list(MISSING)
        else:
            assert draft['schema_version'] == 1 and draft['issues'] == [diagnosis, GUIDANCE[diagnosis]]
    assert ctx.routing_decision == route and ctx.token_budget == 500


@pytest.mark.parametrize('diagnosis', DIAGNOSES)
def test_repair_context_keeps_validated_paths_without_freeform_redaction_or_authority(diagnosis: str) -> None:
    ctx, request, _ = case()
    result = scaffold_feedback(ctx, request, diagnosis)
    feedback = generated_draft_feedback(result, context=ctx, request=request)
    assert feedback is not None
    artifact = Artifact(id=uuid4(), type='tool_result', producer=request.actor, content={
        'tool_name': request.tool_name, 'result': feedback,
    })
    text = project_repair_failure_text(artifact, expected_artifact_id=str(artifact.id),
                                       expected_artifact_sha256=artifact.content_sha256, validated_feedback=feedback)
    body = json.loads(text.split('=', 1)[1])
    draft = body['diagnostic']['generated_draft_rejection']
    if diagnosis == 'scaffold':
        assert draft['schema_version'] == 4 and draft['missing_files'] == list(MISSING)
    else:
        assert draft['issues'] == [diagnosis, GUIDANCE[diagnosis]]
    assert draft['execution_started'] is False and body['untrusted'] is True and body['status'] == 'failed'
    assert str(ctx.actor_id) not in text and 'approval_id' not in text
    assert 'claims' not in text and 'dependency_pins' not in text


@pytest.mark.parametrize('field', ['tenant_id', 'user_id', 'run_id', 'project_id', 'session_id',
                                   'request_sha256', 'proposer_actor', 'observation_sha256', 'phase',
                                   'execution_started'])
def test_schema4_feedback_rejects_cross_scope_or_invalid_observation(field: str) -> None:
    ctx, request, _ = case()
    result = scaffold_feedback(ctx, request)
    raw = result.to_payload()['payload']
    assert isinstance(raw, dict)
    draft = raw['generated_draft_rejection']
    assert isinstance(draft, dict)
    draft[field] = True if field == 'execution_started' else 'forged'
    tampered = replace(result, payload=raw)
    assert generated_draft_feedback(tampered, context=ctx, request=request) is None
