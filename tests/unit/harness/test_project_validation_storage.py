"""Safe tree copy contract; runnable directly without importing agent_hub."""

from __future__ import annotations

import os
import re
import runpy
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

_MODULE = (
    Path(__file__).resolve().parents[3] / "src/agent_hub/harness/project_validation_storage.py"
)


class _Copy(Protocol):
    def __call__(
        self, source: Path, target: Path, *, deadline: float, reject_hardlinks: bool,
    ) -> dict[str, object]: ...


def _copy(
    source: Path, target: Path, *, reject_hardlinks: bool = False, deadline: float | None = None,
) -> dict[str, object]:
    assert _MODULE.is_file(), "standalone safe-copy implementation is missing"
    api = cast(_Copy, runpy.run_path(str(_MODULE))["copy_validation_tree"])
    return api(
        source, target, reject_hardlinks=reject_hardlinks,
        deadline=time.monotonic() + 30 if deadline is None else deadline,
    )


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source tree"
    source.mkdir()
    (source / "payload.txt").write_bytes(b"original payload")
    return source


def test_rollback_has_bounded_budget_after_copy_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = runpy.run_path(str(_MODULE))
    root = tmp_path / "owned partial copy"
    root.mkdir()
    owned: dict[tuple[str, ...], os.stat_result] = {(): root.lstat()}
    for index in range(12):
        path = root / f"file-{index}"
        path.write_bytes(b"owned")
        owned[(path.name,)] = path.lstat()
    tree = module["_Tree"](root, root.lstat(), time.monotonic() - 10)
    elapsed = [0.0]
    unlink = os.unlink

    def slow_unlink(path: Any, *, dir_fd: int | None = None) -> None:
        unlink(path, dir_fd=dir_fd)
        elapsed[0] += 0.25

    with monkeypatch.context() as patch:
        patch.setattr(time, "monotonic", lambda: elapsed[0])
        patch.setattr(os, "unlink", slow_unlink)
        with pytest.raises(TimeoutError, match="deadline"):
            module["_cleanup"](tree, owned)
    assert elapsed[0] <= 1.25
    assert 0 < len(list(root.iterdir())) < 12


def _symlink(path: Path, text: str, *, directory: bool = False) -> None:
    try:
        path.symlink_to(text, target_is_directory=directory)
    except OSError as exc:
        if sys.platform == "win32" and exc.winerror == 1314:
            pytest.skip("Windows symlink privilege unavailable")
        raise


def test_runpy_api_copies_empty_tree(tmp_path: Path) -> None:
    source = tmp_path / "empty"
    source.mkdir()
    result = _copy(source, tmp_path / "copy")
    assert set(result) == {"sha256", "files", "bytes"}
    assert re.fullmatch("[0-9a-f]{64}", str(result["sha256"]))
    assert result["files"] == 0 and result["bytes"] == 0
    assert (tmp_path / "copy").is_dir()


def test_complete_tree_spaces_and_two_independent_copies(tmp_path: Path) -> None:
    source = _source(tmp_path)
    (source / "empty directory").mkdir()
    (source / "nested").mkdir()
    (source / "nested" / ".hidden").write_bytes(b"\x00\xff\n")
    (source / "zero").touch()
    first, second = tmp_path / "first copy", tmp_path / "second copy"
    one, two = _copy(source, first), _copy(source, second)
    assert one == two
    assert one["files"] == 3 and one["bytes"] == 19
    assert (first / "empty directory").is_dir()
    assert (first / "nested" / ".hidden").read_bytes() == b"\x00\xff\n"
    assert (first / "zero").read_bytes() == b""
    for relative in ("payload.txt", "nested/.hidden", "zero"):
        paths = [root / relative for root in (source, first, second)]
        assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
        assert all(path.stat().st_nlink == 1 for path in paths)
    (first / "payload.txt").write_bytes(b"changed")
    assert (source / "payload.txt").read_bytes() == b"original payload"
    assert (second / "payload.txt").read_bytes() == b"original payload"


@pytest.mark.parametrize("change", ["content", "size", "path", "kind", "empty_directory"])
def test_digest_covers_content_and_structure(tmp_path: Path, change: str) -> None:
    source = _source(tmp_path)
    before = _copy(source, tmp_path / "before")["sha256"]
    file = source / "payload.txt"
    if change == "content":
        file.write_bytes(b"modified payload")
    elif change == "size":
        file.write_bytes(b"longer original payload")
    elif change == "path":
        file.rename(source / "renamed.txt")
    elif change == "kind":
        file.unlink()
        file.mkdir()
    else:
        (source / "empty").mkdir()
    assert _copy(source, tmp_path / "after")["sha256"] != before


def test_digest_ignores_creation_order_and_timestamps(tmp_path: Path) -> None:
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    for name in ["b", "a"]:
        (left / name).write_bytes(name.encode())
    for name in ["a", "b"]:
        (right / name).write_bytes(name.encode())
        os.utime(right / name, (1, 1))
    assert _copy(left, tmp_path / "l") == _copy(right, tmp_path / "r")


def test_node_bin_relative_links_and_directory_chain(tmp_path: Path) -> None:
    source = _source(tmp_path)
    package = source / "node_modules" / "tool"
    package.mkdir(parents=True)
    (package / "cli.js").write_bytes(b"console.log('ok')")
    bins = source / "node_modules" / ".bin"
    bins.mkdir()
    _symlink(bins / "tool", "../tool/cli.js")
    _symlink(source / "package", "node_modules/tool", directory=True)
    _symlink(source / "cli", "package/cli.js")
    target = tmp_path / "copy"
    result = _copy(source, target)
    assert result["files"] == 2 and result["bytes"] == 33
    assert os.readlink(target / "node_modules" / ".bin" / "tool") == "../tool/cli.js"
    assert os.readlink(target / "cli") == "package/cli.js"
    assert (target / "cli").read_bytes() == b"console.log('ok')"
    assert _copy(target, tmp_path / "recopy") == result


def test_digest_includes_link_text(tmp_path: Path) -> None:
    source = _source(tmp_path)
    link = source / "link"
    _symlink(link, "payload.txt")
    before = _copy(source, tmp_path / "first")
    link.unlink()
    _symlink(link, "./payload.txt")
    assert _copy(source, tmp_path / "second")["sha256"] != before["sha256"]


@pytest.mark.parametrize("kind", ["absolute", "escape", "dangling", "self", "cycle", "via_link"])
def test_rejects_unsafe_links_without_touching_source(tmp_path: Path, kind: str) -> None:
    source = _source(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep outside")
    link = source / "unsafe"
    text = {
        "absolute": str(outside), "escape": "../outside", "dangling": "missing",
        "self": "unsafe", "cycle": "other", "via_link": "other/payload.txt",
    }[kind]
    _symlink(link, text)
    if kind == "cycle":
        _symlink(source / "other", "unsafe")
    elif kind == "via_link":
        _symlink(source / "other", "..", directory=True)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        _copy(source, tmp_path / "copy")
    assert os.path.lexists(link)
    assert (source / "payload.txt").read_bytes() == b"original payload"
    assert outside.read_bytes() == b"keep outside"
    assert not os.path.lexists(tmp_path / "copy")


def test_source_root_symlink_is_rejected(tmp_path: Path) -> None:
    source = _source(tmp_path)
    alias = tmp_path / "alias"
    _symlink(alias, source.name, directory=True)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        _copy(alias, tmp_path / "copy")
    assert (source / "payload.txt").exists()
    assert not (tmp_path / "copy").exists()


@pytest.mark.parametrize("target_kind", ["directory", "file", "symlink", "dangling"])
def test_existing_target_is_never_removed(tmp_path: Path, target_kind: str) -> None:
    source = _source(tmp_path)
    target = tmp_path / "target"
    if target_kind == "directory":
        target.mkdir()
        (target / "keep").write_bytes(b"owned by caller")
    elif target_kind == "file":
        target.write_bytes(b"owned by caller")
    else:
        _symlink(target, source.name if target_kind == "symlink" else "absent", directory=True)
    before = target.lstat()
    with pytest.raises((OSError, ValueError, RuntimeError)):
        _copy(source, target)
    assert target.lstat().st_ino == before.st_ino
    if target_kind == "directory":
        assert (target / "keep").read_bytes() == b"owned by caller"
    assert (source / "payload.txt").exists()


@pytest.mark.parametrize("relation", ["same", "child", "parent"])
def test_overlapping_paths_rejected(tmp_path: Path, relation: str) -> None:
    source = _source(tmp_path)
    target = {"same": source, "child": source / "copy", "parent": tmp_path}[relation]
    with pytest.raises((OSError, ValueError, RuntimeError)):
        _copy(source, target)
    assert (source / "payload.txt").read_bytes() == b"original payload"
    assert not (source / "copy").exists()


def test_overlap_through_parent_alias_rejected(tmp_path: Path) -> None:
    source = _source(tmp_path)
    alias = tmp_path / "alias"
    _symlink(alias, source.name, directory=True)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        _copy(source, alias / "copy")
    assert not (source / "copy").exists()


@pytest.mark.parametrize("reject", [False, True])
def test_hardlinks_are_rejected_or_copied_independently(tmp_path: Path, reject: bool) -> None:
    source = _source(tmp_path)
    os.link(source / "payload.txt", source / "alias")
    target = tmp_path / "copy"
    if reject:
        with pytest.raises((OSError, ValueError, RuntimeError)):
            _copy(source, target, reject_hardlinks=True)
        assert not target.exists()
    else:
        result = _copy(source, target)
        assert result["files"] == 2 and result["bytes"] == 32
        assert (target / "alias").stat().st_nlink == 1
        (target / "alias").write_bytes(b"changed")
        assert (target / "payload.txt").read_bytes() == b"original payload"
    assert (source / "payload.txt").read_bytes() == b"original payload"


@pytest.mark.parametrize("deadline", [0.0, float("nan"), float("inf")])
def test_invalid_or_expired_deadline_leaves_no_target(tmp_path: Path, deadline: float) -> None:
    source = _source(tmp_path)
    with pytest.raises((TimeoutError, ValueError)):
        _copy(source, tmp_path / "copy", deadline=deadline)
    assert not (tmp_path / "copy").exists()
    assert (source / "payload.txt").exists()


def test_deadline_checked_during_work_and_partial_copy_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    target = tmp_path / "copy"
    real_clock = time.monotonic

    def clock() -> float:
        return real_clock() + (60 if (target / "payload.txt").exists() else 0)

    deadline = real_clock() + 30
    monkeypatch.setattr(time, "monotonic", clock)
    with pytest.raises(TimeoutError):
        _copy(source, target, deadline=deadline)
    assert not target.exists()
    assert (source / "payload.txt").read_bytes() == b"original payload"


@pytest.mark.parametrize("change", ["replace_file", "replace_root", "stat", "add_file"])
def test_source_change_after_snapshot_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    source = _source(tmp_path)
    target = tmp_path / "copy"
    original_mkdir = Path.mkdir

    def mkdir(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        original_mkdir(path, mode, parents=parents, exist_ok=exist_ok)
        if path == target:
            if change == "replace_file":
                file = source / "payload.txt"
                old = file.stat()
                file.rename(tmp_path / "original-file")
                file.write_bytes(b"original payload")
                os.utime(file, ns=(old.st_atime_ns, old.st_mtime_ns))
            elif change == "replace_root":
                source.rename(tmp_path / "original-tree")
                source.mkdir()
                (source / "payload.txt").write_bytes(b"original payload")
            elif change == "stat":
                os.utime(source / "payload.txt", ns=(1, 1))
            else:
                (source / "new").write_bytes(b"unscanned")

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        _copy(source, target)
    assert not target.exists()
    assert (source / "payload.txt").read_bytes() == b"original payload"


def test_target_creation_race_does_not_clean_callers_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    target = tmp_path / "copy"
    original_mkdir = Path.mkdir

    def mkdir(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        if path == target:
            original_mkdir(target)
            (target / "keep").write_bytes(b"caller")
        original_mkdir(path, mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(FileExistsError):
        _copy(source, target)
    assert (target / "keep").read_bytes() == b"caller"


def test_target_replacement_is_not_removed_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    target = tmp_path / "copy"
    original_read = os.read
    replaced = False

    def read(fd: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(fd, size)
        if target.exists() and not replaced:
            replaced = True
            target.rename(tmp_path / "moved-copy")
            target.mkdir()
            (target / "keep").write_bytes(b"caller")
            raise OSError("injected read failure after target replacement")
        return chunk

    if sys.platform == "win32":
        pytest.skip("Windows denies renaming directories containing open files")
    monkeypatch.setattr(os, "read", read)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        _copy(source, target)
    assert replaced and (target / "keep").read_bytes() == b"caller"
    assert (source / "payload.txt").read_bytes() == b"original payload"


def test_large_file_uses_bounded_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source(tmp_path)
    payload = b"abcdef\x00\xff" * (1024 * 1024)
    (source / "large.bin").write_bytes(payload)
    original_read = os.read
    sizes: list[int] = []

    def read(fd: int, size: int) -> bytes:
        assert 0 < size <= 1024 * 1024, "unbounded file read"
        sizes.append(size)
        return original_read(fd, size)

    def no_whole_file(path: Path) -> bytes:
        raise AssertionError(f"whole-file read: {path}")

    monkeypatch.setattr(os, "read", read)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", no_whole_file)
        result = _copy(source, tmp_path / "copy")
    assert sizes and result["bytes"] == len(payload) + 16
    assert (tmp_path / "copy" / "large.bin").read_bytes() == payload


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX special files")
@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_rejects_special_files(tmp_path: Path, kind: str) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX special files")
    source = _source(tmp_path)
    sock: socket.socket | None = None
    try:
        if kind == "fifo":
            os.mkfifo(source / "pipe")
        else:
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(source / "socket"))
        with pytest.raises((OSError, ValueError, RuntimeError)):
            _copy(source, tmp_path / "copy")
        assert not (tmp_path / "copy").exists()
    finally:
        if sock is not None:
            sock.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction")
def test_rejects_junction(tmp_path: Path) -> None:
    source = _source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"outside")
    junction = source / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    try:
        with pytest.raises((OSError, ValueError, RuntimeError)):
            _copy(source, tmp_path / "copy")
        assert not (tmp_path / "copy").exists()
        assert (outside / "keep").read_bytes() == b"outside"
    finally:
        junction.rmdir()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable permission")
def test_preserves_executable_bits_without_special_mode_bits(tmp_path: Path) -> None:
    source = _source(tmp_path)
    file = source / "payload.txt"
    file.chmod(0o4755)
    _copy(source, tmp_path / "copy")
    assert stat.S_IMODE((tmp_path / "copy" / "payload.txt").stat().st_mode) == 0o755


@pytest.mark.parametrize("link_text", ["payload.txt/", "payload.txt/.", "payload.txt/../payload.txt"])
def test_link_resolution_rejects_file_as_directory(tmp_path: Path, link_text: str) -> None:
    # The resolver is pure; this also exercises POSIX link syntax on Windows
    # machines without symlink privileges. Entries use actual file/dir stats.
    source = _source(tmp_path)
    module: dict[str, Any] = runpy.run_path(str(_MODULE))
    entry = module["_Entry"]
    entries = {
        (): entry(source.lstat(), "directory"),
        ("payload.txt",): entry((source / "payload.txt").lstat(), "file"),
        ("link",): entry((source / "payload.txt").lstat(), "link", link_text),
    }
    with pytest.raises(ValueError):
        module["_resolve_link"](("link",), entries, time.monotonic() + 10)


@pytest.mark.parametrize("mutation", ["write_error", "source_content", "target_content"])
def test_io_failure_and_mid_copy_changes_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    source = _source(tmp_path)
    target = tmp_path / "copy"
    original_write, original_read = os.write, os.read
    triggered = False

    def write(fd: int, data: bytes) -> int:
        nonlocal triggered
        if not triggered:
            triggered = True
            if mutation == "write_error":
                raise OSError("injected write failure")
            (source / "payload.txt").write_bytes(b"modified payload")
        return original_write(fd, data)

    def read(fd: int, size: int) -> bytes:
        nonlocal triggered
        chunk = original_read(fd, size)
        output = target / "payload.txt"
        if output.exists() and output.stat().st_size and not triggered:
            triggered = True
            output.write_bytes(b"corrupted target")
        return chunk

    if mutation == "target_content":
        monkeypatch.setattr(os, "read", read)
    else:
        monkeypatch.setattr(os, "write", write)
    with pytest.raises((OSError, RuntimeError)):
        _copy(source, target)
    assert triggered and not target.exists()
    expected = b"modified payload" if mutation == "source_content" else b"original payload"
    assert (source / "payload.txt").read_bytes() == expected


@pytest.mark.parametrize("source_kind", ["missing", "file"])
def test_invalid_source_creates_no_target(tmp_path: Path, source_kind: str) -> None:
    source = tmp_path / "source"
    if source_kind == "file":
        source.write_bytes(b"keep")
    with pytest.raises((OSError, ValueError)):
        _copy(source, tmp_path / "copy")
    assert not (tmp_path / "copy").exists()
    if source_kind == "file":
        assert source.read_bytes() == b"keep"
