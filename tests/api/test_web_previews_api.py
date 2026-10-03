from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_hub.api.routers.previews import WebPreviewResponse, WebPreviewService
from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.previews import PreviewLaunch, PreviewManager, PreviewNotFound
from agent_hub.previews import bridge as preview_bridge
from agent_hub.previews.dynamic_runtime import DynamicPreviewResponse


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
        "application_transport": False,
    }
    assert payload["preview_url"].endswith("/content/")
    assert "token" not in payload["preview_url"]
    assert "httponly" in started.headers["set-cookie"].casefold()
    root = client.get(payload["preview_url"])
    asset = client.get(f"{payload['preview_url']}style.css")
    script = client.get(f"{payload['preview_url']}assets/app.js")

    assert root.status_code == 200
    assert "preview" in root.text
    assert f"{payload['preview_url']}style.css" in root.text
    assert f"{payload['preview_url']}assets/app.js" in root.text
    assert f'<base href="{payload["preview_url"]}">' in root.text
    storage_shim = root.text.index("data-agent-preview-storage-shim")
    application_script = root.text.index(f"{payload['preview_url']}assets/app.js")
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
            f"/api/v1/web-previews/{started['id']}",
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
        f"/api/v1/web-previews/{started['id']}/renew",
        headers=_bearer(),
    )
    stopped = client.delete(
        f"/api/v1/web-previews/{started['id']}",
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


class AppRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[tuple[str, str], ...], bytes]] = []
        self.closed = False
        self.fail_close = False
        from agent_hub.previews.dynamic_runtime import DynamicPreviewResponse

        self.response = DynamicPreviewResponse(
            201, (("content-type", "application/json"),), b'{"id":1}'
        )

    def request(
        self, method: str, target: str, headers: tuple[tuple[str, str], ...], body: bytes
    ) -> DynamicPreviewResponse:
        self.calls.append((method, target, headers, body))
        return self.response

    def close(self) -> None:
        from agent_hub.previews.dynamic_runtime import DynamicPreviewCleanupError

        if self.fail_close:
            raise DynamicPreviewCleanupError("cleanup not confirmed")
        self.closed = True


class AppBackend:
    def __init__(self) -> None:
        self.runtime = AppRuntime()

    def start(self, source_root: Path, preview_id: str, lifetime_seconds: int) -> AppRuntime:
        assert (source_root / "server.js").is_file()
        return self.runtime


class StartedPreview(TypedDict):
    id: str
    preview_url: str
    application_transport: bool


DynamicClient = tuple[
    TestClient, AuthenticatedPrincipal, StubConversationService, AppBackend, StartedPreview
]


@pytest.fixture
def dynamic_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[DynamicClient]:

    # Contract fixture only; the actual trusted bridge belongs to the parent.
    def preview_fetch_shim(preview_id: str) -> str:
        return f'<script data-test-fetch-shim="{preview_id}"></script>'

    monkeypatch.setattr(preview_bridge, "preview_fetch_shim", preview_fetch_shim)
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    display = _workspace(tmp_path, principal)
    (display / "index.html").write_text(
        '<!doctype html><script src="/assets/app.js"></script><head></head>'
        '<script>fetch("/tasks")</script><main>actual generated UI</main>'
    )
    no_html = getattr(request, "param", None) == "no_html"
    if no_html:
        (display / "index.html").unlink()
    (display.parent / "package.json").write_text(
        json.dumps({"scripts": {"start": "node server.js"}})
    )
    (display.parent / "server.js").write_text("throw new Error('host execution forbidden')")
    backend = AppBackend()
    conversations = StubConversationService()
    app = create_app(
        auth_service=StubAuthService(principal),
        rate_limiter=object(),
        config_service=object(),
        admin_resource_service=conversations,
        run_service=object(),
        preview_manager=PreviewManager(tmp_path, dynamic_backend=backend),
    )
    client = TestClient(app)
    try:
        response = client.post(
            "/api/v1/web-previews/start",
            headers=_bearer(),
            json={
                "conversation_id": "conv-preview",
                "project_id": "project-preview",
                "workspace_session_id": "session-preview",
                "root": None if no_html else "dist",
            },
        )
        assert response.status_code == 201
        started = WebPreviewResponse.model_validate(response.json())
        assert started.preview_url is not None
        payload: StartedPreview = {
            "id": started.id,
            "preview_url": started.preview_url,
            "application_transport": started.application_transport,
        }
        yield client, principal, conversations, backend, payload
    finally:
        backend.runtime.fail_close = False
        app.state.preview_manager.close()
        client.close()


def envelope(method: str = "POST", target: str = "/tasks?label=a%2Fb") -> dict[str, object]:
    return {
        "method": method,
        "target": target,
        "headers": [["Content-Type", "application/json"]],
        "body_base64": base64.b64encode(b'{"title":"new"}').decode(),
    }


def test_app_request_wire_and_dynamic_shim(dynamic_client: DynamicClient) -> None:
    client, _, _, backend, started = dynamic_client
    assert started["application_transport"] is True
    endpoint = f"/api/v1/web-previews/{started['id']}/app-request"
    response = client.post(endpoint, headers=_bearer(), json=envelope())
    assert response.status_code == 200
    assert response.json() == {
        "status_code": 201,
        "headers": [["content-type", "application/json"]],
        "body_base64": base64.b64encode(b'{"id":1}').decode(),
    }
    assert backend.runtime.calls[0] == (
        "POST",
        "/tasks?label=a%2Fb",
        (("content-type", "application/json"),),
        b'{"title":"new"}',
    )
    from agent_hub.previews.dynamic_runtime import DynamicPreviewResponse

    backend.runtime.response = DynamicPreviewResponse(
        200, (("content-type", "text/html"),), b'<head><script>fetch("/tasks")</script></head>'
    )
    content = client.get(started["preview_url"])
    assert content.status_code == 200
    assert content.text.index("data-test-fetch-shim") < content.text.index('fetch("/tasks")')
    assert "allow-same-origin" not in content.headers["content-security-policy"]


def test_app_request_requires_creator_and_live_scope(dynamic_client: DynamicClient) -> None:
    client, principal, conversations, backend, started = dynamic_client
    endpoint = f"/api/v1/web-previews/{started['id']}/app-request"
    assert client.post(endpoint, json=envelope()).status_code == 401
    cast(FastAPI, client.app).state.auth_service.principal = AuthenticatedPrincipal(
        uuid4(), principal.tenant_id, Role.OPERATOR
    )
    assert client.post(endpoint, headers=_bearer(), json=envelope()).status_code == 404
    cast(FastAPI, client.app).state.auth_service.principal = principal
    conversations.run_statuses = ("running",)
    assert client.post(endpoint, headers=_bearer(), json=envelope()).status_code == 409
    conversations.run_statuses = ()
    conversations.workspace_path = "another-session"
    assert client.post(endpoint, headers=_bearer(), json=envelope()).status_code == 409
    assert not backend.runtime.calls


@pytest.mark.parametrize(
    "patch",
    [
        {"body_base64": "%%%"},
        {"body": ""},
        {"method": "CONNECT"},
        {"target": "//example.com/"},
        {"target": "/%2fsecret"},
        {"headers": [["Content-Type", "x\r\nHost: evil"]]},
        {"headers": [["Authorization", "x", "extra"]]},
    ],
)
def test_app_request_rejects_invalid_envelopes(
    dynamic_client: DynamicClient, patch: dict[str, object]
) -> None:
    client, _, _, backend, started = dynamic_client
    payload = envelope()
    payload.update(patch)
    assert (
        client.post(
            f"/api/v1/web-previews/{started['id']}/app-request", headers=_bearer(), json=payload
        ).status_code
        == 422
    )
    assert not backend.runtime.calls


def test_app_request_preserves_empty_and_error_responses_without_replay(
    dynamic_client: DynamicClient,
) -> None:
    client, _, _, backend, started = dynamic_client
    from agent_hub.previews.dynamic_runtime import DynamicPreviewResponse

    endpoint = f"/api/v1/web-previews/{started['id']}/app-request"
    for method, status, body in [("DELETE", 204, b""), ("PATCH", 422, b"real application error")]:
        backend.runtime.response = DynamicPreviewResponse(status, (), body)
        response = client.post(endpoint, headers=_bearer(), json=envelope(method))
        assert response.status_code == 200
        assert response.json()["status_code"] == status
        assert base64.b64decode(response.json()["body_base64"]) == body
    assert len(backend.runtime.calls) == 2
    assert (
        client.delete(f"/api/v1/web-previews/{started['id']}", headers=_bearer()).status_code == 200
    )
    assert client.post(endpoint, headers=_bearer(), json=envelope()).status_code == 404


def test_app_request_request_limit_and_duplicate_json_fields(dynamic_client: DynamicClient) -> None:
    client, _, _, backend, started = dynamic_client
    endpoint = f"/api/v1/web-previews/{started['id']}/app-request"
    payload = envelope()
    payload["body_base64"] = base64.b64encode(b"x" * (1024 * 1024 + 1)).decode()
    assert client.post(endpoint, headers=_bearer(), json=payload).status_code == 413
    assert (
        client.post(
            endpoint,
            headers={**_bearer(), "Content-Type": "application/json"},
            content='{"method":"POST","method":"GET","target":"/","headers":[],"body_base64":""}',
        ).status_code
        == 422
    )
    assert not backend.runtime.calls


def test_current_requires_creator_and_reauthorizes_conversation(dynamic_client: DynamicClient) -> None:
    client, principal, conversations, _, _ = dynamic_client
    path = "/api/v1/web-previews/conversations/conv-preview"
    cast(FastAPI, client.app).state.auth_service.principal = AuthenticatedPrincipal(
        uuid4(), principal.tenant_id, Role.OPERATOR
    )
    assert client.get(path, headers=_bearer()).status_code == 404
    cast(FastAPI, client.app).state.auth_service.principal = principal
    conversations.archived_at = datetime.now(UTC)
    assert client.get(path, headers=_bearer()).status_code == 409


@pytest.mark.parametrize("https", [False, True])
def test_current_recovers_lost_cookie_without_renewing_lease(
    dynamic_client: DynamicClient, https: bool,
) -> None:
    client, principal, _, _, started = dynamic_client
    if https:
        client.base_url = "https://testserver"
    service = cast(FastAPI, client.app).state.preview_manager
    before = service.current(principal.tenant_id, "conv-preview", principal.user_id)
    cookie_name = f"agent_preview_{started['id'].replace('-', '_')}"
    original_token = client.cookies.get(cookie_name)
    client.cookies.clear()
    assert client.get(started["preview_url"]).status_code == 404

    response = client.get("/api/v1/web-previews/conversations/conv-preview", headers=_bearer())

    assert response.status_code == 200
    cookie = response.headers.get("set-cookie", "")
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()
    assert ("; secure" in cookie.lower()) is https
    assert f"Path=/api/v1/web-previews/{started['id']}/content" in cookie
    assert client.cookies.get(cookie_name) == original_token
    assert original_token and original_token not in response.text
    assert "token" not in response.text
    assert response.json()["lease_expires_at"] == before.lease_expires_at.isoformat().replace("+00:00", "Z")
    assert service.current(principal.tenant_id, "conv-preview", principal.user_id) == before
    content = client.get(started["preview_url"])
    assert content.status_code == 200
    assert original_token not in content.text


@pytest.mark.parametrize("different_tenant", [False, True])
def test_current_never_issues_cookie_to_foreign_user(
    dynamic_client: DynamicClient, different_tenant: bool,
) -> None:
    client, principal, _, _, started = dynamic_client
    client.cookies.clear()
    cast(FastAPI, client.app).state.auth_service.principal = AuthenticatedPrincipal(
        uuid4(), uuid4() if different_tenant else principal.tenant_id, Role.OPERATOR,
    )
    for _ in range(2):
        response = client.get("/api/v1/web-previews/conversations/conv-preview", headers=_bearer())
        assert response.status_code == 404
        assert "set-cookie" not in response.headers
        assert client.get(started["preview_url"]).status_code == 404


@pytest.mark.parametrize("state", ["stopped", "expired", "archived", "active"])
def test_current_denied_scope_or_finished_preview_never_issues_cookie(
    dynamic_client: DynamicClient, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    client, principal, conversations, _, started = dynamic_client
    service = cast(FastAPI, client.app).state.preview_manager
    if state == "stopped":
        service.stop(principal.tenant_id, started["id"], principal.user_id)
    elif state == "expired":
        expiry = service.current(principal.tenant_id, "conv-preview", principal.user_id).lease_expires_at
        monkeypatch.setattr(service._manager, "_clock", lambda: expiry)
    elif state == "archived":
        conversations.archived_at = datetime.now(UTC)
    else:
        conversations.run_statuses = ("running",)
    client.cookies.clear()
    response = client.get("/api/v1/web-previews/conversations/conv-preview", headers=_bearer())
    assert response.status_code == (404 if state in {"stopped", "expired"} else 409)
    assert "set-cookie" not in response.headers
    assert client.get(started["preview_url"]).status_code == 404


def test_current_handles_revocation_before_access_lookup(
    dynamic_client: DynamicClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, principal, _, _, started = dynamic_client
    service = cast(FastAPI, client.app).state.preview_manager
    original_current = service.current

    def revoke_after_read(*args: object) -> object:
        current = original_current(*args)
        service.stop(principal.tenant_id, started["id"], principal.user_id)
        return current

    monkeypatch.setattr(service, "current", revoke_after_read)
    client.cookies.clear()
    response = client.get("/api/v1/web-previews/conversations/conv-preview", headers=_bearer())
    assert response.status_code == 404
    assert "set-cookie" not in response.headers
    assert client.get(started["preview_url"]).status_code == 404


@pytest.mark.parametrize("state", ["stopped", "expired"])
def test_current_rechecks_liveness_after_conversation_authorization(
    dynamic_client: DynamicClient, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    client, principal, conversations, _, started = dynamic_client
    service = cast(FastAPI, client.app).state.preview_manager
    expiry = service.current(principal.tenant_id, "conv-preview", principal.user_id).lease_expires_at
    original_lookup = conversations.get_conversation

    async def lookup(conversation_id: str) -> object:
        if state == "stopped":
            service.stop(principal.tenant_id, started["id"], principal.user_id)
        else:
            monkeypatch.setattr(service._manager, "_clock", lambda: expiry)
        return await original_lookup(conversation_id)

    monkeypatch.setattr(conversations, "get_conversation", lookup)
    client.cookies.clear()
    response = client.get("/api/v1/web-previews/conversations/conv-preview", headers=_bearer())
    assert response.status_code == 404
    assert "set-cookie" not in response.headers


def test_dynamic_shim_is_before_even_scripts_outside_head(dynamic_client: DynamicClient) -> None:
    client, _, _, backend, started = dynamic_client
    from agent_hub.previews.dynamic_runtime import DynamicPreviewResponse

    backend.runtime.response = DynamicPreviewResponse(
        200,
        (("content-type", "text/html"),),
        b'<!doctype html><script>fetch("/tasks")</script><head></head>',
    )
    response = client.get(started["preview_url"])
    assert response.text.index("data-test-fetch-shim") < response.text.index('fetch("/tasks")')
    assert "connect-src 'none'" in response.headers["content-security-policy"]


def test_header_allowlists_and_external_redirect_rejection(dynamic_client: DynamicClient) -> None:
    client, _, _, backend, started = dynamic_client
    from agent_hub.previews.dynamic_runtime import DynamicPreviewResponse

    endpoint = f"/api/v1/web-previews/{started['id']}/app-request"
    payload = envelope()
    payload["headers"] = [
        ["Accept-Language", "zh-CN"],
        ["Cookie", "secret=value"],
        ["Authorization", "Bearer secret"],
        ["Host", "console"],
        ["Origin", "console"],
    ]
    backend.runtime.response = DynamicPreviewResponse(204, (("set-cookie", "secret=value"),), b"")
    response = client.post(endpoint, headers=_bearer(), json=payload)
    assert response.status_code == 200
    assert response.json()["headers"] == []
    assert backend.runtime.calls[0][2] == (("accept-language", "zh-CN"),)
    backend.runtime.response = DynamicPreviewResponse(
        302, (("location", "https://evil.example"),), b""
    )
    assert client.post(endpoint, headers=_bearer(), json=envelope()).status_code == 422


def test_stop_cleanup_failure_remains_revoked_and_retryable(dynamic_client: DynamicClient) -> None:
    client, _, _, backend, started = dynamic_client
    endpoint = f"/api/v1/web-previews/{started['id']}"
    backend.runtime.fail_close = True
    response = client.delete(endpoint, headers=_bearer())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "preview_cleanup_pending"
    assert (
        client.post(endpoint + "/app-request", headers=_bearer(), json=envelope()).status_code
        == 404
    )
    backend.runtime.fail_close = False
    assert client.delete(endpoint, headers=_bearer()).status_code == 200


@pytest.mark.parametrize("phase,reason", [
    ("install", "nonzero_exit"), ("install_validate", "unsafe_tree"),
    ("install_handoff", "permission_denied"), ("build", "timeout"),
])
def test_start_failure_returns_safe_stage_not_missing_broker(
    dynamic_client: DynamicClient, monkeypatch: pytest.MonkeyPatch, phase: str, reason: str,
) -> None:
    from agent_hub.previews import dynamic_runtime

    client, _, _, backend, started = dynamic_client
    assert client.delete(f"/api/v1/web-previews/{started['id']}", headers=_bearer()).status_code == 200

    def fail_start(source_root: Path, preview_id: str, lifetime_seconds: int) -> AppRuntime:
        del source_root, preview_id, lifetime_seconds
        dynamic_runtime._check_result({"ok": False, "error": "preview startup failed",
                                       "phase": phase, "reason": reason})
        raise AssertionError("a failed broker response must not start a preview")

    monkeypatch.setattr(backend, "start", fail_start)
    response = client.post("/api/v1/web-previews/start", headers=_bearer(), json={
        "conversation_id": "conv-preview", "project_id": "project-preview",
        "workspace_session_id": "session-preview", "root": "dist",
    })
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "dynamic_preview_start_failed",
        "message": "generated website preview preparation failed",
        "details": {"phase": phase, "reason": reason},
    }
    assert "set-cookie" not in response.headers
    assert client.get("/api/v1/web-previews/conversations/conv-preview", headers=_bearer()).status_code == 404


def test_unclassified_preview_failure_does_not_invent_configuration_cause() -> None:
    from agent_hub.api.routers.previews import _preview_error
    from agent_hub.previews.dynamic_runtime import DynamicPreviewUnavailable

    error = _preview_error(DynamicPreviewUnavailable("private-path Bearer secret"))
    assert error.code == "dynamic_preview_unavailable"
    assert error.public_message == "isolated website preview is currently unavailable"
    assert error.details is None


def test_slow_dynamic_start_does_not_block_other_owned_preview(tmp_path: Path) -> None:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    display = _workspace(tmp_path, principal)
    entered = threading.Event()
    release = threading.Event()

    class BlockingBackend(AppBackend):
        def start(self, source_root: Path, preview_id: str, lifetime_seconds: int) -> AppRuntime:
            entered.set()
            assert release.wait(5)
            return self.runtime

    backend = BlockingBackend()
    service = WebPreviewService(PreviewManager(tmp_path, dynamic_backend=backend))
    def launch_preview(conversation_id: str) -> WebPreviewResponse:
        return service.start(
            tenant_id=principal.tenant_id,
            conversation_id=conversation_id,
            project_id="project-preview",
            session_id="session-preview",
            root="dist",
            user_id=principal.user_id,
        )

    first = launch_preview("first")
    (display.parent / "package.json").write_text('{"scripts":{"start":"node server.js"}}')
    start_errors: list[BaseException] = []
    completed = threading.Event()

    def start_second() -> None:
        try:
            launch_preview("second")
        except (PreviewNotFound, AssertionError, RuntimeError) as error:
            start_errors.append(error)

    def use_first() -> None:
        assert service.current(principal.tenant_id, "first", principal.user_id) is not None
        access = service._owned_access(principal.tenant_id, first.id, principal.user_id)
        assert service.read(first.id, access.token, "").status_code == 200
        assert service.stop(principal.tenant_id, first.id, principal.user_id).status == "stopped"
        completed.set()

    starter = threading.Thread(target=start_second)
    reader = threading.Thread(target=use_first)
    try:
        starter.start()
        assert entered.wait(2)
        reader.start()
        assert completed.wait(1), "another preview was blocked by application preparation"
    finally:
        release.set()
        starter.join(5)
        reader.join(5)
        service.close()
    assert not start_errors


def test_service_does_not_register_launch_revoked_before_registration(tmp_path: Path) -> None:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    _workspace(tmp_path, principal)

    class RevokingManager(PreviewManager):
        def start(
            self,
            *,
            tenant_id: UUID,
            conversation_id: str,
            project_id: str,
            session_id: str,
            root: str | None = None,
            lease: timedelta | None = None,
        ) -> PreviewLaunch:
            launch = super().start(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                project_id=project_id,
                session_id=session_id,
                root=root,
                lease=lease,
            )
            self.stop(launch.state.preview_id)
            return launch

    service = WebPreviewService(RevokingManager(tmp_path))
    try:
        with pytest.raises(PreviewNotFound):
            service.start(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                conversation_id="conv-preview",
                project_id="project-preview",
                session_id="session-preview",
                root="dist",
            )
        assert not service._access_by_id
    finally:
        service.close()


@pytest.mark.parametrize("dynamic_client", ["no_html"], indirect=True)
def test_cookie_content_cannot_proxy_backend_without_immutable_html(
    dynamic_client: DynamicClient,
) -> None:
    client, principal, _, backend, started = dynamic_client
    cast(FastAPI, client.app).state.auth_service.principal = AuthenticatedPrincipal(
        uuid4(), principal.tenant_id, Role.OPERATOR
    )
    assert client.get(started["preview_url"] + "api/private?mode=read").status_code == 404
    assert client.get(started["preview_url"]).status_code == 404
    assert client.post(f'/api/v1/web-previews/{started["id"]}/app-request',
                       headers=_bearer(), json=envelope()).status_code == 404
    assert not backend.runtime.calls


def test_application_wire_and_response_limits(dynamic_client: DynamicClient) -> None:
    client, _, _, backend, started = dynamic_client
    from agent_hub.previews.dynamic_runtime import DynamicPreviewResponse
    endpoint = f'/api/v1/web-previews/{started["id"]}/app-request'
    oversized_wire = " " * (1536 * 1024 + 1)
    assert client.post(endpoint, headers={**_bearer(), "Content-Type": "application/json"},
                       content=oversized_wire).status_code == 413
    assert not backend.runtime.calls
    backend.runtime.response = DynamicPreviewResponse(200, (), b"x" * (8 * 1024 * 1024 + 1))
    assert client.post(endpoint, headers=_bearer(), json=envelope()).status_code == 413
    assert len(backend.runtime.calls) == 1
