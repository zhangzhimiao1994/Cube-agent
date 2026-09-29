from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_hub.api.routers.previews import WebPreviewService
from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.previews import PreviewManager, PreviewNotFound


class StubAuthService:
    def __init__(self, principal: AuthenticatedPrincipal) -> None:
        self.principal = principal

    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid-token":
            raise InvalidCredentials("bad token")
        return self.principal


class StubConversationService:
    def __init__(self) -> None:
        self.project_id = "project-preview"
        self.workspace_path = "session-preview"
        self.archived_at: object | None = None
        self.run_statuses: tuple[str, ...] = ()
        self.activate_after_first_lookup = False
        self.lookup_count = 0

    def for_principal(self, _tenant_id: object, _user_id: object) -> StubConversationService:
        return self

    async def get_conversation(self, conversation_id: str) -> object:
        self.lookup_count += 1
        run_statuses = (
            ("running",)
            if self.activate_after_first_lookup and self.lookup_count > 1
            else self.run_statuses
        )
        return SimpleNamespace(
            conversation_id=conversation_id,
            project_id=self.project_id,
            workspace_path=self.workspace_path,
            archived_at=self.archived_at,
            runs=[SimpleNamespace(status=value) for value in run_statuses],
        )


def _bearer() -> dict[str, str]:
    return {"Authorization": "Bearer valid-token"}


def _workspace(root: Path, principal: AuthenticatedPrincipal) -> Path:
    session = (
        root
        / str(principal.tenant_id)
        / "projects"
        / "project-preview"
        / "sessions"
        / "session-preview"
        / "dist"
    )
    session.mkdir(parents=True)
    (session / "index.html").write_text(
        (
            '<!doctype html><link rel="stylesheet" href="/style.css">'
            '<script src="/assets/app.js"></script><main>preview</main>'
        ),
        encoding="utf-8",
    )
    (session / "style.css").write_text("main { color: teal; }", encoding="utf-8")
    (session / "assets").mkdir()
    (session / "assets" / "app.js").write_text(
        "window.previewLoaded = true;",
        encoding="utf-8",
    )
    return session


def _client(
    tmp_path: Path,
) -> tuple[TestClient, AuthenticatedPrincipal, StubConversationService]:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    conversations = StubConversationService()
    _workspace(tmp_path, principal)
    app = create_app(
        auth_service=StubAuthService(principal),
        rate_limiter=object(),
        config_service=object(),
        admin_resource_service=conversations,
        run_service=object(),
        preview_manager=PreviewManager(tmp_path),
    )
    return TestClient(app), principal, conversations


@pytest.fixture
def preview_client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, AuthenticatedPrincipal, StubConversationService]]:
    client, principal, conversations = _client(tmp_path)
    try:
        yield client, principal, conversations
    finally:
        cast(FastAPI, client.app).state.preview_manager.close()
        client.close()


def test_preview_uses_http_only_cookie_and_rewrites_root_relative_assets(
    preview_client: tuple[TestClient, AuthenticatedPrincipal, StubConversationService],
) -> None:
    client, _, _ = preview_client

    started = client.post(
        "/api/v1/web-previews/start",
        headers=_bearer(),
        json={
            "conversation_id": "conv-preview",
            "project_id": "project-preview",
            "workspace_session_id": "session-preview",
            "root": "dist",
        },
    )

    assert started.status_code == 201
    payload = started.json()
    assert payload == {
        "id": payload["id"],
        "status": "ready",
        "preview_url": payload["preview_url"],
        "lease_expires_at": payload["lease_expires_at"],
    }
    assert payload["preview_url"].endswith("/content/")
    assert "token" not in payload["preview_url"]
    assert "httponly" in started.headers["set-cookie"].casefold()
    root = client.get(payload["preview_url"])
    asset = client.get(f'{payload["preview_url"]}style.css')
    script = client.get(f'{payload["preview_url"]}assets/app.js')

    assert root.status_code == 200
    assert "preview" in root.text
    assert f'{payload["preview_url"]}style.css' in root.text
    assert f'{payload["preview_url"]}assets/app.js' in root.text
    assert f'<base href="{payload["preview_url"]}">' in root.text
    storage_shim = root.text.index("data-agent-preview-storage-shim")
    application_script = root.text.index(f'{payload["preview_url"]}assets/app.js')
    assert storage_shim < application_script
    assert "Object.defineProperty(window, name" in root.text
    assert 'installStorage("localStorage")' in root.text
    assert 'installStorage("sessionStorage")' in root.text
    assert "const maxEntries = 1024" in root.text
    assert "const maxCharacters = 5 * 1024 * 1024" in root.text
    assert 'new DOMException("Storage quota exceeded", "QuotaExceededError")' in root.text
    assert asset.status_code == 200
    assert asset.text == "main { color: teal; }"
    assert script.status_code == 200
    assert script.text == "window.previewLoaded = true;"
    assert "sandbox" in root.headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline'" in root.headers["content-security-policy"]
    assert "style-src 'self' 'unsafe-inline'" in root.headers["content-security-policy"]
    assert root.headers["referrer-policy"] == "no-referrer"
    assert root.headers["cache-control"] == "no-store"

    anonymous = TestClient(cast(FastAPI, client.app))
    try:
        assert anonymous.get(payload["preview_url"]).status_code == 404
    finally:
        anonymous.close()


def test_preview_management_requires_login_and_is_tenant_scoped(
    preview_client: tuple[TestClient, AuthenticatedPrincipal, StubConversationService],
) -> None:
    client, principal, _ = preview_client
    started = client.post(
        "/api/v1/web-previews/start",
        headers=_bearer(),
        json={
            "conversation_id": "conv-preview",
            "project_id": "project-preview",
            "workspace_session_id": "session-preview",
            "root": "dist",
        },
    ).json()

    assert client.get("/api/v1/web-previews/conversations/conv-preview").status_code == 401

    other = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    cast(FastAPI, client.app).state.auth_service = StubAuthService(other)
    assert (
        client.get(
            "/api/v1/web-previews/conversations/conv-preview",
            headers=_bearer(),
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f'/api/v1/web-previews/{started["id"]}',
            headers=_bearer(),
        ).status_code
        == 404
    )
    cast(FastAPI, client.app).state.auth_service = StubAuthService(principal)


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("project_id", "other-project", "preview_scope_mismatch"),
        ("workspace_path", "other-session", "preview_scope_mismatch"),
        ("archived_at", "archived", "conversation_archived"),
        ("run_statuses", ("running",), "conversation_active"),
    ],
)
def test_preview_start_requires_matching_idle_unarchived_conversation(
    preview_client: tuple[TestClient, AuthenticatedPrincipal, StubConversationService],
    field: str,
    value: object,
    expected_code: str,
) -> None:
    client, _, conversations = preview_client
    setattr(conversations, field, value)

    response = client.post(
        "/api/v1/web-previews/start",
        headers=_bearer(),
        json={
            "conversation_id": "conv-preview",
            "project_id": "project-preview",
            "workspace_session_id": "session-preview",
            "root": "dist",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == expected_code


def test_preview_start_rechecks_idle_state_after_registering_preview(
    preview_client: tuple[TestClient, AuthenticatedPrincipal, StubConversationService],
) -> None:
    client, _, conversations = preview_client
    conversations.activate_after_first_lookup = True

    response = client.post(
        "/api/v1/web-previews/start",
        headers=_bearer(),
        json={
            "conversation_id": "conv-preview",
            "project_id": "project-preview",
            "workspace_session_id": "session-preview",
            "root": "dist",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conversation_active"
    current = client.get(
        "/api/v1/web-previews/conversations/conv-preview",
        headers=_bearer(),
    )
    assert current.status_code == 404


def test_stop_revokes_content_token_and_current_preview(
    preview_client: tuple[TestClient, AuthenticatedPrincipal, StubConversationService],
) -> None:
    client, _, _ = preview_client
    started = client.post(
        "/api/v1/web-previews/start",
        headers=_bearer(),
        json={
            "conversation_id": "conv-preview",
            "project_id": "project-preview",
            "workspace_session_id": "session-preview",
            "root": "dist",
        },
    ).json()

    renewed = client.post(
        f'/api/v1/web-previews/{started["id"]}/renew',
        headers=_bearer(),
    )
    stopped = client.delete(
        f'/api/v1/web-previews/{started["id"]}',
        headers=_bearer(),
    )

    assert renewed.status_code == 200
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"
    assert stopped.json()["preview_url"] is None
    assert client.get(started["preview_url"]).status_code == 404
    assert (
        client.get(
            "/api/v1/web-previews/conversations/conv-preview",
            headers=_bearer(),
        ).status_code
        == 404
    )


def test_preview_response_does_not_expose_internal_runtime_details(
    tmp_path: Path,
    preview_client: tuple[TestClient, AuthenticatedPrincipal, StubConversationService],
) -> None:
    client, _, _ = preview_client

    response = client.post(
        "/api/v1/web-previews/start",
        headers=_bearer(),
        json={
            "conversation_id": "conv-preview",
            "project_id": "project-preview",
            "workspace_session_id": "session-preview",
            "root": "dist",
        },
    )

    assert response.status_code == 201
    serialized = response.text.casefold()
    assert "internal_port" not in serialized
    assert "preview_root" not in serialized
    assert "token_sha256" not in serialized
    assert str(tmp_path).casefold() not in serialized


def test_service_reaper_forgets_expired_raw_preview_token(tmp_path: Path) -> None:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    _workspace(tmp_path, principal)
    now = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)
    clock_value = [now]
    service = WebPreviewService(
        PreviewManager(
            tmp_path,
            default_lease=timedelta(minutes=1),
            reaper_interval=timedelta(hours=1),
            clock=lambda: clock_value[0],
        )
    )
    try:
        started = service.start(
            tenant_id=principal.tenant_id,
            conversation_id="conv-preview",
            project_id="project-preview",
            session_id="session-preview",
            root="dist",
        )
        assert service._owned_access(principal.tenant_id, started.id).token

        clock_value[0] = now + timedelta(minutes=1)
        assert service.reap_expired_access() == (started.id,)

        with pytest.raises(PreviewNotFound):
            service._owned_access(principal.tenant_id, started.id)
    finally:
        service.close()
