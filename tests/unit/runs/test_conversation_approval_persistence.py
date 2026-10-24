from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.domain.runs import RunStatus
from agent_hub.runs.repository import RunConflict, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runs.workspace import workspace_selection
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_repository_approval_upserts import ApprovalProjectionSession
from tests.unit.runs.test_terminal_hooks import RuntimeCompletes


class GrantSession(ApprovalProjectionSession):
    def __init__(self) -> None:
        super().__init__()
        self.run.actor_role = Role.OPERATOR.value
        self.run.routing_decision = {**workspace_selection(
            project_id="project", session_id="main", permission_mode="default",
        ).routing_payload(), "conversation_id": "conversation-owned"}
        self.sources: list[tuple[object, object]] = []
        self.conversation_exists = True
        self.conversation_archived = False

    async def scalar(self, statement: Any) -> object:
        sql = str(statement)
        if "FROM agent_hub_run_checkpoints" in sql:
            return None
        if "FROM agent_hub_conversations" in sql:
            return (SimpleNamespace(archived_at=uuid4() if self.conversation_archived else None)
                    if self.conversation_exists else None)
        if "FROM agent_hub_run_approvals" in sql:
            params = statement.compile().params
            approval = next((a for a in self.approvals.values() if a.approval_id in params.values()), None)
            if approval is not None and "status_1" in params and approval.status != params["status_1"]:
                return None
            if len(statement.selected_columns) == 1:
                return approval.id if approval is not None else None
            return approval
        return await super().scalar(statement)

    async def execute(self, statement: Any) -> Any:
        if str(statement).startswith("SELECT"):
            assert "JOIN agent_hub_runs" in str(statement)
            assert self.run.tenant_id in statement.compile().params.values()
            return SimpleNamespace(all=lambda: self.sources)
        return await super().execute(statement)


def action(db: GrantSession) -> CapabilityRequest:
    assert db.run.actor_id is not None
    return CapabilityRequest(
        tenant_id=db.run.tenant_id, user_id=db.run.actor_id, run_id=db.run.id,
        agent_id="worker", capability="command", operation="run",
        resource="workspace/current/project-verification",
        arguments={"command": ["npm", "test"], "cwd": "."},
        idempotency_key="action-1", tool_origin="builtin",
    )


async def pending(db: GrantSession) -> tuple[RunRepository, CapabilityRequest, str]:
    repository = RunRepository(db)  # type: ignore[arg-type]
    request = action(db)
    result = await DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repository,
    ).invoke(request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.WAITING_APPROVAL and result.approval_id
    return repository, request, result.approval_id


@pytest.mark.parametrize("scope", ("once", "conversation"))
async def test_manual_scope_is_persisted_and_publicly_auditable_without_private_hashes(scope: str) -> None:
    db = GrantSession()
    repository, request, approval_id = await pending(db)
    assert "conversation_binding" in db.approvals[approval_id].payload
    await repository.approve_capability_and_enqueue(
        tenant_id=db.run.tenant_id, run_id=db.run.id, actor_id=db.run.actor_id,
        approval_id=approval_id, version=db.run.version, scope=scope,
    )
    stored = db.approvals[approval_id]
    assert stored.payload["decision_scope"] == scope
    events = await repository.capability_approval_events(db.run.tenant_id, db.run.id)
    assert events[-1]["payload"] == {"decision_scope": scope}
    assert "sha256" not in str(events) and "npm" not in str(events)
    db.run.status = RunStatus.RUNNING.value
    db.sources = [(stored, db.run)]
    assert await repository.is_conversation_capability_approved(request, role=Role.OPERATOR) is (
        scope == "conversation")
    assert not await repository.is_capability_approval_scope_approved(
        db.run.tenant_id, db.run.id, approval_scope=cast(str, stored.payload["approval_scope"]),
    )


async def test_conversation_scope_cannot_be_promoted_from_once_after_resolution() -> None:
    db = GrantSession()
    repository, _, approval_id = await pending(db)
    version = db.run.version
    await repository.approve_capability_and_enqueue(tenant_id=db.run.tenant_id, run_id=db.run.id,
        actor_id=db.run.actor_id, approval_id=approval_id, version=version)
    with pytest.raises(RunConflict):
        await repository.approve_capability_and_enqueue(tenant_id=db.run.tenant_id, run_id=db.run.id,
            actor_id=db.run.actor_id, approval_id=approval_id, version=version, scope="conversation")
    assert len(db.outbox) == 1


@pytest.mark.parametrize("change", ("actor", "tenant", "conversation", "workspace", "permissions", "action",
                                   "status", "origin", "source_scope", "source_context"))
async def test_durable_conversation_grant_fails_closed_at_each_reuse_boundary(change: str) -> None:
    db = GrantSession()
    repository, request, approval_id = await pending(db)
    await repository.approve_capability_and_enqueue(tenant_id=db.run.tenant_id, run_id=db.run.id,
        actor_id=db.run.actor_id, approval_id=approval_id, version=db.run.version, scope="conversation")
    stored = db.approvals[approval_id]
    from copy import deepcopy
    source = deepcopy(db.run)
    db.sources = [(stored, source)]
    db.run.status = RunStatus.RUNNING.value
    if change in {"actor", "tenant"}:
        setattr(db.run, {"actor": "actor_id", "tenant": "tenant_id"}[change], uuid4())
    elif change in {"conversation", "workspace", "permissions"}:
        key = {"conversation": "conversation_id", "workspace": "workspace_session_id",
               "permissions": "requested_permissions"}[change]
        assert db.run.routing_decision is not None
        db.run.routing_decision[key] = ["workspace.read"] if change == "permissions" else "other"
    elif change == "action":
        request = request.model_copy(update={"arguments": {"command": ["npm", "publish"]}})
    elif change == "origin":
        request = request.model_copy(update={"tool_origin": "plugin"})
    elif change == "status":
        db.run.status = RunStatus.CANCELLED.value
    elif change == "source_scope":
        stored.payload["decision_scope"] = "once"
    else:
        assert source.routing_decision is not None
        source.routing_decision["workspace_session_id"] = "other"
    assert not await repository.is_conversation_capability_approved(request, role=Role.OPERATOR)


@pytest.mark.parametrize("denied", (False, True))
async def test_fresh_gateway_reuses_same_conversation_action_but_never_overrides_deny(denied: bool) -> None:
    from copy import deepcopy
    db = GrantSession()
    repository, request, approval_id = await pending(db)
    await repository.approve_capability_and_enqueue(tenant_id=db.run.tenant_id, run_id=db.run.id,
        actor_id=db.run.actor_id, approval_id=approval_id, version=db.run.version, scope="conversation")
    source = deepcopy(db.run)
    db.sources = [(db.approvals[approval_id], source)]
    db.run.id = uuid4()
    db.run.status = RunStatus.RUNNING.value
    request = request.model_copy(update={"run_id": db.run.id, "idempotency_key": "next-run"})

    class Deny:
        def capability_policy_rules(self, tenant_id: UUID) -> tuple[CapabilityRule, ...]:
            return (CapabilityRule(tenant_id, Role.OPERATOR, None, "command", "run",
                                   "workspace/current/project-verification", PolicyEffect.DENY),)

    gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repository, plugin_policy_rules=Deny() if denied else None,
    )
    result = await gateway.invoke(request, role=Role.OPERATOR)
    assert result.status is (CapabilityStatus.DENIED if denied else CapabilityStatus.ALLOWED)
    assert len(db.outbox) == 1


@pytest.mark.parametrize("state", ("active", "missing", "archived", "changed_permissions", "legacy"))
async def test_public_session_availability_matches_real_resolution_gate(state: str) -> None:
    db = GrantSession()
    repository, _, approval_id = await pending(db)
    db.conversation_exists = state != "missing"
    db.conversation_archived = state == "archived"
    if state == "changed_permissions":
        assert db.run.routing_decision is not None
        db.run.routing_decision["requested_permissions"] = ["workspace.read"]
    if state == "legacy":
        db.approvals[approval_id].payload.pop("conversation_binding")
    events = await repository.capability_approval_events(db.run.tenant_id, db.run.id)
    assert events[-1]["conversation_approval_available"] is (state == "active")
    if state != "active":
        with pytest.raises(RunConflict):
            await repository.approve_capability_and_enqueue(tenant_id=db.run.tenant_id, run_id=db.run.id,
                actor_id=db.run.actor_id, approval_id=approval_id, version=db.run.version, scope="conversation")
        assert db.outbox == []


async def test_once_approval_cannot_be_reused_by_different_tool_origin() -> None:
    db = GrantSession()
    repository, request, approval_id = await pending(db)
    await repository.approve_capability_and_enqueue(tenant_id=db.run.tenant_id, run_id=db.run.id,
        actor_id=db.run.actor_id, approval_id=approval_id, version=db.run.version)
    db.run.status = RunStatus.RUNNING.value
    disguised = request.model_copy(update={"tool_origin": "plugin"})
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    assert (await gateway.invoke(disguised, role=Role.OPERATOR)).status is not CapabilityStatus.ALLOWED


@pytest.mark.parametrize("scope", ("once", "conversation"))
async def test_denied_approval_cannot_be_granted_or_enqueued_again(scope: str) -> None:
    db = GrantSession()
    repository, _, approval_id = await pending(db)
    await repository.reject_capability_approval(tenant_id=db.run.tenant_id, run_id=db.run.id,
        actor_id=db.run.actor_id, approval_id=approval_id, version=db.run.version)
    with pytest.raises(RunConflict):
        await repository.approve_capability_and_enqueue(tenant_id=db.run.tenant_id, run_id=db.run.id,
            actor_id=db.run.actor_id, approval_id=approval_id, version=db.run.version, scope=scope)
    assert db.run.status == RunStatus.CANCELLED.value and db.outbox == []


@pytest.mark.parametrize("state", ("active", "missing", "archived", "workspace_changed"))
async def test_existing_conversation_grant_cannot_bypass_conversation_lifecycle(state: str) -> None:
    db = GrantSession()
    repository, request, approval_id = await pending(db)
    await repository.approve_capability_and_enqueue(tenant_id=db.run.tenant_id, run_id=db.run.id,
        actor_id=db.run.actor_id, approval_id=approval_id, version=db.run.version, scope="conversation")
    db.sources = [(db.approvals[approval_id], db.run)]
    db.run.status = RunStatus.RUNNING.value
    db.conversation_exists = state != "missing"
    db.conversation_archived = state == "archived"
    if state == "workspace_changed":
        assert db.run.routing_decision is not None
        db.run.routing_decision["workspace_session_id"] = "other"
    assert await repository.is_conversation_capability_approved(request, role=Role.OPERATOR) is (state == "active")
    # Conversation grants must never bypass their lifecycle through the old exact shortcut.
    assert not await repository.is_capability_approval_approved(
        db.run.tenant_id, db.run.id,
        cast(str, db.approvals[approval_id].payload["approval_fingerprint"]), tool_origin="builtin",
    )
    result = await DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repository,
    ).invoke(request, role=Role.OPERATOR)
    assert (result.status is CapabilityStatus.ALLOWED) is (state == "active")


async def test_real_service_forwards_conversation_scope_to_durable_repository() -> None:
    db = GrantSession()
    repository, _, approval_id = await pending(db)
    assert db.run.actor_id is not None
    service = RunService(repository, runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
                         router=None, task_queue=None)  # type: ignore[arg-type]
    result = await service.approve_capability(tenant_id=db.run.tenant_id, run_id=db.run.id,
        actor_id=db.run.actor_id, approval_id=approval_id, version=db.run.version, scope="conversation")
    assert result.status is RunStatus.QUEUED
    assert db.approvals[approval_id].payload["decision_scope"] == "conversation"
    assert len(db.outbox) == 1


async def test_snapshot_binds_pending_id_and_current_run_version_after_checkpoint_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copy import deepcopy

    from agent_hub.domain.runs import TaskMode
    from agent_hub.runtime.contracts import RuntimeCheckpoint

    db = GrantSession()
    db.run.worker_lease_token = uuid4()
    repository, _, approval_id = await pending(db)
    original = deepcopy(db.approvals[approval_id].payload)
    requested_version = db.run.version
    db.run.version += 1

    async def checkpoint(*_args: object, **_kwargs: object) -> RuntimeCheckpoint:
        return RuntimeCheckpoint(id=uuid4(), runtime_type="crew", runtime_version="11",
            run_id=db.run.id, tenant_id=db.run.tenant_id, mode=TaskMode.DISPATCH,
            state={"tools": {"tool-key": {"status": "waiting_approval", "approval_id": approval_id}}})

    monkeypatch.setattr(repository, "latest_checkpoint", checkpoint)
    snapshot = await repository.capability_approval_snapshot(
        db.run.tenant_id, db.run.id, actor_id=cast(UUID, db.run.actor_id),
    )
    assert snapshot.ready and snapshot.conversation_approval_available
    assert snapshot.conversation_approval_id == approval_id
    assert snapshot.conversation_approval_version == db.run.version != requested_version
    assert snapshot.items[0]["sequence"] == requested_version
    assert db.approvals[approval_id].payload == original
    with pytest.raises(RunConflict):
        await repository.approve_capability_and_enqueue(tenant_id=db.run.tenant_id, run_id=db.run.id,
            actor_id=db.run.actor_id, approval_id=approval_id, version=requested_version, scope="conversation")
    assert db.outbox == []


@pytest.mark.parametrize("state", ("archived", "missing_checkpoint", "missing_pending", "terminal"))
async def test_snapshot_without_real_support_returns_no_session_binding(state: str) -> None:
    db = GrantSession()
    if state == "missing_checkpoint":
        db.run.worker_lease_token = uuid4()
    repository, _, _ = await pending(db)
    if state == "archived":
        db.conversation_archived = True
    elif state == "missing_pending":
        db.approvals.clear()
    elif state == "terminal":
        # A terminal run has no pending receipt in a valid persisted state.
        db.approvals.clear()
        db.run.status = RunStatus.CANCELLED.value
    snapshot = await repository.capability_approval_snapshot(
        db.run.tenant_id, db.run.id, actor_id=cast(UUID, db.run.actor_id),
    )
    assert not snapshot.conversation_approval_available
    assert snapshot.conversation_approval_id is None and snapshot.conversation_approval_version is None


async def test_public_approval_route_uses_one_bound_snapshot_not_split_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.api.routers.runs import RunServiceProtocol, run_capability_approvals
    from agent_hub.auth.models import AuthenticatedPrincipal

    db = GrantSession()
    repository, request, approval_id = await pending(db)

    async def unsafe_split_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("API must not combine independently read events and readiness")

    monkeypatch.setattr(repository, "capability_approval_events", unsafe_split_read)
    monkeypatch.setattr(repository, "capability_approval_ready", unsafe_split_read)
    service = RunService(repository, runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
                         router=None, task_queue=None)  # type: ignore[arg-type]
    result = await run_capability_approvals(request.run_id, cast(RunServiceProtocol, service),
        AuthenticatedPrincipal(tenant_id=request.tenant_id, user_id=request.user_id, role=Role.OPERATOR))
    assert result.ready and result.conversation_approval_available
    assert result.conversation_approval_id == approval_id
    assert result.conversation_approval_version == db.run.version


async def test_snapshot_reads_run_once_under_share_lock_and_rejects_other_actor() -> None:
    from sqlalchemy.dialects import postgresql

    from agent_hub.runs.repository import RunNotFound

    class RecordingGrantSession(GrantSession):
        def __init__(self) -> None:
            super().__init__()
            self.run_reads: list[str] = []

        async def scalar(self, statement: Any) -> object:
            if "FROM agent_hub_runs" in str(statement):
                self.run_reads.append(str(statement.compile(dialect=postgresql.dialect())))  # type: ignore[no-untyped-call]
            return await super().scalar(statement)

    db = RecordingGrantSession()
    repository, _, _ = await pending(db)
    db.run_reads.clear()
    snapshot = await repository.capability_approval_snapshot(
        db.run.tenant_id, db.run.id, actor_id=cast(UUID, db.run.actor_id))
    assert snapshot.conversation_approval_available
    assert len(db.run_reads) == 1 and "FOR SHARE" in db.run_reads[0]
    with pytest.raises(RunNotFound):
        await repository.capability_approval_snapshot(db.run.tenant_id, db.run.id, actor_id=uuid4())
