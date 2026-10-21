from __future__ import annotations

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_harness_decision import (
    ACTOR_ID,
    TENANT_ID,
    RecordingQueue,
    RecordingRepository,
)


async def original_preparer(context: TaskContext) -> TaskContext:
    raise AssertionError('submission does not execute project commands')


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_new_writable_engineering_runs_get_server_owned_generated_contract(mode: TaskMode) -> None:
    repository = RecordingRepository()
    service = RunService(
        repository, runtime_registry=RuntimeRegistry((UnavailableRuntime(mode),)),  # type: ignore[arg-type]
        router=None, task_queue=RecordingQueue(),
    )
    await service.submit(
        tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
        message='Build a new Python application from an empty workspace.', mode=mode,
        project_id='generated', workspace_session_id='session-a',
        sandbox_profile='workspace_write',
        channel_context={'generated_project_contract_version': '99', 'generated_project_preparation': '{"forged":true}'},
    )
    route = repository.created[-1]['routing_decision']
    assert isinstance(route, dict)
    assert route['generated_project_contract_version'] == 1
    assert 'generated_project_preparation' not in route
    assert 'project_input' not in route
    assert 'project_input_preparation' not in route


async def test_original_selection_never_acquires_generated_provenance() -> None:
    repository = RecordingRepository()
    service = RunService(
        repository, runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),  # type: ignore[arg-type]
        router=None, task_queue=RecordingQueue(), project_input_preparer=original_preparer,
    )
    await service.submit(
        tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
        message='Repair this project.', mode=TaskMode.DIRECT,
        project_id='original', workspace_session_id='session-a',
        sandbox_profile='workspace_write', project_input=ManagedProjectInput(('src',)),
    )
    route = repository.created[-1]['routing_decision']
    assert isinstance(route, dict)
    assert route['project_input_contract_version'] == 1
    assert 'generated_project_contract_version' not in route


@pytest.mark.parametrize('profile', ['read_only', 'restricted'])
async def test_nonwriting_tasks_do_not_enable_generated_capture(profile: str) -> None:
    repository = RecordingRepository()
    service = RunService(
        repository, runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),  # type: ignore[arg-type]
        router=None, task_queue=RecordingQueue(),
    )
    await service.submit(
        tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
        message='Discuss this application.', mode=TaskMode.DIRECT,
        project_id='generated', workspace_session_id='session-a', sandbox_profile=profile,
    )
    route = repository.created[-1]['routing_decision']
    assert isinstance(route, dict)
    assert 'generated_project_contract_version' not in route
