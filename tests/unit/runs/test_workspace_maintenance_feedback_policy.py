"""Fresh server-owned feedback version must not upgrade existing runs."""

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

KEY = "workspace_maintenance_feedback_version"


@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID))
def test_submit_persists_server_policy_without_new_permissions(mode: TaskMode) -> None:
    repository = RecordingRepository()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(mode),)),
        router=None, task_queue=RecordingQueue(),
    )
    message = "Fix existing production sources and deliver a ZIP."
    finish(service.submit(
        tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
        message=message, mode=mode, project_id="existing", workspace_session_id="session",
        sandbox_profile="read_only", requested_permissions=("workspace.read",),
        channel_context={KEY: "0"},
    ))
    record = repository.created[0]
    route = record["routing_decision"]
    assert isinstance(route, dict)
    assert type(route[KEY]) is int and route[KEY] == 1
    assert record["request"] == message and record["actor_id"] == ACTOR_ID
    assert route["sandbox_profile"] == "read_only"
    assert route["requested_permissions"] == ["workspace.read"]


def test_auto_submission_also_persists_server_policy() -> None:
    repository = ConversationModeRepository(None)
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)),
        router=None, task_queue=RecordingQueue(),
    )
    finish(service.submit(tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
                          message="Build a large website with persistent data.", mode=TaskMode.AUTO))
    route = repository.created[-1]["routing_decision"]
    assert isinstance(route, dict) and type(route[KEY]) is int and route[KEY] == 1


@pytest.mark.parametrize("version", (None, 0, 1))
def test_budget_refresh_never_backfills_policy(version: int | None) -> None:
    route: dict[str, object] = {"project_scale": "medium"}
    if version is not None:
        route[KEY] = version
    before = dict(route)
    refreshed = _with_runtime_token_budget_policy(route, configured_tokens=100_000, mode=TaskMode.DISPATCH)
    assert route == before and (KEY in refreshed) is (version is not None)
    if version is not None:
        assert type(refreshed[KEY]) is int and refreshed[KEY] == version
