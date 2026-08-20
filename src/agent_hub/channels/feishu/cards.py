from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from agent_hub.domain.runs import TaskMode


class FeishuActionKind(StrEnum):
    MODE_CHOICE = "mode_choice"
    APPROVAL = "approval"


class FeishuActionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FeishuActionSubject:
    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    kind: FeishuActionKind
    arguments_hash: str


@dataclass(frozen=True, slots=True)
class FeishuActionRecord:
    action_id: str
    subject: FeishuActionSubject
    expires_at: float


class InMemoryFeishuActionStore:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        max_records: int = 10_000,
    ) -> None:
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        self._monotonic = monotonic
        self._max_records = max_records
        self._records: dict[str, FeishuActionRecord] = {}

    def issue(
        self,
        subject: FeishuActionSubject,
        *,
        ttl_seconds: float,
    ) -> FeishuActionRecord:
        self._purge()
        if len(self._records) >= self._max_records:
            raise RuntimeError("feishu action store is full")
        action_id = secrets.token_urlsafe(32)
        while action_id in self._records:
            action_id = secrets.token_urlsafe(32)
        record = FeishuActionRecord(
            action_id=action_id,
            subject=subject,
            expires_at=self._monotonic() + ttl_seconds,
        )
        self._records[action_id] = record
        return record

    def consume(
        self,
        action_id: str,
        *,
        actor_user_id: UUID,
        expected_subject: FeishuActionSubject,
    ) -> FeishuActionRecord:
        record = self._records.get(action_id)
        if record is None:
            raise FeishuActionError("action not found")
        if self._monotonic() >= record.expires_at:
            self._records.pop(action_id, None)
            raise FeishuActionError("action expired")
        if actor_user_id != record.subject.user_id or record.subject != expected_subject:
            raise FeishuActionError("action mismatch")
        self._records.pop(action_id, None)
        return record

    def _purge(self) -> None:
        now = self._monotonic()
        expired = [
            action_id for action_id, record in self._records.items() if now >= record.expires_at
        ]
        for action_id in expired:
            self._records.pop(action_id, None)


@dataclass(frozen=True, slots=True)
class FeishuCard:
    payload: dict[str, object]


class FeishuCardBuilder:
    def __init__(
        self,
        store: InMemoryFeishuActionStore,
        *,
        action_ttl_seconds: float = 600,
    ) -> None:
        self._store = store
        self._action_ttl_seconds = action_ttl_seconds

    def mode_choice_card(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        run_id: UUID,
        options: tuple[TaskMode, ...],
        reason: str,
    ) -> FeishuCard:
        actions = []
        for index, mode in enumerate(options, start=1):
            subject = _subject(
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                kind=FeishuActionKind.MODE_CHOICE,
                arguments={"mode": mode.value, "index": index},
            )
            record = self._store.issue(subject, ttl_seconds=self._action_ttl_seconds)
            actions.append(
                {
                    "text": f"{index}. {mode.value}",
                    "value": {
                        "action_id": record.action_id,
                        "mode": mode.value,
                        "index": index,
                    },
                }
            )
        return FeishuCard(
            payload={
                "type": "mode_choice",
                "title": "请选择执行模式",
                "reason": reason,
                "run_id": str(run_id),
                "usage_hint": _mode_choice_usage_hint(),
                "actions": actions,
            }
        )

    def approval_card(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        run_id: UUID,
        approval_id: str,
        summary: str,
    ) -> FeishuCard:
        actions = []
        for decision in ("approve", "reject"):
            subject = _subject(
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                kind=FeishuActionKind.APPROVAL,
                arguments={"approval_id": approval_id, "decision": decision},
            )
            record = self._store.issue(subject, ttl_seconds=self._action_ttl_seconds)
            actions.append(
                {
                    "text": "批准" if decision == "approve" else "拒绝",
                    "value": {
                        "action_id": record.action_id,
                        "approval_id": approval_id,
                        "decision": decision,
                    },
                }
            )
        return FeishuCard(
            payload={
                "type": "approval",
                "title": "需要审批",
                "summary": summary,
                "run_id": str(run_id),
                "actions": actions,
            }
        )


def _mode_choice_usage_hint() -> str:
    return (
        "默认会继续当前飞书会话；需要新开时可说“新建对话”“换个话题”“重新开始”；"
        "需要换模式时可说“切换到讨论模式/混合模式/派发模式/直接模式”；"
        "也可以直接回复本次选项编号。"
    )


def consume_mode_action(
    store: InMemoryFeishuActionStore,
    action_id: str,
    *,
    tenant_id: UUID,
    user_id: UUID,
    run_id: UUID,
    mode: TaskMode,
    index: int,
) -> FeishuActionRecord:
    expected = _subject(
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=run_id,
        kind=FeishuActionKind.MODE_CHOICE,
        arguments={"mode": mode.value, "index": index},
    )
    return store.consume(action_id, actor_user_id=user_id, expected_subject=expected)


def consume_approval_action(
    store: InMemoryFeishuActionStore,
    action_id: str,
    *,
    tenant_id: UUID,
    user_id: UUID,
    run_id: UUID,
    approval_id: str,
    decision: str,
) -> FeishuActionRecord:
    expected = _subject(
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=run_id,
        kind=FeishuActionKind.APPROVAL,
        arguments={"approval_id": approval_id, "decision": decision},
    )
    return store.consume(action_id, actor_user_id=user_id, expected_subject=expected)


def _subject(
    *,
    tenant_id: UUID,
    user_id: UUID,
    run_id: UUID,
    kind: FeishuActionKind,
    arguments: dict[str, object],
) -> FeishuActionSubject:
    return FeishuActionSubject(
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=run_id,
        kind=kind,
        arguments_hash=_arguments_hash(arguments),
    )


def _arguments_hash(arguments: dict[str, object]) -> str:
    encoded = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "FeishuActionError",
    "FeishuActionKind",
    "FeishuActionRecord",
    "FeishuActionSubject",
    "FeishuCard",
    "FeishuCardBuilder",
    "InMemoryFeishuActionStore",
    "consume_approval_action",
    "consume_mode_action",
]
