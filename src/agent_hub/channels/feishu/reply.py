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
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def reply_text(
        self,
        *,
        settings: FeishuSettings,
        message_id: str,
        text: str,
    ) -> None:
        token = await self._tenant_access_token(settings)
        reply_payload = _reply_payload_for_text(text)
        url = f"{self._api_base}/im/v1/messages/{quote(message_id, safe='')}/reply"
        async with httpx.AsyncClient(timeout=self._timeout_seconds, transport=self._transport) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=reply_payload,
            )
        if response.status_code >= 400:
            raise FeishuReplyError(f"reply request failed status={response.status_code}")
        payload = _json_object(response)
        code = payload.get("code")
        if code not in {0, None}:
            raise FeishuReplyError(f"reply request rejected code={code}")

    async def _tenant_access_token(self, settings: FeishuSettings) -> str:
        url = f"{self._api_base}/auth/v3/tenant_access_token/internal"
        async with httpx.AsyncClient(timeout=self._timeout_seconds, transport=self._transport) as client:
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
                for chunk in _reply_text_chunks(text):
                    await self.sender.reply_text(
                        settings=settings,
                        message_id=source_message_id,
                        text=chunk,
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
            return _waiting_choice_reply_text(record)
        if status is RunStatus.WAITING_APPROVAL:
            return (
                "这个任务需要你确认主 Agent 的调整建议后才能继续，"
                "请到 Web UI 的运行详情中审批。\n"
                f"Run ID: {run_id}"
            )
        if status is RunStatus.CANCELLED:
            return f"任务已取消。\nRun ID: {run_id}"
        artifacts = await self.run_repository.artifacts(tenant_id, run_id)
        events = await self.run_repository.events(tenant_id, run_id)
        return _failed_reply_text(record, artifacts, events)


def _waiting_choice_reply_text(record: RunRecord) -> str:
    lines = [
        "这个任务需要你先选择下一步处理方式。",
        f"Run ID: {record.id}",
    ]
    choices = _channel_choice_lines(record)
    if choices:
        lines.extend(["", "可直接回复本次选项编号：", *choices])
    else:
        lines.extend(["", "请到 Web UI 的运行详情中完成选择。"])
    lines.extend(
        [
            "",
            "使用提示：默认会继续当前飞书会话。",
            "新建对话：可说“新建对话”“换个话题”“重新开始”。",
            "切换模式：可说“切换到讨论模式/混合模式/派发模式/直接模式”。",
        ]
    )
    return "\n".join(lines).strip()


def _failed_reply_text(
    record: RunRecord,
    artifacts: tuple[dict[str, object], ...],
    events: tuple[dict[str, object], ...],
) -> str:
    reason = _failure_reason(events) or "请在 Web UI 的日志中心查看详情。"
    final_text = _final_artifact_text(artifacts)
    lines = [
        "任务执行失败",
        f"Run ID: {record.id}",
    ]
    if record.mode is not None:
        lines.append(f"执行模式: {record.mode.value}")
    lines.append(f"用户请求: {_clip_inline(record.request, 220)}")
    lines.extend(_section("错误原因", [_clip_inline(reason, _MAX_LINE_CHARS)]))
    lines.extend(
        _section("Agent 调度", _routing_lines(record, events), empty="未记录单独调度计划。")
    )
    lines.extend(_section("讨论情况", _discussion_lines(events), empty="未记录讨论过程。"))
    lines.extend(_section("裁决情况", _review_lines(events), empty="未记录单独裁决事件。"))
    produced = _child_output_lines(events, artifacts)
    if final_text:
        produced.append(f"- 最近产物: {_clip_inline(final_text, _MAX_LINE_CHARS)}")
    lines.extend(_section("已产生内容", produced, empty="失败前没有保存可发送的产物。"))
    return "\n".join(lines).strip()


def _channel_choice_lines(record: RunRecord) -> list[str]:
    routing = record.routing_decision or {}
    raw_choices = routing.get("channel_choices")
    if not isinstance(raw_choices, Sequence) or isinstance(raw_choices, str | bytes | bytearray):
        return []
    lines: list[str] = []
    for raw_choice in raw_choices:
        if not isinstance(raw_choice, Mapping):
            continue
        key = _safe_text(raw_choice.get("key"))
        label = _safe_text(raw_choice.get("label"))
        value = _safe_text(raw_choice.get("value"))
        choice_type = _safe_text(raw_choice.get("type"))
        if key is None:
            continue
        text = label or value or choice_type or "选项"
        details: list[str] = []
        confidence = raw_choice.get("confidence")
        risk = _safe_text(raw_choice.get("risk"))
        reason = _safe_text(raw_choice.get("reason"))
        if isinstance(confidence, int | float):
            details.append(f"置信度 {confidence:.2f}")
        if risk is not None:
            details.append(f"风险 {risk}")
        if reason is not None:
            details.append(_clip_inline(reason, 80))
        suffix = f"（{'；'.join(details)}）" if details else ""
        lines.append(f"{key}. {text}{suffix}")
    return lines[:_MAX_SECTION_ITEMS]


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
        _section("Agent 调度", _routing_lines(record, events), empty="未记录单独调度计划。")
    )
    lines.extend(_section("讨论情况", _discussion_lines(events), empty="未记录讨论过程。"))
    lines.extend(_section("裁决情况", _review_lines(events), empty="未记录单独裁决事件。"))
    lines.extend(
        _section(
            "子 Agent 输出",
            _child_output_lines(events, artifacts),
            empty="未记录子 Agent 分步输出。",
        )
    )
    lines.extend(
        _section(
            "最终结果",
            [final_text]
            if final_text
            else ["没有生成可直接发送的文本结果，请到 Web UI 查看完整运行详情。"],
        )
    )
    return "\n".join(lines).strip().strip()


def _section(title: str, lines: Sequence[str], *, empty: str | None = None) -> list[str]:
    content = [line for line in lines if line.strip()]
    if not content and empty is not None:
        content = [empty]
    return ["", _section_title(title), *content]


def _section_title(title: str) -> str:
    if title in {"子 Agent 输出", "已产生内容"}:
        return f"【中间产物｜{title}】"
    return f"【{title}】"


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
    decision_roles = _decision_role_ids(events)
    for event in events:
        kind = event.get("kind")
        payload = _mapping(event.get("payload"))
        actor = _safe_text(event.get("actor")) or "unknown"
        role = _safe_text(payload.get("role")) or actor
        model = _safe_text(payload.get("logical_model"))
        if kind == "review.completed":
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
            continue
        if kind != "step.completed" or actor not in decision_roles:
            continue
        output = _safe_text(payload.get("output")) or _safe_text(payload.get("summary"))
        if output is None:
            continue
        prefix = f"- {role}({actor})"
        if model is not None:
            prefix += f"[{model}]"
        lines.append(f"{prefix}: {_clip_inline(output, _MAX_LINE_CHARS)}")
    return lines[:_MAX_SECTION_ITEMS]


def _decision_role_ids(events: tuple[dict[str, object], ...]) -> set[str]:
    decision_purposes = {"verify", "critique", "risk_review", "record_decision", "synthesize"}
    role_ids: set[str] = {"final_synthesizer"}
    for role in _mapping_items(_main_agent_plan(events).get("roles")):
        role_id = _safe_text(role.get("id"))
        purpose = _safe_text(role.get("purpose"))
        role_name = (_safe_text(role.get("role")) or "").casefold()
        if role_id is None:
            continue
        if purpose in decision_purposes or any(
            marker in role_name
            for marker in ("review", "审查", "复核", "裁决", "synthesizer", "综合")
        ):
            role_ids.add(role_id)
    return role_ids


def _reply_payload_for_text(text: str) -> dict[str, str]:
    bounded = _bounded_reply_text(text)
    if _contains_markdown_table(bounded):
        return {
            "msg_type": "post",
            "content": json.dumps(
                {
                    "post": {
                        "zh_cn": {
                            "title": "魔方agent",
                            "content": [[{"tag": "md", "text": bounded}]],
                        }
                    }
                },
                ensure_ascii=False,
            ),
        }
    return {"msg_type": "text", "content": json.dumps({"text": bounded}, ensure_ascii=False)}


def _contains_markdown_table(text: str) -> bool:
    lines = text.splitlines()
    return any(_is_markdown_table_start(lines, index) for index in range(max(0, len(lines) - 1)))

def _json_object(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as error:
        raise FeishuReplyError("Feishu response is not JSON") from error
    if not isinstance(payload, dict):
        raise FeishuReplyError("Feishu response is not an object")
    return payload


def _reply_text_chunks(text: str) -> tuple[str, ...]:
    stripped = text.strip()
    if not stripped:
        return ("任务已完成。",)
    if len(stripped) <= _MAX_REPLY_TEXT_CHARS:
        return (stripped,)
    chunks = _split_text(stripped, _MAX_REPLY_TEXT_CHARS - 32)
    while True:
        total = len(chunks)
        prefix_len = max(len(f"（{index}/{total}）\n") for index in range(1, total + 1))
        next_chunks = _split_text(stripped, _MAX_REPLY_TEXT_CHARS - prefix_len)
        if len(next_chunks) == total:
            return tuple(
                f"（{index}/{total}）\n{chunk}" for index, chunk in enumerate(next_chunks, start=1)
            )
        chunks = next_chunks


def _split_text(text: str, maximum: int) -> list[str]:
    remaining = text.strip()
    chunks: list[str] = []
    while len(remaining) > maximum:
        boundary = remaining.rfind("\n\n", 0, maximum)
        if boundary < maximum // 2:
            boundary = remaining.rfind("\n", 0, maximum)
        if boundary < maximum // 2:
            boundary = remaining.rfind("。", 0, maximum)
        if boundary < maximum // 2:
            boundary = maximum
        chunk = remaining[:boundary].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[boundary:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks or ["任务已完成。"]


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
    return _stabilize_markdown_table_blocks(candidates[-1])


def _artifact_text(artifact: Mapping[str, object]) -> str | None:
    content = artifact.get("content")
    if not isinstance(content, Mapping):
        return None
    table = _format_table_content(content)
    if table is not None:
        return table
    text = content.get("text")
    return _safe_block_text(text)


def _stabilize_markdown_table_blocks(text: str) -> str:
    lines = text.splitlines()
    result: list[str] = []
    index = 0
    while index < len(lines):
        if _is_markdown_table_start(lines, index):
            table_lines: list[str] = []
            while index < len(lines) and "|" in lines[index]:
                table_lines.append(lines[index].strip())
                index += 1
            if result and result[-1].strip():
                result.append("")
            result.extend(table_lines)
            if index < len(lines) and lines[index].strip():
                result.append("")
            continue
        result.append(lines[index])
        index += 1
    return "\n".join(result).strip()


def _is_markdown_table_start(lines: Sequence[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index].strip()
    separator = lines[index + 1].strip()
    if header.startswith("```"):
        return False
    if header.count("|") < 2 or separator.count("|") < 2:
        return False
    normalized = separator.replace("|", "").replace(" ", "").replace(":", "")
    return bool(normalized) and set(normalized) <= {"-"}


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


def _format_table_content(content: Mapping[str, object]) -> str | None:
    raw_table = content.get("table")
    table = _mapping(raw_table) if isinstance(raw_table, Mapping) else dict(content)
    rows = _table_rows(table.get("rows") or table.get("data"))
    if not rows:
        return None
    columns = _table_columns(table.get("columns") or table.get("headers"), rows)
    if not columns:
        return None
    header_labels = [label for _, label in columns]
    lines = [
        "| " + " | ".join(_escape_table_cell(label) for label in header_labels) + " |",
        "| " + " | ".join("---" for _ in header_labels) + " |",
    ]
    for row in rows:
        values: list[str] = []
        if isinstance(row, Mapping):
            for key, _label in columns:
                values.append(_table_cell_text(row.get(key)))
        else:
            sequence = list(row)
            for index, _column in enumerate(columns):
                values.append(_table_cell_text(sequence[index] if index < len(sequence) else ""))
        lines.append("| " + " | ".join(_escape_table_cell(value) for value in values) + " |")
    return "\n".join(lines)


def _table_rows(value: object) -> list[Mapping[str, object] | Sequence[object]]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        return []
    rows: list[Mapping[str, object] | Sequence[object]] = []
    for item in value[:50]:
        if isinstance(item, Mapping) or (
            not isinstance(item, str | bytes | bytearray) and isinstance(item, Sequence)
        ):
            rows.append(item)
    return rows


def _table_columns(
    value: object, rows: Sequence[Mapping[str, object] | Sequence[object]]
) -> list[tuple[str, str]]:
    columns: list[tuple[str, str]] = []
    if not isinstance(value, str | bytes | bytearray) and isinstance(value, Sequence):
        for item in value[:20]:
            if isinstance(item, Mapping):
                key = (
                    _safe_text(item.get("key"))
                    or _safe_text(item.get("id"))
                    or _safe_text(item.get("name"))
                    or _safe_text(item.get("label"))
                )
                label = _safe_text(item.get("label")) or _safe_text(item.get("name")) or key
                if key and label:
                    columns.append((key, label))
            else:
                text = _safe_text(item)
                if text:
                    columns.append((text, text))
    if columns:
        return columns
    first = rows[0]
    if isinstance(first, Mapping):
        return [(str(key), str(key)) for key in list(first.keys())[:20] if isinstance(key, str)]
    return [(str(index), f"列 {index + 1}") for index in range(min(len(first), 20))]


def _table_cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, int | float | bool):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _safe_block_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    lines = [" ".join(line.split()) for line in value.strip().splitlines()]
    block = "\n".join(line for line in lines if line)
    return block or None


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
