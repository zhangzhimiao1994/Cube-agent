from __future__ import annotations

import hmac
import time
from dataclasses import dataclass
from hashlib import sha1, sha256
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from agent_hub.api.routers.admin import InMemoryAdminResourceService
from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.channels.base import InboundMessage
from agent_hub.channels.feishu.media import FeishuMediaError
from agent_hub.channels.feishu.settings import FeishuSettings
from agent_hub.channels.feishu.verify import FeishuVerifier

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
USER_ID = UUID("11111111-1111-4111-8111-111111111111")


class StubAuthService:
    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid-token":
            raise InvalidCredentials("bad token")
        return AuthenticatedPrincipal(USER_ID, TENANT_ID, Role.SUPER_ADMIN)


class RecordingGateway:
    def __init__(self) -> None:
        self.messages: list[InboundMessage] = []

    async def handle(self, message: InboundMessage) -> object:
        self.messages.append(message)
        return object()


@dataclass(frozen=True, slots=True)
class StubVisionArtifact:
    summary: str


@dataclass(frozen=True, slots=True)
class StubVisionResult:
    artifact: StubVisionArtifact


@dataclass(frozen=True, slots=True)
class StubFeishuImageAnalysis:
    result: StubVisionResult
    channel_metadata: dict[str, str]


class StubFeishuMediaService:
    def __init__(self) -> None:
        self.messages: list[InboundMessage] = []

    async def analyze_images(self, message: InboundMessage) -> tuple[StubFeishuImageAnalysis, ...]:
        self.messages.append(message)
        return (
            StubFeishuImageAnalysis(
                result=StubVisionResult(StubVisionArtifact("whiteboard architecture diagram")),
                channel_metadata={"resource_key": "img_123"},
            ),
        )


class FailingFeishuMediaService:
    async def analyze_images(self, message: InboundMessage) -> tuple[StubFeishuImageAnalysis, ...]:
        raise FeishuMediaError(
            "image MIME mismatch",
            diagnostics={
                "channel": message.channel.value,
                "message_id": message.message_id,
                "resource_key": message.attachments[0].external_key,
                "tenant_key": message.tenant_external_id,
                "attachment_kind": message.attachments[0].kind.value,
                "reason": "image MIME mismatch",
            },
        )


def client(gateway: RecordingGateway) -> TestClient:
    return TestClient(
        create_app(
            auth_service=StubAuthService(),
            rate_limiter=object(),
            feishu_gateway=gateway,
        )
    )


def slack_headers(signing_secret: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = "v0=" + hmac.new(
        signing_secret.encode(),
        b"v0:" + timestamp.encode() + b":" + body,
        sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature,
    }


def sha1_query(token: str, timestamp: str = "1700000000", nonce: str = "nonce") -> str:
    signature = sha1("".join(sorted((token, timestamp, nonce))).encode()).hexdigest()
    return f"timestamp={timestamp}&nonce={nonce}&signature={signature}"


@pytest.mark.parametrize(
    ("path", "env", "payload", "channel", "text"),
    [
        (
            "/channels/dingtalk/events",
            {
                "DINGTALK_APP_KEY": "app",
                "DINGTALK_APP_SECRET": "secret",
                "DINGTALK_WEBHOOK_TOKEN": "token",
            },
            {
                "text": {"content": "DingTalk task"},
                "senderStaffId": "u1",
                "conversationId": "c1",
                "msgId": "m1",
            },
            "dingtalk",
            "DingTalk task",
        ),
        (
            "/channels/wecom/bot/events",
            {"WECOM_BOT_WEBHOOK_KEY": "key", "WECOM_BOT_WEBHOOK_TOKEN": "token"},
            {
                "text": "WeCom bot task",
                "sender": "u1",
                "conversation_id": "c1",
                "message_id": "m1",
            },
            "wecom_bot",
            "WeCom bot task",
        ),
        (
            "/channels/qq/events",
            {"QQ_BOT_APP_ID": "app", "QQ_BOT_TOKEN": "bot", "QQ_WEBHOOK_TOKEN": "token"},
            {
                "content": "QQ task",
                "user_id": "u1",
                "conversation_id": "c1",
                "message_id": "m1",
            },
            "qq",
            "QQ task",
        ),
        (
            "/channels/custom/events",
            {"CUSTOM_WEBHOOK_TOKEN": "token"},
            {
                "text": "Custom task",
                "sender": "u1",
                "conversation_id": "c1",
                "message_id": "m1",
            },
            "custom_webhook",
            "Custom task",
        ),
    ],
)
def test_token_channel_webhooks_submit_to_gateway(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    env: dict[str, str],
    payload: dict[str, object],
    channel: str,
    text: str,
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    gateway = RecordingGateway()

    response = client(gateway).post(
        path,
        headers={"X-Agent-Hub-Channel-Token": "token"},
        json=payload,
    )

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "channel": channel}
    assert len(gateway.messages) == 1
    assert gateway.messages[0].channel.value == channel
    assert gateway.messages[0].text == text


def test_generic_channel_webhook_preserves_attachment_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_WEBHOOK_TOKEN", "token")
    gateway = RecordingGateway()

    response = client(gateway).post(
        "/channels/custom/events",
        headers={"X-Agent-Hub-Channel-Token": "token"},
        json={
            "text": "Review this file",
            "sender": "u1",
            "conversation_id": "c1",
            "message_id": "m1",
            "attachments": [
                {
                    "kind": "image",
                    "external_key": "att_0123456789abcdef0123456789abcdef",
                    "filename": "screen.png",
                    "content_type": "image/png",
                }
            ],
        },
    )

    assert response.status_code == 202
    assert gateway.messages[0].attachments[0].kind.value == "image"
    assert gateway.messages[0].attachments[0].external_key == "att_0123456789abcdef0123456789abcdef"


def test_telegram_webhook_accepts_official_secret_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_TOKEN", "telegram-secret")
    gateway = RecordingGateway()

    response = client(gateway).post(
        "/channels/telegram/events",
        headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
        json={
            "message": {
                "text": "Telegram task",
                "from": {"id": "u1"},
                "chat": {"id": "c1"},
                "message_id": "m1",
            },
            "update_id": "e1",
        },
    )

    assert response.status_code == 202
    assert gateway.messages[0].channel.value == "telegram"
    assert gateway.messages[0].text == "Telegram task"


def test_slack_webhook_accepts_signed_event(monkeypatch: pytest.MonkeyPatch) -> None:
    signing_secret = "slack-signing-secret"
    monkeypatch.setenv("SLACK_BOT_TOKEN", "bot")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", signing_secret)
    gateway = RecordingGateway()
    body = (
        b'{"event":{"text":"Slack task","user":"u1","channel":"c1"},'
        b'"event_id":"m1","team_id":"team"}'
    )

    response = client(gateway).post(
        "/channels/slack/events",
        headers=slack_headers(signing_secret, body),
        content=body,
    )

    assert response.status_code == 202
    assert gateway.messages[0].channel.value == "slack"
    assert gateway.messages[0].text == "Slack task"


def test_slack_url_verification_uses_signed_request(monkeypatch: pytest.MonkeyPatch) -> None:
    signing_secret = "slack-signing-secret"
    monkeypatch.setenv("SLACK_BOT_TOKEN", "bot")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", signing_secret)
    gateway = RecordingGateway()
    body = b'{"type":"url_verification","challenge":"challenge-value"}'

    response = client(gateway).post(
        "/channels/slack/events",
        headers=slack_headers(signing_secret, body),
        content=body,
    )

    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-value"}
    assert gateway.messages == []


def test_slack_webhook_requires_slack_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    signing_secret = "slack-signing-secret"
    monkeypatch.setenv("SLACK_BOT_TOKEN", "bot")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", signing_secret)
    gateway = RecordingGateway()

    response = client(gateway).post(
        "/channels/slack/events",
        headers={"X-Agent-Hub-Channel-Token": signing_secret},
        json={"event": {"text": "Slack task"}},
    )

    assert response.status_code == 401
    assert response.json() == {"error": "invalid_channel_token", "channel": "slack"}
    assert gateway.messages == []


def test_wechat_official_webhook_accepts_sha1_signature_and_xml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "wechat-token"
    monkeypatch.setenv("WECHATMP_APP_ID", "app")
    monkeypatch.setenv("WECHATMP_APP_SECRET", "secret")
    monkeypatch.setenv("WECHATMP_TOKEN", token)
    gateway = RecordingGateway()
    body = (
        b"<xml>"
        b"<Content>WeChat public account task</Content>"
        b"<FromUserName>u1</FromUserName>"
        b"<ToUserName>c1</ToUserName>"
        b"<MsgId>m1</MsgId>"
        b"</xml>"
    )

    response = client(gateway).post(
        f"/channels/wechatmp/events?{sha1_query(token)}",
        headers={"Content-Type": "application/xml"},
        content=body,
    )

    assert response.status_code == 202
    assert gateway.messages[0].channel.value == "wechat_official"
    assert gateway.messages[0].text == "WeChat public account task"


def test_wecom_app_url_verification_returns_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "wecom-token"
    monkeypatch.setenv("WECOM_CORP_ID", "corp")
    monkeypatch.setenv("WECOM_AGENT_ID", "agent")
    monkeypatch.setenv("WECOM_SECRET", "secret")
    monkeypatch.setenv("WECOM_TOKEN", token)
    gateway = RecordingGateway()

    response = client(gateway).get(f"/channels/wecom/app/events?{sha1_query(token)}&echostr=ok")

    assert response.status_code == 200
    assert response.text == "ok"
    assert gateway.messages == []


def test_wechat_customer_service_webhook_accepts_sha1_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "wechat-kf-token"
    monkeypatch.setenv("WECHAT_KF_CORP_ID", "corp")
    monkeypatch.setenv("WECHAT_KF_SECRET", "secret")
    monkeypatch.setenv("WECHAT_KF_TOKEN", token)
    gateway = RecordingGateway()

    response = client(gateway).post(
        f"/channels/wechat-kf/events?{sha1_query(token)}",
        json={
            "Content": "WeChat customer service task",
            "FromUserName": "u1",
            "ToUserName": "c1",
            "MsgId": "m1",
        },
    )

    assert response.status_code == 202
    assert gateway.messages[0].channel.value == "wechat_customer_service"
    assert gateway.messages[0].text == "WeChat customer service task"


def test_generic_channel_webhook_rejects_missing_config() -> None:
    gateway = RecordingGateway()
    response = client(gateway).post(
        "/channels/custom/events",
        headers={"X-Agent-Hub-Channel-Token": "token"},
        json={"text": "task"},
    )

    assert response.status_code == 503
    assert response.json()["error"] == "channel_not_configured"
    assert response.json()["missing"] == ["CUSTOM_WEBHOOK_TOKEN"]
    assert gateway.messages == []


def test_saved_channel_config_is_used_by_webhook_runtime() -> None:
    gateway = RecordingGateway()
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
        feishu_gateway=gateway,
    )
    app.state.admin_resource_service = InMemoryAdminResourceService()
    api = TestClient(app)

    saved = api.post(
        "/api/v1/admin/channels/custom_webhook/config",
        headers={"Authorization": "Bearer valid-token"},
        json={"values": {"CUSTOM_WEBHOOK_TOKEN": "saved-token"}},
    )
    response = api.post(
        "/channels/custom/events",
        headers={"X-Agent-Hub-Channel-Token": "saved-token"},
        json={"text": "Runtime channel task", "sender": "u1", "conversation_id": "c1"},
    )

    assert saved.status_code == 200
    assert response.status_code == 202
    assert response.json() == {"accepted": True, "channel": "custom_webhook"}
    assert gateway.messages[0].text == "Runtime channel task"


def test_saved_feishu_config_is_used_by_webhook_runtime() -> None:
    gateway = RecordingGateway()
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
        feishu_gateway=gateway,
    )
    app.state.admin_resource_service = InMemoryAdminResourceService()
    api = TestClient(app)
    verifier = FeishuVerifier(
        app_id="cli_saved_feishu",
        verification_token="saved-verification-token",
        encrypt_key="saved-encrypt-key",
    )
    body = b'{"token":"saved-verification-token","challenge":"challenge-from-saved-config"}'
    timestamp = str(int(time.time()))
    nonce = "nonce"

    saved = api.post(
        "/api/v1/admin/channels/feishu/config",
        headers={"Authorization": "Bearer valid-token"},
        json={
            "values": {
                "AGENT_HUB_PUBLIC_URL": "https://agent.example.com",
                "FEISHU_APP_ID": "cli_saved_feishu",
                "FEISHU_APP_SECRET": "saved-secret",
                "FEISHU_VERIFICATION_TOKEN": "saved-verification-token",
                "FEISHU_ENCRYPT_KEY": "saved-encrypt-key",
                "FEISHU_TRANSPORT": "webhook",
            }
        },
    )
    response = api.post(
        "/channels/feishu/events",
        headers={
            "Content-Type": "application/json",
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": verifier.sign(body, timestamp=timestamp, nonce=nonce),
        },
        content=body,
    )

    assert saved.status_code == 200
    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-from-saved-config"}
    assert gateway.messages == []


def test_feishu_webhook_accepts_token_only_event_when_signature_headers_are_absent() -> None:
    gateway = RecordingGateway()
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
        feishu_gateway=gateway,
    )
    app.state.admin_resource_service = InMemoryAdminResourceService()
    api = TestClient(app)

    saved = api.post(
        "/api/v1/admin/channels/feishu/config",
        headers={"Authorization": "Bearer valid-token"},
        json={
            "values": {
                "AGENT_HUB_PUBLIC_URL": "https://agent.example.com",
                "FEISHU_APP_ID": "cli_saved_feishu",
                "FEISHU_APP_SECRET": "saved-secret",
                "FEISHU_VERIFICATION_TOKEN": "saved-verification-token",
                "FEISHU_ENCRYPT_KEY": "saved-encrypt-key",
                "FEISHU_TRANSPORT": "webhook",
            }
        },
    )
    response = api.post(
        "/channels/feishu/events",
        json={
            "schema": "2.0",
            "header": {
                "event_id": "evt_unsigned",
                "event_type": "im.message.receive_v1",
                "token": "saved-verification-token",
                "app_id": "cli_saved_feishu",
                "tenant_key": "tenant_1",
                "create_time": str(int(time.time())),
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_unsigned",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": "{\"text\":\"/direct hello\"}",
                },
            },
        },
    )

    assert saved.status_code == 200
    assert response.status_code == 202
    assert gateway.messages[0].text == "/direct hello"


def test_feishu_webhook_appends_image_analysis_context() -> None:
    gateway = RecordingGateway()
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
        feishu_gateway=gateway,
    )
    app.state.admin_resource_service = InMemoryAdminResourceService()
    media_service = StubFeishuMediaService()
    app.state.feishu_media_service = media_service
    api = TestClient(app)

    saved = api.post(
        "/api/v1/admin/channels/feishu/config",
        headers={"Authorization": "Bearer valid-token"},
        json={
            "values": {
                "AGENT_HUB_PUBLIC_URL": "https://agent.example.com",
                "FEISHU_APP_ID": "cli_saved_feishu",
                "FEISHU_APP_SECRET": "saved-secret",
                "FEISHU_VERIFICATION_TOKEN": "saved-verification-token",
                "FEISHU_ENCRYPT_KEY": "saved-encrypt-key",
                "FEISHU_TRANSPORT": "webhook",
            }
        },
    )
    response = api.post(
        "/channels/feishu/events",
        json={
            "schema": "2.0",
            "header": {
                "event_id": "evt_image",
                "event_type": "im.message.receive_v1",
                "token": "saved-verification-token",
                "app_id": "cli_saved_feishu",
                "tenant_key": "tenant_1",
                "create_time": str(int(time.time())),
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_image",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "image",
                    "content": (
                        "{\"image_key\":\"img_123\",\"mime_type\":\"image/png\","
                        "\"file_name\":\"diagram.png\"}"
                    ),
                },
            },
        },
    )

    assert saved.status_code == 200
    assert response.status_code == 202
    assert len(media_service.messages) == 1
    assert len(gateway.messages) == 1
    assert gateway.messages[0].text == (
        "[image]\n\nChannel image analysis:\n"
        "- resource_key=img_123; summary=whiteboard architecture diagram"
    )


def test_feishu_webhook_uses_media_service_factory_with_runtime_settings() -> None:
    gateway = RecordingGateway()
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
        feishu_gateway=gateway,
    )
    app.state.admin_resource_service = InMemoryAdminResourceService()
    media_service = StubFeishuMediaService()
    settings_seen: list[str] = []

    def factory(settings: FeishuSettings) -> StubFeishuMediaService:
        settings_seen.append(settings.app_id)
        return media_service

    app.state.feishu_media_service_factory = factory
    api = TestClient(app)

    saved = api.post(
        "/api/v1/admin/channels/feishu/config",
        headers={"Authorization": "Bearer valid-token"},
        json={
            "values": {
                "AGENT_HUB_PUBLIC_URL": "https://agent.example.com",
                "FEISHU_APP_ID": "cli_runtime_feishu",
                "FEISHU_APP_SECRET": "saved-secret",
                "FEISHU_VERIFICATION_TOKEN": "saved-verification-token",
                "FEISHU_ENCRYPT_KEY": "saved-encrypt-key",
                "FEISHU_TRANSPORT": "webhook",
            }
        },
    )
    response = api.post(
        "/channels/feishu/events",
        json={
            "schema": "2.0",
            "header": {
                "event_id": "evt_factory_image",
                "event_type": "im.message.receive_v1",
                "token": "saved-verification-token",
                "app_id": "cli_runtime_feishu",
                "tenant_key": "tenant_1",
                "create_time": str(int(time.time())),
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_factory_image",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "image",
                    "content": "{\"image_key\":\"img_123\",\"mime_type\":\"image/png\"}",
                },
            },
        },
    )

    assert saved.status_code == 200
    assert response.status_code == 202
    assert settings_seen == ["cli_runtime_feishu"]
    assert len(media_service.messages) == 1
    assert "whiteboard architecture diagram" in gateway.messages[0].text


def test_feishu_webhook_logs_media_failure_and_submits_original_message() -> None:
    gateway = RecordingGateway()
    service = InMemoryAdminResourceService()
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
        feishu_gateway=gateway,
    )
    app.state.admin_resource_service = service
    app.state.feishu_media_service = FailingFeishuMediaService()
    api = TestClient(app)

    saved = api.post(
        "/api/v1/admin/channels/feishu/config",
        headers={"Authorization": "Bearer valid-token"},
        json={
            "values": {
                "AGENT_HUB_PUBLIC_URL": "https://agent.example.com",
                "FEISHU_APP_ID": "cli_saved_feishu",
                "FEISHU_APP_SECRET": "saved-secret",
                "FEISHU_VERIFICATION_TOKEN": "saved-verification-token",
                "FEISHU_ENCRYPT_KEY": "saved-encrypt-key",
                "FEISHU_TRANSPORT": "webhook",
            }
        },
    )
    response = api.post(
        "/channels/feishu/events",
        json={
            "schema": "2.0",
            "header": {
                "event_id": "evt_bad_image",
                "event_type": "im.message.receive_v1",
                "token": "saved-verification-token",
                "app_id": "cli_saved_feishu",
                "tenant_key": "tenant_1",
                "create_time": str(int(time.time())),
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_bad_image",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "image",
                    "content": "{\"image_key\":\"img_bad\",\"mime_type\":\"image/png\"}",
                },
            },
        },
    )
    logs = api.get(
        "/api/v1/admin/logs?category=channel_error",
        headers={"Authorization": "Bearer valid-token"},
    )

    assert saved.status_code == 200
    assert response.status_code == 202
    assert gateway.messages[0].text == "[image]"
    matching = [
        item
        for item in logs.json()
        if item["source"] == "channels.feishu.media"
        and item["details"].get("message_id") == "om_bad_image"
    ]
    assert matching
    assert matching[0]["details"]["resource_key"] == "img_bad"


def test_feishu_webhook_acks_supported_platform_events_even_when_no_message() -> None:
    gateway = RecordingGateway()
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
        feishu_gateway=gateway,
    )
    app.state.admin_resource_service = InMemoryAdminResourceService()
    api = TestClient(app)

    saved = api.post(
        "/api/v1/admin/channels/feishu/config",
        headers={"Authorization": "Bearer valid-token"},
        json={
            "values": {
                "AGENT_HUB_PUBLIC_URL": "https://agent.example.com",
                "FEISHU_APP_ID": "cli_saved_feishu",
                "FEISHU_APP_SECRET": "saved-secret",
                "FEISHU_VERIFICATION_TOKEN": "saved-verification-token",
                "FEISHU_ENCRYPT_KEY": "saved-encrypt-key",
                "FEISHU_TRANSPORT": "webhook",
            }
        },
    )
    response = api.post(
        "/channels/feishu/events",
        json={
            "schema": "2.0",
            "header": {
                "event_id": "evt_p2p_entered",
                "event_type": "im.chat.access_event.bot_p2p_chat_entered_v1",
                "token": "saved-verification-token",
                "app_id": "cli_saved_feishu",
                "tenant_key": "tenant_1",
                "create_time": str(int(time.time())),
            },
            "event": {
                "operator_id": {"open_id": "ou_user"},
                "chat_id": "oc_chat",
            },
        },
    )

    assert saved.status_code == 200
    assert response.status_code == 200
    assert response.json() == {"accepted": True, "ignored": True}
    assert gateway.messages == []


def test_feishu_webhook_records_ignored_platform_event_diagnostics() -> None:
    gateway = RecordingGateway()
    service = InMemoryAdminResourceService()
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
        feishu_gateway=gateway,
    )
    app.state.admin_resource_service = service
    api = TestClient(app)

    saved = api.post(
        "/api/v1/admin/channels/feishu/config",
        headers={"Authorization": "Bearer valid-token"},
        json={
            "values": {
                "AGENT_HUB_PUBLIC_URL": "https://agent.example.com",
                "FEISHU_APP_ID": "cli_saved_feishu",
                "FEISHU_APP_SECRET": "saved-secret",
                "FEISHU_VERIFICATION_TOKEN": "saved-verification-token",
                "FEISHU_ENCRYPT_KEY": "saved-encrypt-key",
                "FEISHU_TRANSPORT": "webhook",
            }
        },
    )
    response = api.post(
        "/channels/feishu/events",
        json={
            "schema": "2.0",
            "header": {
                "event_id": "evt_group_entered",
                "event_type": "im.chat.member.bot.added_v1",
                "token": "saved-verification-token",
                "app_id": "cli_saved_feishu",
                "tenant_key": "tenant_1",
                "create_time": str(int(time.time())),
            },
            "event": {
                "operator_id": {"open_id": "ou_user"},
                "chat_id": "oc_chat",
            },
        },
    )
    logs = api.get(
        "/api/v1/admin/logs?category=channel_error",
        headers={"Authorization": "Bearer valid-token"},
    )

    assert saved.status_code == 200
    assert response.status_code == 200
    assert response.json() == {"accepted": True, "ignored": True}
    assert gateway.messages == []
    assert logs.status_code == 200
    matching = [
        item
        for item in logs.json()
        if item["source"] == "channels.feishu.webhook"
        and item["details"].get("event_id") == "evt_group_entered"
    ]
    assert matching
    assert matching[0]["level"] == "warning"
    assert matching[0]["details"] == {
        "event_id": "evt_group_entered",
        "event_type": "im.chat.member.bot.added_v1",
        "reason": "unsupported event type",
        "tenant_key": "tenant_1",
    }


def test_generic_channel_webhook_rejects_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_WEBHOOK_TOKEN", "correct")
    gateway = RecordingGateway()

    response = client(gateway).post(
        "/channels/custom/events",
        headers={"X-Agent-Hub-Channel-Token": "wrong"},
        json={"text": "task"},
    )

    assert response.status_code == 401
    assert response.json() == {"error": "invalid_channel_token", "channel": "custom_webhook"}
    assert gateway.messages == []
