"""Server-selected readonly catalog protocol; service/storage transports are offline."""

from __future__ import annotations

import hashlib
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.action_direct import _binding, _json
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.engineering_admission import _fingerprint
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_action_admission_policy import execute_policy
from tests.unit.runs.test_harness_decision import (
    ACTOR_ID,
    TENANT_ID,
    RecordingQueue,
    RecordingRepository,
)
from tests.unit.runtime.test_direct_project_verification import context

SUBMISSION = "engineering_catalog_reads_submission_version"
POLICY = "engineering_catalog_reads_version"


def fresh_route() -> dict[str, Any]:
    return {
        SUBMISSION: 1, "project_delivery": "workspace", "artifact_strategy": "workspace_bundle",
        "sandbox_profile": "workspace_write", "permission_mode": "default",
        "project_id": "catalog-project", "workspace_session_id": "catalog-session",
        "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
        "runtime_token_absolute_tokens": 4096, "runtime_token_soft_base_tokens": 2048,
        "runtime_timeout_seconds": 60.0, "runtime_timeout_absolute_seconds": 120.0,
        "runtime_timeout_soft_seconds": 30.0,
    }


async def test_submit_owns_catalog_marker_without_selecting_or_granting_execution() -> None:
    repository = RecordingRepository()
    service = RunService(cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None, task_queue=RecordingQueue())
    submitted = await service.submit(tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=Role.OPERATOR,
        message="Inspect the existing project.", mode=TaskMode.DIRECT,
        project_id="catalog-project", workspace_session_id="catalog-session",
        sandbox_profile="workspace_write", permission_mode="default",
        requested_permissions=("workspace.read",), channel_context={SUBMISSION: "0", POLICY: "1"})
    assert submitted.status is RunStatus.QUEUED and len(repository.created) == 1
    created = repository.created[0]
    route = created["routing_decision"]
    assert isinstance(route, dict)
    assert type(route.get(SUBMISSION)) is int and route[SUBMISSION] == 1
    assert POLICY not in route
    assert route["requested_permissions"] == ["workspace.read"]
    assert route["permission_mode"] == "default" and route["sandbox_profile"] == "workspace_write"
    assert created["actor_id"] == ACTOR_ID and created["actor_role"] is Role.OPERATOR


@pytest.mark.parametrize("permission", ["default", "auto_review"])
@pytest.mark.parametrize("permissions", [["workspace.read"], ["workspace.read", "workspace.write", "command.run"]])
async def test_fresh_direct_selects_catalog_without_other_markers_or_authority_growth(
    permission: str, permissions: list[str],
) -> None:
    route = fresh_route()
    route["permission_mode"] = permission
    route["requested_permissions"] = permissions
    control = {key: value for key, value in route.items() if key != SUBMISSION}
    before, _ = await execute_policy(control, mode=TaskMode.DIRECT)
    for decision in await execute_policy(route, mode=TaskMode.DIRECT):
        assert type(decision.get(POLICY)) is int and decision[POLICY] == 1
        assert {key: value for key, value in decision.items() if key not in {SUBMISSION, POLICY}} == before


@pytest.mark.parametrize("marker", [None, True, False, 1.0, "1", 0, 2, {}, []])
async def test_catalog_requires_exact_integer_submission_marker(marker: Any) -> None:
    route = fresh_route()
    route[SUBMISSION] = marker
    for decision in await execute_policy(route, mode=TaskMode.DIRECT):
        assert POLICY not in decision
        assert type(decision[SUBMISSION]) is type(marker) and decision[SUBMISSION] == marker


async def test_legacy_diagnostic_submission_is_not_catalog_authority() -> None:
    route = fresh_route()
    del route[SUBMISSION]
    route.update(engineering_diagnostic_reads_submission_version=1,
        engineering_prerequisite_repair_submission_version=1)
    for decision in await execute_policy(route, mode=TaskMode.DIRECT):
        assert SUBMISSION not in decision and POLICY not in decision


@pytest.mark.parametrize("old_action", [None, True, False, "1", 0, 1, 2, 1.0])
async def test_any_existing_action_marker_prevents_catalog_upgrade(old_action: Any) -> None:
    route = fresh_route()
    route["engineering_action_version"] = old_action
    for decision in await execute_policy(route, mode=TaskMode.DIRECT):
        assert POLICY not in decision


@pytest.mark.parametrize("mode", [TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_catalog_selection_is_direct_only(mode: TaskMode) -> None:
    for decision in await execute_policy(fresh_route(), mode=mode):
        assert POLICY not in decision


@pytest.mark.parametrize("checkpoint,recovery", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("existing", ["absent", None, True, "1", 0, 1, 2])
async def test_recovery_preserves_catalog_policy_presence_and_type(
    checkpoint: bool, recovery: bool, existing: Any,
) -> None:
    route = fresh_route()
    if existing != "absent":
        route[POLICY] = existing
    for decision in await execute_policy(route, mode=TaskMode.DIRECT, checkpoint=checkpoint, recovery=recovery):
        if existing == "absent":
            assert POLICY not in decision
        else:
            assert type(decision[POLICY]) is type(existing) and decision[POLICY] == existing


@pytest.mark.parametrize("field,value", [
    ("project_delivery", None), ("project_delivery", "chat"),
    ("artifact_strategy", None), ("artifact_strategy", "inline"),
    ("sandbox_profile", None), ("sandbox_profile", "read_only"),
    ("permission_mode", None), ("permission_mode", "full_access"),
    ("permission_mode", "read_only"), ("permission_mode", True),
])
async def test_catalog_requires_exact_workspace_and_permission_scope(field: str, value: Any) -> None:
    route = fresh_route()
    route[field] = value
    for decision in await execute_policy(route, mode=TaskMode.DIRECT):
        assert POLICY not in decision


def test_absent_catalog_policy_keeps_previous_action_binding_bytes() -> None:
    route = {"project_delivery": "workspace", "artifact_strategy": "workspace_bundle",
        "permission_mode": "default", "engineering_diagnostic_reads_version": 1,
        "engineering_prerequisite_repair_version": 1}
    ctx = context(routing_decision=route)
    expected = hashlib.sha256(_json({
        "scope": _fingerprint(ctx), "route": route, "contracts": {}, "guidance": {},
        "resolved_model": "main", "selected_model": {}, "instructions": None, "sources": [],
    })).hexdigest()
    assert _binding(ctx, "main") == expected


@pytest.mark.parametrize("changed", [None, True, False, "1", 0, 2, 1.0])
def test_catalog_binding_distinguishes_selected_protocol_from_other_types(changed: Any) -> None:
    ctx = context(routing_decision={POLICY: 1})
    modified = ctx.model_copy(update={"routing_decision": {POLICY: changed}})
    assert _binding(ctx, "main") != _binding(modified, "main")


@pytest.mark.parametrize("version", [None, True, False, "1", 0, 1, 2, 1.0])
def test_catalog_binding_distinguishes_explicit_marker_from_absence(version: Any) -> None:
    ctx = context(routing_decision={})
    modified = ctx.model_copy(update={"routing_decision": {POLICY: version}})
    assert _binding(ctx, "main") != _binding(modified, "main")


@pytest.mark.parametrize("field", ["run_id", "tenant_id", "actor_id", "actor_role", "project_id", "workspace_session_id"])
def test_selected_catalog_action_binding_remains_full_scope_bound(field: str) -> None:
    ctx = context(routing_decision=fresh_route() | {POLICY: 1})
    if field in {"project_id", "workspace_session_id"}:
        modified = ctx.model_copy(update={"routing_decision": dict(ctx.routing_decision) | {field: "foreign"}})
    else:
        modified = ctx.model_copy(update={field: Role.ADMIN if field == "actor_role" else uuid4()})
    assert _binding(ctx, "main") != _binding(modified, "main")
