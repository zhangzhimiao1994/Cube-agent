from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from agent_hub.api.errors import error_responses
from agent_hub.channels.base import InboundMessage
from agent_hub.channels.directives import directive_summary, parse_channel_directives
from agent_hub.channels.feishu.normalize import (
    UnsupportedFeishuEvent,
    normalize_feishu_event,
)
from agent_hub.channels.feishu.reply import (
    FeishuRunReplyDispatcher,
    log_feishu_reply_failure,
)
from agent_hub.channels.feishu.settings import FeishuSettings
from agent_hub.channels.feishu.verify import (
    FeishuVerificationError,
    FeishuVerifier,
)

_LOGGER = logging.getLogger(__name__)


class ChannelGatewayProtocol(Protocol):
    async def handle(self, message: InboundMessage) -> object: ...


@dataclass(frozen=True, slots=True)
class FeishuWebhookResult:
    message: InboundMessage | None
    challenge: str | None = None
    submission: object | None = None
    ignored_reason: str | None = None
    ignored_event_type: str | None = None
    ignored_event_id: str | None = None
    ignored_tenant_key: str | None = None


class FeishuWebhookReceiver:
    def __init__(
        self,
        *,
        verifier: FeishuVerifier,
        gateway: ChannelGatewayProtocol | None,
        bot_open_id: str | None,
    ) -> None:
        self._verifier = verifier
        self._gateway = gateway
        self._bot_open_id = bot_open_id

    async def receive(
        self,
        body: bytes,
        *,
        headers: dict[str, str],
    ) -> FeishuWebhookResult:
        verified = self._verifier.verify_webhook(
            body,
            timestamp=headers.get("x-lark-request-timestamp"),
            nonce=headers.get("x-lark-request-nonce"),
            signature=headers.get("x-lark-signature"),
        )
        if verified.challenge is not None:
            return FeishuWebhookResult(message=None, challenge=verified.challenge)
        try:
            message = normalize_feishu_event(verified.payload, bot_open_id=self._bot_open_id)
        except UnsupportedFeishuEvent as error:
            header = verified.payload.get("header")
            header = header if isinstance(header, dict) else {}
            return FeishuWebhookResult(
                message=None,
                ignored_reason=str(error) or "unsupported event",
                ignored_event_type=_diagnostic_text(header.get("event_type")),
                ignored_event_id=_diagnostic_text(header.get("event_id")),
                ignored_tenant_key=_diagnostic_text(
                    header.get("tenant_key") or header.get("tenant_key_v2")
                ),
            )
        submission: object | None = None
        if self._gateway is not None:
            submission = await self._gateway.handle(message)
        return FeishuWebhookResult(message=message, submission=submission)


def build_feishu_webhook_receiver(
    settings: FeishuSettings,
    *,
    gateway: ChannelGatewayProtocol | None,
    clock: Callable[[], float] | None = None,
) -> FeishuWebhookReceiver:
    verifier = FeishuVerifier(
        app_id=settings.app_id,
        verification_token=settings.verification_token_value(),
        encrypt_key=settings.encrypt_key_value(),
        allowed_tenant_keys=settings.allowed_tenant_keys,
        timestamp_tolerance_seconds=settings.timestamp_tolerance_seconds,
        clock=clock,
    )
    return FeishuWebhookReceiver(
        verifier=verifier,
        gateway=gateway,
        bot_open_id=settings.bot_open_id,
    )


def create_feishu_webhook_router(
    *,
    settings: FeishuSettings,
    gateway: ChannelGatewayProtocol,
) -> APIRouter:
    receiver = build_feishu_webhook_receiver(settings, gateway=gateway)
    router = APIRouter(tags=["feishu"])

    @router.post(settings.webhook_path, response_model=None)
    async def receive_event(request: Request) -> Response:
        body = await request.body()
        headers = {key.lower(): value for key, value in request.headers.items()}
        try:
            result = await receiver.receive(body, headers=headers)
        except FeishuVerificationError:
            return JSONResponse(status_code=401, content={"error": "invalid_feishu_event"})
        if result.challenge is not None:
            return JSONResponse(content={"challenge": result.challenge})
        if result.message is None:
            return JSONResponse(content={"accepted": True, "ignored": True})
        return JSONResponse(status_code=202, content={"accepted": True})

    return router


def create_lazy_feishu_webhook_router(
    *,
    settings_factory: Callable[[], FeishuSettings] = FeishuSettings,
    gateway_provider: Callable[[Request], ChannelGatewayProtocol | None],
    runtime_config_provider: Callable[[Request], Awaitable[Mapping[str, str]] | Mapping[str, str]] | None = None,
) -> APIRouter:
    """Create the main-app Feishu webhook route without loading env at import time."""

    router = APIRouter(
        tags=["feishu"],
        responses=error_responses(401, 405, 413, 422, 500, 503),
    )

    @router.post("/channels/feishu/events", response_model=None, status_code=202)
    async def receive_event(request: Request) -> Response:
        try:
            settings = settings_factory()
            if runtime_config_provider is not None:
                runtime_config = runtime_config_provider(request)
                if inspect.isawaitable(runtime_config):
                    runtime_config = await runtime_config
                settings = _feishu_settings_from_runtime_config(settings, runtime_config)
        except (ValidationError, ValueError):
            return JSONResponse(status_code=503, content={"error": "feishu_not_configured"})
        gateway = gateway_provider(request)
        if gateway is None:
            return JSONResponse(status_code=503, content={"error": "feishu_not_configured"})
        receiver = build_feishu_webhook_receiver(settings, gateway=gateway)
        body = await request.body()
        headers = {key.lower(): value for key, value in request.headers.items()}
        try:
            result = await receiver.receive(body, headers=headers)
        except FeishuVerificationError as error:
            await _record_feishu_verification_failure(request, error, headers=headers)
            return JSONResponse(status_code=401, content={"error": "invalid_feishu_event"})
        if result.challenge is not None:
            return JSONResponse(content={"challenge": result.challenge})
        if result.message is None:
            await _record_feishu_ignored_event(request, result)
            return JSONResponse(content={"accepted": True, "ignored": True})
        _schedule_feishu_reply(request, settings, result)
        return JSONResponse(status_code=202, content={"accepted": True})

    return router


def _feishu_settings_from_runtime_config(
    settings: FeishuSettings, runtime_config: Mapping[str, str]
) -> FeishuSettings:
    overrides: dict[str, object] = {}
    mapping = {
        "FEISHU_APP_ID": "app_id",
        "FEISHU_APP_SECRET": "app_secret",
        "FEISHU_VERIFICATION_TOKEN": "verification_token",
        "FEISHU_ENCRYPT_KEY": "encrypt_key",
        "FEISHU_BOT_OPEN_ID": "bot_open_id",
        "FEISHU_TRANSPORT": "transport",
        "FEISHU_WEBHOOK_PATH": "webhook_path",
        "FEISHU_TIMESTAMP_TOLERANCE_SECONDS": "timestamp_tolerance_seconds",
    }
    for env_name, field_name in mapping.items():
        value = runtime_config.get(env_name)
        if value:
            overrides[field_name] = value
    allowed_tenants = runtime_config.get("FEISHU_ALLOWED_TENANT_KEYS")
    if allowed_tenants:
        overrides["allowed_tenant_keys"] = frozenset(
            item.strip() for item in allowed_tenants.split(",") if item.strip()
        )
    if not overrides:
        return settings
    return FeishuSettings.model_validate({**settings.model_dump(), **overrides})


def _schedule_feishu_reply(
    request: Request,
    settings: FeishuSettings,
    result: FeishuWebhookResult,
) -> None:
    message = result.message
    if message is None:
        return
    if bool(getattr(result.submission, "duplicate", False)):
        return
    run_id = getattr(result.submission, "run_id", None)
    dispatcher = getattr(request.app.state, "feishu_reply_dispatcher", None)
    if not isinstance(dispatcher, FeishuRunReplyDispatcher):
        return
    tenant_id = getattr(request.app.state, "bootstrap_tenant_id", None)
    if tenant_id is None:
        return
    log_service = getattr(request.app.state, "admin_resource_service", None)

    async def task() -> None:
        try:
            if run_id is None:
                await dispatcher.sender.reply_text(
                    settings=settings,
                    message_id=message.message_id,
                    text=directive_summary(parse_channel_directives(message.text)),
                )
                return
            await dispatcher.sender.reply_text(
                settings=settings,
                message_id=message.message_id,
                text=directive_summary(parse_channel_directives(message.text)),
            )
            await dispatcher.reply_when_terminal(
                tenant_id=tenant_id,
                run_id=run_id,
                source_message_id=message.message_id,
                settings=settings,
            )
        except Exception as error:  # noqa: BLE001 - best-effort channel delivery boundary
            await log_feishu_reply_failure(
                log_service=log_service,
                run_id=run_id,
                message_id=message.message_id,
                error=error,
            )

    tasks = getattr(request.app.state, "feishu_reply_tasks", None)
    if not isinstance(tasks, set):
        return
    created = asyncio.create_task(task())
    tasks.add(created)
    created.add_done_callback(tasks.discard)


async def _record_feishu_verification_failure(
    request: Request,
    error: FeishuVerificationError,
    *,
    headers: Mapping[str, str],
) -> None:
    reason = str(error) or "verification failed"
    _LOGGER.warning("feishu_verification_failed reason=%s", reason)
    service = getattr(request.app.state, "admin_resource_service", None)
    recorder = getattr(service, "record_log", None)
    if recorder is None:
        return
    try:
        await recorder(
            category="channel_error",
            level="error",
            title="飞书事件校验失败",
            message=reason,
            source="channels.feishu.webhook",
            details={
                "reason": reason,
                "has_timestamp": str(bool(headers.get("x-lark-request-timestamp"))),
                "has_nonce": str(bool(headers.get("x-lark-request-nonce"))),
                "has_signature": str(bool(headers.get("x-lark-signature"))),
            },
        )
    except Exception:
        _LOGGER.exception("feishu_verification_failure_log_failed")


async def _record_feishu_ignored_event(
    request: Request,
    result: FeishuWebhookResult,
) -> None:
    if result.ignored_reason is None:
        return
    service = getattr(request.app.state, "admin_resource_service", None)
    recorder = getattr(service, "record_log", None)
    if recorder is None:
        return
    details = {
        "reason": result.ignored_reason,
        "event_type": result.ignored_event_type or "unknown",
        "event_id": result.ignored_event_id or "unknown",
        "tenant_key": result.ignored_tenant_key or "unknown",
    }
    try:
        await recorder(
            category="channel_error",
            level="warning",
            title="Feishu event ignored",
            message=result.ignored_reason,
            source="channels.feishu.webhook",
            details=details,
        )
    except Exception:
        _LOGGER.exception("feishu_ignored_event_log_failed")


def _diagnostic_text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:256]


__all__ = [
    "ChannelGatewayProtocol",
    "FeishuWebhookReceiver",
    "FeishuWebhookResult",
    "build_feishu_webhook_receiver",
    "create_feishu_webhook_router",
    "create_lazy_feishu_webhook_router",
]
