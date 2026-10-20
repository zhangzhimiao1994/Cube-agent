"""Root-only bounded selected-wheel copies; never expose the shared catalog.

Copies are immutable stage data, not grants. No archive extraction, import,
installation, project execution or cleanup occurs here. Errors are path-free.
"""

from __future__ import annotations

import errno
import hashlib
import math
import os
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from agent_hub.capabilities.python_project_wheels import PythonWheelBinding
from agent_hub.capability_installer.python_attestation import ScanLimits
from agent_hub.skills.sandbox.project_contract_broker import (
    _Observed,
    _root,
    _seal_readonly,
    _stamp,
    _write_all,
)
from agent_hub.skills.sandbox.python_project_copy import _directory_flags, _same


class PythonWheelStageError(ValueError):
    pass


def _need(value: object, code: str = "wheel_stage_unsafe") -> None:
    if not value:
        raise PythonWheelStageError(code)


def _check(deadline: float) -> None:
    _need(time.monotonic() < deadline, "wheel_stage_timeout")


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except PythonWheelStageError:
        raise
    except OSError as error:
        code = (
            "wheel_stage_limit"
            if error.errno in {errno.ENOSPC, errno.EDQUOT}
            else "wheel_stage_unsafe"
        )
        raise PythonWheelStageError(code) from None
    except (ValueError, TypeError, RuntimeError, OverflowError, AttributeError):
        raise PythonWheelStageError("wheel_stage_unsafe") from None


def _validate_selection(
    selected: tuple[PythonWheelBinding, ...], limits: ScanLimits, deadline: float
) -> None:
    _need(
        type(deadline) in {int, float} and math.isfinite(deadline) and deadline > 0,
        "wheel_stage_invalid",
    )
    _need(
        type(limits) is ScanLimits
        and all(
            type(value) is int and value > 0
            for value in (
                limits.max_bytes,
                limits.max_entries,
                limits.max_depth,
                limits.max_path_bytes,
            )
        ),
        "wheel_stage_invalid",
    )
    _need(
        type(selected) is tuple and len(selected) <= min(128, limits.max_entries),
        "wheel_stage_limit",
    )
    filenames: set[str] = set()
    names: set[str] = set()
    for wheel in selected:
        _check(deadline)
        _need(type(wheel) is PythonWheelBinding, "wheel_stage_invalid")
        _need(
            PythonWheelBinding(wheel.filename, wheel.name, wheel.version, wheel.sha256) == wheel,
            "wheel_stage_invalid",
        )
        _need(wheel.filename not in filenames and wheel.name not in names, "wheel_stage_invalid")
        _need(len(wheel.filename.encode("utf-8")) <= limits.max_path_bytes, "wheel_stage_limit")
        filenames.add(wheel.filename)
        names.add(wheel.name)


def _path(root: Path) -> None:
    _need(
        isinstance(root, Path)
        and root.is_absolute()
        and ".." not in root.parts
        and "\0" not in str(root)
        and len(os.fsencode(root)) <= 4096
        and len(root.parts) <= 64,
        "wheel_stage_invalid",
    )


def _native() -> None:
    if sys.platform != "linux":
        raise PythonWheelStageError("wheel_stage_unsupported")
    _need(os.geteuid() == 0, "wheel_stage_unsupported")


def _file(info: os.stat_result, *, sealed: bool) -> None:
    if sys.platform != "linux":
        raise PythonWheelStageError("wheel_stage_unsupported")
    _need(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == 0
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) in ({0o444} if sealed else {0o444, 0o644})
    )


def _members(fd: int, maximum: int, deadline: float) -> set[str]:
    result: set[str] = set()
    with os.scandir(fd) as members:
        for member in members:
            _check(deadline)
            result.add(member.name)
            _need(len(result) <= maximum, "wheel_stage_limit")
    return result


def _digest(fd: int, size: int, deadline: float) -> str:
    digest = hashlib.sha256()
    count = 0
    while True:
        _check(deadline)
        chunk = os.read(fd, min(1024**2, size - count + 1))
        if not chunk:
            break
        count += len(chunk)
        _need(count <= size, "wheel_stage_changed")
        digest.update(chunk)
    _need(count == size, "wheel_stage_changed")
    return digest.hexdigest()


def verify_staged_python_wheels(
    root: Path, selected: tuple[PythonWheelBinding, ...], *, limits: ScanLimits, deadline: float
) -> None:
    with _errors():
        _validate_selection(selected, limits, deadline)
        _path(root)
        _native()
        if sys.platform != "linux":
            raise PythonWheelStageError("wheel_stage_unsupported")
        with ExitStack() as stack:
            pins: list[_Observed] = []
            directory = _root(root, 0, stack, pins)
            directory_identity = _identity(os.fstat(directory))
            _need(stat.S_IMODE(os.fstat(directory).st_mode) == 0o555)
            _need(
                _members(directory, limits.max_entries, deadline)
                == {wheel.filename for wheel in selected}
            )
            total = 0
            for wheel in selected:
                info = os.stat(wheel.filename, dir_fd=directory, follow_symlinks=False)
                _file(info, sealed=True)
                total += info.st_size
                _need(total <= limits.max_bytes, "wheel_stage_limit")
                fd = os.open(
                    wheel.filename,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=directory,
                )
                try:
                    _same(directory, wheel.filename, info, fd)
                    _need(
                        _digest(fd, info.st_size, deadline) == wheel.sha256, "wheel_stage_changed"
                    )
                    _same(directory, wheel.filename, info, fd)
                    pins.append(_Observed(directory, wheel.filename, _identity(info), True))
                finally:
                    os.close(fd)
            for pin in pins:
                _check(deadline)
                pin.check()
            _need(_identity(os.fstat(directory)) == directory_identity, "wheel_stage_changed")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return _stamp(info, full=True)


def stage_python_project_wheels(
    catalog: Path,
    stage: Path,
    selected: tuple[PythonWheelBinding, ...],
    *,
    limits: ScanLimits,
    deadline: float,
    disk_reserve_bytes: int = 0,
) -> Path:
    """Copy only selected regular wheels, verify bytes/identities, seal 0444/0555.

    Catalog file modes are deliberately restricted to root-owned 0444 or 0644.
    FD concurrency is bounded by path depth plus two file descriptors.
    """
    with _errors():
        _validate_selection(selected, limits, deadline)
        _path(catalog)
        _path(stage)
        _need(
            not catalog.is_relative_to(stage) and not stage.is_relative_to(catalog),
            "wheel_stage_invalid",
        )
        _need(
            type(disk_reserve_bytes) is int and 0 <= disk_reserve_bytes < 2**63,
            "wheel_stage_invalid",
        )
        _native()
        if sys.platform != "linux":
            raise PythonWheelStageError("wheel_stage_unsupported")
        with ExitStack() as stack:
            pins: list[_Observed] = []
            source = _root(catalog, 0, stack, pins)
            parent = _root(stage, 0, stack, pins, stage=True)
            snapshots: dict[str, os.stat_result] = {}
            total = 0
            for wheel in selected:
                _check(deadline)
                info = os.stat(wheel.filename, dir_fd=source, follow_symlinks=False)
                _file(info, sealed=False)
                total += info.st_size
                _need(total <= limits.max_bytes, "wheel_stage_limit")
                snapshots[wheel.filename] = info
            disk = os.fstatvfs(parent)
            _need(disk.f_bavail * disk.f_frsize >= total + disk_reserve_bytes, "wheel_stage_limit")
            os.mkdir("selected-wheels", 0o700, dir_fd=parent)
            target = os.open("selected-wheels", _directory_flags(), dir_fd=parent)
            stack.callback(os.close, target)
            directory_info = os.fstat(target)
            _need(directory_info.st_uid == 0 and stat.S_IMODE(directory_info.st_mode) == 0o700)
            directory_pin = _Observed(
                parent, "selected-wheels", _stamp(directory_info, full=False), False, target
            )
            directory_pin.check()
            for wheel in selected:
                info = snapshots[wheel.filename]
                fd = os.open(
                    wheel.filename,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=source,
                )
                try:
                    _same(source, wheel.filename, info, fd)
                    output = os.open(
                        wheel.filename,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=target,
                    )
                    try:
                        count = 0
                        digest = hashlib.sha256()
                        while True:
                            _check(deadline)
                            chunk = os.read(fd, min(1024**2, info.st_size - count + 1))
                            if not chunk:
                                break
                            count += len(chunk)
                            _need(count <= info.st_size, "wheel_stage_changed")
                            digest.update(chunk)
                            _write_all(output, chunk)
                        _need(
                            count == info.st_size and digest.hexdigest() == wheel.sha256,
                            "wheel_stage_changed",
                        )
                        _same(source, wheel.filename, info, fd)
                        pins.append(_Observed(source, wheel.filename, _identity(info), True))
                        _seal_readonly(output, 0o444)
                        _file(os.fstat(output), sealed=True)
                        _same(target, wheel.filename, os.fstat(output), output)
                    finally:
                        os.close(output)
                finally:
                    os.close(fd)
            for pin in pins:
                _check(deadline)
                pin.check()
            directory_pin.check()
            os.fchmod(target, 0o555)
            os.fsync(target)
            _same(parent, "selected-wheels", os.fstat(target), target)
            os.fsync(parent)
        result = stage / "selected-wheels"
        verify_staged_python_wheels(result, selected, limits=limits, deadline=deadline)
        return result
