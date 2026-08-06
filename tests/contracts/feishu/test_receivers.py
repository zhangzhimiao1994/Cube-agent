from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from agent_hub.channels.base import ConversationType, InboundMessage
from agent_hub.channels.feishu.settings import FeishuSettings, FeishuTransport
from agent_hub.channels.feishu.verify import FeishuVerificationError, FeishuVerifier
from agent_hub.channels.feishu.webhook import (
    build_feishu_webhook_receiver,
    create_feishu_webhook_router,
)
from agent_hub.channels.feishu.websocket import (
    FeishuWebSocketClient,
    FeishuWebSocketConnector,
    build_feishu_websocket_receiver,
)

NOW = 1_800_000_000


@dataclass(slots=True)
class RecordingGateway:
    messages: list[InboundMessage] = field(default_factory=list)

    async def handle(self, message: InboundMessage) -> object:
        self.messages.append(message)
        return object()


@dataclass(slots=True)
class ScriptedClient:
    payloads: list[dict[str, Any]]
    fail_after: bool = False

    async def events(self) -> AsyncIterator[dict[str, object]]:
        for payload in self.payloads:
            yield payload
        if self.fail_after:
            raise RuntimeError("connection dropped")


def settings(**overrides: object) -> FeishuSettings:
    values: dict[str, object] = {
        "app_id": "cli_a_test",
        "verification_token": "test-token",
        "encrypt_key": "test-encrypt-key",
        "bot_open_id": "ou_bot",
        "timestamp_tolerance_seconds": 300,
    }
    values.update(overrides)
    return FeishuSettings.model_validate(values)


def private_text_event(
    *,
    text: str = "hello",
    event_id: str = "evt_123",
    message_id: str = "om_123",
) -> dict[str, Any]:
    return {
        "token": "test-token",
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "app_id": "cli_a_test",
            "tenant_key": "tenant_1",
            "create_time": str(NOW),
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": message_id,
                "chat_id": "oc_chat",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def group_text_event(*, mentions_bot: bool) -> dict[str, Any]:
    event = private_text_event(event_id="evt_group", message_id="om_group")
    message = event["event"]["message"]
    message["chat_type"] = "group"
    message["mentions"] = (
        [{"id": {"open_id": "ou_bot"}, "name": "Agent Hub"}] if mentions_bot else []
    )
    return event


def image_event() -> dict[str, Any]:
    event = private_text_event(event_id="evt_image", message_id="om_image")
    message = event["event"]["message"]
    message["message_type"] = "image"
    message["content"] = json.dumps({"image_key": "img_123", "file_name": "cat.png"})
    return event


def signed_body(payload: dict[str, Any], verifier: FeishuVerifier) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    timestamp = str(NOW)
    nonce = "nonce_1"
    return body, {
        "x-lark-request-timestamp": timestamp,
        "x-lark-request-nonce": nonce,
        "x-lark-signature": verifier.sign(body, timestamp=timestamp, nonce=nonce),
    }


@pytest.mark.parametrize("transport", ["webhook", "websocket"])
async def test_receivers_produce_same_inbound_message(transport: str) -> None:
    config = settings()
    gateway = RecordingGateway()
    message: InboundMessage | None
    if transport == "webhook":
        webhook_receiver = build_feishu_webhook_receiver(
            config,
            gateway=gateway,
            clock=lambda: float(NOW),
        )
        verifier = FeishuVerifier(
            app_id=config.app_id,
            verification_token=config.verification_token_value(),
            encrypt_key=config.encrypt_key_value(),
            clock=lambda: float(NOW),
        )
        body, headers = signed_body(private_text_event(), verifier)
        result = await webhook_receiver.receive(body, headers=headers)
        message = result.message
    else:
        websocket_receiver = build_feishu_websocket_receiver(
            config,
            gateway=gateway,
            clock=lambda: float(NOW),
        )
        message = await websocket_receiver.receive(private_text_event())

    assert message is not None
    assert message.message_id == "om_123"
    assert message.text == "hello"
    assert gateway.messages == [message]


async def test_webhook_rejects_invalid_signature() -> None:
    gateway = RecordingGateway()
    app = FastAPI()
    app.include_router(create_feishu_webhook_router(settings=settings(), gateway=gateway))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/channels/feishu/events",
            content=b"{}",
            headers={
                "X-Lark-Request-Timestamp": str(NOW),
                "X-Lark-Request-Nonce": "nonce_1",
                "X-Lark-Signature": "bad",
            },
        )

    assert response.status_code == 401
    assert gateway.messages == []


async def test_webhook_returns_url_challenge() -> None:
    config = settings()
    verifier = FeishuVerifier(
        app_id=config.app_id,
        verification_token=config.verification_token_value(),
        encrypt_key=config.encrypt_key_value(),
        clock=lambda: float(NOW),
    )
    body, headers = signed_body({"token": "test-token", "challenge": "challenge_1"}, verifier)
    receiver = build_feishu_webhook_receiver(config, gateway=None, clock=lambda: float(NOW))

    result = await receiver.receive(body, headers=headers)

    assert result.challenge == "challenge_1"
    assert result.message is None


async def test_webhook_accepts_encrypted_event() -> None:
    config = settings()
    verifier = FeishuVerifier(
        app_id=config.app_id,
        verification_token=config.verification_token_value(),
        encrypt_key=config.encrypt_key_value(),
        clock=lambda: float(NOW),
    )
    encrypted = {"encrypt": verifier.encrypt_for_test(private_text_event(text="secret hello"))}
    body, headers = signed_body(encrypted, verifier)
    receiver = build_feishu_webhook_receiver(config, gateway=None, clock=lambda: float(NOW))

    result = await receiver.receive(body, headers=headers)

    assert result.message is not None
    assert result.message.text == "secret hello"


async def test_verifier_rejects_unapproved_tenant_identity() -> None:
    receiver = build_feishu_websocket_receiver(
        settings(allowed_tenant_keys=frozenset({"tenant_allowed"})),
        gateway=None,
        clock=lambda: float(NOW),
    )

    with pytest.raises(FeishuVerificationError, match="tenant"):
        await receiver.receive(private_text_event())


async def test_group_mentions_are_preserved_for_gateway_policy() -> None:
    receiver = build_feishu_websocket_receiver(settings(), gateway=None, clock=lambda: float(NOW))

    without_mention = await receiver.receive(group_text_event(mentions_bot=False))
    with_mention = await receiver.receive(group_text_event(mentions_bot=True))

    assert without_mention is not None
    assert without_mention.conversation_type is ConversationType.GROUP
    assert without_mention.mentions_bot is False
    assert with_mention is not None
    assert with_mention.mentions_bot is True


async def test_image_event_preserves_attachment_metadata() -> None:
    receiver = build_feishu_websocket_receiver(settings(), gateway=None, clock=lambda: float(NOW))

    message = await receiver.receive(image_event())

    assert message is not None
    assert message.text == "[image]"
    assert len(message.attachments) == 1
    assert message.attachments[0].external_key == "img_123"
    assert message.attachments[0].filename == "cat.png"


async def test_unsupported_events_are_ignored_cleanly() -> None:
    receiver = build_feishu_websocket_receiver(settings(), gateway=None, clock=lambda: float(NOW))
    payload = private_text_event()
    payload["header"]["event_type"] = "im.message.deleted_v1"

    assert await receiver.receive(payload) is None


async def test_websocket_reconnects_refreshes_credentials_and_shutdowns() -> None:
    config = settings()
    gateway = RecordingGateway()
    receiver = build_feishu_websocket_receiver(config, gateway=gateway, clock=lambda: float(NOW))
    clients: list[FeishuWebSocketClient] = [
        ScriptedClient([private_text_event(event_id="evt_1", message_id="om_1")], fail_after=True),
        ScriptedClient([private_text_event(event_id="evt_2", message_id="om_2")]),
    ]
    refreshes = 0

    async def client_factory() -> FeishuWebSocketClient:
        return clients.pop(0)

    async def credential_refresh() -> None:
        nonlocal refreshes
        refreshes += 1

    connector: FeishuWebSocketConnector

    async def sleep(_: float) -> None:
        if not clients:
            connector.request_shutdown()
        await asyncio.sleep(0)

    connector = FeishuWebSocketConnector(
        receiver=receiver,
        client_factory=client_factory,
        credential_refresh=credential_refresh,
        reconnect_min_seconds=0,
        reconnect_max_seconds=0,
        sleep=sleep,
    )

    await connector.run_forever()

    assert [message.message_id for message in gateway.messages] == ["om_1", "om_2"]
    assert refreshes == 2
    assert connector.metrics.reconnects == 2
    assert connector.metrics.failures == 1
    assert connector.ready is False


def test_transport_selection_supports_webhook_websocket_and_both() -> None:
    assert settings(transport=FeishuTransport.WEBHOOK).enabled_transports() == {
        FeishuTransport.WEBHOOK
    }
    assert settings(transport=FeishuTransport.WEBSOCKET).enabled_transports() == {
        FeishuTransport.WEBSOCKET
    }
    assert settings(transport=FeishuTransport.BOTH).enabled_transports() == {
        FeishuTransport.WEBHOOK,
        FeishuTransport.WEBSOCKET,
    }
