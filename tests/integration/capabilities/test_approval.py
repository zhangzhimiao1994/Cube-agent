from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalExpired,
    ApprovalMismatch,
    ApprovalRejected,
    ApprovalService,
    InMemoryApprovalStore,
    fingerprint_capability_request,
)
from agent_hub.capabilities.gateway import CapabilityGateway, CapabilityStatus
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.db.models import RunApprovalRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunRepository
from agent_hub.runtime.contracts import EventKind, RunEvent

TENANT_ID = UUID("33333333-3333-4333-8333-333333333333")
USER_ID = UUID("44444444-4444-4444-8444-444444444444")
OTHER_USER_ID = UUID("55555555-5555-4555-8555-555555555555")


@pytest.fixture
async def run_session_factory(
    database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from agent_hub.db.session import build_database
    from tests.integration.conftest import _clean_database

    database = build_database(database_url)
    try:
        await _clean_database(database.session_factory)
        yield database.session_factory
    finally:
        await _clean_database(database.session_factory)
        await database.dispose()


def external_message_request(
    run_id: UUID,
    *,
    user_id: UUID = USER_ID,
    resource: str = "external/team",
    arguments: dict[str, object] | None = None,
) -> CapabilityRequest:
    return CapabilityRequest(
        tenant_id=TENANT_ID,
        user_id=user_id,
        agent_id="dispatcher",
        capability="message",
        operation="send",
        resource=resource,
        arguments={"body": "hello"} if arguments is None else arguments,
        idempotency_key=f"message:{run_id}",
        run_id=run_id,
    )


def approval_policy() -> CapabilityPolicy:
    return CapabilityPolicy(
        (
            CapabilityRule(
                tenant_id=TENANT_ID,
                role=Role.OPERATOR,
                agent_id="dispatcher",
                capability="message",
                operation="send",
                resource_prefix="external",
                effect=PolicyEffect.REQUIRE_APPROVAL,
            ),
        )
    )


async def create_running_run(repository: RunRepository) -> UUID:
    record = await repository.create_run(
        tenant_id=TENANT_ID,
        actor_id=USER_ID,
        request="send a message",
        mode=TaskMode.DISPATCH,
        status=RunStatus.RUNNING,
        idempotency_key=f"run-{uuid4()}",
        enqueue=False,
    )
    return record.id


async def test_restricted_call_pauses_run_for_approval(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    approvals = ApprovalService(InMemoryApprovalStore())
    gateway = CapabilityGateway(approval_policy(), approvals, run_repository=repository)

    result = await gateway.invoke(external_message_request(run_id), role=Role.OPERATOR)
    run = await repository.get(TENANT_ID, run_id)

    assert result.status is CapabilityStatus.WAITING_APPROVAL
    assert result.approval_id is not None
    assert result.run_id == run_id
    assert run.status is RunStatus.WAITING_APPROVAL


async def test_approval_resumes_exact_waiting_run(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    approvals = ApprovalService(InMemoryApprovalStore())
    gateway = CapabilityGateway(approval_policy(), approvals, run_repository=repository)
    request = external_message_request(run_id)
    result = await gateway.invoke(request, role=Role.OPERATOR)

    resolved = await approvals.approve(result.approval_id or "", request, actor_id=USER_ID)
    run = await repository.get(TENANT_ID, run_id)

    assert resolved.status == "approved"
    assert run.status is RunStatus.RUNNING


async def test_approval_is_valid_only_for_exact_actor_resource_run_and_arguments(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    approvals = ApprovalService(InMemoryApprovalStore())
    gateway = CapabilityGateway(approval_policy(), approvals, run_repository=repository)
    request = external_message_request(run_id)
    result = await gateway.invoke(request, role=Role.OPERATOR)

    with pytest.raises(ApprovalMismatch, match="approval does not match request"):
        await approvals.approve(
            result.approval_id or "",
            external_message_request(run_id, arguments={"body": "changed"}),
            actor_id=USER_ID,
        )
    with pytest.raises(ApprovalMismatch, match="approval does not match request"):
        await approvals.approve(
            result.approval_id or "",
            external_message_request(run_id, resource="external/other"),
            actor_id=USER_ID,
        )
    with pytest.raises(ApprovalMismatch, match="approval does not match request"):
        await approvals.approve(result.approval_id or "", request, actor_id=OTHER_USER_ID)

    other_run_id = await create_running_run(repository)
    with pytest.raises(ApprovalMismatch, match="approval does not match request"):
        await approvals.approve(
            result.approval_id or "",
            external_message_request(other_run_id),
            actor_id=USER_ID,
        )
    run = await repository.get(TENANT_ID, run_id)
    assert run.status is RunStatus.WAITING_APPROVAL


async def test_expired_approval_cancels_call_and_run(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 6, tzinfo=UTC)

    def clock() -> datetime:
        return now

    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    approvals = ApprovalService(
        InMemoryApprovalStore(),
        expires_in=timedelta(seconds=1),
        now=clock,
    )
    gateway = CapabilityGateway(approval_policy(), approvals, run_repository=repository)
    request = external_message_request(run_id)
    result = await gateway.invoke(request, role=Role.OPERATOR)
    now = now + timedelta(seconds=2)

    with pytest.raises(ApprovalExpired, match="approval expired"):
        await approvals.approve(result.approval_id or "", request, actor_id=USER_ID)
    run = await repository.get(TENANT_ID, run_id)
    assert run.status is RunStatus.CANCELLED


async def test_unanswered_expired_approval_reaper_cancels_run(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 6, tzinfo=UTC)

    def clock() -> datetime:
        return now

    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    approvals = ApprovalService(
        InMemoryApprovalStore(),
        expires_in=timedelta(seconds=1),
        now=clock,
    )
    gateway = CapabilityGateway(approval_policy(), approvals, run_repository=repository)
    await gateway.invoke(external_message_request(run_id), role=Role.OPERATOR)
    now = now + timedelta(seconds=2)

    assert await approvals.expire_due() == 1
    run = await repository.get(TENANT_ID, run_id)
    assert run.status is RunStatus.CANCELLED


async def test_rejected_approval_cancels_call_and_run(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    approvals = ApprovalService(InMemoryApprovalStore())
    gateway = CapabilityGateway(approval_policy(), approvals, run_repository=repository)
    request = external_message_request(run_id)
    result = await gateway.invoke(request, role=Role.OPERATOR)

    with pytest.raises(ApprovalRejected, match="approval rejected"):
        await approvals.reject(result.approval_id or "", actor_id=USER_ID)
    run = await repository.get(TENANT_ID, run_id)

    assert run.status is RunStatus.CANCELLED


async def test_approval_cannot_resume_cancelled_waiting_run(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    approvals = ApprovalService(InMemoryApprovalStore())
    gateway = CapabilityGateway(approval_policy(), approvals, run_repository=repository)
    request = external_message_request(run_id)
    result = await gateway.invoke(request, role=Role.OPERATOR)
    record = await approvals.get(result.approval_id or "")
    assert record is not None
    await repository.resolve_capability_approval(
        TENANT_ID,
        run_id,
        RunStatus.CANCELLED,
        approval_id=record.id,
        approval_fingerprint=record.request_fingerprint,
    )

    with pytest.raises(RunConflict, match="run is not waiting for approval"):
        await approvals.approve(result.approval_id or "", request, actor_id=USER_ID)
    run = await repository.get(TENANT_ID, run_id)
    assert run.status is RunStatus.CANCELLED


async def test_old_approval_cannot_cancel_later_waiting_approval(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    approvals = ApprovalService(InMemoryApprovalStore())
    gateway = CapabilityGateway(approval_policy(), approvals, run_repository=repository)
    request = external_message_request(run_id)
    first = await gateway.invoke(request, role=Role.OPERATOR)
    await approvals.approve(first.approval_id or "", request, actor_id=USER_ID)
    second = await gateway.invoke(request, role=Role.OPERATOR)

    with pytest.raises(ApprovalMismatch, match="approval does not match request"):
        await approvals.reject(first.approval_id or "", actor_id=USER_ID)
    run = await repository.get(TENANT_ID, run_id)
    assert second.approval_id is not None
    assert run.status is RunStatus.WAITING_APPROVAL


async def test_replayed_approval_request_returns_existing_pending_approval(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    gateway = CapabilityGateway(
        approval_policy(),
        ApprovalService(InMemoryApprovalStore()),
        run_repository=repository,
    )
    request = external_message_request(run_id)

    first = await gateway.invoke(request, role=Role.OPERATOR)
    replayed = await gateway.invoke(request, role=Role.OPERATOR)

    assert first.status is CapabilityStatus.WAITING_APPROVAL
    assert replayed.status is CapabilityStatus.WAITING_APPROVAL
    assert replayed.approval_id == first.approval_id


async def test_capability_approval_upserts_refresh_row_action_for_review_and_resolution(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    gateway = CapabilityGateway(
        approval_policy(),
        ApprovalService(InMemoryApprovalStore()),
        run_repository=repository,
    )
    result = await gateway.invoke(external_message_request(run_id), role=Role.OPERATOR)
    approval_id = result.approval_id or ""

    async def approval_row() -> RunApprovalRow:
        async with run_session_factory() as session:
            return (
                await session.scalars(
                    select(RunApprovalRow).where(
                        RunApprovalRow.run_id == run_id,
                        RunApprovalRow.approval_id == approval_id,
                    )
                )
            ).one()

    pending = await approval_row()
    assert pending.action == "capability_tool"
    assert pending.status == "pending"

    await repository.record_capability_approval_review(
        TENANT_ID,
        run_id,
        approval_id=approval_id,
        approval_fingerprint=fingerprint_capability_request(external_message_request(run_id)),
        reviewer="auto_policy",
        reason="safe replay",
        status="approved",
    )
    reviewed = await approval_row()
    assert reviewed.action == "capability_tool.auto_review"
    assert reviewed.status == "approved"

    run = await repository.get(TENANT_ID, run_id)
    await repository.approve_capability_and_enqueue(
        tenant_id=TENANT_ID,
        run_id=run_id,
        approval_id=approval_id,
        version=run.version,
    )
    resolved = await approval_row()
    assert resolved.action == "capability_tool"
    assert resolved.status == "approved"


async def test_runtime_approval_event_upsert_refreshes_row_action_on_resolution(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    approval_id = "approval-event-upsert"

    async with await repository.run_transaction() as session, session.begin():
        await repository.persist_event(
            session,
            tenant_id=TENANT_ID,
            run_id=run_id,
            event=RunEvent(
                kind=EventKind.APPROVAL_REQUESTED,
                sequence=1,
                run_id=run_id,
                actor="main",
                approval_id=approval_id,
                action="publish_report",
                reason="operator approval required",
            ),
        )
        await repository.persist_event(
            session,
            tenant_id=TENANT_ID,
            run_id=run_id,
            event=RunEvent(
                kind=EventKind.APPROVAL_RESOLVED,
                sequence=2,
                run_id=run_id,
                actor="operator",
                approval_id=approval_id,
                decision="approved",
            ),
        )

    async with run_session_factory() as session:
        row = (
            await session.scalars(
                select(RunApprovalRow).where(
                    RunApprovalRow.run_id == run_id,
                    RunApprovalRow.approval_id == approval_id,
                )
            )
        ).one()

    assert row.action == "resolved"
    assert row.status == "approved"


async def test_approval_invocation_requires_running_run(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    record = await repository.create_run(
        tenant_id=TENANT_ID,
        actor_id=USER_ID,
        request="queued",
        mode=TaskMode.DISPATCH,
        status=RunStatus.QUEUED,
        idempotency_key=f"run-{uuid4()}",
        enqueue=False,
    )
    gateway = CapabilityGateway(
        approval_policy(),
        ApprovalService(InMemoryApprovalStore()),
        run_repository=repository,
    )

    with pytest.raises(RunConflict, match="run is not running"):
        await gateway.invoke(external_message_request(record.id), role=Role.OPERATOR)


async def test_allowed_and_denied_gateway_outcomes_are_stable(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    policy = CapabilityPolicy(
        (
            CapabilityRule(
                tenant_id=TENANT_ID,
                role=Role.OPERATOR,
                agent_id="dispatcher",
                capability="file",
                operation="read",
                resource_prefix="workspace/docs",
                effect=PolicyEffect.ALLOW,
            ),
        )
    )
    gateway = CapabilityGateway(policy, ApprovalService(InMemoryApprovalStore()), repository)

    allowed = await gateway.invoke(
        CapabilityRequest(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            agent_id="dispatcher",
            capability="file",
            operation="read",
            resource="workspace/docs/readme.md",
            arguments={},
            idempotency_key="readme",
            run_id=run_id,
        ),
        role=Role.OPERATOR,
    )
    denied = await gateway.invoke(external_message_request(run_id), role=Role.OPERATOR)
    run = await repository.get(TENANT_ID, run_id)

    assert allowed.status is CapabilityStatus.ALLOWED
    assert denied.status is CapabilityStatus.DENIED
    assert denied.reason == "capability denied"
    assert run.status is RunStatus.RUNNING


async def test_approved_capability_is_persisted_for_exact_replay(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    gateway = CapabilityGateway(
        approval_policy(),
        ApprovalService(InMemoryApprovalStore()),
        run_repository=repository,
    )
    pending_request = external_message_request(run_id)
    result = await gateway.invoke(pending_request, role=Role.OPERATOR)
    run = await repository.get(TENANT_ID, run_id)

    approved = await repository.approve_capability_and_enqueue(
        tenant_id=TENANT_ID,
        run_id=run_id,
        approval_id=result.approval_id or "",
        version=run.version,
    )

    assert approved.status is RunStatus.QUEUED
    assert await repository.is_capability_approval_approved(
        TENANT_ID,
        run_id,
        fingerprint_capability_request(pending_request),
    )
    assert not await repository.is_capability_approval_approved(
        TENANT_ID,
        run_id,
        fingerprint_capability_request(
            external_message_request(run_id, arguments={"body": "changed"})
        ),
    )


async def test_rejected_capability_cancels_waiting_run(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = RunRepository(run_session_factory)
    run_id = await create_running_run(repository)
    gateway = CapabilityGateway(
        approval_policy(),
        ApprovalService(InMemoryApprovalStore()),
        run_repository=repository,
    )
    result = await gateway.invoke(external_message_request(run_id), role=Role.OPERATOR)
    run = await repository.get(TENANT_ID, run_id)

    rejected = await repository.reject_capability_approval(
        tenant_id=TENANT_ID,
        run_id=run_id,
        approval_id=result.approval_id or "",
        version=run.version,
    )

    assert rejected.status is RunStatus.CANCELLED
