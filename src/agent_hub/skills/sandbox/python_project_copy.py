"""Bounded Linux copies of an already normalized, drained private environment.

The trusted caller supplies canonical RootScopes, a physical export UID, all
digests and the lifetime fence. These are never model-selected paths or installer
authority. Source may belong to a DynamicUser below a root-owned sticky export;
destination's parent must be the current euid's exclusive 0700 directory. The
caller keeps both scopes private and all writers drained throughout this call.

Only the initial venv --copies recipe is supported. Nothing imports or executes
environment code, installs, authorizes, publishes, or removes anything. On error
the caller retains any partial destination for its UNKNOWN/cleanup policy.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import select
import signal
import stat
import sys
import time
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from agent_hub.capability_installer.python_attestation import (
    MANIFEST_NAME,
    METADATA_NAME,
    ScanLimits,
    _identity,
)
from agent_hub.skills.sandbox.project_contract_broker import (
    _Observed,
    _root,
    _seal_readonly,
    _stamp,
)
from agent_hub.skills.sandbox.python_project_environment import (
    PythonProjectEnvironmentObservation,
    _entries,
    _freeze_security_manifest,
    _scan_limits,
    observe_python_project_environment,
    python_environment_content_sha256,
)

_Reason = Literal[
    "copy_invalid",
    "copy_unsupported",
    "copy_unsafe",
    "copy_mismatch",
    "copy_limit",
    "copy_timeout",
    "copy_destination_exists",
]
_REASONS = frozenset(
    {
        "copy_invalid",
        "copy_unsupported",
        "copy_unsafe",
        "copy_mismatch",
        "copy_limit",
        "copy_timeout",
        "copy_destination_exists",
    }
)
_SHA = re.compile(r"[0-9a-f]{64}")
_CONTROLS = frozenset({MANIFEST_NAME, METADATA_NAME})


class PythonProjectCopyError(ValueError):
    """A finite path-free rejection, including distinct quota and timeout reasons."""

    def __init__(self, reason: _Reason) -> None:
        self.reason = reason if type(reason) is str and reason in _REASONS else "copy_invalid"
        super().__init__(self.reason)


def _need(condition: object, reason: _Reason = "copy_unsafe") -> None:
    if not condition:
        raise PythonProjectCopyError(reason)


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise PythonProjectCopyError("copy_timeout")


def _arguments(
    source: Path,
    destination: Path,
    source_owner: int,
    interpreter: Path,
    digests: tuple[str, ...],
    limits: ScanLimits,
    deadline: float,
) -> None:
    for path in (source, destination, interpreter):
        _need(isinstance(path, Path), "copy_invalid")
        _need(
            path.is_absolute()
            and ".." not in path.parts
            and "\0" not in str(path)
            and "\\" not in path.as_posix()
            and len(os.fsencode(path)) <= 4096
            and len(path.parts) <= 64
            and path != Path(path.anchor),
            "copy_invalid",
        )
    _need(
        not source.is_relative_to(destination) and not destination.is_relative_to(source),
        "copy_invalid",
    )
    _need(type(source_owner) is int and 0 <= source_owner < 2**32 - 1, "copy_invalid")
    _need(
        all(type(value) is str and _SHA.fullmatch(value) is not None for value in digests),
        "copy_invalid",
    )
    _need(
        type(deadline) in {float, int} and math.isfinite(deadline) and deadline > 0, "copy_invalid"
    )
    try:
        _scan_limits(limits)
    except (ValueError, TypeError, AttributeError):
        raise PythonProjectCopyError("copy_invalid") from None


def _recipe_entries(manifest: object, interpreter_sha256: str) -> dict[str, Mapping[str, Any]]:
    """Eligibility only; a supplied manifest never proves filesystem facts."""
    try:
        entries = _entries(manifest)
        _need(isinstance(manifest, Mapping) and manifest.get("interpreter_receipt") is None)
        for path, entry in entries.items():
            _need(path not in _CONTROLS and not path.endswith(".pyc"))
            if entry["kind"] == "symlink":
                _need(
                    path == "lib64"
                    and entry["target"] == "lib"
                    and "lib" in entries
                    and entries["lib"]["kind"] == "directory"
                )
        _need("bin" in entries and entries["bin"]["kind"] == "directory")
        for name in ("python", "python3", "python3.12"):
            executable = entries.get("bin/" + name)
            _need(executable is not None)
            assert executable is not None
            _need(
                executable["kind"] == "file"
                and executable["identity"][3] == 1
                and bool(executable["identity"][2] & 0o111)
                and executable["sha256"] == interpreter_sha256,
                "copy_mismatch",
            )
        return entries
    except PythonProjectCopyError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise PythonProjectCopyError("copy_unsafe") from None


def _directory_flags() -> int:
    if sys.platform != "linux":
        raise PythonProjectCopyError("copy_unsupported")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _same(parent: int, name: str, expected: os.stat_result, descriptor: int | None = None) -> None:
    _Observed(parent, name, _stamp(expected, full=True), True, descriptor).check()


def _same_snapshots(left: dict[str, os.stat_result], right: dict[str, os.stat_result]) -> bool:
    return left.keys() == right.keys() and all(
        _stamp(info, full=True) == _stamp(right[path], full=True) for path, info in left.items()
    )


def _snapshot(
    root: int,
    owner: int,
    limits: ScanLimits,
    deadline: float,
) -> dict[str, os.stat_result]:
    """Keep stat records, never an FD per member; reject links before observation."""
    result: dict[str, os.stat_result] = {}
    total = 0

    def walk(descriptor: int, path: str, depth: int) -> None:
        nonlocal total
        _check(deadline)
        before = os.fstat(descriptor)
        _need(
            before.st_uid == owner
            and stat.S_ISDIR(before.st_mode)
            and not before.st_mode & 0o7022
            and before.st_mode & 0o500 == 0o500
        )
        if path == ".":
            _need(stat.S_IMODE(before.st_mode) == 0o555)
        result[path] = before
        with os.scandir(descriptor) as members:
            for member in members:
                _check(deadline)
                relative = member.name if path == "." else path + "/" + member.name
                _need(
                    len(result) <= limits.max_entries
                    and depth + 1 <= limits.max_depth
                    and len(os.fsencode(relative)) <= limits.max_path_bytes,
                    "copy_limit",
                )
                _need(relative not in _CONTROLS and not relative.endswith(".pyc"))
                info = os.stat(member.name, dir_fd=descriptor, follow_symlinks=False)
                _need(info.st_uid == owner)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(member.name, _directory_flags(), dir_fd=descriptor)
                    try:
                        _same(descriptor, member.name, info, child)
                        walk(child, relative, depth + 1)
                        _same(descriptor, member.name, info, child)
                    finally:
                        os.close(child)
                else:
                    _need(info.st_nlink == 1)
                    if stat.S_ISLNK(info.st_mode):
                        _need(
                            relative == "lib64"
                            and os.readlink(member.name, dir_fd=descriptor) == "lib"
                        )
                    else:
                        _need(
                            stat.S_ISREG(info.st_mode)
                            and not info.st_mode & 0o7022
                            and info.st_mode & 0o400
                        )
                        total += info.st_size
                        _need(0 <= info.st_size and total <= limits.max_bytes, "copy_limit")
                    result[relative] = info
                    _same(descriptor, member.name, info)
        _need(_stamp(os.fstat(descriptor), full=True) == _stamp(before, full=True))

    walk(root, ".", 0)
    _need(len(result) - 1 <= limits.max_entries, "copy_limit")
    if "lib64" in result:
        _need("lib" in result and stat.S_ISDIR(result["lib"].st_mode))
    return result


def _write_all(descriptor: int, raw: bytes, deadline: float) -> None:
    remaining = memoryview(raw)
    while remaining:
        _check(deadline)
        count = os.write(descriptor, remaining)
        _need(count > 0)
        remaining = remaining[count:]
    _check(deadline)


def _observe(
    root: Path,
    input_sha: str,
    limits: ScanLimits,
    deadline: float,
) -> PythonProjectEnvironmentObservation:
    """Bound the legacy observer's entire scan, not just its call boundaries.

    A forked read-only worker runs the actual observer with normalization off.
    Only fixed-shape evidence crosses a bounded pipe. No environment code or
    interpreter is run. Always kill/reap this worker before returning, including
    on timeout; no scanner thread or process outlives the caller's lifetime fence.
    """
    if sys.platform != "linux":
        raise PythonProjectCopyError("copy_unsupported")
    _check(deadline)
    reader, writer = os.pipe()
    try:
        pid = os.fork()
    except BaseException:
        os.close(reader)
        os.close(writer)
        raise
    if pid == 0:
        exit_code = 0
        try:
            os.close(reader)
            actual = observe_python_project_environment(
                root,
                interpreter=root / "bin" / "python",
                environment_input_sha256=input_sha,
                limits=limits,
                normalize_bytecode=False,
            )
            manifest: dict[str, Any] = dict(actual.security_manifest)
            manifest["entries"] = [dict(entry) for entry in _entries(manifest).values()]
            raw = json.dumps(manifest, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            _write_all(writer, raw, deadline)
        except BaseException:  # noqa: BLE001 - never unwind a scan worker into the broker.
            exit_code = 1
        finally:
            try:
                os.close(writer)
            finally:
                os._exit(exit_code)
    os.close(writer)
    chunks: list[bytes] = []
    count = 0
    maximum = (limits.max_entries + 1) * (6 * limits.max_path_bytes + 1024) + 16384
    try:
        while True:
            _check(deadline)
            ready, _, _ = select.select([reader], [], [], max(0.0, deadline - time.monotonic()))
            _check(deadline)
            _need(ready, "copy_timeout")
            chunk = os.read(reader, min(1024 * 1024, maximum - count + 1))
            if not chunk:
                break
            count += len(chunk)
            _need(count <= maximum, "copy_limit")
            chunks.append(chunk)
    finally:
        os.close(reader)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        while True:
            try:
                os.waitpid(pid, 0)
                break
            except InterruptedError:
                continue
    _check(deadline)
    frozen = _freeze_security_manifest(json.loads(b"".join(chunks)), limits=limits)
    digest = python_environment_content_sha256(frozen, environment_input_sha256=input_sha)
    total = frozen["total_bytes"]
    assert type(total) is int
    _check(deadline)
    return PythonProjectEnvironmentObservation(digest, frozen, total, 0)


def _copy_tree(
    source: int,
    destination: int,
    snapshot: dict[str, os.stat_result],
    entries: dict[str, Mapping[str, Any]],
    limits: ScanLimits,
    deadline: float,
) -> None:
    if sys.platform != "linux":
        raise PythonProjectCopyError("copy_unsupported")
    children: dict[str, list[str]] = {}
    for path, entry in entries.items():
        _check(deadline)
        _need(path in snapshot)
        info = snapshot[path]
        identity = _identity(info)[:3] if entry["kind"] == "directory" else _identity(info)
        _need(identity == list(entry["identity"]))
        if path != ".":
            children.setdefault(PurePosixPath(path).parent.as_posix(), []).append(path)
    _need(set(entries) == set(snapshot))
    total = 0

    def walk(source_fd: int, target_fd: int, path: str) -> None:
        nonlocal total
        for relative in children.get(path, ()):
            _check(deadline)
            name = PurePosixPath(relative).name
            expected = snapshot[relative]
            _same(source_fd, name, expected)
            if stat.S_ISLNK(expected.st_mode):
                _need(relative == "lib64" and os.readlink(name, dir_fd=source_fd) == "lib")
                os.symlink("lib", name, dir_fd=target_fd)
                _same(source_fd, name, expected)
                continue
            directory = stat.S_ISDIR(expected.st_mode)
            flags = (
                _directory_flags()
                if directory
                else (os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            )
            current = os.open(name, flags, dir_fd=source_fd)
            try:
                _same(source_fd, name, expected, current)
                if directory:
                    os.mkdir(name, 0o700, dir_fd=target_fd)
                    target = os.open(name, _directory_flags(), dir_fd=target_fd)
                else:
                    target = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=target_fd,
                    )
                try:
                    if directory:
                        walk(current, target, relative)
                        mode = 0o555
                    else:
                        digest = hashlib.sha256()
                        count = 0
                        while True:
                            _check(deadline)
                            chunk = os.read(
                                current,
                                min(
                                    1024 * 1024,
                                    expected.st_size - count + 1,
                                    limits.max_bytes - total + 1,
                                ),
                            )
                            _check(deadline)
                            if not chunk:
                                break
                            count += len(chunk)
                            total += len(chunk)
                            _need(total <= limits.max_bytes, "copy_limit")
                            _need(count <= expected.st_size)
                            digest.update(chunk)
                            _write_all(target, chunk, deadline)
                        _need(
                            count == expected.st_size
                            and digest.hexdigest() == entries[relative]["sha256"]
                        )
                        mode = 0o555 if expected.st_mode & 0o111 else 0o444
                    _same(source_fd, name, expected, current)
                    _check(deadline)
                    _seal_readonly(target, mode)
                    _check(deadline)
                finally:
                    os.close(target)
                _same(source_fd, name, expected, current)
            finally:
                os.close(current)
        _need(_stamp(os.fstat(source_fd), full=True) == _stamp(snapshot[path], full=True))

    walk(source, destination, ".")
    _check(deadline)
    _seal_readonly(destination, 0o555)
    _check(deadline)


def copy_python_project_environment(
    source: Path,
    destination: Path,
    *,
    source_owner: int,
    interpreter: Path,
    interpreter_sha256: str,
    environment_input_sha256: str,
    expected_environment_sha256: str,
    limits: ScanLimits,
    deadline: float,
) -> PythonProjectEnvironmentObservation:
    """Copy once into a new private pending directory; return actual target evidence.

    ``source_owner`` is obtained by the trusted broker from its private export
    tree, never a model parameter. ``deadline`` is absolute monotonic time. The
    ``interpreter`` argument names the caller's canonical trusted runtime; its
    digest was captured upstream. This helper never opens that runtime path.
    Actual observation always binds source/bin/python or destination/bin/python,
    respectively, and all three copied bin files must match interpreter_sha256.
    External interpreter receipts are refused, so they cannot alter semantics.
    root broker or unit DynamicUser owns the new target. The caller guarantees
    RootScope trust, drained bwrap children, quota tmpfs, and exclusive lifetime;
    this helper cannot establish those prerequisites or publish a cache entry.
    """
    try:
        _arguments(
            source,
            destination,
            source_owner,
            interpreter,
            (interpreter_sha256, environment_input_sha256, expected_environment_sha256),
            limits,
            deadline,
        )
        if sys.platform != "linux" or os.name != "posix":
            raise PythonProjectCopyError("copy_unsupported")
        _need(source.anchor == destination.anchor == interpreter.anchor == "/", "copy_invalid")
        _check(deadline)
        owner = os.geteuid()
        with ExitStack() as stack:
            pins: list[_Observed] = []
            source_fd = _root(source, source_owner, stack, pins)
            parent_fd = _root(destination.parent, owner, stack, pins)
            _need(stat.S_IMODE(os.fstat(parent_fd).st_mode) == 0o700)
            snapshot = _snapshot(source_fd, source_owner, limits, deadline)
            initial = _observe(source, environment_input_sha256, limits, deadline)
            entries = _recipe_entries(initial.security_manifest, interpreter_sha256)
            _need(initial.environment_sha256 == expected_environment_sha256, "copy_mismatch")
            _need(_same_snapshots(snapshot, _snapshot(source_fd, source_owner, limits, deadline)))
            for pin in pins:
                _check(deadline)
                pin.check()
            try:
                os.mkdir(destination.name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                raise PythonProjectCopyError("copy_destination_exists") from None
            target_fd = os.open(destination.name, _directory_flags(), dir_fd=parent_fd)
            stack.callback(os.close, target_fd)
            created = os.fstat(target_fd)
            _need(created.st_uid == owner and stat.S_IMODE(created.st_mode) == 0o700)
            _same(parent_fd, destination.name, created, target_fd)
            _copy_tree(source_fd, target_fd, snapshot, entries, limits, deadline)
            sealed = os.fstat(target_fd)
            _need(
                (sealed.st_dev, sealed.st_ino, sealed.st_uid)
                == (created.st_dev, created.st_ino, owner)
                and stat.S_IMODE(sealed.st_mode) == 0o555
            )
            _same(parent_fd, destination.name, sealed, target_fd)
            actual = _observe(destination, environment_input_sha256, limits, deadline)
            _recipe_entries(actual.security_manifest, interpreter_sha256)
            _need(actual.environment_sha256 == expected_environment_sha256, "copy_mismatch")
            final = _snapshot(source_fd, source_owner, limits, deadline)
            _need(_same_snapshots(snapshot, final))
            _same(parent_fd, destination.name, sealed, target_fd)
            for pin in pins:
                _check(deadline)
                pin.check()
            _check(deadline)
            return actual
    except PythonProjectCopyError:
        raise
    except OSError as error:
        reason: _Reason = (
            "copy_limit" if error.errno in {errno.ENOSPC, errno.EDQUOT} else "copy_unsafe"
        )
        raise PythonProjectCopyError(reason) from None
    except (ValueError, TypeError, RuntimeError, OverflowError, KeyError, AttributeError):
        raise PythonProjectCopyError("copy_unsafe") from None
