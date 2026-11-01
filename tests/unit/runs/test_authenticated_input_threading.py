from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Never
from uuid import uuid4

import pytest

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runs.repository import RunRecord
from agent_hub.runs.service import RunService
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.conversation_authenticated_input_typed_fixture import (
    ActiveConversationRepository,
    EditSpy,
    PersistenceSpy,
    SubmitSpyService,
    UnexpectedQueue,
)


class ReachedPersistence(Exception):
    pass


@pytest.mark.parametrize('branch', ['direct', 'auto', 'temporary_agent', 'evolution',
                                     'schedule', 'openclaw', 'project_preflight'])
async def test_submission_and_approval_helpers_preserve_explicit_evidence(branch: str) -> None:
    user = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    message = 'inspect notes'
    evidence = _capture_authenticated_input(user, message, {'mode': 'direct'})
    async def create_run(**kwargs: object) -> Never:
        assert kwargs.get('authenticated_input') is evidence
        assert kwargs['request'] == message
        route = kwargs['routing_decision']
        assert isinstance(route, dict)
        assert 'authenticated_input' not in route
        raise ReachedPersistence
    repository = PersistenceSpy(create_run)
    service = RunService(repository, runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
                         router=None, task_queue=UnexpectedQueue())
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


async def test_queue_and_edit_do_not_drop_explicit_input() -> None:
    user = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    evidence = _capture_authenticated_input(user, 'message', {})
    active = RunRecord(id=uuid4(), tenant_id=user.tenant_id, actor_id=user.user_id,
        request='active', mode=TaskMode.DIRECT, status=RunStatus.RUNNING,
        version=1, created_at=datetime.now(UTC), routing_decision=None)
    async def submit(**kwargs: object) -> Never:
        assert kwargs.get('authenticated_input') is evidence
        raise ReachedPersistence
    async def edit(*args: object, **kwargs: object) -> Never:
        assert kwargs.get('authenticated_input') is evidence
        raise ReachedPersistence
    service = SubmitSpyService(ActiveConversationRepository(active),
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)), router=None, task_queue=UnexpectedQueue(),
        conversation_queue_repository=EditSpy(edit))
    service.callback = submit
    with pytest.raises(ReachedPersistence):
        await service.queue_message(tenant_id=user.tenant_id, actor_id=user.user_id,
            actor_role=user.role, conversation_id='conv-proof', message='message',
            mode=TaskMode.DIRECT, idempotency_key='queue', authenticated_input=evidence)
    with pytest.raises(ReachedPersistence):
        await service.edit_queued_message(user.tenant_id, uuid4(), version=1, message='message',
                                          authenticated_input=evidence)


async def test_internal_submission_does_not_infer_evidence_from_actor_or_route_flags() -> None:
    async def create_run(**kwargs: object) -> Never:
        assert 'authenticated_input' not in kwargs
        raise ReachedPersistence
    service = RunService(PersistenceSpy(create_run),
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None, task_queue=UnexpectedQueue())
    with pytest.raises(ReachedPersistence):
        await service.submit(tenant_id=uuid4(), actor_id=uuid4(), actor_role=Role.ADMIN,
            message='human original authorized request', mode=TaskMode.DIRECT,
            channel_context={'source': 'human', 'origin': 'authenticated_api'})
