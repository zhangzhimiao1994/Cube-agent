"""Real legacy factories retain admission; SQL/capacity/model transport are offline doubles."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.workspace import workspace_selection
from agent_hub.runtime import defaults
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from tests.unit.runs.test_repository_approval_upserts import ApprovalProjectionSession
from tests.unit.runtime.test_configured_runtime import (
    TENANT_ID,
    FakeCapabilityAvailability,
    FakeConfigService,
    FakeSecretService,
)
from tests.unit.runtime.test_discuss_repository_wiring import (
    OfflineCapacity,
    OfflineTransport,
    configured_document,
    response,
)


class NoExecutionBackend(FakeCapabilityAvailability):
    def __init__(self) -> None:
        super().__init__({"calculator", "read_context", "workspace.write_text", "project.inspect",
                          "workspace.list", "project.verify", "workspace.bundle"})
        self.effects = 0

    async def execute(self, **kwargs: Any) -> Any:
        self.effects += 1
        raise AssertionError("legacy admission must not execute a project tool")


def task(db: ApprovalProjectionSession, mode: TaskMode, permission_mode: str) -> TaskContext:
    db.run.tenant_id = TENANT_ID
    db.run.mode = mode.value
    db.run.actor_role = Role.OPERATOR.value
    db.run.routing_decision = {
        **workspace_selection(project_id="project", session_id="main",
            permission_mode=cast(Any, permission_mode),
            requested_permissions=("workspace.read", "workspace.write", "command.run"),
        ).routing_payload(),
        "conversation_id": "fixed-legacy-admission",
        "selected_agent_ids": ("analyst", "critic"), "main_agent_model": "main",
    }
    return TaskContext(run_id=db.run.id, tenant_id=TENANT_ID, actor_id=db.run.actor_id,
        actor_role=Role.OPERATOR, execution_token=uuid4(), mode=mode,
        request="Compare two explicit project implementation views.", token_budget=4096,
        timeout_seconds=10,
        routing_decision=cast(Mapping[str, JsonValue], db.run.routing_decision))


def factory(mode: TaskMode, db: ApprovalProjectionSession, transport: OfflineTransport,
            backend: NoExecutionBackend) -> tuple[Any, RunRepository]:
    repository = RunRepository(cast(Any, db))
    policy = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repository,
    )
    harness = HarnessToolGateway(backend, policy_gateway=policy)
    registry = defaults.configured_runtime_registry(
        config_service=cast(Any, FakeConfigService(configured_document())),
        secret_service=cast(Any, FakeSecretService()), redis_client=object(),
        transport=transport, capability_gateway=backend, harness_tool_gateway=harness,
        artifact_repository=InMemoryArtifactRepository(),
    )
    return registry.get(mode), repository


@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID))
@pytest.mark.parametrize("permission_mode", ("default", "auto_review"))
async def test_real_legacy_factory_publishes_approvable_checkpoint_before_model_or_tool(
    mode: TaskMode, permission_mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = ApprovalProjectionSession()
    context = task(db, mode, permission_mode)
    backend = NoExecutionBackend()
    transport = OfflineTransport([response("analyst"), response("[COMPLETE] Fixed offline reply.")])
    monkeypatch.setattr(defaults, "CapacityPool", OfflineCapacity)
    monkeypatch.setenv("AGENT_HUB_CREWAI_STORAGE_DIR", str(tmp_path / "crewai"))
    runtime, _ = factory(mode, db, transport, backend)

    try:
        events = [event async for event in runtime.run(context)]
    except RuntimeError:
        assert transport.requests == [], "legacy child billed a model before recoverable approval"
        raise

    assert transport.requests == [], "legacy child billed a model before recoverable approval"
    assert backend.effects == 0
    assert db.run.status == RunStatus.WAITING_APPROVAL.value
    checkpoint = next((event.checkpoint for event in events if event.checkpoint is not None), None)
    assert checkpoint is not None and checkpoint.runtime_type == "engineering_admission"
    approval_id = checkpoint.state["approval_id"]
    assert isinstance(approval_id, str) and checkpoint_waits_for_approval(checkpoint, approval_id)
    stored = db.approvals[approval_id]
    assert stored.status == "pending" and stored.payload["approval_origin"] == "builtin"
    assert db.run.routing_decision is not None
    assert stored.payload["approval_fingerprint"] == db.run.routing_decision["approval_fingerprint"]
    assert checkpoint.state["tool_name"] == "project.verify"


async def test_real_legacy_discuss_fresh_factory_resumes_existing_admission_after_exact_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = ApprovalProjectionSession()
    context = task(db, TaskMode.DISCUSS, "default")
    backend = NoExecutionBackend()
    first_transport = OfflineTransport([])
    monkeypatch.setattr(defaults, "CapacityPool", OfflineCapacity)
    first, repository = factory(TaskMode.DISCUSS, db, first_transport, backend)
    events = [event async for event in first.run(context)]
    assert first_transport.requests == []
    checkpoint = next((event.checkpoint for event in events if event.checkpoint is not None), None)
    assert checkpoint is not None and checkpoint.runtime_type == "engineering_admission"
    approval_id = checkpoint.state["approval_id"]
    assert isinstance(approval_id, str)
    await repository.approve_capability_and_enqueue(tenant_id=TENANT_ID, run_id=context.run_id,
        actor_id=context.actor_id, approval_id=approval_id, version=db.run.version)
    assert db.run.status == RunStatus.QUEUED.value
    db.run.status = RunStatus.RUNNING.value

    fresh_transport = OfflineTransport([response("analyst"), response("[COMPLETE] Fixed offline reply.")])
    fresh, _ = factory(TaskMode.DISCUSS, db, fresh_transport, backend)
    await fresh.restore_checkpoint(checkpoint)
    resumed = context.model_copy(update={"checkpoint": checkpoint, "execution_token": uuid4()})
    events = [event async for event in fresh.run(resumed)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(fresh_transport.completed) == 2 and not first_transport.requests
    assert backend.effects == 0 and len(db.approvals) == 1
