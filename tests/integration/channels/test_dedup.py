from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.channels.base import Channel, ConversationType, InboundMessage
from agent_hub.channels.dedup import ChannelDedupRepository
from agent_hub.channels.gateway import ChannelGateway
from agent_hub.db.models import ChannelDedupRow
from agent_hub.db.session import build_database
from tests.integration.conftest import _clean_database

TENANT_EXTERNAL_ID = "tenant_1"


@dataclass(slots=True)
class RecordingSubmitter:
    calls: list[tuple[InboundMessage, str]]

    async def submit(
        self,
        message: InboundMessage,
        *,
        idempotency_key: str,
    ) -> UUID:
        self.calls.append((message, idempotency_key))
        return UUID("11111111-1111-4111-8111-111111111111")


@dataclass(slots=True)
class FailingOnceSubmitter:
    calls: int = 0

    async def submit(
        self,
        message: InboundMessage,
        *,
        idempotency_key: str,
    ) -> UUID:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary submit failure")
        return UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture
async def channel_session_factory(
    database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database = build_database(database_url)
    try:
        await _clean_database(database.session_factory)
        yield database.session_factory
    finally:
        await _clean_database(database.session_factory)
        await database.dispose()


def message(
    *,
    tenant_external_id: str = TENANT_EXTERNAL_ID,
    event_id: str = "evt_1",
    message_id: str = "om_1",
    conversation_type: Literal["private", "group"] = "private",
    mentions_bot: bool = False,
) -> InboundMessage:
    return InboundMessage(
        channel=Channel.FEISHU,
        tenant_external_id=tenant_external_id,
        sender_external_id="ou_1",
        conversation_external_id="oc_1",
        message_id=message_id,
        event_id=event_id,
        conversation_type=ConversationType(conversation_type),
        text="hello",
        mentions_bot=mentions_bot,
        attachments=(),
        received_at=datetime(2026, 8, 7, tzinfo=UTC),
    )


async def test_duplicate_event_submits_one_task(
    channel_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    submitter = RecordingSubmitter([])
    gateway = ChannelGateway(
        deduplicator=ChannelDedupRepository(channel_session_factory),
        submitter=submitter,
    )

    first = await gateway.handle(message(event_id="evt_same", message_id="om_1"))
    second = await gateway.handle(message(event_id="evt_same", message_id="om_2"))

    assert first.accepted is True
    assert first.duplicate is False
    assert second.duplicate is True
    assert second.run_id == first.run_id
    assert len(submitter.calls) == 1


async def test_duplicate_message_id_submits_one_task(
    channel_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    submitter = RecordingSubmitter([])
    gateway = ChannelGateway(
        deduplicator=ChannelDedupRepository(channel_session_factory),
        submitter=submitter,
    )

    first = await gateway.handle(message(event_id="evt_1", message_id="om_same"))
    second = await gateway.handle(message(event_id="evt_2", message_id="om_same"))

    assert second.duplicate is True
    assert second.run_id == first.run_id
    assert len(submitter.calls) == 1


async def test_same_message_id_in_different_tenants_submits_separately(
    channel_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    submitter = RecordingSubmitter([])
    gateway = ChannelGateway(
        deduplicator=ChannelDedupRepository(channel_session_factory),
        submitter=submitter,
    )

    first = await gateway.handle(
        message(
            tenant_external_id="tenant_a",
            event_id="evt_tenant_a",
            message_id="om_same",
        )
    )
    second = await gateway.handle(
        message(
            tenant_external_id="tenant_b",
            event_id="evt_tenant_b",
            message_id="om_same",
        )
    )

    assert first.duplicate is False
    assert second.duplicate is False
    assert len(submitter.calls) == 2


async def test_failed_first_submission_releases_reservation_for_retry(
    channel_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    submitter = FailingOnceSubmitter()
    gateway = ChannelGateway(
        deduplicator=ChannelDedupRepository(channel_session_factory),
        submitter=submitter,
    )
    inbound = message(event_id="evt_retry", message_id="om_retry")

    with pytest.raises(RuntimeError, match="temporary submit failure"):
        await gateway.handle(inbound)

    retry = await gateway.handle(inbound)

    assert retry.accepted is True
    assert retry.duplicate is False
    assert retry.run_id == UUID("22222222-2222-4222-8222-222222222222")
    assert submitter.calls == 2


async def test_idempotency_key_is_bounded_for_maximum_external_ids(
    channel_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = ChannelDedupRepository(channel_session_factory)
    reservation = await repository.reserve(
        message(
            tenant_external_id="t" * 256,
            event_id="e" * 256,
            message_id="m" * 256,
        )
    )

    assert len(reservation.idempotency_key) <= 128


async def test_concurrent_duplicate_arrival_submits_once(
    channel_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    submitter = RecordingSubmitter([])
    gateway = ChannelGateway(
        deduplicator=ChannelDedupRepository(channel_session_factory),
        submitter=submitter,
    )
    inbound = message(event_id="evt_concurrent", message_id="om_concurrent")

    results = await asyncio.gather(gateway.handle(inbound), gateway.handle(inbound))

    assert sum(result.duplicate for result in results) == 1
    assert {result.run_id for result in results} == {
        UUID("11111111-1111-4111-8111-111111111111")
    }
    assert len(submitter.calls) == 1


async def test_group_message_without_mention_is_ignored_before_dedup(
    channel_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    submitter = RecordingSubmitter([])
    gateway = ChannelGateway(
        deduplicator=ChannelDedupRepository(channel_session_factory),
        submitter=submitter,
    )

    result = await gateway.handle(
        message(event_id="evt_group", message_id="om_group", conversation_type="group")
    )

    assert result.accepted is False
    assert result.reason == "group_message_without_bot_mention"
    assert submitter.calls == []


async def test_cleanup_removes_only_completed_old_records(
    channel_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = ChannelDedupRepository(channel_session_factory)
    active = await repository.reserve(message(event_id="evt_active", message_id="om_active"))
    completed = await repository.reserve(message(event_id="evt_done", message_id="om_done"))
    await repository.mark_submitted(completed.id, uuid4())
    await repository.mark_completed(completed.id)
    old_timestamp = datetime(2026, 8, 6, tzinfo=UTC)
    async with channel_session_factory() as session, session.begin():
        await session.execute(
            update(ChannelDedupRow)
                .where(ChannelDedupRow.id.in_([active.id, completed.id]))
                .values(created_at=old_timestamp, completed_at=old_timestamp)
        )

    removed = await repository.cleanup_completed_older_than(
        datetime(2026, 8, 7, tzinfo=UTC) - timedelta(seconds=1)
    )
    active_again = await repository.reserve(message(event_id="evt_active", message_id="om_active"))

    assert removed == 1
    assert active_again.duplicate_found is True
    assert active_again.id == active.id
