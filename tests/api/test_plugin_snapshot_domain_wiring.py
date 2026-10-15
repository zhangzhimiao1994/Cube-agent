"""Actual app lifespan storage boundaries, with session/service doubles only."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.api.routers.admin import InMemoryAdminResourceService
from agent_hub.app import _execution_scope_protected_paths, _plugin_snapshot_store_root, create_app
from agent_hub.plugins.handover_stage_reservations import ReservationPolicy, StageReservationStore
from agent_hub.settings import Settings
from tests.api.test_foundation_api import FakeDatabase, FakeRedis, Probe, StubAuthService
from tests.unit.plugins.test_handover_stage_journal import private_directory


@pytest.mark.parametrize("explicit", [False, True])
def test_reservation_app_uses_a_separate_snapshot_domain(tmp_path: Path, explicit: bool) -> None:
    root = tmp_path / "packages"
    private_directory(root)
    source = tmp_path / ("source-store" if explicit else "packages-sources")
    settings = Settings.model_validate(
        {
            "plugin_package_store_dir": root,
            "plugin_source_snapshot_store_dir": source if explicit else None,
            "project_workspace_dir": tmp_path / "workspaces",
            "capability_environment_root_dir": tmp_path / "environments",
            "generated_artifact_dir": tmp_path / "generated",
            "scheduler_enabled": False,
        }
    )
    database = FakeDatabase()
    store = StageReservationStore(
        root,
        ReservationPolicy(
            max_bytes=100_000_000, max_members=20_000, max_operations=2, min_available_bytes=0
        ),
    )
    application = create_app(
        settings=settings,
        plugin_publication_reservations=store,
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
        services = application.state.plugin_handover_services
        assert services is not None
        assert services._plans._snapshots._root == source
        assert services._confirmation._snapshots._root == source
        assert source in _execution_scope_protected_paths(settings)
        assert not (root / ".handover-snapshots").exists()
        assert store.usage().operations == 0


@pytest.mark.parametrize("nested", ["same", "inside", "outside"])
def test_configured_snapshot_and_artifact_domains_cannot_overlap(
    tmp_path: Path,
    nested: str,
) -> None:
    root = tmp_path / "packages"
    private_directory(root)
    source = {"same": root, "inside": root / "sources", "outside": tmp_path}[nested]
    settings = Settings.model_construct(
        plugin_package_store_dir=root,
        plugin_source_snapshot_store_dir=source,
    )
    store = StageReservationStore(
        root,
        ReservationPolicy(max_bytes=100_000_000, max_members=20_000, max_operations=2),
    )
    with pytest.raises(ValueError, match="snapshot.*overlap"):
        create_app(settings=settings, plugin_publication_reservations=store)
    assert not (source / ".handover-snapshots").exists()


@pytest.mark.parametrize("path", [Path("relative"), Path("/"), Path("E:/")])
def test_snapshot_setting_rejects_relative_or_volume_root(path: Path) -> None:
    assert "plugin_source_snapshot_store_dir" in Settings.model_fields
    with pytest.raises(ValidationError):
        Settings.model_validate({"plugin_source_snapshot_store_dir": path})


def test_legacy_snapshot_root_remains_unchanged_without_reservations(tmp_path: Path) -> None:
    root = tmp_path / "legacy-packages"
    assert (
        _plugin_snapshot_store_root(Settings.model_construct(plugin_package_store_dir=root)) == root
    )


def test_explicit_overlap_is_rejected_without_reservation_injection(tmp_path: Path) -> None:
    settings = Settings.model_construct(
        plugin_package_store_dir=tmp_path / "packages",
        plugin_source_snapshot_store_dir=tmp_path / "packages/sources",
    )
    with pytest.raises(ValueError, match="snapshot.*overlap"):
        create_app(settings=settings)


def test_snapshot_alias_to_artifacts_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    private_directory(root)
    alias = tmp_path / "source-alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink privilege unavailable")
    settings = Settings.model_construct(
        plugin_package_store_dir=root,
        plugin_source_snapshot_store_dir=alias,
    )
    with pytest.raises(ValueError, match="snapshot.*overlap"):
        _plugin_snapshot_store_root(settings, separate=True)
