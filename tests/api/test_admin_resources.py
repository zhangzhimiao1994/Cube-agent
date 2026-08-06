from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from agent_hub.api.routers.admin import InMemoryAdminResourceService
from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
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
