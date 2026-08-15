from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote
from uuid import UUID

import httpx

from agent_hub.channels.feishu.settings import FeishuSettings
from agent_hub.domain.runs import RunStatus
from agent_hub.runs.repository import RunNotFound, RunRecord, RunRepository

_LOGGER = logging.getLogger(__name__)
_DEFAULT_FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
_MAX_REPLY_TEXT_CHARS = 3800
_MAX_SECTION_ITEMS = 6
_MAX_LINE_CHARS = 520
_MAX_FINAL_CHARS = 1100


class FeishuReplyError(RuntimeError):
    """Stable Feishu outbound failure that never contains secrets."""


class FeishuReplySender(Protocol):
    async def reply_text(
        self,
        *,
        settings: FeishuSettings,
        message_id: str,
        text: str,
    ) -> None: ...


class FeishuOpenAPIReplySender:
    def __init__(
        self,
        *,
        api_base: str = _DEFAULT_FEISHU_API_BASE,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def reply_text(
        self,
        *,
        settings: FeishuSettings,
        message_id: str,
        text: str,
    ) -> None:
        token = await self._tenant_access_token(settings)
        content = json.dumps({"text": _bounded_reply_text(text)}, ensure_ascii=False)
        url = f"{self._api_base}/im/v1/messages/{quote(message_id, safe='')}/reply"
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"msg_type": "text", "content": content},
            )
        if response.status_code >= 400:
            raise FeishuReplyError(f"reply request failed status={response.status_code}")
        payload = _json_object(response)
        code = payload.get("code")
        if code not in {0, None}:
            raise FeishuReplyError(f"reply request rejected code={code}")

    async def _tenant_access_token(self, settings: FeishuSettings) -> str:
        url = f"{self._api_base}/auth/v3/tenant_access_token/internal"
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                url,
                json={
                    "app_id": settings.app_id,
                    "app_secret": settings.app_secret_value(),
                },
            )
        if response.status_code >= 400:
            raise FeishuReplyError(f"token request failed status={response.status_code}")
        payload = _json_object(response)
        if payload.get("code") != 0:
            raise FeishuReplyError(f"token request rejected code={payload.get('code')}")
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuReplyError("token response is missing tenant_access_token")
        return token


@dataclass(frozen=True, slots=True)
class FeishuRunReplyDispatcher:
    run_repository: RunRepository
    sender: FeishuReplySender
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    poll_interval_seconds: float = 1.0
    timeout_seconds: float = 300.0

    async def reply_when_terminal(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        source_message_id: str,
        settings: FeishuSettings,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        last_status: RunStatus | None = None
        while True:
            try:
                record = await self.run_repository.get(tenant_id, run_id)
            except RunNotFound as error:
                raise FeishuReplyError("run disappeared before Feishu reply") from error
            last_status = record.status
            if record.status in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.WAITING_USER_MODE,
                RunStatus.WAITING_APPROVAL,
            }:
                text = await self._reply_text_for_record(tenant_id, run_id, record)
                await self.sender.reply_text(
                    settings=settings,
                    message_id=source_message_id,
                    text=text,
                )
                return
            if asyncio.get_running_loop().time() >= deadline:
                await self.sender.reply_text(
                    settings=settings,
                    message_id=source_message_id,
                    text=_timeout_text(run_id, last_status),
                )
                return
            await self.sleep(self.poll_interval_seconds)

    async def _reply_text_for_record(
        self,
        tenant_id: UUID,
        run_id: UUID,
        record: RunRecord,
    ) -> str:
        status = record.status
        if status is RunStatus.COMPLETED:
            artifacts = await self.run_repository.artifacts(tenant_id, run_id)
            events = await self.run_repository.events(tenant_id, run_id)
            return _completed_reply_text(record, artifacts, events)
        if status is RunStatus.WAITING_USER_MODE:
            return (
                "这个任务需要你选择执行模式后才能继续。\n"
                "可回复：/direct、/dispatch、/discuss 或 /hybrid 加上任务内容；"
                "也可以到 Web UI 的运行详情中选择模式。\n"
                f"Run ID: {run_id}"
            )
        if status is RunStatus.WAITING_APPROVAL:
            return (
                "这个任务需要你确认主 Agent 的调整建议后才能继续，"
                "请到 Web UI 的运行详情中审批。\n"
                f"Run ID: {run_id}"
            )
        if status is RunStatus.CANCELLED:
            return f"任务已取消。\nRun ID: {run_id}"
        events = await self.run_repository.events(tenant_id, run_id)
        reason = _failure_reason(events)
        if reason:
            return f"任务执行失败：{reason}\nRun ID: {run_id}"
        return f"任务执行失败，请在 Web UI 的日志中心查看详情。\nRun ID: {run_id}"


def _completed_reply_text(
    record: RunRecord,
    artifacts: tuple[dict[str, object], ...],
    events: tuple[dict[str, object], ...],
) -> str:
    final_text = _final_artifact_text(artifacts)
    lines = [
        "任务执行完成",
        f"Run ID: {record.id}",
        f"状态: {record.status.value}",
    ]
    if record.mode is not None:
        lines.append(f"执行模式: {record.mode.value}")
    lines.append(f"用户请求: {_clip_inline(record.request, 220)}")

    lines.extend(
        _section(
            "最终结果",
            [_clip_block(final_text, _MAX_FINAL_CHARS)]
            if final_text
            else ["没有生成可直接发送的文本结果，请到 Web UI 查看完整运行详情。"],
        )
    )
    lines.extend(
        _section("Agent 调度", _routing_lines(record, events), empty="未记录单独调度计划。")
    )
    lines.extend(
        _section(
            "子 Agent 输出",
            _child_output_lines(events, artifacts),
            empty="未记录子 Agent 分步输出。",
        )
    )
    lines.extend(_section("讨论情况", _discussion_lines(events), empty="未记录讨论过程。"))
    lines.extend(_section("裁决情况", _review_lines(events), empty="未记录单独裁决事件。"))
    return _bounded_reply_text("\n".join(lines))


def _section(title: str, lines: Sequence[str], *, empty: str | None = None) -> list[str]:
    content = [line for line in lines if line.strip()]
    if not content and empty is not None:
        content = [empty]
    return ["", title, *content]


def _routing_lines(record: RunRecord, events: tuple[dict[str, object], ...]) -> list[str]:
    plan = _main_agent_plan(events)
    routing = record.routing_decision or {}
    lines: list[str] = []
    main_model = _safe_text(plan.get("main_agent_model")) or _safe_text(
        routing.get("main_agent_model")
    )
    if main_model is not None:
        lines.append(f"- 主 Agent 模型: {main_model}")
    if record.mode is not None:
        lines.append(f"- 运行模式: {record.mode.value}")
    selected = _text_list(routing.get("selected_agent_ids"))
    if selected:
        lines.append(f"- 选中 Agent: {', '.join(selected[:_MAX_SECTION_ITEMS])}")
    roles = _mapping_items(plan.get("roles"))
    for role in roles[:_MAX_SECTION_ITEMS]:
        role_id = _safe_text(role.get("id")) or "unknown"
        role_name = _safe_text(role.get("role"))
        purpose = _safe_text(role.get("purpose"))
        model = _safe_text(role.get("logical_model")) or _safe_text(role.get("model"))
        details = "，".join(item for item in (role_name, purpose, model) if item)
        lines.append(f"- {role_id}" + (f"（{details}）" if details else ""))
    return lines[: _MAX_SECTION_ITEMS + 3]


def _main_agent_plan(events: tuple[dict[str, object], ...]) -> dict[str, object]:
    for event in events:
        if event.get("kind") == "step.started" and event.get("step_id") == "main_agent_plan":
            return _mapping(event.get("payload"))
    return {}


def _child_output_lines(
    events: tuple[dict[str, object], ...],
    artifacts: tuple[dict[str, object], ...],
) -> list[str]:
    lines: list[str] = []
    for event in events:
        if event.get("kind") != "step.completed":
            continue
        payload = _mapping(event.get("payload"))
        actor = _safe_text(event.get("actor")) or "unknown"
        role = _safe_text(payload.get("role")) or actor
        model = _safe_text(payload.get("logical_model"))
        output = _safe_text(payload.get("output")) or _safe_text(payload.get("summary"))
        if output is None:
            continue
        prefix = f"- {role}({actor})"
        if model is not None:
            prefix += f"[{model}]"
        lines.append(f"{prefix}: {_clip_inline(output, _MAX_LINE_CHARS)}")
    if lines:
        return lines[:_MAX_SECTION_ITEMS]
    for artifact in artifacts:
        producer = _safe_text(artifact.get("producer"))
        text = _artifact_text(artifact)
        if producer is None or text is None or producer in {"final_synthesizer", "main_agent"}:
            continue
        lines.append(f"- {producer}: {_clip_inline(text, _MAX_LINE_CHARS)}")
    return lines[:_MAX_SECTION_ITEMS]


def _discussion_lines(events: tuple[dict[str, object], ...]) -> list[str]:
    lines: list[str] = []
    for event in events:
        if event.get("kind") == "discussion.started":
            participants = _text_list(event.get("participants"))
            if participants:
                lines.append(f"- 参与者: {', '.join(participants)}")
            continue
        if event.get("kind") != "message.created":
            continue
        actor = _safe_text(event.get("actor")) or "unknown"
        message = _safe_text(event.get("message"))
        if message is None:
            continue
        payload = _mapping(event.get("payload"))
        model = _safe_text(payload.get("logical_model"))
        prefix = f"- {actor}"
        if model is not None:
            prefix += f"[{model}]"
        lines.append(f"{prefix}: {_clip_inline(message, _MAX_LINE_CHARS)}")
    return lines[:_MAX_SECTION_ITEMS]


def _review_lines(events: tuple[dict[str, object], ...]) -> list[str]:
    lines: list[str] = []
    for event in events:
        if event.get("kind") != "review.completed":
            continue
        payload = _mapping(event.get("payload"))
        actor = _safe_text(event.get("actor")) or "unknown"
        role = _safe_text(payload.get("role")) or actor
        verdict = _safe_text(payload.get("verdict")) or "unknown"
        feedback = (
            _safe_text(payload.get("feedback"))
            or _safe_text(payload.get("warning"))
            or _safe_text(payload.get("rationale"))
            or _safe_text(payload.get("summary"))
        )
        line = f"- {role}({actor}): {verdict}"
        if feedback is not None:
            line += f" - {_clip_inline(feedback, _MAX_LINE_CHARS)}"
        lines.append(line)
    return lines[:_MAX_SECTION_ITEMS]


def _json_object(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as error:
        raise FeishuReplyError("Feishu response is not JSON") from error
    if not isinstance(payload, dict):
        raise FeishuReplyError("Feishu response is not an object")
    return payload


def _bounded_reply_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "任务已完成。"
    if len(stripped) <= _MAX_REPLY_TEXT_CHARS:
        return stripped
    return stripped[:_MAX_REPLY_TEXT_CHARS] + "\n\n……内容较长，已截断；请到 Web UI 查看完整结果。"


def _final_artifact_text(artifacts: tuple[dict[str, object], ...]) -> str | None:
    candidates: list[str] = []
    for artifact in artifacts:
        text = _artifact_text(artifact)
        if text is not None:
            candidates.append(text)
    if not candidates:
        return None
    return candidates[-1]


def _artifact_text(artifact: Mapping[str, object]) -> str | None:
    content = artifact.get("content")
    if not isinstance(content, Mapping):
        return None
    text = content.get("text")
    return _safe_text(text)


def _failure_reason(events: tuple[dict[str, object], ...]) -> str | None:
    for event in reversed(events):
        if event.get("kind") == "runtime.failed":
            reason = event.get("reason")
            if isinstance(reason, str) and reason.strip():
                return reason.strip()
    return None


def _timeout_text(run_id: UUID, status: RunStatus | None) -> str:
    status_text = "unknown" if status is None else status.value
    return (
        "任务已经收到，但运行时间较长，稍后请在 Web UI 查看完整结果。\n"
        f"Run ID: {run_id}\n当前状态: {status_text}"
    )


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            result[key] = item
    return result


def _mapping_items(value: object) -> list[dict[str, object]]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        return []
    result: list[dict[str, object]] = []
    for item in value:
        mapped = _mapping(item)
        if mapped:
            result.append(mapped)
    return result


def _text_list(value: object) -> list[str]:
    if isinstance(value, str):
        text = _safe_text(value)
        return [] if text is None else [text]
    if isinstance(value, bytes | bytearray) or not isinstance(value, Sequence):
        return []
    result: list[str] = []
    for item in value:
        text = _safe_text(item)
        if text is not None:
            result.append(text)
    return result


def _safe_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = " ".join(value.split())
    return stripped or None


def _clip_inline(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)] + "…"


def _clip_block(text: str | None, limit: int) -> str:
    if text is None:
        return ""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: max(1, limit - 1)] + "…"


async def log_feishu_reply_failure(
    *,
    log_service: object | None,
    run_id: UUID | None,
    message_id: str,
    error: Exception,
) -> None:
    _LOGGER.warning(
        "feishu_reply_failed run_id=%s error_type=%s",
        run_id,
        type(error).__name__,
    )
    if log_service is None:
        return
    recorder = getattr(log_service, "record_log", None)
    if recorder is None:
        return
    try:
        await recorder(
            category="channel_error",
            level="error",
            title="飞书回复失败",
            message=str(error) or "Feishu reply failed",
            source="channels.feishu.reply",
            details={
                "run_id": "" if run_id is None else str(run_id),
                "message_id": message_id,
                "error_type": type(error).__name__,
            },
        )
    except Exception:
        _LOGGER.exception("feishu_reply_failure_log_failed run_id=%s", run_id)


__all__ = [
    "FeishuOpenAPIReplySender",
    "FeishuReplyError",
    "FeishuReplySender",
    "FeishuRunReplyDispatcher",
    "log_feishu_reply_failure",
]
