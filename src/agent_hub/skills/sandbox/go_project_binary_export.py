"""Linux no-follow Go binary sealing, export and independent Root observation.

These helpers return measurements, never authority, receipts or execution.
The caller owns DynamicUser/systemd isolation, trusted mounted roots, descendant
quiescence, storage limits and failure cleanup. The environment marker is only
the outer runner's assertion. A read-only mode is not an immutable inode: callers
must compare repeated observations around their phases and authority decisions.
Root must compare its independently observed hash/size with runner metadata.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    'GoProjectBinaryExportError',
    'export_go_project_binary',
    'measure_go_project_binary',
    'measure_go_project_export',
    'observe_go_project_binary',
    'seal_go_project_binary',
]

_ERROR = 'go_project_binary_export_rejected'
_DIRECTORY = (os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
              | getattr(os, 'O_CLOEXEC', 0))
_READ = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
         | getattr(os, 'O_CLOEXEC', 0))
_WRITE = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
          | getattr(os, 'O_CLOEXEC', 0))


class GoProjectBinaryExportError(ValueError):
    """A constant diagnostic; untrusted OS paths/messages never cross the API."""


def _need(condition: object) -> None:
    if not condition:
        raise GoProjectBinaryExportError(_ERROR)


@contextmanager
def _guard() -> Iterator[None]:
    try:
        yield
    except Exception:  # noqa: BLE001 - fail closed without leaking host paths.
        raise GoProjectBinaryExportError(_ERROR) from None


def _remaining(deadline: float) -> None:
    _need(time.monotonic() < deadline)


def _limits(maximum: int, deadline: float) -> None:
    _need(type(maximum) is int and maximum >= 64)
    try:
        valid = type(deadline) in {int, float} and math.isfinite(deadline)
    except OverflowError:
        valid = False
    _need(valid)
    _remaining(deadline)


def _uid(value: int) -> None:
    # Linux uid_t reserves UINT32_MAX as the no-change sentinel.
    _need(type(value) is int and 0 < value < 2**32 - 1)


def _expected(digest: str, size: int, maximum: int) -> None:
    _need(type(digest) is str and re.fullmatch('[0-9a-f]{64}', digest) is not None
          and type(size) is int and 64 <= size <= maximum)


def _native(*, root: bool) -> int:
    if sys.platform != 'linux':
        raise GoProjectBinaryExportError(_ERROR)
    _need(getattr(os, 'O_NOFOLLOW', 0) and getattr(os, 'O_DIRECTORY', 0)
          and getattr(os, 'O_CLOEXEC', 0) and os.open in os.supports_dir_fd
          and os.stat in os.supports_dir_fd and os.scandir in os.supports_fd)
    uid = os.getuid()
    _need(os.geteuid() == uid)
    if root:
        _need(uid == 0)
    else:
        _uid(uid)
        _need(os.getgid() == os.getegid()
              and os.environ.get('AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX') == 'systemd')
    return uid


def _stamp(info: os.stat_result, *, full: bool = True) -> tuple[int, ...]:
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
    return (*identity, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns) if full else identity


@dataclass(frozen=True, slots=True)
class _Pin:
    parent: int
    name: str
    descriptor: int
    identity: tuple[int, ...]
    full: bool = True

    def check(self) -> None:
        _need(_stamp(os.fstat(self.descriptor), full=self.full) == self.identity
              and _stamp(os.stat(self.name, dir_fd=self.parent, follow_symlinks=False),
                         full=self.full) == self.identity)


def _check(pins: list[_Pin], deadline: float) -> None:
    for pin in pins:
        _remaining(deadline)
        pin.check()
    _remaining(deadline)


def _child(parent: int, name: str, stack: ExitStack) -> int:
    descriptor = os.open(name, _DIRECTORY, dir_fd=parent)
    stack.callback(os.close, descriptor)
    _need(stat.S_ISDIR(os.fstat(descriptor).st_mode))
    return descriptor


def _pin_path(path: Path, uid: int, stack: ExitStack, pins: list[_Pin],
              deadline: float, *, writable_leaf: bool = False) -> int:
    _need(isinstance(path, Path) and path.is_absolute() and path.anchor == '/'
          and len(path.parts) > 1 and '..' not in path.parts
          and '\\' not in str(path) and '\0' not in str(path))
    parent = os.open('/', _DIRECTORY)
    stack.callback(os.close, parent)
    for index, name in enumerate(path.parts[1:]):
        _remaining(deadline)
        child = _child(parent, name, stack)
        info = os.fstat(child)
        _need(info.st_uid in {0, uid})
        if not (writable_leaf and index == len(path.parts) - 2):
            _need(not info.st_mode & 0o022
                  or info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
        pin = _Pin(parent, name, child, _stamp(info, full=False), False)
        pin.check()
        pins.append(pin)
        parent = child
    return parent


def _file(parent: int, stack: ExitStack) -> tuple[int, _Pin]:
    descriptor = os.open('project', _READ, dir_fd=parent)
    stack.callback(os.close, descriptor)
    info = os.fstat(descriptor)
    pin = _Pin(parent, 'project', descriptor, _stamp(info))
    pin.check()
    return descriptor, pin


def _header(header: bytes) -> None:
    # Go uses ELFOSABI_NONE (0); ELFOSABI_LINUX (3) is also a Linux header.
    # This is a header measurement, not a promise the ELF will load or run.
    _need(len(header) == 64 and header[:7] == b'\x7fELF\x02\x01\x01'
          and header[7] in {0, 3} and header[8] == 0
          and int.from_bytes(header[16:18], 'little') in {2, 3}
          and header[18:24] == b'\x3e\x00\x01\x00\x00\x00'
          and int.from_bytes(header[52:54], 'little') == 64)


def _shape(info: os.stat_result, *, owner: int | None, maximum: int,
           sealed: bool, service_uid: int | None = None) -> None:
    _uid(info.st_uid)
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
          and (owner is None or info.st_uid == owner)
          and info.st_uid != service_uid and 64 <= info.st_size <= maximum)
    mode = stat.S_IMODE(info.st_mode)
    _need(mode == 0o555 if sealed else not mode & 0o7022)


def _write_all(descriptor: int, chunk: bytes, deadline: float) -> None:
    remaining = memoryview(chunk)
    while remaining:
        _remaining(deadline)
        count = os.write(descriptor, remaining)
        _need(0 < count <= len(remaining))
        remaining = remaining[count:]


def _measure(descriptor: int, pin: _Pin, *, owner: int | None, maximum: int,
             deadline: float, sealed: bool = True, destination: int | None = None,
             service_uid: int | None = None) -> dict[str, object]:
    _remaining(deadline)
    pin.check()
    before = os.fstat(descriptor)
    _shape(before, owner=owner, maximum=maximum, sealed=sealed, service_uid=service_uid)
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest, size, header = hashlib.sha256(), 0, b''
    while True:
        _remaining(deadline)
        # A transfer chunk size, not an artifact/resource quota.
        chunk = os.read(descriptor, min(64 * 1024, before.st_size - size + 1))
        _remaining(deadline)
        if not chunk:
            break
        size += len(chunk)
        _need(size <= before.st_size)
        digest.update(chunk)
        if len(header) < 64:
            header += chunk[:64 - len(header)]
        if destination is not None:
            _write_all(destination, chunk, deadline)
    _need(size == before.st_size)
    _header(header)
    pin.check()
    _remaining(deadline)
    return {'filename': 'project', 'sha256': digest.hexdigest(), 'size': size}


def _seal(descriptor: int, deadline: float) -> None:
    if sys.platform != 'linux':
        raise GoProjectBinaryExportError(_ERROR)
    _remaining(deadline)
    os.fchmod(descriptor, 0o555)
    _remaining(deadline)
    os.fsync(descriptor)
    _remaining(deadline)


def _only(parent: int, expected: str | None, deadline: float) -> None:
    _remaining(deadline)
    os.lseek(parent, 0, os.SEEK_SET)
    found = False
    with os.scandir(parent) as entries:
        for entry in entries:
            _remaining(deadline)
            _need(not found and expected is not None and entry.name == expected)
            found = True
    _need(found == (expected is not None))
    _remaining(deadline)


def seal_go_project_binary(output_root: Path, *, maximum: int,
                           deadline: float) -> dict[str, object]:
    """Seal current-uid output/project to 0555, then fully remeasure it.

    Only a native, non-root outer DynamicUser context is accepted. No project
    filename, shell or executable can be supplied. Failure cleanup is caller-owned.
    """
    with _guard(), ExitStack() as stack:
        _limits(maximum, deadline)
        uid = _native(root=False)
        pins: list[_Pin] = []
        parent = _pin_path(output_root, uid, stack, pins, deadline)
        _need(os.fstat(parent).st_uid == uid and not os.fstat(parent).st_mode & 0o022)
        directory = _stamp(os.fstat(parent))
        descriptor, pin = _file(parent, stack)
        measured = _measure(descriptor, pin, owner=uid, maximum=maximum,
                            deadline=deadline, sealed=False)
        before = os.fstat(descriptor)
        _seal(descriptor, deadline)
        after = os.fstat(descriptor)
        _need((before.st_dev, before.st_ino, before.st_uid, before.st_gid, before.st_nlink,
               before.st_size, before.st_mtime_ns)
              == (after.st_dev, after.st_ino, after.st_uid, after.st_gid, after.st_nlink,
                  after.st_size, after.st_mtime_ns))
        sealed = _Pin(parent, 'project', descriptor, _stamp(after))
        _need(_measure(descriptor, sealed, owner=uid, maximum=maximum, deadline=deadline) == measured)
        _need(_stamp(os.fstat(parent)) == directory)
        _check([*pins, sealed], deadline)
        return measured


def measure_go_project_binary(output_root: Path, *, maximum: int,
                              deadline: float) -> dict[str, object]:
    """Fully observe an already sealed current-uid output/project, without writes."""
    with _guard(), ExitStack() as stack:
        _limits(maximum, deadline)
        uid = _native(root=False)
        pins: list[_Pin] = []
        parent = _pin_path(output_root, uid, stack, pins, deadline)
        _need(os.fstat(parent).st_uid == uid and not os.fstat(parent).st_mode & 0o022)
        directory = _stamp(os.fstat(parent))
        descriptor, pin = _file(parent, stack)
        measured = _measure(descriptor, pin, owner=uid, maximum=maximum, deadline=deadline)
        _need(_stamp(os.fstat(parent)) == directory)
        _check([*pins, pin], deadline)
        return measured


def export_go_project_binary(output_root: Path, export_root: Path, *, expected_sha256: str,
    expected_bytes: int, maximum: int, deadline: float,
) -> dict[str, object]:
    """Copy only sealed project into a caller-mounted empty root:group 0770 export.

    O_EXCL prevents overwrite. Both files and all directory bindings are checked;
    the complete source and destination are independently hashed after fsync.
    On failure, the caller must discard the entire isolated export directory.
    """
    with _guard(), ExitStack() as stack:
        _limits(maximum, deadline)
        _expected(expected_sha256, expected_bytes, maximum)
        uid = _native(root=False)
        _need(output_root != export_root and not output_root.is_relative_to(export_root)
              and not export_root.is_relative_to(output_root))
        pins: list[_Pin] = []
        source = _pin_path(output_root, uid, stack, pins, deadline)
        _need(os.fstat(source).st_uid == uid and not os.fstat(source).st_mode & 0o022)
        source_directory = _stamp(os.fstat(source))
        destination = _pin_path(export_root, uid, stack, pins, deadline, writable_leaf=True)
        info = os.fstat(destination)
        if sys.platform != 'linux':
            raise GoProjectBinaryExportError(_ERROR)
        _uid(info.st_gid)
        _need(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o770
              and info.st_gid in {os.getgid(), *os.getgroups()})
        _only(destination, None, deadline)
        descriptor, pin = _file(source, stack)
        expected: dict[str, object] = {'filename': 'project', 'sha256': expected_sha256,
                                      'size': expected_bytes}
        _need(_measure(descriptor, pin, owner=uid, maximum=maximum, deadline=deadline) == expected)
        target = os.open('project', _WRITE, 0o600, dir_fd=destination)
        stack.callback(os.close, target)
        target_pin = _Pin(destination, 'project', target, _stamp(os.fstat(target)))
        target_pin.check()
        _need(stat.S_ISREG(os.fstat(target).st_mode) and os.fstat(target).st_uid == uid
              and os.fstat(target).st_nlink == 1 and os.fstat(target).st_size == 0)
        export_directory = _stamp(os.fstat(destination))
        _need(_measure(descriptor, pin, owner=uid, maximum=maximum, deadline=deadline,
                       destination=target) == expected)
        _seal(target, deadline)
        target_pin = _Pin(destination, 'project', target, _stamp(os.fstat(target)))
        target_pin.check()
        copied, copied_pin = _file(destination, stack)
        _need(_stamp(os.fstat(copied)) == _stamp(os.fstat(target)))
        _need(_measure(copied, copied_pin, owner=uid, maximum=maximum, deadline=deadline) == expected)
        _need(_measure(descriptor, pin, owner=uid, maximum=maximum, deadline=deadline) == expected)
        os.fsync(destination)
        _only(destination, 'project', deadline)
        _need(_stamp(os.fstat(source)) == source_directory
              and _stamp(os.fstat(destination)) == export_directory)
        _check([*pins, pin, target_pin, copied_pin], deadline)
        return expected


def _observe(parent_fd: int, *, gid: int, service_uid: int, maximum: int, deadline: float,
             expected_dynamic_uid: int | None = None) -> dict[str, object]:
    _limits(maximum, deadline)
    _native(root=True)
    _uid(gid)
    _uid(service_uid)
    if expected_dynamic_uid is not None:
        _uid(expected_dynamic_uid)
        _need(expected_dynamic_uid != service_uid)
    _need(type(parent_fd) is int and parent_fd >= 0)
    before = os.fstat(parent_fd)
    _need(stat.S_ISDIR(before.st_mode) and before.st_uid == 0
          and stat.S_IMODE(before.st_mode) == 0o700)
    with ExitStack() as stack:
        pins: list[_Pin] = []
        # Re-pin every named ancestor and the stage binding, not just a detached
        # directory FD. Deleted/replaced/otherwise unnameable stages fail closed.
        parent = _pin_path(Path(os.readlink(f'/proc/self/fd/{parent_fd}')),
                           0, stack, pins, deadline)
        _need(_stamp(os.fstat(parent)) == _stamp(before))
        export = _child(parent, 'export', stack)
        info = os.fstat(export)
        _need(info.st_uid == 0 and info.st_gid == gid and stat.S_IMODE(info.st_mode) == 0o770)
        export_pin = _Pin(parent, 'export', export, _stamp(info))
        export_pin.check()
        _only(export, 'project', deadline)
        descriptor, pin = _file(export, stack)
        measured = _measure(descriptor, pin, owner=expected_dynamic_uid, service_uid=service_uid,
                            maximum=maximum, deadline=deadline)
        _only(export, 'project', deadline)
        _need(_stamp(os.fstat(parent_fd)) == _stamp(before)
              and _stamp(os.fstat(parent)) == _stamp(before))
        _check([*pins, export_pin, pin], deadline)
        return measured


def observe_go_project_binary(parent_fd: int, *, gid: int, service_uid: int,
                              maximum: int, deadline: float) -> dict[str, object]:
    """Root independently measures stage/export/project, never executes or writes.

    parent_fd must identify a named root-owned 0700 stage. Export must be exactly
    root:gid 0770 with only project. The file uid must be a finite valid non-root,
    non-service uid; this observation does not itself prove systemd provenance.
    Caller supplies trusted service_uid, quiesces descendants, compares returned
    hash/size and repeats observation after its last authority decision.
    """
    with _guard():
        return _observe(parent_fd, gid=gid, service_uid=service_uid,
                        maximum=maximum, deadline=deadline)


def measure_go_project_export(stage_root: Path, *, allowed_gid: int, expected_dynamic_uid: int,
    service_uid: int, expected_sha256: str, expected_bytes: int, maximum: int, deadline: float,
) -> dict[str, object]:
    """Path convenience API with a trusted exact uid and explicit metadata comparison."""
    with _guard(), ExitStack() as stack:
        _limits(maximum, deadline)
        _expected(expected_sha256, expected_bytes, maximum)
        _native(root=True)
        pins: list[_Pin] = []
        parent = _pin_path(stage_root, 0, stack, pins, deadline)
        measured = _observe(parent, gid=allowed_gid, service_uid=service_uid, maximum=maximum,
                            deadline=deadline, expected_dynamic_uid=expected_dynamic_uid)
        _need(measured == {'filename': 'project', 'sha256': expected_sha256, 'size': expected_bytes})
        _check(pins, deadline)
        return measured
