from __future__ import annotations

import hmac
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha1, sha256
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from agent_hub.api.routers.admin import InMemoryAdminResourceService
from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.channels.base import InboundMessage
from agent_hub.channels.feishu.media import FeishuMediaError
from agent_hub.channels.feishu.reply import FeishuRunReplyDispatcher
from agent_hub.channels.feishu.settings import FeishuSettings
from agent_hub.channels.feishu.verify import FeishuVerifier
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRecord, RunRepository

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


class RecordingFeishuReplySender:
    def __init__(self) -> None:
        self.replies: list[tuple[FeishuSettings, str, str]] = []

    async def reply_text(
        self,
        *,
        settings: FeishuSettings,
        message_id: str,
        text: str,
    ) -> None:
        self.replies.append((settings, message_id, text))


class StructuredRunRepository:
    run_id = UUID("22222222-2222-4222-8222-222222222222")

    async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
        return RunRecord(
            id=run_id,
            tenant_id=tenant_id,
            actor_id=USER_ID,
            request="给我生成一个中秋晚会的方案",
            mode=TaskMode.HYBRID,
            status=RunStatus.COMPLETED,
            version=3,
            created_at=datetime(2026, 8, 15, tzinfo=UTC),
            routing_decision={
                "main_agent_model": "main-m3",
                "selected_agent_ids": ["planner", "quality_reviewer"],
            },
        )

    async def artifacts(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
        del tenant_id, run_id
        return (
            {
                "id": "artifact-step",
                "type": "text",
                "producer": "planner",
                "content": {"text": "流程方案草案。"},
            },
            {
                "id": "artifact-final",
                "type": "text",
                "producer": "final_synthesizer",
                "content": {"text": "最终方案：主题、流程、预算、风险和验收标准。"},
            },
        )

    async def events(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
        del tenant_id, run_id
        return (
            {
                "kind": "step.started",
                "step_id": "main_agent_plan",
                "actor": "main_agent",
                "payload": {
                    "main_agent_model": "main-m3",
                    "roles": [
                        {
                            "id": "planner",
                            "role": "Planner",
                            "purpose": "execute",
                            "logical_model": "m3",
                        },
                        {
                            "id": "quality_reviewer",
                            "role": "Quality Reviewer",
                            "purpose": "verify",
                            "logical_model": "m3",
                        },
                    ],
                },
            },
            {
                "kind": "step.completed",
                "step_id": "planner_step",
                "actor": "planner",
                "payload": {
                    "role": "Planner",
                    "logical_model": "m3",
                    "output": "给出活动流程和物料清单。",
                },
            },
            {
                "kind": "discussion.started",
                "actor": "planner",
                "session_id": "session-1",
                "participants": ["planner", "quality_reviewer"],
            },
            {
                "kind": "message.created",
                "actor": "planner",
                "message": "方案主线可采用游园会加晚会。",
                "payload": {"logical_model": "m3"},
            },
            {
                "kind": "message.created",
                "actor": "quality_reviewer",
                "message": "需要补充天气和安全预案。",
                "payload": {"logical_model": "m3"},
            },
            {
                "kind": "review.completed",
                "actor": "quality_reviewer",
                "payload": {
                    "role": "Quality Reviewer",
                    "verdict": "approve",
                    "feedback": "已覆盖核心验收项。",
                },
            },
            {"kind": "checkpoint.saved", "message": "internal checkpoint"},
            {"kind": "model.started", "message": "internal model call"},
        )


class RecordingFeishuSkillHandler:
    def __init__(self, reply_text: str = "Skill 已扫描入库，待审批：writer") -> None:
        self.messages: list[InboundMessage] = []
        self.reply_text = reply_text

    async def handle(self, message: InboundMessage, *, settings: FeishuSettings) -> object:
        del settings
        self.messages.append(message)

        class Result:
            handled = True
            reply_text = self.reply_text

        return Result()


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
    signature = (
        "v0="
        + hmac.new(
            signing_secret.encode(),
            b"v0:" + timestamp.encode() + b":" + body,
            sha256,
        ).hexdigest()
    )
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


def test_generic_channel_webhook_preserves_attachment_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                    "content": '{"text":"/direct hello"}',
                },
            },
        },
    )

    assert saved.status_code == 200
    assert response.status_code == 202
    assert gateway.messages[0].text == "/direct hello"


def test_saved_feishu_command_aliases_do_not_rewrite_channel_text() -> None:
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
                "FEISHU_COMMAND_ALIASES": "方案=//派单, 代码=//vi",
                "FEISHU_TRANSPORT": "webhook",
            }
        },
    )
    response = api.post(
        "/channels/feishu/events",
        json={
            "schema": "2.0",
            "header": {
                "event_id": "evt_alias",
                "event_type": "im.message.receive_v1",
                "token": "saved-verification-token",
                "app_id": "cli_saved_feishu",
                "tenant_key": "tenant_1",
                "create_time": str(int(time.time())),
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_alias",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": '{"text":"方案 写一个中秋晚会方案"}',
                },
            },
        },
    )

    assert saved.status_code == 200
    assert set(saved.json()["saved"]) == {
        "AGENT_HUB_PUBLIC_URL",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_VERIFICATION_TOKEN",
        "FEISHU_ENCRYPT_KEY",
        "FEISHU_COMMAND_ALIASES",
        "FEISHU_TRANSPORT",
    }
    assert response.status_code == 202
    assert gateway.messages[0].text == "方案 写一个中秋晚会方案"


def test_feishu_webhook_submits_help_text_to_main_agent_entry() -> None:
    gateway = RecordingGateway()
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
        feishu_gateway=gateway,
    )
    app.state.admin_resource_service = InMemoryAdminResourceService()
    reply_sender = RecordingFeishuReplySender()
    app.state.feishu_reply_sender = reply_sender
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
                "FEISHU_COMMAND_ALIASES": "菜单=//帮助, 方案=//派单",
                "FEISHU_TRANSPORT": "webhook",
            }
        },
    )
    response = api.post(
        "/channels/feishu/events",
        json={
            "schema": "2.0",
            "header": {
                "event_id": "evt_help_alias",
                "event_type": "im.message.receive_v1",
                "token": "saved-verification-token",
                "app_id": "cli_saved_feishu",
                "tenant_key": "tenant_1",
                "create_time": str(int(time.time())),
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_help_alias",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": '{"text":"菜单"}',
                },
            },
        },
    )

    assert saved.status_code == 200
    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert gateway.messages[0].text == "菜单"
    assert reply_sender.replies == []


def test_feishu_webhook_appends_image_analysis_context() -> None:
    gateway = RecordingGateway()
    service = InMemoryAdminResourceService()
    service.settings = service.settings.model_copy(update={"multimedia_generation_enabled": True})
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
        feishu_gateway=gateway,
    )
    app.state.admin_resource_service = service
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
                        '{"image_key":"img_123","mime_type":"image/png","file_name":"diagram.png"}'
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


def test_feishu_webhook_replies_when_image_arrives_with_multimodal_disabled() -> None:
    gateway = RecordingGateway()
    service = InMemoryAdminResourceService()
    media_service = StubFeishuMediaService()
    reply_sender = RecordingFeishuReplySender()
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
        feishu_gateway=gateway,
    )
    app.state.admin_resource_service = service
    app.state.feishu_media_service = media_service
    app.state.feishu_reply_sender = reply_sender
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
                "event_id": "evt_image_disabled",
                "event_type": "im.message.receive_v1",
                "token": "saved-verification-token",
                "app_id": "cli_saved_feishu",
                "tenant_key": "tenant_1",
                "create_time": str(int(time.time())),
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_image_disabled",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "image",
                    "content": '{"image_key":"img_123","mime_type":"image/png"}',
                },
            },
        },
    )

    assert saved.status_code == 200
    assert response.status_code == 202
    assert gateway.messages == []
    assert media_service.messages == []
    assert len(reply_sender.replies) == 1
    assert reply_sender.replies[0][1] == "om_image_disabled"
    assert "暂时无法处理图片" in reply_sender.replies[0][2]


def test_feishu_webhook_routes_skill_file_command_to_protected_handler() -> None:
    gateway = RecordingGateway()
    service = InMemoryAdminResourceService()
    reply_sender = RecordingFeishuReplySender()
    skill_handler = RecordingFeishuSkillHandler()
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
        feishu_gateway=gateway,
    )
    app.state.admin_resource_service = service
    app.state.feishu_reply_sender = reply_sender
    app.state.feishu_skill_command_handler = skill_handler
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
                "event_id": "evt_skill_file",
                "event_type": "im.message.receive_v1",
                "token": "saved-verification-token",
                "app_id": "cli_saved_feishu",
                "tenant_key": "tenant_1",
                "create_time": str(int(time.time())),
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_skill_file",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "file",
                    "content": (
                        '{"file_key":"file_1","file_name":"writer.zip",'
                        '"mime_type":"application/zip","text":"/skill install"}'
                    ),
                },
            },
        },
    )

    assert saved.status_code == 200
    assert response.status_code == 202
    assert gateway.messages == []
    assert len(skill_handler.messages) == 1
    assert skill_handler.messages[0].text == "/skill install"
    assert skill_handler.messages[0].attachments[0].kind.value == "file"
    assert reply_sender.replies[0][1] == "om_skill_file"
    assert "待审批" in reply_sender.replies[0][2]


def test_feishu_webhook_uses_media_service_factory_with_runtime_settings() -> None:
    gateway = RecordingGateway()
    service = InMemoryAdminResourceService()
    service.settings = service.settings.model_copy(update={"multimedia_generation_enabled": True})
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
        feishu_gateway=gateway,
    )
    app.state.admin_resource_service = service
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
                    "content": '{"image_key":"img_123","mime_type":"image/png"}',
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
    service.settings = service.settings.model_copy(update={"multimedia_generation_enabled": True})
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
                    "content": '{"image_key":"img_bad","mime_type":"image/png"}',
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


def test_feishu_terminal_reply_summarizes_user_relevant_run_process() -> None:
    sender = RecordingFeishuReplySender()
    repository = StructuredRunRepository()
    dispatcher = FeishuRunReplyDispatcher(
        run_repository=cast(RunRepository, repository),
        sender=sender,
        poll_interval_seconds=0.01,
        timeout_seconds=1.0,
    )

    async def run() -> None:
        await dispatcher.reply_when_terminal(
            tenant_id=TENANT_ID,
            run_id=StructuredRunRepository.run_id,
            source_message_id="om_process_summary",
            settings=FeishuSettings.model_validate(
                {"app_id": "cli_saved_feishu", "app_secret": "secret"}
            ),
        )

    import asyncio

    asyncio.run(run())

    assert len(sender.replies) == 1
    text = sender.replies[0][2]
    assert "最终结果" in text
    assert "最终方案：主题、流程、预算、风险和验收标准。" in text
    assert "Agent 调度" in text
    assert "主 Agent 模型: main-m3" in text
    assert "子 Agent 输出" in text
    assert "Planner(planner)[m3]: 给出活动流程和物料清单。" in text
    assert "讨论情况" in text
    assert "quality_reviewer[m3]: 需要补充天气和安全预案。" in text
    assert "裁决情况" in text
    assert "Quality Reviewer(quality_reviewer): approve - 已覆盖核心验收项。" in text
    assert "checkpoint.saved" not in text
    assert "model.started" not in text
    assert "internal checkpoint" not in text
