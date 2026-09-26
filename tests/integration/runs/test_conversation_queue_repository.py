from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import RunOutboxRow, RunRow
from agent_hub.domain.runs import ConversationQueueStatus, RunStatus, TaskMode
from agent_hub.runs.conversation_queue import (
    ConversationQueueConflict,
    ConversationQueueRepository,
    EnqueueConversationMessage,
)


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id,
    request: str,
    blocked_by_run_id=None,
    status: RunStatus = RunStatus.QUEUED,
    worker_lease_expires_at=None,
) -> RunRow:
    row = RunRow(
        id=uuid4(),
        tenant_id=tenant_id,
        actor_id=uuid4(),
        request=request,
        mode=TaskMode.DIRECT.value,
        status=status.value,
        routing_decision={"conversation_id": "conv-queue"},
        blocked_by_run_id=blocked_by_run_id,
        worker_lease_expires_at=worker_lease_expires_at,
        version=1,
    )
    async with session_factory() as session, session.begin():
        session.add(row)
        await session.flush()
    return row


@pytest.mark.asyncio
async def test_enqueue_is_ordered_idempotent_and_tenant_scoped(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    predecessor = await _run(auth_session_factory, tenant_id=tenant_id, request="running")
    first_successor = await _run(
        auth_session_factory,
        tenant_id=tenant_id,
        request="first",
        blocked_by_run_id=predecessor.id,
    )
    second_successor = await _run(
        auth_session_factory,
        tenant_id=tenant_id,
        request="second",
        blocked_by_run_id=predecessor.id,
    )
    other_predecessor = await _run(
        auth_session_factory, tenant_id=other_tenant_id, request="other-running"
    )
    other_successor = await _run(
        auth_session_factory,
        tenant_id=other_tenant_id,
        request="other",
        blocked_by_run_id=other_predecessor.id,
    )
    repository = ConversationQueueRepository(auth_session_factory)

    first = await repository.enqueue(
        EnqueueConversationMessage(
            tenant_id=tenant_id,
            conversation_id="conv-queue",
            predecessor_run_id=predecessor.id,
            successor_run_id=first_successor.id,
            message="first",
            attachments=({"id": "attachment-1"},),
            references={"reference_conversation_id": "conv-old"},
            idempotency_key="queue-1",
        )
    )
    duplicate = await repository.enqueue(
        EnqueueConversationMessage(
            tenant_id=tenant_id,
            conversation_id="conv-queue",
            predecessor_run_id=predecessor.id,
            successor_run_id=first_successor.id,
            message="ignored duplicate",
            idempotency_key="queue-1",
        )
    )
    second = await repository.enqueue(
        EnqueueConversationMessage(
            tenant_id=tenant_id,
            conversation_id="conv-queue",
            predecessor_run_id=predecessor.id,
            successor_run_id=second_successor.id,
            message="second",
            idempotency_key="queue-2",
        )
    )
    other = await repository.enqueue(
        EnqueueConversationMessage(
            tenant_id=other_tenant_id,
            conversation_id="conv-queue",
            predecessor_run_id=other_predecessor.id,
            successor_run_id=other_successor.id,
            message="other",
            idempotency_key="queue-1",
        )
    )

    assert duplicate.id == first.id
    assert (first.position, second.position, other.position) == (1, 2, 1)
    assert first.attachments == ({"id": "attachment-1"},)
    assert first.references == {"reference_conversation_id": "conv-old"}
    assert second.predecessor_run_id == first_successor.id
    assert [item.message for item in await repository.list_for_conversation(tenant_id, "conv-queue")] == [
        "first",
        "second",
    ]


@pytest.mark.asyncio
async def test_edit_and_cancel_update_the_same_blocked_successor(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    predecessor = await _run(auth_session_factory, tenant_id=tenant_id, request="running")
    successor = await _run(
        auth_session_factory,
        tenant_id=tenant_id,
        request="before",
        blocked_by_run_id=predecessor.id,
    )
    repository = ConversationQueueRepository(auth_session_factory)
    item = await repository.enqueue(
        EnqueueConversationMessage(
            tenant_id=tenant_id,
            conversation_id="conv-queue",
            predecessor_run_id=predecessor.id,
            successor_run_id=successor.id,
            message="before",
            attachments=({"id": "keep-me"},),
            references={"reference_conversation_id": "conv-old"},
            idempotency_key="queue-edit",
        )
    )

    edited = await repository.edit(
        tenant_id,
        item.id,
        expected_version=1,
        message="after",
    )
    with pytest.raises(ConversationQueueConflict, match="version"):
        await repository.edit(
            tenant_id,
            item.id,
            expected_version=1,
            message="stale",
        )
    cancelled = await repository.cancel(
        tenant_id,
        item.id,
        expected_version=edited.version,
    )

    async with auth_session_factory() as session:
        successor_row = await session.scalar(select(RunRow).where(RunRow.id == successor.id))
    assert edited.message == "after"
    assert edited.attachments == ({"id": "keep-me"},)
    assert cancelled.status is ConversationQueueStatus.CANCELLED
    assert successor_row is not None
    assert successor_row.request == "after"
    assert successor_row.status == RunStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_redirect_promotes_selected_item_and_preserves_other_queue_items(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    active = await _run(
        auth_session_factory,
        tenant_id=tenant_id,
        request="active",
        status=RunStatus.RUNNING,
        worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    first_run = await _run(
        auth_session_factory,
        tenant_id=tenant_id,
        request="first",
        blocked_by_run_id=active.id,
    )
    selected_run = await _run(
        auth_session_factory,
        tenant_id=tenant_id,
        request="selected",
        blocked_by_run_id=first_run.id,
    )
    last_run = await _run(
        auth_session_factory,
        tenant_id=tenant_id,
        request="last",
        blocked_by_run_id=selected_run.id,
    )
    repository = ConversationQueueRepository(auth_session_factory)

    first = await repository.enqueue(
        EnqueueConversationMessage(
            tenant_id=tenant_id,
            conversation_id="conv-queue",
            predecessor_run_id=active.id,
            successor_run_id=first_run.id,
            message="first",
            idempotency_key="redirect-first",
        )
    )
    selected = await repository.enqueue(
        EnqueueConversationMessage(
            tenant_id=tenant_id,
            conversation_id="conv-queue",
            predecessor_run_id=first_run.id,
            successor_run_id=selected_run.id,
            message="selected",
            idempotency_key="redirect-selected",
        )
    )
    last = await repository.enqueue(
        EnqueueConversationMessage(
            tenant_id=tenant_id,
            conversation_id="conv-queue",
            predecessor_run_id=selected_run.id,
            successor_run_id=last_run.id,
            message="last",
            idempotency_key="redirect-last",
        )
    )

    redirected = await repository.redirect(
        tenant_id,
        selected.id,
        expected_version=selected.version,
    )
    queue = await repository.list_for_conversation(tenant_id, "conv-queue")

    assert redirected.status is ConversationQueueStatus.REDIRECTING
    assert [item.id for item in queue] == [selected.id, first.id, last.id]
    assert [item.status for item in queue] == [
        ConversationQueueStatus.REDIRECTING,
        ConversationQueueStatus.QUEUED,
        ConversationQueueStatus.QUEUED,
    ]
    assert queue[0].predecessor_run_id == active.id
    assert queue[1].predecessor_run_id == selected_run.id
    assert queue[2].predecessor_run_id == first_run.id

    released = await repository.release_next_for_terminal_run(tenant_id, active.id)
    assert released is not None
    assert released.id == selected.id
    assert released.status is ConversationQueueStatus.RELEASED


@pytest.mark.asyncio
async def test_release_waiting_successor_unblocks_without_execution_outbox(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    predecessor = await _run(
        auth_session_factory,
        tenant_id=tenant_id,
        request="active",
        status=RunStatus.RUNNING,
    )
    successor = await _run(
        auth_session_factory,
        tenant_id=tenant_id,
        request="needs approval",
        blocked_by_run_id=predecessor.id,
        status=RunStatus.WAITING_APPROVAL,
    )
    repository = ConversationQueueRepository(auth_session_factory)
    item = await repository.enqueue(
        EnqueueConversationMessage(
            tenant_id=tenant_id,
            conversation_id="conv-queue",
            predecessor_run_id=predecessor.id,
            successor_run_id=successor.id,
            message="needs approval",
            idempotency_key="waiting-approval",
        )
    )
    async with auth_session_factory() as session, session.begin():
        predecessor_row = await session.scalar(select(RunRow).where(RunRow.id == predecessor.id))
        assert predecessor_row is not None
        predecessor_row.status = RunStatus.COMPLETED.value

    released = await repository.release_next_for_terminal_run(tenant_id, predecessor.id)

    async with auth_session_factory() as session:
        successor_row = await session.scalar(select(RunRow).where(RunRow.id == successor.id))
        outbox_count = await session.scalar(
            select(func.count()).select_from(RunOutboxRow).where(RunOutboxRow.run_id == successor.id)
        )
    assert released is not None
    assert released.id == item.id
    assert released.status is ConversationQueueStatus.RELEASED
    assert successor_row is not None
    assert successor_row.blocked_by_run_id is None
    assert successor_row.status == RunStatus.WAITING_APPROVAL.value
    assert outbox_count == 0
