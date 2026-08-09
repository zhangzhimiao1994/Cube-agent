import asyncio
import io
import zipfile
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers.admin import (
    AgentResourceRequest,
    InMemoryAdminResourceService,
    ModelDeploymentRequest,
    PersistentAdminResourceService,
    SecretCreateRequest,
)
from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, TokenUsage
from agent_hub.security.secrets import SecretReference


class FakeConfigService:
    def __init__(self) -> None:
        self.current: ConfigRevision | None = None
        self.drafts: list[dict[str, object]] = []

    async def get_current(self, tenant_id: UUID) -> ConfigRevision | None:
        assert tenant_id == TENANT_ID
        return self.current

    async def create_draft(
        self,
        tenant_id: UUID,
        actor_id: UUID,
        document: object,
    ) -> ConfigRevision:
        assert tenant_id == TENANT_ID
        assert actor_id == ACTOR_ID
        assert isinstance(document, dict)
        self.drafts.append(document)
        return ConfigRevision(
            id=uuid4(),
            tenant_id=tenant_id,
            version=len(self.drafts),
            status=ConfigStatus.DRAFT,
            document=document,
            created_by=actor_id,
            created_at=datetime.now(UTC),
        )

    async def publish(
        self,
        tenant_id: UUID,
        version: int,
        actor_id: UUID,
    ) -> ConfigRevision:
        assert tenant_id == TENANT_ID
        assert actor_id == ACTOR_ID
        document = self.drafts[version - 1]
        self.current = ConfigRevision(
            id=uuid4(),
            tenant_id=tenant_id,
            version=version,
            status=ConfigStatus.PUBLISHED,
            document=document,
            created_by=actor_id,
            created_at=datetime.now(UTC),
        )
        return self.current


class FakeSecretService:
    def __init__(self) -> None:
        self.values: list[str] = []
        self.resolved: list[tuple[UUID, str]] = []

    async def create_or_get(
        self,
        tenant_id: UUID,
        actor_id: UUID,
        plaintext: str,
    ) -> SecretReference:
        assert tenant_id == TENANT_ID
        assert actor_id == ACTOR_ID
        self.values.append(plaintext)
        return SecretReference(tenant_id=tenant_id, secret_id=SECRET_ID)

    async def resolve(self, tenant_id: UUID, reference: object) -> str:
        assert tenant_id == TENANT_ID
        assert isinstance(reference, str)
        self.resolved.append((tenant_id, reference))
        return "sk-live"


class FakeModelTransport:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[Deployment, ModelRequest, str]] = []

    async def complete(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> ModelResponse:
        self.calls.append((deployment, request, api_key))
        if self.error is not None:
            raise self.error
        return ModelResponse(
            text="agent-hub-model-check-ok",
            usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        )


TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
ACTOR_ID = UUID("11111111-1111-4111-8111-111111111111")
SECRET_ID = UUID("22222222-2222-4222-8222-222222222222")
USER_ID = UUID("11111111-1111-4111-8111-111111111111")


class StubAuthService:
    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid-token":
            raise InvalidCredentials("bad token")
        return AuthenticatedPrincipal(USER_ID, TENANT_ID, Role.SUPER_ADMIN)


def client() -> TestClient:
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
    )
    app.state.admin_resource_service = InMemoryAdminResourceService()
    return TestClient(app)


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer valid-token"}


def model_payload() -> dict[str, object]:
    return {
        "provider": "deepseek",
        "api_base": "https://api.deepseek.example/v1",
        "upstream_model": "deepseek-chat",
        "logical_model": "planner",
        "capabilities": ["text", "tool_calling"],
        "credential_ref": "secret_1",
        "quota_scope": "deepseek_account_1",
        "max_concurrency": 1,
        "target_utilization": 0.8,
        "reserved_capacity": 0,
        "rpm": 60,
        "tpm": 100000,
        "queue_timeout_seconds": 60,
        "fallback": "planner_backup",
        "weight": 100,
    }


def skill_archive() -> bytes:
    manifest = (
        "name: safe_skill\n"
        "version: 1.0.0\n"
        "entry_point: main.py\n"
        "compatible_runtime: python3.12\n"
        "declared_tools:\n"
        "  - filesystem.read\n"
        "dependency_lock_hash: "
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("skill.yaml", manifest)
        archive.writestr("main.py", "print('ok')\n")
    return buffer.getvalue()


def test_model_pool_reports_serial_slot_and_queue_policy() -> None:
    response = client().post("/api/v1/admin/models", headers=headers(), json=model_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["upstream_model"] == "deepseek-chat"
    assert body["effective_slots"] == 1
    assert body["saturation_policy"] == "queue_first_then_fallback"


def test_secret_create_and_get_never_return_value_or_fingerprint() -> None:
    api = client()

    created = api.post(
        "/api/v1/admin/secrets",
        headers=headers(),
        json={"label": "deepseek", "value": "sk-secret-value"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["last_four"] == "alue"
    assert "sk-secret-value" not in created.text
    assert "fingerprint" not in body

    fetched = api.get(f"/api/v1/admin/secrets/{body['ref']}", headers=headers())
    assert fetched.status_code == 200
    assert "sk-secret-value" not in fetched.text
    assert "fingerprint" not in fetched.json()


def test_duplicate_secret_is_rejected_by_fingerprint_without_disclosure() -> None:
    api = client()

    first = api.post(
        "/api/v1/admin/secrets",
        headers=headers(),
        json={"label": "one", "value": "same-secret"},
    )
    second = api.post(
        "/api/v1/admin/secrets",
        headers=headers(),
        json={"label": "two", "value": "same-secret"},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert "same-secret" not in second.text


def test_probe_returns_non_saturating_recommendation() -> None:
    response = client().post(
        "/api/v1/admin/models/probe",
        headers=headers(),
        json={"quota_scope": "deepseek_account_1", "desired_concurrency": 32},
    )

    assert response.status_code == 200
    assert response.json()["recommended_concurrency"] == 8
    assert "explicitly" in response.json()["warning"]


def test_draft_diff_publish_conflict_and_rollback() -> None:
    api = client()

    draft = api.put(
        "/api/v1/admin/config/draft",
        headers=headers(),
        json={"yaml": "models:\n  - planner\n"},
    )
    diff = api.post(
        "/api/v1/admin/config/diff",
        headers=headers(),
        json={"yaml": "models:\n  - planner\n"},
    )
    publish = api.post(
        "/api/v1/admin/config/publish",
        headers=headers(),
        json={"expected_version": 0},
    )
    conflict = api.post(
        "/api/v1/admin/config/publish",
        headers=headers(),
        json={"expected_version": 0},
    )
    rollback = api.post("/api/v1/admin/config/rollback/0", headers=headers())

    assert draft.json() == {"version": 0, "status": "draft"}
    assert diff.json()["changed"] == ["configuration"]
    assert publish.json() == {"version": 1, "status": "published"}
    assert conflict.status_code == 409
    assert rollback.json() == {"version": 0, "status": "rolled_back"}


def test_agent_and_workflow_crud() -> None:
    api = client()

    agent = api.post(
        "/api/v1/admin/agents",
        headers=headers(),
        json={"id": "planner", "name": "Planner", "enabled": True},
    )
    workflow = api.post(
        "/api/v1/admin/workflows",
        headers=headers(),
        json={"id": "dispatch", "name": "Dispatch", "enabled": True},
    )

    assert agent.status_code == 200
    assert workflow.status_code == 200
    assert api.get("/api/v1/admin/agents", headers=headers()).json()[0]["id"] == "planner"
    assert (
        api.get("/api/v1/admin/workflows", headers=headers()).json()[0]["id"] == "dispatch"
    )


def test_channel_status_exposes_feishu_setup_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_live")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret-live")
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "verify-live")
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "encrypt-live")
    monkeypatch.setenv("FEISHU_TRANSPORT", "webhook")
    monkeypatch.setenv("AGENT_HUB_PUBLIC_URL", "https://agent.example.com")

    response = client().get("/api/v1/admin/channels", headers=headers())

    assert response.status_code == 200
    payload = response.json()
    by_id = {item["id"]: item for item in payload}
    assert by_id["feishu"]["status"] == "configured"
    assert (
        by_id["feishu"]["public_webhook_url"]
        == "https://agent.example.com/channels/feishu/events"
    )
    assert by_id["feishu"]["missing"] == []
    assert {
        "feishu",
        "dingtalk",
        "wecom_bot",
        "wecom_app",
        "wechat_official",
        "wechat_customer_service",
        "telegram",
        "slack",
        "qq",
        "custom_webhook",
    }.issubset(by_id)
    assert by_id["wechat_official"]["status"] == "missing_config"
    assert by_id["custom_webhook"]["status"] == "missing_config"
    serialized = response.text
    assert "secret-live" not in serialized
    assert "verify-live" not in serialized
    assert "encrypt-live" not in serialized


def test_all_channel_statuses_are_configured_when_required_env_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AGENT_HUB_PUBLIC_URL",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_VERIFICATION_TOKEN",
        "FEISHU_ENCRYPT_KEY",
        "DINGTALK_APP_KEY",
        "DINGTALK_APP_SECRET",
        "DINGTALK_WEBHOOK_TOKEN",
        "WECOM_BOT_WEBHOOK_KEY",
        "WECOM_BOT_WEBHOOK_TOKEN",
        "WECOM_CORP_ID",
        "WECOM_AGENT_ID",
        "WECOM_SECRET",
        "WECOM_TOKEN",
        "WECHATMP_APP_ID",
        "WECHATMP_APP_SECRET",
        "WECHATMP_TOKEN",
        "WECHAT_KF_CORP_ID",
        "WECHAT_KF_SECRET",
        "WECHAT_KF_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "QQ_BOT_APP_ID",
        "QQ_BOT_TOKEN",
        "QQ_WEBHOOK_TOKEN",
        "CUSTOM_WEBHOOK_TOKEN",
    ):
        monkeypatch.setenv(name, "configured")

    response = client().get("/api/v1/admin/channels", headers=headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload
    assert {item["status"] for item in payload} == {"configured"}
    assert all(item["missing"] == [] for item in payload)


def test_operational_run_listing_details_and_controls() -> None:
    api = client()

    runs = api.get("/api/v1/admin/runs", headers=headers())
    assert runs.status_code == 200
    run_id = runs.json()[0]["id"]
    assert runs.json()[0]["queue_wait_ms"] >= 0
    assert runs.json()[0]["capacity_wait_ms"] >= 0
    assert runs.json()[0]["cost_usd"] == "0.0132"

    detail = api.get(f"/api/v1/admin/runs/{run_id}", headers=headers())
    pause = api.post(f"/api/v1/admin/runs/{run_id}/pause", headers=headers())
    resume = api.post(f"/api/v1/admin/runs/{run_id}/resume", headers=headers())
    cancel = api.post(f"/api/v1/admin/runs/{run_id}/cancel", headers=headers())

    assert detail.status_code == 200
    assert detail.json()["mode"] == "dispatch"
    assert detail.json()["events"][0]["kind"] == "queued"
    assert detail.json()["artifacts"][0]["title"] == "Readiness report"
    assert pause.json()["status"] == "paused"
    assert resume.json()["status"] == "running"
    assert cancel.json()["status"] == "cancelled"


def test_skill_upload_approve_mcp_memory_and_audit_are_safe() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills",
        headers=headers(),
        json={"filename": "safe-skill.zip"},
    )
    approved = api.post(
        f"/api/v1/admin/skills/{uploaded.json()['id']}/approve",
        headers=headers(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())
    created_mcp = api.post(
        "/api/v1/admin/mcp",
        headers=headers(),
        json={"id": "browser", "name": "Browser MCP", "allowed_tools": ["open_page"]},
    )
    mcp = api.get("/api/v1/admin/mcp", headers=headers())
    created_memory = api.post(
        "/api/v1/admin/memory",
        headers=headers(),
        json={
            "id": "logging-policy",
            "scope": "tenant",
            "value": "Default production log collection level is warning.",
        },
    )
    memory = api.get("/api/v1/admin/memory", headers=headers())
    updated_memory = api.patch(
        f"/api/v1/admin/memory/{memory.json()[0]['id']}",
        headers=headers(),
        json={"value": "Updated non-dangerous operation policy."},
    )
    audit = api.get("/api/v1/admin/audit?action=config.publish", headers=headers())

    assert uploaded.json()["status"] == "quarantined"
    assert approved.json()["status"] == "enabled"
    assert skills.json()[0]["requested_permissions"] == ["filesystem:read"]
    assert created_mcp.json()["health"] == "configured"
    assert any(item["id"] == "browser" for item in mcp.json())
    assert created_memory.json()["id"] == "logging-policy"
    assert any(item["id"] == "logging-policy" for item in memory.json())
    assert updated_memory.json()["value"] == "Updated non-dangerous operation policy."
    assert audit.json()[0]["action"] == "config.publish"
    serialized = uploaded.text + approved.text + skills.text + mcp.text + memory.text + audit.text
    for forbidden in ("api_key", "fingerprint", "hidden_reasoning", "chain_of_thought"):
        assert forbidden not in serialized.lower()


def test_unified_logs_include_audit_model_mode_and_feature_errors() -> None:
    api = client()
    app = cast(Any, api.app)
    service = cast(
        InMemoryAdminResourceService,
        app.state.admin_resource_service,
    )

    service.logs.extend(
        [
            service.make_log(
                category="model_error",
                level="error",
                title="模型可用性测试失败",
                message="provider returned status=401",
                source="models.create",
                details={"provider": "deepseek", "status_code": "401"},
            ),
            service.make_log(
                category="mode_error",
                level="error",
                title="模式运行失败",
                message="dispatch runtime failed",
                source="runs.execute",
                details={"mode": "dispatch"},
            ),
            service.make_log(
                category="feature_error",
                level="warning",
                title="主要功能运行错误",
                message="skill package is invalid",
                source="skills.upload",
                details={"feature": "skills"},
            ),
            service.make_log(
                category="agent_error",
                level="warning",
                title="Agent 角色配置错误",
                message="agent model is required",
                source="agents.upsert",
                details={"agent_id": "director", "reason": "missing_model"},
            ),
        ]
    )
    asyncio.run(
        service.record_log(
            category="feature_error",
            level="info",
            title="正常运行流水",
            message="this normal trace must not be collected",
            source="feature.normal",
        )
    )

    all_logs = api.get("/api/v1/admin/logs", headers=headers())
    model_logs = api.get("/api/v1/admin/logs?category=model_error", headers=headers())
    channel_logs = api.get("/api/v1/admin/logs?category=channel_error", headers=headers())

    assert all_logs.status_code == 200
    categories = {item["category"] for item in all_logs.json()}
    assert {
        "audit",
        "model_error",
        "mode_error",
        "feature_error",
        "agent_error",
        "channel_error",
    } <= categories
    assert model_logs.status_code == 200
    assert [item["category"] for item in model_logs.json()] == ["model_error"]
    assert channel_logs.status_code == 200
    assert channel_logs.json()
    assert all(item["level"] == "warning" for item in channel_logs.json())
    serialized = all_logs.text
    assert "this normal trace must not be collected" not in serialized
    for forbidden in ("api_key", "fingerprint", "hidden_reasoning", "chain_of_thought"):
        assert forbidden not in serialized.lower()


def test_skill_archive_upload_scans_real_zip_package() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-skill.zip"},
        content=skill_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["name"] == "safe_skill"
    assert body["status"] == "scanned"
    assert body["requested_permissions"] == ["tool:filesystem.read"]
    assert any("content sha256" in item for item in body["scan_diff"])
    assert skills.json()[0]["id"] == body["id"]


def test_skill_archive_upload_rejects_invalid_zip_without_saving_metadata() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "broken.zip"},
        content=b"not-a-zip",
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 422
    assert uploaded.json()["error"]["code"] == "invalid_skill_package"
    assert skills.json() == []


def test_memory_forget_removes_record() -> None:
    api = client()
    memory_id = api.get("/api/v1/admin/memory", headers=headers()).json()[0]["id"]

    forgotten = api.delete(f"/api/v1/admin/memory/{memory_id}", headers=headers())
    remaining = api.get("/api/v1/admin/memory", headers=headers())

    assert forgotten.status_code == 200
    assert forgotten.json() == {"status": "forgotten"}
    assert remaining.json() == []


def test_hermes_records_feedback_and_recommends_from_prior_lessons() -> None:
    api = client()

    feedback = api.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "success",
            "lesson": "Use group chat when debate review is required.",
            "tags": ["debate", "review"],
            "weight": 5,
        },
    )
    recommendation = api.post(
        "/api/v1/admin/hermes/recommend",
        headers=headers(),
        json={
            "task": "Run a debate review for this architecture.",
            "mode_candidates": ["dispatch", "group_chat"],
            "model_candidates": ["deepseek-chat", "gpt-4o"],
            "skill_candidates": ["architecture-review", "safe-shell"],
        },
    )
    insights = api.get("/api/v1/admin/hermes", headers=headers())

    assert feedback.status_code == 200
    assert recommendation.status_code == 200
    assert recommendation.json()["recommended_mode"] == "group_chat"
    assert recommendation.json()["recommended_model"] == "deepseek-chat"
    assert recommendation.json()["confidence"] > 0.45
    assert any("Hermes lesson" in reason for reason in recommendation.json()["reasons"])
    assert any(
        insight["lesson"] == "Use group chat when debate review is required."
        for insight in insights.json()
    )


def test_hermes_feedback_rejects_sensitive_content_without_echoing_it() -> None:
    response = client().post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "failure",
            "lesson": "Do not store api_key sk-secret-value in memory.",
            "tags": ["security"],
            "weight": 10,
        },
    )

    assert response.status_code == 422
    assert "sk-secret-value" not in response.text


@pytest.mark.asyncio
async def test_persistent_admin_models_write_to_published_config() -> None:
    configs = FakeConfigService()
    secrets = FakeSecretService()
    transport = FakeModelTransport()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="deepseek",
            api_base="https://api.deepseek.com/v1",
            upstream_model="deepseek-chat",
            logical_model="main",
            capabilities=["text", "tool_calling"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="deepseek-account",
            max_concurrency=4,
            target_utilization=0.8,
            reserved_capacity=0,
            rpm=60,
            tpm=100000,
            queue_timeout_seconds=60,
            fallback=None,
            weight=100,
        )
    )

    assert created.logical_model == "main"
    assert created.upstream_model == "deepseek-chat"
    assert configs.current is not None
    deployment, request, api_key = transport.calls[0]
    assert deployment.provider_model == "deepseek/deepseek-chat"
    assert deployment.request_model == "deepseek-chat"
    assert request.logical_model == "main"
    assert api_key == "sk-live"
    assert secrets.resolved == [
        (TENANT_ID, f"secret://{SECRET_ID}"),
    ]
    assert configs.current.document == {
        "models": {
            "main": {
                "deployments": [
                    {
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "api_base": "https://api.deepseek.com/v1",
                        "credential_ref": f"secret://{SECRET_ID}",
                        "quota_scope_id": "deepseek-account",
                        "max_concurrency": 4,
                        "target_utilization": 0.8,
                        "reserved_slots": 0,
                        "rpm": 60,
                        "tpm": 100000,
                        "capabilities": ["text", "tool_calling"],
                    }
                ]
            },
        },
        "agents": [],
    }


@pytest.mark.asyncio
async def test_persistent_admin_agents_write_to_published_config() -> None:
    configs = FakeConfigService()
    configs.current = ConfigRevision(
        id=uuid4(),
        tenant_id=TENANT_ID,
        version=1,
        status=ConfigStatus.PUBLISHED,
        document={
            "models": {
                "main": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "credential_ref": f"secret://{SECRET_ID}",
                            "quota_scope_id": "deepseek-account",
                        }
                    ]
                }
            },
            "agents": [],
        },
        created_by=ACTOR_ID,
        created_at=datetime.now(UTC),
    )
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    created = await service.upsert_agent(
        AgentResourceRequest(
            id="director",
            name="导演",
            enabled=True,
            role="短视频导演",
            prompt="负责拆解选题、镜头语言和成片节奏。",
            model="main",
            skills=["script_review"],
        )
    )
    listed = await service.list_agents()

    assert created.id == "director"
    assert created.role == "短视频导演"
    assert listed == (created,)
    assert configs.current is not None
    assert configs.current.version == 1
    assert configs.current.document["agents"] == [
        {
            "id": "director",
            "role": "短视频导演",
            "prompt": "负责拆解选题、镜头语言和成片节奏。",
            "model": "main",
            "skills": ["script_review"],
        }
    ]


@pytest.mark.asyncio
async def test_persistent_admin_model_is_not_published_when_availability_check_fails() -> None:
    configs = FakeConfigService()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=FakeModelTransport(RuntimeError("provider returned status=401")),
    )

    with pytest.raises(PublicAPIError) as error:
        await service.create_model(
            ModelDeploymentRequest(
                provider="deepseek",
                api_base="https://api.deepseek.com/v1",
                upstream_model="deepseek-chat",
                logical_model="main",
                capabilities=["text"],
                credential_ref=f"secret://{SECRET_ID}",
                quota_scope="deepseek-account",
                max_concurrency=4,
                target_utilization=0.8,
                reserved_capacity=0,
                rpm=60,
                tpm=100000,
                queue_timeout_seconds=60,
                fallback=None,
                weight=100,
            )
        )

    assert error.value.code == "model_unavailable"
    assert "status=401" in error.value.public_message
    assert error.value.details == {
        "stage": "model_availability_check",
        "provider": "deepseek",
        "api_base": "https://api.deepseek.com/v1",
        "logical_model": "main",
        "upstream_model": "deepseek-chat",
        "status_code": "401",
        "reason": "provider returned status=401",
        "hint": "检查 API Key 是否有效、API Base 是否可从服务器访问、模型名是否属于该服务商账号。",
    }
    assert "sk-live" not in error.value.public_message
    assert "credential_ref" not in error.value.details
    model_logs = await service.list_logs("model_error")
    assert len(model_logs) == 1
    assert model_logs[0].message == "provider returned status=401"
    assert model_logs[0].details["provider"] == "deepseek"
    assert model_logs[0].details["status_code"] == "401"
    assert configs.drafts == []
    assert configs.current is None


@pytest.mark.asyncio
async def test_persistent_admin_model_logs_preflight_availability_failures() -> None:
    configs = FakeConfigService()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=FakeModelTransport(),
    )

    with pytest.raises(PublicAPIError) as error:
        await service.create_model(
            ModelDeploymentRequest(
                provider="minimax",
                api_base="https://api.minimax.chat/v1",
                upstream_model="abab6.5s-chat",
                logical_model="vision_only",
                capabilities=["vision"],
                credential_ref=f"secret://{SECRET_ID}",
                quota_scope="minimax-account",
                max_concurrency=4,
                target_utilization=0.8,
                reserved_capacity=0,
                rpm=60,
                tpm=100000,
                queue_timeout_seconds=60,
                fallback=None,
                weight=100,
            )
        )

    assert error.value.code == "model_unavailable"
    model_logs = await service.list_logs("model_error")
    assert len(model_logs) == 1
    assert model_logs[0].message == "model availability check requires text capability"
    assert model_logs[0].details["provider"] == "minimax"
    assert model_logs[0].details["logical_model"] == "vision_only"
    serialized = model_logs[0].model_dump_json()
    assert "credential_ref" not in serialized
    assert "secret://" not in serialized
    assert configs.drafts == []
    assert configs.current is None


@pytest.mark.asyncio
async def test_persistent_admin_secret_uses_sealed_secret_service() -> None:
    secrets = FakeSecretService()
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    reference = await service.create_secret(
        SecretCreateRequest(label="deepseek", value=SecretStr("sk-live-1234"))
    )

    assert reference.ref == f"secret://{SECRET_ID}"
    assert reference.last_four == "1234"
    assert secrets.values == ["sk-live-1234"]
