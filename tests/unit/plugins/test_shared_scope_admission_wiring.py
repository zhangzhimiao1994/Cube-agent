"""Actual shared-lock callers enroll and bind infrastructure without payload authority."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from starlette.requests import Request

from agent_hub.plugins.handover_confirmation import (
    _publish_artifact,
    _shared_fs_lock,
    _shared_fs_lock_at_root,
)
from agent_hub.plugins.handover_stage_reservations import ReservationPolicy, StageReservationStore
from agent_hub.plugins.handover_storage import Conflict
from tests.unit.plugins.test_handover_stage_journal import private_directory

TENANT = UUID("11111111-1111-4111-8111-111111111111")


def configured(tmp_path: Path) -> tuple[StageReservationStore, Request]:
    root = tmp_path / "store"
    private_directory(root)
    store = StageReservationStore(
        root,
        ReservationPolicy(
            max_bytes=200_000_000, max_members=50_000, max_operations=1, min_available_bytes=0
        ),
    )
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    plugin_publication_reservations=store,
                    settings=SimpleNamespace(plugin_package_store_dir=root),
                )
            ),
        }
    )
    return store, request


def test_refused_payload_leaves_accounted_lock_and_other_plugin_can_publish(tmp_path: Path) -> None:
    store, request = configured(tmp_path)
    root = store.store_root
    with (
        _shared_fs_lock(request, TENANT, "refused.plugin"),
        pytest.raises(RuntimeError, match="no longer valid"),
    ):
        _publish_artifact(
            root / str(TENANT) / "refused.plugin" / ("b" * 64),
            (("file", b"safe"),),
            store_root=root,
            tenant=TENANT,
            plugin_id="refused.plugin",
            reservation_store=store,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            revalidate=lambda: False,
        )
    assert store.usage().operations == 0
    assert not (root / str(TENANT)).exists()
    with _shared_fs_lock(request, TENANT, "allowed.plugin"):
        assert (
            _publish_artifact(
                root / str(TENANT) / "allowed.plugin" / ("c" * 64),
                (("file", b"safe"),),
                store_root=root,
                tenant=TENANT,
                plugin_id="allowed.plugin",
                reservation_store=store,
                deadline=datetime.now(UTC) + timedelta(seconds=30),
                revalidate=lambda: True,
            )
            is True
        )
    assert store.usage().operations == 1


@pytest.mark.parametrize("change", ["identity", "plugin", "root"])
def test_acquired_shared_lock_must_match_enrolled_scope(tmp_path: Path, change: str) -> None:
    store, _ = configured(tmp_path)
    scope = store.prepare_scope_lock(
        tenant=TENANT,
        plugin_id="plugin",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        revalidate=lambda: True,
    )
    if change == "identity":
        scope = replace(scope, scope_lock_identity=(0,) * 15)
    elif change == "plugin":
        scope = replace(scope, plugin_id="foreign")
    else:
        scope = replace(scope, store_root=tmp_path / "foreign")
    with (
        pytest.raises(Conflict),
        _shared_fs_lock_at_root(
            store.store_root, TENANT, "plugin", scope=scope
        ),
    ):
        pytest.fail("foreign enrolled scope reached caller")
    assert store.usage().operations == 0
