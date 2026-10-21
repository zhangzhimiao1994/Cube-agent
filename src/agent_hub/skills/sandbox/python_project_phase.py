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
import unicodedata
import unittest
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, redirect_stdout
from pathlib import Path, PureWindowsPath
from types import FunctionType, MappingProxyType
from typing import Any, BinaryIO, Never, TypedDict, cast

_RUNTIME = Path('/runtime')
_ENVIRONMENT = Path('/environment')
_WHEELHOUSE = Path('/wheelhouse')
_WORKSPACE = Path('/workspace')
_OUTPUT = Path('/output')
_PHASES = ('install', 'check_environment', 'build', 'test')
_GENERATED = 'generated_test_execution'
_MANIFEST_LIMIT = 128 * 1024
_CONTROL_NAMES = frozenset(name.lower() for name in (
    'ACCEPTANCE.json', 'package.json', 'npm-shrinkwrap.json', 'package-lock.json', 'pnpm-lock.yaml',
    'yarn.lock', 'bun.lock', 'bun.lockb', 'pyproject.toml', 'requirements.txt', 'requirements.lock',
    'uv.lock', 'poetry.lock', 'Pipfile', 'Pipfile.lock', 'setup.py', 'setup.cfg', 'pytest.ini',
    'tox.ini', 'conftest.py', 'AGENTS.md', 'DELIVERY_MANIFEST.json'))


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


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _need(key not in result)
        result[key] = value
    return result


def _nonfinite(_: str) -> object:
    raise PhaseError('phase_failed')


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('ascii')


def _test_id(value: object) -> str:
    _need(type(value) is str)
    name = cast(str, value)
    parts = name.split('.')
    _need(len(name) <= 512 and len(parts) >= 3 and unicodedata.normalize('NFC', name) == name
          and all(part.isidentifier() and not keyword.iskeyword(part) for part in parts))
    return name


def validate_generated_python_test_execution(value: object) -> Mapping[str, object]:
    """Validate a safe projection, not provenance, authorization or a raw report.

    SHA binds the raw manifest SHA and sorted exact successful unittest IDs.
    The phase is standalone stdlib; outer callers may independently reuse this.
    """
    try:
        _need(isinstance(value, Mapping) and set(value) == {
            'schema_version', 'manifest_sha256', 'count', 'names', 'test_evidence_sha256'})
        item = cast(Mapping[str, object], value)
        _need(type(item['schema_version']) is int and item['schema_version'] == 1
              and type(item['count']) is int and 1 <= item['count'] <= 1000)
        for key in ('manifest_sha256', 'test_evidence_sha256'):
            _need(type(item[key]) is str and re.fullmatch(r'[a-f0-9]{64}', cast(str, item[key])))
        _need(type(item['names']) in (list, tuple))
        rows = cast(list[object] | tuple[object, ...], item['names'])
        _need(1 <= len(rows) <= 1000)
        names = tuple(_test_id(name) for name in rows)
        _need(len(names) == item['count'] and tuple(sorted(names)) == names
              and len({name.casefold() for name in names}) == len(names))
        core = {'schema_version': 1, 'manifest_sha256': item['manifest_sha256'],
                'count': len(names), 'names': names}
        _need(hashlib.sha256(_encoded(core)).hexdigest() == item['test_evidence_sha256'])
        return MappingProxyType({**core, 'test_evidence_sha256': item['test_evidence_sha256']})
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError, PhaseError):
        raise ValueError('generated_python_test_execution_invalid') from None


def _acceptance_path(value: object) -> str:
    _need(type(value) is str)
    path = cast(str, value)
    _need(0 < len(path) <= 1024)
    for part in path.split('/'):
        _need(re.fullmatch(r'[A-Za-z0-9_.-]{1,255}', part) and part not in {'.', '..'}
              and not part.endswith('.') and not PureWindowsPath(part).is_reserved()
              and part.lower() not in _CONTROL_NAMES)
    return path


def _generated_manifest(expected_sha256: str | None) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    if expected_sha256 is None:
        return None
    _need(type(expected_sha256) is str and re.fullmatch(r'[a-f0-9]{64}', expected_sha256) is not None)
    path = _WORKSPACE / 'ACCEPTANCE.json'
    with _read_regular(path, sealed_input=True) as reader:
        raw = reader.read(_MANIFEST_LIMIT + 1)
    _need(0 < len(raw) <= _MANIFEST_LIMIT and hashlib.sha256(raw).hexdigest() == expected_sha256)
    item = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_constant=_nonfinite)
    _need(type(item) is dict and set(item) == {'schema_version', 'stack', 'editable_paths', 'test_files', 'criteria'}
          and type(item['schema_version']) is int and item['schema_version'] == 1 and item['stack'] == 'python')
    paths: dict[str, tuple[str, ...]] = {}
    spellings: dict[str, str] = {'acceptance.json': 'ACCEPTANCE.json'}
    for key, limit in (('editable_paths', 128), ('test_files', 256)):
        rows = item[key]
        _need(type(rows) is list and 1 <= len(rows) <= limit)
        normalized = tuple(_acceptance_path(row) for row in rows)
        _need(len(set(normalized)) == len(normalized))
        for name in normalized:
            parts = name.split('/')
            _need(not any('/'.join(parts[:end]) in normalized for end in range(1, len(parts))))
            for end in range(1, len(parts) + 1):
                prefix = '/'.join(parts[:end])
                _need(prefix.lower() not in spellings or spellings[prefix.lower()] == prefix)
                spellings[prefix.lower()] = prefix
        paths[key] = normalized
    for test in paths['test_files']:
        parts = test.split('/')
        _need(len(parts) >= 2 and parts[0] == 'tests'
              and re.fullmatch(r'test[A-Za-z0-9_]*\.py', parts[-1])
              and all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', part) for part in parts[1:-1])
              and not any(test == edit or test.startswith(edit + '/') or edit.startswith(test + '/')
                          for edit in paths['editable_paths']))
        for end in range(1, len(parts)):
            _directory(_WORKSPACE.joinpath(*parts[:end]))
        _regular(_WORKSPACE / test, sealed_input=True)
    criteria = item['criteria']
    _need(type(criteria) is list and 1 <= len(criteria) <= 256)
    ids: list[str] = []
    names: list[str] = []
    for row in criteria:
        _need(type(row) is dict and set(row) == {'id', 'description', 'tests'}
              and type(row['id']) is str and re.fullmatch(r'AC-[0-9]{3}', row['id'])
              and row['id'] != 'AC-000')
        description = row['description']
        _need(type(description) is str and 0 < len(description) <= 2048
              and description.strip() == description and unicodedata.normalize('NFC', description) == description
              and not any(unicodedata.category(c).startswith('C') for c in description))
        _need(type(row['tests']) is list and 1 <= len(row['tests']) <= 1000)
        ids.append(row['id'])
        names.extend(_test_id(name) for name in row['tests'])
        _need(len(names) <= 1000)
    _need(len(set(ids)) == len(ids) and len({name.casefold() for name in names}) == len(names))
    return hashlib.sha256(raw).hexdigest(), tuple(sorted(paths['test_files'])), tuple(sorted(names))


def _generated_collector(
    files: tuple[str, ...], expected: tuple[str, ...],
) -> Callable[[], int]:
    # Capture before importing installed project code. This rejects simple
    # framework/runner replacement, not arbitrary same-process reflection.
    importlib.import_module('unittest.mock')  # Allow this standard lazy import before pinning unittest.
    case_type, suite_type = unittest.TestCase, unittest.TestSuite
    loader = unittest.TestLoader()
    get_names, case_id = unittest.TestLoader.getTestCaseNames, unittest.TestCase.id
    runner_type, run = unittest.TextTestRunner, unittest.TextTestRunner.run
    result_type = unittest.TextTestResult
    case_run = case_type.run
    _need(isinstance(case_run, FunctionType))
    assert isinstance(case_run, FunctionType)
    get_frame, case_run_code = sys._getframe, case_run.__code__
    successes: list[str] = []
    starts: list[str] = []
    stops: list[str] = []
    rejected: list[bool] = []
    active: list[unittest.TestCase] = []
    class_snapshots: list[tuple[type[unittest.TestCase], tuple[object, ...]]] = []

    def class_code(target: type[unittest.TestCase]) -> tuple[object, ...]:
        return tuple((name, id(value), id(getattr(
            value.__func__ if isinstance(value, (classmethod, staticmethod)) else value, '__code__', None)))
            for name, value in vars(target).items()
            if isinstance(value, (FunctionType, classmethod, staticmethod)))

    class Result(unittest.TextTestResult):
        def startTest(self, test: unittest.TestCase) -> None:
            check()
            _need(not active and not set(vars(test)) & set(core_methods))
            active.append(test)
            starts.append(case_id(test))
            super().startTest(test)

        def stopTest(self, test: unittest.TestCase) -> None:
            check()
            _need(len(active) == 1 and active[0] is test)
            stops.append(case_id(test))
            active.clear()
            super().stopTest(test)

        def addSuccess(self, test: unittest.TestCase) -> None:
            check()
            _need(len(active) == 1 and active[0] is test and get_frame(1).f_code is case_run_code)
            successes.append(case_id(test))
            super().addSuccess(test)

        def addSkip(self, test: unittest.TestCase, reason: str) -> None:
            rejected.append(True)
            super().addSkip(test, reason)

        def addExpectedFailure(self, test: unittest.TestCase, err: Any) -> None:
            rejected.append(True)
            super().addExpectedFailure(test, err)

        def addFailure(self, test: unittest.TestCase, err: Any) -> None:
            rejected.append(True)
            super().addFailure(test, err)

        def addError(self, test: unittest.TestCase, err: Any) -> None:
            rejected.append(True)
            super().addError(test, err)

        def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase, err: Any) -> None:
            if err is not None:
                rejected.append(True)
            super().addSubTest(test, subtest, err)

        def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
            rejected.append(True)
            super().addUnexpectedSuccess(test)

    targets: tuple[object, ...] = (unittest, unittest.case, unittest.suite, unittest.loader,
        unittest.runner, unittest.result, case_type, suite_type, unittest.TestResult,
        result_type, unittest.TestLoader, runner_type, Result, vars(unittest.case)['_Outcome'], json, hashlib)

    def fingerprint() -> tuple[object, ...]:
        entries: list[object] = []
        for target in targets:
            members = vars(target)
            entries.append(tuple((name, id(value), id(value.__code__) if isinstance(value, FunctionType) else 0)
                                 for name, value in members.items() if name != '__warningregistry__'))
        return tuple(entries)

    snapshot = fingerprint()

    def check() -> None:
        _need(fingerprint() == snapshot)
        for target, before in class_snapshots:
            _need(class_code(target) == before)

    core = ('run', '__call__', 'id', 'countTestCases', '_callSetUp', '_callTearDown',
            '_callTestMethod', 'doCleanups', '_callCleanup', '__getattribute__')
    core_methods = {name: getattr(case_type, name) for name in core}

    def make_result(stream: Any, descriptions: bool, verbosity: int) -> unittest.TextTestResult[Any]:
        return Result(stream, descriptions, verbosity)

    def execute() -> int:
        check()
        cases: list[unittest.TestCase] = []
        sys.path.insert(0, str(_WORKSPACE / 'tests'))
        for file in files:
            name = file.removeprefix('tests/').removesuffix('.py').replace('/', '.')
            _need(name not in sys.modules)
            module = importlib.import_module(name)
            check()
            _need(Path(cast(str, module.__file__)).resolve(strict=True) == _WORKSPACE / file
                  and 'load_tests' not in vars(module))
            for value in vars(module).values():
                if not isinstance(value, type) or not issubclass(value, case_type) or value is case_type:
                    continue
                if value.__module__ != name:
                    continue
                _need(all(getattr(value, key) is method for key, method in core_methods.items()))
                for method in get_names(loader, value):
                    _need(len(cases) < len(expected))
                    case = value(method)
                    _need(type(case) is value and not set(vars(case)) & set(core)
                          and case._testMethodName == method)
                    cases.append(case)
        actual = tuple(case_id(case) for case in cases)
        _need(len(actual) == len(expected) and tuple(sorted(actual)) == expected)
        observed_classes: set[type[unittest.TestCase]] = set()
        for case in cases:
            for cls in type(case).__mro__:
                if cls in (case_type, object):
                    break
                if cls not in observed_classes:
                    observed_classes.add(cls)
                    class_snapshots.append((cls, class_code(cls)))
        check()
        result = run(runner_type(stream=sys.stderr, verbosity=1, resultclass=make_result), suite_type(cases))
        check()
        _need(not active and not rejected and len(starts) == len(stops) == len(successes) == len(expected)
              and tuple(sorted(starts)) == tuple(sorted(stops)) == tuple(sorted(successes)) == expected
              and result.testsRun == len(expected) and result.wasSuccessful()
              and not result.skipped and not result.expectedFailures and not result.unexpectedSuccesses)
        return len(expected)
    return execute


def _test(module_name: str, *, generated_manifest_sha256: str | None = None) -> dict[str, object]:
    _environment_interpreter()
    site = _OUTPUT / 'project-site'
    tests = _WORKSPACE / 'tests'
    _directory(site)
    _directory(tests)
    generated = _generated_manifest(generated_manifest_sha256)
    collect = None if generated is None else _generated_collector(generated[1], generated[2])
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
        if collect is not None:
            count = collect()
            _need(sys.modules.get(module_name) is imported)
        else:
            suite = unittest.TestLoader().discover(start_dir=str(tests), pattern='test*.py',
                                                  top_level_dir=str(tests))
            count = suite.countTestCases()
            _need(count > 0)
            result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)
            _need(result.testsRun == count and result.wasSuccessful())
            count = result.testsRun
    finally:
        sys.path[:] = previous
    detail: dict[str, object] = {'test_count': count,
        'import': {'module': module_name, 'origin': relative_origin}}
    if generated is not None:
        core = {'schema_version': 1, 'manifest_sha256': generated[0], 'count': count, 'names': generated[2]}
        projection = {**core, 'test_evidence_sha256': hashlib.sha256(_encoded(core)).hexdigest()}
        detail[_GENERATED] = dict(validate_generated_python_test_execution(projection))
    return detail


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


def run_phase(phase: str, *, import_module: str,
              generated_manifest_sha256: str | None = None) -> dict[str, object]:
    """Execute a fixed phase; callers must supply the real sandbox externally."""
    _isolation_guard()
    try:
        _selection(phase, import_module)
        _need(generated_manifest_sha256 is None or phase == 'test')
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
                metadata = _test(import_module, generated_manifest_sha256=generated_manifest_sha256)
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
        parser.add_argument('--generated-manifest-sha256')
        args = parser.parse_args(argv)
        _selection(args.phase, args.import_module)
        metadata = run_phase(args.phase, import_module=args.import_module,
            generated_manifest_sha256=args.generated_manifest_sha256)
    except (Exception, SystemExit, KeyboardInterrupt):  # noqa: BLE001 - CLI fails closed.
        print(json.dumps({'status': 'failed', 'error': 'phase_failed'}))
        return 1
    print(json.dumps({'status': 'completed', **metadata}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
