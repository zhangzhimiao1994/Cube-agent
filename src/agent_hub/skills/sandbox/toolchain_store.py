"""Root-only atomic SDK storage and fresh read-only measurement, not authority.

The caller owns the global lock, exact approval, reservations and UNKNOWN fence.
Failures preserve pending or published state; SDKs are never executed or GC'd.
Receipts are comparisons against fresh archive/tree measurements, not a registry
of trusted host paths or evidence that a compiler can run.
"""

from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import stat
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

from agent_hub.skills.sandbox.project_contract_broker import _Observed, _root, _stamp
from agent_hub.skills.sandbox.toolchain_archive import (
    ArchiveLimits,
    GoArchiveManifest,
    ToolchainArchiveError,
    inspect_go_archive,
)
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainArtifact
from agent_hub.skills.sandbox.toolchain_stage import (
    ToolchainStageError,
    ToolchainStageObservation,
    measure_staged_go_toolchain,
    stage_go_toolchain,
)

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_CHUNK = 64 * 1024
_RECEIPT_LIMIT = 64 * 1024
_CONTENTS = frozenset({"payload", "archive.tar.gz", "receipt.json"})
_CODES = frozenset(
    {
        "toolchain_store_unsafe",
        "toolchain_store_unsupported",
        "toolchain_store_timeout",
        "toolchain_store_missing",
        "toolchain_store_conflict",
    }
)


class ToolchainStoreError(ValueError):
    """Finite codes only, including when an underlying OS operation fails."""

    def __init__(self, code: str = "toolchain_store_unsafe") -> None:
        self.code = code if code in _CODES else "toolchain_store_unsafe"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class PublishedToolchain:
    artifact_sha256: str
    observation: ToolchainStageObservation
    reused: bool


def _need(value: object, code: str = "toolchain_store_unsafe") -> None:
    if not value:
        raise ToolchainStoreError(code)


def _check(deadline: float) -> None:
    _need(type(deadline) in {int, float})
    try:
        _need(math.isfinite(deadline) and deadline > 0)
    except OverflowError:
        raise ToolchainStoreError() from None
    _need(time.monotonic() < deadline, "toolchain_store_timeout")


def _native(deadline: float) -> None:
    if (
        sys.platform != "linux"
        or os.geteuid() != 0
        or not all(
            getattr(os, flag, 0)
            for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK")
        )
        or not {os.open, os.mkdir, os.stat, os.rename} <= os.supports_dir_fd
        or os.scandir not in os.supports_fd
    ):
        raise ToolchainStoreError("toolchain_store_unsupported")
    _check(deadline)


def _artifact(artifact: ToolchainArtifact) -> ToolchainArtifact:
    _need(type(artifact) is ToolchainArtifact)
    return ToolchainArtifact.from_json(artifact.to_json())


def _inspect(data: bytes, artifact: ToolchainArtifact, deadline: float) -> GoArchiveManifest:
    _need(type(data) is bytes)
    manifest = inspect_go_archive(
        data,
        expected_sha256=artifact.sha256,
        expected_size_bytes=artifact.archive_bytes,
        limits=ArchiveLimits(
            max_archive_bytes=artifact.archive_bytes,
            max_expanded_bytes=artifact.tar_bytes_limit,
            max_entries=artifact.archive_entries_limit,
            max_directories=artifact.directory_entries_limit,
        ),
        deadline=deadline,
    )
    _need(
        manifest.version == "go" + artifact.version
        and manifest.expanded_bytes <= artifact.unpacked_bytes_limit
    )
    return manifest


def _receipt(artifact: ToolchainArtifact, observation: ToolchainStageObservation) -> bytes:
    raw = json.dumps(
        {
            "schema_version": 1,
            "artifact_sha256": artifact.artifact_sha256,
            "archive_sha256": observation.archive_sha256,
            "archive_bytes": artifact.archive_bytes,
            "tree_sha256": observation.tree_sha256,
            "binary_sha256": observation.binary_sha256,
            "gofmt_sha256": observation.gofmt_sha256,
            "file_count": observation.file_count,
            "total_bytes": observation.total_bytes,
            "version": observation.version,
            "platform": observation.platform,
            "status": observation.status,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    _need(0 < len(raw) <= _RECEIPT_LIMIT)
    return raw


def _pin_root(path: Path, stack: ExitStack, observed: list[_Observed]) -> int:
    _need(
        isinstance(path, Path)
        and path.is_absolute()
        and path.anchor == "/"
        and len(path.parts) > 1
        and ".." not in path.parts
        and "\\" not in str(path)
    )
    return _root(path, 0, stack, observed, stage=True)


def _child(parent: int, name: str, stack: ExitStack, observed: list[_Observed]) -> int:
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    stack.callback(os.close, fd)
    info = os.fstat(fd)
    _need(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == 0
        and stat.S_IMODE(info.st_mode) == 0o700
        and info.st_dev == os.fstat(parent).st_dev
    )
    entry = _Observed(parent, name, _stamp(info, full=True), True, fd)
    entry.check()
    observed.append(entry)
    return fd


def _recheck(observed: list[_Observed], deadline: float) -> None:
    for entry in observed:
        _check(deadline)
        entry.check()


def _names(fd: int, expected: frozenset[str], deadline: float) -> None:
    names: set[str] = set()
    with os.scandir(fd) as entries:
        for entry in entries:
            _check(deadline)
            names.add(entry.name)
            _need(len(names) <= len(expected))
    _need(names == expected)


def _read_file(
    parent: int,
    name: str,
    limit: int,
    deadline: float,
    stack: ExitStack,
    observed: list[_Observed],
    *,
    exact_size: bool = False,
) -> bytes:
    _check(deadline)
    fd = os.open(name, _READ_FLAGS, dir_fd=parent)
    stack.callback(os.close, fd)
    before = os.fstat(fd)
    _need(
        stat.S_ISREG(before.st_mode)
        and before.st_uid == 0
        and before.st_nlink == 1
        and stat.S_IMODE(before.st_mode) == 0o600
        and (before.st_size == limit if exact_size else 0 < before.st_size <= limit)
    )
    entry = _Observed(parent, name, _stamp(before, full=True), True, fd)
    entry.check()
    chunks: list[bytes] = []
    total = 0
    while True:
        _check(deadline)
        chunk = os.read(fd, min(_CHUNK, before.st_size + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        _need(total <= before.st_size)
        chunks.append(chunk)
    _need(total == before.st_size)
    entry.check()
    observed.append(entry)
    return b"".join(chunks)


def _write_file(parent: int, name: str, data: bytes, deadline: float) -> None:
    if sys.platform != "linux":
        raise ToolchainStoreError("toolchain_store_unsupported")
    _check(deadline)
    fd = os.open(name, _WRITE_FLAGS, 0o600, dir_fd=parent)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            _check(deadline)
            count = os.write(fd, view[:_CHUNK])
            _need(0 < count <= min(_CHUNK, len(view)))
            view = view[count:]
        os.fsync(fd)
        info = os.fstat(fd)
        _need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_size == len(data)
        )
        _Observed(parent, name, _stamp(info, full=True), True, fd).check()
    finally:
        os.close(fd)


def _rename_noreplace(source: int, name: str, destination: int, target: str) -> None:
    # Fail closed when libc/kernel lacks this atomic primitive; never emulate it
    # with exists()+rename(), which could overwrite a racing published entry.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    _need(rename is not None, "toolchain_store_unsupported")
    assert rename is not None
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source, os.fsencode(name), destination, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        if code in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
            raise ToolchainStoreError("toolchain_store_unsupported")
        raise OSError(code, "toolchain_store_rename")


def _exists(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _publish_container(
    root_fd: int,
    pending_fd: int,
    pending: str,
    name: str,
    deadline: float,
) -> _Observed:
    _check(deadline)
    before = _Observed(root_fd, pending, _stamp(os.fstat(pending_fd), full=True), True, pending_fd)
    before.check()
    try:
        _rename_noreplace(root_fd, pending, root_fd, name)
    except OSError as error:
        if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise ToolchainStoreError("toolchain_store_conflict") from None
        raise
    os.fsync(root_fd)
    # Reopening a valid-looking winner is insufficient: the final entry must be
    # the very container whose descriptor stayed pinned throughout construction.
    result = _Observed(root_fd, name, _stamp(os.fstat(pending_fd), full=True), True, pending_fd)
    result.check()
    _check(deadline)
    return result


def _observe(
    root: Path, root_fd: int, name: str, artifact: ToolchainArtifact, deadline: float
) -> ToolchainStageObservation:
    _check(deadline)
    with ExitStack() as stack:
        observed: list[_Observed] = []
        container = _child(root_fd, name, stack, observed)
        _names(container, _CONTENTS, deadline)
        _child(container, "payload", stack, observed)
        data = _read_file(
            container,
            "archive.tar.gz",
            artifact.archive_bytes,
            deadline,
            stack,
            observed,
            exact_size=True,
        )
        receipt = _read_file(container, "receipt.json", _RECEIPT_LIMIT, deadline, stack, observed)
        manifest = _inspect(data, artifact, deadline)
        observation = measure_staged_go_toolchain(
            root / name / "payload", artifact=artifact, manifest=manifest, deadline=deadline
        )
        _need(receipt == _receipt(artifact, observation))
        _names(container, _CONTENTS, deadline)
        _recheck(observed, deadline)
        return observation


def _error(error: Exception) -> NoReturn:
    if isinstance(error, ToolchainStoreError):
        raise error
    if isinstance(error, ToolchainStageError):
        if error.code == "toolchain_stage_timeout":
            raise ToolchainStoreError("toolchain_store_timeout") from None
        if error.code == "toolchain_stage_unsupported":
            raise ToolchainStoreError("toolchain_store_unsupported") from None
    if isinstance(error, ToolchainArchiveError) and error.code == "archive_timeout":
        raise ToolchainStoreError("toolchain_store_timeout") from None
    raise ToolchainStoreError() from None


def observe_published_toolchain(
    runtime_root: Path,
    *,
    artifact: ToolchainArtifact,
    deadline: float,
) -> PublishedToolchain:
    """Read pinned archive/receipt and remeasure the whole SDK, without writes."""
    try:
        _native(deadline)
        artifact = _artifact(artifact)
        with ExitStack() as stack:
            observed: list[_Observed] = []
            fd = _pin_root(runtime_root, stack, observed)
            name = artifact.artifact_sha256
            _need(_exists(fd, name), "toolchain_store_missing")
            result = _observe(runtime_root, fd, name, artifact, deadline)
            _recheck(observed, deadline)
            return PublishedToolchain(name, result, True)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError, RecursionError) as error:
        _error(error)


def publish_staged_toolchain(
    runtime_root: Path,
    stage: Path,
    data: bytes,
    *,
    artifact: ToolchainArtifact,
    deadline: float,
) -> PublishedToolchain:
    """Move or EXDEV-restage into a private pending container, then publish once.

    Caller-held lock/approval/reservations are preconditions, not inferred here.
    Reuse is read-only and leaves incoming alone. No failure cleans pending or
    already-published state; the caller owns UNKNOWN decisions after an SDK move.
    """
    try:
        _native(deadline)
        artifact = _artifact(artifact)
        manifest = _inspect(data, artifact, deadline)
        with ExitStack() as stack:
            roots: list[_Observed] = []
            source: list[_Observed] = []
            root_fd = _pin_root(runtime_root, stack, roots)
            source_fd = _pin_root(stage, stack, source)
            _need(not stage.is_relative_to(runtime_root) and not runtime_root.is_relative_to(stage))
            source_identity = _stamp(os.fstat(source_fd), full=False)
            source_snapshot = _Observed(
                source[-1].parent,
                source[-1].name,
                _stamp(os.fstat(source_fd), full=True),
                True,
                source_fd,
            )
            incoming = measure_staged_go_toolchain(
                stage, artifact=artifact, manifest=manifest, deadline=deadline
            )
            source_snapshot.check()
            _recheck(roots + source, deadline)
            name = artifact.artifact_sha256
            if _exists(root_fd, name):
                result = _observe(runtime_root, root_fd, name, artifact, deadline)
                source_snapshot.check()
                _recheck(roots + source, deadline)
                return PublishedToolchain(name, result, True)
            pending = ".pending-" + uuid4().hex
            os.mkdir(pending, 0o700, dir_fd=root_fd)
            pending_pins: list[_Observed] = []
            pending_fd = _child(root_fd, pending, stack, pending_pins)
            # Contents necessarily change while constructing pending. Pin identity
            # now; the complete-container fresh measurement takes a full snapshot.
            pending_pin = _Observed(
                root_fd, pending, _stamp(os.fstat(pending_fd), full=False), False, pending_fd
            )
            _write_file(pending_fd, "archive.tar.gz", data, deadline)
            _write_file(pending_fd, "receipt.json", _receipt(artifact, incoming), deadline)
            source_snapshot.check()
            _recheck(roots + source, deadline)
            pending_pin.check()
            moved = False
            try:
                _rename_noreplace(source[-1].parent, source[-1].name, pending_fd, "payload")
                moved = True
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                os.mkdir("payload", 0o700, dir_fd=pending_fd)
                stage_go_toolchain(
                    data,
                    artifact=artifact,
                    stage=runtime_root / pending / "payload",
                    deadline=deadline,
                )
            payload_pins: list[_Observed] = []
            payload_fd = _child(pending_fd, "payload", stack, payload_pins)
            if moved:
                _need(_stamp(os.fstat(payload_fd), full=False) == source_identity)
            else:
                source_snapshot.check()
            _recheck(roots + (source[:-1] if moved else source), deadline)
            pending_pin.check()
            result = _observe(runtime_root, root_fd, pending, artifact, deadline)
            _need(result == incoming)
            _recheck(payload_pins, deadline)
            os.fsync(payload_fd)
            os.fsync(pending_fd)
            if moved:
                os.fsync(source[-1].parent)
            _check(deadline)
            _recheck(roots + (source[:-1] if moved else source), deadline)
            pending_pin.check()
            published_pin = _publish_container(root_fd, pending_fd, pending, name, deadline)
            fresh = _observe(runtime_root, root_fd, name, artifact, deadline)
            _need(fresh == incoming)
            _recheck(roots + (source[:-1] if moved else source), deadline)
            published_pin.check()
            _check(deadline)
            return PublishedToolchain(name, fresh, False)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError, RecursionError) as error:
        _error(error)
