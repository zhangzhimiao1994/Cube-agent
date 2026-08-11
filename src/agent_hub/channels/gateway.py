from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agent_hub.channels.base import InboundMessage, should_accept
from agent_hub.channels.dedup import DedupReservation
from agent_hub.channels.directives import ChannelDirectiveError


class InboundTaskSubmitter(Protocol):
    """Ordinary application submission boundary for normalized channel messages."""

    async def submit(self, message: InboundMessage, *, idempotency_key: str) -> UUID: ...


class InboundDeduplicator(Protocol):
    """Durable inbound idempotency boundary."""

    async def reserve(self, message: InboundMessage) -> DedupReservation: ...

    async def mark_submitted(self, reservation_id: UUID, run_id: UUID) -> None: ...

    async def wait_for_run_id(self, reservation_id: UUID) -> UUID | None: ...

    async def release_reservation(self, reservation_id: UUID) -> None: ...


@dataclass(frozen=True, slots=True)
class ChannelGatewayResult:
    accepted: bool
    duplicate: bool
    run_id: UUID | None
    reason: str | None = None


class ChannelGateway:
    """Submit only normalized app-level inbound messages into Agent Hub."""

    def __init__(
        self,
        *,
        submitter: InboundTaskSubmitter,
        deduplicator: InboundDeduplicator,
    ) -> None:
        self._submitter = submitter
        self._deduplicator = deduplicator

    async def handle(self, message: InboundMessage) -> ChannelGatewayResult:
        if not should_accept(message):
            return ChannelGatewayResult(
                accepted=False,
                duplicate=False,
                run_id=None,
                reason="group_message_without_bot_mention",
            )

        reservation = await self._deduplicator.reserve(message)
        if not reservation.is_new:
            run_id = reservation.run_id
            if run_id is None:
                run_id = await self._deduplicator.wait_for_run_id(reservation.reservation_id)
                if run_id is None:
                    return ChannelGatewayResult(
                        accepted=True,
                        duplicate=True,
                        run_id=None,
                        reason="duplicate_pending_timeout",
                    )
            return ChannelGatewayResult(
                accepted=True,
                duplicate=True,
                run_id=run_id,
            )

        try:
            run_id = await self._submitter.submit(
                message,
                idempotency_key=reservation.idempotency_key,
            )
            await self._deduplicator.mark_submitted(reservation.reservation_id, run_id)
        except ChannelDirectiveError as error:
            with suppress(Exception):
                await self._deduplicator.release_reservation(reservation.reservation_id)
            return ChannelGatewayResult(
                accepted=False,
                duplicate=False,
                run_id=None,
                reason=error.reason,
            )
        except Exception:
            with suppress(Exception):
                await self._deduplicator.release_reservation(reservation.reservation_id)
            raise
        return ChannelGatewayResult(
            accepted=True,
            duplicate=False,
            run_id=run_id,
        )


__all__ = [
    "ChannelGateway",
    "ChannelGatewayResult",
    "InboundDeduplicator",
    "InboundTaskSubmitter",
]
