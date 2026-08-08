from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from agent_hub.api.errors import error_responses
from agent_hub.channels.base import InboundMessage
from agent_hub.channels.feishu.normalize import (
    UnsupportedFeishuEvent,
    normalize_feishu_event,
)
from agent_hub.channels.feishu.settings import FeishuSettings
from agent_hub.channels.feishu.verify import (
    FeishuVerificationError,
    FeishuVerifier,
)


class ChannelGatewayProtocol(Protocol):
    async def handle(self, message: InboundMessage) -> object: ...


@dataclass(frozen=True, slots=True)
class FeishuWebhookResult:
    message: InboundMessage | None
    challenge: str | None = None


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
        except UnsupportedFeishuEvent:
            return FeishuWebhookResult(message=None)
        if self._gateway is not None:
            await self._gateway.handle(message)
        return FeishuWebhookResult(message=message)


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
            return Response(status_code=204)
        return JSONResponse(status_code=202, content={"accepted": True})

    return router


def create_lazy_feishu_webhook_router(
    *,
    settings_factory: Callable[[], FeishuSettings] = FeishuSettings,
    gateway_provider: Callable[[Request], ChannelGatewayProtocol | None],
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
        except FeishuVerificationError:
            return JSONResponse(status_code=401, content={"error": "invalid_feishu_event"})
        if result.challenge is not None:
            return JSONResponse(content={"challenge": result.challenge})
        if result.message is None:
            return Response(status_code=204)
        return JSONResponse(status_code=202, content={"accepted": True})

    return router


__all__ = [
    "ChannelGatewayProtocol",
    "FeishuWebhookReceiver",
    "FeishuWebhookResult",
    "build_feishu_webhook_receiver",
    "create_feishu_webhook_router",
    "create_lazy_feishu_webhook_router",
]
