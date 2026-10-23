"""Approval preparation is captured before the issuer releases the live lease."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.gateway import CapabilityGateway, CapabilityStatus
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.runtime.action_approval_capture import (
    ActionApprovalCaptureError,
    action_approval_capture_active,
    before_action_approval,
    capture_action_approval,
)
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

APPROVAL = 'approval_capture_one'


def inputs() -> tuple[TaskContext, HarnessToolCallRequest, CapabilityRequest]:
    context = TaskContext(tenant_id=uuid4(), run_id=uuid4(), actor_id=uuid4(),
        actor_role=Role.OPERATOR, execution_token=uuid4(), mode=TaskMode.DIRECT,
        request='Build the workspace project.')
    assert context.actor_id is not None
    action = HarnessToolCallRequest(run_id=context.run_id, actor='main_agent',
        tool_name='project.verify', arguments={}, approval_required=False,
        sandbox='workspace_write', idempotency_key='verify-one')
    request = CapabilityRequest(tenant_id=context.tenant_id, run_id=context.run_id,
        user_id=context.actor_id, agent_id=action.actor, capability='command', operation='run',
        resource='workspace/current/project-verification', idempotency_key=action.idempotency_key)
    return context, action, request


async def empty_frame() -> Mapping[str, JsonValue]:
    return {}


async def test_no_hook_is_none_but_captured_empty_frame_is_valid_after_exit() -> None:
    context, action, request = inputs()
    assert not action_approval_capture_active()
    await before_action_approval(request, APPROVAL)
    with capture_action_approval(context, action, empty_frame) as capture:
        assert action_approval_capture_active()
        assert capture.frame_for(APPROVAL) is None
        await before_action_approval(request, APPROVAL)
    assert not action_approval_capture_active()
    assert capture.frame_for(APPROVAL) == {}
    assert capture._read is None
    assert capture._identity is None


@pytest.mark.parametrize('field', ['tenant_id', 'run_id', 'user_id', 'agent_id', 'idempotency_key'])
async def test_hook_identity_mismatch_is_sticky_and_never_reads(field: str) -> None:
    context, action, request = inputs()
    called = False

    async def read() -> Mapping[str, JsonValue]:
        nonlocal called
        called = True
        return {}

    changed = request.model_copy(update={field: uuid4() if field.endswith('_id') else 'other'})
    with capture_action_approval(context, action, read) as capture:
        with pytest.raises(ActionApprovalCaptureError):
            await before_action_approval(changed, APPROVAL)
        with pytest.raises(ActionApprovalCaptureError):
            await before_action_approval(request, APPROVAL)
        with pytest.raises(ActionApprovalCaptureError):
            capture.frame_for(APPROVAL)
    assert not called


@pytest.mark.parametrize('where', ['hook', 'frame'])
async def test_exact_approval_id_rejects_a_second_different_id(where: str) -> None:
    context, action, request = inputs()
    with capture_action_approval(context, action, empty_frame) as capture:
        await before_action_approval(request, APPROVAL)
        with pytest.raises(ActionApprovalCaptureError):
            if where == 'hook':
                await before_action_approval(request, 'approval_other')
            else:
                capture.frame_for('approval_other')
        with pytest.raises(ActionApprovalCaptureError):
            capture.frame_for(APPROVAL)


async def test_same_id_is_idempotent_and_frame_is_detached_from_mutable_preparation() -> None:
    context, action, request = inputs()
    contracts: dict[str, JsonValue] = {'phase': 'captured'}
    frame: dict[str, JsonValue] = {'contracts': contracts}
    calls = 0

    async def read() -> Mapping[str, JsonValue]:
        nonlocal calls
        calls += 1
        return frame

    with capture_action_approval(context, action, read) as capture:
        await before_action_approval(request, APPROVAL)
        contracts['phase'] = 'changed'
        await before_action_approval(request, APPROVAL)
        assert capture.frame_for(APPROVAL) == {'contracts': {'phase': 'captured'}}
    assert calls == 1


async def test_inherited_binding_is_revoked_and_still_reports_capture_present() -> None:
    context, action, request = inputs()
    resume = asyncio.Event()

    async def child() -> None:
        await resume.wait()
        assert action_approval_capture_active()
        with pytest.raises(ActionApprovalCaptureError):
            await before_action_approval(request, APPROVAL)

    with capture_action_approval(context, action, empty_frame) as capture:
        task = asyncio.create_task(child())
    resume.set()
    await task
    with pytest.raises(ActionApprovalCaptureError):
        capture.frame_for(APPROVAL)


async def test_inflight_hook_never_looks_like_an_untriggered_adapter() -> None:
    context, action, request = inputs()
    entered, release = asyncio.Event(), asyncio.Event()

    async def read() -> Mapping[str, JsonValue]:
        entered.set()
        await release.wait()
        return {}

    with capture_action_approval(context, action, read) as capture:
        task = asyncio.create_task(before_action_approval(request, APPROVAL))
        await entered.wait()
        try:
            with pytest.raises(ActionApprovalCaptureError):
                capture.frame_for(APPROVAL)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(ActionApprovalCaptureError):
            capture.frame_for(APPROVAL)


async def test_revocation_while_reading_cannot_publish_a_frame() -> None:
    context, action, request = inputs()
    entered, release = asyncio.Event(), asyncio.Event()

    async def read() -> Mapping[str, JsonValue]:
        entered.set()
        await release.wait()
        return {'contracts': {}}

    with capture_action_approval(context, action, read) as capture:
        task = asyncio.create_task(before_action_approval(request, APPROVAL))
        await entered.wait()
    release.set()
    with pytest.raises(ActionApprovalCaptureError):
        await task
    with pytest.raises(ActionApprovalCaptureError):
        capture.frame_for(APPROVAL)


async def test_nested_binding_restores_outer_collector() -> None:
    context, action, request = inputs()
    other, other_action, other_request = inputs()
    with capture_action_approval(context, action, empty_frame) as outer:
        with capture_action_approval(other, other_action, empty_frame) as inner:
            await before_action_approval(other_request, 'approval_inner')
        await before_action_approval(request, APPROVAL)
    assert inner.frame_for('approval_inner') == outer.frame_for(APPROVAL) == {}


@pytest.mark.parametrize('key', ['native_assistant', 'reasoning_content', 'execution_token'])
async def test_sensitive_frame_is_rejected_without_exposing_data(key: str) -> None:
    context, action, request = inputs()

    async def read() -> Mapping[str, JsonValue]:
        return {'contracts': {key: 'private-content'}}

    with capture_action_approval(context, action, read) as capture:
        with pytest.raises(ActionApprovalCaptureError) as caught:
            await before_action_approval(request, APPROVAL)
        assert 'private-content' not in str(caught.value)
        assert caught.value.__context__ is None
        with pytest.raises(ActionApprovalCaptureError):
            capture.frame_for(APPROVAL)


@pytest.mark.parametrize('failure', ['error', 'cancel', 'cleanup'])
async def test_capture_failure_is_sticky_and_cleanup_exceptions_are_preserved(failure: str) -> None:
    context, action, request = inputs()
    error: BaseException
    if failure == 'cancel':
        error = asyncio.CancelledError()
    elif failure == 'cleanup':
        error = ProcessCleanupIncomplete('private-content')
    else:
        error = OSError('private-content')

    async def read() -> Mapping[str, JsonValue]:
        raise error

    with capture_action_approval(context, action, read) as capture:
        expected = ActionApprovalCaptureError if failure == 'error' else type(error)
        with pytest.raises(expected) as caught:
            await before_action_approval(request, APPROVAL)
        if failure == 'error':
            assert 'private-content' not in str(caught.value)
            assert caught.value.__context__ is None
        else:
            assert caught.value is error
        with pytest.raises(ActionApprovalCaptureError):
            capture.frame_for(APPROVAL)


class IssuerRepository:
    def __init__(self, store: InMemoryApprovalStore) -> None:
        self.store = store
        self.running = True
        self.live_lease = True
        self.begins = 0
        self.published_before_begin: bool | None = None

    async def begin_capability_approval(
        self, tenant_id: UUID, run_id: UUID, *, approval_id: str,
        approval_fingerprint: str, approval_scope: str | None, actor_id: UUID,
    ) -> None:
        self.published_before_begin = await self.store.get(approval_id) is not None
        self.begins += 1
        self.running, self.live_lease = False, False


def issuer(request: CapabilityRequest, effect: PolicyEffect = PolicyEffect.REQUIRE_APPROVAL,
           ) -> tuple[CapabilityGateway, IssuerRepository, InMemoryApprovalStore]:
    store = InMemoryApprovalStore()
    repository = IssuerRepository(store)
    policy = CapabilityPolicy((CapabilityRule(request.tenant_id, Role.OPERATOR, request.agent_id,
        request.capability, request.operation, request.resource, effect),))
    return CapabilityGateway(policy, ApprovalService(store), repository), repository, store


async def test_real_issuer_captures_before_waiting_and_publishes_only_after_begin() -> None:
    context, action, request = inputs()
    gateway, repository, store = issuer(request)
    reads = 0

    async def read() -> Mapping[str, JsonValue]:
        nonlocal reads
        assert repository.running and repository.live_lease
        reads += 1
        return {'contracts': {'phase': 'prepared'}}

    with capture_action_approval(context, action, read) as capture:
        result = await gateway.invoke(request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.WAITING_APPROVAL and result.approval_id is not None
    assert capture.frame_for(result.approval_id) == {'contracts': {'phase': 'prepared'}}
    assert reads == repository.begins == 1
    assert not repository.running and not repository.live_lease
    assert repository.published_before_begin is False
    assert await store.get(result.approval_id) is not None


async def test_real_issuer_does_not_begin_or_publish_when_capture_fails() -> None:
    context, action, request = inputs()
    gateway, repository, store = issuer(request)

    async def read() -> Mapping[str, JsonValue]:
        raise OSError('private read failed')

    with capture_action_approval(context, action, read), pytest.raises(ActionApprovalCaptureError):
        await gateway.invoke(request, role=Role.OPERATOR)
    assert repository.running and repository.live_lease and repository.begins == 0
    assert await store.list_pending() == ()


async def test_real_issuer_preserves_legacy_publication_order_without_collector() -> None:
    _, _, request = inputs()
    gateway, repository, store = issuer(request)
    result = await gateway.invoke(request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.WAITING_APPROVAL and result.approval_id is not None
    assert repository.published_before_begin is True
    assert await store.get(result.approval_id) is not None


@pytest.mark.parametrize('effect', [PolicyEffect.ALLOW, PolicyEffect.DENY])
async def test_allow_or_deny_does_not_trigger_capture(effect: PolicyEffect) -> None:
    context, action, request = inputs()
    gateway, repository, store = issuer(request, effect)

    async def unexpected() -> Mapping[str, JsonValue]:
        pytest.fail('only an actual approval wait may capture preparation')

    with capture_action_approval(context, action, unexpected) as capture:
        result = await gateway.invoke(request, role=Role.OPERATOR)
        assert capture.frame_for(APPROVAL) is None
    assert result.status in (CapabilityStatus.ALLOWED, CapabilityStatus.DENIED)
    assert repository.begins == 0 and await store.list_pending() == ()


async def test_revoked_inherited_issuer_cannot_publish_an_approval() -> None:
    context, action, request = inputs()
    gateway, repository, store = issuer(request)
    resume = asyncio.Event()

    async def child() -> None:
        await resume.wait()
        with pytest.raises(ActionApprovalCaptureError):
            await gateway.invoke(request, role=Role.OPERATOR)

    with capture_action_approval(context, action, empty_frame):
        task = asyncio.create_task(child())
    resume.set()
    await task
    assert repository.begins == 0 and await store.list_pending() == ()
