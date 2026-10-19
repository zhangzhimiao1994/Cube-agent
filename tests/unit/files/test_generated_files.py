from __future__ import annotations

import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from agent_hub.files.generated import GeneratedFileStore

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PNG_MIME = "image/png"
ZIP_MIME = "application/zip"


@pytest.mark.parametrize("method", ["bytes", "copy", "move"])
def test_long_artifact_paths_preserve_timestamp_and_scoped_download(
    tmp_path: Path, method: str,
) -> None:
    root = tmp_path / ("workspace-" + "a" * 100) / "generated"
    store = GeneratedFileStore(root)
    tenant_id, run_id, artifact_id = uuid4(), uuid4(), uuid4()
    data = b"PK\x03\x04long-path-artifact"
    source = tmp_path / "source.zip"
    source.write_bytes(data)
    if method == "bytes":
        metadata = store.store_bytes(
            tenant_id, run_id, artifact_id, "verified-project.zip", ZIP_MIME, data,
            timestamp_filename=True,
        )
    else:
        metadata = store.store_path(
            tenant_id, run_id, artifact_id, "verified-project.zip", ZIP_MIME, source,
            move=method == "move", timestamp_filename=True,
        )
    assert len(str(root / metadata.storage_key)) > 260
    assert metadata.filename.startswith("verified-project_")
    assert metadata.sha256 == sha256(data).hexdigest()
    assert store.resolve_for(tenant_id, run_id, artifact_id, metadata.storage_key).read_bytes() == data
    assert source.exists() is (method != "move")
    with pytest.raises(ValueError, match="context"):
        store.resolve_for(uuid4(), run_id, artifact_id, metadata.storage_key)
    store.delete_run(tenant_id, run_id)
    with pytest.raises(FileNotFoundError):
        store.resolve(metadata.storage_key)


@pytest.mark.parametrize("method", ["store_bytes", "store_path"])
@pytest.mark.parametrize(
    ("filename", "mime_type", "expected"),
    [
        ("report.docx", DOCX_MIME, "report_20261018T012345123456Z.docx"),
        ("deck.pptx", PPTX_MIME, "deck_20261018T012345123456Z.pptx"),
        ("project.zip", ZIP_MIME, "project_20261018T012345123456Z.zip"),
        ("source.tar.gz", "application/gzip", "source_20261018T012345123456Z.tar.gz"),
        ("report.v2.DOCX", DOCX_MIME, "report.v2_20261018T012345123456Z.DOCX"),
        (
            "report_20990101T000000000000Z.docx",
            DOCX_MIME,
            "report_20990101T000000000000Z_20261018T012345123456Z.docx",
        ),
    ],
)
def test_generated_names_use_trusted_utc_at_both_storage_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    filename: str,
    mime_type: str,
    expected: str,
) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> FrozenDatetime:
            assert tz is UTC
            return cls(2026, 10, 18, 1, 23, 45, 123456, tzinfo=UTC)

    monkeypatch.setattr("agent_hub.files.generated.datetime", FrozenDatetime, raising=False)
    tenant_id, run_id, artifact_id = uuid4(), uuid4(), uuid4()
    store = GeneratedFileStore(tmp_path / "generated")
    data = b"generated content"
    if method == "store_bytes":
        metadata = store.store_bytes(
            tenant_id, run_id, artifact_id, filename, mime_type, data, timestamp_filename=True
        )
    else:
        source = tmp_path / filename
        source.write_bytes(data)
        metadata = store.store_path(
            tenant_id, run_id, artifact_id, filename, mime_type, source, timestamp_filename=True
        )
        assert source.read_bytes() == data

    assert metadata.filename == expected
    assert metadata.storage_key == f"{tenant_id}/{run_id}/{artifact_id}/{expected}"
    assert store.resolve(metadata.storage_key).name == expected
    assert store.resolve(metadata.storage_key).read_bytes() == data
    assert metadata.to_public_dict()["filename"] == expected
    assert metadata.to_content_file()["filename"] == expected
    assert "storage_key" not in metadata.to_public_dict()
    assert str(tmp_path) not in str(metadata.to_public_dict())
    assert metadata.sha256 == sha256(data).hexdigest()


@pytest.mark.parametrize("stem", ["a" * 235, "\u62a5" * 80])
def test_timestamped_names_fit_portable_filesystem_component_limit(
    tmp_path: Path, stem: str
) -> None:
    root = Path(f"\\\\?\\{tmp_path.resolve()}") if os.name == "nt" else tmp_path
    store = GeneratedFileStore(root)
    before = datetime.now(UTC)
    metadata = store.store_bytes(
        uuid4(), uuid4(), uuid4(), f"{stem}.docx", DOCX_MIME, b"docx", timestamp_filename=True
    )
    after = datetime.now(UTC)

    assert len(metadata.filename.encode("utf-8")) <= 255
    stamp = metadata.filename.rsplit("_", 1)[1].removesuffix(".docx")
    created = datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    assert before <= created <= after
    assert store.resolve(metadata.storage_key).read_bytes() == b"docx"


def test_existing_untimestamped_storage_keys_remain_readable(tmp_path: Path) -> None:
    tenant_id, run_id, artifact_id = uuid4(), uuid4(), uuid4()
    storage_key = f"{tenant_id}/{run_id}/{artifact_id}/legacy.docx"
    legacy = tmp_path / storage_key
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")

    assert GeneratedFileStore(tmp_path).resolve_for(
        tenant_id, run_id, artifact_id, storage_key
    ).read_bytes() == b"legacy"


def test_plain_store_reuses_filename_for_repeated_materialization(tmp_path: Path) -> None:
    tenant_id, run_id, artifact_id = uuid4(), uuid4(), uuid4()
    store = GeneratedFileStore(tmp_path)
    first = store.store_bytes(tenant_id, run_id, artifact_id, "workspace.zip", ZIP_MIME, b"zip")
    second = store.store_bytes(tenant_id, run_id, artifact_id, "workspace.zip", ZIP_MIME, b"zip")

    assert first == second
    assert first.filename == "workspace.zip"
    assert list(store.resolve(first.storage_key).parent.iterdir()) == [store.resolve(first.storage_key)]


def test_store_bytes_writes_file_under_tenant_run_artifact_scope(tmp_path: Path) -> None:
    tenant_id = uuid4()
    run_id = uuid4()
    artifact_id = uuid4()
    data = b"docx-bytes"
    store = GeneratedFileStore(tmp_path)

    metadata = store.store_bytes(
        tenant_id=tenant_id,
        run_id=run_id,
        artifact_id=artifact_id,
        filename="weekly-report.docx",
        mime_type=DOCX_MIME,
        data=data,
    )

    expected_path = tmp_path / str(tenant_id) / str(run_id) / str(artifact_id) / "weekly-report.docx"
    assert expected_path.read_bytes() == data
    assert store.resolve(metadata.storage_key) == expected_path


def test_store_path_moves_file_without_loading_or_retaining_source(tmp_path: Path) -> None:
    tenant_id = uuid4()
    run_id = uuid4()
    artifact_id = uuid4()
    source = tmp_path / "source" / "bundle.zip"
    source.parent.mkdir()
    source.write_bytes(b"PK\x03\x04workspace-bundle")
    store = GeneratedFileStore(tmp_path / "generated")

    metadata = store.store_path(
        tenant_id=tenant_id,
        run_id=run_id,
        artifact_id=artifact_id,
        filename="workspace.zip",
        mime_type=ZIP_MIME,
        source=source,
        move=True,
    )

    assert source.exists() is False
    stored = store.resolve(metadata.storage_key)
    assert stored.read_bytes() == b"PK\x03\x04workspace-bundle"
    assert metadata.size_bytes == stored.stat().st_size
    assert metadata.sha256 == sha256(stored.read_bytes()).hexdigest()


@pytest.mark.parametrize("timestamp_filename", [False, True])
@pytest.mark.parametrize(
    "filename",
    [
        "../report.docx",
        "/tmp/report.docx",
        "   ",
        "bad\x1fname.docx",
        "NUL.docx",
        "CON",
        "COM1.pptx",
        "report.docx.",
    ],
)
def test_store_bytes_rejects_filenames_that_could_escape_or_are_not_safe(
    tmp_path: Path, filename: str, timestamp_filename: bool
) -> None:
    store = GeneratedFileStore(tmp_path)

    with pytest.raises(ValueError, match="filename"):
        store.store_bytes(
            tenant_id=uuid4(),
            run_id=uuid4(),
            artifact_id=uuid4(),
            filename=filename,
            mime_type=DOCX_MIME,
            data=b"content",
            timestamp_filename=timestamp_filename,
        )

    assert not any(tmp_path.rglob("*"))


def test_store_bytes_allows_office_image_and_archive_mime_types(tmp_path: Path) -> None:
    store = GeneratedFileStore(tmp_path)

    docx = store.store_bytes(uuid4(), uuid4(), uuid4(), "report.docx", DOCX_MIME, b"docx")
    pptx = store.store_bytes(uuid4(), uuid4(), uuid4(), "deck.pptx", PPTX_MIME, b"pptx")
    png = store.store_bytes(uuid4(), uuid4(), uuid4(), "chart.png", PNG_MIME, b"\x89PNG")
    archive = store.store_bytes(uuid4(), uuid4(), uuid4(), "bundle.zip", ZIP_MIME, b"PK\x03\x04")

    assert docx.mime_type == DOCX_MIME
    assert pptx.mime_type == PPTX_MIME
    assert png.mime_type == PNG_MIME
    assert archive.mime_type == ZIP_MIME


@pytest.mark.parametrize("timestamp_filename", [False, True])
def test_store_bytes_rejects_unknown_mime_type_with_stable_error(
    tmp_path: Path, timestamp_filename: bool
) -> None:
    store = GeneratedFileStore(tmp_path)

    with pytest.raises(ValueError, match="unsupported generated file MIME type"):
        store.store_bytes(
            tenant_id=uuid4(),
            run_id=uuid4(),
            artifact_id=uuid4(),
            filename="report.bin",
            mime_type="application/octet-stream",
            data=b"binary",
            timestamp_filename=timestamp_filename,
        )


@pytest.mark.parametrize("timestamp_filename", [False, True])
def test_store_bytes_rejects_mime_extension_mismatch(
    tmp_path: Path, timestamp_filename: bool
) -> None:
    store = GeneratedFileStore(tmp_path)

    with pytest.raises(ValueError, match="filename extension"):
        store.store_bytes(
            tenant_id=uuid4(),
            run_id=uuid4(),
            artifact_id=uuid4(),
            filename="installer.exe",
            mime_type=PNG_MIME,
            data=b"\x89PNG",
            timestamp_filename=timestamp_filename,
        )


def test_metadata_contains_public_file_contract(tmp_path: Path) -> None:
    tenant_id = uuid4()
    run_id = uuid4()
    artifact_id = uuid4()
    data = b"pptx-bytes"
    store = GeneratedFileStore(tmp_path)

    metadata = store.store_bytes(
        tenant_id=tenant_id,
        run_id=run_id,
        artifact_id=artifact_id,
        filename="launch-deck.pptx",
        mime_type=PPTX_MIME,
        data=data,
    )

    assert metadata.filename == "launch-deck.pptx"
    assert metadata.mime_type == PPTX_MIME
    assert metadata.size_bytes == len(data)
    assert metadata.sha256 == sha256(data).hexdigest()
    assert metadata.storage_key == (
        f"{tenant_id}/{run_id}/{artifact_id}/launch-deck.pptx"
    )
    assert metadata.download_url == (
        f"/api/v1/runs/{run_id}/artifacts/{artifact_id}/download"
    )
    assert metadata.to_public_dict() == {
        "filename": "launch-deck.pptx",
        "mime_type": PPTX_MIME,
        "size_bytes": len(data),
        "sha256": sha256(data).hexdigest(),
        "download_url": f"/api/v1/runs/{run_id}/artifacts/{artifact_id}/download",
    }
    assert metadata.to_content_file() == {
        **metadata.to_public_dict(),
        "storage_key": f"{tenant_id}/{run_id}/{artifact_id}/launch-deck.pptx",
    }


@pytest.mark.parametrize(
    "storage_key",
    [
        "../outside.docx",
        f"{uuid4()}/{uuid4()}/{uuid4()}/../../outside.docx",
        f"{uuid4()}/{uuid4()}/{uuid4()}/",
        "/absolute/path/report.docx",
    ],
)
def test_resolve_rejects_tampered_storage_key(tmp_path: Path, storage_key: str) -> None:
    store = GeneratedFileStore(tmp_path)

    with pytest.raises(ValueError, match="storage_key"):
        store.resolve(storage_key)


def test_resolve_rejects_missing_file_with_valid_storage_key(tmp_path: Path) -> None:
    tenant_id = uuid4()
    run_id = uuid4()
    artifact_id = uuid4()
    store = GeneratedFileStore(tmp_path)
    storage_key = f"{tenant_id}/{run_id}/{artifact_id}/missing.docx"

    with pytest.raises(FileNotFoundError):
        store.resolve(storage_key)


def test_delete_run_removes_only_that_runs_generated_files(tmp_path: Path) -> None:
    tenant_id = uuid4()
    run_id = uuid4()
    sibling_run_id = uuid4()
    artifact_id = uuid4()
    sibling_artifact_id = uuid4()
    store = GeneratedFileStore(tmp_path)

    metadata = store.store_bytes(
        tenant_id,
        run_id,
        artifact_id,
        "report.docx",
        DOCX_MIME,
        b"docx",
    )
    sibling_metadata = store.store_bytes(
        tenant_id,
        sibling_run_id,
        sibling_artifact_id,
        "deck.pptx",
        PPTX_MIME,
        b"pptx",
    )

    store.delete_run(tenant_id, run_id)

    with pytest.raises(FileNotFoundError):
        store.resolve(metadata.storage_key)
    assert store.resolve(sibling_metadata.storage_key).read_bytes() == b"pptx"


def test_delete_run_ignores_missing_generated_directory(tmp_path: Path) -> None:
    GeneratedFileStore(tmp_path).delete_run(uuid4(), uuid4())


def test_resolve_for_returns_existing_file_when_context_matches(tmp_path: Path) -> None:
    tenant_id = uuid4()
    run_id = uuid4()
    artifact_id = uuid4()
    store = GeneratedFileStore(tmp_path)
    metadata = store.store_bytes(tenant_id, run_id, artifact_id, "report.docx", DOCX_MIME, b"docx")

    assert store.resolve_for(tenant_id, run_id, artifact_id, metadata.storage_key) == store.resolve(
        metadata.storage_key
    )


@pytest.mark.parametrize(
    ("requested_run_matches", "requested_artifact_matches"),
    [
        (False, True),
        (True, False),
    ],
)
def test_resolve_for_rejects_existing_file_from_different_context(
    tmp_path: Path, requested_run_matches: bool, requested_artifact_matches: bool
) -> None:
    tenant_id = uuid4()
    stored_run_id = uuid4()
    stored_artifact_id = uuid4()
    requested_run_id = stored_run_id if requested_run_matches else uuid4()
    requested_artifact_id = stored_artifact_id if requested_artifact_matches else uuid4()
    store = GeneratedFileStore(tmp_path)
    metadata = store.store_bytes(
        tenant_id, stored_run_id, stored_artifact_id, "report.docx", DOCX_MIME, b"docx"
    )

    with pytest.raises(ValueError, match="storage_key context"):
        store.resolve_for(tenant_id, requested_run_id, requested_artifact_id, metadata.storage_key)
