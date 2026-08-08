from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_hub.channels.base import InboundMessage
from agent_hub.domain.runs import TaskMode
from agent_hub.routing.rules import parse_explicit_command


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
        idempotency_key: str | None = None,
    ) -> SubmittedRunLike: ...


@dataclass(frozen=True, slots=True)
class RunServiceInboundSubmitter:
    """Adapt normalized channel messages to the durable run submission boundary."""

    run_service: RunSubmissionService
    tenant_id: UUID

    async def submit(self, message: InboundMessage, *, idempotency_key: str) -> UUID:
        command = parse_explicit_command(message.text)
        mode = TaskMode.AUTO
        task_text = message.text
        if command.mode is not None and not command.invalid:
            mode = command.mode
            if command.task_text:
                task_text = command.task_text
        submitted = await self.run_service.submit(
            tenant_id=self.tenant_id,
            actor_id=_channel_actor_id(message),
            message=task_text,
            mode=mode,
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


__all__ = ["RunServiceInboundSubmitter", "RunSubmissionService"]
