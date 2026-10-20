"""Root-only, no-clobber wheel publication; caller holds stage_root/active.lock.

All v4 cache readers must share that admission lock. This module neither fetches
nor executes packages and never creates temporary entries inside the flat cache.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import math
import os
import re
import secrets
import stat
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.python_input_fetch import WheelDownload


class PythonInputPublishError(RuntimeError):
    """Known pre-publication rejection; message is a finite, non-sensitive code."""


def _need(condition: object, code: str = "python_input_publish_unsafe") -> None:
    if not condition:
        raise PythonInputPublishError(code)


def _deadline(deadline: float) -> None:
    try:
        valid = (type(deadline) in {int, float} and math.isfinite(deadline)
                 and time.monotonic() < deadline)
    except (OverflowError, ValueError):
        valid = False
    _need(valid, "python_input_publish_deadline")


def _close(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        raise ProcessCleanupIncomplete("python_input_publish_cleanup_incomplete") from None


def _stamp(info: os.stat_result, *, full: bool = False) -> tuple[int, ...]:
    basic = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
    return (*basic, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns) if full else basic


@dataclass(frozen=True)
class _Observed:
    # Same basic-directory identity as project_contract_broker, without its import graph.
    parent: int
    name: str
    descriptor: int
    identity: tuple[int, ...]

    def check(self) -> None:
        _need(_stamp(os.stat(self.name, dir_fd=self.parent, follow_symlinks=False)) == self.identity
              and _stamp(os.fstat(self.descriptor)) == self.identity)


def _path_parts(path: Path) -> tuple[str, ...]:
    _need(path.is_absolute() and path.anchor == "/" and ".." not in path.parts
          and "\\" not in str(path) and "\0" not in str(path))
    return path.parts[1:]


def _open_root(path: Path, stack: ExitStack, observed: list[_Observed]) -> int:
    if sys.platform != "linux":
        raise PythonInputPublishError("python_input_publish_root_linux_required")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parts = _path_parts(path)
    parent = os.open("/", flags)
    stack.callback(_close, parent)
    for name in (".", *parts):
        descriptor = os.open(name, flags, dir_fd=parent)
        stack.callback(_close, descriptor)
        info = os.fstat(descriptor)
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == info.st_gid == 0
              and not info.st_mode & 0o022)
        entry = _Observed(parent, name, descriptor, _stamp(info))
        entry.check()
        observed.append(entry)
        parent = descriptor
    _need(stat.S_IMODE(os.fstat(parent).st_mode) == 0o700)
    return parent


def _check_roots(observed: list[_Observed], deadline: float) -> None:
    for entry in observed:
        _deadline(deadline)
        entry.check()


def _validate(entry: WheelDownload, data: bytes, deadline: float) -> None:
    _deadline(deadline)
    try:
        _need(type(data) is bytes and type(entry.filename) is str
              and len(entry.filename) <= 255
              and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*\.whl", entry.filename)
              and type(entry.size_bytes) is int and entry.size_bytes == len(data) > 0
              and type(entry.sha256) is str and re.fullmatch(r"[a-f0-9]{64}", entry.sha256)
              and hashlib.sha256(data).hexdigest() == entry.sha256,
              "python_input_publish_invalid_wheel")
        from agent_hub.skills.sandbox.python_input_fetch import validate_wheel_bytes

        validate_wheel_bytes(entry, data)
    except Exception:  # noqa: BLE001 - static validator details must not expose wheel contents.
        raise PythonInputPublishError("python_input_publish_invalid_wheel") from None
    _deadline(deadline)


def _verified(parent: int, name: str, entry: WheelDownload,
              deadline: float) -> tuple[int, ...] | None:
    if sys.platform != "linux":
        raise PythonInputPublishError("python_input_publish_root_linux_required")
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=parent)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(descriptor)
        identity = _stamp(before, full=True)
        _need(stat.S_ISREG(before.st_mode) and before.st_uid == before.st_gid == 0
              and before.st_dev == os.fstat(parent).st_dev and before.st_nlink == 1
              and stat.S_IMODE(before.st_mode) == 0o444 and before.st_size == entry.size_bytes)
        _need(_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False), full=True) == identity)
        digest, total = hashlib.sha256(), 0
        while True:
            _deadline(deadline)
            chunk = os.read(descriptor, min(65536, entry.size_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            _need(total <= entry.size_bytes)
            digest.update(chunk)
        _need(total == entry.size_bytes and digest.hexdigest() == entry.sha256,
              "python_input_publish_conflict")
        _need(_stamp(os.fstat(descriptor), full=True) == identity
              and _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False), full=True) == identity)
        return identity
    finally:
        _close(descriptor)


def _check_file(parent: int, name: str, identity: tuple[int, ...]) -> None:
    _need(_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False), full=True) == identity)


def _rename_noreplace(source: int, name: str, target: int, filename: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "renameat2 unavailable")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(source, os.fsencode(name), target, os.fsencode(filename), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, "wheel publication rename failed")


def _temp_identity(parent: int, name: str, descriptor: int,
                   identity: tuple[int, ...]) -> None:
    info = os.fstat(descriptor)
    _need((info.st_dev, info.st_ino) == identity and info.st_dev == os.fstat(parent).st_dev
          and stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 0 and info.st_nlink == 1)
    _need(_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False), full=True)
          == _stamp(info, full=True))


def _discard_temp(parent: int, name: str, descriptor: int, identity: tuple[int, ...]) -> None:
    info = os.fstat(parent)
    _need(info.st_uid == info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o700)
    _temp_identity(parent, name, descriptor, identity)
    os.unlink(name, dir_fd=parent)
    os.fsync(parent)


# These errno results guarantee rename did not publish. All others retain evidence.
_RENAME_REJECTED = {errno.EEXIST, errno.EXDEV, errno.ENOSYS, errno.EACCES, errno.EPERM,
                    errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.ENOENT, errno.EINVAL}
_CREATE_REJECTED = _RENAME_REJECTED | {errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG,
                                     errno.EMFILE, errno.ENFILE}


def publish_verified_wheel(
    cache_root: Path, staging_root: Path, entry: WheelDownload, data: bytes, *, deadline: float,
) -> Literal["published", "reused"]:
    """Publish verified bytes under an externally held Root admission lock.

    deadline is an absolute time.monotonic() bound, including verification.
    ProcessCleanupIncomplete means the caller must retain its durable UNKNOWN
    fence: neither publication nor cleanup may be replayed based on this result.
    """
    if sys.platform != "linux" or os.geteuid() != 0 or os.getegid() != 0:
        raise PythonInputPublishError("python_input_publish_root_linux_required")
    _validate(entry, data, deadline)
    observed: list[_Observed] = []
    temp: tuple[str, int, tuple[int, ...]] | None = None
    renamed = False
    rename_started = False
    temp_open_started = False
    try:
        with ExitStack() as stack:
            cache_entries: list[_Observed] = []
            cache = _open_root(cache_root, stack, cache_entries)
            stage_entries: list[_Observed] = []
            staging = _open_root(staging_root, stack, stage_entries)
            observed = cache_entries + stage_entries
            cache_id, stage_id = _stamp(os.fstat(cache))[:2], _stamp(os.fstat(staging))[:2]
            _need(cache_id[0] == stage_id[0]
                  and cache_id not in {node.identity[:2] for node in stage_entries}
                  and stage_id not in {node.identity[:2] for node in cache_entries})
            _check_roots(observed, deadline)
            try:
                existing = _verified(cache, entry.filename, entry, deadline)
                if existing is not None:
                    _check_roots(observed, deadline)
                    _check_file(cache, entry.filename, existing)
                    return "reused"
                name = ".wheel-publish-" + secrets.token_hex(16)
                temp_open_started = True
                try:
                    descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL
                                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=staging)
                except OSError as error:
                    if error.errno in _CREATE_REJECTED:
                        temp_open_started = False
                    raise
                stack.callback(_close, descriptor)
                temp = name, descriptor, _stamp(os.fstat(descriptor))[:2]
                temp_open_started = False
                os.fchmod(descriptor, 0o600)
                _temp_identity(staging, *temp)
                remaining = memoryview(data)
                while remaining:
                    _deadline(deadline)
                    count = os.write(descriptor, remaining[:65536])
                    _need(0 < count <= len(remaining), "python_input_publish_io_error")
                    remaining = remaining[count:]
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o444)
                os.fsync(descriptor)
                _temp_identity(staging, *temp)
                _need(_verified(staging, name, entry, deadline) is not None)
                os.fsync(staging)
                _check_roots(observed, deadline)
                _temp_identity(staging, *temp)
                rename_started = True
                try:
                    _rename_noreplace(staging, name, cache, entry.filename)
                except OSError as error:
                    if error.errno not in _RENAME_REJECTED:
                        raise
                    rename_started = False
                    if error.errno != errno.EEXIST:
                        raise PythonInputPublishError("python_input_publish_rename_rejected") from None
                    existing = _verified(cache, entry.filename, entry, deadline)
                    _need(existing is not None)
                    _check_roots(observed, deadline)
                    _discard_temp(staging, *temp)
                    temp = None
                    _check_roots(observed, deadline)
                    assert existing is not None
                    _check_file(cache, entry.filename, existing)
                    return "reused"
                renamed = True
                os.fsync(cache)
                os.fsync(staging)
                final = _verified(cache, entry.filename, entry, deadline)
                _need(final is not None and final[:2] == temp[2])
                _check_roots(observed, deadline)
                assert final is not None
                _check_file(cache, entry.filename, final)
                return "published"
            except BaseException:
                if renamed or rename_started or temp_open_started:
                    raise ProcessCleanupIncomplete("python_input_publish_cleanup_incomplete") from None
                if temp is not None:
                    try:
                        _discard_temp(staging, *temp)
                        temp = None
                    except BaseException:  # noqa: BLE001 - failed cleanup must retain UNKNOWN.
                        raise ProcessCleanupIncomplete("python_input_publish_cleanup_incomplete") from None
                raise
    except ProcessCleanupIncomplete:
        raise
    except BaseException as error:
        if renamed or rename_started or temp_open_started:
            raise ProcessCleanupIncomplete("python_input_publish_cleanup_incomplete") from None
        if isinstance(error, PythonInputPublishError) or not isinstance(error, Exception):
            raise
        raise PythonInputPublishError("python_input_publish_io_error") from None
