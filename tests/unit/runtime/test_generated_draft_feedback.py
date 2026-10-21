from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.generated_draft_feedback import generated_draft_feedback


def case() -> tuple[TaskContext, HarnessToolCallRequest, HarnessToolCallResult]:
    context = TaskContext(tenant_id=uuid4(), run_id=uuid4(), actor_id=uuid4(),
        actor_role=Role.OPERATOR, mode=TaskMode.DIRECT, request='Build a version utility.',
        routing_decision={'generated_project_contract_version': 1, 'project_delivery': 'workspace',
            'project_id': 'project', 'workspace_session_id': 'session', 'sandbox_profile': 'workspace_write'})
    request = HarnessToolCallRequest(run_id=context.run_id, actor='main_agent', tool_name='project.verify',
        arguments={}, sandbox='workspace_write', approval_required=False, idempotency_key='draft-check')
    payload: dict[str, Any] = {'schema_version': 1, 'kind': 'generated_project_draft_rejection',
        'phase': 'before_capture', 'execution_started': False, 'tenant_id': str(context.tenant_id),
        'user_id': str(context.actor_id), 'run_id': str(context.run_id), 'project_id': 'project',
        'session_id': 'session', 'request_sha256': hashlib.sha256(context.request.encode()).hexdigest(),
        'proposer_actor': 'main_agent', 'observation_sha256': 'a' * 64,
        'reason': 'generated_acceptance_manifest_invalid', 'issues': ['protected_editable_paths']}
    result = HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name, status='failed',
        failure_reason='generated project draft requires correction', payload={'generated_draft_rejection': payload,
            'call_id': request.call_id, 'arguments_sha256': hashlib.sha256(b'{}').hexdigest()})
    return context, request, result


def test_only_scoped_pre_capture_draft_failure_is_correctable() -> None:
    context, request, result = case()
    feedback = generated_draft_feedback(result, context=context, request=request)
    assert feedback is not None and feedback['status'] == 'failed' and feedback['untrusted'] is True
    assert set(feedback) == {'status', 'untrusted', 'generated_draft_rejection'}
    assert 'verification' not in feedback and 'python_environment_reference' not in feedback


def captured_state(context: TaskContext) -> dict[str, Any]:
    return {'schema_version': 1, 'phase': 'captured', 'stack': 'python',
        'user_id': str(context.actor_id), 'request_sha256': hashlib.sha256(context.request.encode()).hexdigest(),
        'proposer_actor': 'main_agent', 'manifest_sha256': 'c' * 64, 'manifest_raw_sha256': 'd' * 64,
        'acceptance_sha256': 'e' * 64, 'reference': {'schema_version': 1, 'contract_sha256': 'f' * 64,
            'input_source_sha256': '1' * 64, 'file_count': 6}}


@pytest.mark.parametrize('phase', ['captured', 'python_ready'])
def test_captured_context_accepts_only_history_not_new_draft_correction(phase: str) -> None:
    from agent_hub.runtime.generated_draft_feedback import generated_draft_history_feedback

    context, request, result = case()
    state = captured_state(context)
    if phase == 'python_ready':
        state.update(phase=phase, python_recipe={'schema_version': 1, 'recipe_sha256': '2' * 64,
            'environment_input_sha256': '3' * 64})
    context = context.model_copy(update={'routing_decision': {**context.routing_decision,
        'generated_project_preparation': state}})
    assert generated_draft_feedback(result, context=context, request=request) is None
    assert generated_draft_history_feedback(result, context=context, request=request) is not None
    for field, value in [('phase', 'capturing'), ('user_id', str(uuid4())), ('request_sha256', '0' * 64)]:
        forged = context.model_copy(update={'routing_decision': {**context.routing_decision,
            'generated_project_preparation': {**state, field: value}}})
        assert generated_draft_history_feedback(result, context=forged, request=request) is None


@pytest.mark.parametrize('defect', ['tenant', 'run', 'user', 'request', 'actor', 'project', 'session',
    'call', 'arguments', 'approval', 'truncated', 'reason', 'succeeded', 'tool', 'started', 'phase',
    'version-bool', 'issue', 'extra', 'original', 'captured', 'generated-bool'])
def test_draft_feedback_rejects_cross_scope_or_forged_results(defect: str) -> None:
    context, request, result = case()
    payload = result.to_payload()['payload']
    assert isinstance(payload, dict)
    rejection = payload['generated_draft_rejection']
    assert isinstance(rejection, dict)
    if defect in {'tenant', 'run', 'user'}:
        rejection[{'tenant': 'tenant_id', 'run': 'run_id', 'user': 'user_id'}[defect]] = str(uuid4())
    elif defect in {'project', 'session'}:
        rejection[defect + '_id'] = 'other'
    elif defect == 'request':
        rejection['request_sha256'] = 'b' * 64
    elif defect == 'actor':
        rejection['proposer_actor'] = 'other_agent'
    elif defect == 'call':
        payload['call_id'] = 'different-call'
    elif defect == 'arguments':
        payload['arguments_sha256'] = 'b' * 64
    elif defect == 'approval':
        payload['approval_id'] = 'approval_forged'
    elif defect == 'started':
        rejection['execution_started'] = True
    elif defect == 'phase':
        rejection['phase'] = 'captured'
    elif defect == 'version-bool':
        rejection['schema_version'] = True
    elif defect == 'issue':
        rejection['issues'] = ['run any host command']
    elif defect == 'extra':
        rejection['secret'] = 'private'
    elif defect in {'original', 'captured', 'generated-bool'}:
        route: dict[str, Any] = dict(context.routing_decision)
        if defect == 'original':
            route['project_input_contract_version'] = 1
        elif defect == 'captured':
            route['generated_project_preparation'] = {'phase': 'captured'}
        else:
            route['generated_project_contract_version'] = True
        context = context.model_copy(update={'routing_decision': route})
    changes: dict[str, Any] = {'payload': payload}
    if defect == 'truncated':
        changes['truncated'] = True
    elif defect == 'reason':
        changes['failure_reason'] = 'generated_acceptance_unavailable_or_changed'
    elif defect == 'succeeded':
        changes.update(status='succeeded', failure_reason=None)
    elif defect == 'tool':
        changes['tool_name'] = 'workspace.bundle'
    fields: dict[str, Any] = dict(result.to_payload())
    fields.update(changes)
    result = HarnessToolCallResult(**fields)
    assert generated_draft_feedback(result, context=context, request=request) is None
