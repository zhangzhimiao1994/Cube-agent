"""Fresh server policy only; no retroactive cleanup authority for legacy runs."""

from __future__ import annotations

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
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
from tests.unit.runs.test_terminal_hooks import (
    ExecutableFakeRepository,
    RuntimeRecordsRestoredCheckpointCompletes,
    SelfRepairCheckpointRepository,
)

POLICY = 'terminal_failed_python_environment_cleanup_version'


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_fresh_run_uses_server_cleanup_policy_without_expanding_execution_scope(mode: TaskMode) -> None:
    repository = RecordingRepository()
    service = RunService(repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(mode),)), router=None, task_queue=RecordingQueue())
    message = 'Modify the existing CLI and deliver tested code.'
    result = await service.submit(tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
        message=message, mode=mode, project_id='cleanup-project', workspace_session_id='cleanup-session',
        sandbox_profile='read_only', requested_permissions=('workspace.read',),
        channel_context={POLICY: '0'})
    assert result.status is RunStatus.QUEUED
    created = repository.created[0]
    route = created['routing_decision']
    assert isinstance(route, dict)
    assert type(route.get(POLICY)) is int and route[POLICY] == 1
    assert created['request'] == message and created['actor_id'] == ACTOR_ID
    assert created['actor_role'] is Role.OPERATOR
    assert route['requested_mode'] == mode.value
    assert route['sandbox_profile'] == 'read_only'
    assert route['requested_permissions'] == ['workspace.read']
    assert route['project_id'] == 'cleanup-project' and route['workspace_session_id'] == 'cleanup-session'


async def test_auto_submission_has_same_server_selected_cleanup_policy() -> None:
    repository = ConversationModeRepository(None)
    service = RunService(repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)), router=None,
        task_queue=RecordingQueue())
    await service.submit(tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
        message='Build a CLI with persistent configuration.', mode=TaskMode.AUTO,
        channel_context={POLICY: '2'})
    route = repository.created[-1]['routing_decision']
    assert isinstance(route, dict)
    assert type(route.get(POLICY)) is int and route[POLICY] == 1


@pytest.mark.parametrize('version', [None, 0, 1, True, '1', 2])
def test_budget_refresh_does_not_issue_cleanup_authority(version: object) -> None:
    route: dict[str, object] = {'project_scale': 'medium'}
    if version is not None:
        route[POLICY] = version
    before = dict(route)
    refreshed = _with_runtime_token_budget_policy(route, configured_tokens=100000, mode=TaskMode.DISPATCH)
    assert route == before
    assert (POLICY in refreshed) is (version is not None)
    if version is not None:
        assert type(refreshed[POLICY]) is type(version) and refreshed[POLICY] == version


@pytest.mark.parametrize('version', [None, 0, 1, True, '1', 2])
@pytest.mark.parametrize('with_checkpoint', [False, True])
async def test_execute_preserves_persisted_policy_without_upgrading_legacy(
    version: object, with_checkpoint: bool,
) -> None:
    route: dict[str, object] = {'source': 'self_repair', 'self_repair_accepted': True,
        'sandbox_profile': 'read_only', 'requested_permissions': ['workspace.read']}
    if version is not None:
        route[POLICY] = version
    before = dict(route)
    repository = (SelfRepairCheckpointRepository(routing_decision=route) if with_checkpoint
        else ExecutableFakeRepository(routing_decision=route))
    repository.row.actor_role = Role.OPERATOR.value
    checkpoint = repository.checkpoint if isinstance(repository, SelfRepairCheckpointRepository) else None
    checkpoint_payload = None if checkpoint is None else checkpoint.to_payload()
    runtime = RuntimeRecordsRestoredCheckpointCompletes()
    service = RunService(repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=RecordingQueue())
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.COMPLETED and len(runtime.contexts) == 1
    assert runtime.contexts[0].checkpoint == checkpoint
    if checkpoint is not None:
        assert checkpoint.to_payload() == checkpoint_payload
    for persisted in (repository.row.routing_decision, runtime.contexts[0].to_payload()['routing_decision']):
        assert isinstance(persisted, dict)
        assert (POLICY in persisted) is (version is not None)
        for key, value in before.items():
            assert type(persisted[key]) is type(value) and persisted[key] == value
    assert route == before
