from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from agent_hub.channels.base import (
    AttachmentKind,
    Channel,
    ConversationType,
    InboundAttachment,
    InboundMessage,
    OutboundKind,
    OutboundMessage,
    should_accept,
)


def inbound_message(
    *,
    conversation_type: Literal["private", "group"] = "private",
    mentions_bot: bool = False,
) -> InboundMessage:
    return InboundMessage(
        channel=Channel.FEISHU,
        tenant_external_id="tenant_1",
        sender_external_id="user_1",
        conversation_external_id="chat_1",
        message_id=f"om_{uuid4().hex}",
        event_id=f"evt_{uuid4().hex}",
        conversation_type=ConversationType(conversation_type),
        text="hello",
        mentions_bot=mentions_bot,
        attachments=(),
        received_at=datetime(2026, 8, 7, tzinfo=UTC),
    )


def test_private_message_is_accepted_without_mention() -> None:
    assert should_accept(inbound_message(conversation_type="private", mentions_bot=False)) is True


def test_group_message_requires_mention() -> None:
    assert should_accept(inbound_message(conversation_type="group", mentions_bot=False)) is False
    assert should_accept(inbound_message(conversation_type="group", mentions_bot=True)) is True


def test_inbound_attachment_accepts_image_and_file_only() -> None:
    image = InboundAttachment(
        kind=AttachmentKind.IMAGE,
        external_key="img_1",
        filename="cat.png",
        declared_mime="image/png",
    )

    assert image.kind == "image"


def test_outbound_types_are_channel_independent() -> None:
    message = OutboundMessage(
        kind=OutboundKind.FINAL_ANSWER,
        channel=Channel.FEISHU,
        tenant_external_id="tenant_1",
        conversation_external_id="chat_1",
        text="done",
        run_id=str(UUID("11111111-1111-4111-8111-111111111111")),
    )

    assert message.kind is OutboundKind.FINAL_ANSWER
    assert not hasattr(message, "card_json")
