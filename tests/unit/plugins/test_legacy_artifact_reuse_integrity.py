"""Controlled filesystem/metadata tests, without child or namespace-isolation proof."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from starlette.requests import Request

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers import admin
from agent_hub.plugins import handover_confirmation as confirmation
from agent_hub.plugins import handover_stage_journal as publication
from agent_hub.plugins.handover_snapshots import _windows_security
from agent_hub.plugins.handover_stage_journal import StagePublicationJournal

TENANT = UUID("11111111-1111-4111-8111-111111111111")
FILES = (("README.md", b"controlled package\n"), ("adapter/main.py", b"pass\n"))
INVALID = (
    "extra_file",
    "extra_directory",
    "wrong_content",
    "missing_file",
    "hardlink_file",
    "symlink_file",
    "symlink_directory",
    "symlink_root",
    "special_file",
)


@pytest.fixture
def tmp_path(tmp_path: Path) -> Path:
    # The real journal requires a trusted store, including native Windows ACLs.
    store = tmp_path / "store"
    if sys.platform == "win32":
        _windows_security(store, private=True, create=True)
    else:
        store.mkdir(mode=0o700)
    return store


def _archive(
    files: tuple[tuple[str, bytes], ...] = FILES,
    *,
    compression: int = zipfile.ZIP_STORED,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        archive.writestr("plugin.json", json.dumps({"id": "reuse-tool", "name": "Reuse"}))
        for name, content in files:
            archive.writestr(name, content)
    return buffer.getvalue()


def _store(directory: Path, archive: bytes) -> admin._StoredPluginPackageMetadata:
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    settings=SimpleNamespace(plugin_package_store_dir=directory),
                )
            ),
        }
    )
    return admin._stored_plugin_package_metadata(
        request,
        tenant_id=TENANT,
        plugin_id="reuse-tool",
        package=admin.PluginPackageMetadata(
            kind="adapter_package", verified_public_key_sha256="a" * 64
        ),
        archive_bytes=archive,
        content_sha256=hashlib.sha256(archive).hexdigest(),
    )


def _populate(root: Path) -> None:
    root.mkdir(parents=True)
    for name, content in FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _invalidate(root: Path, case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    member = root / "adapter" / "main.py"
    if case == "extra_file":
        (root / ".existing-marker").write_bytes(b"keep unknown data")
    elif case == "extra_directory":
        (root / "unknown-empty-directory").mkdir()
    elif case == "wrong_content":
        member.write_bytes(b"do not replace these bytes\n")
    elif case == "missing_file":
        member.unlink()
    elif case == "hardlink_file":
        os.link(member, root.parent / "owned-hardlink-victim")
    else:
        path = (
            root
            if case == "symlink_root"
            else member.parent
            if case == "symlink_directory"
            else member
        )
        mode = stat.S_IFIFO if case == "special_file" else stat.S_IFLNK
        original_lstat = Path.lstat

        def lstat(target: Path) -> os.stat_result:
            info = original_lstat(target)
            if target != path:
                return info
            values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            values["st_mode"] = mode | stat.S_IMODE(info.st_mode)
            return cast(os.stat_result, SimpleNamespace(**values))

        # Link/special-node metadata is injected; no symlink privilege or FIFO read is used.
        monkeypatch.setattr(Path, "lstat", lstat)


def _snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        info = os.lstat(path)
        snapshot[path.relative_to(root).as_posix()] = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_mtime_ns,
            path.read_bytes() if stat.S_ISREG(info.st_mode) else None,
        )
    return snapshot


def _events(store: Path) -> list[dict[str, Any]]:
    ledger = store / ".stage-ownership" / str(TENANT) / "reuse-tool"
    if not ledger.exists():
        return []
    operations = list(ledger.iterdir())
    assert len(operations) == 1
    return [json.loads(path.read_bytes()) for path in sorted(operations[0].iterdir())]


@pytest.mark.parametrize("case", INVALID)
def test_reuse_matcher_rejects_nonexact_or_unsafe_existing_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    root = tmp_path / "canonical"
    _populate(root)
    _invalidate(root, case, monkeypatch)
    before = _snapshot(root)
    try:
        assert not admin._plugin_package_artifact_matches(root, FILES)
    finally:
        assert _snapshot(root) == before, "reuse validation mutated the canonical tree"


@pytest.mark.parametrize("case", INVALID)
def test_existing_nonexact_canonical_is_rejected_without_stage_writes_or_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    archive = _archive()
    root = tmp_path / str(TENANT) / "reuse-tool" / hashlib.sha256(archive).hexdigest()
    _populate(root)
    _invalidate(root, case, monkeypatch)
    before = _snapshot(root)
    writes: list[Path] = []
    original_write = Path.write_bytes

    def write(path: Path, content: bytes) -> int:
        writes.append(path)
        return original_write(path, content)

    monkeypatch.setattr(Path, "write_bytes", write)
    try:
        with pytest.raises(PublicAPIError) as caught:
            _store(tmp_path, archive)
        assert caught.value.status_code == 409
        assert caught.value.code == "plugin_package_artifact_conflict"
        assert str(tmp_path) not in caught.value.public_message
    finally:
        assert _snapshot(root) == before, "mismatch replaced or removed the canonical tree"
        assert not writes, "rejected reuse wrote a replacement stage"
        assert not list(root.parent.glob("tmp-*"))


def test_exact_canonical_is_reused_without_rewriting_its_identity(tmp_path: Path) -> None:
    archive = _archive()
    root = tmp_path / str(TENANT) / "reuse-tool" / hashlib.sha256(archive).hexdigest()
    _populate(root)
    before = _snapshot(root)
    stored = _store(tmp_path, archive)
    assert admin._plugin_package_artifact_matches(root, FILES)
    assert not stored.artifact_created
    assert _snapshot(root) == before
    assert not list(root.parent.glob("tmp-*"))
    assert not (tmp_path / ".stage-ownership").exists()


def test_absent_canonical_is_published_by_the_existing_install_flow(tmp_path: Path) -> None:
    archive = _archive()
    root = tmp_path / str(TENANT) / "reuse-tool" / hashlib.sha256(archive).hexdigest()
    assert not root.exists()
    stored = _store(tmp_path, archive)
    assert stored.artifact_created
    assert stored.package is not None and stored.package.artifact is not None
    assert stored.package.artifact.file_count == len(FILES)
    assert stored.package.artifact.total_size_bytes == sum(len(content) for _, content in FILES)
    assert admin._plugin_package_artifact_matches(root, FILES)
    assert not list(root.parent.glob("tmp-*"))
    events = _events(tmp_path)
    assert [event["state"] for event in events] == [
        "creating", "recording", "recording", "recording", "recording", "recording",
        "ready", "rename_attempted", "published",
    ]
    assert all(event["tenant"] == str(TENANT) for event in events)
    assert all(event["plugin_id"] == "reuse-tool" for event in events)
    assert all(event["target_digest"] == root.name for event in events)
    assert events[0]["reserved_bytes"] == 24
    assert events[0]["reserved_members"] == 4
    assert events[-1]["details"]["node"]["ino"] == root.lstat().st_ino


def test_concurrent_empty_canonical_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive()
    root = tmp_path / str(TENANT) / "reuse-tool" / hashlib.sha256(archive).hexdigest()
    original_rename = confirmation._rename_noreplace
    contender: list[tuple[int, int]] = []

    def rename(parent: confirmation._PinnedDirectory, stage: str, digest: str) -> None:
        root.mkdir()
        info = root.lstat()
        contender.append((info.st_dev, info.st_ino))
        original_rename(parent, stage, digest)

    monkeypatch.setattr(confirmation, "_rename_noreplace", rename)
    with pytest.raises(PublicAPIError) as caught:
        _store(tmp_path, archive)
    assert caught.value.status_code == 409
    assert caught.value.code == "plugin_package_artifact_conflict"
    info = root.lstat()
    assert contender == [(info.st_dev, info.st_ino)]
    assert list(root.iterdir()) == []
    stages = list(root.parent.glob("tmp-*"))
    assert len(stages) == 1
    assert (stages[0] / "README.md").read_bytes() == b"controlled package\n"


def test_exact_canonical_after_precheck_is_reused_without_claiming_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _archive()
    root = tmp_path / str(TENANT) / "reuse-tool" / hashlib.sha256(archive).hexdigest()
    original_info = confirmation._PinnedDirectory.info
    contender: list[dict[str, tuple[object, ...]]] = []

    def info(directory: confirmation._PinnedDirectory, name: str) -> os.stat_result:
        if directory.path == root.parent and name == root.name and not root.exists():
            _populate(root)
            contender.append(_snapshot(root))
        return original_info(directory, name)

    monkeypatch.setattr(confirmation._PinnedDirectory, "info", info)
    stored = _store(tmp_path, archive)
    assert not stored.artifact_created
    assert contender == [_snapshot(root)]
    assert stored.package is not None and stored.package.artifact is not None
    assert stored.package.artifact.file_count == 2
    assert not list(root.parent.glob("tmp-*"))
    assert not (tmp_path / ".stage-ownership").exists()


@pytest.mark.parametrize("component", ["store", "tenant"])
def test_parent_alias_metadata_is_rejected_before_creating_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str,
) -> None:
    archive = _archive()
    tenant = tmp_path / str(TENANT)
    alias = tmp_path
    if component == "tenant":
        tenant.mkdir()
        alias = tenant
    before = _snapshot(alias)
    original_lstat = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        info = original_lstat(path)
        if path != alias:
            return info
        values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        values["st_mode"] = stat.S_IFLNK | stat.S_IMODE(info.st_mode)
        return cast(os.stat_result, SimpleNamespace(**values))

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(PublicAPIError) as caught:
        _store(tmp_path, archive)
    assert caught.value.status_code == 409
    assert caught.value.code == "plugin_package_artifact_conflict"
    assert str(tmp_path) not in caught.value.public_message
    assert _snapshot(alias) == before
    assert not (tenant / "reuse-tool").exists()
    assert not (tmp_path / ".stage-ownership").exists()


def test_parent_pin_failure_is_unavailable_before_creating_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(path: Path, *, create: bool = False) -> confirmation._PinnedDirectory:
        raise OSError(f"controlled parent failure: {path}")

    monkeypatch.setattr(confirmation, "_PinnedDirectory", unavailable)
    with pytest.raises(PublicAPIError) as caught:
        _store(tmp_path, _archive())
    assert caught.value.status_code == 503
    assert caught.value.code == "plugin_package_store_unavailable"
    assert str(tmp_path) not in caught.value.public_message
    assert not (tmp_path / str(TENANT)).exists()


def test_creating_failure_creates_no_stage_or_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _archive()
    root = tmp_path / str(TENANT) / "reuse-tool" / hashlib.sha256(archive).hexdigest()
    original_append = StagePublicationJournal._append

    def append(journal: StagePublicationJournal, state: str, details: dict[str, object]) -> None:
        if state == "creating":
            raise OSError("controlled creating failure")
        original_append(journal, state, details)

    monkeypatch.setattr(StagePublicationJournal, "_append", append)
    with pytest.raises(PublicAPIError) as caught:
        _store(tmp_path, archive)
    assert caught.value.status_code == 503
    assert caught.value.code == "plugin_package_store_unavailable"
    assert not root.exists()
    assert not list(root.parent.glob("tmp-*"))
    assert _events(tmp_path) == []


@pytest.mark.parametrize("fault", ["write", "sync"])
def test_publication_failure_retains_unknown_stage_and_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    archive = _archive()
    root = tmp_path / str(TENANT) / "reuse-tool" / hashlib.sha256(archive).hexdigest()
    original_write = os.write
    original_sync = confirmation._PinnedDirectory.sync

    def write(fd: int, data: bytes | memoryview) -> int:
        if bytes(data) == b"controlled package\n":
            original_write(fd, b"controlled")
            raise OSError("controlled member write failure")
        return original_write(fd, data)

    def sync(directory: confirmation._PinnedDirectory) -> None:
        if directory.path.name.startswith("tmp-gh-"):
            raise OSError("controlled stage sync failure")
        original_sync(directory)

    if fault == "write":
        monkeypatch.setattr(os, "write", write)
    else:
        monkeypatch.setattr(confirmation._PinnedDirectory, "sync", sync)
    with pytest.raises(PublicAPIError) as caught:
        _store(tmp_path, archive)
    assert caught.value.status_code == 503
    assert caught.value.code == "plugin_package_store_unavailable"
    assert str(tmp_path) not in caught.value.public_message
    assert not root.exists()
    stages = list(root.parent.glob("tmp-*"))
    assert len(stages) == 1
    expected = b"controlled" if fault == "write" else b"controlled package\n"
    assert (stages[0] / "README.md").read_bytes() == expected
    events = _events(tmp_path)
    assert events[0]["state"] == "creating"
    assert events[-1]["state"] == "recording"
    assert not any(event["state"] in {"rename_attempted", "published"} for event in events)


def test_second_exact_install_adds_no_publication_records(tmp_path: Path) -> None:
    archive = _archive()
    first = _store(tmp_path, archive)
    assert first.artifact_created
    ledger = tmp_path / ".stage-ownership"
    assert ledger.exists()
    before = _snapshot(ledger)
    second = _store(tmp_path, archive)
    assert not second.artifact_created
    assert _snapshot(ledger) == before


def test_legacy_512_files_publish_with_real_journal(tmp_path: Path) -> None:
    files = tuple((f"file-{index:03d}.txt", b"x") for index in range(512))
    archive = _archive(files)
    stored = _store(tmp_path, archive)
    assert stored.artifact_created
    assert stored.package is not None and stored.package.artifact is not None
    assert stored.package.artifact.file_count == 512
    root = tmp_path / str(TENANT) / "reuse-tool" / hashlib.sha256(archive).hexdigest()
    assert len(list(root.iterdir())) == 512
    events = _events(tmp_path)
    assert len(events) == 1029
    assert events[0]["reserved_members"] == 513
    assert events[-1]["state"] == "published"


def test_legacy_two_million_source_bytes_have_separate_metadata_budget(tmp_path: Path) -> None:
    files = (("file.bin", b"x" * 2_000_000),)
    archive = _archive(files, compression=zipfile.ZIP_DEFLATED)
    assert len(archive) < 2_000_000
    stored = _store(tmp_path, archive)
    assert stored.artifact_created
    assert stored.package is not None and stored.package.artifact is not None
    assert stored.package.artifact.total_size_bytes == 2_000_000
    root = tmp_path / str(TENANT) / "reuse-tool" / hashlib.sha256(archive).hexdigest()
    assert (root / "file.bin").read_bytes() == b"x" * 2_000_000
    events = _events(tmp_path)
    assert events[0]["reserved_bytes"] == 2_000_000
    assert events[-1]["state"] == "published"
    assert (tmp_path / ".stage-ownership").exists()
    metadata_bytes = sum(
        len(path.read_bytes()) for path in (tmp_path / ".stage-ownership").rglob("*.json")
    )
    assert metadata_bytes < 16 * 1024 * 1024


def test_legacy_512_files_with_three_directory_levels_publish(tmp_path: Path) -> None:
    files = tuple((f"d{index}/a/b/file.txt", b"x") for index in range(512))
    archive = _archive(files)
    root = tmp_path / str(TENANT) / "reuse-tool" / hashlib.sha256(archive).hexdigest()
    stored = _store(tmp_path, archive)
    assert stored.artifact_created
    assert stored.package is not None and stored.package.artifact is not None
    assert stored.package.artifact.file_count == 512
    assert stored.package.artifact.total_size_bytes == 512
    for name, content in files:
        assert (root / name).read_bytes() == content
    assert not list(root.parent.glob("tmp-*"))
    events = _events(tmp_path)
    assert events[0]["reserved_members"] == 2049
    assert events[0]["member_budget"] == 2049
    assert len(events) == 1029
    assert events[-1]["state"] == "published"
    records = list((tmp_path / ".stage-ownership").rglob("*.json"))
    sizes = [path.stat().st_size for path in records]
    assert max(sizes) <= 1_048_576
    assert sum(sizes) <= 16 * 1024 * 1024


@pytest.mark.parametrize("limit", ["record", "total"])
def test_journal_byte_limit_is_explicit_and_retains_unknown_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str,
) -> None:
    files = tuple((f"file-{index:03d}.txt", b"x") for index in range(32))
    archive = _archive(files)
    root = tmp_path / str(TENANT) / "reuse-tool" / hashlib.sha256(archive).hexdigest()
    if limit == "record":
        monkeypatch.setattr(publication, "_MAX_BYTES", 8192)
    else:
        monkeypatch.setattr(publication, "_MAX_TOTAL_BYTES", 8192)
    with pytest.raises(PublicAPIError) as caught:
        _store(tmp_path, archive)
    assert caught.value.status_code == 503
    assert caught.value.details == {"reason": "publication_journal_unavailable"}
    assert not root.exists()
    stages = list(root.parent.glob("tmp-*"))
    assert len(stages) == 1
    assert (stages[0] / "file-000.txt").read_bytes() == b"x"
    events = _events(tmp_path)
    assert events[0]["state"] == "creating"
    assert not any(event["state"] in {"rename_attempted", "published"} for event in events)


def test_unknown_disk_capacity_is_unavailable_without_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _archive()
    root = tmp_path / str(TENANT) / "reuse-tool" / hashlib.sha256(archive).hexdigest()
    monkeypatch.setattr(publication, "_available_bytes", lambda directory: None)
    with pytest.raises(PublicAPIError) as caught:
        _store(tmp_path, archive)
    assert caught.value.status_code == 503
    assert caught.value.code == "plugin_package_store_unavailable"
    assert caught.value.details == {"reason": "publication_journal_unavailable"}
    assert not root.exists()
    assert not list(root.parent.glob("tmp-*"))
    assert not (tmp_path / ".stage-ownership").exists()
