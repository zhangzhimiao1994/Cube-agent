"""Fixed offline phase mounts layered on the unchanged sealed-source adapter.

Only the outer trusted systemd runner uses this module. Its environment/output
directories are quota-tmpfs children, never persistent host-cache paths. Source
is reconstructed read-only before phase-specific writable mounts are added.
"""

from __future__ import annotations

import hashlib
import keyword
import os
import re
import runpy
import stat
import sys
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath


def _need(condition: object) -> None:
    if not condition:
        raise ValueError('python_project_mounts_rejected')


def _fd(value: int) -> str:
    _need(type(value) is int and value >= 0)
    return f'/proc/self/fd/{value}'


def python_phase_mount_arguments(
    phase: str, *, environment_fd: int, output_fd: int, phase_fd: int, import_module: str,
) -> tuple[str, ...]:
    _need(type(phase) is str and phase in {'install', 'check_environment', 'build', 'test'})
    _need(type(import_module) is str and 0 < len(import_module) <= 255
        and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*', import_module)
        and not any(keyword.iskeyword(part) for part in import_module.split('.')))
    environment, output, script = _fd(environment_fd), _fd(output_fd), _fd(phase_fd)
    _need(len({environment, output, script}) == 3)
    python = '/runtime/bin/python3.12' if phase == 'install' else '/environment/bin/python'
    return ('--bind' if phase == 'install' else '--ro-bind', environment, '/environment',
        '--bind' if phase == 'build' else '--ro-bind', output, '/output',
        '--ro-bind-data', str(phase_fd), '/trusted/phase.py', '--',
        python, '-I', '-B', '/trusted/phase.py', phase, '--import-module', import_module)


def python_phase_prefix(*, work_root: PurePath, runtime_fd: int, wheel_fd: int) -> tuple[str, ...]:
    _need(isinstance(work_root, PurePath) and PurePosixPath(str(work_root)).is_absolute() and '..' not in work_root.parts
          and '\0' not in str(work_root))
    runtime, wheels = _fd(runtime_fd), _fd(wheel_fd)
    _need(runtime != wheels)
    argv: tuple[str, ...] = ('/usr/bin/bwrap', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
        '--unshare-user', '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--unshare-net',
        '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin', '--ro-bind', '/lib', '/lib',
        '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/run', '--tmpfs', '/var',
        '--dir', '/trusted', '--bind', str(work_root), '/workspace',
        '--ro-bind', runtime, '/runtime', '--ro-bind', wheels, '/wheelhouse',
        '--chdir', '/workspace', '--clearenv',
        '--setenv', 'PATH', '/environment/bin:/runtime/bin:/usr/bin:/bin',
        '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'HOME', '/tmp', '--setenv', 'TMPDIR', '/tmp',
        '--setenv', 'LD_LIBRARY_PATH', '/runtime/lib',
        '--setenv', 'AGENT_HUB_PYTHON_PROJECT_ISOLATED', '1')
    if Path('/lib64').exists():
        argv += ('--ro-bind', '/lib64', '/lib64')
    return (*argv, '--', '/usr/bin/true')


def _identity(info: os.stat_result) -> tuple[int, ...]:
    # Authorized child directories change a writable directory's nlink, size
    # and clocks, not its identity. Replacement/permission changes still fail.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


def _file_stamp(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True, slots=True)
class _Pinned:
    parent: int
    name: str
    descriptor: int
    identity: tuple[int, ...]

    def check(self) -> None:
        _need(_identity(os.fstat(self.descriptor)) == self.identity
            and _identity(os.stat(self.name, dir_fd=self.parent, follow_symlinks=False)) == self.identity)


def _pin_directory(path: Path, stack: ExitStack, pins: list[_Pinned]) -> int:
    if sys.platform != 'linux':
        raise ValueError('python_project_mounts_rejected')
    _need(path.is_absolute() and path.anchor == '/' and '..' not in path.parts)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open('/', flags)
    stack.callback(os.close, descriptor)
    for name in path.parts[1:]:
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        _need(stat.S_ISDIR(before.st_mode) and before.st_uid in {0, os.getuid()}
            and (not before.st_mode & 0o022 or before.st_uid == 0 and before.st_mode & stat.S_ISVTX))
        child = os.open(name, flags, dir_fd=descriptor)
        stack.callback(os.close, child)
        pin = _Pinned(descriptor, name, child, _identity(before))
        pin.check()
        pins.append(pin)
        descriptor = child
    _need(not os.fstat(descriptor).st_mode & 0o022)
    return descriptor


def _seal_script(path: Path, stack: ExitStack, pins: list[_Pinned]) -> int:
    if sys.platform != 'linux':
        raise ValueError('python_project_mounts_rejected')

    parent = _pin_directory(path.parent, stack, pins)
    descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    stack.callback(os.close, descriptor)
    before = os.fstat(descriptor)
    _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
        and before.st_uid in {0, os.getuid()} and not before.st_mode & 0o022
        and 0 < before.st_size <= 128 * 1024)
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(descriptor, 128 * 1024 - size + 1)
        if not chunk:
            break
        size += len(chunk)
        _need(size <= 128 * 1024)
        chunks.append(chunk)
    data = b''.join(chunks)
    _need(len(data) == before.st_size and _file_stamp(os.fstat(descriptor)) == _file_stamp(before)
        and _file_stamp(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) == _file_stamp(before))
    return _seal_data(data, 'python-project-phase', stack)


def _seal_data(data: bytes, label: str, stack: ExitStack) -> int:
    if sys.platform != 'linux':
        raise ValueError('python_project_mounts_rejected')
    import fcntl

    sealed = os.memfd_create(label, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    stack.callback(os.close, sealed)
    remaining = memoryview(data)
    while remaining:
        count = os.write(sealed, remaining)
        _need(count > 0)
        remaining = remaining[count:]
    fcntl.fcntl(sealed, 1033, 15)
    _need(fcntl.fcntl(sealed, 1034) & 15 == 15)
    os.lseek(sealed, 0, os.SEEK_SET)
    return sealed


@dataclass(frozen=True, slots=True)
class PythonPhaseMountPlan:
    argv: tuple[str, ...]
    pass_fds: tuple[int, ...]


def _closed_execution(command: tuple[str, ...], descriptors: tuple[int, ...], close_code: str) -> tuple[str, ...]:
    _need(descriptors and all(type(fd) is int and fd >= 3 for fd in descriptors)
        and len(set(descriptors)) == len(descriptors))
    return ('--', '/usr/bin/python3', '-I', '-S', '-B', '-c', close_code,
        ','.join(map(str, descriptors)), *command)


@contextmanager
def build_python_phase_mounts(
    phase: str, *, work_root: Path, runtime_root: Path, wheel_root: Path,
    environment_root: Path, output_root: Path, phase_script: Path, import_module: str,
    contract_arguments: Mapping[str, object], private_lock: bytes | None = None,
) -> Iterator[PythonPhaseMountPlan]:
    _need(sys.platform == 'linux'
        and os.environ.get('AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX') == 'systemd')
    roots = (work_root, runtime_root, wheel_root, environment_root, output_root)
    _need(all(left != right and not left.is_relative_to(right)
        for left in roots for right in roots if left is not right))
    _need(not any(phase_script.is_relative_to(root) for root in (work_root, environment_root, output_root)))
    _need(contract_arguments.get('work_root') == work_root
        and contract_arguments.get('ephemeral_dirs', ()) == ())
    _need(private_lock is None or phase == 'install' and type(private_lock) is bytes
          and 0 < len(private_lock) <= 256 * 1024)
    support = runpy.run_path(str(Path(__file__).with_name('project_contract_mounts.py')))
    with ExitStack() as stack:
        pins: list[_Pinned] = []
        runtime_fd, wheel_fd, environment_fd, output_fd = (
            _pin_directory(root, stack, pins) for root in roots[1:])
        _need(len({(os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            for fd in (runtime_fd, wheel_fd, environment_fd, output_fd)}) == 4)
        script_fd = _seal_script(phase_script, stack, pins)
        prefix = python_phase_prefix(work_root=work_root, runtime_fd=runtime_fd, wheel_fd=wheel_fd)
        with support['build_contract_mounts'](prefix, **contract_arguments) as source:
            for pin in pins:
                pin.check()
            # The source adapter permits no writable auxiliary mounts. Add only
            # these fixed phase-specific FD targets after it seals the source.
            split = source.argv.index('--')
            phase_args = python_phase_mount_arguments(phase,
                environment_fd=environment_fd, output_fd=output_fd, phase_fd=script_fd, import_module=import_module)
            boundary = phase_args.index('--')
            inherited = (*source.pass_fds, runtime_fd, wheel_fd, environment_fd, output_fd, script_fd)
            auxiliary = phase_args[:boundary]
            command = phase_args[boundary + 1:]
            if private_lock is not None:
                lock_fd = _seal_data(private_lock, 'python-project-private-lock', stack)
                auxiliary += ('--perms', '0444', '--ro-bind-data', str(lock_fd), '/trusted/requirements.lock')
                inherited += (lock_fd,)
                command += ('--private-lock-sha256', hashlib.sha256(private_lock).hexdigest())
            argv = (*source.argv[:split], *auxiliary,
                *_closed_execution(command, inherited, support['_CLOSE_FDS_AND_EXEC']))
            yield PythonPhaseMountPlan(argv, inherited)
            for pin in pins:
                pin.check()
