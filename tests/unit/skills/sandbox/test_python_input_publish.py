"""Publisher tests never fetch, extract, import or execute package contents."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import importlib
import io
import os
import posixpath
import shutil
import stat
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

RAW = b"statically checked wheel bytes; never executed"
FILENAME = "demo_pkg-1.0-py3-none-any.whl"
CACHE = PurePosixPath("/private/cache")
STAGING = PurePosixPath("/private/staging")


def test_public_publisher_entry_exists() -> None:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.python_input_publish") is not None


@dataclass(frozen=True)
class Entry:
    filename: str = FILENAME
    name: str = "demo-pkg"
    version: str = "1.0"
    sha256: str = hashlib.sha256(RAW).hexdigest()
    size_bytes: int = len(RAW)
    url: str = "https://files.pythonhosted.org/packages/ab/" + FILENAME


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    name = "agent_hub.skills.sandbox.python_input_publish"
    assert importlib.util.find_spec(name) is not None, "atomic root wheel publisher is missing"
    module = importlib.import_module(name)
    validator = ModuleType("agent_hub.skills.sandbox.python_input_fetch")
    validator.validate_wheel_bytes = lambda entry, data: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, validator.__name__, validator)
    return module


@dataclass
class Node:
    ino: int
    mode: int
    data: bytes = b""
    uid: int = 0
    gid: int = 0
    dev: int = 1
    nlink: int = 1
    mtime: int = 1


class FileSystem:
    """Small FD filesystem model, including publication and durability fault points."""

    O_DIRECTORY = 1 << 20
    O_NOFOLLOW = 1 << 21
    O_CLOEXEC = 1 << 22
    O_NONBLOCK = 1 << 23
    O_TMPFILE = (1 << 24) | O_DIRECTORY

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.fds: dict[int, tuple[Node, str, int]] = {}
        self.events: list[tuple[str, str]] = []
        self.rename_error: int | None = None
        self.after_rename_error = False
        self.fail_cache_fsync = False
        self.fail_unlink = False
        self.fail_write = False
        self.rename_hook: Any = None
        self.fsync_hook: Any = None
        self.fstat_hook: Any = None
        self.tmpfile_supported = False
        self.tmpfile_error: int | None = None
        self.unnamed: list[Node] = []
        self.link_error: int | None = None
        self.after_link_error = False
        self.link_hook: Any = None
        for name in ("/", "/private", str(CACHE), str(STAGING)):
            self.add(name, stat.S_IFDIR | 0o700)

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def add(self, path: str, mode: int, data: bytes = b"", **kwargs: Any) -> Node:
        node = Node(len(self.nodes) + 100, mode, data, **kwargs)
        self.nodes[path] = node
        return node

    def path(self, name: str, dir_fd: int | None) -> str:
        return posixpath.normpath(name if name.startswith("/") else
                                 posixpath.join(self.fds[dir_fd][1], name))  # type: ignore[index]

    def open(self, name: str, flags: int, mode: int = 0o777,
             *, dir_fd: int | None = None) -> int:
        path = self.path(name, dir_fd)
        if flags & self.O_TMPFILE == self.O_TMPFILE:
            if not self.tmpfile_supported or self.tmpfile_error:
                raise OSError(self.tmpfile_error or errno.EOPNOTSUPP, "unnamed unavailable")
            assert path == str(CACHE) and not flags & os.O_EXCL
            node = Node(1000 + len(self.unnamed), stat.S_IFREG | mode,
                        nlink=0, dev=self.nodes[path].dev)
            self.unnamed.append(node)
            fd = max(self.fds, default=10) + 1
            self.fds[fd] = (node, path + "/<unnamed>", 0)
            self.events.append(("tmpfile", path))
            return fd
        if flags & os.O_CREAT:
            if path in self.nodes:
                raise FileExistsError(errno.EEXIST, "exists")
            self.add(path, stat.S_IFREG | mode)
            self.nodes[posixpath.dirname(path)].mtime += 1
            self.events.append(("create", path))
        if path not in self.nodes:
            raise FileNotFoundError(errno.ENOENT, "absent")
        node = self.nodes[path]
        if stat.S_ISLNK(node.mode):
            raise OSError(errno.ELOOP, "symlink")
        fd = max(self.fds, default=10) + 1
        self.fds[fd] = (node, path, 0)
        return fd

    def close(self, fd: int) -> None:
        del self.fds[fd]

    def info(self, node: Node) -> Any:
        return SimpleNamespace(st_dev=node.dev, st_ino=node.ino, st_mode=node.mode,
                               st_uid=node.uid, st_gid=node.gid, st_nlink=node.nlink,
                               st_size=len(node.data), st_mtime_ns=node.mtime, st_ctime_ns=1)

    def fstat(self, fd: int) -> Any:
        if self.fstat_hook:
            self.fstat_hook(self.fds[fd][0], self.fds[fd][1])
        return self.info(self.fds[fd][0])

    def stat(self, name: str, *, dir_fd: int | None = None,
             follow_symlinks: bool = True) -> Any:
        path = self.path(name, dir_fd)
        if path not in self.nodes:
            raise FileNotFoundError(errno.ENOENT, "absent")
        return self.info(self.nodes[path])

    def read(self, fd: int, count: int) -> bytes:
        node, path, offset = self.fds[fd]
        result = node.data[offset:offset + count]
        self.fds[fd] = (node, path, offset + len(result))
        return result

    def write(self, fd: int, data: Any) -> int:
        if self.fail_write:
            raise OSError(errno.ENOSPC, "full")
        node, path, offset = self.fds[fd]
        chunk = bytes(data[:7])  # Exercise short writes.
        node.data = node.data[:offset] + chunk + node.data[offset + len(chunk):]
        node.mtime += 1
        self.fds[fd] = (node, path, offset + len(chunk))
        return len(chunk)

    def lseek(self, fd: int, offset: int, whence: int) -> int:
        node, path, _ = self.fds[fd]
        self.fds[fd] = (node, path, offset)
        return offset

    def fchmod(self, fd: int, mode: int) -> None:
        node, path, _ = self.fds[fd]
        node.mode = stat.S_IFMT(node.mode) | mode
        self.events.append(("chmod", path))

    def fsync(self, fd: int) -> None:
        node, path, _ = self.fds[fd]
        self.events.append(("fsync", path))
        if self.fsync_hook:
            self.fsync_hook(node, path)
        if path == str(CACHE) and self.fail_cache_fsync:
            raise OSError(errno.EIO, "uncertain durability")

    def unlink(self, name: str, *, dir_fd: int) -> None:
        if self.fail_unlink:
            raise OSError(errno.EIO, "uncertain unlink")
        path = self.path(name, dir_fd)
        self.events.append(("unlink", path))
        del self.nodes[path]

    def geteuid(self) -> int:
        return 0

    def getegid(self) -> int:
        return 0

    def rename(self, source: int, name: str, target: int, filename: str) -> None:
        source_path, target_path = self.path(name, source), self.path(filename, target)
        self.events.append(("rename", target_path))
        if self.rename_hook:
            self.rename_hook(source_path, target_path)
        if self.rename_error:
            raise OSError(self.rename_error, "rename denied")
        if target_path in self.nodes:
            raise FileExistsError(errno.EEXIST, "no clobber")
        self.nodes[target_path] = self.nodes.pop(source_path)
        if self.after_rename_error:
            raise OSError(errno.EIO, "result unknown")

    def link_unnamed(self, descriptor: int, target: int, filename: str) -> None:
        node = self.fds[descriptor][0]
        path = self.path(filename, target)
        self.events.append(("link", path))
        assert node.nlink == 0 and stat.S_IMODE(node.mode) == 0o444
        assert node.data == RAW and not any(value is node for value in self.nodes.values())
        if self.link_hook:
            self.link_hook(node, path)
        if self.link_error:
            raise OSError(self.link_error, "link rejected")
        if path in self.nodes:
            raise FileExistsError(errno.EEXIST, "no clobber")
        self.nodes[path] = node
        node.nlink = 1
        if self.after_link_error:
            raise OSError(errno.EIO, "link outcome unknown")


@pytest.fixture
def fs(api: Any, monkeypatch: pytest.MonkeyPatch) -> FileSystem:
    result = FileSystem()
    monkeypatch.setattr(api, "os", result)
    monkeypatch.setattr(api, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(api, "_rename_noreplace", result.rename)
    return result


def publish(api: Any, entry: Any = None, **kwargs: Any) -> str:
    return cast(str, api.publish_verified_wheel(CACHE, STAGING, entry or Entry(), RAW,
                                               deadline=kwargs.pop("deadline", 200.0), **kwargs))


@pytest.fixture
def exdev(api: Any, fs: FileSystem, monkeypatch: pytest.MonkeyPatch) -> FileSystem:
    fs.rename_error = errno.EXDEV
    fs.tmpfile_supported = True
    monkeypatch.setattr(api, "_link_unnamed", fs.link_unnamed, raising=False)
    return fs


def test_exdev_only_uses_anonymous_cache_fd_and_atomic_final_link(api: Any, exdev: FileSystem) -> None:
    assert publish(api) == "published"
    final = exdev.nodes[str(CACHE) + "/" + FILENAME]
    assert final is exdev.unnamed[0] and final.nlink == 1 and final.data == RAW
    assert stat.S_IMODE(final.mode) == 0o444 and final.uid == final.gid == 0
    assert exdev.events.count(("tmpfile", str(CACHE))) == 1
    assert exdev.events.count(("link", str(CACHE) + "/" + FILENAME)) == 1
    assert not any(kind == "create" and path.startswith(str(CACHE) + "/")
                   for kind, path in exdev.events)
    assert not any(path.startswith(str(STAGING) + "/") for path in exdev.nodes)
    assert exdev.events.index(("rename", str(CACHE) + "/" + FILENAME)) < exdev.events.index(("tmpfile", str(CACHE)))
    assert not exdev.fds
    assert publish(api) == "reused" and len(exdev.unnamed) == 1


@pytest.mark.parametrize("error", [None, errno.ENOSYS, errno.EACCES, errno.ENOSPC, errno.EIO])
def test_non_exdev_never_tries_anonymous_fallback(api: Any, exdev: FileSystem, error: int | None) -> None:
    exdev.rename_error = error
    if error is None:
        assert publish(api) == "published"
    else:
        expected = ProcessCleanupIncomplete if error == errno.EIO else api.PythonInputPublishError
        with pytest.raises(expected):
            publish(api)
    assert not exdev.unnamed and not any(kind == "link" for kind, _ in exdev.events)


@pytest.mark.parametrize("error", [errno.EOPNOTSUPP, errno.ENOSYS, errno.EACCES, errno.ENOSPC])
def test_exdev_with_unavailable_tmpfile_never_creates_named_cache_temp(
    api: Any, exdev: FileSystem, error: int,
) -> None:
    exdev.tmpfile_error = error
    with pytest.raises(api.PythonInputPublishError):
        publish(api)
    assert not exdev.unnamed and not exdev.fds
    assert not any(path.startswith(str(CACHE) + "/") for path in exdev.nodes)
    assert not any(".wheel-publish-" in path for path in exdev.nodes)


@pytest.mark.parametrize("same", [False, True])
def test_exdev_link_eexist_measures_existing_bytes_and_never_clobbers(
    api: Any, exdev: FileSystem, same: bool,
) -> None:
    def arrive(node: Node, path: str) -> None:
        exdev.add(path, stat.S_IFREG | 0o444, RAW if same else b"other wheel")
    exdev.link_hook = arrive
    if same:
        assert publish(api) == "reused"
    else:
        with pytest.raises(api.PythonInputPublishError):
            publish(api)
    assert exdev.nodes[str(CACHE) + "/" + FILENAME].data == (RAW if same else b"other wheel")
    assert exdev.unnamed[0].nlink == 0 and not exdev.fds
    assert not any(".wheel-publish-" in path for path in exdev.nodes)


@pytest.mark.parametrize("field,value", [("mode", stat.S_IFREG | 0o644), ("nlink", 2),
    ("uid", 1000), ("mode", stat.S_IFLNK | 0o777)])
def test_exdev_link_eexist_does_not_reuse_unsafe_target(api: Any, exdev: FileSystem,
                                                     field: str, value: int) -> None:
    def arrive(node: Node, path: str) -> None:
        existing = exdev.add(path, stat.S_IFREG | 0o444, RAW)
        setattr(existing, field, value)
    exdev.link_hook = arrive
    with pytest.raises(api.PythonInputPublishError):
        publish(api)
    assert getattr(exdev.nodes[str(CACHE) + "/" + FILENAME], field) == value
    assert exdev.unnamed[0].nlink == 0 and not exdev.fds


@pytest.mark.parametrize("after", [False, True])
def test_exdev_unknown_link_result_preserves_staged_evidence_and_any_final(
    api: Any, exdev: FileSystem, after: bool,
) -> None:
    exdev.link_error = None if after else errno.EIO
    exdev.after_link_error = after
    with pytest.raises(ProcessCleanupIncomplete):
        publish(api)
    assert not any(kind == "unlink" for kind, _ in exdev.events)
    assert any(".wheel-publish-" in path and node.data == RAW for path, node in exdev.nodes.items())
    assert (str(CACHE) + "/" + FILENAME in exdev.nodes) is after
    assert not exdev.fds


@pytest.mark.parametrize("error", [errno.EPERM, errno.ENOSYS, errno.EXDEV, errno.ENOSPC])
def test_exdev_known_link_rejection_closes_unnamed_and_cleans_only_owned_stage(
    api: Any, exdev: FileSystem, error: int,
) -> None:
    exdev.link_error = error
    exdev.add(str(STAGING) + "/unrelated", stat.S_IFREG | 0o600, b"keep")
    with pytest.raises(api.PythonInputPublishError):
        publish(api)
    assert exdev.unnamed[0].nlink == 0 and not exdev.fds
    assert exdev.nodes[str(STAGING) + "/unrelated"].data == b"keep"
    assert not any(".wheel-publish-" in path for path in exdev.nodes)
    assert str(CACHE) + "/" + FILENAME not in exdev.nodes


def test_exdev_post_link_fsync_uncertainty_keeps_final_and_stage(api: Any, exdev: FileSystem) -> None:
    exdev.fail_cache_fsync = True
    with pytest.raises(ProcessCleanupIncomplete):
        publish(api)
    assert exdev.nodes[str(CACHE) + "/" + FILENAME].data == RAW
    assert any(".wheel-publish-" in path for path in exdev.nodes)
    assert not any(kind == "unlink" for kind, _ in exdev.events)


def test_exdev_post_link_audit_failure_keeps_final_and_stage(api: Any, exdev: FileSystem) -> None:
    def change_root(node: Node, path: str) -> None:
        exdev.nodes[str(STAGING)].mode = stat.S_IFDIR | 0o755
    exdev.link_hook = change_root
    with pytest.raises(ProcessCleanupIncomplete):
        publish(api)
    assert exdev.nodes[str(CACHE) + "/" + FILENAME].data == RAW
    assert any(".wheel-publish-" in path for path in exdev.nodes)


def test_exdev_copy_is_remeasured_before_any_link(api: Any, exdev: FileSystem) -> None:
    def corrupt(node: Node, path: str) -> None:
        if "<unnamed>" in path and stat.S_IMODE(node.mode) == 0o444:
            node.data = b"x" * len(RAW)
    exdev.fsync_hook = corrupt
    with pytest.raises(api.PythonInputPublishError):
        publish(api)
    assert not any(kind == "link" for kind, _ in exdev.events)
    assert not any(".wheel-publish-" in path for path in exdev.nodes)


def test_exdev_actual_copy_passes_static_validator_again(
    api: Any, exdev: FileSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = []
    def validate(entry: Any, data: bytes) -> None:
        checked.append(data)
        if len(checked) == 2:
            raise ValueError("private metadata invalid on revalidation")
    monkeypatch.setattr(sys.modules["agent_hub.skills.sandbox.python_input_fetch"],
                        "validate_wheel_bytes", validate)
    with pytest.raises(api.PythonInputPublishError, match="invalid_wheel"):
        publish(api)
    assert checked == [RAW, RAW] and not any(kind == "link" for kind, _ in exdev.events)


def test_exdev_successful_link_then_stage_cleanup_failure_is_uncertain(api: Any, exdev: FileSystem) -> None:
    exdev.fail_unlink = True
    with pytest.raises(ProcessCleanupIncomplete):
        publish(api)
    assert exdev.nodes[str(CACHE) + "/" + FILENAME].data == RAW
    assert any(".wheel-publish-" in path for path in exdev.nodes)


def test_linkat_uses_proc_fd_follow_not_empty_path_or_new_capability(
    api: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    def link(*args: Any) -> int:
        calls.append(args)
        return 0
    monkeypatch.setattr(api.ctypes, "CDLL", lambda *a, **k: SimpleNamespace(linkat=link))
    implementation = getattr(api, "_link_unnamed", None)
    assert callable(implementation), "proc-fd atomic publisher is missing"
    implementation(42, 20, FILENAME)
    assert calls == [(-100, b"/proc/self/fd/42", 20, FILENAME.encode(), 1024)]


@pytest.mark.parametrize("when", ["copy", "link", "post_link"])
def test_exdev_cancellation_is_known_only_before_link_starts(
    api: Any, exdev: FileSystem, monkeypatch: pytest.MonkeyPatch, when: str,
) -> None:
    def copy_cancel(node: Node, path: str) -> None:
        if "<unnamed>" in path:
            raise asyncio.CancelledError()
    def link_cancel(node: Node, path: str) -> None:
        raise asyncio.CancelledError()
    if when == "copy":
        exdev.fsync_hook = copy_cancel
    elif when == "link":
        exdev.link_hook = link_cancel
    else:
        def post_cancel(node: Node, path: str) -> None:
            if path == str(CACHE):
                raise asyncio.CancelledError()
        exdev.fsync_hook = post_cancel
    expected = asyncio.CancelledError if when == "copy" else ProcessCleanupIncomplete
    with pytest.raises(expected):
        publish(api)
    staged = any(".wheel-publish-" in path for path in exdev.nodes)
    assert staged is (when != "copy") and not exdev.fds
    assert (str(CACHE) + "/" + FILENAME in exdev.nodes) is (when == "post_link")


@pytest.mark.parametrize("mutation", ["hash", "mode", "hardlink"])
def test_exdev_final_integrity_failure_never_deletes_published_evidence(
    api: Any, exdev: FileSystem, mutation: str,
) -> None:
    def corrupt(node: Node, path: str) -> None:
        if path != str(CACHE):
            return
        final = exdev.nodes[str(CACHE) + "/" + FILENAME]
        if mutation == "hash":
            final.data = b"x" * len(RAW)
        elif mutation == "mode":
            final.mode = stat.S_IFREG | 0o644
        else:
            final.nlink = 2
    exdev.fsync_hook = corrupt
    with pytest.raises(ProcessCleanupIncomplete):
        publish(api)
    assert str(CACHE) + "/" + FILENAME in exdev.nodes
    assert any(".wheel-publish-" in path for path in exdev.nodes)
    assert not any(kind == "unlink" for kind, _ in exdev.events)


def test_publishes_readonly_bytes_outside_flat_cache_then_reuses(api: Any, fs: FileSystem) -> None:
    assert publish(api) == "published"
    final = fs.nodes[str(CACHE) + "/" + FILENAME]
    assert final.data == RAW and stat.S_IMODE(final.mode) == 0o444
    assert final.uid == final.gid == 0 and final.nlink == 1
    assert next(path for kind, path in fs.events if kind == "create").startswith(str(STAGING) + "/")
    assert not any(path.startswith(str(STAGING) + "/") for path in fs.nodes)
    assert ("fsync", str(CACHE)) in fs.events and ("fsync", str(STAGING)) in fs.events
    assert publish(api) == "reused"
    assert sum(kind == "rename" for kind, _ in fs.events) == 1
    assert not fs.fds


@pytest.mark.parametrize("mutation", ["bytes", "size", "sha", "filename", "boolsize"])
def test_invalid_input_never_allocates(api: Any, fs: FileSystem, mutation: str) -> None:
    changes: dict[str, Any] = {"bytes": {"sha256": "0" * 64}, "size": {"size_bytes": 1},
                               "sha": {"sha256": "bad"}, "filename": {"filename": "../evil.whl"},
                               "boolsize": {"size_bytes": True}}
    with pytest.raises(api.PythonInputPublishError):
        publish(api, replace(Entry(), **changes[mutation]))
    assert not fs.events


def test_static_validator_failure_is_finite_and_allocates_nothing(api: Any, fs: FileSystem,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(*args: Any) -> None:
        raise ValueError("untrusted wheel private contents")
    monkeypatch.setattr(sys.modules["agent_hub.skills.sandbox.python_input_fetch"],
                        "validate_wheel_bytes", reject)
    with pytest.raises(api.PythonInputPublishError) as caught:
        publish(api)
    assert "private" not in str(caught.value) and not fs.events


@pytest.mark.parametrize("deadline", [99.0, float("nan"), float("inf"), True])
def test_invalid_or_expired_deadline_never_allocates(api: Any, fs: FileSystem,
                                                   deadline: float) -> None:
    with pytest.raises(api.PythonInputPublishError):
        publish(api, deadline=deadline)
    assert not fs.events


@pytest.mark.parametrize("field,value", [("mode", stat.S_IFREG | 0o644), ("nlink", 2),
    ("uid", 1000), ("gid", 1000), ("data", b"different"), ("dev", 2),
    ("mode", stat.S_IFLNK | 0o777), ("mode", stat.S_IFIFO | 0o444)])
def test_existing_unsafe_or_conflicting_entry_is_never_changed(api: Any, fs: FileSystem,
                                                             field: str, value: Any) -> None:
    path = str(CACHE) + "/" + FILENAME
    node = fs.add(path, stat.S_IFREG | 0o444, RAW)
    setattr(node, field, value)
    before = replace(node)
    with pytest.raises(api.PythonInputPublishError):
        publish(api)
    assert node == before and fs.nodes[path] is node and not fs.events


@pytest.mark.parametrize("path,field,value", [
    (str(CACHE), "mode", stat.S_IFDIR | 0o755),
    (str(STAGING), "mode", stat.S_IFDIR | 0o750),
    ("/private", "mode", stat.S_IFDIR | 0o1777),
    ("/private", "uid", 1000), (str(STAGING), "dev", 2),
    (str(STAGING), "ino", 102), (str(STAGING), "mode", stat.S_IFLNK | 0o700),
])
def test_unsafe_roots_or_ancestor_or_alias_fail_before_temp(api: Any, fs: FileSystem,
                                                         path: str, field: str, value: int) -> None:
    setattr(fs.nodes[path], field, value)
    with pytest.raises(api.PythonInputPublishError):
        publish(api)
    assert not fs.events and not fs.fds


@pytest.mark.parametrize("error", [errno.EXDEV, errno.ENOSYS, errno.EACCES, errno.ENOSPC])
def test_known_rename_failure_removes_only_own_temp(api: Any, fs: FileSystem, error: int) -> None:
    fs.rename_error = error
    fs.add(str(STAGING) + "/unrelated", stat.S_IFREG | 0o600, b"keep")
    with pytest.raises(api.PythonInputPublishError):
        publish(api)
    assert fs.nodes[str(STAGING) + "/unrelated"].data == b"keep"
    assert not any(".wheel-publish-" in path for path in fs.nodes)
    assert str(CACHE) + "/" + FILENAME not in fs.nodes


@pytest.mark.parametrize("after", [False, True])
def test_rename_result_uncertain_preserves_evidence(api: Any, fs: FileSystem, after: bool) -> None:
    fs.after_rename_error = after
    fs.rename_error = None if after else errno.EIO
    with pytest.raises(ProcessCleanupIncomplete):
        publish(api)
    assert not any(kind == "unlink" for kind, _ in fs.events)
    assert any(node.data == RAW for node in fs.nodes.values())


def test_post_publish_fsync_failure_never_removes_final(api: Any, fs: FileSystem) -> None:
    fs.fail_cache_fsync = True
    with pytest.raises(ProcessCleanupIncomplete):
        publish(api)
    assert fs.nodes[str(CACHE) + "/" + FILENAME].data == RAW
    assert not any(kind == "unlink" for kind, _ in fs.events)


def test_failed_temp_cleanup_is_uncertain(api: Any, fs: FileSystem) -> None:
    fs.fail_write = fs.fail_unlink = True
    with pytest.raises(ProcessCleanupIncomplete):
        publish(api)
    assert any(".wheel-publish-" in path for path in fs.nodes)


def test_allocation_followed_by_unmeasurable_fd_is_uncertain(api: Any, fs: FileSystem) -> None:
    def fail(node: Node, path: str) -> None:
        if ".wheel-publish-" in path:
            raise OSError(errno.EIO, "unmeasurable allocation")
    fs.fstat_hook = fail
    with pytest.raises(ProcessCleanupIncomplete):
        publish(api)
    assert any(".wheel-publish-" in path for path in fs.nodes)


def test_cleanup_directory_fsync_failure_is_uncertain(api: Any, fs: FileSystem) -> None:
    fs.fail_write = True
    def fail(node: Node, path: str) -> None:
        if path == str(STAGING):
            raise OSError(errno.EIO, "unlink durability unknown")
    fs.fsync_hook = fail
    with pytest.raises(ProcessCleanupIncomplete):
        publish(api)
    assert any(kind == "unlink" for kind, _ in fs.events)
    assert str(CACHE) + "/" + FILENAME not in fs.nodes


def test_confirmed_prepublication_cancellation_cleans_temp_and_propagates(
    api: Any, fs: FileSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cancel(*args: Any) -> None:
        raise asyncio.CancelledError()
    monkeypatch.setattr(fs, "write", cancel)
    with pytest.raises(asyncio.CancelledError):
        publish(api)
    assert not any(".wheel-publish-" in path for path in fs.nodes)


def test_postpublication_cancellation_is_uncertain(api: Any, fs: FileSystem) -> None:
    def cancel(node: Node, path: str) -> None:
        if path == str(CACHE):
            raise asyncio.CancelledError()
    fs.fsync_hook = cancel
    with pytest.raises(ProcessCleanupIncomplete):
        publish(api)
    assert fs.nodes[str(CACHE) + "/" + FILENAME].data == RAW


def test_expiry_during_write_cleans_without_needing_fresh_deadline(
    api: Any, fs: FileSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = fs.write
    def expire(fd: int, raw: Any) -> int:
        result = original(fd, raw)
        monkeypatch.setattr(api.time, "monotonic", lambda: 300.0)
        return result
    monkeypatch.setattr(fs, "write", expire)
    with pytest.raises(api.PythonInputPublishError, match="deadline"):
        publish(api)
    assert not any(".wheel-publish-" in path for path in fs.nodes)


def test_root_linux_guard_cannot_be_bypassed_on_other_platform(
    api: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "sys", SimpleNamespace(platform="win32"))
    with pytest.raises(api.PythonInputPublishError, match="root_linux_required"):
        publish(api)


def test_nonroot_guard_rejects_before_validation(
    api: Any, fs: FileSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fs, "geteuid", lambda: 1000)
    with pytest.raises(api.PythonInputPublishError, match="root_linux_required"):
        publish(api)
    assert not fs.events


def test_existing_verified_file_changed_before_return_is_rejected(
    api: Any, fs: FileSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = str(CACHE) + "/" + FILENAME
    node = fs.add(path, stat.S_IFREG | 0o444, RAW)
    original = fs.close
    def close(fd: int) -> None:
        if fs.fds[fd][1] == path:
            node.mode = stat.S_IFREG | 0o644
        original(fd)
    monkeypatch.setattr(fs, "close", close)
    with pytest.raises(api.PythonInputPublishError):
        publish(api)
    assert not any(kind == "create" for kind, _ in fs.events)


@pytest.mark.parametrize("parts", ["relative/cache", "//private/cache", "/private/../cache",
                                  "/private/cache\\alias"])
def test_path_aliases_are_rejected(api: Any, parts: str) -> None:
    with pytest.raises(api.PythonInputPublishError):
        api._path_parts(PurePosixPath(parts))


def test_cache_and_staging_cannot_be_same_or_nested(api: Any, fs: FileSystem) -> None:
    nested = str(CACHE) + "/stage"
    fs.add(nested, stat.S_IFDIR | 0o700)
    for stage in (CACHE, PurePosixPath(nested), PurePosixPath("/private")):
        with pytest.raises(api.PythonInputPublishError):
            api.publish_verified_wheel(CACHE, stage, Entry(), RAW, deadline=200.0)
    assert not fs.events


def test_exclusive_temp_collision_does_not_delete_other_publishers_file(
    api: Any, fs: FileSystem, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api.secrets, "token_hex", lambda count: "a" * 32)
    path = str(STAGING) + "/.wheel-publish-" + "a" * 32
    fs.add(path, stat.S_IFREG | 0o600, b"prior attempt evidence")
    with pytest.raises(api.PythonInputPublishError):
        publish(api)
    assert fs.nodes[path].data == b"prior attempt evidence" and not fs.events


def test_exact_renameat2_flag_and_enosys_without_fallback(api: Any,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    def rename(*args: Any) -> int:
        calls.append(args)
        return 0
    monkeypatch.setattr(api.ctypes, "CDLL", lambda *a, **k: SimpleNamespace(renameat2=rename))
    api._rename_noreplace(10, "temporary", 20, FILENAME)
    assert calls == [(10, b"temporary", 20, FILENAME.encode(), 1)]
    monkeypatch.setattr(api.ctypes, "CDLL", lambda *a, **k: SimpleNamespace())
    with pytest.raises(OSError) as caught:
        api._rename_noreplace(10, "temporary", 20, FILENAME)
    assert caught.value.errno == errno.ENOSYS


def real_wheel() -> tuple[Any, bytes]:
    fetch = importlib.import_module("agent_hub.skills.sandbox.python_input_fetch")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in (
            ("demo_pkg/__init__.py", b"raise AssertionError('must never execute')\n"),
            ("demo_pkg-1.0.dist-info/METADATA", b"Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.0\n\n"),
            ("demo_pkg-1.0.dist-info/WHEEL", b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"),
            ("demo_pkg-1.0.dist-info/RECORD", b""),
        ):
            archive.writestr(name, data)
    raw = output.getvalue()
    entry = fetch.WheelDownload(FILENAME, "demo-pkg", "1.0", hashlib.sha256(raw).hexdigest(),
                                len(raw), Entry().url)
    return entry, raw


def test_real_static_fetch_validator_checks_wheel_without_executing_it() -> None:
    publisher = importlib.import_module("agent_hub.skills.sandbox.python_input_publish")
    fetch = importlib.import_module("agent_hub.skills.sandbox.python_input_fetch")
    entry, raw = real_wheel()
    publisher._validate(entry, raw, time.monotonic() + 10)
    invalid = b"not a zip, but matching catalog bytes/hash"
    bad = fetch.WheelDownload(FILENAME, "demo-pkg", "1.0", hashlib.sha256(invalid).hexdigest(),
                              len(invalid), Entry().url)
    with pytest.raises(publisher.PythonInputPublishError, match="invalid_wheel"):
        publisher._validate(bad, invalid, time.monotonic() + 10)


@pytest.mark.parametrize("same", [False, True])
def test_eexist_race_is_measured_not_overwritten(api: Any, fs: FileSystem, same: bool) -> None:
    def arrive(source: str, target: str) -> None:
        fs.add(target, stat.S_IFREG | 0o444, RAW if same else b"other wheel")
    fs.rename_hook = arrive
    if same:
        assert publish(api) == "reused"
    else:
        with pytest.raises(api.PythonInputPublishError):
            publish(api)
    assert fs.nodes[str(CACHE) + "/" + FILENAME].data == (RAW if same else b"other wheel")
    assert not any(".wheel-publish-" in path for path in fs.nodes)


@pytest.mark.parametrize("after_publish", [False, True])
def test_root_identity_change_detected_without_deleting_replacement(api: Any, fs: FileSystem,
                                                                 after_publish: bool) -> None:
    def mutate(node: Node, path: str) -> None:
        if (path == str(CACHE)) == after_publish and (after_publish or ".wheel-publish-" in path):
            fs.nodes[str(CACHE)] = replace(fs.nodes[str(CACHE)], ino=999)
    fs.fsync_hook = mutate
    error = ProcessCleanupIncomplete if after_publish else api.PythonInputPublishError
    with pytest.raises(error):
        publish(api)


@pytest.mark.skipif(sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0,
                    reason="requires actual Linux root FD/renameat2/permissions; not emulated")
def test_native_root_publish_reuse_and_real_links() -> None:
    api = importlib.import_module("agent_hub.skills.sandbox.python_input_publish")
    entry, raw = real_wheel()
    root = Path(tempfile.mkdtemp(prefix="wheel-publisher-test-", dir="/root"))
    try:
        cache, staging = root / "cache", root / "stage"
        cache.mkdir(mode=0o700)
        staging.mkdir(mode=0o700)
        assert api.publish_verified_wheel(cache, staging, entry, raw,
                                          deadline=time.monotonic() + 10) == "published"
        assert api.publish_verified_wheel(cache, staging, entry, raw,
                                          deadline=time.monotonic() + 10) == "reused"
        target = cache / FILENAME
        assert stat.S_IMODE(target.stat().st_mode) == 0o444 and target.read_bytes() == raw
        os.link(target, root / "alias")
        with pytest.raises(api.PythonInputPublishError):
            api.publish_verified_wheel(cache, staging, entry, raw, deadline=time.monotonic() + 10)
        (root / "alias").unlink()
        target.unlink()
        target.symlink_to(root / "external")
        with pytest.raises(api.PythonInputPublishError):
            api.publish_verified_wheel(cache, staging, entry, raw, deadline=time.monotonic() + 10)
        assert not list(staging.iterdir())
    finally:
        shutil.rmtree(root)
