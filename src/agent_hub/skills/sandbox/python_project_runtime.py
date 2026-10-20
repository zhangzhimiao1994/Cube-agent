"""Read-only Linux distribution measurements from trusted local configuration.

Nothing is executed or imported from the measured tree. A successful observation
is neither Python compatibility evidence nor an immutability lease: the broker
must recheck the distribution and bind it read-only before subsequent use.
"""

from __future__ import annotations

import hashlib
import math
import os
import stat
import sys
import time
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent_hub.capability_installer.python_attestation import ScanLimits

_UNSAFE = 'Python distribution observation is unsafe or invalid'
_DIRECTORY_FLAGS = (os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
                    | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0))
_READ_FLAGS = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
               | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
_Relative = tuple[str, ...]


class PythonDistributionError(ValueError):
    """A local distribution cannot be safely measured; messages omit paths."""


class PythonDistributionUnsupportedError(PythonDistributionError):
    """Native Linux directory-relative nofollow operations are required."""


class PythonDistributionLimitError(PythonDistributionError):
    """The configured finite storage or traversal budget was exceeded."""


class PythonDistributionTimeoutError(PythonDistributionError):
    """The absolute monotonic deadline expired."""


@dataclass(frozen=True, slots=True)
class PythonDistributionObservation:
    root: Path
    interpreter: Path
    binary_sha256: str
    content_sha256: str
    total_bytes: int
    file_count: int


def _need(condition: object) -> None:
    if not condition:
        raise PythonDistributionError(_UNSAFE)


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise PythonDistributionTimeoutError('Python distribution observation deadline exceeded')


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    # Race detection only. None of these identity/clock fields enter the digest.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _safe(info: os.stat_result) -> None:
    _need(info.st_uid == 0)
    if stat.S_ISLNK(info.st_mode):
        # Linux symlink permissions are normally 0777 and are not access controls.
        _need(info.st_nlink == 1)
    else:
        _need(not info.st_mode & 0o7022)
        _need(stat.S_ISDIR(info.st_mode)
              or stat.S_ISREG(info.st_mode) and info.st_nlink == 1)


@contextmanager
def _directory(
    parent: int | None, name: str, deadline: float, expected: tuple[int, ...] | None = None,
) -> Iterator[int]:
    _check(deadline)
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _safe(before)
    _need(stat.S_ISDIR(before.st_mode))
    identity = _stamp(before)
    _need(expected is None or identity == expected)
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        _need(_stamp(os.fstat(descriptor)) == identity)
        _need(_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) == identity)
        yield descriptor
        _check(deadline)
        _need(_stamp(os.fstat(descriptor)) == identity)
        _need(_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) == identity)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class _Content:
    kind: str
    executable: int
    size: int = 0
    sha256: str = ''
    target: bytes = b''


@dataclass
class _Budget:
    limits: ScanLimits
    total_bytes: int = 0
    entries: int = 0

    def consume(self, count: int) -> None:
        if count < 0 or count > self.limits.max_bytes - self.total_bytes:
            raise PythonDistributionLimitError('Python distribution byte limit exceeded')
        self.total_bytes += count

    def member(self, relative: _Relative) -> None:
        self.entries += 1
        if (self.entries > self.limits.max_entries
                or len(relative) > self.limits.max_depth
                or len(_path_bytes(relative)) > self.limits.max_path_bytes):
            raise PythonDistributionLimitError('Python distribution traversal limit exceeded')


def _path_bytes(relative: _Relative) -> bytes:
    return b'/'.join(os.fsencode(part) for part in relative)


def _regular(parent: int, name: str, before: os.stat_result,
             budget: _Budget, deadline: float) -> _Content:
    _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1)
    if before.st_size > budget.limits.max_bytes - budget.total_bytes:
        raise PythonDistributionLimitError('Python distribution byte limit exceeded')
    _check(deadline)
    descriptor = os.open(name, _READ_FLAGS, dir_fd=parent)
    try:
        identity = _stamp(before)
        _need(_stamp(os.fstat(descriptor)) == identity)
        digest = hashlib.sha256()
        total = 0
        while True:
            _check(deadline)
            chunk = os.read(descriptor, min(1024**2,
                           budget.limits.max_bytes - budget.total_bytes + 1))
            if not chunk:
                break
            budget.consume(len(chunk))
            total += len(chunk)
            digest.update(chunk)
        _check(deadline)
        _need(total == before.st_size and _stamp(os.fstat(descriptor)) == identity)
        _need(_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) == identity)
        return _Content('file', before.st_mode & 0o111, total, digest.hexdigest())
    finally:
        os.close(descriptor)


def _names(parent: int, prefix: _Relative, budget: _Budget, deadline: float) -> list[str]:
    names: list[str] = []
    with os.scandir(parent) as entries:
        for entry in entries:
            _check(deadline)
            budget.member((*prefix, entry.name))
            names.append(entry.name)
    _check(deadline)
    names.sort(key=os.fsencode)
    return names


def _capture(
    parent: int, prefix: _Relative, budget: _Budget, deadline: float,
    snapshots: dict[_Relative, tuple[int, ...]], contents: dict[_Relative, _Content],
) -> None:
    info = os.fstat(parent)
    snapshots[prefix] = _stamp(info)
    contents[prefix] = _Content('directory', info.st_mode & 0o111)
    for name in _names(parent, prefix, budget, deadline):
        _check(deadline)
        relative = (*prefix, name)
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        _safe(info)
        snapshots[relative] = _stamp(info)
        if stat.S_ISDIR(info.st_mode):
            with _directory(parent, name, deadline, snapshots[relative]) as child:
                _capture(child, relative, budget, deadline, snapshots, contents)
        elif stat.S_ISLNK(info.st_mode):
            if (info.st_size > budget.limits.max_bytes - budget.total_bytes
                    or info.st_size > budget.limits.max_path_bytes):
                raise PythonDistributionLimitError('Python distribution link limit exceeded')
            target = os.readlink(os.fsencode(name), dir_fd=parent)
            _check(deadline)
            budget.consume(len(target))
            _need(len(target) == info.st_size)
            _need(_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) == snapshots[relative])
            contents[relative] = _Content('symlink', info.st_mode & 0o111, target=target)
        else:
            contents[relative] = _regular(parent, name, info, budget, deadline)


def _resolve(
    relative: _Relative, contents: Mapping[_Relative, _Content], deadline: float,
) -> _Relative:
    """Resolve only captured names, with POSIX component ordering and cycle checks."""
    pending: deque[str | _Relative] = deque(relative)
    active: set[_Relative] = set()
    resolved: _Relative = ()
    while pending:
        _check(deadline)
        part = pending.popleft()
        if isinstance(part, tuple):
            active.remove(part)
            continue
        _need(resolved in contents and contents[resolved].kind == 'directory')
        if part in {'', '.'}:
            continue
        if part == '..':
            _need(resolved)
            resolved = resolved[:-1]
            continue
        candidate = (*resolved, part)
        _need(candidate in contents)
        member = contents[candidate]
        if member.kind == 'symlink':
            _need(candidate not in active and member.target and not member.target.startswith(b'/')
                  and b'\0' not in member.target)
            active.add(candidate)
            # A marker removes the link from the active expansion set before
            # processing the caller's tail. Repeated noncyclic aliases are legal.
            pending.appendleft(candidate)
            pending.extendleft(reversed(os.fsdecode(member.target).split('/')))
        else:
            resolved = candidate
    _check(deadline)
    _need(resolved in contents and contents[resolved].kind in {'directory', 'file'})
    return resolved


def _content_digest(contents: Mapping[_Relative, _Content], deadline: float) -> str:
    """Versioned, length-framed semantic records; no absolute paths or stat stamps."""
    _check(deadline)
    digest = hashlib.sha256(b'python-distribution-v1\0')
    for relative in sorted(contents, key=_path_bytes):
        _check(deadline)
        member = contents[relative]
        fields = (_path_bytes(relative), member.kind.encode('ascii'),
                  member.executable.to_bytes(2, 'big'), str(member.size).encode('ascii'),
                  member.sha256.encode('ascii'), member.target)
        for field in fields:
            digest.update(len(field).to_bytes(8, 'big'))
            digest.update(field)
    _check(deadline)
    return digest.hexdigest()


def _verify(
    parent: int, prefix: _Relative, budget: _Budget, deadline: float,
    snapshots: Mapping[_Relative, tuple[int, ...]],
) -> None:
    _need(_stamp(os.fstat(parent)) == snapshots[prefix])
    for name in _names(parent, prefix, budget, deadline):
        _check(deadline)
        relative = (*prefix, name)
        _need(relative in snapshots)
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        _need(_stamp(info) == snapshots[relative])
        if stat.S_ISDIR(info.st_mode):
            with _directory(parent, name, deadline, snapshots[relative]) as child:
                _verify(child, relative, budget, deadline, snapshots)
    _check(deadline)
    _need(_stamp(os.fstat(parent)) == snapshots[prefix])


def observe_python_distribution(
    root: Path, *, interpreter_relative: str, limits: ScanLimits, deadline: float,
) -> PythonDistributionObservation:
    """Measure a trusted configured root and fixed ``bin/python3.12`` selection.

    ``deadline`` is a finite positive absolute ``time.monotonic()`` value.
    ``total_bytes`` includes all regular file bytes (including pyc) and raw link
    targets; ``file_count`` counts regular files, without following aliases.
    The selected interpreter may be an internal alias to a captured executable
    regular file. Neither configuration argument may originate in model args.
    """
    try:
        _need(isinstance(root, Path))
        _need(type(interpreter_relative) is str and interpreter_relative == 'bin/python3.12')
        _need(type(limits) is ScanLimits)
        _need(all(type(value) is int and value > 0 for value in (
            limits.max_bytes, limits.max_entries, limits.max_depth, limits.max_path_bytes)))
        _need(type(deadline) in {float, int} and math.isfinite(deadline) and deadline > 0)
        if (sys.platform != 'linux' or os.name != 'posix'
                or not all(getattr(os, name, 0) for name in (
                    'O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK', 'O_CLOEXEC'))
                or not {os.open, os.stat, os.readlink} <= os.supports_dir_fd
                or os.stat not in os.supports_follow_symlinks
                or os.scandir not in os.supports_fd):
            raise PythonDistributionUnsupportedError(
                'Python distribution observation requires native Linux nofollow support')
        _check(deadline)
        _need(root.is_absolute() and root.anchor == '/' and '..' not in root.parts
              and '\0' not in str(root) and len(os.fsencode(root)) <= 4096)
        contents: dict[_Relative, _Content] = {}
        snapshots: dict[_Relative, tuple[int, ...]] = {}
        budget = _Budget(limits)
        with ExitStack() as stack:
            descriptor = stack.enter_context(_directory(None, '/', deadline))
            for name in root.parts[1:]:
                descriptor = stack.enter_context(_directory(descriptor, name, deadline))
            _capture(descriptor, (), budget, deadline, snapshots, contents)
            for relative, member in contents.items():
                _check(deadline)
                if member.kind == 'symlink':
                    _resolve(relative, contents, deadline)
            binary = contents[_resolve(('bin', 'python3.12'), contents, deadline)]
            _need(binary.kind == 'file' and binary.executable & stat.S_IXUSR)
            content_sha256 = _content_digest(contents, deadline)
            verification = _Budget(limits)
            _verify(descriptor, (), verification, deadline, snapshots)
            _need(verification.entries == budget.entries)
            file_count = sum(member.kind == 'file' for member in contents.values())
        _check(deadline)
        return PythonDistributionObservation(root, root / interpreter_relative, binary.sha256,
                                             content_sha256, budget.total_bytes, file_count)
    except PythonDistributionError:
        raise
    except (OSError, RuntimeError, ValueError, OverflowError):
        # OSError and codec exceptions can contain configured paths or raw names.
        raise PythonDistributionError(_UNSAFE) from None
