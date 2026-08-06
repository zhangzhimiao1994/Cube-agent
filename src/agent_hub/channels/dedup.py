from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.channels.base import InboundMessage
from agent_hub.db.models import ChannelInboundDedupRow


@dataclass(frozen=True, slots=True)
class DedupReservation:
    reservation_id: UUID
    is_new: bool
    run_id: UUID | None
    idempotency_key: str

    @classmethod
    def new(cls, *, reservation_id: UUID, idempotency_key: str) -> DedupReservation:
        return cls(
            reservation_id=reservation_id,
            is_new=True,
            run_id=None,
            idempotency_key=idempotency_key,
        )

    @classmethod
    def duplicate(
        cls,
        *,
        reservation_id: UUID,
        idempotency_key: str,
        run_id: UUID | None,
    ) -> DedupReservation:
        return cls(
            reservation_id=reservation_id,
            is_new=False,
            run_id=run_id,
            idempotency_key=idempotency_key,
        )

    @property
    def id(self) -> UUID:
        return self.reservation_id

    @property
    def duplicate_found(self) -> bool:
        return not self.is_new


class InboundDedupRepository:
    """PostgreSQL-backed idempotency reservations for inbound channel deliveries."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        pending_poll_interval_seconds: float = 0.05,
        pending_wait_timeout_seconds: float = 5.0,
    ) -> None:
        self._session_factory = session_factory
        self._pending_poll_interval_seconds = pending_poll_interval_seconds
        self._pending_wait_timeout_seconds = pending_wait_timeout_seconds

    async def reserve(self, message: InboundMessage) -> DedupReservation:
        for _ in range(2):
            try:
                return await self._reserve_once(message)
            except IntegrityError:
                continue
        return await self._load_existing(message)

    async def mark_submitted(self, reservation_id: UUID, run_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            row = await session.get(
                ChannelInboundDedupRow,
                reservation_id,
                with_for_update=True,
            )
            if row is None:
                raise KeyError("dedup reservation was not found")
            row.run_id = run_id
            row.status = "submitted"

    async def mark_completed(self, reservation_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            row = await session.get(
                ChannelInboundDedupRow,
                reservation_id,
                with_for_update=True,
            )
            if row is None:
                raise KeyError("dedup reservation was not found")
            row.status = "completed"
            row.completed_at = datetime.now(UTC)

    async def release_reservation(self, reservation_id: UUID) -> None:
        """Remove a still-pending reservation after submission failed.

        The delete is intentionally narrow: once a run_id is attached or the reservation
        has moved beyond ``reserved``, retries must observe the existing row instead of
        racing a duplicate task submission.
        """
        async with self._session_factory() as session, session.begin():
            await session.execute(
                delete(ChannelInboundDedupRow).where(
                    ChannelInboundDedupRow.id == reservation_id,
                    ChannelInboundDedupRow.status == "reserved",
                    ChannelInboundDedupRow.run_id.is_(None),
                )
            )

    async def wait_for_run_id(self, reservation_id: UUID) -> UUID | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._pending_wait_timeout_seconds
        while True:
            async with self._session_factory() as session:
                run_id = await session.scalar(
                    select(ChannelInboundDedupRow.run_id).where(
                        ChannelInboundDedupRow.id == reservation_id
                    )
                )
            if run_id is not None:
                return run_id
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(
                min(self._pending_poll_interval_seconds, max(0.0, deadline - loop.time()))
            )

    async def cleanup_completed_older_than(self, cutoff: datetime) -> int:
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("cleanup cutoff must be timezone-aware")
        async with self._session_factory() as session, session.begin():
            ids = (
                await session.scalars(
                    select(ChannelInboundDedupRow.id).where(
                        ChannelInboundDedupRow.status == "completed",
                        ChannelInboundDedupRow.completed_at < cutoff,
                    )
                )
            ).all()
            if not ids:
                return 0
            await session.execute(
                delete(ChannelInboundDedupRow).where(
                    ChannelInboundDedupRow.id.in_(ids),
                )
            )
            return len(ids)

    async def _reserve_once(self, message: InboundMessage) -> DedupReservation:
        async with self._session_factory() as session, session.begin():
            existing = await self._find_existing(session, message, for_update=True)
            if existing is not None:
                return self._reservation_from_existing(existing)

            reservation_id = uuid4()
            idempotency_key = _idempotency_key(message)
            row = ChannelInboundDedupRow(
                id=reservation_id,
                channel=message.channel.value,
                tenant_external_id=message.tenant_external_id,
                event_id=message.event_id,
                message_id=message.message_id,
                run_id=None,
                status="reserved",
                idempotency_key=idempotency_key,
                completed_at=None,
            )
            session.add(row)
            await session.flush()
            return DedupReservation.new(
                reservation_id=reservation_id,
                idempotency_key=idempotency_key,
            )

    async def _load_existing(self, message: InboundMessage) -> DedupReservation:
        async with self._session_factory() as session:
            existing = await self._find_existing(session, message, for_update=False)
            if existing is None:
                raise RuntimeError("dedup reservation conflicted but could not be reloaded")
            return self._reservation_from_existing(existing)

    @staticmethod
    async def _find_existing(
        session: AsyncSession,
        message: InboundMessage,
        *,
        for_update: bool,
    ) -> ChannelInboundDedupRow | None:
        statement = (
            select(ChannelInboundDedupRow)
            .where(
                or_(
                    (
                        (ChannelInboundDedupRow.channel == message.channel.value)
                        & (
                            ChannelInboundDedupRow.tenant_external_id
                            == message.tenant_external_id
                        )
                        & (ChannelInboundDedupRow.event_id == message.event_id)
                    ),
                    (
                        (ChannelInboundDedupRow.channel == message.channel.value)
                        & (
                            ChannelInboundDedupRow.tenant_external_id
                            == message.tenant_external_id
                        )
                        & (ChannelInboundDedupRow.message_id == message.message_id)
                    ),
                )
            )
            .order_by(ChannelInboundDedupRow.created_at, ChannelInboundDedupRow.id)
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(ChannelInboundDedupRow | None, await session.scalar(statement))

    @staticmethod
    def _reservation_from_existing(row: ChannelInboundDedupRow) -> DedupReservation:
        return DedupReservation.duplicate(
            reservation_id=row.id,
            idempotency_key=row.idempotency_key,
            run_id=row.run_id,
        )


ChannelDedupRepository = InboundDedupRepository


def _idempotency_key(message: InboundMessage) -> str:
    payload = (
        f"{message.channel.value}\0{message.tenant_external_id}\0{message.event_id}"
    ).encode()
    digest = hashlib.sha256(payload).hexdigest()
    return f"channel:{digest}"


__all__ = [
    "ChannelDedupRepository",
    "DedupReservation",
    "InboundDedupRepository",
]
