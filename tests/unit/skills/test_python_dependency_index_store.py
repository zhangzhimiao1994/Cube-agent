"""Portable values and adversarial FD simulation, not Linux native IO evidence."""

from __future__ import annotations

import errno
import importlib
import importlib.util
import io
import os
import posixpath
import stat
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from agent_hub.skills.sandbox.python_dependency_metadata import (
    PythonDependencyMetadataIndex,
    extract_python_wheel_metadata,
)
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_project_backend import RootConfig
from tests.unit.skills.test_python_dependency_metadata import wheel

MODULE = "agent_hub.skills.sandbox.python_dependency_index_store"
STATE = "/private/state"
WHEELS = "/private/wheels"


def test_index_store_api_exists() -> None:
    assert importlib.util.find_spec(MODULE) is not None, "Root metadata index store is missing"


@pytest.fixture
def api() -> Any:
    return importlib.import_module(MODULE)


def supply_configuration() -> RootSupplyConfig:
    return RootSupplyConfig(Path(STATE), (wheel()[0],), 64 * 1024**2, 0, 60)


def root_configuration() -> RootConfig:
    return RootConfig.from_payload({
        "schema_version": 1, "runtime_root": "/private/runtime", "wheel_root": WHEELS,
        "environment_root": "/private/environments", "stage_root": "/private/stage",
        "interpreter": {"implementation": "cpython", "version": "3.12.4", "abi": "cp312",
            "platform": "linux_x86_64", "binary_sha256": "a" * 64,
            "base_runtime_sha256": "b" * 64},
        "cache_policy": {"max_bytes": 2**30, "max_entries": 8,
            "disk_reserve_bytes": 0, "retention_seconds": 60},
        "runtime_max_bytes": 2**28, "wheel_max_bytes": 2**28,
        "max_environment_bytes": 2**28,
    })


def metadata_index(supply: RootSupplyConfig) -> PythonDependencyMetadataIndex:
    entry, raw = wheel()
    return PythonDependencyMetadataIndex(supply.digest, (extract_python_wheel_metadata(entry, raw),))


def final_path(supply: RootSupplyConfig) -> str:
    return STATE + "/dependency-metadata-" + supply.digest + ".json"


@dataclass
class Node:
    inode: int
    mode: int
    data: bytes = b""
    uid: int = 0
    gid: int = 0
    nlink: int = 1
    dev: int = 1
    mtime: int = 1
    ctime: int = 1
    size_override: int | None = None


class FileSystem:
    """Model paths, held inodes, short writes, flock and publication fault points."""

    O_DIRECTORY = 1 << 20
    O_NOFOLLOW = 1 << 21
    O_CLOEXEC = 1 << 22
    O_NONBLOCK = 1 << 23
    O_RDONLY = os.O_RDONLY
    O_RDWR = os.O_RDWR
    O_CREAT = os.O_CREAT
    O_EXCL = os.O_EXCL
    SEEK_SET = os.SEEK_SET
    name = "posix"

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.fds: dict[int, tuple[Node, str, int]] = {}
        self.events: list[tuple[str, str]] = []
        self.clock = 100.0
        self.locks: dict[int, int] = {}
        self.block_lock = False
        self.after_read: Any = None
        self.after_fsync: Any = None
        self.before_link: Any = None
        self.after_link: Any = None
        self.fail_link: int | None = None
        self.fail_write = False
        self.fail_unlink = False
        self.create_collision = False
        self.next_inode = 100
        for path in ("/", "/private", STATE, WHEELS):
            self.add(path, stat.S_IFDIR | 0o700)
        self.supports_dir_fd = {self.open, self.stat, self.link, self.unlink}
        self.supports_follow_symlinks = {self.stat, self.link}

    def add(self, path: str, mode: int, data: bytes = b"", **changes: Any) -> Node:
        self.next_inode += 1
        node = Node(self.next_inode, mode, data, **changes)
        self.nodes[path] = node
        return node

    def path(self, name: str, dir_fd: int | None) -> str:
        if name.startswith("/"):
            return posixpath.normpath(name)
        assert dir_fd is not None
        return posixpath.normpath(posixpath.join(self.fds[dir_fd][1], name))

    def open(self, name: str, flags: int, mode: int = 0o777,
             *, dir_fd: int | None = None) -> int:
        path = self.path(name, dir_fd)
        self.events.append(("open", path))
        assert flags & self.O_NOFOLLOW and flags & self.O_CLOEXEC
        if flags & self.O_CREAT:
            assert flags & self.O_EXCL and mode == 0o600
            if self.create_collision:
                self.add(path, stat.S_IFREG | 0o600, b"foreign temp")
            if path in self.nodes:
                raise FileExistsError(errno.EEXIST, "exists")
            self.add(path, stat.S_IFREG | mode)
            self.events.append(("create", path))
        if path not in self.nodes:
            raise FileNotFoundError(errno.ENOENT, "absent")
        node = self.nodes[path]
        if stat.S_ISLNK(node.mode):
            raise OSError(errno.ELOOP, "symlink")
        if flags & self.O_DIRECTORY and not stat.S_ISDIR(node.mode):
            raise NotADirectoryError(errno.ENOTDIR, "not a directory")
        fd = max(self.fds, default=10) + 1
        self.fds[fd] = (node, path, 0)
        return fd

    def close(self, fd: int) -> None:
        self.locks.pop(fd, None)
        del self.fds[fd]

    def info(self, node: Node) -> Any:
        return SimpleNamespace(st_dev=node.dev, st_ino=node.inode, st_mode=node.mode,
            st_uid=node.uid, st_gid=node.gid, st_nlink=node.nlink,
            st_size=len(node.data) if node.size_override is None else node.size_override,
            st_mtime_ns=node.mtime, st_ctime_ns=node.ctime)

    def fstat(self, fd: int) -> Any:
        return self.info(self.fds[fd][0])

    def stat(self, name: str, *, dir_fd: int | None = None,
             follow_symlinks: bool = True) -> Any:
        assert not follow_symlinks
        path = self.path(name, dir_fd)
        if path not in self.nodes:
            raise FileNotFoundError(errno.ENOENT, "absent")
        return self.info(self.nodes[path])

    def read(self, fd: int, count: int) -> bytes:
        node, path, offset = self.fds[fd]
        assert count > 0
        self.events.append(("read", path))
        body = node.data[offset:offset + min(count, 137)]
        self.fds[fd] = (node, path, offset + len(body))
        if self.after_read:
            self.after_read(node, path)
        return body

    def write(self, fd: int, data: Any) -> int:
        if self.fail_write:
            raise OSError(errno.ENOSPC, "disk full")
        node, path, offset = self.fds[fd]
        part = bytes(data[:37])
        node.data = node.data[:offset] + part + node.data[offset + len(part):]
        node.mtime += 1
        node.ctime += 1
        self.fds[fd] = (node, path, offset + len(part))
        self.events.append(("write", path))
        return len(part)

    def lseek(self, fd: int, offset: int, whence: int) -> int:
        assert whence == self.SEEK_SET
        node, path, _ = self.fds[fd]
        self.fds[fd] = (node, path, offset)
        return offset

    def fchmod(self, fd: int, mode: int) -> None:
        node, _, _ = self.fds[fd]
        node.mode = stat.S_IFMT(node.mode) | mode
        node.ctime += 1

    def fsync(self, fd: int) -> None:
        node, path, _ = self.fds[fd]
        self.events.append(("fsync", path))
        if self.after_fsync:
            self.after_fsync(node, path)

    def link(self, source: str, target: str, *, src_dir_fd: int,
             dst_dir_fd: int, follow_symlinks: bool = True) -> None:
        assert not follow_symlinks
        assert self.locks.get(dst_dir_fd) == 2, "publish must hold state LOCK_EX"
        source_path, target_path = self.path(source, src_dir_fd), self.path(target, dst_dir_fd)
        self.events.append(("link", target_path))
        if self.before_link:
            self.before_link(source_path, target_path)
        if self.fail_link:
            raise OSError(self.fail_link, "link rejected")
        if target_path in self.nodes:
            raise FileExistsError(errno.EEXIST, "exists")
        node = self.nodes[source_path]
        self.nodes[target_path] = node
        node.nlink += 1
        node.ctime += 1
        if self.after_link:
            self.after_link(source_path, target_path)

    def unlink(self, name: str, *, dir_fd: int) -> None:
        path = self.path(name, dir_fd)
        assert path in self.nodes
        assert "/.dependency-metadata-" in path, "never remove final or a wheel"
        assert self.locks.get(dir_fd) == 2
        if self.fail_unlink:
            raise OSError(errno.EIO, "unlink unknown")
        self.events.append(("unlink", path))
        node = self.nodes.pop(path)
        node.nlink -= 1
        node.ctime += 1

    def geteuid(self) -> int:
        return 0

    def getegid(self) -> int:
        return 0

    def flock(self, fd: int, flags: int) -> None:
        assert self.fds[fd][1] == STATE
        assert flags in {1 | 4, 2 | 4}
        self.events.append(("lock", str(flags & 3)))
        if self.block_lock:
            raise BlockingIOError(errno.EWOULDBLOCK, "locked")
        self.locks[fd] = flags & 3

    def sleep(self, duration: float) -> None:
        assert 0 < duration <= 0.05
        self.clock += duration


@pytest.fixture
def simulated(api: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    filesystem = FileSystem()
    catalog_module = importlib.import_module("agent_hub.skills.sandbox.python_input_supply_catalog")
    monkeypatch.setattr(api, "os", filesystem)
    monkeypatch.setattr(catalog_module, "os", filesystem)
    monkeypatch.setattr(api, "sys", SimpleNamespace(platform="linux", argv=[],
        stdout=io.StringIO(), stderr=io.StringIO()))
    clock = SimpleNamespace(monotonic=lambda: filesystem.clock, sleep=filesystem.sleep)
    # An absolute deadline and every consumer must share the same simulated clock.
    for module in (api, importlib.import_module("agent_hub.skills.sandbox.python_dependency_metadata"),
                   importlib.import_module("agent_hub.skills.sandbox.python_input_fetch")):
        monkeypatch.setattr(module, "time", clock)
    lock_module = ModuleType("fcntl")
    for name, constant_value in (("LOCK_SH", 1), ("LOCK_EX", 2), ("LOCK_NB", 4),
                        ("flock", filesystem.flock)):
        monkeypatch.setattr(lock_module, name, constant_value, raising=False)
    monkeypatch.setitem(sys.modules, "fcntl", lock_module)
    supply = supply_configuration()
    value = metadata_index(supply)
    yield SimpleNamespace(fs=filesystem, supply=supply, root=root_configuration(), value=value)
    assert filesystem.fds == {}, "all directory/file FDs must close even on failure"


def install_index(simulated: Any, raw: bytes | None = None) -> Node:
    filesystem: FileSystem = simulated.fs
    return filesystem.add(final_path(simulated.supply), stat.S_IFREG | 0o600,
                            simulated.value.to_bytes() if raw is None else raw)


def install_wheel(simulated: Any) -> Node:
    entry, raw = wheel()
    filesystem: FileSystem = simulated.fs
    return filesystem.add(WHEELS + "/" + entry.filename, stat.S_IFREG | 0o444, raw)


def assert_code(api: Any, code: str, operation: Any) -> None:
    with pytest.raises(api.PythonDependencyIndexError) as failure:
        operation()
    assert failure.value.code == code
    assert str(failure.value) == code


@pytest.mark.parametrize("action", ["read", "publish"])
def test_actual_windows_and_nonroot_profiles_fail_before_io(
    api: Any, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    monkeypatch.setattr(api, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(api, "os", SimpleNamespace(open=lambda *a, **k: pytest.fail("unsafe IO")))
    assert_code(api, "unsupported", lambda: api.read_dependency_metadata_index(supply_configuration())
        if action == "read" else api.publish_dependency_metadata_index(
            root_configuration(), supply_configuration()))


@pytest.mark.parametrize("case", ["uid", "gid", "dir_fd", "follow_symlinks", "nofollow"])
def test_native_capabilities_and_root_credentials_are_required_before_io(
    api: Any, simulated: Any, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    if case in {"uid", "gid"}:
        monkeypatch.setattr(simulated.fs, "geteuid" if case == "uid" else "getegid", lambda: 10001)
    elif case == "dir_fd":
        simulated.fs.supports_dir_fd.clear()
    elif case == "follow_symlinks":
        simulated.fs.supports_follow_symlinks.clear()
    else:
        monkeypatch.setattr(simulated.fs, "O_NOFOLLOW", 0)
    assert_code(api, "unsupported", lambda: api.read_dependency_metadata_index(simulated.supply))
    assert simulated.fs.events == []


def test_cold_read_verifies_fixed_file_without_accessing_wheel_root(api: Any, simulated: Any) -> None:
    install_index(simulated)
    result = api.read_dependency_metadata_index(simulated.supply)
    assert result == simulated.value
    assert ("lock", "1") in simulated.fs.events
    assert not any(path.startswith(WHEELS) for _, path in simulated.fs.events)
    assert not any(event in {"create", "write", "link", "unlink"}
                   for event, _ in simulated.fs.events)


def test_index_byte_limit_is_inclusive_and_never_truncates(api: Any, simulated: Any) -> None:
    raw = simulated.value.to_bytes()
    node = install_index(simulated, raw.ljust(8 * 1024 * 1024, b" "))
    assert api.read_dependency_metadata_index(simulated.supply) == simulated.value
    simulated.fs.events.clear()
    node.data += b" "
    assert_code(api, "metadata_untrusted", lambda: api.read_dependency_metadata_index(simulated.supply))
    assert not any(event == "read" for event, _ in simulated.fs.events)


@pytest.mark.parametrize("case", ["missing_file", "missing_state"])
def test_missing_metadata_has_finite_unavailable_code(api: Any, simulated: Any, case: str) -> None:
    if case == "missing_state":
        del simulated.fs.nodes[STATE]
    assert_code(api, "metadata_unavailable", lambda: api.read_dependency_metadata_index(simulated.supply))


@pytest.mark.parametrize("case", [
    "index_mode", "index_owner", "index_group", "index_hardlink", "index_symlink", "fifo",
    "state_mode", "state_owner", "ancestor_writable", "ancestor_symlink", "oversize",
    "zero", "truncated", "malformed", "duplicate", "nonfinite",
])
def test_reader_rejects_unsafe_or_malformed_profiles(api: Any, simulated: Any, case: str) -> None:
    node = install_index(simulated)
    if case == "index_mode":
        node.mode = stat.S_IFREG | 0o644
    elif case in {"index_owner", "index_group"}:
        setattr(node, "uid" if case.endswith("owner") else "gid", 10001)
    elif case == "index_hardlink":
        node.nlink = 2
    elif case in {"index_symlink", "fifo"}:
        node.mode = (stat.S_IFLNK if case == "index_symlink" else stat.S_IFIFO) | 0o600
    elif case.startswith("state_"):
        setattr(simulated.fs.nodes[STATE], "mode" if case.endswith("mode") else "uid",
                stat.S_IFDIR | 0o755 if case.endswith("mode") else 10001)
    elif case.startswith("ancestor_"):
        simulated.fs.nodes["/private"].mode = (stat.S_IFDIR | 0o722
            if case.endswith("writable") else stat.S_IFLNK | 0o777)
    elif case == "oversize":
        node.size_override = 8 * 1024 * 1024 + 1
    elif case == "zero":
        node.data = b""
    elif case == "truncated":
        node.size_override = len(node.data) + 1
    elif case == "malformed":
        node.data = b"{"
    elif case == "duplicate":
        node.data = node.data.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1')
    else:
        node.data = node.data.replace(b'"schema_version":1', b'"schema_version":NaN')
    assert_code(api, "metadata_untrusted", lambda: api.read_dependency_metadata_index(simulated.supply))


def test_nonregular_index_is_rejected_before_opening_it(api: Any, simulated: Any) -> None:
    install_index(simulated).mode = stat.S_IFCHR | 0o600
    assert_code(api, "metadata_untrusted", lambda: api.read_dependency_metadata_index(simulated.supply))
    assert ("open", final_path(simulated.supply)) not in simulated.fs.events


@pytest.mark.parametrize("case", ["digest", "missing", "extra", "hash"])
def test_full_catalog_match_is_required(api: Any, simulated: Any, case: str) -> None:
    value = simulated.value
    if case == "digest":
        value = replace(value, catalog_sha256="f" * 64)
    elif case == "missing":
        value = replace(value, records=())
    elif case == "extra":
        entry, raw = wheel(name="zzz")
        value = replace(value, records=(*value.records, extract_python_wheel_metadata(entry, raw)))
    else:
        item = value.records[0]
        value = replace(value, records=(replace(item, wheel=replace(item.wheel, sha256="f" * 64)),))
    install_index(simulated, value.to_bytes())
    assert_code(api, "catalog_changed", lambda: api.read_dependency_metadata_index(simulated.supply))


@pytest.mark.parametrize("case", ["file_path", "file_fd", "directory_path", "directory_mode", "catalog"])
def test_reader_rechecks_held_fd_and_directory_identity_after_read(
    api: Any, simulated: Any, case: str,
) -> None:
    install_index(simulated)
    fired = False

    def mutate(node: Node, path: str) -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        if case == "file_path":
            simulated.fs.add(path, node.mode, node.data)
        elif case == "file_fd":
            node.mtime += 1
        elif case == "directory_path":
            simulated.fs.add(STATE, stat.S_IFDIR | 0o700)
        elif case == "directory_mode":
            simulated.fs.nodes["/private"].mode = stat.S_IFDIR | 0o777
        else:
            object.__setattr__(simulated.supply, "min_free_bytes", 1)

    simulated.fs.after_read = mutate
    expected = "catalog_changed" if case == "catalog" else "metadata_untrusted"
    assert_code(api, expected, lambda: api.read_dependency_metadata_index(simulated.supply))


@pytest.mark.parametrize("action", ["read", "publish"])
def test_disappearing_open_index_is_untrusted_and_never_triggers_republication(
    api: Any, simulated: Any, action: str,
) -> None:
    install_index(simulated)
    install_wheel(simulated)

    def disappear(node: Node, path: str) -> None:
        if path == final_path(simulated.supply):
            moved = simulated.fs.nodes.pop(path, None)
            if moved is not None:
                simulated.fs.nodes[path + ".moved"] = moved

    simulated.fs.after_read = disappear
    assert_code(api, "metadata_untrusted", lambda: api.read_dependency_metadata_index(simulated.supply)
        if action == "read" else api.publish_dependency_metadata_index(simulated.root, simulated.supply))
    assert not any(event == "create" for event, _ in simulated.fs.events)


def test_reader_waits_for_publication_lock_before_inspecting_two_link_file(api: Any, simulated: Any) -> None:
    install_index(simulated).nlink = 2
    simulated.fs.block_lock = True
    assert_code(api, "timeout", lambda: api.read_dependency_metadata_index(simulated.supply))
    assert not any(event == "read" for event, _ in simulated.fs.events)


@pytest.mark.parametrize("action", ["read", "publish"])
def test_lock_contention_is_bounded_by_deadline(api: Any, simulated: Any, action: str) -> None:
    simulated.fs.block_lock = True
    assert_code(api, "timeout", lambda: api.read_dependency_metadata_index(simulated.supply)
        if action == "read" else api.publish_dependency_metadata_index(simulated.root, simulated.supply))
    assert simulated.fs.clock > 100
    assert not any(event == "create" for event, _ in simulated.fs.events)


def test_publish_uses_actual_wheel_metadata_then_durable_no_replace_link(
    api: Any, simulated: Any,
) -> None:
    install_wheel(simulated)
    result = api.publish_dependency_metadata_index(simulated.root, simulated.supply)
    assert result == simulated.value
    final = simulated.fs.nodes[final_path(simulated.supply)]
    assert final.data == simulated.value.to_bytes()
    assert final.mode == stat.S_IFREG | 0o600 and final.nlink == 1
    assert not any("/.dependency-metadata-" in path for path in simulated.fs.nodes)
    events = simulated.fs.events
    linked = next(i for i, event in enumerate(events) if event[0] == "link")
    assert any(event == "fsync" and path.startswith(STATE + "/.")
               for event, path in events[:linked])
    assert ("fsync", STATE) in events[linked:]
    assert ("lock", "2") in events
    assert not any(path.startswith(WHEELS) and event in {"write", "create", "unlink", "link"}
                   for event, path in events)


@pytest.mark.parametrize("missing", [False, True])
def test_publish_covers_whole_catalog_and_sorts_records_only_after_all_wheels_validate(
    api: Any, simulated: Any, monkeypatch: pytest.MonkeyPatch, missing: bool,
) -> None:
    original_deadline = 150.0
    deadlines = []

    def extract(entry: Any, raw: bytes, *, deadline: float | None = None) -> Any:
        deadlines.append(deadline)
        return extract_python_wheel_metadata(entry, raw, deadline=deadline)

    monkeypatch.setattr(api, "extract_python_wheel_metadata", extract)
    last, last_raw = wheel(name="zzz")
    first, first_raw = wheel(name="aaa", headers=b"Requires-Dist: zzz>=1\n")
    simulated.supply = replace(simulated.supply, catalog=(last, first))
    simulated.fs.add(WHEELS + "/" + last.filename, stat.S_IFREG | 0o444, last_raw)
    if missing:
        assert_code(api, "metadata_unavailable",
                    lambda: api.publish_dependency_metadata_index(simulated.root, simulated.supply, deadline=original_deadline))
        assert not any(event in {"create", "link"} for event, _ in simulated.fs.events)
    else:
        simulated.fs.add(WHEELS + "/" + first.filename, stat.S_IFREG | 0o444, first_raw)
        result = api.publish_dependency_metadata_index(simulated.root, simulated.supply, deadline=original_deadline)
        assert tuple(item.wheel.name for item in result.records) == ("aaa", "zzz")
        assert result.records[0].requires_dist == ("zzz>=1",)
        assert result.require_catalog(simulated.supply.catalog, simulated.supply.digest) is None

    assert deadlines == [original_deadline] * (1 if missing else 2)


def test_existing_correct_index_is_reused_even_with_no_wheels(api: Any, simulated: Any) -> None:
    original = install_index(simulated)
    assert api.publish_dependency_metadata_index(simulated.root, simulated.supply) == simulated.value
    assert simulated.fs.nodes[final_path(simulated.supply)] is original
    assert not any(path.startswith(WHEELS) for _, path in simulated.fs.events)
    assert not any(event in {"write", "link", "unlink", "create"} for event, _ in simulated.fs.events)


@pytest.mark.parametrize("raw,code", [(b"{", "metadata_untrusted"), (b"", "metadata_untrusted")])
def test_publisher_never_replaces_bad_existing_index(api: Any, simulated: Any, raw: bytes, code: str) -> None:
    original = install_index(simulated, raw)
    assert_code(api, code, lambda: api.publish_dependency_metadata_index(simulated.root, simulated.supply))
    assert simulated.fs.nodes[final_path(simulated.supply)] is original
    assert not any(event in {"create", "link", "unlink", "write"} for event, _ in simulated.fs.events)


def test_publisher_keeps_a_different_catalog_index_as_evidence(api: Any, simulated: Any) -> None:
    different = replace(simulated.value, catalog_sha256="f" * 64)
    existing = install_index(simulated, different.to_bytes())
    assert_code(api, "catalog_changed",
                lambda: api.publish_dependency_metadata_index(simulated.root, simulated.supply))
    assert simulated.fs.nodes[final_path(simulated.supply)] is existing
    assert not any(event in {"create", "link", "write", "unlink"} for event, _ in simulated.fs.events)


@pytest.mark.parametrize("case", ["absent", "sha", "size", "metadata", "mode", "link"])
def test_publisher_requires_real_complete_safe_wheel_bytes(api: Any, simulated: Any, case: str) -> None:
    node = install_wheel(simulated)
    if case == "absent":
        del simulated.fs.nodes[WHEELS + "/" + simulated.supply.catalog[0].filename]
    elif case == "sha":
        node.data = node.data.replace(b"never import wheel", b"never import wheeX")
    elif case == "size":
        node.data += b"x"
    elif case == "metadata":
        entry, raw = wheel(headers=b"Requires-Python: >=3\nRequires-Python: <4\n")
        simulated.supply = replace(simulated.supply, catalog=(entry,))
        node.data = raw
    elif case == "mode":
        node.mode |= 0o022
    else:
        node.nlink = 2
    assert_code(api, "metadata_unavailable" if case == "absent" else "metadata_untrusted",
                lambda: api.publish_dependency_metadata_index(simulated.root, simulated.supply))
    assert not any(event in {"create", "link", "write"} for event, _ in simulated.fs.events)


def test_expired_wheel_validation_cannot_publish(api: Any, simulated: Any) -> None:
    install_wheel(simulated)
    simulated.fs.after_read = lambda node, path: setattr(simulated.fs, "clock", 10000.0)
    assert_code(api, "timeout", lambda: api.publish_dependency_metadata_index(simulated.root, simulated.supply))
    assert not any(event == "link" for event, _ in simulated.fs.events)


def test_deadline_covers_the_actual_extractor_and_not_only_reads(
    api: Any, simulated: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_wheel(simulated)
    original_deadline = 150.0
    deadlines = []

    def slow_extract(entry: Any, raw: bytes, *, deadline: float | None = None) -> Any:
        deadlines.append(deadline)
        value = extract_python_wheel_metadata(entry, raw, deadline=deadline)
        simulated.fs.clock = 10000.0
        return value

    monkeypatch.setattr(api, "extract_python_wheel_metadata", slow_extract)
    assert_code(api, "timeout", lambda: api.publish_dependency_metadata_index(simulated.root, simulated.supply, deadline=original_deadline))
    assert not any(event == "create" for event, _ in simulated.fs.events)

    assert deadlines == [original_deadline]


@pytest.mark.parametrize("case", ["write", "link", "link_unknown", "postlink_fsync", "foreign_temp"])
def test_publication_faults_preserve_finals_and_only_cleanup_confirmed_own_temp(
    api: Any, simulated: Any, case: str,
) -> None:
    install_wheel(simulated)
    simulated.fs.fail_write = case == "write"
    simulated.fs.fail_link = errno.EPERM if case == "link" else None
    if case == "link_unknown":
        def unknown(source: str, target: str) -> None:
            raise OSError(errno.EIO, "unknown link outcome")
        simulated.fs.after_link = unknown
    elif case == "postlink_fsync":
        def fail_fsync(node: Node, path: str) -> None:
            if path == STATE and final_path(simulated.supply) in simulated.fs.nodes:
                raise OSError(errno.EIO, "unknown durability")
        simulated.fs.after_fsync = fail_fsync
    elif case == "foreign_temp":
        def replace_temp(node: Node, path: str) -> None:
            if "/.dependency-metadata-" in path:
                simulated.fs.add(path, stat.S_IFREG | 0o600, b"foreign evidence")
        simulated.fs.after_fsync = replace_temp
    assert_code(api, "metadata_untrusted",
                lambda: api.publish_dependency_metadata_index(simulated.root, simulated.supply))
    if case in {"link_unknown", "postlink_fsync"}:
        assert simulated.fs.nodes[final_path(simulated.supply)].data == simulated.value.to_bytes()
    if case == "foreign_temp":
        assert any(node.data == b"foreign evidence" for node in simulated.fs.nodes.values())
    else:
        assert not any("/.dependency-metadata-" in path for path in simulated.fs.nodes)


def test_temp_name_collision_is_never_removed(api: Any, simulated: Any) -> None:
    install_wheel(simulated)
    simulated.fs.create_collision = True
    assert_code(api, "metadata_untrusted",
                lambda: api.publish_dependency_metadata_index(simulated.root, simulated.supply))
    assert any(node.data == b"foreign temp" for node in simulated.fs.nodes.values())
    assert not any(event == "unlink" for event, _ in simulated.fs.events)


def test_temp_owner_drift_is_detected_before_publishing_any_final(api: Any, simulated: Any) -> None:
    install_wheel(simulated)

    def change_owner(node: Node, path: str) -> None:
        if "/.dependency-metadata-" in path:
            node.uid = 10001

    simulated.fs.after_fsync = change_owner
    assert_code(api, "metadata_untrusted",
                lambda: api.publish_dependency_metadata_index(simulated.root, simulated.supply))
    assert not any(event == "link" for event, _ in simulated.fs.events)
    assert final_path(simulated.supply) not in simulated.fs.nodes


def test_final_appearing_during_link_is_retained_without_replacement(api: Any, simulated: Any) -> None:
    install_wheel(simulated)

    def add_foreign(source: str, target: str) -> None:
        simulated.fs.add(target, stat.S_IFREG | 0o600, b"foreign final")

    simulated.fs.before_link = add_foreign
    assert_code(api, "metadata_untrusted",
                lambda: api.publish_dependency_metadata_index(simulated.root, simulated.supply))
    assert simulated.fs.nodes[final_path(simulated.supply)].data == b"foreign final"
    assert not any("/.dependency-metadata-" in path for path in simulated.fs.nodes)


def test_state_cannot_be_inside_the_flat_wheel_root(api: Any, simulated: Any) -> None:
    supply = replace(simulated.supply, state_root=Path(WHEELS + "/state"))
    assert_code(api, "metadata_untrusted", lambda: api.publish_dependency_metadata_index(simulated.root, supply))
    assert not any(event == "create" for event, _ in simulated.fs.events)


@pytest.mark.parametrize("arguments", [[], ["--read"], ["--publish", "/user/path"],
                                       ["--publish", "--publish"], ["--url", "https://secret"]])
def test_cli_accepts_only_explicit_publish_and_no_user_paths_or_urls(
    api: Any, monkeypatch: pytest.MonkeyPatch, arguments: list[str], capsys: Any,
) -> None:
    assert api.main(arguments) == 2
    output = capsys.readouterr()
    assert output.out == "" and output.err.strip() in {
        "metadata_untrusted", "unsupported", "metadata_unavailable", "catalog_changed", "timeout"}
    assert "secret" not in output.err


def test_cli_loads_only_existing_configuration_loaders(api: Any, simulated: Any,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    backend = importlib.import_module("agent_hub.skills.sandbox.python_project_backend")
    catalog = importlib.import_module("agent_hub.skills.sandbox.python_input_supply_catalog")
    monkeypatch.setattr(backend, "read_root_configuration", lambda: simulated.root)
    monkeypatch.setattr(catalog, "read_supply_configuration", lambda: simulated.supply)
    install_index(simulated)
    assert api.main(["--publish"]) == 0
    assert api.sys.stderr.getvalue() == ""
    assert STATE not in api.sys.stdout.getvalue()


@pytest.fixture
def linux_root_profile() -> Iterator[Path]:
    if sys.platform != "linux" or os.geteuid() != 0 or os.getegid() != 0:
        pytest.skip("Linux Root true IO pending server verification; FD simulation is separate")
    with tempfile.TemporaryDirectory(prefix="dependency-index-test-", dir="/run") as directory:
        profile = Path(directory)
        profile.chmod(0o700)
        yield profile


def native_profile(profile: Path) -> tuple[RootConfig, RootSupplyConfig, Path]:
    state, cache = profile / "state", profile / "wheels"
    state.mkdir(mode=0o700)
    cache.mkdir(mode=0o700)
    root = replace(root_configuration(), wheel_root=cache)
    supply = replace(supply_configuration(), state_root=state)
    entry, raw = wheel()
    path = cache / entry.filename
    path.write_bytes(raw)
    path.chmod(0o444)
    return root, supply, path


def test_linux_real_root_publication_read_and_cold_reuse(api: Any, linux_root_profile: Path) -> None:
    root, supply, wheel_path = native_profile(linux_root_profile)
    value = api.publish_dependency_metadata_index(root, supply)
    assert value == metadata_index(supply)
    path = supply.state_root / ("dependency-metadata-" + supply.digest + ".json")
    info = path.stat()
    assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == info.st_gid == 0
    assert info.st_nlink == 1 and path.read_bytes() == value.to_bytes()
    assert [item.name for item in supply.state_root.iterdir()] == [path.name]
    assert [item.name for item in root.wheel_root.iterdir()] == [wheel_path.name]
    wheel_path.unlink()
    assert api.read_dependency_metadata_index(supply) == value
    assert api.publish_dependency_metadata_index(root, supply) == value


@pytest.mark.parametrize("case", ["mode", "hardlink", "symlink", "state_mode", "ancestor_mode"])
def test_linux_real_root_rejects_unsafe_index_profiles(
    api: Any, linux_root_profile: Path, case: str,
) -> None:
    root, supply, _ = native_profile(linux_root_profile)
    api.publish_dependency_metadata_index(root, supply)
    path = supply.state_root / ("dependency-metadata-" + supply.digest + ".json")
    if case == "mode":
        path.chmod(0o644)
    elif case == "hardlink":
        os.link(path, supply.state_root / "alias")
    elif case == "symlink":
        moved = path.with_suffix(".moved")
        path.rename(moved)
        path.symlink_to(moved)
    elif case == "state_mode":
        supply.state_root.chmod(0o755)
    else:
        linux_root_profile.chmod(0o722)
    assert_code(api, "metadata_untrusted", lambda: api.read_dependency_metadata_index(supply))


def test_linux_real_directory_flock_bounds_reader_wait(
    api: Any, linux_root_profile: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux flock only")
    import fcntl

    root, supply, _ = native_profile(linux_root_profile)
    api.publish_dependency_metadata_index(root, supply)
    parent = os.open(supply.state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(api, "_READ_SECONDS", 0.02)
        assert_code(api, "timeout", lambda: api.read_dependency_metadata_index(supply))
    finally:
        os.close(parent)
    assert api.read_dependency_metadata_index(supply) == metadata_index(supply)
