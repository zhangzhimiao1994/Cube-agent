from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote
from uuid import UUID

import httpx

from agent_hub.channels.feishu.settings import FeishuSettings
from agent_hub.domain.runs import RunStatus
from agent_hub.runs.repository import RunNotFound, RunRepository

_LOGGER = logging.getLogger(__name__)
_DEFAULT_FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
_MAX_REPLY_TEXT_CHARS = 3800


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
                text = await self._reply_text_for_record(tenant_id, run_id, record.status)
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
        status: RunStatus,
    ) -> str:
        if status is RunStatus.COMPLETED:
            artifacts = await self.run_repository.artifacts(tenant_id, run_id)
            text = _final_artifact_text(artifacts)
            if text is not None:
                return text
            return f"任务已完成，但没有生成可直接发送的文本结果。\nRun ID: {run_id}"
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
        content = artifact.get("content")
        if not isinstance(content, dict):
            continue
        text = content.get("text")
        if isinstance(text, str) and text.strip():
            candidates.append(text.strip())
    if not candidates:
        return None
    return candidates[-1]


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
