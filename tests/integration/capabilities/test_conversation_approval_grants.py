"""Real PostgreSQL approval persistence; no worker, tool executor or model calls."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.api.routers.runs import (
    RunCapabilityApprovalsResponse,
    RunServiceProtocol,
    run_capability_approvals,
)
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.db.models import (
    ConversationRow,
    RunApprovalRow,
    RunCheckpointRow,
    RunOutboxRow,
    RunRow,
    TenantRow,
    UserRow,
)
from agent_hub.db.session import build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunNotFound, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runs.workspace import PermissionMode, workspace_selection
from agent_hub.runtime.contracts import RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry

pytestmark = pytest.mark.integration

Scope = Literal["once", "conversation"]
_PRIVATE_ARGUMENT_MARKER = "SYNTHETIC-ARGUMENT-MUST-NOT-BE-PUBLIC"


class NeverInvokedRuntime:
    mode = TaskMode.DIRECT

    def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        raise AssertionError("approval persistence tests must not execute a runtime")

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("approval persistence tests must not invoke a runtime")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError("approval persistence tests must not invoke a runtime")

    async def cancel(self) -> None:
        raise AssertionError("approval persistence tests must not invoke a runtime")


class NeverInvokedQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        raise AssertionError("only the transactional outbox may be written by these tests")


@dataclass(frozen=True)
class PgGrantStore:
    database_url: str = field(repr=False)
    sessions: async_sessionmaker[AsyncSession] = field(repr=False)
    tenant_id: UUID
    actor_id: UUID
    other_actor_id: UUID
    other_tenant_id: UUID
    other_tenant_actor_id: UUID
    conversation_id: str
    other_conversation_id: str

    def repository(self) -> RunRepository:
        return RunRepository(self.sessions)

    @asynccontextmanager
    async def independent_repository(self) -> AsyncIterator[RunRepository]:
        database = build_database(self.database_url)
        try:
            yield RunRepository(database.session_factory)
        finally:
            await database.dispose()

    async def new_action(
        self, *, tenant_id: UUID | None = None, actor_id: UUID | None = None,
        role: Role = Role.OPERATOR, conversation_id: str | None = None, session_id: str = "main",
        permissions: tuple[str, ...] | None = None, permission_mode: PermissionMode = "default",
        sandbox_profile: str = "workspace_write",
    ) -> CapabilityRequest:
        route = workspace_selection(
            project_id="pg-approval-project", session_id=session_id,
            sandbox_profile=sandbox_profile, requested_permissions=permissions, permission_mode=permission_mode,
        ).routing_payload()
        record = await self.repository().create_run(
            tenant_id=tenant_id or self.tenant_id, actor_id=actor_id or self.actor_id, actor_role=role,
            request="Synthetic approval persistence test; never execute tools",
            mode=TaskMode.DIRECT, status=RunStatus.RUNNING, idempotency_key=f"pg-approval-{uuid4().hex}",
            routing_decision={**route, "conversation_id": conversation_id or self.conversation_id}, enqueue=False,
        )
        return CapabilityRequest(
            tenant_id=record.tenant_id, user_id=actor_id or self.actor_id, run_id=record.id,
            agent_id="worker", capability="command", operation="run",
            resource="workspace/current/project-verification",
            arguments={"command": ["npm", "test"], "cwd": ".", "test_marker": _PRIVATE_ARGUMENT_MARKER},
            idempotency_key="invocation-one", tool_origin="builtin",
        )

    async def set_run_status(self, request: CapabilityRequest, status: RunStatus) -> None:
        # Simulate worker lifecycle using only this test's synthetic run.
        async with self.sessions() as session, session.begin():
            await session.execute(update(RunRow).where(
                RunRow.tenant_id == request.tenant_id, RunRow.id == request.run_id,
            ).values(status=status.value))

    async def mutate_conversation(self, state: str) -> None:
        async with self.sessions() as session, session.begin():
            statement = (delete(ConversationRow) if state == "missing" else
                         update(ConversationRow).values(archived_at=datetime.now(UTC)))
            await session.execute(statement.where(
                ConversationRow.tenant_id == self.tenant_id,
                ConversationRow.conversation_id == self.conversation_id,
            ))

    async def count(self, model: type[RunApprovalRow | RunOutboxRow], run_id: UUID) -> int:
        async with self.sessions() as session:
            value = await session.scalar(select(func.count()).select_from(model).where(model.run_id == run_id))
            return int(value or 0)


@pytest.fixture
async def pg_grants(database_url: str) -> AsyncIterator[PgGrantStore]:
    """Use the parent integration migration fixture, but clean only our random tenants."""
    database = build_database(database_url)
    store = PgGrantStore(database_url, database.session_factory, uuid4(), uuid4(), uuid4(), uuid4(), uuid4(),
                         f"conv-pg-{uuid4().hex}", f"conv-pg-other-{uuid4().hex}")
    tenants = (store.tenant_id, store.other_tenant_id)
    try:
        async with store.sessions() as session, session.begin():
            session.add_all([TenantRow(id=tenant, slug=f"approval-pg-{tenant.hex}", name="Approval PG test")
                             for tenant in tenants])
            await session.flush()
            session.add_all([
                UserRow(id=store.actor_id, tenant_id=store.tenant_id, username="grant-actor", role="operator"),
                UserRow(id=store.other_actor_id, tenant_id=store.tenant_id, username="other-actor", role="operator"),
                UserRow(id=store.other_tenant_actor_id, tenant_id=store.other_tenant_id,
                        username="other-tenant-actor", role="operator"),
            ])
            for tenant, conversation in ((store.tenant_id, store.conversation_id),
                                         (store.tenant_id, store.other_conversation_id),
                                         (store.other_tenant_id, store.conversation_id)):
                session.add(ConversationRow(tenant_id=tenant, conversation_id=conversation,
                    title="Synthetic approval test", project_id="pg-approval-project", workspace_path="main"))
        yield store
    finally:
        try:
            async with store.sessions() as session, session.begin():
                # Run children, including approvals/outbox, have ON DELETE CASCADE.
                await session.execute(delete(RunRow).where(RunRow.tenant_id.in_(tenants)))
                await session.execute(delete(ConversationRow).where(ConversationRow.tenant_id.in_(tenants)))
                await session.execute(delete(UserRow).where(UserRow.tenant_id.in_(tenants)))
                await session.execute(delete(TenantRow).where(TenantRow.id.in_(tenants)))
        finally:
            await database.dispose()


def _gateway(repository: RunRepository) -> DefaultRuntimeCapabilityPolicyGateway:
    return DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)


async def _public(
    repository: RunRepository, request: CapabilityRequest, *, actor_id: UUID | None = None, role: Role = Role.OPERATOR,
) -> RunCapabilityApprovalsResponse:
    service = RunService(repository, runtime_registry=RuntimeRegistry((NeverInvokedRuntime(),)),
                         router=None, task_queue=NeverInvokedQueue())
    return await run_capability_approvals(request.run_id, cast(RunServiceProtocol, service),
        AuthenticatedPrincipal(tenant_id=request.tenant_id, user_id=actor_id or request.user_id, role=role))


async def _pending(store: PgGrantStore, request: CapabilityRequest, *, role: Role = Role.OPERATOR) -> str:
    result = await _gateway(store.repository()).invoke(request, role=role)
    assert result.status is CapabilityStatus.WAITING_APPROVAL
    assert result.approval_id is not None
    return result.approval_id


async def _approve(store: PgGrantStore, request: CapabilityRequest, approval_id: str, scope: Scope) -> None:
    repository = store.repository()
    current = await repository.get(request.tenant_id, request.run_id, actor_id=request.user_id)
    result = await repository.approve_capability_and_enqueue(
        tenant_id=request.tenant_id, run_id=request.run_id, actor_id=request.user_id,
        approval_id=approval_id, version=current.version, scope=scope,
    )
    assert result.status is RunStatus.QUEUED
    assert await store.count(RunOutboxRow, request.run_id) == 1


async def _grant(
    store: PgGrantStore, scope: Scope = "conversation", *, role: Role = Role.OPERATOR,
) -> tuple[CapabilityRequest, str]:
    request = await store.new_action(role=role)
    approval_id = await _pending(store, request, role=role)
    public = await _public(store.repository(), request, role=role)
    assert public.ready and public.conversation_approval_available
    assert public.conversation_approval_id == approval_id
    assert public.conversation_approval_version == (await store.repository().get(request.tenant_id, request.run_id)).version
    await _approve(store, request, approval_id, scope)
    return request, approval_id


async def test_conversation_grant_survives_fresh_repository_and_store(pg_grants: PgGrantStore) -> None:
    source, _ = await _grant(pg_grants)
    await pg_grants.set_run_status(source, RunStatus.COMPLETED)
    target = await pg_grants.new_action()
    async with pg_grants.independent_repository() as repository:
        assert await repository.is_conversation_capability_approved(target, role=Role.OPERATOR)
        assert (await _gateway(repository).invoke(target, role=Role.OPERATOR)).status is CapabilityStatus.ALLOWED
        assert (await repository.get(target.tenant_id, target.run_id)).status is RunStatus.RUNNING
    assert await pg_grants.count(RunApprovalRow, target.run_id) == 0
    assert await pg_grants.count(RunOutboxRow, target.run_id) == 0


@pytest.mark.parametrize("scope", ("once", "conversation"))
async def test_once_and_conversation_differ_for_same_action_in_new_run(pg_grants: PgGrantStore, scope: Scope) -> None:
    source, _ = await _grant(pg_grants, scope)
    await pg_grants.set_run_status(source, RunStatus.RUNNING)
    assert (await _gateway(pg_grants.repository()).invoke(source, role=Role.OPERATOR)).status is CapabilityStatus.ALLOWED
    await pg_grants.set_run_status(source, RunStatus.COMPLETED)
    target = await pg_grants.new_action()
    async with pg_grants.independent_repository() as repository:
        assert await repository.is_conversation_capability_approved(target, role=Role.OPERATOR) is (scope == "conversation")
        result = await _gateway(repository).invoke(target, role=Role.OPERATOR)
        assert result.status is (CapabilityStatus.ALLOWED if scope == "conversation" else CapabilityStatus.WAITING_APPROVAL)
        public = await _public(repository, source)
        assert not public.conversation_approval_available
        assert public.items[-1]["payload"] == {"decision_scope": scope}
        assert _PRIVATE_ARGUMENT_MARKER not in str(public.model_dump())
        assert "sha256" not in str(public.model_dump()) and "approval_origin" not in str(public.model_dump())


@pytest.mark.parametrize("scope", ("once", "conversation"))
async def test_once_and_conversation_differ_for_new_invocation_in_same_run(pg_grants: PgGrantStore, scope: Scope) -> None:
    source, _ = await _grant(pg_grants, scope)
    await pg_grants.set_run_status(source, RunStatus.RUNNING)
    next_invocation = source.model_copy(update={"idempotency_key": "invocation-two"})
    async with pg_grants.independent_repository() as repository:
        result = await _gateway(repository).invoke(next_invocation, role=Role.OPERATOR)
        assert result.status is (CapabilityStatus.ALLOWED if scope == "conversation" else CapabilityStatus.WAITING_APPROVAL)


@pytest.mark.parametrize("scope", ("once", "conversation", "legacy_once"))
@pytest.mark.parametrize("state", ("missing", "archived"))
async def test_exact_retry_respects_conversation_lifecycle_but_preserves_once(
    pg_grants: PgGrantStore, scope: str, state: str,
) -> None:
    request, approval_id = await _grant(pg_grants, "once" if scope == "legacy_once" else cast(Scope, scope))
    if scope == "legacy_once":
        async with pg_grants.sessions() as session, session.begin():
            approval = await session.scalar(select(RunApprovalRow).where(
                RunApprovalRow.tenant_id == request.tenant_id, RunApprovalRow.run_id == request.run_id,
                RunApprovalRow.approval_id == approval_id,
            ))
            assert approval is not None
            payload = dict(approval.payload)
            for key in ("decision_scope", "conversation_binding", "approval_origin"):
                payload.pop(key, None)
            resolution = payload["public_resolution"]
            assert isinstance(resolution, dict)
            payload["public_resolution"] = {**resolution, "payload": {}}
            approval.payload = payload
    await pg_grants.set_run_status(request, RunStatus.RUNNING)
    await pg_grants.mutate_conversation(state)
    # Same run, arguments AND retry key: this must traverse the ordinary exact
    # approval lookup before reaching the conversation-grant lookup.
    async with pg_grants.independent_repository() as repository:
        result = await _gateway(repository).invoke(request, role=Role.OPERATOR)
        assert result.status is (CapabilityStatus.WAITING_APPROVAL if scope == "conversation"
                                 else CapabilityStatus.ALLOWED)
        current = await repository.get(request.tenant_id, request.run_id)
        if scope == "conversation":
            assert current.status is RunStatus.WAITING_APPROVAL
            assert result.approval_id is not None and result.approval_id != approval_id
            public = await _public(repository, request)
            assert public.ready and not public.conversation_approval_available
        else:
            assert current.status is RunStatus.RUNNING and result.approval_id is None
    assert await pg_grants.count(RunApprovalRow, request.run_id) == (2 if scope == "conversation" else 1)
    assert await pg_grants.count(RunOutboxRow, request.run_id) == 1


@pytest.mark.parametrize("boundary", (
    "tenant", "actor", "role", "conversation", "workspace", "permissions", "permission_context",
    "read_only", "arguments", "origin",
))
async def test_conversation_grant_cannot_cross_persisted_scope(pg_grants: PgGrantStore, boundary: str) -> None:
    if boundary == "role":
        async with pg_grants.sessions() as session, session.begin():
            await session.execute(update(UserRow).where(UserRow.id == pg_grants.actor_id).values(role=Role.ADMIN.value))
    source, _ = await _grant(pg_grants, role=Role.ADMIN if boundary == "role" else Role.OPERATOR)
    if boundary == "role":
        async with pg_grants.sessions() as session, session.begin():
            await session.execute(update(UserRow).where(UserRow.id == pg_grants.actor_id).values(role=Role.OPERATOR.value))
    await pg_grants.set_run_status(source, RunStatus.COMPLETED)
    role = Role.OPERATOR
    target = await pg_grants.new_action(
        tenant_id=pg_grants.other_tenant_id if boundary == "tenant" else None,
        actor_id=(pg_grants.other_tenant_actor_id if boundary == "tenant" else
                  pg_grants.other_actor_id if boundary == "actor" else None),
        role=role, conversation_id=pg_grants.other_conversation_id if boundary == "conversation" else None,
        session_id="other" if boundary == "workspace" else "main",
        permissions=(("workspace.read", "workspace.write") if boundary == "permissions" else
                     ("workspace.read", "command.run") if boundary == "permission_context" else
                     ("workspace.read",) if boundary == "read_only" else None),
        permission_mode="read_only" if boundary == "read_only" else "default",
        sandbox_profile="read_only" if boundary == "read_only" else "workspace_write",
    )
    if boundary == "arguments":
        target = target.model_copy(update={"arguments": {"command": ["npm", "publish"], "cwd": "."}})
    if boundary == "origin":
        target = target.model_copy(update={"tool_origin": "plugin"})
    async with pg_grants.independent_repository() as repository:
        assert not await repository.is_conversation_capability_approved(target, role=role)
        result = await _gateway(repository).invoke(target, role=role)
        assert result.status is not CapabilityStatus.ALLOWED
    assert await pg_grants.count(RunOutboxRow, target.run_id) == 0


@pytest.mark.parametrize("state", ("missing", "archived"))
async def test_conversation_lifecycle_blocks_existing_grant_and_public_available(pg_grants: PgGrantStore, state: str) -> None:
    await _grant(pg_grants)
    await pg_grants.mutate_conversation(state)
    target = await pg_grants.new_action()
    async with pg_grants.independent_repository() as repository:
        assert not await repository.is_conversation_capability_approved(target, role=Role.OPERATOR)
        assert (await _gateway(repository).invoke(target, role=Role.OPERATOR)).status is CapabilityStatus.WAITING_APPROVAL
        public = await _public(repository, target)
        assert public.ready and not public.conversation_approval_available
    assert await pg_grants.count(RunOutboxRow, target.run_id) == 0


@pytest.mark.parametrize("state", ("active", "missing", "archived", "permissions", "unsupported", "legacy", "unknown_version"))
async def test_public_available_matches_pending_submission_gate(pg_grants: PgGrantStore, state: str) -> None:
    request = await pg_grants.new_action()
    if state == "unsupported":
        request = request.model_copy(update={"tool_origin": "plugin"})
    approval_id = await _pending(pg_grants, request)
    if state in {"missing", "archived"}:
        await pg_grants.mutate_conversation(state)
    if state in {"permissions", "legacy", "unknown_version"}:
        async with pg_grants.sessions() as session, session.begin():
            if state == "permissions":
                run = await session.get(RunRow, request.run_id)
                assert run is not None and run.routing_decision is not None
                run.routing_decision = {**run.routing_decision, "requested_permissions": ["workspace.read"]}
            else:
                approval = await session.scalar(select(RunApprovalRow).where(
                    RunApprovalRow.tenant_id == request.tenant_id, RunApprovalRow.run_id == request.run_id,
                    RunApprovalRow.approval_id == approval_id,
                ))
                assert approval is not None
                payload = dict(approval.payload)
                if state == "legacy":
                    payload.pop("conversation_binding")
                else:
                    binding = payload["conversation_binding"]
                    assert isinstance(binding, dict)
                    payload["conversation_binding"] = {**binding, "schema_version": 2}
                approval.payload = payload
    async with pg_grants.independent_repository() as repository:
        public = await _public(repository, request)
        assert public.ready and public.conversation_approval_available is (state == "active")
        assert _PRIVATE_ARGUMENT_MARKER not in str(public.model_dump()) and "sha256" not in str(public.model_dump())
        before = await repository.get(request.tenant_id, request.run_id)
        if state == "active":
            await _approve(pg_grants, request, approval_id, "conversation")
        else:
            with pytest.raises(RunConflict):
                await repository.approve_capability_and_enqueue(tenant_id=request.tenant_id, run_id=request.run_id,
                    actor_id=request.user_id, approval_id=approval_id, version=before.version, scope="conversation")
            after = await repository.get(request.tenant_id, request.run_id)
            assert (after.status, after.version) == (before.status, before.version)
            assert await pg_grants.count(RunOutboxRow, request.run_id) == 0


async def test_current_policy_deny_precedes_durable_conversation_grant(pg_grants: PgGrantStore) -> None:
    await _grant(pg_grants)
    target = await pg_grants.new_action()

    class Deny:
        def capability_policy_rules(self, tenant_id: UUID) -> tuple[CapabilityRule, ...]:
            return (CapabilityRule(tenant_id, Role.OPERATOR, None, "command", "run",
                                   "workspace/current/project-verification", PolicyEffect.DENY),)

    async with pg_grants.independent_repository() as repository:
        assert await repository.is_conversation_capability_approved(target, role=Role.OPERATOR)
        gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository,
                                                       plugin_policy_rules=Deny())
        assert (await gateway.invoke(target, role=Role.OPERATOR)).status is CapabilityStatus.DENIED
    assert await pg_grants.count(RunApprovalRow, target.run_id) == 0
    assert await pg_grants.count(RunOutboxRow, target.run_id) == 0


async def test_foreign_actor_cannot_resolve_pending_conversation_approval(pg_grants: PgGrantStore) -> None:
    request = await pg_grants.new_action()
    approval_id = await _pending(pg_grants, request)
    repository = pg_grants.repository()
    before = await repository.get(request.tenant_id, request.run_id)
    with pytest.raises(RunNotFound):
        await repository.capability_approval_events(request.tenant_id, request.run_id, actor_id=pg_grants.other_actor_id)
    with pytest.raises(RunNotFound):
        await repository.approve_capability_and_enqueue(tenant_id=request.tenant_id, run_id=request.run_id,
            actor_id=pg_grants.other_actor_id, approval_id=approval_id, version=before.version, scope="conversation")
    after = await repository.get(request.tenant_id, request.run_id)
    assert (after.status, after.version) == (before.status, before.version)
    assert await pg_grants.count(RunOutboxRow, request.run_id) == 0


@pytest.mark.parametrize("scope", ("once", "conversation"))
async def test_rejected_approval_cannot_be_resolved_or_promoted(pg_grants: PgGrantStore, scope: Scope) -> None:
    request = await pg_grants.new_action()
    approval_id = await _pending(pg_grants, request)
    repository = pg_grants.repository()
    before = await repository.get(request.tenant_id, request.run_id)
    await repository.reject_capability_approval(tenant_id=request.tenant_id, run_id=request.run_id,
        actor_id=request.user_id, approval_id=approval_id, version=before.version)
    async with pg_grants.independent_repository() as fresh:
        with pytest.raises(RunConflict):
            await fresh.approve_capability_and_enqueue(tenant_id=request.tenant_id, run_id=request.run_id,
                actor_id=request.user_id, approval_id=approval_id, version=before.version, scope=scope)
        assert (await fresh.get(request.tenant_id, request.run_id)).status is RunStatus.CANCELLED
        assert not (await _public(fresh, request)).conversation_approval_available
    assert await pg_grants.count(RunOutboxRow, request.run_id) == 0


async def test_checkpoint_progress_returns_current_version_and_preserves_submit_fence(pg_grants: PgGrantStore) -> None:
    request = await pg_grants.new_action()
    approval_id = await _pending(pg_grants, request)
    repository = pg_grants.repository()
    before = await repository.get(request.tenant_id, request.run_id)
    checkpoint = RuntimeCheckpoint(id=uuid4(), runtime_type="crew", runtime_version="11",
        run_id=request.run_id, tenant_id=request.tenant_id, mode=TaskMode.DISPATCH,
        state={"tools": {"tool-key": {"status": "waiting_approval", "approval_id": approval_id}}})
    async with pg_grants.sessions() as session, session.begin():
        run = await session.get(RunRow, request.run_id)
        assert run is not None and run.routing_decision is not None
        run.mode = TaskMode.DISPATCH.value
        run.routing_decision = {**run.routing_decision, "approval_checkpoint_required": True}
        run.version += 1
        session.add(RunCheckpointRow(id=checkpoint.id, tenant_id=request.tenant_id, run_id=request.run_id,
            sequence=1, runtime_type=checkpoint.runtime_type, runtime_version=checkpoint.runtime_version,
            mode=checkpoint.mode.value, state=checkpoint.state, state_sha256=checkpoint.state_sha256,
            payload=checkpoint.to_payload()))
    async with pg_grants.independent_repository() as fresh:
        public = await _public(fresh, request)
        assert public.ready and public.conversation_approval_available
        assert public.conversation_approval_id == approval_id
        assert public.items[0]["sequence"] == before.version
        assert public.conversation_approval_version == before.version + 1
        with pytest.raises(RunConflict):
            await fresh.approve_capability_and_enqueue(tenant_id=request.tenant_id, run_id=request.run_id,
                actor_id=request.user_id, approval_id=approval_id, version=before.version, scope="conversation")
        assert await pg_grants.count(RunOutboxRow, request.run_id) == 0
        assert public.conversation_approval_version is not None
        after = await fresh.approve_capability_and_enqueue(tenant_id=request.tenant_id, run_id=request.run_id,
            actor_id=request.user_id, approval_id=approval_id, version=public.conversation_approval_version,
            scope="conversation")
        assert after.status is RunStatus.QUEUED
    assert await pg_grants.count(RunOutboxRow, request.run_id) == 1
