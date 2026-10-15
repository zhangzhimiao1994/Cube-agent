"""Startup and ingress regressions; doubles are not authenticated delivery proof."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Message, Receive, Scope, Send

from agent_hub.api.middleware import RequestBodyLimitMiddleware, _body_limit
from agent_hub.api.routers.admin import (
    InMemoryAdminResourceService,
    PersistentAdminResourceService,
)
from agent_hub.app import create_app
from agent_hub.plugins.handover_snapshots import InvalidSnapshot, SnapshotUnavailable
from agent_hub.settings import Settings
from tests.api.test_foundation_api import FakeDatabase, FakeRedis, Probe, StubAuthService


def test_injected_run_service_still_gets_real_handover_dependencies(tmp_path: Path) -> None:
    database, redis = FakeDatabase(), FakeRedis()
    configured = Settings.model_validate(
        {
            "project_workspace_dir": tmp_path / "workspaces",
            "generated_artifact_dir": tmp_path / "generated",
            "plugin_package_store_dir": tmp_path / "packages",
            "capability_environment_root_dir": tmp_path / "capabilities",
            "scheduler_enabled": False,
        }
    )
    application = create_app(
        settings=configured,
        session_factory=cast(async_sessionmaker[AsyncSession], database.session_factory),
        redis_client=redis,
        auth_service=StubAuthService(),
        config_service=object(),
        admin_resource_service=InMemoryAdminResourceService(),
        user_admin_service=object(),
        run_service=object(),
        rate_limiter=object(),
        database_probe=Probe(),
        redis_probe=Probe(),
    )
    with TestClient(application):
        service = getattr(application.state, "plugin_handover_services", None)
        assert service is not None
        reviews = application.state.plugin_review_services
        assert reviews is not None and reviews._confirmation is service._confirmation
        assert service._plans._storage is service._storage
        assert service._confirmation._storage is service._storage
        assert service._plans._grants is service._grants
        assert service._confirmation._grants is service._grants
        assert service._plans._snapshots is service._confirmation._snapshots
        assert service._storage._session_factory is database.session_factory
        assert service._plans._sources._runs._session_factory is database.session_factory
        assert service._plans._sources._workspaces is application.state.project_workspace_store
        assert service._plans._sources._generated_root == (tmp_path / "generated").absolute()
    assert not database.disposed and not redis.closed


def test_default_persistent_admin_uses_the_current_review_validator(tmp_path: Path) -> None:
    database = FakeDatabase()
    application = create_app(
        settings=Settings.model_validate({
            "project_workspace_dir": tmp_path / "workspaces",
            "plugin_package_store_dir": tmp_path / "packages",
            "capability_environment_root_dir": tmp_path / "capabilities",
            "scheduler_enabled": False,
        }),
        session_factory=cast(async_sessionmaker[AsyncSession], database.session_factory),
        redis_client=FakeRedis(), auth_service=StubAuthService(), config_service=object(),
        user_admin_service=object(), run_service=object(), rate_limiter=object(),
        database_probe=Probe(), redis_probe=Probe(),
    )
    with TestClient(application):
        service = application.state.admin_resource_service
        assert isinstance(service, PersistentAdminResourceService)
        reviews = application.state.plugin_review_services
        assert reviews is not None
        assert service._generated_plugin_approval_validator == reviews.validate_generated_approval_in_session
        scoped = service.for_principal(uuid4(), uuid4())
        assert scoped._generated_plugin_approval_validator == reviews.validate_generated_approval_in_session


def test_missing_sessions_has_no_in_memory_handover_fallback(tmp_path: Path) -> None:
    application = create_app(
        settings=Settings.model_validate({"project_workspace_dir": tmp_path / "workspaces"}),
        auth_service=StubAuthService(),
        config_service=object(),
        admin_resource_service=InMemoryAdminResourceService(),
        user_admin_service=object(),
        run_service=object(),
        rate_limiter=object(),
        database_probe=Probe(),
        redis_probe=Probe(),
    )
    with TestClient(application) as client:
        assert getattr(application.state, "plugin_handover_services", None) is None
        assert application.state.plugin_review_services is None
        response = client.get(
            f"/api/v1/runs/{uuid4()}/plugin-handovers/gh-{'a' * 64}",
            headers={"Authorization": "Bearer valid-token"},
        )
    assert response.status_code == 503


def test_restarted_lifespan_does_not_retain_previous_storage_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = FakeDatabase()
    application = create_app(
        settings=Settings.model_validate(
            {
                "project_workspace_dir": tmp_path / "workspaces",
                "plugin_package_store_dir": tmp_path / "packages",
                "scheduler_enabled": False,
            }
        ),
        session_factory=cast(async_sessionmaker[AsyncSession], database.session_factory),
        redis_client=FakeRedis(),
        auth_service=StubAuthService(),
        config_service=object(),
        admin_resource_service=InMemoryAdminResourceService(),
        user_admin_service=object(),
        run_service=object(),
        rate_limiter=object(),
        database_probe=Probe(),
        redis_probe=Probe(),
    )
    with TestClient(application):
        assert application.state.plugin_handover_services is not None
        assert application.state.plugin_review_services is not None

    def unavailable(*, package_root: Path) -> None:
        del package_root
        raise SnapshotUnavailable("unavailable")

    monkeypatch.setattr(
        "agent_hub.plugins.handover_snapshots.FrozenSourceSnapshotStore", unavailable
    )
    with TestClient(application):
        assert application.state.plugin_handover_services is None
        assert application.state.plugin_review_services is None


@pytest.mark.parametrize("error_type", [SnapshotUnavailable, InvalidSnapshot])
def test_unavailable_snapshot_storage_does_not_break_other_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
) -> None:
    attempted: list[Path] = []

    def unavailable(*, package_root: Path) -> None:
        attempted.append(package_root)
        raise error_type("private-storage-secret-that-must-not-be-logged")

    monkeypatch.setattr(
        "agent_hub.plugins.handover_snapshots.FrozenSourceSnapshotStore", unavailable
    )
    database = FakeDatabase()
    application = create_app(
        settings=Settings.model_validate(
            {
                "project_workspace_dir": tmp_path / "workspaces",
                "plugin_package_store_dir": tmp_path / "packages",
                "scheduler_enabled": False,
            }
        ),
        session_factory=cast(async_sessionmaker[AsyncSession], database.session_factory),
        redis_client=FakeRedis(),
        auth_service=StubAuthService(),
        config_service=object(),
        admin_resource_service=InMemoryAdminResourceService(),
        user_admin_service=object(),
        run_service=object(),
        rate_limiter=object(),
        database_probe=Probe(),
        redis_probe=Probe(),
    )
    with TestClient(application) as client:
        assert attempted == [tmp_path / "packages"]
        assert application.state.plugin_handover_services is None
        assert client.get("/health/live").status_code == 200
        response = client.get(
            f"/api/v1/runs/{uuid4()}/plugin-handovers/gh-{'a' * 64}",
            headers={"Authorization": "Bearer valid-token"},
        )
        assert response.status_code == 503
    assert "private-storage-secret" not in caplog.text
    assert "plugin_handover_unavailable" in caplog.text


@pytest.mark.parametrize("operation,limit", [("", 1024 * 1024), ("/cancel", 4096)])
def test_precise_handover_json_ingress_limits(operation: str, limit: int) -> None:
    path = f"/api/v1/runs/{uuid4()}/plugin-handovers"
    if operation:
        path += "/gh-" + "a" * 64 + operation
    assert _body_limit(cast(Scope, {"type": "http", "method": "POST", "path": path})) == limit


def test_confirmation_is_not_prebuffered_by_json_middleware() -> None:
    path = f"/api/v1/runs/{uuid4()}/plugin-handovers/gh-{'a' * 64}/confirm"
    assert _body_limit(cast(Scope, {"type": "http", "method": "POST", "path": path})) is None


async def test_json_limit_checks_real_chunks_without_trusting_length() -> None:
    scope = cast(
        Scope,
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/v1/runs/{uuid4()}/plugin-handovers/gh-{'a' * 64}/cancel",
            "headers": [(b"content-length", b"1")],
            "http_version": "1.1",
        },
    )
    messages: list[Message] = [
        {"type": "http.request", "body": b"x" * 4096, "more_body": True},
        {"type": "http.request", "body": b"y", "more_body": False},
    ]
    sent: list[Message] = []
    reached: list[bool] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive, send
        reached.append(True)

    await RequestBodyLimitMiddleware(application)(scope, receive, send)
    assert not reached
    assert sent[0]["status"] == 413
    assert b"request_too_large" in cast(bytes, sent[1]["body"])


async def test_exact_json_boundary_is_forwarded_without_changing_bytes() -> None:
    scope = cast(
        Scope,
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/v1/runs/{uuid4()}/plugin-handovers/gh-{'a' * 64}/cancel",
            "headers": [],
            "http_version": "1.1",
        },
    )
    messages: list[Message] = [
        {"type": "http.request", "body": b"x" * 4095, "more_body": True},
        {"type": "http.request", "body": b"y", "more_body": False},
    ]
    received: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        raise AssertionError(f"middleware must not send a response: {message['type']}")

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, send
        received.append(await receive())
        received.append(await receive())

    await RequestBodyLimitMiddleware(application)(scope, receive, send)
    assert received == [
        {"type": "http.request", "body": b"x" * 4095 + b"y", "more_body": False},
        {"type": "http.disconnect"},
    ]
