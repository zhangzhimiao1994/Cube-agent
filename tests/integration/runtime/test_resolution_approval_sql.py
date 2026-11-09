"""Actual PostgreSQL grant persistence; no model or Root execution is simulated."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.python_metadata_preparation import metadata_scope
from agent_hub.capabilities.python_resolution_authorization import original_resolution_scope
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.db.models import RunApprovalRow, RunCheckpointRow, RunOutboxRow, RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.contracts import JsonValue, RuntimeCheckpoint, TaskContext, _freeze_json
from agent_hub.skills.sandbox.python_registry_protocol import digest
from agent_hub.skills.sandbox.python_resolution_protocol import ProtectedOriginalResolutionSource


def approval_checkpoint(tenant: UUID, run_id: UUID, mode: TaskMode, approval_id: str) -> RuntimeCheckpoint:
    """Mode-specific durable boundary, without claiming a private continuation was consumed."""
    runtime_type, version = "direct_action", "2"
    state: dict[str, JsonValue] = {"phase": "concrete_action", "completed": False,
        "approval_id": approval_id, "tool_name": "project.verify", "context_sha256": "a" * 64,
        "elapsed_seconds": 1, "timeout_seconds": 120,
        "reference": {"version": 1, "content_sha256": "b" * 64, "total_bytes": 1,
            "segments": ({"id": str(uuid4()), "sha256": "c" * 64},)}}
    if mode is TaskMode.DISPATCH:
        runtime_type, version = "crew", "11"
        state = {"tools": {"resolution": {"status": "waiting_approval", "approval_id": approval_id}}}
    elif mode in {TaskMode.DISCUSS, TaskMode.HYBRID}:
        arguments_hash = hashlib.sha256(b"{}").hexdigest()
        identity = f"{run_id}:engineer:0:project.verify:{arguments_hash}"
        state = {"terminal": False, "tool_ledger": ({"status": "waiting_approval", "approval_id": approval_id,
            "replay_safe": False, "actor": "engineer", "name": "project.verify", "position": 0,
            "arguments_hash": arguments_hash, "idempotency_key": hashlib.sha256(identity.encode()).hexdigest()},)}
        runtime_type, version = "autogen", "2"
        if mode is TaskMode.HYBRID:
            from agent_hub.runtime.hybrid import HybridPlan

            child = RuntimeCheckpoint(id=uuid4(), tenant_id=tenant, run_id=run_id, mode=TaskMode.DISCUSS,
                runtime_type=runtime_type, runtime_version=version, state=state)
            state = {"terminal": False, "next_stage": 1, "plan_digest": HybridPlan().digest,
                "child_checkpoint": _freeze_json(child.to_payload())}
            runtime_type, version = "hybrid", "3"
    checkpoint = RuntimeCheckpoint(id=uuid4(), tenant_id=tenant, run_id=run_id, mode=mode,
        runtime_type=runtime_type, runtime_version=version, state=state)
    assert checkpoint_waits_for_approval(checkpoint, approval_id)
    return checkpoint


@pytest.mark.parametrize("mode", [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
@pytest.mark.parametrize("grant", ["once", "automatic"])
async def test_resolution_grant_survives_real_sql_and_rejects_scope_drift(
    auth_session_factory: async_sessionmaker[AsyncSession], mode: TaskMode, grant: str,
) -> None:
    factory = auth_session_factory
    tenant, run_id, actor, token = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    route: dict[str, JsonValue] = {
        "conversation_id": "resolution-sql-" + run_id.hex,
        "project_id": "resolution-project", "workspace_session_id": "main",
        "generated_project_contract_version": 1,
        "python_dependency_preflight_version": 1,
        "python_dependency_preflight_submission_version": 1,
        "sandbox_profile": "workspace_write", "execution_backend": "systemd",
        "requested_permissions": ("workspace.read", "workspace.write", "command.run"),
        "runtime_timeout_seconds": 120,
    }
    async with factory() as session, session.begin():
        session.add(RunRow(id=run_id, tenant_id=tenant, actor_id=actor, actor_role=Role.OPERATOR.value,
            request="Build a Python CLI with its required dependencies", mode=mode.value,
            status=RunStatus.RUNNING.value, version=1, routing_decision=route,
            worker_id="resolution-sql-worker", worker_lease_token=token,
            worker_lease_expires_at=now + timedelta(minutes=2), created_at=now))
    repository = RunRepository(factory)
    record = await repository.get(tenant, run_id)
    assert record is not None
    context = TaskContext(run_id=run_id, tenant_id=tenant, actor_id=actor, actor_role=Role.OPERATOR,
        execution_token=token, mode=mode, request=record.request, routing_decision=route, timeout_seconds=120)
    with bind_execution_authority(context, worker_id="resolution-sql-worker", actor="engineer"):
        scope = metadata_scope(record, actor="engineer", live=True)
        arguments: dict[str, object] = {
            "purpose": "python_dependency_resolution", "key": "a" * 64,
            "source_sha256": "b" * 64, "root_sha256": "c" * 64,
            "catalog_sha256": "d" * 64, "permissions_sha256": scope.permissions_sha256,
            "original_deadline": (now + timedelta(seconds=120)).isoformat(),
            "registry": "https://pypi.org", "artifact_host": "files.pythonhosted.org",
            "install_allowed": False, "requirements": ["click>=8"], "max_wheel_bytes": 64 * 1024**2,
            "environment": {"implementation_name": "cpython", "implementation_version": "3.12.14",
                "os_name": "posix", "platform_machine": "x86_64", "platform_release": "6.1",
                "platform_system": "Linux", "platform_version": "native-test",
                "platform_python_implementation": "CPython", "python_full_version": "3.12.14",
                "python_version": "3.12", "sys_platform": "linux"},
        }
        request = CapabilityRequest(tenant_id=tenant, run_id=run_id, user_id=actor, agent_id="engineer",
            capability="command", operation="run", resource="workspace/current/project-resolution/python/" + digest(arguments),
            arguments=arguments, idempotency_key="resolution-sql")
        approval_scope = capability_approval_scope(request)
        assert approval_scope is not None
        fingerprint = fingerprint_capability_request(request)
        assert not await repository.is_capability_approval_scope_approved(tenant, run_id, approval_scope)
        if grant == "automatic":
            approval_id = "auto_review_" + hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
            await repository.record_capability_approval_review(tenant, run_id, approval_id=approval_id,
                approval_fingerprint=fingerprint, reviewer="auto_review", reason="specific action accepted",
                status="approved", approval_scope=approval_scope, approval_request=request)
        else:
            approval_id = "approval_sql_once_" + run_id.hex
            waiting = await repository.begin_capability_approval(tenant, run_id, approval_id=approval_id,
                approval_fingerprint=fingerprint, approval_scope=approval_scope, actor_id=actor, approval_request=request)
            assert waiting.status is RunStatus.WAITING_APPROVAL
            checkpoint = approval_checkpoint(tenant, run_id, mode, approval_id)
            # The real recovery fence needs a durable checkpoint after the approval event.
            # This test checks SQL authority, not whether model continuation data was consumed.
            state = checkpoint.to_payload()["state"]
            assert isinstance(state, dict)
            async with factory() as session, session.begin():
                session.add(RunCheckpointRow(id=checkpoint.id, tenant_id=tenant, run_id=run_id,
                    sequence=100, mode=mode.value, runtime_type=checkpoint.runtime_type,
                    runtime_version=checkpoint.runtime_version, state=state,
                    state_sha256=checkpoint.state_sha256, payload=checkpoint.to_payload()))
            approved = await repository.approve_capability_and_enqueue(tenant_id=tenant, run_id=run_id,
                actor_id=actor, approval_id=approval_id, version=waiting.version)
            assert approved.status is RunStatus.QUEUED
            await repository.approve_capability_and_enqueue(tenant_id=tenant, run_id=run_id,
                actor_id=actor, approval_id=approval_id, version=waiting.version)
            async with factory() as session:
                count = await session.scalar(select(func.count()).select_from(RunOutboxRow).where(RunOutboxRow.run_id == run_id))
            assert count == 1
    assert await repository.is_capability_approval_scope_approved(tenant, run_id, approval_scope)
    assert await repository.is_capability_approval_approved(tenant, run_id, fingerprint)
    assert not await repository.is_capability_approval_scope_approved(uuid4(), run_id, approval_scope)
    assert not await repository.is_capability_approval_scope_approved(tenant, uuid4(), approval_scope)
    async with factory() as session:
        stored = await session.scalar(select(RunApprovalRow).where(RunApprovalRow.run_id == run_id,
            RunApprovalRow.approval_id == approval_id))
        assert stored is not None
        original_payload = dict(stored.payload)
    for defect in ("missing_binding", "stale_binding"):
        changed_payload = dict(original_payload)
        if defect == "missing_binding":
            changed_payload.pop("python_resolution_binding")
        else:
            binding = changed_payload["python_resolution_binding"]
            assert isinstance(binding, dict)
            stored_scope = binding["scope"]
            assert isinstance(stored_scope, dict)
            changed_payload["python_resolution_binding"] = {**binding,
                "scope": {**stored_scope, "session_id": "stale-session"}}
        async with factory() as session, session.begin():
            await session.execute(update(RunApprovalRow).where(RunApprovalRow.run_id == run_id,
                RunApprovalRow.approval_id == approval_id).values(payload=changed_payload))
        assert not await repository.is_capability_approval_approved(tenant, run_id, fingerprint)
        assert not await repository.is_capability_approval_scope_approved(tenant, run_id, approval_scope)
    async with factory() as session, session.begin():
        await session.execute(update(RunApprovalRow).where(RunApprovalRow.run_id == run_id,
            RunApprovalRow.approval_id == approval_id).values(payload=original_payload))
    async with factory() as session, session.begin():
        await session.execute(update(RunRow).where(RunRow.id == run_id).values(actor_id=uuid4()))
    assert not await repository.is_capability_approval_scope_approved(tenant, run_id, approval_scope)
    assert not await repository.is_capability_approval_approved(tenant, run_id, fingerprint)


async def test_expired_original_resolution_releases_wait_without_creating_work(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = auth_session_factory
    tenant, run_id, actor, token = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    deadline = now + timedelta(minutes=2)
    reference: dict[str, JsonValue] = {
        "schema_version": 1, "contract_sha256": "a" * 64,
        "input_source_sha256": "b" * 64, "file_count": 6,
    }
    route: dict[str, JsonValue] = {
        "conversation_id": "original-sql-" + run_id.hex,
        "project_id": "resolution-original", "workspace_session_id": "main",
        "project_input_contract_version": 1,
        "project_input": {"schema_version": 1, "kind": "managed_workspace",
                          "editable_paths": ("src/expense_cli/cli.py",)},
        "project_input_preparation": {"schema_version": 1, "phase": "captured", "reference": reference},
        "sandbox_profile": "workspace_write", "execution_backend": "systemd",
        "requested_permissions": ("workspace.read", "workspace.write", "command.run"),
        "runtime_timeout_seconds": 120,
    }
    async with factory() as session, session.begin():
        session.add(RunRow(id=run_id, tenant_id=tenant, actor_id=actor, actor_role=Role.OPERATOR.value,
            request="Repair a captured Python CLI", mode=TaskMode.DISCUSS.value, status=RunStatus.RUNNING.value,
            version=1, routing_decision=route, worker_id="original-sql-worker", worker_lease_token=token,
            worker_lease_expires_at=deadline, created_at=now))
    repository = RunRepository(factory)
    record = await repository.get(tenant, run_id)
    source = ProtectedOriginalResolutionSource.model_validate({
        "original_reference": reference, "protected_sha256": "c" * 64, "manifest_sha256": "d" * 64,
    }, strict=True)
    bound = original_resolution_scope(record, actor="main_agent", source=source, live=True)
    key = "e" * 64
    arguments: dict[str, object] = {
        "purpose": "python_dependency_resolution", "key": key,
        "source_sha256": source.source_sha256, "root_sha256": "f" * 64,
        "catalog_sha256": "a" * 64, "permissions_sha256": bound.permissions_sha256,
        "original_deadline": deadline.isoformat(), "registry": "https://pypi.org",
        "artifact_host": "files.pythonhosted.org", "install_allowed": False,
        "requirements": ["click<9,>=8.1"], "max_wheel_bytes": 64 * 1024**2,
        "protected_original": source.model_dump(mode="json"),
        "environment": {"implementation_name": "cpython", "implementation_version": "3.12.14",
            "os_name": "posix", "platform_machine": "x86_64", "platform_release": "6.1",
            "platform_system": "Linux", "platform_version": "native-test",
            "platform_python_implementation": "CPython", "python_full_version": "3.12.14",
            "python_version": "3.12", "sys_platform": "linux"},
    }
    request = CapabilityRequest(tenant_id=tenant, run_id=run_id, user_id=actor, agent_id="main_agent",
        capability="command", operation="run", resource="workspace/current/project-resolution/python/" + digest(arguments),
        arguments=arguments, idempotency_key="resolution-" + key)
    scope = capability_approval_scope(request)
    assert scope is not None
    approval_id = "approval_original_expiry_" + run_id.hex
    waiting = await repository.begin_capability_approval(tenant, run_id, approval_id=approval_id,
        approval_fingerprint=fingerprint_capability_request(request), approval_scope=scope,
        actor_id=actor, approval_request=request)
    assert waiting.status is RunStatus.WAITING_APPROVAL and waiting.worker_lease_token is None
    assert await repository.expire_original_python_resolution_approvals(limit=10, now=now) == 0

    later = deadline + timedelta(seconds=1)
    async with factory() as session, session.begin():
        locked = await session.scalar(select(RunRow).where(RunRow.id == run_id).with_for_update())
        assert locked is not None
        # The recovery sweep must not wait on another owner.
        assert await repository.expire_original_python_resolution_approvals(limit=10, now=later) == 0
    bad_run, bad_id = uuid4(), "invalid_approval_" + uuid4().hex
    async with factory() as session, session.begin():
        pending_row = await session.scalar(select(RunApprovalRow).where(RunApprovalRow.run_id == run_id))
        assert pending_row is not None and waiting.routing_decision is not None
        private_binding = pending_row.payload["python_resolution_binding"]
        assert isinstance(private_binding, dict)
        # This earlier row passes SQL's finite shape filters, but its private
        # scope/digest belongs to another run and therefore must never be retired.
        bad_payload = {**pending_row.payload, "python_resolution_binding": {**private_binding,
            "arguments": {**arguments, "original_deadline": (deadline - timedelta(seconds=1)).isoformat()}}}
        session.add(RunRow(id=bad_run, tenant_id=tenant, actor_id=actor, actor_role=Role.OPERATOR.value,
            request=record.request, mode=TaskMode.DISCUSS.value, status=RunStatus.WAITING_APPROVAL.value,
            version=waiting.version, created_at=now, routing_decision={**waiting.routing_decision, "approval_id": bad_id}))
        await session.flush()
        session.add(RunApprovalRow(id=uuid4(), tenant_id=tenant, run_id=bad_run,
            approval_id=bad_id, action="capability_tool", status="pending", payload=bad_payload, created_at=now))
    assert await repository.expire_original_python_resolution_approvals(limit=1, now=later) == 0
    assert await repository.expire_original_python_resolution_approvals(limit=1, now=later) == 1
    assert await repository.expire_original_python_resolution_approvals(limit=10, now=later) == 0
    terminal = await repository.get(tenant, run_id)
    assert terminal.status is RunStatus.FAILED and terminal.version == waiting.version + 1
    snapshot = await repository.capability_approval_snapshot(tenant, run_id, actor_id=actor)
    assert not snapshot.ready
    assert len(snapshot.items) == 2 and snapshot.items[0]["reason"] == "依赖审批已过期"
    assert snapshot.items[1]["payload"] == {"approval_expired": True}
    assert "decision" not in snapshot.items[1] and snapshot.items[1]["created_at"] == later
    assert (await repository.get(tenant, bad_run)).status is RunStatus.WAITING_APPROVAL
    async with factory() as session:
        row = await session.scalar(select(RunApprovalRow).where(RunApprovalRow.run_id == run_id))
        assert row is not None and row.status == "expired"
        private = row.payload["python_resolution_binding"]
        assert isinstance(private, dict) and private["arguments"] == arguments
        count = await session.scalar(select(func.count()).select_from(RunOutboxRow).where(RunOutboxRow.run_id == run_id))
    assert count == 0
