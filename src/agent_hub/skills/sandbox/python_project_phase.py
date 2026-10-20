"""Fixed native phases, invoked ONLY inside the trusted Linux launcher.

The marker is a fail-closed invocation guard, NOT proof of a sandbox. The outer
runner owns systemd/bwrap, immutable input mounts, fresh owned writable mounts,
network denial, cgroup deadlines/output limits and descendant cleanup. It must
seal /environment before build/test and mount project-site read-only for test.
Lock/wheel capture and verification, cache export and attestation belong there.

Run this standalone script with explicit -I -B. Build/test use the sealed venv
interpreter. Venv bootstraps pip using trusted stdlib ensurepip; its INTERNAL
invocation/compilation behavior still needs launcher-side identity validation.
Metadata is native process evidence only, never a claim of business correctness
or trustworthy attestation against project code running in this same process.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import keyword
import os
import re
import shutil
import stat
import subprocess
import sys
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import BinaryIO, Never, TypedDict, cast

_RUNTIME = Path('/runtime')
_ENVIRONMENT = Path('/environment')
_WHEELHOUSE = Path('/wheelhouse')
_WORKSPACE = Path('/workspace')
_OUTPUT = Path('/output')
_PHASES = ('install', 'check_environment', 'build', 'test')


class PhaseError(RuntimeError):
    """Phase rejection with no raw paths or project exception messages."""


class _WheelMetadata(TypedDict):
    filename: str
    sha256: str
    size: int


def _need(condition: object) -> None:
    if not condition:
        raise PhaseError('phase_failed')


def _isolation_guard() -> None:
    _need(sys.platform == 'linux'
          and os.environ.get('AGENT_HUB_PYTHON_PROJECT_ISOLATED') == '1')


def _selection(phase: str, module: str) -> None:
    _need(type(phase) is str and phase in _PHASES)
    _need(type(module) is str and len(module) <= 255
          and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*(\.[A-Za-z_][A-Za-z_0-9]*)*', module)
          and not any(keyword.iskeyword(part) for part in module.split('.')))


def _directory(path: Path, *, owned: bool = False, empty: bool = False) -> os.stat_result:
    if sys.platform != 'linux':
        raise PhaseError('phase_failed')
    info = path.lstat()
    _need(stat.S_ISDIR(info.st_mode) and path.resolve(strict=True) == path)
    if owned:
        _need(info.st_uid == os.getuid())
    if empty:
        _need(next(path.iterdir(), None) is None)
    return info


def _mounts() -> None:
    roots = (_RUNTIME, _ENVIRONMENT, _WHEELHOUSE, _WORKSPACE, _OUTPUT)
    identities = []
    for path in roots:
        _need(path.is_absolute())
        info = _directory(path)
        identities.append((info.st_dev, info.st_ino))
    _need(len(set(identities)) == len(roots))
    _need(all(not left.is_relative_to(right)
              for left in roots for right in roots if left != right))


def _regular(path: Path, *, sealed_input: bool = False) -> os.stat_result:
    info = path.lstat()
    # bwrap's read-only data mounts are anonymous sealed files (nlink0), not
    # shared hardlinks. Permit them only for the reconstructed source mount.
    _need(stat.S_ISREG(info.st_mode) and (info.st_nlink == 1
        or sealed_input and info.st_nlink == 0 and path.is_relative_to(_WORKSPACE)))
    return info


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_regular(path: Path, *, sealed_input: bool = False) -> BinaryIO:
    before = _regular(path, sealed_input=sealed_input)
    flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    descriptor = os.open(path, flags)
    try:
        _need(_stamp(os.fstat(descriptor)) == _stamp(before))
        stream = os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise
    return stream


def _execute(arguments: list[str]) -> None:
    # No shell, buffering, parallelism or local timeout: the outer cgroup owns it.
    subprocess.run(arguments, check=True, cwd='/', stdin=subprocess.DEVNULL,
                   stdout=sys.stderr, stderr=sys.stderr)


def _pip(*arguments: str) -> None:
    _execute([str(_ENVIRONMENT / 'bin/python'), '-I', '-B', '-m', 'pip',
              '--isolated', *arguments])


def _install() -> dict[str, object]:
    _directory(_ENVIRONMENT, owned=True, empty=True)
    _regular(_WORKSPACE / 'requirements.lock', sealed_input=True)
    _execute([str(_RUNTIME / 'bin/python3.12'), '-I', '-B', '-m', 'venv',
              '--copies', str(_ENVIRONMENT)])
    _pip('install', '--no-index', f'--find-links={_WHEELHOUSE}', '--require-hashes',
         '--only-binary=:all:', '--no-deps', '--no-cache-dir',
         '--disable-pip-version-check', '--no-compile',
         '-r', str(_WORKSPACE / 'requirements.lock'))
    _pip('check')
    return {'requires_dist_check': 'completed'}


def _environment_interpreter() -> None:
    _need(sys.executable == str(_ENVIRONMENT / 'bin/python')
          and sys.prefix == str(_ENVIRONMENT))


def _copy_source(source: Path, destination: Path) -> None:
    # Input immutability is provided by the outer mount; reject ALL links and
    # special files rather than dereferencing them or preserving shared inodes.
    info = source.lstat()
    if stat.S_ISDIR(info.st_mode):
        _directory(source)
        destination.mkdir(mode=0o700)
        for child in source.iterdir():
            _copy_source(child, destination / child.name)
    else:
        _regular(source, sealed_input=True)
        with _read_regular(source, sealed_input=True) as reader, destination.open('xb') as writer:
            shutil.copyfileobj(reader, writer)
        destination.chmod(0o700 if info.st_mode & 0o111 else 0o600)


def _wheel_metadata(directory: Path, name: str) -> _WheelMetadata:
    _need(type(name) is str
          and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.+!-]*\.whl', name)
          and name not in {'.', '..'})
    _directory(directory, owned=True)
    _need([member.name for member in directory.iterdir()] == [name])
    wheel = directory / name
    before = _regular(wheel)
    digest = hashlib.sha256()
    size = 0
    with _read_regular(wheel) as reader:
        while chunk := reader.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
        _need(size > 0 and size == before.st_size
              and _stamp(os.fstat(reader.fileno())) == _stamp(before))
    _need(_stamp(_regular(wheel)) == _stamp(before))
    return {'filename': name, 'sha256': digest.hexdigest(), 'size': size}


def _build() -> dict[str, object]:
    _environment_interpreter()
    _directory(_OUTPUT, owned=True, empty=True)
    source = _OUTPUT / 'source'
    wheels = _OUTPUT / 'wheels'
    site = _OUTPUT / 'project-site'
    _copy_source(_WORKSPACE, source)
    wheels.mkdir(mode=0o700)
    previous = Path.cwd()
    try:
        os.chdir(source)
        backend = importlib.import_module('setuptools.build_meta')
        name = backend.build_wheel(str(wheels))
    finally:
        os.chdir(previous)
    metadata = _wheel_metadata(wheels, name)
    site.mkdir(mode=0o700)
    _pip('install', '--no-index', '--no-deps', '--no-cache-dir', '--no-compile',
         '--target', str(site), str(wheels / metadata['filename']))
    return {'wheel': metadata}


def _test(module_name: str) -> dict[str, object]:
    _environment_interpreter()
    site = _OUTPUT / 'project-site'
    tests = _WORKSPACE / 'tests'
    _directory(site)
    _directory(tests)
    previous = sys.path[:]
    try:
        parts = module_name.split('.')
        _need(not any('.'.join(parts[:index]) in sys.modules
                      for index in range(1, len(parts) + 1)))
        sys.path.insert(0, str(site))
        importlib.invalidate_caches()
        imported = importlib.import_module(module_name)
        origin = getattr(getattr(imported, '__spec__', None), 'origin', None)
        _need(type(origin) is str and Path(origin).is_absolute())
        resolved = Path(cast(str, origin)).resolve(strict=True)
        _need(resolved.is_relative_to(site))
        relative_origin = resolved.relative_to(site).as_posix()
        suite = unittest.TestLoader().discover(start_dir=str(tests), pattern='test*.py',
                                              top_level_dir=str(tests))
        count = suite.countTestCases()
        _need(count > 0)
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)
        _need(result.testsRun == count and result.wasSuccessful())
    finally:
        sys.path[:] = previous
    return {'test_count': result.testsRun,
            'import': {'module': module_name, 'origin': relative_origin}}


@contextmanager
def _phase_output() -> Iterator[None]:
    # Python-only redirect_stdout does not cover native writes or descendants.
    # Keep FD1 for the final protocol JSON; all phase progress goes to FD2.
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.set_inheritable(saved, False)
        os.dup2(2, 1)
        with redirect_stdout(sys.stderr):
            yield
    finally:
        try:
            sys.stdout.flush()
        finally:
            try:
                os.dup2(saved, 1)
            finally:
                os.close(saved)


def run_phase(phase: str, *, import_module: str) -> dict[str, object]:
    """Execute a fixed phase; callers must supply the real sandbox externally."""
    _isolation_guard()
    try:
        _selection(phase, import_module)
        _mounts()
        sys.dont_write_bytecode = True
        with _phase_output():
            if phase == 'install':
                metadata = _install()
            elif phase == 'check_environment':
                _environment_interpreter()
                _pip('check')
                metadata = {'requires_dist_check': 'completed'}
            elif phase == 'build':
                metadata = _build()
            else:
                metadata = _test(import_module)
        return {'phase': phase, 'evidence': 'native_process_only', **metadata}
    except Exception:  # noqa: BLE001 - never expose backend/process exception text.
        raise PhaseError('phase_failed') from None


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise PhaseError('phase_failed')


def main(argv: list[str] | None = None) -> int:
    """CLI: ``<phase> --import-module <qualified.module>``; JSON on stdout."""
    try:
        _isolation_guard()
        parser = _Parser(add_help=False, allow_abbrev=False)
        parser.add_argument('phase', choices=_PHASES)
        parser.add_argument('--import-module', required=True)
        args = parser.parse_args(argv)
        _selection(args.phase, args.import_module)
        metadata = run_phase(args.phase, import_module=args.import_module)
    except (Exception, SystemExit, KeyboardInterrupt):  # noqa: BLE001 - CLI fails closed.
        print(json.dumps({'status': 'failed', 'error': 'phase_failed'}))
        return 1
    print(json.dumps({'status': 'completed', **metadata}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
