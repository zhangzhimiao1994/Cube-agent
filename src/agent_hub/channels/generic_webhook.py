from __future__ import annotations

import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha1, sha256
from typing import Any, Protocol
from uuid import uuid4
from xml.etree import ElementTree

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from agent_hub.api.errors import error_responses
from agent_hub.channels.base import Channel, ConversationType, InboundMessage


class ChannelGatewayProtocol(Protocol):
    async def handle(self, message: InboundMessage) -> object: ...


@dataclass(frozen=True, slots=True)
class GenericWebhookSpec:
    id: str
    channel: Channel
    path: str
    required_env: tuple[str, ...]
    token_env: str


GENERIC_WEBHOOK_SPECS: tuple[GenericWebhookSpec, ...] = (
    GenericWebhookSpec(
        id="dingtalk",
        channel=Channel.DINGTALK,
        path="/channels/dingtalk/events",
        required_env=("DINGTALK_APP_KEY", "DINGTALK_APP_SECRET", "DINGTALK_WEBHOOK_TOKEN"),
        token_env="DINGTALK_WEBHOOK_TOKEN",
    ),
    GenericWebhookSpec(
        id="wecom_bot",
        channel=Channel.WECOM_BOT,
        path="/channels/wecom/bot/events",
        required_env=("WECOM_BOT_WEBHOOK_KEY", "WECOM_BOT_WEBHOOK_TOKEN"),
        token_env="WECOM_BOT_WEBHOOK_TOKEN",
    ),
    GenericWebhookSpec(
        id="wecom_app",
        channel=Channel.WECOM_APP,
        path="/channels/wecom/app/events",
        required_env=("WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_SECRET", "WECOM_TOKEN"),
        token_env="WECOM_TOKEN",
    ),
    GenericWebhookSpec(
        id="wechat_official",
        channel=Channel.WECHAT_OFFICIAL,
        path="/channels/wechatmp/events",
        required_env=("WECHATMP_APP_ID", "WECHATMP_APP_SECRET", "WECHATMP_TOKEN"),
        token_env="WECHATMP_TOKEN",
    ),
    GenericWebhookSpec(
        id="wechat_customer_service",
        channel=Channel.WECHAT_CUSTOMER_SERVICE,
        path="/channels/wechat-kf/events",
        required_env=("WECHAT_KF_CORP_ID", "WECHAT_KF_SECRET", "WECHAT_KF_TOKEN"),
        token_env="WECHAT_KF_TOKEN",
    ),
    GenericWebhookSpec(
        id="telegram",
        channel=Channel.TELEGRAM,
        path="/channels/telegram/events",
        required_env=("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_TOKEN"),
        token_env="TELEGRAM_WEBHOOK_TOKEN",
    ),
    GenericWebhookSpec(
        id="slack",
        channel=Channel.SLACK,
        path="/channels/slack/events",
        required_env=("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"),
        token_env="SLACK_SIGNING_SECRET",
    ),
    GenericWebhookSpec(
        id="qq",
        channel=Channel.QQ,
        path="/channels/qq/events",
        required_env=("QQ_BOT_APP_ID", "QQ_BOT_TOKEN", "QQ_WEBHOOK_TOKEN"),
        token_env="QQ_WEBHOOK_TOKEN",
    ),
    GenericWebhookSpec(
        id="custom_webhook",
        channel=Channel.CUSTOM_WEBHOOK,
        path="/channels/custom/events",
        required_env=("CUSTOM_WEBHOOK_TOKEN",),
        token_env="CUSTOM_WEBHOOK_TOKEN",
    ),
)


def create_generic_channel_webhook_router(
    *,
    env: Mapping[str, str],
    gateway_provider: Callable[[Request], ChannelGatewayProtocol | None],
) -> APIRouter:
    router = APIRouter(
        tags=["channels"],
        responses=error_responses(401, 405, 413, 422, 500, 503),
    )

    for spec in GENERIC_WEBHOOK_SPECS:
        _add_generic_route(router, spec, env=env, gateway_provider=gateway_provider)

    return router


def _add_generic_route(
    router: APIRouter,
    spec: GenericWebhookSpec,
    *,
    env: Mapping[str, str],
    gateway_provider: Callable[[Request], ChannelGatewayProtocol | None],
) -> None:
    @router.post(spec.path, response_model=None, status_code=202)
    async def receive_event(request: Request) -> Response:
        missing = [name for name in spec.required_env if not env.get(name)]
        if missing:
            return JSONResponse(
                status_code=503,
                content={"error": "channel_not_configured", "channel": spec.id, "missing": missing},
            )
        expected_token = env[spec.token_env]
        body = await request.body()
        if not _request_is_authorized(spec, request, body, expected_token):
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_channel_token", "channel": spec.id},
            )
        gateway = gateway_provider(request)
        if gateway is None:
            return JSONResponse(status_code=503, content={"error": "channel_gateway_unavailable"})
        try:
            payload = _parse_body(body, request.headers.get("content-type", ""))
            if spec.channel is Channel.SLACK and payload.get("type") == "url_verification":
                challenge = payload.get("challenge")
                return JSONResponse(content={"challenge": challenge if isinstance(challenge, str) else ""})
            message = _normalize_message(spec, payload)
        except (TypeError, ValueError) as error:
            return JSONResponse(
                status_code=422,
                content={"error": "invalid_channel_event", "channel": spec.id, "reason": str(error)},
            )
        await gateway.handle(message)
        return JSONResponse(status_code=202, content={"accepted": True, "channel": spec.id})

    @router.get(spec.path, response_model=None)
    async def verify_endpoint(request: Request) -> Response:
        missing = [name for name in spec.required_env if not env.get(name)]
        if missing:
            return JSONResponse(
                status_code=503,
                content={"error": "channel_not_configured", "channel": spec.id, "missing": missing},
            )
        expected_token = env[spec.token_env]
        if not _request_is_authorized(spec, request, b"", expected_token):
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_channel_token", "channel": spec.id},
            )
        echostr = request.query_params.get("echostr")
        if echostr is not None:
            return Response(content=echostr, media_type="text/plain")
        return JSONResponse(content={"configured": True, "channel": spec.id})


def _request_is_authorized(
    spec: GenericWebhookSpec,
    request: Request,
    body: bytes,
    expected_token: str,
) -> bool:
    supplied_token = (
        request.headers.get("x-agent-hub-channel-token")
        or request.query_params.get("token")
        or request.query_params.get("access_token")
    )
    if _allows_generic_shared_token(spec) and supplied_token and hmac.compare_digest(
        supplied_token, expected_token
    ):
        return True
    if spec.channel is Channel.TELEGRAM:
        telegram_token = request.headers.get("x-telegram-bot-api-secret-token")
        return bool(telegram_token and hmac.compare_digest(telegram_token, expected_token))
    if spec.channel in {
        Channel.WECOM_APP,
        Channel.WECHAT_OFFICIAL,
        Channel.WECHAT_CUSTOMER_SERVICE,
    }:
        return _verify_sha1_query_signature(request, expected_token)
    if spec.channel is Channel.SLACK:
        return _verify_slack_signature(request, body, expected_token)
    return False


def _allows_generic_shared_token(spec: GenericWebhookSpec) -> bool:
    return spec.channel in {
        Channel.CUSTOM_WEBHOOK,
        Channel.DINGTALK,
        Channel.QQ,
        Channel.WECOM_BOT,
    }


def _verify_sha1_query_signature(request: Request, token: str) -> bool:
    timestamp = request.query_params.get("timestamp")
    nonce = request.query_params.get("nonce")
    signature = request.query_params.get("signature") or request.query_params.get("msg_signature")
    if not timestamp or not nonce or not signature:
        return False
    raw = "".join(sorted((token, timestamp, nonce)))
    expected = sha1(raw.encode()).hexdigest()
    return hmac.compare_digest(expected, signature)


def _verify_slack_signature(request: Request, body: bytes, signing_secret: str) -> bool:
    timestamp = request.headers.get("x-slack-request-timestamp")
    signature = request.headers.get("x-slack-signature")
    if not timestamp or not signature:
        return False
    try:
        timestamp_seconds = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - timestamp_seconds) > 300:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _parse_body(body: bytes, content_type: str) -> dict[str, Any]:
    if not body:
        raise ValueError("empty request body")
    if "xml" in content_type:
        return _xml_to_dict(body)
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise TypeError("request body must be a JSON object")
    return parsed


def _xml_to_dict(body: bytes) -> dict[str, Any]:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise ValueError("request body must be valid XML") from error
    return {child.tag: child.text or "" for child in root}


def _normalize_message(spec: GenericWebhookSpec, payload: dict[str, Any]) -> InboundMessage:
    text = _first_text(
        payload,
        (
            ("text", "content"),
            ("text",),
            ("content",),
            ("event", "text"),
            ("event", "content"),
            ("message", "text"),
            ("message",),
            ("Content",),
        ),
    )
    if text is None:
        raise ValueError("missing text content")
    sender = _first_text(
        payload,
        (
            ("senderStaffId",),
            ("FromUserName",),
            ("from", "id"),
            ("message", "from", "id"),
            ("event", "user"),
            ("author", "id"),
            ("user_id",),
            ("sender",),
        ),
        fallback="unknown_sender",
    ) or "unknown_sender"
    conversation = _first_text(
        payload,
        (
            ("conversationId",),
            ("ToUserName",),
            ("chat", "id"),
            ("message", "chat", "id"),
            ("event", "channel"),
            ("channel_id",),
            ("conversation_id",),
        ),
        fallback="default_conversation",
    ) or "default_conversation"
    event_id = _first_text(
        payload,
        (
            ("msgId",),
            ("MsgId",),
            ("update_id",),
            ("message", "message_id"),
            ("event_id",),
            ("id",),
            ("message_id",),
        ),
        fallback=f"event_{uuid4().hex}",
    ) or f"event_{uuid4().hex}"
    tenant = _first_text(
        payload,
        (
            ("team_id",),
            ("tenant_id",),
            ("corp_id",),
            ("guild_id",),
        ),
        fallback=spec.id,
    ) or spec.id
    return InboundMessage(
        channel=spec.channel,
        tenant_external_id=_safe_external_value(tenant),
        sender_external_id=_safe_external_value(sender),
        conversation_external_id=_safe_external_value(conversation),
        message_id=_safe_external_value(event_id),
        event_id=_safe_external_value(event_id),
        conversation_type=ConversationType.PRIVATE,
        text=text,
        mentions_bot=True,
        received_at=datetime.now(UTC),
    )


def _first_text(
    payload: dict[str, Any],
    paths: tuple[tuple[str, ...], ...],
    *,
    fallback: str | None = None,
) -> str | None:
    for path in paths:
        value: Any = payload
        for part in path:
            if not isinstance(value, dict) or part not in value:
                value = None
                break
            value = value[part]
        if value is not None:
            return str(value)
    return fallback


def _safe_external_value(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "_.:@-" else "_" for char in value)
    return cleaned[:256] or "unknown"


__all__ = ["GENERIC_WEBHOOK_SPECS", "GenericWebhookSpec", "create_generic_channel_webhook_router"]
