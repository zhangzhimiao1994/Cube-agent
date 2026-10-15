"""HTTP wiring of explicitly configured cooperative admission, with stub authentication."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from agent_hub.api.routers.admin import InMemoryAdminResourceService
from agent_hub.app import create_app
from agent_hub.plugins.handover_stage_reservations import ReservationPolicy, StageReservationStore
from agent_hub.settings import Settings
from tests.api.test_admin_resources import (
    AcceptingRuntimePluginService,
    StubAuthService,
    headers,
    plugin_public_key_value,
    signed_plugin_archive,
)
from tests.unit.plugins.test_handover_stage_journal import private_directory


@pytest.fixture
def store(tmp_path: Path) -> StageReservationStore:
    root = tmp_path / "store"
    private_directory(root)
    return StageReservationStore(
        root,
        ReservationPolicy(
            max_bytes=100_000_000, max_members=20_000, max_operations=1, min_available_bytes=0
        ),
    )


def test_app_retains_only_matching_explicit_reservation_store(store: StageReservationStore) -> None:
    settings = Settings.model_construct(plugin_package_store_dir=store.store_root)
    app = create_app(
        settings=settings,
        plugin_publication_reservations=store,
        auth_service=StubAuthService(),
        rate_limiter=object(),
    )
    assert app.state.plugin_publication_reservations is store
    with pytest.raises(ValueError):
        create_app(
            settings=Settings.model_construct(plugin_package_store_dir=store.store_root / "other"),
            plugin_publication_reservations=store,
        )


def test_legacy_http_install_charges_once_and_refuses_another_publication(
    store: StageReservationStore,
) -> None:
    app = create_app(
        settings=Settings.model_construct(plugin_package_store_dir=store.store_root),
        plugin_publication_reservations=store,
        auth_service=StubAuthService(),
        rate_limiter=object(),
    )
    app.state.admin_resource_service = InMemoryAdminResourceService()
    app.state.settings = Settings.model_construct(plugin_package_store_dir=store.store_root)
    app.state.plugin_service = AcceptingRuntimePluginService()
    api = TestClient(app)
    private_key = ed25519.Ed25519PrivateKey.generate()
    assert (
        api.post(
            "/api/v1/admin/plugins/signing-keys",
            headers=headers(),
            json={
                "key_id": "calendar-prod",
                "algorithm": "ed25519",
                "public_key": plugin_public_key_value(private_key),
            },
        ).status_code
        == 200
    )
    archive = signed_plugin_archive(private_key, files={"adapter/main.py": "pass\n"})
    upload_headers = {
        **headers(),
        "Content-Type": "application/zip",
        "X-Agent-Hub-Plugin-Filename": "calendar.zip",
    }
    first = api.post("/api/v1/admin/plugins/install", headers=upload_headers, content=archive)
    assert first.status_code == 200, first.json()
    assert store.usage().operations == 1
    assert (
        api.post(
            "/api/v1/admin/plugins/install", headers=upload_headers, content=archive
        ).status_code
        == 200
    )
    assert store.usage().operations == 1
    different = signed_plugin_archive(private_key, files={"adapter/main.py": "changed = True\n"})
    refused = api.post("/api/v1/admin/plugins/install", headers=upload_headers, content=different)
    assert refused.status_code == 503
    assert store.usage().operations == 1
    assert not list(store.store_root.rglob("tmp-gh-*"))


def test_scope_admission_capacity_refusal_is_a_safe_http_error(
    store: StageReservationStore,
) -> None:
    limited = StageReservationStore(store.store_root, replace(store._policy, max_bytes=32768))
    settings = Settings.model_construct(plugin_package_store_dir=store.store_root)
    app = create_app(
        settings=settings,
        plugin_publication_reservations=limited,
        auth_service=StubAuthService(),
        rate_limiter=object(),
    )
    app.state.settings = settings
    app.state.admin_resource_service = InMemoryAdminResourceService()
    app.state.plugin_service = AcceptingRuntimePluginService()
    api = TestClient(app)
    private_key = ed25519.Ed25519PrivateKey.generate()
    assert (
        api.post(
            "/api/v1/admin/plugins/signing-keys",
            headers=headers(),
            json={
                "key_id": "calendar-prod",
                "algorithm": "ed25519",
                "public_key": plugin_public_key_value(private_key),
            },
        ).status_code
        == 200
    )
    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar.zip",
        },
        content=signed_plugin_archive(private_key),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "plugin_package_store_unavailable"
    assert str(store.store_root) not in response.text
    assert limited.usage().operations == 0
    assert not (store.store_root / ".uninstall-locks").exists()
