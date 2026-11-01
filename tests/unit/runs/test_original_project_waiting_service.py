"""Pre-model approval is a durable wait, not a runtime failure or cancellation."""

from copy import deepcopy
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.snapshot_approval_types_fixture import json_state
from tests.unit.runs.test_terminal_hooks import ExecutableFakeRepository, RuntimeCompletes


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("resolved", [None, "queued", "running", "cancelled"])
async def test_original_admission_wait_exits_service_without_runtime_failure(
    version: int, resolved: str | None, caplog: pytest.LogCaptureFixture,
) -> None:
    from agent_hub.runs.project_inputs import OriginalProjectApprovalPending

    preparation: dict[str, object] = {
        "schema_version": version, "phase": "captured",
        "reference": {"schema_version": 1, "contract_sha256": "a" * 64,
                      "input_source_sha256": "b" * 64, "file_count": 6},
    }
    if version == 2:
        preparation["python_recipe"] = {
            "schema_version": 1, "recipe_sha256": "c" * 64,
            "environment_input_sha256": "d" * 64,
        }
    route = {
        "project_input_contract_version": 1,
        "project_input": {"schema_version": 1, "kind": "managed_workspace", "editable_paths": ["src"]},
        "project_id": "project", "workspace_session_id": "session",
        "sandbox_profile": "workspace_write", "execution_backend": "systemd",
        "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
    }
    repository = ExecutableFakeRepository(routing_decision=route)
    repository.row.actor_role = Role.OPERATOR.value

    async def prepare(context: TaskContext) -> TaskContext:
        row = repository.row
        resource = "workspace/current/project-verification"
        if version == 2:
            resource += "/python/" + "c" * 64
        row.routing_decision = {
            **route, "project_input_preparation": deepcopy(preparation),
            "approval_kind": "capability_tool", "approval_id": "approval_" + "e" * 32,
            "approval_checkpoint_required": False,
            "approval_scope": (
                f"capability-scope:v1:tenant={row.tenant_id}:user={row.actor_id}:run={row.id}:"
                f"capability=command:operation=run:resource={resource}"
            ),
        }
        row.status = RunStatus.WAITING_APPROVAL.value
        RunRepository.clear_worker_lease(cast(Any, row))
        if resolved is not None:
            row.status = resolved
            row.routing_decision = {key: value for key, value in row.routing_decision.items()
                                    if not key.startswith('approval_')}
            if resolved == 'running':
                row.worker_id, row.worker_lease_token = 'replacement-worker', uuid4()
        raise OriginalProjectApprovalPending("approval_" + "e" * 32, json_state(preparation))

    service = RunService(cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)), router=None,
        task_queue=cast(Any, object()), project_input_preparer=prepare)
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus(resolved or 'waiting_approval')
    if resolved == 'running':
        assert repository.row.worker_id == 'replacement-worker'
    else:
        assert all(getattr(repository.row, field) is None for field in (
            "worker_id", "worker_lease_token", "worker_lease_expires_at", "worker_heartbeat_at"))
    assert not repository.artifacts
    assert not any(event.kind in {"runtime.failed", "runtime.completed", "model.started"}
                   for event in repository.event_log)
    assert "run_execute_failed" not in caplog.text
    assert "cleanup is not confirmed" not in caplog.text


@pytest.mark.parametrize("mutation", [None, "scope", "approval", "checkpoint", "phase", "recipe", "worker", "actor", "mode"])
def test_pending_original_approval_requires_exact_durable_capture(mutation: str | None) -> None:
    from agent_hub.runs.project_inputs import OriginalProjectApprovalPending

    preparation = {
        "schema_version": 2, "phase": "captured",
        "reference": {"schema_version": 1, "contract_sha256": "a" * 64,
                      "input_source_sha256": "b" * 64, "file_count": 6},
        "python_recipe": {"schema_version": 1, "recipe_sha256": "c" * 64,
                          "environment_input_sha256": "d" * 64},
    }
    route = {"project_input_contract_version": 1,
             "project_input": {"schema_version": 1, "kind": "managed_workspace", "editable_paths": ["src"]},
             "project_id": "project", "workspace_session_id": "session",
             "sandbox_profile": "workspace_write", "execution_backend": "systemd",
             "requested_permissions": ["workspace.read", "workspace.write", "command.run"]}
    repository = ExecutableFakeRepository(routing_decision=route)
    row = repository.row
    row.actor_role = Role.OPERATOR.value
    context = TaskContext(run_id=row.id, tenant_id=row.tenant_id, actor_id=row.actor_id,
                          actor_role=Role.OPERATOR, mode=TaskMode.DISPATCH,
                          request=row.request, routing_decision=cast(Any, route))
    row.status = RunStatus.WAITING_APPROVAL.value
    row.routing_decision = {
        **route, "project_input_preparation": deepcopy(preparation),
        "approval_kind": "capability_tool", "approval_id": "approval_" + "e" * 32,
        "approval_checkpoint_required": False,
        "approval_scope": (
            f"capability-scope:v1:tenant={row.tenant_id}:user={row.actor_id}:run={row.id}:"
            "capability=command:operation=run:resource=workspace/current/project-verification/python/" + "c" * 64),
    }
    pending = OriginalProjectApprovalPending("approval_" + "e" * 32, json_state(preparation))
    changed = cast(dict[str, Any], row.routing_decision)
    if mutation == "scope":
        changed["approval_scope"] = "workspace/current/project-verification"
    elif mutation == "approval":
        changed["approval_id"] = "approval_" + "f" * 32
    elif mutation == "checkpoint":
        changed["approval_checkpoint_required"] = True
    elif mutation == "phase":
        changed["project_input_preparation"]["phase"] = "executing"
    elif mutation == "recipe":
        changed["project_input_preparation"]["python_recipe"]["recipe_sha256"] = "f" * 64
    elif mutation == "worker":
        row.worker_id = "still-running"
    elif mutation == "actor":
        row.actor_role = Role.ADMIN.value
    elif mutation == "mode":
        row.mode = "direct"
    assert pending.matches(repository._record(), context) is (mutation is None)
