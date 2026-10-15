"""Capture approved package bytes into owned Linux sealed memfds.

The caller supplies server-validated authority and holds its approval scope.
Consumers borrow the FDs until context exit and arrange subprocess pass_fds.
This module never executes code or derives approval from installed content.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins.execution_authority import PackageExecutionMember

_FAILURE = "Plugin package immutable capture failed"
_MAX_DEPTH = 64
_MAX_PATH_BYTES = 4096
_CHUNK_BYTES = 65536


@dataclass(frozen=True, slots=True)
class SealedPackageFile:
    path: str
    fd: int


@dataclass(frozen=True, slots=True)
class SealedPackageView:
    files: tuple[SealedPackageFile, ...]

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return tuple(file.fd for file in self.files)


@dataclass(slots=True)
class _Budget:
    max_bytes: int
    deadline: float
    consumed: int = 0

    def check(self) -> None:
        if monotonic() >= self.deadline:
            raise ValueError(_FAILURE)


@dataclass(frozen=True, slots=True)
class _Directory:
    fd: int
    parent_fd: int | None
    name: str
    info: os.stat_result
    ancestor: bool = False


@dataclass(frozen=True, slots=True)
class _CapturedFile:
    path: str
    data: bytes


def _safe_parts(path: str) -> tuple[str, ...]:
    if (
        not path
        or len(path.encode("utf-8")) > _MAX_PATH_BYTES
        or any(character in path for character in ("\\", ":", "\x00"))
    ):
        raise ValueError(_FAILURE)
    parts = tuple(path.split("/"))
    if len(parts) > _MAX_DEPTH or any(
        part in {"", ".", ".."} or part.casefold() == "plugin.json" for part in parts
    ):
        raise ValueError(_FAILURE)
    return parts


def _approved_members(
    members: tuple[PackageExecutionMember, ...], max_files: int, budget: _Budget
) -> tuple[dict[str, PackageExecutionMember], set[str]]:
    if not isinstance(members, tuple) or not members or len(members) > max_files + 1:
        raise ValueError(_FAILURE)
    approved: dict[str, PackageExecutionMember] = {}
    directories: set[str] = set()
    seen: set[str] = set()
    total = 0
    for member in members:
        budget.check()
        if not isinstance(member, PackageExecutionMember):
            raise TypeError(_FAILURE)
        parts = ("plugin.json",) if member.path == "plugin.json" else _safe_parts(member.path)
        if (
            member.path in seen
            or type(member.size) is not int
            or member.size < 0
            or re.fullmatch(r"[0-9a-f]{64}", member.sha256) is None
        ):
            raise ValueError(_FAILURE)
        seen.add(member.path)
        # The signed report includes the rewritten archive manifest, while the
        # installer omits it. Project it out; never read or seal a host manifest.
        if member.path == "plugin.json":
            continue
        total += member.size
        if total > budget.max_bytes or len(approved) >= max_files:
            raise ValueError(_FAILURE)
        approved[member.path] = member
        directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    if not approved or directories.intersection(approved):
        raise ValueError(_FAILURE)
    return approved, directories


def _require_linux() -> None:
    if sys.platform != "linux" or os.name != "posix":
        raise ValueError(_FAILURE)
    # All flags are mandatory. Missing primitives never select a path fallback.
    for attribute in (
        "O_DIRECTORY",
        "O_NOFOLLOW",
        "O_NONBLOCK",
        "O_CLOEXEC",
        "memfd_create",
        "MFD_CLOEXEC",
        "MFD_ALLOW_SEALING",
    ):
        if not hasattr(os, attribute):
            raise ValueError(_FAILURE)


def _directory_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
    )


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        *_directory_identity(info),
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _entry_stat(name: str, parent_fd: int | None) -> os.stat_result:
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def _directory(
    name: str, parent_fd: int | None, stack: ExitStack, budget: _Budget,
    *, ancestor: bool = False,
) -> _Directory:
    if sys.platform != "linux":
        raise ValueError(_FAILURE)
    budget.check()
    before = _entry_stat(name, parent_fd)
    if not stat.S_ISDIR(before.st_mode) or _reparse(before):
        raise ValueError(_FAILURE)
    fd = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
    )
    stack.callback(os.close, fd)
    identity = _directory_identity if ancestor else _identity
    if identity(os.fstat(fd)) != identity(before):
        raise ValueError(_FAILURE)
    budget.check()
    return _Directory(fd, parent_fd, name, before, ancestor)


def _names(fd: int, limit: int, budget: _Budget) -> tuple[str, ...]:
    names: set[str] = set()
    with os.scandir(fd) as entries:
        for entry in entries:
            budget.check()
            if entry.name in names or len(names) >= limit:
                raise ValueError(_FAILURE)
            names.add(entry.name)
    return tuple(sorted(names))


def _regular(
    name: str,
    parent_fd: int,
    before: os.stat_result,
    expected: PackageExecutionMember,
    budget: _Budget,
) -> bytes:
    if sys.platform != "linux":
        raise ValueError(_FAILURE)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or _reparse(before)
        or before.st_size != expected.size
        or before.st_size > budget.max_bytes - budget.consumed
    ):
        raise ValueError(_FAILURE)
    budget.check()
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        if _identity(os.fstat(fd)) != _identity(before):
            raise ValueError(_FAILURE)
        chunks: list[bytes] = []
        count = 0
        while True:
            budget.check()
            chunk = os.read(fd, min(_CHUNK_BYTES, expected.size - count + 1))
            budget.check()
            if not chunk:
                break
            count += len(chunk)
            budget.consumed += len(chunk)
            if count > expected.size or budget.consumed > budget.max_bytes:
                raise ValueError(_FAILURE)
            chunks.append(chunk)
        if (
            count != expected.size
            or _identity(os.fstat(fd)) != _identity(before)
            or _identity(_entry_stat(name, parent_fd)) != _identity(before)
        ):
            raise ValueError(_FAILURE)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if hashlib.sha256(data).hexdigest() != expected.sha256:
        raise ValueError(_FAILURE)
    budget.check()
    return data


def _capture_tree(
    root: Path,
    approved: dict[str, PackageExecutionMember],
    allowed_directories: set[str],
    budget: _Budget,
) -> tuple[_CapturedFile, ...]:
    if (
        not root.is_absolute()
        or root.anchor != "/"
        or ".." in root.parts
        or len(root.parts) > _MAX_DEPTH
        or len(os.fsencode(root)) > _MAX_PATH_BYTES
    ):
        raise ValueError(_FAILURE)
    captured: dict[str, bytes] = {}
    observed: list[tuple[int, str, os.stat_result]] = []
    directories: list[_Directory] = []
    tree_names: dict[int, tuple[str, ...]] = {}
    entry_limit = len(approved) + len(allowed_directories)
    visited: set[str] = set()

    with ExitStack() as stack:
        # The sole absolute open bootstraps the filesystem root. Every remaining
        # component, including the package's ancestry, is opened relative to an FD.
        current = _directory("/", None, stack, budget, ancestor=len(root.parts) > 1)
        directories.append(current)
        for index, component in enumerate(root.parts[1:], start=1):
            current = _directory(
                component, current.fd, stack, budget, ancestor=index < len(root.parts) - 1,
            )
            directories.append(current)

        def walk(directory: _Directory, prefix: str) -> None:
            names = _names(directory.fd, entry_limit, budget)
            tree_names[directory.fd] = names
            for name in names:
                budget.check()
                relative = f"{prefix}/{name}" if prefix else name
                _safe_parts(relative)
                if relative in visited or len(visited) >= entry_limit:
                    raise ValueError(_FAILURE)
                visited.add(relative)
                before = _entry_stat(name, directory.fd)
                if stat.S_ISDIR(before.st_mode) and relative in allowed_directories:
                    child = _directory(name, directory.fd, stack, budget)
                    if _identity(child.info) != _identity(before):
                        raise ValueError(_FAILURE)
                    directories.append(child)
                    walk(child, relative)
                elif relative in approved:
                    captured[relative] = _regular(
                        name, directory.fd, before, approved[relative], budget
                    )
                    observed.append((directory.fd, name, before))
                else:
                    raise ValueError(_FAILURE)

        walk(current, "")
        if captured.keys() != approved.keys():
            raise ValueError(_FAILURE)
        # Revalidate earlier members after the last read, using the same pinned
        # parents. Detect late additions and replacement of an already read file.
        for parent_fd, name, before in observed:
            budget.check()
            if _identity(_entry_stat(name, parent_fd)) != _identity(before):
                raise ValueError(_FAILURE)
        for directory in reversed(directories):
            budget.check()
            if (
                directory.fd in tree_names
                and _names(directory.fd, entry_limit, budget) != tree_names[directory.fd]
            ):
                raise ValueError(_FAILURE)
            # Shared ancestry may change metadata when unrelated siblings change.
            identity = _directory_identity if directory.ancestor else _identity
            if identity(os.fstat(directory.fd)) != identity(directory.info) or identity(
                _entry_stat(directory.name, directory.parent_fd)
            ) != identity(directory.info):
                raise ValueError(_FAILURE)
    budget.check()
    return tuple(_CapturedFile(path, captured[path]) for path in sorted(captured))


def _seal_file(file: _CapturedFile, budget: _Budget) -> SealedPackageFile:
    if sys.platform != "linux":
        raise ValueError(_FAILURE)
    import fcntl

    budget.check()
    fd = os.memfd_create("approved-package", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    transferred = False
    try:
        remaining = memoryview(file.data)
        while remaining:
            budget.check()
            count = os.write(fd, remaining)
            if count <= 0:
                raise ValueError(_FAILURE)
            remaining = remaining[count:]
        seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SEAL
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
        if (
            fcntl.fcntl(fd, fcntl.F_GET_SEALS) & seals != seals
            or os.get_inheritable(fd)
            or os.fstat(fd).st_size != len(file.data)
        ):
            raise ValueError(_FAILURE)
        os.lseek(fd, 0, os.SEEK_SET)
        budget.check()
        result = SealedPackageFile(file.path, fd)
        transferred = True
        return result
    finally:
        if not transferred:
            os.close(fd)


@contextmanager
def capture_sealed_package(
    root: Path,
    members: tuple[PackageExecutionMember, ...],
    *,
    max_files: int = 512,
    max_total_bytes: int = 2097152,
    timeout_seconds: float = 10,
) -> Iterator[SealedPackageView]:
    """Yield owned immutable bytes; close every FD on exit, including cancellation.

    Capture failures have a fixed public message. Consumer exceptions propagate
    after cleanup. A unique trusted root plugin.json is excluded from the member
    projection and payload budgets; any installed plugin.json is forbidden.
    The timeout bounds cooperative checks around filesystem calls;
    it cannot interrupt a kernel call stalled on the underlying filesystem.
    """
    files: list[SealedPackageFile] = []
    try:
        try:
            if (
                type(max_files) is not int
                or max_files <= 0
                or type(max_total_bytes) is not int
                or max_total_bytes < 0
                or isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0
            ):
                raise ValueError(_FAILURE)
            budget = _Budget(max_total_bytes, monotonic() + timeout_seconds)
            approved, directories = _approved_members(members, max_files, budget)
            _require_linux()
            captured = _capture_tree(root, approved, directories, budget)
            for file in captured:
                files.append(_seal_file(file, budget))
            budget.check()
            sealed = SealedPackageView(tuple(files))
        except Exception:  # noqa: BLE001 - never expose filesystem or source details.
            raise RuntimeCapabilityError(_FAILURE) from None
        yield sealed
    finally:
        for sealed_file in reversed(files):
            with suppress(OSError):
                os.close(sealed_file.fd)
