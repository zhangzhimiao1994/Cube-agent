from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from agent_hub.files.generated import GeneratedFileStore

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PNG_MIME = "image/png"
ZIP_MIME = "application/zip"


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
    tmp_path: Path, filename: str
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


def test_store_bytes_rejects_unknown_mime_type_with_stable_error(tmp_path: Path) -> None:
    store = GeneratedFileStore(tmp_path)

    with pytest.raises(ValueError, match="unsupported generated file MIME type"):
        store.store_bytes(
            tenant_id=uuid4(),
            run_id=uuid4(),
            artifact_id=uuid4(),
            filename="report.bin",
            mime_type="application/octet-stream",
            data=b"binary",
        )


def test_store_bytes_rejects_mime_extension_mismatch(tmp_path: Path) -> None:
    store = GeneratedFileStore(tmp_path)

    with pytest.raises(ValueError, match="filename extension"):
        store.store_bytes(
            tenant_id=uuid4(),
            run_id=uuid4(),
            artifact_id=uuid4(),
            filename="installer.exe",
            mime_type=PNG_MIME,
            data=b"\x89PNG",
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
        f"/api/v1/admin/runs/{run_id}/artifacts/{artifact_id}/download"
    )
    assert metadata.to_public_dict() == {
        "filename": "launch-deck.pptx",
        "mime_type": PPTX_MIME,
        "size_bytes": len(data),
        "sha256": sha256(data).hexdigest(),
        "download_url": f"/api/v1/admin/runs/{run_id}/artifacts/{artifact_id}/download",
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
