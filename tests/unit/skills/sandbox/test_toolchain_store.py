"""Fake SDK bytes test storage IO only, never SDK executability or approval."""

from __future__ import annotations

import errno
import hashlib
import importlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.skills.sandbox.toolchain_archive import ArchiveLimits, inspect_go_archive
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainArtifact
from agent_hub.skills.sandbox.toolchain_stage import ToolchainStageObservation, stage_go_toolchain


def test_store_module_exists() -> None:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.toolchain_store") is not None


@pytest.fixture
def api() -> Any:
    if importlib.util.find_spec("agent_hub.skills.sandbox.toolchain_store") is None:
        pytest.skip("store absent; module-existence regression records RED")
    return importlib.import_module("agent_hub.skills.sandbox.toolchain_store")


def fixture() -> tuple[bytes, ToolchainArtifact, ToolchainStageObservation]:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for path, data in (
            ("go/bin/go", b"not-executable-sdk"),
            ("go/bin/gofmt", b"not-executable-gofmt"),
            ("go/VERSION", b"go1.27.3\n"),
            ("go/src/fmt/x.go", b"package fmt\n"),
        ):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o755 if path.startswith("go/bin/") else 0o644
            archive.addfile(info, io.BytesIO(data))
        empty = tarfile.TarInfo("go/empty/")
        empty.type = tarfile.DIRTYPE
        empty.mode = 0o755
        archive.addfile(empty)
    data = stream.getvalue()
    artifact = ToolchainArtifact.from_payload(
        {
            "stack": "go",
            "version": "1.27.3",
            "platform": "linux_amd64",
            "url": "https://dl.google.com/go/go1.27.3.linux-amd64.tar.gz",
            "sha256": hashlib.sha256(data).hexdigest(),
            "archive_bytes": len(data),
            "unpacked_bytes_limit": 1024,
            "archive_entries_limit": 16,
            "directory_entries_limit": 16,
            "tar_bytes_limit": 10240,
        }
    )
    manifest = inspect_go_archive(
        data,
        expected_sha256=artifact.sha256,
        expected_size_bytes=len(data),
        limits=ArchiveLimits(len(data), 10240, 16),
    )
    members = {f.path: f for f in manifest.files}
    observation = ToolchainStageObservation(
        artifact.version,
        artifact.platform,
        artifact.sha256,
        manifest.tree_sha256,
        members["go/bin/go"].sha256,
        members["go/bin/gofmt"].sha256,
        manifest.expanded_bytes,
        len(manifest.files),
        manifest,
    )
    return data, artifact, observation


def deadline() -> float:
    return time.monotonic() + 30


def native() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("Linux Root descriptor IO required; Windows skip is NOT native PASS")


def prepared(tmp_path: Path) -> tuple[Path, Path, bytes, ToolchainArtifact]:
    native()
    tmp_path.chmod(0o700)
    root, stage = tmp_path / "runtime", tmp_path / "incoming"
    root.mkdir(mode=0o700)
    stage.mkdir(mode=0o700)
    data, artifact, _ = fixture()
    stage_go_toolchain(data, artifact=artifact, stage=stage, deadline=deadline())
    return root, stage, data, artifact


def published(api: Any, tmp_path: Path) -> tuple[Path, Path, bytes, ToolchainArtifact]:
    root, stage, data, artifact = prepared(tmp_path)
    api.publish_staged_toolchain(root, stage, data, artifact=artifact, deadline=deadline())
    return root, root / artifact.artifact_sha256, data, artifact


def test_result_is_frozen_slotted_and_contains_no_hostpath(api: Any) -> None:
    _, artifact, observation = fixture()
    value = api.PublishedToolchain(artifact.artifact_sha256, observation, False)
    assert not hasattr(value, "__dict__")
    assert value.artifact_sha256 == artifact.artifact_sha256
    assert not hasattr(value, "path")
    with pytest.raises(FrozenInstanceError):
        value.reused = True


def test_compact_receipt_binds_full_identity_without_manifest(api: Any) -> None:
    _, artifact, observation = fixture()
    raw = api._receipt(artifact, observation)
    value = json.loads(raw)
    assert len(raw) < 65536
    assert b"manifest" not in raw and b"go/src/" not in raw
    assert value == {
        "schema_version": 1,
        "artifact_sha256": artifact.artifact_sha256,
        "archive_sha256": artifact.sha256,
        "archive_bytes": artifact.archive_bytes,
        "tree_sha256": observation.tree_sha256,
        "binary_sha256": observation.binary_sha256,
        "gofmt_sha256": observation.gofmt_sha256,
        "file_count": observation.file_count,
        "total_bytes": observation.total_bytes,
        "version": artifact.version,
        "platform": artifact.platform,
        "status": "measured_not_executed",
    }
    altered = ToolchainArtifact.from_payload(artifact.to_payload() | {"archive_entries_limit": 17})
    assert api._receipt(altered, observation) != raw


@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan"), True, "soon", None])
def test_bad_deadlines_fail_finitely(api: Any, bad: object) -> None:
    with pytest.raises(api.ToolchainStoreError):
        api._check(bad)


def test_expired_deadline_is_distinct(api: Any) -> None:
    with pytest.raises(api.ToolchainStoreError, match="toolchain_store_timeout"):
        api._check(time.monotonic() - 1)


def test_error_never_echoes_arbitrary_input(api: Any) -> None:
    error = api.ToolchainStoreError("/private/credential")
    assert error.code == str(error) == "toolchain_store_unsafe"


def test_unsupported_platform_writes_nothing(api: Any, tmp_path: Path) -> None:
    if sys.platform == "linux" and os.geteuid() == 0:
        pytest.skip("native host covered by descriptor tests")
    data, artifact, _ = fixture()
    for operation, args in (
        (api.publish_staged_toolchain, (tmp_path, tmp_path, data)),
        (api.observe_published_toolchain, (tmp_path,)),
    ):
        with pytest.raises(api.ToolchainStoreError, match="toolchain_store_unsupported"):
            operation(*args, artifact=artifact, deadline=deadline())
    assert not list(tmp_path.iterdir())


def test_samefs_publish_moves_stage_then_fresh_observe_is_readonly(
    api: Any, tmp_path: Path
) -> None:
    root, stage, data, artifact = prepared(tmp_path)
    inode = stage.stat().st_ino
    result = api.publish_staged_toolchain(root, stage, data, artifact=artifact, deadline=deadline())
    entry = root / artifact.artifact_sha256
    assert not stage.exists()
    assert entry.name != artifact.sha256
    assert sorted(p.name for p in entry.iterdir()) == ["archive.tar.gz", "payload", "receipt.json"]
    assert (entry / "payload").stat().st_ino == inode
    assert (entry / "payload/go/empty").is_dir()
    for p in (root, entry, entry / "payload"):
        assert p.stat().st_mode & 0o777 == 0o700
    for p in (entry / "archive.tar.gz", entry / "receipt.json"):
        assert p.stat().st_mode & 0o777 == 0o600 and p.stat().st_nlink == 1
    assert (entry / "archive.tar.gz").read_bytes() == data
    before = {
        p: (p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
        for p in (root, entry, *entry.rglob("*"))
    }
    fresh = api.observe_published_toolchain(root, artifact=artifact, deadline=deadline())
    assert not result.reused and fresh.reused
    assert result.observation == fresh.observation
    assert fresh.observation.status == "measured_not_executed"
    assert before == {
        p: (p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in before
    }


def test_existing_valid_entry_reuses_without_mutating_incoming(api: Any, tmp_path: Path) -> None:
    root, _, data, artifact = published(api, tmp_path)
    stage = tmp_path / "again"
    stage.mkdir(mode=0o700)
    original = stage_go_toolchain(data, artifact=artifact, stage=stage, deadline=deadline())
    before = stage.stat()
    result = api.publish_staged_toolchain(root, stage, data, artifact=artifact, deadline=deadline())
    assert result.reused and result.observation == original
    assert stage.stat() == before
    assert [p.name for p in root.iterdir()] == [artifact.artifact_sha256]


def test_exdev_restages_without_removing_incoming(
    api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, stage, data, artifact = prepared(tmp_path)
    real = api._rename_noreplace

    def cross_device(src: int, name: str, dst: int, target: str) -> None:
        if target == "payload":
            raise OSError(errno.EXDEV, "fixture EXDEV")
        real(src, name, dst, target)

    monkeypatch.setattr(api, "_rename_noreplace", cross_device)
    result = api.publish_staged_toolchain(root, stage, data, artifact=artifact, deadline=deadline())
    payload = root / artifact.artifact_sha256 / "payload"
    assert stage.exists() and payload.stat().st_ino != stage.stat().st_ino
    assert (
        result.observation
        == api.observe_published_toolchain(root, artifact=artifact, deadline=deadline()).observation
    )


@pytest.mark.parametrize(
    "case",
    [
        "extra",
        "missing",
        "archive",
        "receipt",
        "sdk",
        "mode",
        "symlink",
        "hardlink",
        "payload-mode",
        "oversize-receipt",
        "archive-mode",
        "receipt-mode",
        "duplicate-receipt",
    ],
)
def test_fresh_rejects_tampered_entry(api: Any, tmp_path: Path, case: str) -> None:
    root, entry, _, artifact = published(api, tmp_path)
    if case == "extra":
        (entry / "extra").write_bytes(b"x")
    elif case == "missing":
        (entry / "receipt.json").unlink()
    elif case == "archive":
        path = entry / "archive.tar.gz"
        raw = path.read_bytes()
        path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    elif case == "receipt":
        (entry / "receipt.json").write_bytes(b"{}")
    elif case == "duplicate-receipt":
        path = entry / "receipt.json"
        path.write_bytes(b'{"schema_version":1,' + path.read_bytes()[1:])
    elif case == "sdk":
        path = entry / "payload/go/bin/go"
        path.chmod(0o755)
        path.write_bytes(b"changed")
        path.chmod(0o555)
    elif case == "mode":
        entry.chmod(0o755)
    elif case == "payload-mode":
        (entry / "payload").chmod(0o755)
    elif case.endswith("-mode"):
        (entry / ("archive.tar.gz" if case == "archive-mode" else "receipt.json")).chmod(0o644)
    elif case == "oversize-receipt":
        (entry / "receipt.json").write_bytes(b"x" * 65537)
    elif case == "symlink":
        (entry / "receipt.json").unlink()
        (entry / "receipt.json").symlink_to("/etc/passwd")
    else:
        os.link(entry / "archive.tar.gz", tmp_path / "linked-archive")
    with pytest.raises(api.ToolchainStoreError):
        api.observe_published_toolchain(root, artifact=artifact, deadline=deadline())
    assert entry.exists()


def test_forged_receipt_cannot_authorize_changed_sdk(api: Any, tmp_path: Path) -> None:
    root, entry, _, artifact = published(api, tmp_path)
    path = entry / "payload/go/bin/go"
    path.chmod(0o755)
    path.write_bytes(b"forged")
    path.chmod(0o555)
    control = entry / "receipt.json"
    receipt = json.loads(control.read_bytes())
    receipt["binary_sha256"] = hashlib.sha256(b"forged").hexdigest()
    control.write_bytes(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(api.ToolchainStoreError):
        api.observe_published_toolchain(root, artifact=artifact, deadline=deadline())


@pytest.mark.parametrize(
    "case",
    [
        "root-mode",
        "root-symlink",
        "ancestor-symlink",
        "ancestor-owner",
        "stage-symlink",
        "bad-data",
        "bad-sdk",
        "existing-invalid",
    ],
)
def test_publish_rejects_before_creating_pending(api: Any, tmp_path: Path, case: str) -> None:
    root, stage, data, artifact = prepared(tmp_path)
    actual_root = root
    if case == "root-mode":
        root.chmod(0o755)
    elif case == "root-symlink":
        root = tmp_path / "root-link"
        root.symlink_to(actual_root, target_is_directory=True)
    elif case == "ancestor-symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        root = alias / "runtime"
    elif case == "ancestor-owner":
        if sys.platform == "linux":
            os.chown(tmp_path, 10001, 10001)
    elif case == "stage-symlink":
        alias = tmp_path / "stage-link"
        alias.symlink_to(stage, target_is_directory=True)
        stage = alias
    elif case == "bad-data":
        data += b"bad"
    elif case == "bad-sdk":
        (stage / "go/bin/go").chmod(0o755)
    else:
        (root / artifact.artifact_sha256).mkdir(mode=0o700)
    try:
        with pytest.raises(api.ToolchainStoreError):
            api.publish_staged_toolchain(root, stage, data, artifact=artifact, deadline=deadline())
        assert not any(p.name.startswith(".pending-") for p in actual_root.iterdir())
        assert stage.exists()
    finally:
        if sys.platform == "linux":
            os.chown(tmp_path, 0, 0)


def file_stat(size: int = 5, **changes: int) -> Any:
    import stat

    return SimpleNamespace(
        **(
            {
                "st_dev": 1,
                "st_ino": 9,
                "st_mode": stat.S_IFREG | 0o600,
                "st_uid": 0,
                "st_gid": 0,
                "st_nlink": 1,
                "st_size": size,
                "st_mtime_ns": 2,
                "st_ctime_ns": 3,
            }
            | changes
        )
    )


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "short",
        "long",
        "replaced",
        "changed",
        "hardlink",
        "owner",
        "mode",
        "huge",
        "empty",
        "directory",
    ],
)
def test_bounded_fd_read_faults_not_native_evidence(
    api: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    import stat
    from contextlib import ExitStack

    info = file_stat()
    if case == "hardlink":
        info.st_nlink = 2
    elif case == "owner":
        info.st_uid = 10001
    elif case == "mode":
        info.st_mode = stat.S_IFREG | 0o644
    elif case == "huge":
        info.st_size = 1000000
    elif case == "empty":
        info.st_size = 0
    elif case == "directory":
        info.st_mode = stat.S_IFDIR | 0o600
    reads: list[int] = []
    chunks = iter([b"abc" if case == "short" else b"abcdef" if case == "long" else b"abcde", b""])

    def read(fd: int, size: int) -> bytes:
        assert fd == 21
        reads.append(size)
        return next(chunks)

    stats = 0

    def fstat(fd: int) -> Any:
        nonlocal stats
        stats += 1
        return file_stat(st_ctime_ns=6) if case == "changed" and stats > 1 else info

    def open_file(name: str, flags: int, *, dir_fd: int) -> int:
        assert (name, dir_fd, flags) == ("receipt.json", 20, api._READ_FLAGS)
        return 21

    monkeypatch.setattr(api.os, "open", open_file)
    monkeypatch.setattr(api.os, "close", lambda fd: None)
    monkeypatch.setattr(api.os, "read", read)
    monkeypatch.setattr(api.os, "fstat", fstat)
    monkeypatch.setattr(
        api.os, "stat", lambda *a, **kw: file_stat(st_ino=8) if case == "replaced" else info
    )
    pins: list[Any] = []
    with ExitStack() as stack:
        if case == "valid":
            assert api._read_file(20, "receipt.json", 100, deadline(), stack, pins) == b"abcde"
            assert len(pins) == 1
            pins[0].check()
            assert reads == [6, 1]
        else:
            with pytest.raises(ValueError):
                api._read_file(20, "receipt.json", 100, deadline(), stack, pins)
            assert not pins
    assert all(size <= 101 for size in reads)


@pytest.mark.parametrize(
    "code,expected",
    [
        (errno.EXDEV, errno.EXDEV),
        (errno.EEXIST, errno.EEXIST),
        (errno.EIO, errno.EIO),
        (errno.ENOSYS, "toolchain_store_unsupported"),
        (errno.EINVAL, "toolchain_store_unsupported"),
        (errno.EOPNOTSUPP, "toolchain_store_unsupported"),
        (0, None),
    ],
)
def test_renameat2_flag_and_errno_no_emulated_fallback(
    api: Any, monkeypatch: pytest.MonkeyPatch, code: int, expected: int | str | None
) -> None:
    class Function:
        argtypes: Any = None
        restype: Any = None

        def __call__(self, *args: object) -> int:
            assert args == (11, b"incoming", 12, b"payload", 1)
            return -1 if code else 0

    monkeypatch.setattr(api.ctypes, "CDLL", lambda *a, **kw: SimpleNamespace(renameat2=Function()))
    monkeypatch.setattr(api.ctypes, "get_errno", lambda: code)
    if type(expected) is int:
        with pytest.raises(OSError) as caught:
            api._rename_noreplace(11, "incoming", 12, "payload")
        assert caught.value.errno == expected
    elif isinstance(expected, str):
        with pytest.raises(api.ToolchainStoreError, match=expected):
            api._rename_noreplace(11, "incoming", 12, "payload")
    else:
        api._rename_noreplace(11, "incoming", 12, "payload")


def test_renameat2_missing_fails_closed(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.ctypes, "CDLL", lambda *a, **kw: SimpleNamespace())
    with pytest.raises(api.ToolchainStoreError, match="toolchain_store_unsupported"):
        api._rename_noreplace(11, "incoming", 12, "payload")


@pytest.mark.parametrize("swapped", [False, True])
def test_final_name_must_still_identify_original_pending_fd_not_native_evidence(
    api: Any, monkeypatch: pytest.MonkeyPatch, swapped: bool
) -> None:
    import stat

    info = file_stat(st_mode=stat.S_IFDIR | 0o700)
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(api, "_rename_noreplace", lambda *args: calls.append(args))
    monkeypatch.setattr(api.os, "fsync", lambda fd: calls.append(("fsync", fd)))
    monkeypatch.setattr(api.os, "fstat", lambda fd: info)
    monkeypatch.setattr(
        api.os,
        "stat",
        lambda name, **kw: file_stat(
            st_mode=stat.S_IFDIR | 0o700, st_ino=10 if swapped and name == "artifact" else 9
        ),
    )
    if swapped:
        with pytest.raises(ValueError):
            api._publish_container(11, 12, ".pending-fixture", "artifact", deadline())
    else:
        pin = api._publish_container(11, 12, ".pending-fixture", "artifact", deadline())
        pin.check()
    assert calls == [(11, ".pending-fixture", 11, "artifact"), ("fsync", 11)]


@pytest.mark.parametrize("kind", ["os", "stage", "stage-timeout", "archive-timeout", "store"])
def test_exception_translation_is_finite(api: Any, kind: str) -> None:
    from agent_hub.skills.sandbox.toolchain_archive import ToolchainArchiveError
    from agent_hub.skills.sandbox.toolchain_stage import ToolchainStageError

    failures = {
        "os": OSError("/private/source"),
        "stage": ToolchainStageError(),
        "stage-timeout": ToolchainStageError("toolchain_stage_timeout"),
        "archive-timeout": ToolchainArchiveError("archive_timeout"),
        "store": api.ToolchainStoreError("toolchain_store_conflict"),
    }
    expected = (
        "toolchain_store_timeout"
        if kind.endswith("timeout")
        else "toolchain_store_conflict"
        if kind == "store"
        else "toolchain_store_unsafe"
    )
    with pytest.raises(api.ToolchainStoreError, match=expected):
        api._error(failures[kind])


def test_absent_observe_does_not_create_entry(api: Any, tmp_path: Path) -> None:
    root, _, _, artifact = prepared(tmp_path)
    with pytest.raises(api.ToolchainStoreError, match="toolchain_store_missing"):
        api.observe_published_toolchain(root, artifact=artifact, deadline=deadline())
    assert not list(root.iterdir())


@pytest.mark.parametrize("phase", ["archive-write", "move", "measure", "fsync", "publish"])
def test_uncertain_failure_preserves_pending_without_gc(
    api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    root, stage, data, artifact = prepared(tmp_path)
    if phase == "archive-write":
        monkeypatch.setattr(
            api, "_write_file", lambda *a: (_ for _ in ()).throw(OSError("private"))
        )
    elif phase in {"move", "publish"}:
        real = api._rename_noreplace

        def rename(src: int, name: str, dst: int, target: str) -> None:
            if (target == "payload") == (phase == "move"):
                raise OSError(errno.EIO, "private host path")
            real(src, name, dst, target)

        monkeypatch.setattr(api, "_rename_noreplace", rename)
    elif phase == "fsync":
        monkeypatch.setattr(api.os, "fsync", lambda *a: (_ for _ in ()).throw(OSError("private")))
    else:
        real_measure = api.measure_staged_go_toolchain

        def measure(path: Path, **kwargs: Any) -> Any:
            if path.name == "payload":
                raise OSError("private host path")
            return real_measure(path, **kwargs)

        monkeypatch.setattr(api, "measure_staged_go_toolchain", measure)
    with pytest.raises(api.ToolchainStoreError) as caught:
        api.publish_staged_toolchain(root, stage, data, artifact=artifact, deadline=deadline())
    assert "private" not in str(caught.value)
    pending = list(root.iterdir())
    assert len(pending) == 1 and pending[0].name.startswith(".pending-")


def test_atomic_final_collision_never_overwrites_winner(
    api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, stage, data, artifact = prepared(tmp_path)
    real = api._rename_noreplace

    def collide(src: int, name: str, dst: int, target: str) -> None:
        if target == artifact.artifact_sha256:
            (root / target).mkdir(mode=0o700)
            (root / target / "retained").write_bytes(b"winner")
        real(src, name, dst, target)

    monkeypatch.setattr(api, "_rename_noreplace", collide)
    with pytest.raises(api.ToolchainStoreError, match="toolchain_store_conflict"):
        api.publish_staged_toolchain(root, stage, data, artifact=artifact, deadline=deadline())
    assert (root / artifact.artifact_sha256 / "retained").read_bytes() == b"winner"
    assert len([p for p in root.iterdir() if p.name.startswith(".pending-")]) == 1


@pytest.mark.parametrize("case", ["archive", "receipt", "payload", "root"])
def test_fresh_pins_detect_mutations_during_measurement(
    api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    root, entry, _, artifact = published(api, tmp_path)
    real = api.measure_staged_go_toolchain

    def measure(path: Path, **kwargs: Any) -> Any:
        result = real(path, **kwargs)
        if case in {"archive", "receipt"}:
            file = entry / ("archive.tar.gz" if case == "archive" else "receipt.json")
            replacement = tmp_path / "replacement"
            replacement.write_bytes(file.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(file)
        elif case == "payload":
            (entry / "payload").rename(tmp_path / "old-payload")
            (entry / "payload").mkdir(mode=0o700)
        else:
            root.rename(tmp_path / "old-root")
            root.mkdir(mode=0o700)
        return result

    monkeypatch.setattr(api, "measure_staged_go_toolchain", measure)
    with pytest.raises(api.ToolchainStoreError):
        api.observe_published_toolchain(root, artifact=artifact, deadline=deadline())
    assert root.exists()


def test_failure_after_atomic_rename_preserves_published_state(
    api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, stage, data, artifact = prepared(tmp_path)
    real = api._publish_container

    def fail_after_rename(*args: Any) -> Any:
        real(*args)
        raise OSError(errno.EIO, "post-rename fixture")

    monkeypatch.setattr(api, "_publish_container", fail_after_rename)
    with pytest.raises(api.ToolchainStoreError):
        api.publish_staged_toolchain(root, stage, data, artifact=artifact, deadline=deadline())
    assert not stage.exists()
    assert [p.name for p in root.iterdir()] == [artifact.artifact_sha256]
    assert api.observe_published_toolchain(root, artifact=artifact, deadline=deadline()).reused
