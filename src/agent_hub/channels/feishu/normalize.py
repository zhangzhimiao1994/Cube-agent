from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from agent_hub.channels.base import (
    AttachmentKind,
    Channel,
    ConversationType,
    InboundAttachment,
    InboundMessage,
)


class UnsupportedFeishuEvent(RuntimeError):
    """The callback is valid Feishu data but not a supported inbound message."""


def normalize_feishu_event(
    payload: dict[str, Any],
    *,
    bot_open_id: str | None = None,
) -> InboundMessage:
    header = _dict(payload.get("header"), "header")
    event = _dict(payload.get("event"), "event")
    event_type = str(header.get("event_type", ""))
    if event_type and event_type != "im.message.receive_v1":
        raise UnsupportedFeishuEvent("unsupported event type")
    message = _dict(event.get("message"), "event.message")
    sender = _dict(event.get("sender"), "event.sender")

    message_type = str(message.get("message_type", ""))
    content = _message_content(message)
    text = _message_text(message_type, content)
    attachments = _attachments(message_type, content)
    if not text and attachments:
        text = "[attachment]"
    if not text:
        raise UnsupportedFeishuEvent("empty message")

    chat_type = str(message.get("chat_type", ""))
    conversation_type = (
        ConversationType.PRIVATE if chat_type == "p2p" else ConversationType.GROUP
    )
    return InboundMessage(
        channel=Channel.FEISHU,
        tenant_external_id=_tenant_external_id(header),
        sender_external_id=_sender_external_id(sender),
        conversation_external_id=_required_str(message, "chat_id"),
        message_id=_required_str(message, "message_id"),
        event_id=_required_str(header, "event_id"),
        conversation_type=conversation_type,
        text=text,
        mentions_bot=_mentions_bot(message, bot_open_id=bot_open_id),
        attachments=tuple(attachments),
        received_at=_received_at(header),
    )


def _tenant_external_id(header: dict[str, Any]) -> str:
    return str(header.get("tenant_key") or header.get("tenant_key_v2") or "unknown")


def _sender_external_id(sender: dict[str, Any]) -> str:
    sender_id = sender.get("sender_id")
    if isinstance(sender_id, dict):
        value = sender_id.get("open_id") or sender_id.get("user_id") or sender_id.get("union_id")
        if isinstance(value, str) and value:
            return value
    raise UnsupportedFeishuEvent("missing sender id")


def _message_content(message: dict[str, Any]) -> dict[str, Any]:
    raw = message.get("content")
    if not isinstance(raw, str):
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _message_text(message_type: str, content: dict[str, Any]) -> str:
    if message_type == "text":
        return str(content.get("text") or "").strip()
    if message_type == "image":
        return str(content.get("text") or "[image]").strip()
    if message_type == "file":
        return str(content.get("file_name") or "[file]").strip()
    return str(content.get("text") or "").strip()


def _attachments(message_type: str, content: dict[str, Any]) -> list[InboundAttachment]:
    if message_type == "image":
        image_key = content.get("image_key")
        if isinstance(image_key, str) and image_key:
            return [
                InboundAttachment(
                    kind=AttachmentKind.IMAGE,
                    external_key=image_key,
                    filename=content.get("file_name") if isinstance(content.get("file_name"), str) else None,
                    declared_mime=content.get("mime_type")
                    if isinstance(content.get("mime_type"), str)
                    else None,
                )
            ]
    if message_type == "file":
        file_key = content.get("file_key")
        if isinstance(file_key, str) and file_key:
            return [
                InboundAttachment(
                    kind=AttachmentKind.FILE,
                    external_key=file_key,
                    filename=content.get("file_name") if isinstance(content.get("file_name"), str) else None,
                    declared_mime=content.get("mime_type") if isinstance(content.get("mime_type"), str) else None,
                )
            ]
    return []


def _mentions_bot(message: dict[str, Any], *, bot_open_id: str | None) -> bool:
    mentions = message.get("mentions")
    if not isinstance(mentions, list):
        return False
    if bot_open_id is None:
        return bool(mentions)
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        mention_id = mention.get("id")
        if isinstance(mention_id, dict) and mention_id.get("open_id") == bot_open_id:
            return True
    return False


def _received_at(header: dict[str, Any]) -> datetime:
    raw = header.get("create_time")
    if isinstance(raw, str) and raw.isdecimal():
        timestamp = int(raw)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        return datetime.fromtimestamp(timestamp, tz=UTC)
    return datetime.now(UTC)


def _required_str(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise UnsupportedFeishuEvent(f"missing {key}")
    return value


def _dict(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UnsupportedFeishuEvent(f"missing {name}")
    return value


__all__ = ["UnsupportedFeishuEvent", "normalize_feishu_event"]
