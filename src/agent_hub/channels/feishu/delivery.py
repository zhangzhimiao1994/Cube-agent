from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agent_hub.channels.feishu.cards import FeishuCard, FeishuCardBuilder
from agent_hub.domain.runs import TaskMode


class FeishuDeliveryError(RuntimeError):
    pass


class FeishuApiProtocol(Protocol):
    async def send_card(self, conversation_id: str, card: FeishuCard) -> None: ...

    async def send_text(self, conversation_id: str, text: str) -> None: ...


@dataclass(frozen=True, slots=True)
class FeishuClarification:
    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    conversation_id: str
    options: tuple[TaskMode, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class _PendingTextChoice:
    clarification: FeishuClarification
    expires_at: float


class FeishuDelivery:
    def __init__(
        self,
        *,
        api: FeishuApiProtocol,
        card_builder: FeishuCardBuilder,
        monotonic: Callable[[], float] = time.monotonic,
        text_choice_ttl_seconds: float = 600,
    ) -> None:
        self._api = api
        self._card_builder = card_builder
        self._monotonic = monotonic
        self._text_choice_ttl_seconds = text_choice_ttl_seconds
        self._pending_text: dict[str, _PendingTextChoice] = {}

    async def send_clarification(self, clarification: FeishuClarification) -> None:
        card = self._card_builder.mode_choice_card(
            tenant_id=clarification.tenant_id,
            user_id=clarification.user_id,
            run_id=clarification.run_id,
            options=clarification.options,
            reason=clarification.reason,
        )
        self._pending_text[clarification.conversation_id] = _PendingTextChoice(
            clarification=clarification,
            expires_at=self._monotonic() + self._text_choice_ttl_seconds,
        )
        try:
            await self._api.send_card(clarification.conversation_id, card)
        except FeishuDeliveryError:
            await self._api.send_text(
                clarification.conversation_id,
                _clarification_text(clarification),
            )

    async def send_approval(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        run_id: UUID,
        conversation_id: str,
        approval_id: str,
        summary: str,
    ) -> None:
        card = self._card_builder.approval_card(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            approval_id=approval_id,
            summary=summary,
        )
        try:
            await self._api.send_card(conversation_id, card)
        except FeishuDeliveryError:
            await self._api.send_text(
                conversation_id,
                f"需要审批：{summary}\n请回复 approve {approval_id} 或 reject {approval_id}",
            )

    def consume_text_choice(
        self,
        *,
        conversation_id: str,
        sender_user_id: UUID,
        text: str,
    ) -> TaskMode | None:
        pending = self._pending_text.get(conversation_id)
        if pending is None:
            return None
        if self._monotonic() >= pending.expires_at:
            self._pending_text.pop(conversation_id, None)
            return None
        if sender_user_id != pending.clarification.user_id:
            return None
        stripped = text.strip()
        if not stripped.isdecimal():
            return None
        index = int(stripped)
        if index < 1 or index > len(pending.clarification.options):
            return None
        self._pending_text.pop(conversation_id, None)
        return pending.clarification.options[index - 1]


def _clarification_text(clarification: FeishuClarification) -> str:
    lines = [f"请选择执行模式：{clarification.reason}"]
    for index, mode in enumerate(clarification.options, start=1):
        lines.append(f"{index}. {mode.value}")
    lines.extend(
        [
            "",
            "使用提示：默认会继续当前飞书会话。",
            "新建对话：可说“新建对话”“换个话题”“重新开始”。",
            "切换模式：可说“切换到讨论模式/混合模式/派发模式/直接模式”。",
            "也可以直接回复数字完成本次选择。",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "FeishuApiProtocol",
    "FeishuClarification",
    "FeishuDelivery",
    "FeishuDeliveryError",
]
