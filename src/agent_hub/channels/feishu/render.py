from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class FeishuVisibleEventKind(StrEnum):
    PROGRESS = "progress"
    DISCUSSION = "discussion"
    TOOL = "tool"
    WARNING = "warning"
    INTERNAL_TURN = "internal_turn"


@dataclass(frozen=True, slots=True)
class FeishuVisibleEvent:
    kind: FeishuVisibleEventKind
    text: str


@dataclass(frozen=True, slots=True)
class FeishuFinalReply:
    text: str
    citations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class FeishuRenderer:
    def __init__(
        self,
        *,
        max_message_chars: int = 1800,
        progress_interval_seconds: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_message_chars <= 0 or progress_interval_seconds < 0:
            raise ValueError("renderer limits are invalid")
        self._max_message_chars = max_message_chars
        self._progress_interval_seconds = progress_interval_seconds
        self._monotonic = monotonic
        self._last_progress_at: dict[str, float] = {}

    def progress(self, run_id: str, phase: str, *, force: bool = False) -> str | None:
        now = self._monotonic()
        previous = self._last_progress_at.get(run_id)
        if (
            not force
            and previous is not None
            and now - previous < self._progress_interval_seconds
        ):
            return None
        self._last_progress_at[run_id] = now
        return f"进度：{_bounded_line(phase)}"

    def final_answer(self, reply: FeishuFinalReply) -> tuple[str, ...]:
        parts = [f"最终结论：\n{reply.text.strip()}"]
        if reply.citations:
            parts.append("引用：\n" + "\n".join(f"- {item}" for item in reply.citations))
        if reply.warnings:
            parts.append("注意：\n" + "\n".join(f"- {item}" for item in reply.warnings))
        return tuple(_split_message("\n\n".join(parts), self._max_message_chars))

    def details(
        self,
        events: tuple[FeishuVisibleEvent, ...],
        *,
        can_view_details: bool,
    ) -> tuple[str, ...]:
        if not can_view_details:
            return ("没有权限查看 details。",)
        visible = [
            event
            for event in events
            if event.kind
            in {
                FeishuVisibleEventKind.PROGRESS,
                FeishuVisibleEventKind.DISCUSSION,
                FeishuVisibleEventKind.TOOL,
                FeishuVisibleEventKind.WARNING,
            }
        ]
        if not visible:
            return ("暂无可公开 details。",)
        body = "\n".join(f"[{event.kind.value}] {_bounded_line(event.text)}" for event in visible)
        return tuple(_split_message(body, self._max_message_chars))


def _bounded_line(value: str) -> str:
    normalized = " ".join(value.split())
    return normalized[:500] if normalized else "处理中"


def _split_message(text: str, maximum: int) -> list[str]:
    remaining = text.strip()
    chunks: list[str] = []
    while len(remaining) > maximum:
        boundary = remaining.rfind("\n", 0, maximum)
        if boundary < maximum // 2:
            boundary = remaining.rfind("。", 0, maximum)
        if boundary < maximum // 2:
            boundary = maximum
        chunks.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


__all__ = [
    "FeishuFinalReply",
    "FeishuRenderer",
    "FeishuVisibleEvent",
    "FeishuVisibleEventKind",
]
