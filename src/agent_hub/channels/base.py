from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Channel(StrEnum):
    CUSTOM_WEBHOOK = "custom_webhook"
    DINGTALK = "dingtalk"
    FEISHU = "feishu"
    QQ = "qq"
    SLACK = "slack"
    TELEGRAM = "telegram"
    WEB = "web"
    WECHAT_CUSTOMER_SERVICE = "wechat_customer_service"
    WECHAT_OFFICIAL = "wechat_official"
    WECOM_APP = "wecom_app"
    WECOM_BOT = "wecom_bot"


ChannelName = Channel


class ConversationType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"


class AttachmentKind(StrEnum):
    IMAGE = "image"
    FILE = "file"


class OutboundKind(StrEnum):
    PROGRESS = "progress"
    CLARIFICATION = "clarification"
    APPROVAL = "approval"
    FINAL_ANSWER = "final_answer"
    ERROR = "error"
    FILE_RESPONSE = "file_response"
    IMAGE_RESPONSE = "image_response"


class InboundAttachment(BaseModel):
    """Channel-neutral attachment metadata accepted by app services."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AttachmentKind
    external_key: str
    filename: str | None = Field(default=None, max_length=512)
    declared_mime: str | None = None

    @field_validator("external_key")
    @classmethod
    def safe_external_key(cls, value: str) -> str:
        return _safe_external_id(value, name="attachment key")

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str | None) -> str | None:
        if value is not None:
            _safe_text(value, name="filename", max_bytes=512, allow_blank=False)
        return value

    @field_validator("declared_mime")
    @classmethod
    def safe_mime_type(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[-\w.+]+/[-\w.+]+", value) is None:
            raise ValueError("declared MIME type is invalid")
        return value


class InboundMessage(BaseModel):
    """Normalized inbound message contract shared by all channel adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: Channel
    tenant_external_id: str
    sender_external_id: str
    conversation_external_id: str
    message_id: str
    event_id: str
    conversation_type: ConversationType
    text: str
    mentions_bot: bool
    attachments: tuple[InboundAttachment, ...] = ()
    received_at: datetime

    @field_validator(
        "tenant_external_id",
        "sender_external_id",
        "conversation_external_id",
        "message_id",
        "event_id",
    )
    @classmethod
    def safe_external_identifier(cls, value: str) -> str:
        return _safe_external_id(value, name="external identifier")

    @field_validator("text")
    @classmethod
    def safe_message_text(cls, value: str) -> str:
        return _safe_text(value, name="message text", max_bytes=65_536, allow_blank=False)

    @field_validator("received_at")
    @classmethod
    def timezone_aware_received_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        return value


class OutboundMessage(BaseModel):
    """Channel-neutral outbound response contract.

    App services choose one of these app-level kinds; channel adapters are responsible for
    rendering platform-specific text, cards, files, or images outside the service boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: OutboundKind
    channel: Channel
    tenant_external_id: str
    conversation_external_id: str
    text: str
    run_id: str | None = None
    attachment_external_key: str | None = None
    attachment_kind: Literal[AttachmentKind.FILE, AttachmentKind.IMAGE] | None = None

    @field_validator("tenant_external_id", "conversation_external_id")
    @classmethod
    def safe_target_identifier(cls, value: str) -> str:
        return _safe_external_id(value, name="external identifier")

    @field_validator("text")
    @classmethod
    def safe_outbound_text(cls, value: str) -> str:
        return _safe_text(value, name="outbound text", max_bytes=65_536, allow_blank=True)

    @field_validator("attachment_external_key")
    @classmethod
    def safe_attachment_key(cls, value: str | None) -> str | None:
        if value is not None:
            return _safe_external_id(value, name="attachment key")
        return value


def should_accept(message: InboundMessage) -> bool:
    return (
        message.conversation_type is ConversationType.PRIVATE
        or message.mentions_bot
    )


def _safe_external_id(value: str, *, name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.:@-]{1,256}", value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _safe_text(value: str, *, name: str, max_bytes: int, allow_blank: bool) -> str:
    if (not allow_blank and not value.strip()) or len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} must be bounded")
    return value


__all__ = [
    "AttachmentKind",
    "Channel",
    "ChannelName",
    "ConversationType",
    "InboundAttachment",
    "InboundMessage",
    "OutboundKind",
    "OutboundMessage",
    "should_accept",
]
