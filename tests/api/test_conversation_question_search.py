from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from agent_hub.api.routers import admin as admin_router
from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.settings import Settings

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
USER_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")


class StubAuthService:
    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid-token":
            raise InvalidCredentials("bad token")
        return AuthenticatedPrincipal(USER_ID, TENANT_ID, Role.SUPER_ADMIN)


class SearchService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def search_conversation_questions(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        return {
            "items": [
                {
                    "run_id": RUN_ID,
                    "conversation_id": "conv-search",
                    "project_id": "project-search",
                    "question": "需要展示的问题",
                    "created_at": datetime(2026, 9, 27, 8, 0, tzinfo=UTC),
                    "conversation_title": "搜索会话",
                }
            ],
            "next_cursor": "opaque-cursor",
        }


def _client(service: object, *, auth_service: object | None = None) -> TestClient:
    app = create_app(
        settings=Settings.model_construct(),
        auth_service=auth_service or StubAuthService(),
        rate_limiter=object(),
        admin_resource_service=service,
    )
    return TestClient(app)


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer valid-token"}


def test_search_endpoint_passes_filters_and_returns_only_lightweight_fields() -> None:
    service = SearchService()
    response = _client(service).get(
        "/api/v1/admin/conversation-questions/search",
        headers=_headers(),
        params={
            "q": "问题",
            "project_id": "project-search",
            "archived": "true",
            "limit": 7,
            "cursor": "incoming-cursor",
        },
    )

    assert response.status_code == 200
    assert service.calls == [
        {
            "q": "问题",
            "project_id": "project-search",
            "archived": True,
            "limit": 7,
            "cursor": "incoming-cursor",
        }
    ]
    body = response.json()
    assert body["next_cursor"] == "opaque-cursor"
    assert set(body["items"][0]) == {
        "run_id",
        "conversation_id",
        "project_id",
        "question",
        "created_at",
        "conversation_title",
    }
    assert "routing_decision" not in response.text
    assert "events" not in response.text
    assert "artifacts" not in response.text
    assert "payload" not in response.text


def test_search_endpoint_defaults_to_active_conversations() -> None:
    service = SearchService()
    response = _client(service).get(
        "/api/v1/admin/conversation-questions/search",
        headers=_headers(),
        params={"q": "问题"},
    )

    assert response.status_code == 200
    assert service.calls[0]["archived"] is False
    assert service.calls[0]["limit"] == 20


def test_search_endpoint_requires_run_read_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    required_permissions: list[str] = []

    class RecordingAuthorizer:
        def require(
            self,
            principal: AuthenticatedPrincipal,
            permission: str,
        ) -> AuthenticatedPrincipal:
            required_permissions.append(permission)
            return principal

    monkeypatch.setattr(admin_router, "Authorizer", RecordingAuthorizer)
    response = _client(SearchService()).get(
        "/api/v1/admin/conversation-questions/search",
        headers=_headers(),
        params={"q": "问题"},
    )

    assert response.status_code == 200
    assert required_permissions == ["run:read"]


@pytest.mark.parametrize("limit", [0, 51])
def test_search_endpoint_rejects_limit_outside_1_to_50(limit: int) -> None:
    response = _client(SearchService()).get(
        "/api/v1/admin/conversation-questions/search",
        headers=_headers(),
        params={"q": "问题", "limit": limit},
    )

    assert response.status_code == 422


def test_search_endpoint_rejects_oversized_queries() -> None:
    response = _client(SearchService()).get(
        "/api/v1/admin/conversation-questions/search",
        headers=_headers(),
        params={"q": "问" * 501},
    )

    assert response.status_code == 422


def test_search_endpoint_requires_authentication() -> None:
    response = _client(SearchService()).get(
        "/api/v1/admin/conversation-questions/search",
        params={"q": "问题"},
    )

    assert response.status_code == 401
