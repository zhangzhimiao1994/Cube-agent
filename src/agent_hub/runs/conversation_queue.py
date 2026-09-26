"""Durable ordered messages waiting behind an active conversation run."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import ConversationQueueItemRow, RunOutboxRow, RunRow
from agent_hub.domain.runs import ConversationQueueStatus, RunStatus


@dataclass(frozen=True, slots=True)
class EnqueueConversationMessage:
    tenant_id: UUID
    conversation_id: str
    predecessor_run_id: UUID
    successor_run_id: UUID
    message: str
    idempotency_key: str
    attachments: tuple[dict[str, object], ...] = ()
    references: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ConversationQueueItem:
    id: UUID
    tenant_id: UUID
    conversation_id: str
    predecessor_run_id: UUID
    successor_run_id: UUID
    message: str
    attachments: tuple[dict[str, object], ...]
    references: dict[str, object]
    position: int
    idempotency_key: str
    status: ConversationQueueStatus
    version: int
    failure_detail: str | None
    created_at: datetime
    updated_at: datetime


class ConversationQueueNotFound(RuntimeError):
    """The queue item does not exist in the requested tenant."""


class ConversationQueueConflict(RuntimeError):
    """The queue item can no longer accept the requested mutation."""


def _advisory_lock_key(tenant_id: UUID, conversation_id: str) -> int:
    digest = hashlib.sha256(f"{tenant_id}:{conversation_id}".encode()).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


class ConversationQueueRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def enqueue(self, command: EnqueueConversationMessage) -> ConversationQueueItem:
        conversation_id = command.conversation_id.strip()
        message = command.message.strip()
        idempotency_key = command.idempotency_key.strip()
        if not conversation_id:
            raise ValueError("conversation_id is required")
        if not message:
            raise ValueError("message is required")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        async with self._session_factory() as session, session.begin():
            await self._lock_conversation(session, command.tenant_id, conversation_id)
            existing = await session.scalar(
                select(ConversationQueueItemRow).where(
                    ConversationQueueItemRow.tenant_id == command.tenant_id,
                    ConversationQueueItemRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return self._item(existing)
            predecessor = await self._tenant_run(
                session, command.tenant_id, command.predecessor_run_id
            )
            successor = await self._tenant_run(session, command.tenant_id, command.successor_run_id)
            if predecessor.id == successor.id:
                raise ConversationQueueConflict("successor must differ from predecessor")
            if self._run_conversation_id(predecessor) != conversation_id:
                raise ConversationQueueConflict("predecessor belongs to another conversation")
            if self._run_conversation_id(successor) != conversation_id:
                raise ConversationQueueConflict("successor belongs to another conversation")
            if successor.blocked_by_run_id != command.predecessor_run_id:
                raise ConversationQueueConflict("successor is not blocked by predecessor")
            tail = await session.scalar(
                select(ConversationQueueItemRow)
                .where(
                    ConversationQueueItemRow.tenant_id == command.tenant_id,
                    ConversationQueueItemRow.conversation_id == conversation_id,
                    ConversationQueueItemRow.status.in_(
                        (
                            ConversationQueueStatus.QUEUED.value,
                            ConversationQueueStatus.REDIRECTING.value,
                            ConversationQueueStatus.RELEASED.value,
                            ConversationQueueStatus.RUNNING.value,
                        )
                    ),
                )
                .order_by(ConversationQueueItemRow.position.desc())
                .with_for_update()
            )
            effective_predecessor = predecessor
            if tail is not None and tail.successor_run_id != successor.id:
                effective_predecessor = await self._tenant_run(
                    session, command.tenant_id, tail.successor_run_id
                )
                successor.blocked_by_run_id = effective_predecessor.id
                successor.version += 1
            if RunStatus(effective_predecessor.status) in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                raise ConversationQueueConflict("conversation has no active run")
            next_position = (
                await session.scalar(
                    select(func.max(ConversationQueueItemRow.position)).where(
                        ConversationQueueItemRow.tenant_id == command.tenant_id,
                        ConversationQueueItemRow.conversation_id == conversation_id,
                    )
                )
                or 0
            ) + 1
            row = ConversationQueueItemRow(
                id=uuid4(),
                tenant_id=command.tenant_id,
                conversation_id=conversation_id,
                predecessor_run_id=effective_predecessor.id,
                successor_run_id=successor.id,
                message=message,
                attachments=list(command.attachments),
                references={} if command.references is None else dict(command.references),
                position=next_position,
                idempotency_key=idempotency_key,
                status=ConversationQueueStatus.QUEUED.value,
                version=1,
            )
            session.add(row)
            await session.flush()
            return self._item(row)

    async def list_for_conversation(
        self,
        tenant_id: UUID,
        conversation_id: str,
    ) -> tuple[ConversationQueueItem, ...]:
        normalized = conversation_id.strip()
        if not normalized:
            return ()
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(ConversationQueueItemRow)
                    .where(
                        ConversationQueueItemRow.tenant_id == tenant_id,
                        ConversationQueueItemRow.conversation_id == normalized,
                    )
                    .order_by(
                        ConversationQueueItemRow.position,
                        ConversationQueueItemRow.created_at,
                        ConversationQueueItemRow.id,
                    )
                )
            ).all()
        return tuple(self._item(row) for row in rows)

    async def edit(
        self,
        tenant_id: UUID,
        item_id: UUID,
        *,
        expected_version: int,
        message: str,
    ) -> ConversationQueueItem:
        normalized_message = message.strip()
        if not normalized_message:
            raise ValueError("message is required")
        async with self._session_factory() as session, session.begin():
            row = await self._locked_item(session, tenant_id, item_id)
            await self._lock_conversation(session, tenant_id, row.conversation_id)
            if row.version != expected_version:
                raise ConversationQueueConflict("queue item version is stale")
            if row.status != ConversationQueueStatus.QUEUED.value:
                raise ConversationQueueConflict("queue item is no longer editable")
            successor = await self._tenant_run(session, tenant_id, row.successor_run_id)
            if successor.blocked_by_run_id is None:
                raise ConversationQueueConflict("queue item is already being released")
            row.message = normalized_message
            row.version += 1
            successor.request = normalized_message
            successor.version += 1
            await session.flush()
            return self._item(row)

    async def cancel(
        self,
        tenant_id: UUID,
        item_id: UUID,
        *,
        expected_version: int,
    ) -> ConversationQueueItem:
        async with self._session_factory() as session, session.begin():
            row = await self._locked_item(session, tenant_id, item_id)
            await self._lock_conversation(session, tenant_id, row.conversation_id)
            if row.version != expected_version:
                raise ConversationQueueConflict("queue item version is stale")
            if row.status == ConversationQueueStatus.CANCELLED.value:
                return self._item(row)
            if row.status != ConversationQueueStatus.QUEUED.value:
                raise ConversationQueueConflict("queue item is no longer cancellable")
            successor = await self._tenant_run(session, tenant_id, row.successor_run_id)
            if successor.blocked_by_run_id is None:
                raise ConversationQueueConflict("queue item is already being released")
            row.status = ConversationQueueStatus.CANCELLED.value
            row.version += 1
            successor.status = RunStatus.CANCELLED.value
            successor.blocked_by_run_id = None
            successor.version += 1
            following = await session.scalar(
                select(ConversationQueueItemRow)
                .where(
                    ConversationQueueItemRow.tenant_id == tenant_id,
                    ConversationQueueItemRow.conversation_id == row.conversation_id,
                    ConversationQueueItemRow.predecessor_run_id == successor.id,
                    ConversationQueueItemRow.status == ConversationQueueStatus.QUEUED.value,
                )
                .with_for_update()
            )
            if following is not None:
                following.predecessor_run_id = row.predecessor_run_id
                following.version += 1
                following_run = await self._tenant_run(
                    session, tenant_id, following.successor_run_id
                )
                following_run.blocked_by_run_id = row.predecessor_run_id
                following_run.version += 1
            await session.execute(
                delete(RunOutboxRow).where(
                    RunOutboxRow.tenant_id == tenant_id,
                    RunOutboxRow.run_id == successor.id,
                    RunOutboxRow.delivered.is_(False),
                )
            )
            await session.flush()
            return self._item(row)

    async def redirect(
        self,
        tenant_id: UUID,
        item_id: UUID,
        *,
        expected_version: int,
    ) -> ConversationQueueItem:
        async with self._session_factory() as session, session.begin():
            row = await self._locked_item(session, tenant_id, item_id)
            await self._lock_conversation(session, tenant_id, row.conversation_id)
            if row.version != expected_version:
                raise ConversationQueueConflict("queue item version is stale")
            if row.status == ConversationQueueStatus.REDIRECTING.value:
                return self._item(row)
            if row.status != ConversationQueueStatus.QUEUED.value:
                raise ConversationQueueConflict("queue item cannot change direction")
            earlier_rows = (
                await session.scalars(
                    select(ConversationQueueItemRow)
                    .where(
                        ConversationQueueItemRow.tenant_id == tenant_id,
                        ConversationQueueItemRow.conversation_id == row.conversation_id,
                        ConversationQueueItemRow.position < row.position,
                        ConversationQueueItemRow.status == ConversationQueueStatus.QUEUED.value,
                    )
                    .order_by(ConversationQueueItemRow.position)
                    .with_for_update()
                )
            ).all()
            original_predecessor_id = row.predecessor_run_id
            active_predecessor_id = (
                earlier_rows[0].predecessor_run_id if earlier_rows else original_predecessor_id
            )
            predecessor = await self._tenant_run(session, tenant_id, active_predecessor_id)
            predecessor_status = RunStatus(predecessor.status)
            routing = (
                {} if predecessor.routing_decision is None else dict(predecessor.routing_decision)
            )
            predecessor.routing_decision = {
                **routing,
                "cancellation_reason": "superseded_by_user",
                "redirect_queue_item_id": str(row.id),
            }
            predecessor.status = RunStatus.CANCELLED.value
            predecessor.version += 1
            row.predecessor_run_id = predecessor.id
            row.status = ConversationQueueStatus.REDIRECTING.value
            row.version += 1

            selected_successor = await self._tenant_run(session, tenant_id, row.successor_run_id)
            selected_successor.blocked_by_run_id = predecessor.id
            selected_successor.version += 1

            if earlier_rows:
                first_earlier = earlier_rows[0]
                first_earlier.predecessor_run_id = selected_successor.id
                first_earlier.version += 1
                first_earlier_successor = await self._tenant_run(
                    session, tenant_id, first_earlier.successor_run_id
                )
                first_earlier_successor.blocked_by_run_id = selected_successor.id
                first_earlier_successor.version += 1

                following = await session.scalar(
                    select(ConversationQueueItemRow)
                    .where(
                        ConversationQueueItemRow.tenant_id == tenant_id,
                        ConversationQueueItemRow.conversation_id == row.conversation_id,
                        ConversationQueueItemRow.predecessor_run_id == selected_successor.id,
                        ConversationQueueItemRow.id != first_earlier.id,
                        ConversationQueueItemRow.status == ConversationQueueStatus.QUEUED.value,
                    )
                    .with_for_update()
                )
                if following is not None:
                    last_earlier = earlier_rows[-1]
                    following.predecessor_run_id = last_earlier.successor_run_id
                    following.version += 1
                    following_successor = await self._tenant_run(
                        session, tenant_id, following.successor_run_id
                    )
                    following_successor.blocked_by_run_id = last_earlier.successor_run_id
                    following_successor.version += 1

                promoted_position = earlier_rows[0].position
                for index, earlier in enumerate(earlier_rows, start=1):
                    earlier.position = promoted_position + index
                row.position = promoted_position

            has_live_worker = (
                predecessor_status is RunStatus.RUNNING
                and predecessor.worker_lease_expires_at is not None
                and predecessor.worker_lease_expires_at > datetime.now(UTC)
            )
            if not has_live_worker:
                await session.execute(
                    delete(RunOutboxRow).where(
                        RunOutboxRow.tenant_id == tenant_id,
                        RunOutboxRow.run_id == predecessor.id,
                        RunOutboxRow.delivered.is_(False),
                    )
                )
                await self._release_item(session, tenant_id, row, selected_successor)
            await session.flush()
            return self._item(row)

    async def release_next_for_terminal_run(
        self,
        tenant_id: UUID,
        predecessor_run_id: UUID,
    ) -> ConversationQueueItem | None:
        async with self._session_factory() as session, session.begin():
            predecessor = await self._tenant_run(session, tenant_id, predecessor_run_id)
            routing = {} if predecessor.routing_decision is None else predecessor.routing_decision
            conversation_id = str(routing.get("conversation_id") or "").strip()
            if not conversation_id:
                return None
            await self._lock_conversation(session, tenant_id, conversation_id)
            terminal_status = RunStatus(predecessor.status)
            if terminal_status not in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                raise ConversationQueueConflict("predecessor run is not terminal")
            completed_item = await session.scalar(
                select(ConversationQueueItemRow)
                .where(
                    ConversationQueueItemRow.tenant_id == tenant_id,
                    ConversationQueueItemRow.successor_run_id == predecessor_run_id,
                )
                .with_for_update()
            )
            if completed_item is not None and completed_item.status in {
                ConversationQueueStatus.RELEASED.value,
                ConversationQueueStatus.RUNNING.value,
            }:
                completed_item.status = {
                    RunStatus.COMPLETED: ConversationQueueStatus.COMPLETED.value,
                    RunStatus.FAILED: ConversationQueueStatus.FAILED.value,
                    RunStatus.CANCELLED: ConversationQueueStatus.CANCELLED.value,
                }[terminal_status]
                completed_item.version += 1
            next_item = await session.scalar(
                select(ConversationQueueItemRow)
                .where(
                    ConversationQueueItemRow.tenant_id == tenant_id,
                    ConversationQueueItemRow.conversation_id == conversation_id,
                    ConversationQueueItemRow.predecessor_run_id == predecessor_run_id,
                    ConversationQueueItemRow.status.in_(
                        (
                            ConversationQueueStatus.QUEUED.value,
                            ConversationQueueStatus.REDIRECTING.value,
                        )
                    ),
                )
                .order_by(ConversationQueueItemRow.position)
                .with_for_update()
            )
            if next_item is None:
                return None
            successor = await self._tenant_run(session, tenant_id, next_item.successor_run_id)
            if successor.status == RunStatus.CANCELLED.value:
                next_item.status = ConversationQueueStatus.CANCELLED.value
                next_item.version += 1
                return None
            if successor.blocked_by_run_id is None:
                return self._item(next_item)
            await self._release_item(session, tenant_id, next_item, successor)
            await session.flush()
            return self._item(next_item)

    @staticmethod
    async def _release_item(
        session: AsyncSession,
        tenant_id: UUID,
        item: ConversationQueueItemRow,
        successor: RunRow,
    ) -> None:
        successor.blocked_by_run_id = None
        successor.version += 1
        item.status = ConversationQueueStatus.RELEASED.value
        item.version += 1
        if RunStatus(successor.status) is RunStatus.QUEUED:
            session.add(
                RunOutboxRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    run_id=successor.id,
                    task_name="agent_hub.runs.execute",
                    idempotency_key=f"{tenant_id}:{successor.id}:conversation-queue-release",
                    payload={"run_id": str(successor.id)},
                )
            )

    @staticmethod
    async def _lock_conversation(
        session: AsyncSession,
        tenant_id: UUID,
        conversation_id: str,
    ) -> None:
        await session.execute(
            select(func.pg_advisory_xact_lock(_advisory_lock_key(tenant_id, conversation_id)))
        )

    @staticmethod
    async def _tenant_run(session: AsyncSession, tenant_id: UUID, run_id: UUID) -> RunRow:
        row = await session.scalar(
            select(RunRow)
            .where(RunRow.tenant_id == tenant_id, RunRow.id == run_id)
            .with_for_update()
        )
        if row is None:
            raise ConversationQueueConflict("queue run was not found")
        return row

    @staticmethod
    async def _locked_item(
        session: AsyncSession,
        tenant_id: UUID,
        item_id: UUID,
    ) -> ConversationQueueItemRow:
        row = await session.scalar(
            select(ConversationQueueItemRow)
            .where(
                ConversationQueueItemRow.tenant_id == tenant_id,
                ConversationQueueItemRow.id == item_id,
            )
            .with_for_update()
        )
        if row is None:
            raise ConversationQueueNotFound("queue item was not found")
        return row

    @staticmethod
    def _item(row: ConversationQueueItemRow) -> ConversationQueueItem:
        return ConversationQueueItem(
            id=row.id,
            tenant_id=row.tenant_id,
            conversation_id=row.conversation_id,
            predecessor_run_id=row.predecessor_run_id,
            successor_run_id=row.successor_run_id,
            message=row.message,
            attachments=tuple(dict(item) for item in row.attachments),
            references=dict(row.references),
            position=row.position,
            idempotency_key=row.idempotency_key,
            status=ConversationQueueStatus(row.status),
            version=row.version,
            failure_detail=row.failure_detail,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _run_conversation_id(row: RunRow) -> str:
        routing = {} if row.routing_decision is None else row.routing_decision
        return str(routing.get("conversation_id") or "").strip()
