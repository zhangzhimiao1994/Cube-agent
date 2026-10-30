import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent_hub.api.routers import runs
from agent_hub.auth.models import AuthenticatedPrincipal, Role


@pytest.mark.parametrize('queued', [False, True])
async def test_api_captures_real_principal_and_validated_selection(monkeypatch, queued):
    user = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    body = runs.CreateRunRequest.model_validate({
        'message': '  exact original 文本  ', 'mode': 'direct',
        'project_id': 'project-a', 'workspace_session_id': 'session-a',
        'permission_mode': 'read_only', 'direct_model': 'model-a',
        'authenticated_input': {'actor_role': 'super_admin'}, 'origin': 'human',
    })
    received = []
    class Stop(Exception):
        pass
    async def capture(**kwargs):
        evidence = kwargs.get('authenticated_input')
        assert evidence is not None, 'authenticated API must mint private evidence'
        received.append(evidence)
        raise Stop
    service = SimpleNamespace(submit=capture, queue_message=capture)
    monkeypatch.setattr(runs, '_stop_conversation_preview', AsyncMock())
    monkeypatch.setattr(runs, '_resolve_permission_selection', AsyncMock(return_value=body))
    monkeypatch.setattr(runs, '_default_execution_backend', AsyncMock(return_value='systemd'))
    with pytest.raises(Stop):
        if queued:
            await runs.queue_conversation_message('conv-proof', body, SimpleNamespace(),
                                                  service, user, 'proof')
        else:
            await runs.create_run(body, SimpleNamespace(), service, user, 'proof')
    evidence = received[0]
    assert (evidence.tenant_id, evidence.actor_id, evidence.actor_role) == (
        user.tenant_id, user.user_id, user.role)
    assert evidence.message == '  exact original 文本  '
    scope = json.loads(evidence.input_scope)
    assert scope['selection']['project_id'] == 'project-a'
    assert scope['selection']['permission_mode'] == 'read_only'
    assert scope['selection']['direct_model'] == 'model-a'
    assert scope['conversation_id'] == ('conv-proof' if queued else None)
    assert 'authenticated_input' not in scope['selection']
    assert 'authenticated_input' not in runs.CreateRunRequest.model_fields
    assert 'authenticated_input' not in runs.SubmittedRunResponse.model_fields


async def test_edit_mints_new_input_bound_to_actual_principal():
    user = AuthenticatedPrincipal(uuid4(), uuid4(), Role.ADMIN)
    item_id = uuid4()
    received = []
    class Stop(Exception):
        pass
    async def edit(*args, **kwargs):
        received.append(kwargs.get('authenticated_input'))
        raise Stop
    service = SimpleNamespace(edit_queued_message=edit,
        queued_authenticated_input=AsyncMock(return_value=None))
    with pytest.raises(Stop):
        await runs.edit_conversation_queue_item(item_id,
            runs.QueueItemEditRequest(version=3, message='  edited 文本  '),
            SimpleNamespace(), service, user)
    evidence = received[0]
    assert evidence is not None
    assert evidence.actor_id == user.user_id
    assert evidence.actor_role == Role.ADMIN
    assert evidence.message == '  edited 文本  '
    assert json.loads(evidence.input_scope)['queue_item_id'] == str(item_id)


@pytest.mark.parametrize('queued', [False, True])
def test_http_payload_cannot_supply_provenance_and_private_fields_are_not_returned(monkeypatch, queued):
    from tests.api.test_runs_api import StubRunService, _client, bearer
    client, _service, user = _client()
    received = []
    method = 'queue_message' if queued else 'submit'
    original = getattr(StubRunService, method)
    async def capture(self, *args, **kwargs):
        received.append(kwargs.pop('authenticated_input'))
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(StubRunService, method, capture)
    response = client.post('/api/v1/admin/conversations/conv-http/queue' if queued else '/api/v1/runs',
        headers={**bearer(), 'Idempotency-Key': 'private-http'}, json={
            'message': 'original private message', 'mode': 'direct',
            'authenticated_input': {'actor_role': 'super_admin', 'message_sha256': '0' * 64},
            'origin': 'human', 'actor_id': str(uuid4()), 'actor_role': 'super_admin'})
    assert response.status_code == 202
    evidence = received[0]
    assert (evidence.tenant_id, evidence.actor_id, evidence.actor_role) == (
        user.tenant_id, user.user_id, user.role)
    assert evidence.message == 'original private message'
    for field in ('authenticated_input', 'input_scope', 'scope_sha256', 'message_sha256', 'turn_id'):
        assert field not in response.json()


async def test_edit_retains_only_the_genuine_persisted_submission_selection():
    from agent_hub.runs.authorization import _capture_authenticated_input
    user = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    original = _capture_authenticated_input(user, 'original', {
        'selection': {'project_id': 'project-a', 'workspace_session_id': 'session-a',
                      'requested_permissions': ['workspace.read'], 'mode': 'direct'},
        'resolved_selection': {'execution_backend': 'systemd'},
        'conversation_id': 'conv-proof'})
    received = []
    class Stop(Exception):
        pass
    async def edit(*args, **kwargs):
        received.append(kwargs['authenticated_input'])
        raise Stop
    service = SimpleNamespace(edit_queued_message=edit,
        queued_authenticated_input=AsyncMock(return_value=original))
    with pytest.raises(Stop):
        await runs.edit_conversation_queue_item(uuid4(),
            runs.QueueItemEditRequest(version=1, message='new'), SimpleNamespace(), service, user)
    scope = json.loads(received[0].input_scope)
    assert scope['selection']['project_id'] == 'project-a'
    assert scope['selection']['requested_permissions'] == ['workspace.read']
    assert scope['resolved_selection']['execution_backend'] == 'systemd'
    assert scope['unchanged_selection_from_turn'] == str(original.turn_id)
    assert scope['conversation_id'] == 'conv-proof'
