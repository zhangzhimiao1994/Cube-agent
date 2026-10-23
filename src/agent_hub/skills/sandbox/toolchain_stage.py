"""Root-only SDK staging and fresh content measurements, not an install grant.

The authenticated supply worker must own an empty private stage, exact approval,
disk reservations, lifetime and consumed/UNKNOWN fences. This module never runs
SDK/project code, publishes an environment, cleans uncertain state, or accepts
workspace-controlled archive URLs. Nothing here establishes Go project support.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import stat
import sys
import tarfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_hub.skills.sandbox.project_contract_broker import (
    _Observed,
    _root,
    _seal_readonly,
    _stamp,
    _write_all,
)
from agent_hub.skills.sandbox.toolchain_archive import (
    ArchiveLimits,
    GoArchiveManifest,
    ToolchainArchiveError,
    inspect_go_archive,
)
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainArtifact, ToolchainCatalogError

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class ToolchainStageError(ValueError):
    """Finite errors do not disclose host paths or underlying OS messages."""

    def __init__(self, code: str = "toolchain_stage_unsafe") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ToolchainStageObservation:
    version: str
    platform: str
    archive_sha256: str
    tree_sha256: str
    binary_sha256: str
    gofmt_sha256: str
    total_bytes: int
    file_count: int
    manifest: GoArchiveManifest
    status: Literal["measured_not_executed"] = "measured_not_executed"


def _need(value: object, code: str = "toolchain_stage_unsafe") -> None:
    if not value:
        raise ToolchainStageError(code)


def _native(deadline: float) -> None:
    if (sys.platform != "linux" or os.geteuid() != 0
            or not all(getattr(os, flag, 0) for flag in (
                "O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK"))
            or not {os.open, os.mkdir, os.stat} <= os.supports_dir_fd
            or os.scandir not in os.supports_fd):
        raise ToolchainStageError("toolchain_stage_unsupported")
    _check(deadline)


def _check(deadline: float) -> None:
    _need(type(deadline) in {int, float})
    try:
        _need(math.isfinite(deadline) and deadline > 0)
    except OverflowError:
        raise ToolchainStageError() from None
    _need(time.monotonic() < deadline, "toolchain_stage_timeout")


def _artifact(value: ToolchainArtifact) -> ToolchainArtifact:
    _need(type(value) is ToolchainArtifact)
    return ToolchainArtifact.from_json(value.to_json())


def _directory(parent: int, name: str, stack: ExitStack, *, writable: bool = False) -> int:
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    stack.callback(os.close, fd)
    s = os.fstat(fd)
    _need(stat.S_ISDIR(s.st_mode) and s.st_uid == 0
          and stat.S_IMODE(s.st_mode) == (0o700 if writable else 0o555)
          and _stamp(s, full=False) == _stamp(os.stat(name, dir_fd=parent,
                                                    follow_symlinks=False), full=False))
    return fd


def _stage_root(stage: Path, stack: ExitStack) -> tuple[int, list[_Observed]]:
    _need(type(stage) is Path or isinstance(stage, Path))
    observed: list[_Observed] = []
    fd = _root(stage, 0, stack, observed, stage=True)
    return fd, observed


def _measure(
    parent: int, manifest: GoArchiveManifest, artifact: ToolchainArtifact, deadline: float,
) -> ToolchainStageObservation:
    _need(type(manifest) is GoArchiveManifest
          and manifest.archive_sha256 == artifact.sha256
          and manifest.archive_size_bytes == artifact.archive_bytes
          and manifest.version == "go" + artifact.version
          and 0 < manifest.expanded_bytes <= artifact.unpacked_bytes_limit)
    expected = {f.path: f for f in manifest.files}
    _need(len(expected) == len(manifest.files) <= artifact.archive_entries_limit)
    _need(manifest.directories == tuple(sorted(set(manifest.directories)))
          and 0 < len(manifest.directories) <= artifact.directory_entries_limit
          and "go" in manifest.directories
          and sum(f.size_bytes for f in manifest.files) == manifest.expanded_bytes)
    directories: dict[str, set[str]] = {p: set() for p in manifest.directories}
    for path in (*manifest.directories, *expected):
        parts = path.split("/")
        _need(parts[0] == "go"
              and all(p not in {"", ".", ".."} and "\\" not in p for p in parts))
        for end in range(1, len(parts)):
            prefix = "/".join(parts[:end])
            _need(prefix in directories)
            directories[prefix].add(parts[end])
    _need(not set(directories) & set(expected))
    _need({"go/bin/go", "go/bin/gofmt", "go/VERSION"} <= set(expected))
    snapshots: dict[str, tuple[int, ...]] = {}
    measured: set[str] = set()
    version = b""
    with ExitStack() as stack:
        go = _directory(parent, "go", stack)

        def walk(fd: int, prefix: str) -> None:
            nonlocal version
            _check(deadline)
            before = os.fstat(fd)
            snapshots[prefix] = _stamp(before, full=True)
            names: set[str] = set()
            with os.scandir(fd) as entries:
                for entry in entries:
                    _check(deadline)
                    names.add(entry.name)
                    _need(len(names) <= len(directories[prefix]))
            _need(names == directories[prefix])
            for name in sorted(names):
                _check(deadline)
                path = prefix + "/" + name
                s = os.stat(name, dir_fd=fd, follow_symlinks=False)
                snapshots[path] = _stamp(s, full=True)
                if path in directories:
                    with ExitStack() as child_stack:
                        child = _directory(fd, name, child_stack)
                        walk(child, path)
                    continue
                member = expected[path]
                _need(stat.S_ISREG(s.st_mode) and s.st_uid == 0 and s.st_nlink == 1
                      and s.st_size == member.size_bytes
                      and stat.S_IMODE(s.st_mode) == (0o555 if member.executable else 0o444))
                f = os.open(name, _READ_FLAGS, dir_fd=fd)
                try:
                    _need(_stamp(os.fstat(f), full=True) == _stamp(s, full=True))
                    digest = hashlib.sha256()
                    total = 0
                    while chunk := os.read(f, min(1024**2, member.size_bytes + 1 - total)):
                        _check(deadline)
                        total += len(chunk)
                        _need(total <= member.size_bytes)
                        digest.update(chunk)
                        if path == "go/VERSION":
                            _need(total <= 4096)
                            version += chunk
                    _need(total == member.size_bytes and digest.hexdigest() == member.sha256
                          and _stamp(os.fstat(f), full=True) == _stamp(s, full=True))
                finally:
                    os.close(f)
                measured.add(path)
            _need(_stamp(os.fstat(fd), full=True) == _stamp(before, full=True))

        walk(go, "go")
        _need(measured == set(expected) and version.splitlines()[0] == ("go" + artifact.version).encode())
        def verify(fd: int, prefix: str) -> None:
            _check(deadline)
            _need(_stamp(os.fstat(fd), full=True) == snapshots[prefix])
            names: set[str] = set()
            with os.scandir(fd) as entries:
                for entry in entries:
                    _check(deadline)
                    names.add(entry.name)
                    _need(len(names) <= len(directories[prefix]))
            _need(names == directories[prefix])
            for name in sorted(names):
                path = prefix + "/" + name
                _need(_stamp(os.stat(name, dir_fd=fd, follow_symlinks=False), full=True) == snapshots[path])
                if path in directories:
                    with ExitStack() as child_stack:
                        child = _directory(fd, name, child_stack)
                        verify(child, path)
            _need(_stamp(os.fstat(fd), full=True) == snapshots[prefix])

        verify(go, "go")
    return ToolchainStageObservation(artifact.version, artifact.platform, artifact.sha256,
        manifest.tree_sha256, expected["go/bin/go"].sha256, expected["go/bin/gofmt"].sha256,
        manifest.expanded_bytes, len(expected), manifest)


def measure_staged_go_toolchain(
    stage: Path, *, artifact: ToolchainArtifact, manifest: GoArchiveManifest, deadline: float,
) -> ToolchainStageObservation:
    """Fresh private-stage snapshot against a worker-owned verified archive manifest.

    The manifest is measurement data, not trust or execution authority. A future
    publisher/runner must revalidate its provenance and keep the actual SDK
    mounted read-only under its own exact scope and native lifetime controls.
    """
    try:
        _native(deadline)
        artifact = _artifact(artifact)
        with ExitStack() as stack:
            fd, observed = _stage_root(stage, stack)
            _need(set(os.listdir(fd)) == {"go"})
            result = _measure(fd, manifest, artifact, deadline)
            for entry in observed:
                entry.check()
            _check(deadline)
            return result
    except ToolchainStageError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, OverflowError, IndexError):
        raise ToolchainStageError() from None


def stage_go_toolchain(
    data: bytes, *, artifact: ToolchainArtifact, stage: Path, deadline: float,
) -> ToolchainStageObservation:
    """Verify, copy and seal an SDK beneath an already reserved empty Root700 stage.

    On failure, preserve the caller-owned private stage for its authoritative
    cleanup/UNKNOWN decision. Never overwrite or merge an existing destination.
    """
    try:
        _native(deadline)
        artifact = _artifact(artifact)
        manifest = inspect_go_archive(data, expected_sha256=artifact.sha256,
            expected_size_bytes=artifact.archive_bytes, limits=ArchiveLimits(
                max_archive_bytes=artifact.archive_bytes,
                max_expanded_bytes=artifact.tar_bytes_limit,
                max_entries=artifact.archive_entries_limit,
                max_directories=artifact.directory_entries_limit), deadline=deadline)
        _check(deadline)
        _need(manifest.version == "go" + artifact.version
              and manifest.expanded_bytes <= artifact.unpacked_bytes_limit)
        expected = {f.path: f for f in manifest.files}
        with ExitStack() as stack:
            parent, observed = _stage_root(stage, stack)
            _need(not os.listdir(parent))
            os.mkdir("go", 0o700, dir_fd=parent)
            go = _directory(parent, "go", stack, writable=True)
            directories = {"go"}
            for path in sorted(manifest.directories, key=lambda p: (p.count("/"), p)):
                _check(deadline)
                with ExitStack() as parents:
                    fd = go
                    parts = path.split("/")
                    for end in range(2, len(parts) + 1):
                        prefix = "/".join(parts[:end])
                        if prefix not in directories:
                            os.mkdir(parts[end - 1], 0o700, dir_fd=fd)
                            directories.add(prefix)
                        fd = _directory(fd, parts[end - 1], parents, writable=True)
            copied: set[str] = set()
            with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as archive:
                for member in archive:
                    _check(deadline)
                    if member.isdir():
                        continue
                    _need(member.isfile() and member.name in expected and member.name not in copied)
                    entry = expected[member.name]
                    _need(member.size == entry.size_bytes)
                    parts = member.name.split("/")
                    with ExitStack() as parents:
                        fd = go
                        for end in range(2, len(parts)):
                            prefix = "/".join(parts[:end])
                            if prefix not in directories:
                                os.mkdir(parts[end - 1], 0o700, dir_fd=fd)
                                directories.add(prefix)
                            fd = _directory(fd, parts[end - 1], parents, writable=True)
                        output = os.open(parts[-1], _WRITE_FLAGS, 0o600, dir_fd=fd)
                        try:
                            source = archive.extractfile(member)
                            _need(source is not None)
                            assert source is not None
                            digest = hashlib.sha256()
                            total = 0
                            with source:
                                while chunk := source.read(min(1024**2, entry.size_bytes + 1 - total)):
                                    _check(deadline)
                                    total += len(chunk)
                                    _need(total <= entry.size_bytes)
                                    digest.update(chunk)
                                    _write_all(output, chunk)
                            _need(total == entry.size_bytes and digest.hexdigest() == entry.sha256)
                            _seal_readonly(output, 0o555 if entry.executable else 0o444)
                        finally:
                            os.close(output)
                    copied.add(member.name)
            _need(copied == set(expected))
            for path in sorted(directories, key=lambda p: (p.count("/"), p), reverse=True):
                _check(deadline)
                with ExitStack() as parents:
                    fd = go
                    for name in path.split("/")[1:]:
                        fd = _directory(fd, name, parents, writable=True)
                    _seal_readonly(fd, 0o555)
            for observed_entry in observed:
                observed_entry.check()
            os.fsync(parent)
        return measure_staged_go_toolchain(stage, artifact=artifact, manifest=manifest, deadline=deadline)
    except ToolchainStageError:
        raise
    except ToolchainArchiveError as error:
        if error.code == "archive_timeout":
            raise ToolchainStageError("toolchain_stage_timeout") from None
        raise ToolchainStageError() from None
    except (ToolchainCatalogError, OSError, tarfile.TarError,
            ValueError, TypeError, AttributeError, OverflowError):
        raise ToolchainStageError() from None
