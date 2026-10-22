"""Fixed Go CLI isolation inside the caller's systemd DynamicUser boundary.

The caller owns fresh work, source/contract binding, cgroup/storage/time limits
and confirmed descendant drain. A marker is an outer-runner assertion, never an
authorization minted here. Root must not execute project code through this API.
No SDK, output tree, export directory or host context is exposed to the CLI.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import cast

from agent_hub.capabilities.go_project_business import GoBusinessCase
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

from .project_contract_mounts import ContractMountPlan, _File, _seal, build_contract_mounts
from .python_project_mounts import _closed_execution, _file_stamp, _pin_directory, _Pinned

_STDIN_AND_EXEC = """import errno, os, sys
fd = os.open('/fixture-stdin', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
os.dup2(fd, 0, inheritable=True)
if fd > 2:
    os.close(fd)
for value in sys.argv[1].split(','):
    try:
        os.close(int(value))
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
os.execv('/program', ['/program', *sys.argv[2:]])
"""


def _need(condition: object) -> None:
    if not condition:
        raise ValueError('go_business_mounts_rejected')


class GoBusinessNotAvailable(RuntimeError):
    """The trusted Linux non-root execution environment is unavailable."""


class GoBusinessCleanupIncomplete(ProcessCleanupIncomplete):
    """Pins remain held until the caller confirms outer cgroup/pipe drain."""

    def __init__(self, pins: ExitStack) -> None:
        super().__init__('go_business_cleanup_incomplete')
        self._pins = pins

    def release_after_cleanup(self) -> None:
        """Call only after the outer owner confirms child/cgroup/pipe cleanup."""
        self._pins.close()


def _runtime_available() -> bool:
    return (hasattr(os, 'memfd_create') and os.open in os.supports_dir_fd
            and Path('/proc/self/fd').is_dir()
            and all(Path(path).is_file() and os.access(path, os.X_OK)
                    for path in ('/usr/bin/bwrap', '/usr/bin/python3')))


def _native() -> None:
    if (sys.platform != 'linux'
            or os.environ.get('AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX') != 'systemd'
            or os.getuid() == 0 or os.geteuid() != os.getuid() or not _runtime_available()):
        raise GoBusinessNotAvailable('go_business_not_available')


@dataclass(frozen=True, slots=True)
class GoBusinessMountPlan:
    argv: tuple[str, ...]
    pass_fds: tuple[int, ...]


def _sealed_binary(
    root: Path, stack: ExitStack, *, expected_sha256: str, expected_bytes: int,
) -> int:
    """Measure the fixed nofollow leaf, then export an immutable read-only FD."""
    if sys.platform != 'linux':
        raise GoBusinessNotAvailable('go_business_not_available')
    import fcntl

    pins: list[_Pinned] = []
    parent = _pin_directory(root, stack, pins)
    before = os.stat('project', dir_fd=parent, follow_symlinks=False)
    _need(stat.S_ISREG(before.st_mode) and stat.S_IMODE(before.st_mode) == 0o555
          and before.st_uid in {0, os.getuid()} and before.st_nlink == 1
          and before.st_size == expected_bytes)
    descriptor = os.open('project', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=parent)
    stack.callback(os.close, descriptor)
    _need(_file_stamp(os.fstat(descriptor)) == _file_stamp(before))
    sealed = os.memfd_create('go-business-program', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    stack.callback(os.close, sealed)
    digest, size, header = hashlib.sha256(), 0, b''
    while True:
        chunk = os.read(descriptor, min(65536, expected_bytes - size + 1))
        if not chunk:
            break
        size += len(chunk)
        _need(size <= expected_bytes)
        digest.update(chunk)
        header = (header + chunk[:64])[:64]
        remaining = memoryview(chunk)
        while remaining:
            count = os.write(sealed, remaining)
            _need(count > 0)
            remaining = remaining[count:]
    _need(size == expected_bytes and digest.hexdigest() == expected_sha256
          and _file_stamp(os.fstat(descriptor)) == _file_stamp(before)
          and _file_stamp(os.stat('project', dir_fd=parent, follow_symlinks=False))
          == _file_stamp(before))
    _need(len(header) == 64 and header[:7] == b'\x7fELF\x02\x01\x01'
          and int.from_bytes(header[16:18], 'little') in {2, 3}
          and header[18:24] == b'\x3e\x00\x01\x00\x00\x00')
    for pin in pins:
        pin.check()
    os.fchmod(sealed, 0o555)
    fcntl.fcntl(sealed, fcntl.F_ADD_SEALS, 15)
    _need(fcntl.fcntl(sealed, fcntl.F_GET_SEALS) & 15 == 15)
    readonly = os.open(f'/proc/self/fd/{sealed}', os.O_RDONLY | os.O_CLOEXEC)
    stack.callback(os.close, readonly)
    _need(_file_stamp(os.fstat(readonly)) == _file_stamp(os.fstat(sealed))
          and fcntl.fcntl(readonly, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
          and not os.get_inheritable(readonly))
    return readonly


def _prefix(work: Path) -> tuple[str, ...]:
    argv: tuple[str, ...] = (
        '/usr/bin/bwrap', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
        '--unshare-user', '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--unshare-net',
        '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin', '--ro-bind', '/lib', '/lib',
        '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/run', '--tmpfs', '/var',
        '--bind', str(work), '/workspace', '--chdir', '/workspace', '--clearenv',
        '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'LANG', 'C.UTF-8',
        '--setenv', 'HOME', '/tmp', '--setenv', 'TMPDIR', '/tmp',
    )
    if Path('/lib64').exists():
        argv += ('--ro-bind', '/lib64', '/lib64')
    return (*argv, '--', '/usr/bin/true')


@contextmanager
def build_go_business_mounts(
    *, case: GoBusinessCase, work_root: Path, binary_root: Path,
    expected_binary_sha256: str, expected_binary_bytes: int,
    contract_arguments: Mapping[str, object],
) -> Iterator[GoBusinessMountPlan]:
    """Keep all source/program/stdin FDs pinned through confirmed child drain.

    On ProcessCleanupIncomplete the exception owns the pins; release them via
    release_after_cleanup() only after the outer owner confirms cleanup.
    """
    _native()
    _need(type(case) is GoBusinessCase)
    case = GoBusinessCase.from_payload(case.to_payload())
    _need(type(expected_binary_sha256) is str
          and re.fullmatch('[a-f0-9]{64}', expected_binary_sha256) is not None
          and type(expected_binary_bytes) is int and expected_binary_bytes >= 64)
    arguments = dict(contract_arguments)
    _need(arguments.get('work_root') == work_root and arguments.get('ephemeral_dirs', ()) == ())
    original = arguments.get('original_root')
    roots = (work_root, binary_root, original)
    _need(all(isinstance(root, Path) and root.is_absolute() and root != Path(root.anchor)
              and '..' not in root.parts and '\0' not in str(root) for root in roots))
    paths = (work_root, binary_root, cast(Path, original))
    _need(all(left != right and not left.is_relative_to(right) and not right.is_relative_to(left)
              for left, right in combinations(paths, 2)))
    with ExitStack() as stack:
        binary = _sealed_binary(binary_root, stack, expected_sha256=expected_binary_sha256,
                                expected_bytes=expected_binary_bytes)
        stdin = _seal(_File('fixture-stdin', case.stdin.encode('utf-8')), stack)
        builder = cast(Callable[..., AbstractContextManager[ContractMountPlan]], build_contract_mounts)
        source = stack.enter_context(builder(_prefix(work_root), **arguments))
        inherited = (*source.pass_fds, binary, stdin)
        split = source.argv.index('--')
        argv = (*source.argv[:split],
                '--perms', '0555', '--ro-bind-data', str(binary), '/program',
                '--perms', '0444', '--ro-bind-data', str(stdin), '/fixture-stdin',
                *_closed_execution(case.argv, inherited, _STDIN_AND_EXEC))
        try:
            yield GoBusinessMountPlan(argv, inherited)
        except ProcessCleanupIncomplete as error:
            raise GoBusinessCleanupIncomplete(stack.pop_all()) from error
