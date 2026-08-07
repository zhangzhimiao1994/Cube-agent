from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from agent_hub.channels.base import Channel, ConversationType, InboundMessage
from agent_hub.channels.dedup import DedupReservation
from agent_hub.channels.gateway import ChannelGateway


def message() -> InboundMessage:
    return InboundMessage(
        channel=Channel.FEISHU,
        tenant_external_id="tenant_1",
        sender_external_id="ou_1",
        conversation_external_id="oc_1",
        message_id="om_duplicate",
        event_id="evt_duplicate",
        conversation_type=ConversationType.PRIVATE,
        text="hello",
        mentions_bot=False,
        attachments=(),
        received_at=datetime(2026, 8, 7, tzinfo=UTC),
    )


@dataclass(slots=True)
class FakeDeduplicator:
    reservation_id: UUID = field(default_factory=uuid4)
    run_id: UUID | None = None
    seen: bool = False

    async def reserve(self, inbound: InboundMessage) -> DedupReservation:
        del inbound
        if self.seen:
            return DedupReservation.duplicate(
                reservation_id=self.reservation_id,
                idempotency_key="channel:duplicate",
                run_id=self.run_id,
            )
        self.seen = True
        return DedupReservation.new(
            reservation_id=self.reservation_id,
            idempotency_key="channel:duplicate",
        )

    async def mark_submitted(self, reservation_id: UUID, run_id: UUID) -> None:
        assert reservation_id == self.reservation_id
        self.run_id = run_id

    async def wait_for_run_id(self, reservation_id: UUID) -> UUID | None:
        assert reservation_id == self.reservation_id
        return self.run_id

    async def release_reservation(self, reservation_id: UUID) -> None:
        assert reservation_id == self.reservation_id
        self.seen = False


@dataclass(slots=True)
class Submitter:
    calls: int = 0

    async def submit(self, inbound: InboundMessage, *, idempotency_key: str) -> UUID:
        del inbound, idempotency_key
        self.calls += 1
        return UUID("11111111-1111-4111-8111-111111111111")


@pytest.mark.asyncio
async def test_feishu_duplicate_events_keep_single_run_id() -> None:
    deduplicator = FakeDeduplicator()
    submitter = Submitter()
    gateway = ChannelGateway(submitter=submitter, deduplicator=deduplicator)

    first = await gateway.handle(message())
    second = await gateway.handle(message())

    assert first.duplicate is False
    assert second.duplicate is True
    assert first.run_id == second.run_id
    assert submitter.calls == 1
