"""Submission-bound action admission; real service with in-memory execution/storage boundaries."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.contracts import RuntimeCheckpoint, TaskContext
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.snapshot_approval_types_fixture import (
    CheckpointRepository as _CheckpointRepository,
)
from tests.unit.runs.snapshot_approval_types_fixture import untrusted_channel_submit
from tests.unit.runs.test_harness_decision import (
    ACTOR_ID,
    TENANT_ID,
    RecordingQueue,
    RecordingRepository,
)
from tests.unit.runs.test_terminal_hooks import ContextRecordingRuntime, ExecutableFakeRepository

SUBMISSION = "engineering_action_admission_submission_version"
ADMISSION = "engineering_action_admission_version"
MODES = (TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID)


def fresh_route() -> dict[str, Any]:
    return {
        SUBMISSION: 1, "source": "manual", "project_scale": "small",
        "project_delivery": "workspace", "artifact_strategy": "workspace_bundle",
        "project_id": "admission-project", "workspace_session_id": "admission-session",
        "sandbox_profile": "workspace_write", "permission_mode": "default",
        "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
    }


class RecordingRuntime(ContextRecordingRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.restored: list[RuntimeCheckpoint] = []

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        self.restored.append(checkpoint)


async def execute_policy(
    route: dict[str, Any], *, mode: TaskMode = TaskMode.DISPATCH,
    checkpoint: bool = False, recovery: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    before = deepcopy(route)
    repository_type = _CheckpointRepository if checkpoint else ExecutableFakeRepository
    repository = repository_type(routing_decision=route)
    repository.row.mode = mode.value
    repository.row.actor_role = Role.OPERATOR.value
    if recovery:
        repository.row.status = RunStatus.RUNNING.value
    runtime = RecordingRuntime()
    runtime.mode = mode
    service = RunService(cast(RunRepository, repository), runtime_registry=RuntimeRegistry((runtime,)),
        router=None, task_queue=cast(TaskQueue, object()))
    result = await service.execute(repository.run_id, allow_running_recovery=recovery)
    assert result.status is RunStatus.COMPLETED
    assert len(runtime.contexts) == 1
    context: TaskContext = runtime.contexts[0]
    assert context.mode is mode and context.run_id == repository.run_id
    assert context.actor_id == repository.row.actor_id and context.actor_role is Role.OPERATOR
    assert context.request == repository.row.request
    if checkpoint:
        assert len(runtime.restored) == 1 and context.checkpoint == runtime.restored[0]
        assert context.checkpoint.state == {} and context.checkpoint.runtime_type == "test"
    else:
        assert context.checkpoint is None and runtime.restored == []
    actual = context.to_payload()["routing_decision"]
    persisted = repository.row.routing_decision
    assert isinstance(actual, dict) and isinstance(persisted, dict)
    for key in ("project_id", "workspace_session_id", "requested_permissions", "permission_mode", "sandbox_profile"):
        if key in before:
            assert actual[key] == persisted[key] == before[key]
    if SUBMISSION in before:
        for decision in (actual, persisted):
            assert type(decision[SUBMISSION]) is type(before[SUBMISSION])
            assert decision[SUBMISSION] == before[SUBMISSION]
    else:
        assert SUBMISSION not in actual and SUBMISSION not in persisted
    assert route == before
    return actual, persisted


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("permission_mode", ["default", "auto_review"])
async def test_fresh_submission_selects_action_admission_only_for_engineering_modes(
    mode: TaskMode, permission_mode: str,
) -> None:
    route = fresh_route()
    route["permission_mode"] = permission_mode
    for decision in await execute_policy(route, mode=mode):
        assert type(decision.get(ADMISSION)) is int and decision[ADMISSION] == 1


@pytest.mark.parametrize("permission_mode", ["default", "auto_review"])
async def test_direct_does_not_select_action_admission(permission_mode: str) -> None:
    route = fresh_route()
    route["permission_mode"] = permission_mode
    for decision in await execute_policy(route, mode=TaskMode.DIRECT):
        assert ADMISSION not in decision


@pytest.mark.parametrize("marker", [None, True, False, 1.0, "1", 0, 2, {}])
async def test_malformed_submission_marker_cannot_select_action_admission(marker: Any) -> None:
    route = fresh_route()
    route[SUBMISSION] = marker
    for decision in await execute_policy(route):
        assert ADMISSION not in decision


@pytest.mark.parametrize("mode", MODES)
async def test_legacy_markers_cannot_replace_the_new_submission_marker(mode: TaskMode) -> None:
    route = fresh_route()
    del route[SUBMISSION]
    route.update({"engineering_diagnostic_submission_version": 1,
        "engineering_diagnostic_reads_submission_version": 1,
        "engineering_prerequisite_repair_submission_version": 1})
    for decision in await execute_policy(route, mode=mode):
        assert ADMISSION not in decision


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("old_action", [None, 0, 1, True, "1"])
async def test_any_preexisting_action_marker_prevents_an_upgrade(mode: TaskMode, old_action: Any) -> None:
    route = fresh_route()
    route["engineering_action_version"] = old_action
    for decision in await execute_policy(route, mode=mode):
        assert ADMISSION not in decision


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("checkpoint,recovery", [(True, False), (False, True), (True, True)])
async def test_checkpoint_or_running_recovery_does_not_select_new_action_admission(
    mode: TaskMode, checkpoint: bool, recovery: bool,
) -> None:
    for decision in await execute_policy(fresh_route(), mode=mode, checkpoint=checkpoint, recovery=recovery):
        assert ADMISSION not in decision


@pytest.mark.parametrize("field,value", [
    ("sandbox_profile", "read_only"), ("sandbox_profile", "restricted"), ("sandbox_profile", None),
    ("permission_mode", "full_access"), ("permission_mode", "read_only"),
    ("permission_mode", True), ("permission_mode", None),
    ("requested_permissions", []),
    ("requested_permissions", ["workspace.write", "command.run"]),
    ("requested_permissions", ["workspace.read", "command.run"]),
    ("requested_permissions", ["workspace.read", "workspace.write"]),
    ("requested_permissions", "workspace.read workspace.write command.run"),
    ("requested_permissions", None),
    ("requested_permissions", ["workspace.read", "workspace.write", "command.run", {}]),
    ("requested_permissions", ["workspace.read", "workspace.write", "command.run", []]),
    ("requested_permissions", ["workspace.read", "workspace.write", "command.run", True]),
])
async def test_insufficient_or_malformed_scope_cannot_select_action_admission(field: str, value: Any) -> None:
    route = fresh_route()
    route[field] = value
    for decision in await execute_policy(route):
        assert ADMISSION not in decision


@pytest.mark.parametrize("version", [None, 1, True, "1", 2])
async def test_existing_admission_marker_is_preserved_without_upgrade(version: Any) -> None:
    route = fresh_route()
    route.update({"engineering_action_version": 1, ADMISSION: version})
    for decision in await execute_policy(route, checkpoint=True):
        assert type(decision[ADMISSION]) is type(version) and decision[ADMISSION] == version


async def test_real_submit_persists_internal_submission_marker_without_selecting_execution_policy() -> None:
    repository = RecordingRepository()
    service = RunService(cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DISPATCH),)),
        router=None, task_queue=RecordingQueue())
    result = await untrusted_channel_submit(service, tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
        message="Inspect the existing project without changing execution permissions.", mode=TaskMode.DISPATCH,
        project_id="admission-project", workspace_session_id="admission-session",
        sandbox_profile="workspace_write", requested_permissions=("workspace.read", "workspace.write", "command.run"),
        channel_context={SUBMISSION: "0", ADMISSION: 1})
    assert result.status is RunStatus.QUEUED and len(repository.created) == 1
    created = repository.created[0]
    route = created["routing_decision"]
    assert isinstance(route, dict)
    assert type(route.get(SUBMISSION)) is int and route[SUBMISSION] == 1
    assert ADMISSION not in route
    assert route["sandbox_profile"] == "workspace_write"
    assert route["requested_permissions"] == ["workspace.read", "workspace.write", "command.run"]
    assert created["actor_id"] == ACTOR_ID and created["actor_role"] is Role.OPERATOR
