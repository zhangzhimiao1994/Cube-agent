"""Strict pre-capture feedback binding; no execution receipt or write authority."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from agent_hub.capabilities.generated_project_draft import validated_generated_draft_payload
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.contracts import JsonValue, TaskContext

REJECTION_REASON = 'generated project draft requires correction'


def generated_draft_feedback(
    result: HarnessToolCallResult, *, context: TaskContext, request: HarnessToolCallRequest,
) -> Mapping[str, JsonValue] | None:
    return _feedback(result, context=context, request=request, historical=False)


def generated_draft_history_feedback(
    result: HarnessToolCallResult, *, context: TaskContext, request: HarnessToolCallRequest,
) -> Mapping[str, JsonValue] | None:
    """Validate retained failed data, not permission to correct or replay a call."""
    return _feedback(result, context=context, request=request, historical=True)


def _feedback(
    result: HarnessToolCallResult, *, context: TaskContext, request: HarnessToolCallRequest, historical: bool,
) -> Mapping[str, JsonValue] | None:
    route = context.routing_decision
    if (type(result) is not HarnessToolCallResult or type(request) is not HarnessToolCallRequest
            or result.truncated or result.status != 'failed' or result.failure_reason != REJECTION_REASON
            or request.tool_name != 'project.verify' or result.tool_name != request.tool_name
            or result.call_id != request.call_id or request.arguments or request.run_id != context.run_id
            or request.sandbox != 'workspace_write' or context.actor_id is None or context.actor_role is None
            or type(route.get('generated_project_contract_version')) is not int
            or route.get('generated_project_contract_version') != 1
            or {'project_input', 'project_input_contract_version', 'project_input_preparation',
                'python_project_history'} & route.keys()):
        return None
    if 'generated_project_preparation' in route:
        if not historical:
            return None
        from agent_hub.capabilities.generated_project_service import validate_generated_preparation

        try:
            state = validate_generated_preparation(route['generated_project_preparation'])
        except (RuntimeError, ValueError, TypeError, KeyError, AttributeError):
            return None
        if (state['phase'] not in {'captured', 'python_ready'} or state['user_id'] != str(context.actor_id)
                or state['request_sha256'] != hashlib.sha256(context.request.encode('utf-8')).hexdigest()):
            return None
    payload = result.payload
    if (set(payload) != {'generated_draft_rejection', 'call_id', 'arguments_sha256'}
            or payload['call_id'] != request.call_id
            or payload['arguments_sha256'] != hashlib.sha256(b'{}').hexdigest()):
        return None
    rejection = validated_generated_draft_payload(payload['generated_draft_rejection'])
    if rejection is None:
        return None
    expected = {'tenant_id': str(context.tenant_id), 'run_id': str(context.run_id),
        'user_id': str(context.actor_id), 'proposer_actor': request.actor,
        'project_id': route.get('project_id'), 'session_id': route.get('workspace_session_id'),
        'request_sha256': hashlib.sha256(context.request.encode('utf-8')).hexdigest()}
    if any(type(value) is not str or not value or rejection[key] != value for key, value in expected.items()):
        return None
    return {'status': 'failed', 'untrusted': True, 'generated_draft_rejection': rejection}
