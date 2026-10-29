"""Root-only, scoped Python environment publication with consumed reservations.

The broker supplies policy, invocation and a drained private export. This module
does not authorize installation, run project code, or turn stored JSON into live
proof. A reservation holds the global lock through execution/publication; a
crashed reservation is never reused. READY entries require fresh tree measurement.
Only an explicit root-owned failed/quiescent marker permits a fresh granted
execution to archive the consumed attempt; at most 16 such recoveries are kept.
Unpublished capacity aborts are consumed, never install failures or retry grants.
No automatic eviction can erase UNKNOWN state. Expiry/release are explicit trusted
lifecycle operations; callers must exclude active/retained runs when pruning.
The bounded observer forks; use a single-threaded root helper, not asyncio.to_thread.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID

from agent_hub.capability_installer.python_attestation import ScanLimits
from agent_hub.skills.sandbox.project import _Execution
from agent_hub.skills.sandbox.project_contract_broker import (
    _encoded,
    _Observed,
    _open_directory,
    _read_control,
    _root,
    _stamp,
    _write_all,
)
from agent_hub.skills.sandbox.python_project_copy import (
    PythonProjectCopyError,
    _directory_flags,
    _observe,
    _recipe_entries,
    _same,
    _same_snapshots,
    _snapshot,
    copy_python_project_environment,
)
from agent_hub.skills.sandbox.python_project_environment import PythonProjectEnvironmentObservation
from agent_hub.skills.sandbox.python_project_process import PythonPhaseProcessResult
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonProjectInvocation,
    PythonProjectScope,
    base_python_invocation,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

_Reason = Literal[
    "cache_invalid",
    "cache_unsupported",
    "cache_unsafe",
    "cache_quota",
    "cache_timeout",
    "cache_incomplete",
    "cache_changed",
    "cache_expired",
]
_SHA = re.compile(r"[0-9a-f]{64}")
_CONTROL_LIMIT = 16 * 1024
_CONTROL_ALLOWANCE = 64 * 1024
_READY = frozenset({"reservation.json", "reference.json", "environment"})
_MAX_FAILURES = 16
_MAX_MEMBERS = 4 + 2 * _MAX_FAILURES
_ARCHIVE = re.compile(r"failed-([a-z0-9][a-z0-9_-]{0,127})\.(reservation|outcome)\.json")


class PythonProjectCacheError(ValueError):
    def __init__(self, reason: _Reason) -> None:
        self.reason = reason
        super().__init__(reason)


@contextmanager
def _io_errors() -> Iterator[None]:
    """Map cache-owned operations only, never the reservation caller's body."""
    try:
        yield
    except PythonProjectCacheError:
        raise
    except PythonProjectCopyError as error:
        if error.reason == "copy_timeout":
            raise PythonProjectCacheError("cache_timeout") from None
        if error.reason == "copy_limit":
            raise PythonProjectCacheError("cache_quota") from None
        raise PythonProjectCacheError("cache_changed") from None
    except OSError as error:
        reason: _Reason = (
            "cache_quota" if error.errno in {errno.ENOSPC, errno.EDQUOT} else "cache_unsafe"
        )
        raise PythonProjectCacheError(reason) from None
    except (ValueError, RuntimeError, TypeError, OverflowError, KeyError, AttributeError):
        raise PythonProjectCacheError("cache_unsafe") from None


def _need(condition: object, reason: _Reason = "cache_unsafe") -> None:
    if not condition:
        raise PythonProjectCacheError(reason)


def _check(deadline: float) -> None:
    _need(
        type(deadline) in {int, float} and math.isfinite(deadline) and deadline > 0, "cache_invalid"
    )
    _need(time.monotonic() < deadline, "cache_timeout")


def _native() -> None:
    if sys.platform != "linux":
        raise PythonProjectCacheError("cache_unsupported")
    _need(os.geteuid() == 0, "cache_unsupported")


def _scope(invocation: PythonProjectInvocation) -> PythonProjectScope:
    _need(type(invocation) is PythonProjectInvocation, "cache_invalid")
    item = PythonProjectInvocation.model_validate(invocation.model_dump(), strict=True)
    return PythonProjectScope(
        tenant_id=item.tenant_id,
        run_id=item.run_id,
        project_id=item.project_id,
        session_id=item.session_id,
        original_reference=item.original_reference,
    )


def python_project_cache_key(invocation: PythonProjectInvocation) -> str:
    payload = {
        "scope": _scope(invocation).model_dump(mode="json"),
        "recipe_sha256": invocation.recipe_sha256,
        "environment_input_sha256": invocation.environment_input_sha256,
    }
    return hashlib.sha256(_encoded(payload)).hexdigest()


def _reference(value: object) -> PythonEnvironmentReference:
    try:
        return PythonEnvironmentReference.model_validate_json(_encoded(value), strict=True)
    except (ValueError, TypeError, RuntimeError, OverflowError):
        raise PythonProjectCacheError("cache_changed") from None


def _expected(invocation: PythonProjectInvocation, reference: PythonEnvironmentReference) -> None:
    _need(type(reference) is PythonEnvironmentReference, "cache_invalid")
    _need(
        _reference(reference.model_dump(mode="json")) == reference
        and reference.scope == _scope(invocation)
        and reference.recipe_sha256 == invocation.recipe_sha256
        and reference.environment_input_sha256 == invocation.environment_input_sha256,
        "cache_changed",
    )


def _bind_reference(reference: PythonEnvironmentReference, reservation: dict[str, Any]) -> None:
    _need(
        reference.scope.model_dump(mode="json") == reservation["scope"]
        and reference.recipe_sha256 == reservation["recipe_sha256"]
        and reference.environment_input_sha256 == reservation["environment_input_sha256"]
        and reference.origin_execution_id == reservation["origin_execution_id"],
        "cache_changed",
    )


def _protected_runs(value: object) -> frozenset[tuple[UUID, UUID]]:
    _need(type(value) is frozenset, "cache_invalid")
    assert isinstance(value, frozenset)
    _need(
        len(value) <= 100_000
        and all(
            type(pair) is tuple
            and len(pair) == 2
            and type(pair[0]) is UUID
            and type(pair[1]) is UUID
            for pair in value
        ),
        "cache_invalid",
    )
    return frozenset((pair[0], pair[1]) for pair in value)


def _names(parent: int, maximum: int, deadline: float) -> set[str]:
    result: set[str] = set()
    with os.scandir(parent) as members:
        for member in members:
            _check(deadline)
            result.add(member.name)
            _need(len(result) <= maximum, "cache_quota")
    return result


@dataclass(frozen=True, slots=True)
class PythonProjectCachePolicy:
    max_bytes: int
    max_entries: int
    disk_reserve_bytes: int
    retention_seconds: int

    def __post_init__(self) -> None:
        for value, minimum, maximum in (
            (self.max_bytes, 1, 2**63 - 1),
            (self.max_entries, 1, 512),
            (self.disk_reserve_bytes, 0, 2**63 - 1),
            (self.retention_seconds, 1, 86400),
        ):
            _need(type(value) is int and minimum <= value <= maximum, "cache_invalid")

    def require_capacity(
        self, *, used_bytes: int, entries: int, next_bytes: int, free_bytes: int
    ) -> None:
        values = (used_bytes, entries, next_bytes, free_bytes)
        _need(
            all(type(value) is int and 0 <= value <= 2**63 - 1 for value in values), "cache_quota"
        )
        required = next_bytes + _CONTROL_ALLOWANCE
        _need(
            next_bytes > 0
            and entries < self.max_entries
            and required <= self.max_bytes - used_bytes
            and required <= free_bytes - self.disk_reserve_bytes,
            "cache_quota",
        )


def _write(parent: int, name: str, value: object) -> None:
    if sys.platform != "linux":
        raise PythonProjectCacheError("cache_unsupported")
    raw = _encoded(value)
    _need(0 < len(raw) <= _CONTROL_LIMIT, "cache_quota")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(name, flags, 0o600, dir_fd=parent)
    try:
        _need(stat.S_IMODE(os.fstat(fd).st_mode) == 0o600)
        _write_all(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(parent)


def _read(parent: int, name: str, pins: list[_Observed]) -> dict[str, Any]:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _need(0 < before.st_size <= _CONTROL_LIMIT)
    raw = _read_control(parent, name, 0, pins)
    _same(parent, name, before)
    value: object = json.loads(raw)
    _need(type(value) is dict and _encoded(value) == raw)
    assert isinstance(value, dict)
    return value


def _manifest_sha(observation: PythonProjectEnvironmentObservation) -> str:
    def plain(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [plain(item) for item in value]
        return value

    return hashlib.sha256(_encoded(plain(observation.security_manifest))).hexdigest()


def _usage(parent: int, *, maximum: int, deadline: float) -> int:
    """Account actual files, including incomplete entries; never trust stored size."""
    visited = 0
    total = 0
    device = os.fstat(parent).st_dev
    identities: set[tuple[int, int]] = {(device, os.fstat(parent).st_ino)}

    def walk(fd: int, parts: tuple[str, ...]) -> None:
        nonlocal visited, total
        _need(len(parts) <= 66)
        before = os.fstat(fd)
        with os.scandir(fd) as members:
            for member in members:
                _check(deadline)
                visited += 1
                _need(visited <= maximum, "cache_quota")
                info = os.stat(member.name, dir_fd=fd, follow_symlinks=False)
                identity = (info.st_dev, info.st_ino)
                _need(info.st_uid == 0 and info.st_dev == device and identity not in identities)
                identities.add(identity)
                relative = (*parts, member.name)
                if not parts:
                    _need(
                        member.name == "cache.lock"
                        or (
                            _SHA.fullmatch(member.name) is not None
                            and stat.S_ISDIR(info.st_mode)
                            and stat.S_IMODE(info.st_mode) == 0o700
                        )
                    )
                elif len(parts) == 1:
                    _need(
                        member.name in _READY | {"failed.json", "unpublished.json"}
                        or _ARCHIVE.fullmatch(member.name) is not None
                    )
                    _need(
                        stat.S_ISDIR(info.st_mode)
                        if member.name == "environment"
                        else (stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600)
                    )
                if stat.S_ISDIR(info.st_mode):
                    _need(not info.st_mode & 0o7022)
                    child = os.open(member.name, _directory_flags(), dir_fd=fd)
                    try:
                        pin = _Observed(fd, member.name, _stamp(info, full=True), True, child)
                        pin.check()
                        walk(child, relative)
                        pin.check()
                    finally:
                        os.close(child)
                elif stat.S_ISLNK(info.st_mode):
                    _need(
                        len(relative) == 3
                        and relative[1:] == ("environment", "lib64")
                        and info.st_nlink == 1
                        and os.readlink(member.name, dir_fd=fd) == "lib"
                    )
                    total += info.st_size
                    _same(fd, member.name, info)
                else:
                    _need(
                        stat.S_ISREG(info.st_mode)
                        and info.st_nlink == 1
                        and stat.S_IMODE(info.st_mode) in {0o600, 0o444, 0o555}
                    )
                    total += info.st_size
                    _same(fd, member.name, info)
                _need(total <= 2**63 - 1, "cache_quota")
        _need(_stamp(os.fstat(fd), full=True) == _stamp(before, full=True))

    walk(parent, ())
    return total


def _reservation(
    parent: int,
    key: str,
    policy: PythonProjectCachePolicy,
    pins: list[_Observed],
) -> dict[str, Any]:
    value = _read(parent, "reservation.json", pins)
    return _validate_reservation(value, key, policy.max_bytes - _CONTROL_ALLOWANCE)


def _validate_reservation(value: dict[str, Any], key: str, max_bytes: int) -> dict[str, Any]:
    _need(
        set(value)
        == {
            "schema_version",
            "scope",
            "recipe_sha256",
            "environment_input_sha256",
            "origin_execution_id",
            "expires_at",
            "environment_limit_bytes",
        }
    )
    _need(
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and type(value["expires_at"]) is int
        and 0 < value["expires_at"] < 2**63
        and type(value["environment_limit_bytes"]) is int
        and 0 < value["environment_limit_bytes"] <= max_bytes
        and type(value["origin_execution_id"]) is str
        and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", value["origin_execution_id"]) is not None
    )
    _need(
        all(
            type(value[field]) is str and _SHA.fullmatch(value[field]) is not None
            for field in ("recipe_sha256", "environment_input_sha256")
        )
    )
    scope = PythonProjectScope.model_validate_json(_encoded(value["scope"]), strict=True)
    payload = {
        "scope": scope.model_dump(mode="json"),
        "recipe_sha256": value["recipe_sha256"],
        "environment_input_sha256": value["environment_input_sha256"],
    }
    _need(hashlib.sha256(_encoded(payload)).hexdigest() == key, "cache_changed")
    return value


def _validate_failed_outcome(value: object, reservation: dict[str, Any]) -> None:
    """Validate root-owned safe history, not JSON claims as live process evidence."""
    _need(type(value) is dict)
    assert isinstance(value, dict)
    _need(
        set(value)
        == {
            "schema_version",
            "state",
            "phase",
            "execution_id",
            "reservation_sha256",
            "exit_code",
            "timed_out",
            "unit_quiet",
            "export_empty",
        }
    )
    _need(
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["state"] == "failed_quiescent"
        and value["phase"] == "install"
        and value["execution_id"] == reservation["origin_execution_id"]
        and value["reservation_sha256"] == hashlib.sha256(_encoded(reservation)).hexdigest()
        and type(value["exit_code"]) is int
        and value["exit_code"] != 0
        and value["timed_out"] is False
        and value["unit_quiet"] is True
        and value["export_empty"] is True,
        "cache_incomplete",
    )


def _validate_unpublished_outcome(value: object, reservation: dict[str, Any]) -> None:
    """Known capacity abort, never a failed install or a fresh execution grant."""
    _need(type(value) is dict, "cache_incomplete")
    assert isinstance(value, dict)
    _need(set(value) == {"schema_version", "state", "phase", "execution_id",
        "reservation_sha256", "unit_quiet", "cache_unpublished", "export_disposition", "reason"}
        and type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["state"] == "unpublished_capacity" and value["phase"] == "publish"
        and value["execution_id"] == reservation["origin_execution_id"]
        and value["reservation_sha256"] == hashlib.sha256(_encoded(reservation)).hexdigest()
        and value["unit_quiet"] is True and value["cache_unpublished"] is True
        and value["export_disposition"] == "broker_cleanup"
        and value["reason"] == "native_export_limit", "cache_incomplete")


def _entry_members(
    parent: int, reservation: dict[str, Any], pins: list[_Observed], deadline: float
) -> set[str]:
    names = _names(parent, _MAX_MEMBERS, deadline)
    archives = {name for name in names if _ARCHIVE.fullmatch(name)}
    origins = {match[1] for name in archives if (match := _ARCHIVE.fullmatch(name))}
    _need(len(origins) <= _MAX_FAILURES)
    if archives:
        payload = {
            field: reservation[field]
            for field in ("scope", "recipe_sha256", "environment_input_sha256")
        }
        key = hashlib.sha256(_encoded(payload)).hexdigest()
        for origin in sorted(origins):
            _check(deadline)
            pair = {f"failed-{origin}.reservation.json", f"failed-{origin}.outcome.json"}
            _need(
                pair <= archives and origin != reservation["origin_execution_id"],
                "cache_incomplete",
            )
            previous = _validate_reservation(
                _read(parent, f"failed-{origin}.reservation.json", pins), key, 2**63 - 1
            )
            _need(previous["origin_execution_id"] == origin, "cache_changed")
            _validate_failed_outcome(_read(parent, f"failed-{origin}.outcome.json", pins), previous)
    if "unpublished.json" in names:
        _need(names - archives == {"reservation.json", "unpublished.json"}, "cache_incomplete")
        _validate_unpublished_outcome(_read(parent, "unpublished.json", pins), reservation)
    return names - archives


@dataclass(frozen=True, slots=True)
class _Ready:
    reference: PythonEnvironmentReference
    environment_fd: int
    snapshot: dict[str, os.stat_result]


def _ready(
    parent: int,
    path: Path,
    reservation: dict[str, Any],
    limits: ScanLimits,
    stack: ExitStack,
    pins: list[_Observed],
    deadline: float,
) -> _Ready:
    _need(_entry_members(parent, reservation, pins, deadline) == _READY, "cache_incomplete")
    reference = _reference(_read(parent, "reference.json", pins))
    _bind_reference(reference, reservation)
    env_fd = _open_directory(parent, "environment", 0, stack, pins, mode=0o555, full=True)
    captured = _snapshot(env_fd, 0, limits, deadline)
    for info in captured.values():
        _check(deadline)
        if not stat.S_ISLNK(info.st_mode):
            _need(stat.S_IMODE(info.st_mode) == (0o555 if info.st_mode & 0o111 else 0o444))
    actual = _observe(path / "environment", reference.environment_input_sha256, limits, deadline)
    _need(
        actual.environment_sha256 == reference.environment_sha256
        and _manifest_sha(actual) == reference.security_manifest_sha256,
        "cache_changed",
    )
    python_sha: str | None = None
    members = actual.security_manifest["entries"]
    _need(isinstance(members, tuple))
    assert isinstance(members, tuple)
    for member in members:
        _check(deadline)
        if isinstance(member, Mapping) and member.get("path") == "bin/python":
            value = member.get("sha256")
            _need(isinstance(value, str))
            assert isinstance(value, str)
            python_sha = value
    _need(python_sha is not None)
    assert python_sha is not None
    _recipe_entries(actual.security_manifest, python_sha)
    _need(_same_snapshots(captured, _snapshot(env_fd, 0, limits, deadline)), "cache_changed")
    for pin in pins:
        _check(deadline)
        pin.check()
    return _Ready(reference, env_fd, captured)


def _erase_ready(parent: int, key: str, entry: int, ready: _Ready, deadline: float) -> None:
    """After full READY validation: rooted unlink/rmdir; chmod held directories only."""
    if sys.platform != "linux":
        raise PythonProjectCacheError("cache_unsupported")
    snapshot = ready.snapshot
    entry_info = os.fstat(entry)
    controls = {
        name: os.stat(name, dir_fd=entry, follow_symlinks=False)
        for name in _names(entry, _MAX_MEMBERS, deadline) - {"environment"}
    }
    for info in controls.values():
        _need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600
        )
    children: dict[str, list[str]] = {}
    for path in snapshot:
        _check(deadline)
        if path != ".":
            children.setdefault(PurePosixPath(path).parent.as_posix(), []).append(path)

    def remove(fd: int, path: str) -> None:
        _check(deadline)
        _need(_stamp(os.fstat(fd), full=True) == _stamp(snapshot[path], full=True), "cache_changed")
        os.fchmod(fd, 0o700)
        for relative in children.get(path, ()):
            _check(deadline)
            name = PurePosixPath(relative).name
            info = snapshot[relative]
            _same(fd, name, info)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, _directory_flags(), dir_fd=fd)
                try:
                    _same(fd, name, info, child)
                    remove(child, relative)
                    current = os.fstat(child)
                    _need(
                        (current.st_dev, current.st_ino, current.st_uid)
                        == (info.st_dev, info.st_ino, 0)
                    )
                    _same(fd, name, current, child)
                    os.rmdir(name, dir_fd=fd)
                finally:
                    os.close(child)
            else:
                _need(info.st_nlink == 1)
                if stat.S_ISLNK(info.st_mode):
                    _need(relative == "lib64" and os.readlink(name, dir_fd=fd) == "lib")
                else:
                    _need(stat.S_ISREG(info.st_mode))
                os.unlink(name, dir_fd=fd)
        _need(not _names(fd, 1, deadline), "cache_changed")
        os.fsync(fd)

    _same(parent, key, entry_info, entry)
    _same(entry, "environment", snapshot["."], ready.environment_fd)
    remove(ready.environment_fd, ".")
    _same(entry, "environment", os.fstat(ready.environment_fd), ready.environment_fd)
    os.rmdir("environment", dir_fd=entry)
    for name, info in controls.items():
        _check(deadline)
        _same(entry, name, info)
        os.unlink(name, dir_fd=entry)
    _need(not _names(entry, 1, deadline), "cache_changed")
    _Observed(parent, key, _stamp(entry_info, full=False), False, entry).check()
    os.rmdir(key, dir_fd=parent)
    os.fsync(parent)
    _check(deadline)


@dataclass(slots=True)
class PythonProjectCacheLease:
    """Only valid within reserve(); not serializable authority or a model argument."""

    path: Path
    invocation: PythonProjectInvocation
    limits: ScanLimits
    reservation: dict[str, Any]
    _parent: int
    _pins: list[_Observed]
    _cache: PythonProjectEnvironmentCache
    _root_parent: int
    _closed: bool = False
    _fresh: bool = False

    def _check(self, deadline: float) -> None:
        _need(not self._closed, "cache_invalid")
        _check(deadline)
        for pin in self._pins:
            pin.check()
        # Retention age permits trusted GC; it is not an execution/inspection grant.

    @_io_errors()
    def current(
        self, *, deadline: float, expected: PythonEnvironmentReference | None = None
    ) -> PythonEnvironmentReference | None:
        self._check(deadline)
        if expected is not None:
            _expected(self.invocation, expected)
        if _entry_members(self._parent, self.reservation, self._pins, deadline) == {
            "reservation.json"
        }:
            _need(self._fresh and expected is None, "cache_incomplete")
            return None
        with ExitStack() as stack:
            pins: list[_Observed] = []
            ready = _ready(
                self._parent, self.path, self.reservation, self.limits, stack, pins, deadline
            )
            _expected(self.invocation, ready.reference)
            _need(expected is None or ready.reference == expected, "cache_changed")
            self._check(deadline)
            return ready.reference

    @_io_errors()
    def record_install_failure(
        self,
        phase: PythonPhaseProcessResult,
        *,
        execution: _Execution,
        deadline: float,
    ) -> None:
        """Trusted backend only, after actual manager quiescence; never accept model JSON.

        Timed-out/malformed/successful phases remain UNKNOWN. An empty export is
        measured again through a held root stage FD before the once-only marker.
        """
        self._check(deadline)
        _need(
            self._fresh
            and self.invocation.install_allowed
            and self.current(deadline=deadline) is None,
            "cache_incomplete",
        )
        _need(
            type(phase) is PythonPhaseProcessResult
            and type(phase.exit_code) is int
            and phase.exit_code != 0
            and phase.timed_out is False,
            "cache_incomplete",
        )
        _need(
            type(execution) is _Execution
            and execution.quiet is True
            and execution.launched is True
            and execution.invocation == base_python_invocation(self.invocation)
            and execution.stage is not None,
            "cache_incomplete",
        )
        assert execution.stage is not None
        with ExitStack() as stack:
            pins: list[_Observed] = []
            stage = _root(execution.stage, 0, stack, pins, stage=True)
            export = _open_directory(stage, "export", 0, stack, pins, mode=0o1777, full=True)
            _need(not _names(export, 1, deadline), "cache_incomplete")
            value = {
                "schema_version": 1,
                "state": "failed_quiescent",
                "phase": "install",
                "execution_id": self.invocation.execution_id,
                "reservation_sha256": hashlib.sha256(_encoded(self.reservation)).hexdigest(),
                "exit_code": phase.exit_code,
                "timed_out": False,
                "unit_quiet": True,
                "export_empty": True,
            }
            _validate_failed_outcome(value, self.reservation)
            for pin in pins:
                pin.check()
            self._check(deadline)
            self._cache._capacity(self._root_parent, deadline, additional=len(_encoded(value)))
            _write(self._parent, "failed.json", value)
            for pin in pins:
                pin.check()
        self._fresh = False

    @_io_errors()
    def record_unpublished_capacity(self, *, execution: _Execution, deadline: float) -> None:
        """After a pre-copy native limit refusal; broker alone owns export cleanup.

        No environment/reference may have been created in the cache. A recorded
        abort remains consumed, with no failed-install recovery or automatic GC.
        """
        self._check(deadline)
        _need(self._fresh and self.invocation.install_allowed
            and type(execution) is _Execution and execution.quiet is True
            and execution.launched is True and execution.stage is not None
            and execution.invocation == base_python_invocation(self.invocation), "cache_incomplete")
        _need(self.current(deadline=deadline) is None, "cache_incomplete")
        assert execution.stage is not None
        with ExitStack() as stack:
            pins: list[_Observed] = []
            stage = _root(execution.stage, 0, stack, pins, stage=True)
            _open_directory(stage, "export", 0, stack, pins, mode=0o1777)
            value = {"schema_version": 1, "state": "unpublished_capacity", "phase": "publish",
                "execution_id": self.invocation.execution_id,
                "reservation_sha256": hashlib.sha256(_encoded(self.reservation)).hexdigest(),
                "unit_quiet": True, "cache_unpublished": True,
                "export_disposition": "broker_cleanup", "reason": "native_export_limit"}
            _validate_unpublished_outcome(value, self.reservation)
            for pin in pins:
                pin.check()
            self._check(deadline)
            self._cache._capacity(self._root_parent, deadline, additional=len(_encoded(value)))
            _write(self._parent, "unpublished.json", value)
            for pin in pins:
                pin.check()
        self._fresh = False

    @_io_errors()
    def publish(
        self,
        source: Path,
        *,
        source_owner: int,
        interpreter: Path,
        interpreter_sha256: str,
        expected_environment_sha256: str,
        deadline: float,
    ) -> PythonEnvironmentReference:
        self._check(deadline)
        _need(self._fresh and self.invocation.install_allowed, "cache_incomplete")
        _need(self.current(deadline=deadline) is None, "cache_incomplete")
        actual = copy_python_project_environment(
            source,
            self.path / "environment",
            source_owner=source_owner,
            interpreter=interpreter,
            interpreter_sha256=interpreter_sha256,
            environment_input_sha256=self.invocation.environment_input_sha256,
            expected_environment_sha256=expected_environment_sha256,
            limits=self.limits,
            deadline=deadline,
        )
        reference = PythonEnvironmentReference(
            scope=_scope(self.invocation),
            recipe_sha256=self.invocation.recipe_sha256,
            environment_input_sha256=self.invocation.environment_input_sha256,
            environment_sha256=actual.environment_sha256,
            security_manifest_sha256=_manifest_sha(actual),
            origin_execution_id=self.invocation.execution_id,
        )
        self._check(deadline)
        self._cache._capacity(
            self._root_parent, deadline, additional=len(_encoded(reference.model_dump(mode="json")))
        )
        _write(self._parent, "reference.json", reference.model_dump(mode="json"))
        self._fresh = False
        result = self.current(deadline=deadline, expected=reference)
        assert result is not None
        return result


@dataclass(frozen=True)
class PythonProjectExpiredEnvironment:
    """Root-measured READY selection; never supplied over the service RPC."""

    reference: PythonEnvironmentReference
    reservation_sha256: str


class PythonProjectEnvironmentCache:
    def __init__(self, root: Path, policy: PythonProjectCachePolicy) -> None:
        _need(
            isinstance(root, Path)
            and root.is_absolute()
            and ".." not in root.parts
            and "\0" not in str(root)
            and len(os.fsencode(root)) <= 4096
            and len(root.parts) <= 64,
            "cache_invalid",
        )
        _need(type(policy) is PythonProjectCachePolicy, "cache_invalid")
        self.root = root
        self.policy = policy

    @contextmanager
    def _locked(
        self, deadline: float, *, create: bool = True
    ) -> Iterator[tuple[int, ExitStack, list[_Observed]] | None]:
        resources = self._lock_resources(deadline, create=create)
        locked = resources.__enter__()
        failed = False
        try:
            yield locked
        except BaseException:
            failed = True
            raise
        finally:
            try:
                # Normal exit checks held identities and closes every resource. The
                # caller exception must never enter the internal error mapper.
                resources.__exit__(None, None, None)
            except BaseException:
                if not failed:
                    raise

    @contextmanager
    def _lock_resources(
        self, deadline: float, *, create: bool = True
    ) -> Iterator[tuple[int, ExitStack, list[_Observed]] | None]:
        _native()
        _check(deadline)
        if sys.platform != "linux":
            raise PythonProjectCacheError("cache_unsupported")
        import fcntl

        with _io_errors(), ExitStack() as stack:
            pins: list[_Observed] = []
            parent = _root(self.root, 0, stack, pins, stage=True)
            flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            flags |= (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
            try:
                fd = os.open("cache.lock", flags, 0o600, dir_fd=parent)
            except FileNotFoundError:
                _need(not create and not _names(parent, 1, deadline), "cache_incomplete")
                yield None
                for pin in pins:
                    pin.check()
                return
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            _need(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == 0
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1
                and info.st_size == 0
            )
            lock_pin = _Observed(parent, "cache.lock", _stamp(info, full=True), True, fd)
            lock_pin.check()
            pins.append(lock_pin)
            while True:
                _check(deadline)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
            stack.callback(fcntl.flock, fd, fcntl.LOCK_UN)
            yield parent, stack, pins
            for pin in pins:
                pin.check()

    def _catalog(self, parent: int, deadline: float) -> set[str]:
        names = _names(parent, self.policy.max_entries + 1, deadline)
        _need(
            "cache.lock" in names
            and all(name == "cache.lock" or _SHA.fullmatch(name) for name in names)
        )
        return names - {"cache.lock"}

    def _capacity(self, parent: int, deadline: float, *, additional: int = 0) -> tuple[int, int]:
        if sys.platform != "linux":
            raise PythonProjectCacheError("cache_unsupported")
        used = _usage(
            parent,
            maximum=self.policy.max_entries * (100_010 + 2 * _MAX_FAILURES) + 1,
            deadline=deadline,
        )
        disk = os.fstatvfs(parent)
        free = disk.f_bavail * disk.f_frsize
        _need(
            used + additional <= self.policy.max_bytes
            and free - additional >= self.policy.disk_reserve_bytes,
            "cache_quota",
        )
        _check(deadline)
        return used, free

    @contextmanager
    def reserve(
        self, invocation: PythonProjectInvocation, *, next_bytes: int, deadline: float,
        adaptive: bool = False, minimum_bytes: int = 1,
        fresh_check: Callable[[], None] | None = None,
    ) -> Iterator[PythonProjectCacheLease]:
        key = python_project_cache_key(invocation)
        _need(type(adaptive) is bool and type(minimum_bytes) is int and minimum_bytes > 0
            and (fresh_check is None or callable(fresh_check)), "cache_invalid")
        _need(type(next_bytes) is int and 0 < next_bytes <= self.policy.max_bytes, "cache_quota")
        with self._locked(deadline) as locked:
            with _io_errors():
                assert locked is not None
                parent, stack, pins = locked
                names = self._catalog(parent, deadline)
                used, free = self._capacity(parent, deadline)
                def new_budget() -> int:
                    # Called only under the global cache lock, before any mutation.
                    selected = min(next_bytes, self.policy.max_bytes - used - _CONTROL_ALLOWANCE,
                        free - self.policy.disk_reserve_bytes - _CONTROL_ALLOWANCE) if adaptive else next_bytes
                    _need(selected >= minimum_bytes, "cache_quota")
                    if fresh_check is not None:
                        fresh_check()
                    return selected

                fresh = key not in names
                if fresh:
                    _need(invocation.install_allowed, "cache_incomplete")
                    next_bytes = new_budget()
                    self.policy.require_capacity(
                        used_bytes=used, entries=len(names), next_bytes=next_bytes, free_bytes=free
                    )
                    os.mkdir(key, 0o700, dir_fd=parent)
                directory = _open_directory(parent, key, 0, stack, pins, mode=0o700)
                if not fresh:
                    previous_pins: list[_Observed] = []
                    previous = _reservation(directory, key, self.policy, previous_pins)
                    members = _entry_members(directory, previous, previous_pins, deadline)
                    _need(
                        f"failed-{invocation.execution_id}.reservation.json"
                        not in _names(directory, _MAX_MEMBERS, deadline),
                        "cache_incomplete",
                    )
                    if members == {"reservation.json", "failed.json"}:
                        _need(invocation.install_allowed, "cache_incomplete")
                        outcome = _read(directory, "failed.json", previous_pins)
                        _validate_failed_outcome(outcome, previous)
                        origin = previous["origin_execution_id"]
                        history = _names(directory, _MAX_MEMBERS, deadline)
                        _need(
                            invocation.execution_id != origin
                            and f"failed-{invocation.execution_id}.reservation.json" not in history,
                            "cache_incomplete",
                        )
                        _need(len(history) < 2 + 2 * _MAX_FAILURES, "cache_quota")
                        next_bytes = new_budget()
                        self.policy.require_capacity(
                            used_bytes=used,
                            entries=len(names) - 1,
                            next_bytes=next_bytes,
                            free_bytes=free,
                        )
                        for pin in previous_pins:
                            pin.check()
                        # No overwrite or deletion: preserve each consumed execution.
                        # A crash between these writes leaves an unrecoverable partial.
                        for source, suffix in (
                            ("reservation.json", "reservation"),
                            ("failed.json", "outcome"),
                        ):
                            target = f"failed-{origin}.{suffix}.json"
                            _need(target not in history, "cache_incomplete")
                            os.rename(source, target, src_dir_fd=directory, dst_dir_fd=directory)
                            os.fsync(directory)
                        fresh = True
                    else:
                        _need(members == _READY, "cache_incomplete")
                        pins.extend(previous_pins)
                if fresh:
                    reservation: dict[str, Any] = {
                        "schema_version": 1,
                        "scope": _scope(invocation).model_dump(mode="json"),
                        "recipe_sha256": invocation.recipe_sha256,
                        "environment_input_sha256": invocation.environment_input_sha256,
                        "origin_execution_id": invocation.execution_id,
                        "environment_limit_bytes": next_bytes,
                        "expires_at": int(time.time()) + self.policy.retention_seconds,
                    }
                    _write(directory, "reservation.json", reservation)
                    os.fsync(parent)
                else:
                    reservation = _reservation(directory, key, self.policy, pins)
                    _need(
                        reservation["scope"] == _scope(invocation).model_dump(mode="json")
                        and reservation["recipe_sha256"] == invocation.recipe_sha256
                        and reservation["environment_input_sha256"]
                        == invocation.environment_input_sha256,
                        "cache_changed",
                    )
                lease = PythonProjectCacheLease(
                    self.root / key,
                    invocation,
                    ScanLimits(min(next_bytes, reservation["environment_limit_bytes"])),
                    reservation,
                    directory,
                    pins,
                    self,
                    parent,
                    _fresh=fresh,
                )
            try:
                if not fresh:
                    lease.current(deadline=deadline)
                yield lease
            finally:
                lease._closed = True

    @_io_errors()
    def inspect(
        self,
        invocation: PythonProjectInvocation,
        expected: PythonEnvironmentReference,
        *,
        deadline: float,
    ) -> PythonEnvironmentReference | None:
        """Read-only: never create a lock, reservation, entry or installation grant."""
        key = python_project_cache_key(invocation)
        _expected(invocation, expected)
        with self._locked(deadline, create=False) as locked:
            if locked is None:
                return None
            parent, stack, pins = locked
            names = self._catalog(parent, deadline)
            if key not in names:
                return None
            directory = _open_directory(parent, key, 0, stack, pins, mode=0o700)
            reservation = _reservation(directory, key, self.policy, pins)
            ready = _ready(
                directory,
                self.root / key,
                reservation,
                ScanLimits(reservation["environment_limit_bytes"]),
                stack,
                pins,
                deadline,
            )
            _need(ready.reference == expected, "cache_changed")
            return ready.reference

    @_io_errors()
    def inspect_expired(
        self, invocation: PythonProjectInvocation, *, deadline: float,
    ) -> PythonProjectExpiredEnvironment | None:
        """Select only this key, validating complete READY even before TTL check."""
        key = python_project_cache_key(invocation)
        _need(not invocation.install_allowed, 'cache_invalid')
        with self._locked(deadline, create=False) as locked:
            if locked is None:
                return None
            parent, stack, pins = locked
            if key not in self._catalog(parent, deadline):
                return None
            entry = _open_directory(parent, key, 0, stack, pins, mode=0o700)
            reservation = _reservation(entry, key, self.policy, pins)
            ready = _ready(entry, self.root / key, reservation,
                           ScanLimits(reservation['environment_limit_bytes']), stack, pins, deadline)
            _expected(invocation, ready.reference)
            if time.time() < reservation['expires_at']:
                return None
            for observed in pins:
                observed.check()
            return PythonProjectExpiredEnvironment(
                ready.reference, hashlib.sha256(_encoded(reservation)).hexdigest(),
            )

    def release_expired(
        self, invocation: PythonProjectInvocation, expected: PythonProjectExpiredEnvironment,
        *, deadline: float,
    ) -> bool:
        """Caller holds native admission and proves whole-run quiet; no broad GC."""
        key = python_project_cache_key(invocation)
        _need(type(expected) is PythonProjectExpiredEnvironment and not invocation.install_allowed
              and type(expected.reservation_sha256) is str
              and _SHA.fullmatch(expected.reservation_sha256) is not None, 'cache_invalid')
        _expected(invocation, expected.reference)
        mutating = False
        try:
            with _io_errors(), self._locked(deadline, create=False) as locked:
                if locked is None:
                    return False
                parent, stack, root_pins = locked
                if key not in self._catalog(parent, deadline):
                    return False
                # Entry pins become intentionally invalid when erased. Root/lock
                # identity checks still run on lock exit, as for completed release.
                pins: list[_Observed] = []
                entry = _open_directory(parent, key, 0, stack, pins, mode=0o700)
                reservation = _reservation(entry, key, self.policy, pins)
                _need(hashlib.sha256(_encoded(reservation)).hexdigest() == expected.reservation_sha256,
                      'cache_changed')
                _need(time.time() >= reservation['expires_at'], 'cache_changed')
                ready = _ready(entry, self.root / key, reservation,
                               ScanLimits(reservation['environment_limit_bytes']), stack, pins, deadline)
                _need(ready.reference == expected.reference, 'cache_changed')
                for observed in (*root_pins, *pins):
                    observed.check()
                mutating = True
                _erase_ready(parent, key, entry, ready, deadline)
                return True
        except BaseException:
            if mutating:
                raise ProcessCleanupIncomplete('python_project_cleanup_incomplete') from None
            raise

    @_io_errors()
    def prune_expired(
        self, *, protected_runs: frozenset[tuple[UUID, UUID]], deadline: float
    ) -> int:
        """Caller fences active/retained/UNKNOWN runs; remove complete expired READY only."""
        protected = _protected_runs(protected_runs)
        removed = 0
        with self._locked(deadline, create=False) as locked:
            if locked is None:
                return 0
            parent, _, root_pins = locked
            names = self._catalog(parent, deadline)
            for key in sorted(names):
                _check(deadline)
                with ExitStack() as stack:
                    pins: list[_Observed] = []
                    entry = _open_directory(parent, key, 0, stack, pins, mode=0o700)
                    reservation = (
                        _reservation(entry, key, self.policy, pins)
                        if "reservation.json" in _names(entry, _MAX_MEMBERS, deadline)
                        else None
                    )
                    if reservation is None:
                        continue
                    members = _entry_members(entry, reservation, pins, deadline)
                    if members != _READY:
                        _need(members <= _READY | {"failed.json", "unpublished.json"})
                        continue
                    reference = _reference(_read(entry, "reference.json", pins))
                    _bind_reference(reference, reservation)
                    run = (reference.scope.tenant_id, reference.scope.run_id)
                    if run in protected or time.time() < reservation["expires_at"]:
                        continue
                    ready = _ready(
                        entry,
                        self.root / key,
                        reservation,
                        ScanLimits(reservation["environment_limit_bytes"]),
                        stack,
                        pins,
                        deadline,
                    )
                    for pin in root_pins:
                        pin.check()
                    _erase_ready(parent, key, entry, ready, deadline)
                    removed += 1
            return removed

    @_io_errors()
    def release_completed(
        self,
        invocation: PythonProjectInvocation,
        expected: PythonEnvironmentReference,
        *,
        deadline: float,
    ) -> bool:
        """Trusted terminal lifecycle only; never expose as a model action/boolean.

        The caller guarantees execution has ended and no retained consumer needs
        this scope. Invocation/ref/expiry do not establish that lifecycle authority.
        """
        key = python_project_cache_key(invocation)
        _expected(invocation, expected)
        with self._locked(deadline, create=False) as locked:
            if locked is None:
                return False
            parent, _, root_pins = locked
            names = self._catalog(parent, deadline)
            if key not in names:
                return False
            with ExitStack() as stack:
                pins: list[_Observed] = []
                entry = _open_directory(parent, key, 0, stack, pins, mode=0o700)
                reservation = _reservation(entry, key, self.policy, pins)
                ready = _ready(
                    entry,
                    self.root / key,
                    reservation,
                    ScanLimits(reservation["environment_limit_bytes"]),
                    stack,
                    pins,
                    deadline,
                )
                _need(ready.reference == expected, "cache_changed")
                for pin in root_pins:
                    pin.check()
                _erase_ready(parent, key, entry, ready, deadline)
                return True
