"""Only the last recorded verify may recover verifier-owned authority."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from copy import deepcopy
from typing import Literal
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import JsonValue
from agent_hub.runtime.direct import RuntimeExecutionError
from tests.unit.runtime.test_direct_project_verification import Model, context


def test_runtime_reexports_shared_validator() -> None:
    from agent_hub.runtime.action_checkpoint import action_checkpoint_valid
    from agent_hub.runtime.action_direct import action_checkpoint_valid as exported
    from tests.unit.runs.test_direct_action_approval_checkpoint import APPROVAL, action_checkpoint

    assert exported is action_checkpoint_valid
    assert action_checkpoint_valid(action_checkpoint(), APPROVAL)
    assert not action_checkpoint_valid(action_checkpoint(), 'approval_other')
    assert not action_checkpoint_valid(action_checkpoint(reference={}))


class RecoveryBackend:
    def __init__(self, payload: Mapping[str, JsonValue], *, rejection: str | None = None) -> None:
        self.payload = payload
        self.rejection = rejection
        self.restores: list[tuple[UUID, UUID, str, str]] = []
        self.authority: Mapping[str, JsonValue] | None = None

    def is_replay_safe(self, name: str) -> bool:
        return False

    async def execute(self, **kwargs: object) -> Mapping[str, JsonValue]:
        raise AssertionError('recovery must not execute a native capability')

    async def restore_project_verification(
        self, *, tenant_id: UUID, run_id: UUID, actor: str, action_key: str,
    ) -> Mapping[str, JsonValue]:
        self.restores.append((tenant_id, run_id, actor, action_key))
        if self.rejection is not None:
            raise RuntimeCapabilityError(self.rejection)
        self.authority = self.payload
        return self.payload


class NoNativeGateway:
    def __init__(self) -> None:
        self.requests: list[HarnessToolCallRequest] = []

    async def invoke(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *,
        user_id: UUID | None = None, role: Role | None = None,
    ) -> HarnessToolCallResult:
        self.requests.append(request)
        raise AssertionError('recovery must not repeat a historical native call')


def verified(source: str) -> dict[str, JsonValue]:
    return {
        'verification': {'status': 'passed', 'source_sha256': source * 64,
                         'execution_id': 'native_' + source, 'exit_code': 0},
        'generated_project_acceptance': {'status': 'passed', 'checks': ('build', 'test')},
    }


def runtime(backend: RecoveryBackend) -> tuple[ActionApprovedDirectRuntime, NoNativeGateway]:
    gateway = NoNativeGateway()
    subject = ActionApprovedDirectRuntime(Model(), logical_model='main',
        capability_gateway=backend, harness_tool_gateway=gateway,
        artifact_repository=InMemoryArtifactRepository())
    subject._action_context = context()
    return subject, gateway


def record(
    subject: ActionApprovedDirectRuntime, name: str, key: str,
    payload: Mapping[str, JsonValue], *, status: Literal['succeeded', 'failed'] = 'succeeded',
) -> tuple[HarnessToolCallRequest, HarnessToolCallResult, str]:
    assert subject._action_context is not None
    request = HarnessToolCallRequest(run_id=subject._action_context.run_id,
        actor='main_agent', tool_name=name, arguments={}, approval_required=False,
        sandbox='workspace_write', idempotency_key=key)
    result = HarnessToolCallResult(call_id=request.call_id, tool_name=name,
        status=status, payload=payload,
        failure_reason='project test failed' if status == 'failed' else None)
    raw_sha = hashlib.sha256(('raw:' + key).encode()).hexdigest()
    subject._actions.append({'raw_sha256': raw_sha, 'request': dict(request.to_payload()),
                             'result': dict(result.to_payload())})
    return request, result, raw_sha


@pytest.mark.parametrize('later_status', ['succeeded', 'failed'])
async def test_old_pass_is_history_and_only_last_verify_can_restore(
    later_status: Literal['succeeded', 'failed'],
) -> None:
    old_payload, latest_payload = verified('a'), verified('b')
    # A failed later attempt can leave the DAO's last pass pointing to the old action.
    backend = RecoveryBackend(latest_payload if later_status == 'succeeded' else old_payload)
    subject, gateway = runtime(backend)
    old = record(subject, 'project.verify', 'verify-one', old_payload)
    write = record(subject, 'workspace.write_text', 'write-between', {'written': True})
    latest = record(subject, 'project.verify', 'verify-two',
        latest_payload if later_status == 'succeeded' else {'verification': {'status': 'failed'}},
        status=later_status)
    read = record(subject, 'workspace.read', 'read-after', {'content': 'current source'})

    for request, expected, raw_sha in (old, write):
        assert await subject._invoke(request, raw_sha=raw_sha) == expected
        assert backend.restores == []
        assert backend.authority is None

    request, expected, raw_sha = latest
    assert await subject._invoke(request, raw_sha=raw_sha) == expected
    request, expected, raw_sha = read
    assert await subject._invoke(request, raw_sha=raw_sha) == expected
    ctx = subject._action_context
    assert ctx is not None
    assert backend.restores == (
        [(ctx.tenant_id, ctx.run_id, 'main_agent', 'verify-two')]
        if later_status == 'succeeded' else []
    )
    assert backend.authority == (latest_payload if later_status == 'succeeded' else None)
    assert subject._action_cursor == 4
    assert gateway.requests == []


async def test_last_pass_restores_exact_action_key_and_complete_payload() -> None:
    payload = verified('b')
    backend = RecoveryBackend(deepcopy(payload))
    subject, gateway = runtime(backend)
    request, expected, raw_sha = record(subject, 'project.verify', 'verify-action-key', payload)
    assert request.call_id != request.idempotency_key

    result = await subject._invoke(request, raw_sha=raw_sha)

    assert result == expected
    assert result.payload == payload
    ctx = subject._action_context
    assert ctx is not None
    assert backend.restores == [(ctx.tenant_id, ctx.run_id, 'main_agent', 'verify-action-key')]
    assert backend.authority == payload
    assert subject._action_cursor == 1
    assert gateway.requests == []


@pytest.mark.parametrize('rejection', [
    'project source changed during verification',
    'project verification scope is not authorized',
])
async def test_latest_pass_rejection_stops_recovery_without_native_fallback(rejection: str) -> None:
    backend = RecoveryBackend(verified('b'), rejection=rejection)
    subject, gateway = runtime(backend)
    old = record(subject, 'project.verify', 'verify-one', verified('a'))
    write = record(subject, 'workspace.write_text', 'write-between', {'written': True})
    latest = record(subject, 'project.verify', 'verify-two', verified('b'))
    record(subject, 'workspace.bundle', 'bundle-after', {'artifact_id': 'bundle'})
    for request, expected, raw_sha in (old, write):
        assert await subject._invoke(request, raw_sha=raw_sha) == expected

    request, _, raw_sha = latest
    with pytest.raises(RuntimeCapabilityError, match=rejection):
        await subject._invoke(request, raw_sha=raw_sha)

    assert [call[3] for call in backend.restores] == ['verify-two']
    assert backend.authority is None
    assert subject._action_cursor == 2
    assert gateway.requests == []


@pytest.mark.parametrize('restored', [
    verified('c'),
    {**verified('b'), 'generated_project_acceptance': {'status': 'failed', 'checks': ()}},
    {'verification': verified('b')['verification']},
    {**verified('b'), 'unexpected': True},
])
async def test_complete_restored_payload_mismatch_fails_closed(
    restored: Mapping[str, JsonValue],
) -> None:
    backend = RecoveryBackend(restored)
    subject, gateway = runtime(backend)
    request, _, raw_sha = record(subject, 'project.verify', 'verify-two', verified('b'))

    with pytest.raises(RuntimeExecutionError, match='recovery does not match'):
        await subject._invoke(request, raw_sha=raw_sha)

    assert [call[3] for call in backend.restores] == ['verify-two']
    assert subject._action_cursor == 0
    assert gateway.requests == []


async def test_pending_verify_also_keeps_old_pass_historical() -> None:
    backend = RecoveryBackend(verified('a'))
    subject, gateway = runtime(backend)
    request, expected, raw_sha = record(subject, 'project.verify', 'verify-one', verified('a'))
    pending, _, pending_sha = record(subject, 'project.verify', 'verify-pending', {})
    subject._actions.pop()
    subject._pending = {'raw_sha256': pending_sha, 'request': dict(pending.to_payload())}

    assert await subject._invoke(request, raw_sha=raw_sha) == expected

    assert backend.restores == []
    assert backend.authority is None
    assert subject._action_cursor == 1
    assert gateway.requests == []


async def test_last_pass_without_restore_api_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = RecoveryBackend(verified('b'))
    monkeypatch.setattr(backend, 'restore_project_verification', None)
    subject, gateway = runtime(backend)
    request, _, raw_sha = record(subject, 'project.verify', 'verify-two', verified('b'))

    with pytest.raises(RuntimeExecutionError, match='recovery is unavailable'):
        await subject._invoke(request, raw_sha=raw_sha)

    assert subject._action_cursor == 0
    assert gateway.requests == []
