from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runs.service import RunService
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry


class ReachedPersistence(Exception):
    pass


@pytest.mark.parametrize('branch', ['direct', 'auto', 'temporary_agent', 'evolution',
                                     'schedule', 'openclaw', 'project_preflight'])
async def test_submission_and_approval_helpers_preserve_explicit_evidence(branch):
    user = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    message = 'inspect notes'
    evidence = _capture_authenticated_input(user, message, {'mode': 'direct'})
    async def create_run(**kwargs):
        assert kwargs.get('authenticated_input') is evidence
        assert kwargs['request'] == message
        assert 'authenticated_input' not in kwargs['routing_decision']
        raise ReachedPersistence
    repository = SimpleNamespace(create_run=create_run)
    service = RunService(repository, runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
                         router=None, task_queue=SimpleNamespace())
    with pytest.raises(ReachedPersistence):
        if branch in {'direct', 'auto'}:
            await service.submit(tenant_id=user.tenant_id, actor_id=user.user_id,
                actor_role=user.role, message=message,
                mode=TaskMode.DIRECT if branch == 'direct' else TaskMode.AUTO,
                authenticated_input=evidence)
        else:
            await getattr(service, '_create_' + branch + '_approval_run')(
                tenant_id=user.tenant_id, actor_id=user.user_id, actor_role=user.role,
                message=message, mode=TaskMode.DIRECT,
                proposal=SimpleNamespace(to_payload=dict), idempotency_key=None,
                operator_selection={}, authenticated_input=evidence)


async def test_queue_and_edit_do_not_drop_explicit_input():
    user = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    evidence = _capture_authenticated_input(user, 'message', {})
    async def list_conversation(*args):
        return (SimpleNamespace(id=uuid4(), status='running'),)
    async def submit(**kwargs):
        assert kwargs.get('authenticated_input') is evidence
        raise ReachedPersistence
    async def edit(*args, **kwargs):
        assert kwargs.get('authenticated_input') is evidence
        raise ReachedPersistence
    service = RunService(SimpleNamespace(list_conversation=list_conversation),
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)), router=None, task_queue=SimpleNamespace(),
        conversation_queue_repository=SimpleNamespace(edit=edit))
    service.submit = submit
    with pytest.raises(ReachedPersistence):
        await service.queue_message(tenant_id=user.tenant_id, actor_id=user.user_id,
            actor_role=user.role, conversation_id='conv-proof', message='message',
            mode=TaskMode.DIRECT, idempotency_key='queue', authenticated_input=evidence)
    with pytest.raises(ReachedPersistence):
        await service.edit_queued_message(user.tenant_id, uuid4(), version=1, message='message',
                                          authenticated_input=evidence)


async def test_internal_submission_does_not_infer_evidence_from_actor_or_route_flags():
    async def create_run(**kwargs):
        assert 'authenticated_input' not in kwargs
        raise ReachedPersistence
    service = RunService(SimpleNamespace(create_run=create_run),
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None, task_queue=SimpleNamespace())
    with pytest.raises(ReachedPersistence):
        await service.submit(tenant_id=uuid4(), actor_id=uuid4(), actor_role=Role.ADMIN,
            message='human original authorized request', mode=TaskMode.DIRECT,
            channel_context={'source': 'human', 'origin': 'authenticated_api'})
