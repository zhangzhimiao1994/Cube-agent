from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_hub.channels.base import InboundMessage
from agent_hub.channels.directives import (
    ChannelDirectiveError,
    ChannelResourceHints,
    parse_channel_resource_hints,
)
from agent_hub.domain.runs import TaskMode


class SubmittedRunLike(Protocol):
    id: UUID


class RunSubmissionService(Protocol):
    async def submit(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        attachment_ids: tuple[str, ...] = (),
        channel_context: dict[str, str] | None = None,
        vibe_coding: bool = False,
        idempotency_key: str | None = None,
    ) -> SubmittedRunLike: ...


class ChannelSettingsService(Protocol):
    async def get_settings(self) -> object: ...


@dataclass(frozen=True, slots=True)
class RunServiceInboundSubmitter:
    """Adapt normalized channel messages to the durable run submission boundary."""

    run_service: RunSubmissionService
    tenant_id: UUID
    settings_service: ChannelSettingsService | None = None

    async def submit(self, message: InboundMessage, *, idempotency_key: str) -> UUID:
        task_text = message.text.strip()
        if not task_text:
            raise ChannelDirectiveError("empty_message")
        hints = parse_channel_resource_hints(task_text)
        attachment_ids = _agent_hub_attachment_ids(message)
        task_text = _message_with_attachment_manifest(task_text, message)
        submitted = await self.run_service.submit(
            tenant_id=self.tenant_id,
            actor_id=_channel_actor_id(message),
            message=task_text,
            mode=TaskMode.AUTO,
            attachment_ids=attachment_ids,
            channel_context=_channel_context(message, hints=hints),
            vibe_coding=False,
            idempotency_key=idempotency_key,
        )
        return submitted.id


def _channel_actor_id(message: InboundMessage) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        (
            f"agent-hub:channel:{message.channel.value}:"
            f"{message.tenant_external_id}:{message.sender_external_id}"
        ),
    )


def _agent_hub_attachment_ids(message: InboundMessage) -> tuple[str, ...]:
    ids: list[str] = []
    for attachment in message.attachments:
        if re.fullmatch(r"att_[a-f0-9]{32}", attachment.external_key):
            ids.append(attachment.external_key)
    return tuple(dict.fromkeys(ids))


def _message_with_attachment_manifest(task_text: str, message: InboundMessage) -> str:
    if not message.attachments:
        return task_text
    lines = ["", "Channel attachments:"]
    for attachment in message.attachments:
        lines.append(
            "- "
            f"kind={attachment.kind.value}; "
            f"external_key={attachment.external_key}; "
            f"filename={attachment.filename or 'unknown'}; "
            f"mime={attachment.declared_mime or 'unknown'}"
        )
    return task_text + "\n" + "\n".join(lines)


def _channel_context(message: InboundMessage, *, hints: ChannelResourceHints) -> dict[str, str]:
    context = {
        "source_channel": message.channel.value,
        "channel_tenant_external_id": message.tenant_external_id,
        "channel_sender_external_id": message.sender_external_id,
        "channel_conversation_external_id": message.conversation_external_id,
        "channel_message_id": message.message_id,
        "channel_event_id": message.event_id,
        "channel_conversation_type": message.conversation_type.value,
        "channel_entry_policy": "main_agent_decides",
    }
    if hints.skills:
        context["requested_skills"] = ",".join(hints.skills)
    if hints.mcp_servers:
        context["requested_mcp_servers"] = ",".join(hints.mcp_servers)
    if hints.plugins:
        context["requested_plugins"] = ",".join(hints.plugins)
    return context


__all__ = ["ChannelSettingsService", "RunServiceInboundSubmitter", "RunSubmissionService"]
