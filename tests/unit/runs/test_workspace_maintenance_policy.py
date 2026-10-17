from __future__ import annotations

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.service import RunService, _with_runtime_token_budget_policy
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_conversation_mode import ConversationModeRepository
from tests.unit.runs.test_harness_decision import (
    ACTOR_ID,
    TENANT_ID,
    RecordingQueue,
    RecordingRepository,
)
from tests.unit.runtime.test_role_planning_intent import finish

FLAGS = ('workspace_maintenance_contract_version', 'post_tool_finalization_growth_version')


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
def test_fresh_submit_persists_independent_policies_without_trusting_channel(mode: TaskMode) -> None:
    repository = RecordingRepository()
    service = RunService(repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(mode),)), router=None, task_queue=RecordingQueue())
    message = 'Fix the existing TypeScript project files and deliver a ZIP.'
    finish(service.submit(tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
        message=message, mode=mode, project_id='existing', workspace_session_id='session',
        sandbox_profile='read_only', requested_permissions=('workspace.read',),
        channel_context={key: '0' for key in (*FLAGS, 'role_planning_intent_version')}))
    record = repository.created[0]; route = record['routing_decision']
    assert isinstance(route, dict)
    for key in (*FLAGS, 'role_planning_intent_version'):
        assert type(route[key]) is int and route[key] == 1
    assert record['request'] == message and record['actor_id'] == ACTOR_ID and record['actor_role'] is Role.OPERATOR
    assert route['requested_mode'] == mode.value
    assert route['sandbox_profile'] == 'read_only' and route['requested_permissions'] == ['workspace.read']


def test_fresh_auto_route_has_both_independent_policies() -> None:
    repository = ConversationModeRepository(None)
    service = RunService(repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)), router=None,
        task_queue=RecordingQueue())
    finish(service.submit(tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
        message='Build a large website with authentication and persistent data.', mode=TaskMode.AUTO))
    route = repository.created[-1]['routing_decision']
    assert isinstance(route, dict)
    assert all(type(route[key]) is int and route[key] == 1 for key in FLAGS)


@pytest.mark.parametrize('version', [None, 0, 1])
def test_budget_refresh_preserves_or_omits_policies_without_upgrading(version: int | None) -> None:
    route: dict[str, object] = {'project_scale': 'medium'}
    if version is not None:
        route.update({key: version for key in FLAGS})
    before = dict(route)
    refreshed = _with_runtime_token_budget_policy(route, configured_tokens=100000, mode=TaskMode.DISPATCH)
    assert route == before
    for key in FLAGS:
        assert (key in refreshed) is (version is not None)
        if version is not None:
            assert refreshed[key] == version
