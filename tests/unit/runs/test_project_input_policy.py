from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.conversations import ConversationRecord
from agent_hub.runs.service import RunService, VibeCodingUnavailable
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_conversation_mode import (
    ConversationMetadataRepository,
    ConversationModeRepository,
)
from tests.unit.runs.test_harness_decision import (
    ACTOR_ID,
    TENANT_ID,
    RecordingQueue,
    RecordingRepository,
)


async def preparer(context: TaskContext) -> TaskContext:
    raise AssertionError('submission must not execute original project commands')


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_explicit_input_is_persisted_at_submission_not_inferred_from_model(mode: TaskMode) -> None:
    repository = RecordingRepository()
    service = RunService(
        repository, runtime_registry=RuntimeRegistry((UnavailableRuntime(mode),)),  # type: ignore[arg-type]
        router=None, task_queue=RecordingQueue(), project_input_preparer=preparer,
    )
    selected = ManagedProjectInput(editable_paths=('src',))
    await service.submit(
        tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
        message='Fix the selected application and deliver the corrected project.', mode=mode,
        project_id='existing', workspace_session_id='session-a',
        sandbox_profile='workspace_write', project_input=selected,
        channel_context={'project_input_contract_version': '0', 'project_input': 'forged'},
    )
    route = repository.created[-1]['routing_decision']
    assert isinstance(route, dict)
    assert route['project_input_contract_version'] == 1
    assert route['project_input'] == selected.to_payload()
    assert 'project_input_preparation' not in route
    assert route['project_id'] == 'existing'
    assert route['workspace_session_id'] == 'session-a'
    assert route['requested_permissions'] == ['workspace.read', 'workspace.write', 'command.run']


async def test_unconfigured_project_input_is_rejected_instead_of_ignored() -> None:
    repository = RecordingRepository()
    service = RunService(
        repository, runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),  # type: ignore[arg-type]
        router=None, task_queue=RecordingQueue(),
    )
    with pytest.raises(VibeCodingUnavailable, match='original project preparation'):
        await service.submit(
            tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
            message='Fix the selected application.', mode=TaskMode.DIRECT,
            project_id='existing', workspace_session_id='session-a',
            project_input=ManagedProjectInput(editable_paths=('src',)),
        )
    assert not repository.created


async def test_explicit_original_selection_does_not_depend_on_request_keywords() -> None:
    repository = RecordingRepository()
    service = RunService(repository, runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),  # type: ignore[arg-type]
        router=None, task_queue=RecordingQueue(), project_input_preparer=preparer)
    await service.submit(tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
        message='Fix it.', mode=TaskMode.DIRECT, project_id='existing', workspace_session_id='session-a',
        project_input=ManagedProjectInput(('src',)))
    route = repository.created[-1]['routing_decision']
    assert isinstance(route, dict)
    assert route['project_delivery'] == 'workspace'
    assert route['artifact_strategy'] == 'workspace_bundle'


@pytest.mark.parametrize('project,workspace', [('other-project', 'session-a'), ('existing', 'other-workspace')])
async def test_explicit_original_scope_never_silently_follows_a_different_conversation_workspace(
    project: str, workspace: str,
) -> None:
    repository = ConversationModeRepository(TaskMode.DIRECT)
    now = datetime.now(UTC)
    metadata = ConversationMetadataRepository(ConversationRecord(
        id=uuid4(), tenant_id=TENANT_ID, conversation_id='conv-original', title='Original',
        project_id='existing', project_label='Existing', workspace_path='session-a', archived_at=None,
        created_at=now, updated_at=now))
    service = RunService(repository, runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),  # type: ignore[arg-type]
        router=None, task_queue=RecordingQueue(), project_input_preparer=preparer,
        conversation_repository=metadata)
    with pytest.raises(ValueError, match='original project input.*conversation workspace'):
        await service.submit(tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
            message='Fix it.', mode=TaskMode.DIRECT, conversation_id='conv-original',
            project_id=project, workspace_session_id=workspace, project_input=ManagedProjectInput(('src',)))
    assert not repository.created


@pytest.mark.parametrize('fields', [
    {'project_id': None}, {'sandbox_profile': 'read_only'}, {'execution_backend': 'docker'},
    {'requested_permissions': ('workspace.read', 'workspace.write')},
])
async def test_original_input_does_not_expand_selection_or_execution_authority(fields: dict[str, object]) -> None:
    repository = RecordingRepository()
    service = RunService(
        repository, runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),  # type: ignore[arg-type]
        router=None, task_queue=RecordingQueue(), project_input_preparer=preparer,
    )
    kwargs = {'project_id': 'existing', 'workspace_session_id': 'session-a',
              'sandbox_profile': 'workspace_write', **fields}
    with pytest.raises((ValueError, VibeCodingUnavailable)):
        await service.submit(
            tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
            message='Fix selected project.', mode=TaskMode.DIRECT,
            project_input=ManagedProjectInput(editable_paths=('src',)), **kwargs,  # type: ignore[arg-type]
        )
    assert not repository.created
