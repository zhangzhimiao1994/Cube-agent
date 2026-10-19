from __future__ import annotations

from typing import Any

import pytest

from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.runs.service import VibeCodingUnavailable
from tests.api.test_runs_api import StubRunService, _client, bearer

SELECTION = {'schema_version': 1, 'kind': 'managed_workspace', 'editable_paths': ['src']}


def test_ordinary_user_selection_reaches_submission_as_validated_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service, principal = _client()
    original = StubRunService.submit
    received: list[object] = []

    async def submit(self: StubRunService, *, project_input: object = None, **kwargs: Any) -> Any:
        received.append(project_input)
        assert kwargs['tenant_id'] == principal.tenant_id
        assert kwargs['actor_id'] == principal.user_id
        return await original(self, **kwargs)

    monkeypatch.setattr(StubRunService, 'submit', submit)
    response = client.post('/api/v1/runs', headers=bearer(), json={
        'message': 'Repair the selected application.', 'mode': 'direct',
        'project_id': 'existing', 'workspace_session_id': 'session-a',
        'project_input': SELECTION,
    })
    assert response.status_code == 202
    assert service.submitted
    assert len(received) == 1
    assert type(received[0]) is ManagedProjectInput
    assert received[0].to_payload() == SELECTION


def test_queued_selection_uses_the_same_domain_and_does_not_disappear() -> None:
    client, service, _ = _client()
    response = client.post('/api/v1/admin/conversations/conv-original/queue',
        headers={**bearer(), 'Idempotency-Key': 'original-input-selection'}, json={
            'message': 'Repair selected project after current run.', 'mode': 'direct',
            'project_input': SELECTION,
        })
    assert response.status_code == 202
    selected = service.queued_submit_options[-1].get('project_input')
    assert type(selected) is ManagedProjectInput
    assert selected.to_payload() == SELECTION


def test_queued_unavailable_original_preparation_is_a_clear_conflict_not_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = _client()

    async def unavailable(*args: object, **kwargs: object) -> None:
        raise VibeCodingUnavailable('original project preparation is unavailable')

    monkeypatch.setattr(StubRunService, 'queue_message', unavailable)
    response = client.post('/api/v1/admin/conversations/conv-original/queue',
        headers={**bearer(), 'Idempotency-Key': 'original-input-unavailable'}, json={
            'message': 'Repair selected project.', 'mode': 'direct', 'project_input': SELECTION,
        })
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'vibe_coding_unavailable'
