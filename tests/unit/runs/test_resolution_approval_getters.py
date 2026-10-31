"""Both public grant lookups must reject the same stale or incomplete approval."""
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.capabilities.python_metadata_preparation import metadata_scope
from agent_hub.db.models import RunApprovalRow, RunRow
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.repository import RunRepository


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("defect", ["none", "missing_binding", "session", "permissions", "actor",
    "tenant", "run", "status", "action", "decision_scope", "automatic_id", "missing_scope"])
async def test_fingerprint_and_scope_require_the_same_current_resolution_binding(
    monkeypatch: pytest.MonkeyPatch, automatic: bool, defect: str,
) -> None:
    run = RunRow(id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role="operator",
        request="Build a Python CLI", mode=TaskMode.DIRECT.value, status="running", version=1,
        created_at=datetime.now(UTC), routing_decision={"generated_project_contract_version": 1,
            "python_dependency_preflight_version": 1, "python_dependency_preflight_submission_version": 1,
            "project_id": "project", "workspace_session_id": "main", "sandbox_profile": "workspace_write",
            "execution_backend": "systemd", "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
            "runtime_timeout_seconds": 120})
    scope = (f"capability-scope:v1:tenant={run.tenant_id}:user={run.actor_id}:run={run.id}:"
             "capability=command:operation=run:resource=workspace/current/project-resolution/python/"
             + "a" * 64 + ":agent=engineer")
    fingerprint = "b" * 64
    bound = metadata_scope(RunRepository._record(run), actor="engineer")
    payload: dict[str, object] = {"approval_scope": scope, "approval_fingerprint": fingerprint,
        "python_resolution_binding": {"schema_version": 1, "scope": bound.model_dump(mode="json"),
            "resource": "workspace/current/project-resolution/python/" + "a" * 64}}
    if automatic:
        payload["reviewer"] = "auto_review"
    else:
        payload["decision_scope"] = "once"
    approval = RunApprovalRow(id=uuid4(), tenant_id=run.tenant_id, run_id=run.id, status="approved",
        action="capability_tool.auto_review" if automatic else "capability_tool",
        approval_id="auto_review_" + hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
        if automatic else "approval_once", payload=payload, created_at=datetime.now(UTC))
    expected = defect == "none" or defect == "automatic_id" and not automatic
    if defect == "missing_binding":
        payload.pop("python_resolution_binding")
    elif defect in {"session", "permissions"}:
        assert run.routing_decision is not None
        run.routing_decision = {**run.routing_decision, **({"workspace_session_id": "changed"}
            if defect == "session" else {"requested_permissions": ["workspace.read"]})}
    elif defect == "actor":
        run.actor_id = uuid4()
    elif defect == "tenant":
        approval.tenant_id = uuid4()
    elif defect == "run":
        approval.run_id = uuid4()
    elif defect == "status":
        approval.status = "pending"
    elif defect == "action":
        approval.action = "unrelated_action"
    elif defect == "decision_scope":
        payload["decision_scope"] = "conversation"
    elif defect == "automatic_id" and automatic:
        approval.approval_id = "auto_review_wrong"
    elif defect == "missing_scope":
        payload.pop("approval_scope")
    # Mock only SQL transport. The real DAO public methods and their binding validators run.
    monkeypatch.setattr(AsyncSession, "scalars", AsyncMock(return_value=SimpleNamespace(all=lambda: [approval])))
    monkeypatch.setattr(AsyncSession, "scalar", AsyncMock(return_value=run))
    repository = RunRepository(async_sessionmaker(expire_on_commit=False))
    assert await repository.is_capability_approval_approved(run.tenant_id, run.id, fingerprint) is expected
    assert await repository.is_capability_approval_scope_approved(run.tenant_id, run.id, scope) is expected
