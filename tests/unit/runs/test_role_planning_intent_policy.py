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


@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID))
@pytest.mark.parametrize("agent_ids", ((), ("chosen_one", "chosen_two")))
async def test_fresh_submission_persists_intent_version_without_changing_execution_scope(
    mode: TaskMode,
    agent_ids: tuple[str, ...],
) -> None:
    repository = RecordingRepository()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(mode),)),
        router=None,
        task_queue=RecordingQueue(),
    )
    message = (
        "Fix TypeScript compatibility in the existing project. Never install packages. "
        'UNTRUSTED_DATA_JSON={"diagnostic":"install research server"}'
    )

    submitted = await service.submit(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        actor_role=Role.OPERATOR,
        message=message,
        mode=mode,
        agent_ids=agent_ids,
        project_id="intent-project",
        workspace_session_id="intent-session",
        sandbox_profile="read_only",
        requested_permissions=("workspace.read",),
        channel_context={"role_planning_intent_version": "0"},
    )

    assert submitted.status is RunStatus.QUEUED
    created = repository.created[0]
    route = created["routing_decision"]
    assert isinstance(route, dict)
    assert type(route["role_planning_intent_version"]) is int
    assert route["role_planning_intent_version"] == 2
    assert created["request"] == message
    assert created["actor_id"] == ACTOR_ID
    assert created["actor_role"] is Role.OPERATOR
    assert route["requested_mode"] == mode.value
    assert route["selected_agent_ids"] == list(agent_ids)
    assert route["project_id"] == "intent-project"
    assert route["workspace_session_id"] == "intent-session"
    assert route["sandbox_profile"] == "read_only"
    assert route["requested_permissions"] == ["workspace.read"]


async def test_fresh_auto_route_persists_intent_version() -> None:
    repository = ConversationModeRepository(None)
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)),
        router=None,
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        actor_role=Role.OPERATOR,
        message="Build a large website with authentication and persistent project data.",
        mode=TaskMode.AUTO,
    )

    assert submitted.status is RunStatus.QUEUED
    route = repository.created[-1]["routing_decision"]
    assert isinstance(route, dict)
    assert route["requested_mode"] == "auto"
    assert type(route["role_planning_intent_version"]) is int
    assert route["role_planning_intent_version"] == 2


@pytest.mark.parametrize("version", (None, 0, 1, 2))
def test_runtime_budget_refresh_does_not_upgrade_legacy_role_planning(version: int | None) -> None:
    legacy: dict[str, object] = {"project_scale": "medium"}
    if version is not None:
        legacy["role_planning_intent_version"] = version
    before = dict(legacy)
    refreshed = _with_runtime_token_budget_policy(
        legacy, configured_tokens=100_000, mode=TaskMode.DISPATCH,
    )
    assert ("role_planning_intent_version" in refreshed) is (version is not None)
    if version is not None:
        assert refreshed["role_planning_intent_version"] == version
    assert legacy == before


@pytest.mark.parametrize("version", (None, 0, 1, 2))
@pytest.mark.parametrize("with_checkpoint", (False, True))
async def test_execute_preserves_persisted_role_intent_and_checkpoint(
    version: int | None, with_checkpoint: bool,
) -> None:
    route: dict[str, object] = {
        "source": "self_repair",
        "self_repair_accepted": True,
        "selected_agent_ids": ["chosen_one", "chosen_two"],
        "sandbox_profile": "read_only",
        "requested_permissions": ["workspace.read"],
    }
    if version is not None:
        route["role_planning_intent_version"] = version
    before = dict(route)
    repository = (
        SelfRepairCheckpointRepository(routing_decision=route)
        if with_checkpoint else ExecutableFakeRepository(routing_decision=route)
    )
    repository.row.actor_role = Role.OPERATOR.value
    request = repository.row.request
    checkpoint = (
        repository.checkpoint if isinstance(repository, SelfRepairCheckpointRepository) else None
    )
    checkpoint_payload = None if checkpoint is None else checkpoint.to_payload()
    runtime = RuntimeRecordsRestoredCheckpointCompletes()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((runtime,)),
        router=None,
        task_queue=RecordingQueue(),
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.COMPLETED
    assert len(runtime.contexts) == 1
    context = runtime.contexts[0]
    assert context.request == request
    assert context.actor_id == ACTOR_ID
    assert context.actor_role is Role.OPERATOR
    assert context.checkpoint == checkpoint
    assert runtime.restored_checkpoints == ([] if checkpoint is None else [checkpoint])
    if checkpoint is not None:
        assert checkpoint.to_payload() == checkpoint_payload
    for persisted in (repository.row.routing_decision, context.to_payload()["routing_decision"]):
        assert isinstance(persisted, dict)
        assert ("role_planning_intent_version" in persisted) is (version is not None)
        for key, value in before.items():
            assert persisted[key] == value
    assert route == before
