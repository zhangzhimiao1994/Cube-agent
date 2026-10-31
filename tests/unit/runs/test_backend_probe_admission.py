"""Ordinary submit API + real RunService admission, with only host discovery controlled."""
from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest

from agent_hub import execution_backends as backends
from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.conversations import ConversationRecord
from agent_hub.runs.service import RunService
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from tests.api.test_runs_api import StubAuthService, bearer
from tests.unit.runs.test_conversation_mode import ConversationMetadataRepository
from tests.unit.runs.test_harness_decision import RecordingQueue
from tests.unit.runtime.test_direct_ordinary_submission_oct31 import (
    MESSAGE,
    SubmissionOnlyPreparer,
    SubmissionRepository,
)


@pytest.mark.parametrize('initial_reason', [
    None, 'systemd_broker_probe_timeout', 'systemd_transient_unit_unavailable', 'pending',
])
async def test_ordinary_submission_waits_for_shared_probe_and_only_accepts_confirmed_health(
    monkeypatch: pytest.MonkeyPatch, initial_reason: str | None,
) -> None:
    backends.clear_execution_backend_probe_cache()
    gate, entered = threading.Event(), threading.Event()
    calls = 0
    def host_probe(backend: str) -> str | None:
        nonlocal calls
        if backend != 'systemd':
            return backend+'_not_configured'
        calls += 1
        entered.set()
        assert gate.wait(2)
        return initial_reason if initial_reason != 'pending' and calls == 1 else None
    monkeypatch.setattr(backends, 'probe_skill_broker', host_probe)
    monkeypatch.setattr(backends, 'remote_backend_unavailable_reason', lambda name: name+'_not_configured')
    monkeypatch.setattr(backends, '_DISCOVERY_WAIT_SECONDS', 0.01)
    monkeypatch.setattr(backends, '_PROBE_TRANSIENT_TTL_SECONDS', 0.0)
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    now = datetime.now(UTC)
    conversation = ConversationRecord(
        id=uuid4(), tenant_id=principal.tenant_id, conversation_id='probe-pending',
        title='New project', project_id='probe-project', project_label='Probe project',
        workspace_path='probe-session', archived_at=None, created_at=now, updated_at=now,
    )
    repository = SubmissionRepository()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None, task_queue=RecordingQueue(), project_input_preparer=SubmissionOnlyPreparer(),
        conversation_repository=ConversationMetadataRepository(conversation),
    )
    application = create_app(auth_service=StubAuthService(principal), rate_limiter=object(),
                             config_service=object(), run_service=service)
    body: dict[str, Any] = {
        'message': MESSAGE, 'mode': 'direct', 'conversation_id': conversation.conversation_id,
        'project_id': conversation.project_id, 'workspace_session_id': conversation.workspace_path,
        'sandbox_profile': 'workspace_write', 'permission_mode': 'default',
        'execution_backend': 'systemd', 'direct_model': 'deepseek', 'allowed_models': ['deepseek'],
        'requested_permissions': ['workspace.read', 'workspace.write', 'command.run', 'network.read'],
        'skip_evolution_proposal': True, 'allow_scale_mode_upgrade': False,
        'runtime_timeout_seconds': 0.02 if initial_reason == 'pending' else 1.0,
    }
    async def release_after_admission_starts() -> None:
        await asyncio.sleep(0.08 if initial_reason == 'pending' else 0.02)
        gate.set()
    try:
        statuses = {status.id: status for status in await asyncio.to_thread(backends.probe_execution_backends)}
        assert entered.is_set() and calls == 1
        assert not statuses['systemd'].available and statuses['systemd'].reason == 'systemd_probe_pending'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                     base_url='http://testserver') as client:
            release = asyncio.create_task(release_after_admission_starts())
            response = await client.post('/api/v1/runs', headers=bearer(), json=body)
            # Pending at the explicit short action budget must not reach submit.
            if initial_reason is None:
                assert response.status_code == 202, response.text
                assert repository.evidence.message == MESSAGE
            else:
                assert response.status_code == 409, response.text
                expected = 'systemd_probe_pending' if initial_reason == 'pending' else initial_reason
                assert response.json()['error']['details']['reason'] == expected
                assert repository.evidence is None
            await release
            if initial_reason in {'systemd_broker_probe_timeout', 'pending'}:
                response = await client.post('/api/v1/runs', headers=bearer(), json=body)
                assert response.status_code == 202, response.text
                assert calls == (2 if initial_reason == 'systemd_broker_probe_timeout' else 1)
            elif initial_reason == 'systemd_transient_unit_unavailable':
                response = await client.post('/api/v1/runs', headers=bearer(), json=body)
                assert response.status_code == 409
                assert calls == 1
    finally:
        gate.set()
        backends.clear_execution_backend_probe_cache()
