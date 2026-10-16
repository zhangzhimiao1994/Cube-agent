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


@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID))
async def test_fresh_submission_persists_intent_version_without_changing_execution_scope(
    mode: TaskMode,
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
    assert route["role_planning_intent_version"] == 1
    assert created["request"] == message
    assert created["actor_id"] == ACTOR_ID
    assert created["actor_role"] is Role.OPERATOR
    assert route["requested_mode"] == mode.value
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
    assert route["role_planning_intent_version"] == 1


def test_runtime_budget_refresh_does_not_upgrade_legacy_role_planning() -> None:
    legacy: dict[str, object] = {"project_scale": "medium"}
    refreshed = _with_runtime_token_budget_policy(
        legacy, configured_tokens=100_000, mode=TaskMode.DISPATCH,
    )
    assert "role_planning_intent_version" not in refreshed
    assert legacy == {"project_scale": "medium"}
