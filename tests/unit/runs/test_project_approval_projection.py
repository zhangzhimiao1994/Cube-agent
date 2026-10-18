from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from agent_hub.capabilities.approvals import _run_capability_scope
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.db.models import RunApprovalRow, RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, _public_plugin_approval_events


@pytest.fixture
def project_approval() -> tuple[RunRow, RunApprovalRow]:
    run = RunRow(
        id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), request="project approval",
        mode=TaskMode.DISPATCH.value, status=RunStatus.WAITING_APPROVAL.value,
        version=2, created_at=datetime.now(UTC), routing_decision={
            "approval_kind": "capability_tool", "approval_id": "approval-project",
            "approval_fingerprint": "PRIVATE-FINGERPRINT",
        },
    )
    approval = RunApprovalRow(
        id=uuid4(), tenant_id=run.tenant_id, run_id=run.id,
        approval_id="approval-project", action="capability_tool", status="pending",
        created_at=datetime.now(UTC), payload={
            "approval_fingerprint": "PRIVATE-FINGERPRINT",
            "arguments": {"path": "PRIVATE-PATH", "command": "PRIVATE-COMMAND"},
            "tool_name": "PRIVATE-TOOL", "source": "PRIVATE-SOURCE",
            "capability": "forged", "operation": "forged", "resource": "forged",
            "public_request": {
                "kind": "approval.requested", "sequence": 2, "run_id": str(run.id),
                "approval_id": "approval-project", "actor": "PRIVATE-ACTOR",
                "reason": "PRIVATE-REASON", "metadata": {"token": "PRIVATE-TOKEN"},
                "payload": {"run_id": str(run.id), "version": 2},
            },
        },
    )
    return run, approval


def _scope(run: RunRow, capability: str, operation: str, resource: str) -> str:
    request = CapabilityRequest(
        tenant_id=run.tenant_id, user_id=run.actor_id, run_id=run.id, agent_id="implementer",
        capability=capability, operation=operation, resource=resource,
        arguments={"token": "PRIVATE-TOKEN"}, idempotency_key="project-approval",
    )
    return _run_capability_scope(request, resource)


def _bind_scope(run: RunRow, approval: RunApprovalRow, scope: Any) -> None:
    approval.payload["approval_scope"] = scope
    run.routing_decision["approval_scope"] = scope


def _resolve(run: RunRow, approval: RunApprovalRow, status: str) -> None:
    if status == "pending":
        return
    approval.status = status
    run.status = RunStatus.RUNNING.value
    run.version = 5
    run.routing_decision = {}
    approval.payload["public_resolution"] = {
        "kind": "approval.resolved", "sequence": 3, "run_id": str(run.id),
        "approval_id": approval.approval_id, "decision": status,
        "payload": {"token": "PRIVATE-TOKEN"}, "metadata": "PRIVATE-METADATA",
    }


@pytest.mark.parametrize("status", ["pending", "approved", "rejected"])
@pytest.mark.parametrize("parts", [
    ("file", "write", "workspace/current"),
    ("command", "run", "workspace/current/project-verification"),
    ("file", "create", "workspace/bundle"),
])
def test_project_scope_projects_only_authoritative_six_fields(
    project_approval: tuple[RunRow, RunApprovalRow], status: str,
    parts: tuple[str, str, str],
) -> None:
    run, approval = project_approval
    scope = _scope(run, *parts)
    _bind_scope(run, approval, scope)
    _resolve(run, approval, status)
    original = deepcopy(approval.payload)

    events = _public_plugin_approval_events([approval], run)

    assert events[0]["payload"] == {
        "run_id": str(run.id), "version": 2, "capability": parts[0],
        "operation": parts[1], "resource": parts[2], "approval_scope": scope,
    }
    assert events[0]["actor"] == "capability_gateway"
    assert events[0]["reason"] == "capability requires approval"
    assert "metadata" not in events[0]
    assert "PRIVATE" not in str(events) and "forged" not in str(events)
    assert "tool_name" not in str(events)
    assert approval.payload == original
    if status != "pending":
        assert len(events) == 2
        assert events[1]["payload"] == {} and events[1]["decision"] == status


@pytest.mark.parametrize("status", ["pending", "approved"])
@pytest.mark.parametrize("parts", [
    ("file", "write", "workspace/current"),
    ("file", "create", "workspace/bundle"),
])
@pytest.mark.parametrize("mutation", [
    "tenant", "user", "run", "capability", "operation", "resource", "suffix",
    "prefix", "missing", "none", "dict", "list", "number", "no_actor",
])
def test_unproven_scope_keeps_original_minimal_binding(
    project_approval: tuple[RunRow, RunApprovalRow], mutation: str, status: str,
    parts: tuple[str, str, str],
) -> None:
    run, approval = project_approval
    scope: Any = _scope(run, *parts)
    if mutation in {"tenant", "user", "run"}:
        value = {"tenant": run.tenant_id, "user": run.actor_id, "run": run.id}[mutation]
        scope = scope.replace(f"{mutation}={value}", f"{mutation}={uuid4()}")
    elif mutation == "capability":
        scope = scope.replace("capability=file", "capability=command")
    elif mutation == "operation":
        scope = scope.replace(f"operation={parts[1]}", "operation=read")
    elif mutation == "resource":
        scope = scope.replace(f"resource={parts[2]}", "resource=workspace/other")
    elif mutation == "suffix":
        scope += ":token=PRIVATE-TOKEN"
    elif mutation == "prefix":
        scope = "PRIVATE-PREFIX" + scope
    elif mutation == "missing":
        scope = ""
    elif mutation == "none":
        scope = None
    elif mutation == "dict":
        scope = {"scope": scope}
    elif mutation == "list":
        scope = [scope]
    elif mutation == "number":
        scope = 7
    elif mutation == "no_actor":
        run.actor_id = None
    _bind_scope(run, approval, scope)
    _resolve(run, approval, status)

    events = _public_plugin_approval_events([approval], run)

    assert events[0]["payload"] == {"run_id": str(run.id), "version": 2}
    assert "PRIVATE" not in str(events)


@pytest.mark.parametrize("status", ["pending", "approved", "rejected"])
def test_plugin_binding_and_history_stay_unchanged(
    project_approval: tuple[RunRow, RunApprovalRow], status: str,
) -> None:
    run, approval = project_approval
    _bind_scope(run, approval, _scope(run, "file", "write", "workspace/current"))
    binding = {"plugin_id": "trusted-plugin", "capability_id": "calendar.create_event",
               "run_id": str(run.id), "version": 2}
    approval.payload["public_binding"] = dict(binding)
    approval.payload["public_request"]["payload"] = dict(binding)
    _resolve(run, approval, status)
    original = deepcopy(approval.payload)

    events = _public_plugin_approval_events([approval], run)

    assert events[0]["payload"] == binding
    assert approval.payload == original
    assert "PRIVATE" not in str(events)


@pytest.mark.parametrize("mutation", [
    "scope", "routing_scope_missing", "fingerprint", "version", "status",
    "approval_id", "tenant", "run", "request_run", "request_version",
    "binding_extension", "public_resolution", "public_request",
])
def test_pending_projection_rejects_mismatching_evidence(
    project_approval: tuple[RunRow, RunApprovalRow], mutation: str,
) -> None:
    run, approval = project_approval
    _bind_scope(run, approval, _scope(run, "file", "write", "workspace/current"))
    if mutation == "scope":
        run.routing_decision["approval_scope"] = _scope(
            run, "command", "run", "workspace/current/project-verification",
        )
    elif mutation == "routing_scope_missing":
        del run.routing_decision["approval_scope"]
    elif mutation == "fingerprint":
        run.routing_decision["approval_fingerprint"] = "different"
    elif mutation == "version":
        run.version += 1
    elif mutation == "status":
        run.status = RunStatus.RUNNING.value
    elif mutation == "approval_id":
        run.routing_decision["approval_id"] = "different"
    elif mutation == "tenant":
        approval.tenant_id = uuid4()
    elif mutation == "run":
        approval.run_id = uuid4()
    elif mutation == "request_run":
        approval.payload["public_request"]["run_id"] = str(uuid4())
    elif mutation == "request_version":
        approval.payload["public_request"]["payload"]["version"] = True
    elif mutation == "binding_extension":
        approval.payload["public_request"]["payload"]["tool_name"] = "PRIVATE-TOOL"
    elif mutation == "public_resolution":
        approval.payload["public_resolution"] = {}
    elif mutation == "public_request":
        del approval.payload["public_request"]

    with pytest.raises(RunConflict):
        _public_plugin_approval_events([approval], run)
