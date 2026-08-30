from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_hub.channels.base import Channel, InboundMessage
from agent_hub.channels.directives import (
    ChannelDirectiveError,
    ChannelResourceHints,
    parse_channel_resource_hints,
)
from agent_hub.domain.runs import TaskMode


class SubmittedRunLike(Protocol):
    id: UUID


@runtime_checkable
class ConversationChoiceService(Protocol):
    async def choose_latest_choice_for_conversation(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        conversation_id: str,
        choice_key: str,
        operator_note: str | None = None,
    ) -> SubmittedRunLike | None: ...


class RunSubmissionService(Protocol):
    async def submit(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        attachment_ids: tuple[str, ...] = (),
        conversation_id: str | None = None,
        channel_context: dict[str, str] | None = None,
        vibe_coding: bool = False,
        idempotency_key: str | None = None,
    ) -> SubmittedRunLike: ...


class ChannelSettingsService(Protocol):
    async def get_settings(self) -> object: ...


class ChannelIdentityResolver(Protocol):
    async def resolve_actor_id(
        self,
        *,
        tenant_id: UUID,
        channel: Channel,
        sender_external_id: str,
    ) -> UUID | None: ...


@dataclass(frozen=True, slots=True)
class RunServiceInboundSubmitter:
    """Adapt normalized channel messages to the durable run submission boundary."""

    run_service: RunSubmissionService
    tenant_id: UUID
    settings_service: ChannelSettingsService | None = None
    identity_resolver: ChannelIdentityResolver | None = None

    async def submit(self, message: InboundMessage, *, idempotency_key: str) -> UUID:
        task_text = message.text.strip()
        if not task_text:
            raise ChannelDirectiveError("empty_message")
        conversation_id = _channel_conversation_id(message)
        resolved_actor_id = (
            await self.identity_resolver.resolve_actor_id(
                tenant_id=self.tenant_id,
                channel=message.channel,
                sender_external_id=message.sender_external_id,
            )
            if self.identity_resolver is not None
            else None
        )
        actor_id = resolved_actor_id or _channel_actor_id(message)
        identity_resolution = (
            "bound_user" if resolved_actor_id is not None else "derived_channel_actor"
        )
        choice_key = _numeric_choice_key(task_text)
        if choice_key is not None and isinstance(self.run_service, ConversationChoiceService):
            chosen = await self.run_service.choose_latest_choice_for_conversation(
                tenant_id=self.tenant_id,
                actor_id=actor_id,
                conversation_id=conversation_id,
                choice_key=choice_key,
                operator_note="channel_numeric_choice",
            )
            if chosen is not None:
                return chosen.id
        hints = parse_channel_resource_hints(task_text)
        attachment_ids = _agent_hub_attachment_ids(message)
        task_text = _message_with_attachment_manifest(task_text, message)
        submitted = await self.run_service.submit(
            tenant_id=self.tenant_id,
            actor_id=actor_id,
            message=task_text,
            mode=TaskMode.AUTO,
            attachment_ids=attachment_ids,
            conversation_id=conversation_id,
            channel_context=_channel_context(
                message,
                hints=hints,
                identity_resolution=identity_resolution,
            ),
            vibe_coding=False,
            idempotency_key=idempotency_key,
        )
        return submitted.id


def _numeric_choice_key(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    exact = re.fullmatch(r"([1-9][0-9]{0,2})", stripped)
    if exact is not None:
        return exact.group(1)
    decorated = re.fullmatch(
        r"(?:选|选择)?\s*第?\s*([1-9][0-9]{0,2})\s*(?:项|个|号)?[.。)、，,、]?",
        stripped,
    )
    return None if decorated is None else decorated.group(1)


def _channel_conversation_id(message: InboundMessage) -> str:
    conversation_uuid = uuid5(
        NAMESPACE_URL,
        (
            f"agent-hub:channel-conversation:{message.channel.value}:"
            f"{message.tenant_external_id}:{message.conversation_external_id}"
        ),
    )
    return f"ch-{message.channel.value}-{conversation_uuid.hex}"


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


def _channel_context(
    message: InboundMessage,
    *,
    hints: ChannelResourceHints,
    identity_resolution: str,
) -> dict[str, str]:
    context = {
        "source_channel": message.channel.value,
        "channel_tenant_external_id": message.tenant_external_id,
        "channel_sender_external_id": message.sender_external_id,
        "channel_conversation_external_id": message.conversation_external_id,
        "channel_message_id": message.message_id,
        "channel_event_id": message.event_id,
        "channel_conversation_type": message.conversation_type.value,
        "channel_entry_policy": "main_agent_decides",
        "channel_identity_resolution": identity_resolution,
    }
    if hints.skills:
        context["requested_skills"] = ",".join(hints.skills)
    if hints.mcp_servers:
        context["requested_mcp_servers"] = ",".join(hints.mcp_servers)
    if hints.plugins:
        context["requested_plugins"] = ",".join(hints.plugins)
    return context


__all__ = [
    "ChannelIdentityResolver",
    "ChannelSettingsService",
    "RunServiceInboundSubmitter",
    "RunSubmissionService",
]
