from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers.admin import (
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
    mcp = api.get("/api/v1/admin/mcp", headers=headers())
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
    assert mcp.json()[0]["health"] == "healthy"
    assert updated_memory.json()["value"] == "Updated non-dangerous operation policy."
    assert audit.json()[0]["action"] == "config.publish"
    serialized = uploaded.text + approved.text + skills.text + mcp.text + memory.text + audit.text
    for forbidden in ("api_key", "fingerprint", "hidden_reasoning", "chain_of_thought"):
        assert forbidden not in serialized.lower()


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
    assert "sk-live" not in error.value.public_message
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
