"""Creation evidence must not attribute another publisher's canonical object."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent_hub.plugins import handover_confirmation as publication
from tests.unit.plugins.test_handover_stage_journal import private_directory
from tests.unit.plugins.test_legacy_artifact_reuse_integrity import FILES, TENANT, _archive, _store


def test_publication_reports_created_then_reused_without_another_journal(tmp_path: Path) -> None:
    store = tmp_path / "store"
    root = store / str(TENANT) / "reuse-tool" / ("a" * 64)
    private_directory(store)
    private_directory(store / str(TENANT))
    private_directory(root.parent)
    assert (
        publication._publish_artifact(
            root, FILES, store_root=store, tenant=TENANT, plugin_id="reuse-tool"
        )
        is True
    )
    operations = tuple((store / ".stage-ownership" / str(TENANT) / "reuse-tool").iterdir())
    assert (
        publication._publish_artifact(
            root, FILES, store_root=store, tenant=TENANT, plugin_id="reuse-tool"
        )
        is False
    )
    assert tuple((store / ".stage-ownership" / str(TENANT) / "reuse-tool").iterdir()) == operations


def test_legacy_racing_exact_reuse_is_not_this_requests_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive()
    root = tmp_path / str(TENANT) / "reuse-tool" / hashlib.sha256(archive).hexdigest()
    original = publication._publish_artifact

    def concurrent_publisher(
        selected: Path,
        files: tuple[tuple[str, bytes], ...],
        *,
        store_root: Path | None = None,
        tenant: object = None,
        plugin_id: str | None = None,
        **publication_options: object,
    ) -> object:
        selected.mkdir()
        for name, content in files:
            target = selected / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return original(selected, files, store_root=store_root, tenant=TENANT, plugin_id=plugin_id)

    monkeypatch.setattr(publication, "_publish_artifact", concurrent_publisher)
    stored = _store(tmp_path, archive)
    assert stored.artifact_created is False
    assert root.is_dir()
    assert not (tmp_path / ".stage-ownership").exists()
