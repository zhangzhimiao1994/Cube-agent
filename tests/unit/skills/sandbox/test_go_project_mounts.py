"""Fixed Go CLI plans; Windows structure checks are not native Linux proof."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.skills.sandbox.project_contract_mounts import _File


@pytest.fixture
def mounts() -> Any:
    name = 'agent_hub.skills.sandbox.go_project_mounts'
    assert importlib.util.find_spec(name) is not None, 'Go phase mount adapter missing'
    return importlib.import_module(name)


@pytest.mark.parametrize('phase,expected', [
    ('packages', ('/sdk/bin/go', 'list', '-json', './...')),
    ('build', ('/sdk/bin/go', 'build', '-trimpath', '-mod=readonly',
               '-o', '/output/project', '.')),
    ('test', ('/sdk/bin/go', 'test', '-json', '-count=1', '-vet=off',
              '-mod=readonly', './...')),
])
def test_fixed_root_cli_commands(mounts: Any, phase: str, expected: tuple[str, ...]) -> None:
    assert mounts.go_phase_command(phase) == expected


@pytest.mark.parametrize('phase', [
    '', 'install', 'library', 'go test', 'build -race', '../build',
    'https://example.invalid/module', ['test'], None, True,
])
def test_model_commands_urls_and_flags_are_not_phases(mounts: Any, phase: object) -> None:
    with pytest.raises(ValueError):
        mounts.go_phase_command(phase)


def test_inspection_is_fixed_trusted_helper_with_authorized_limits(mounts: Any) -> None:
    parameters = mounts.GoInspectionParameters('example.invalid/app', 21000, 180000000, 41000,
                                               max_packages=600, max_tests=42000)
    assert mounts.go_inspection_command(parameters) == (
        '/sdk/bin/go', 'run', '-mod=readonly', '/trusted/go_project_inspect.go',
        '--root', '/workspace', '--module', 'example.invalid/app',
        '--max-files', '21000', '--max-bytes', '180000000', '--timeout-ms', '41000',
        '--max-packages', '600', '--max-tests', '42000',
    )
    with pytest.raises(FrozenInstanceError):
        parameters.module_path = 'other'


@pytest.mark.parametrize('values', [
    ('-toolexec=/secret', 10, 100, 100), ('example/../escape', 10, 100, 100),
    ('example/app', True, 100, 100), ('example/app', 0, 100, 100),
    ('example/app', 100001, 100, 100), ('example/app', 10, 0, 100),
    ('example/app', 10, 500 * 1024**2 + 1, 100),
    ('example/app', 10, 100, 0), ('example/app', 10, 100, 3600001),
    ('example/app', 10, 100, 100, True, 10),
    ('example/app', 10, 100, 100, 10, 0),
])
def test_inspection_parameters_reject_injection_and_invalid_budgets(
    mounts: Any, values: tuple[object, ...],
) -> None:
    with pytest.raises(ValueError):
        mounts.GoInspectionParameters(*values)


@pytest.fixture
def structure(mounts: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    roots = {name: tmp_path / name for name in ('work', 'toolchain', 'output', 'cache')}
    original = tmp_path / 'original'
    contract = {'work_root': roots['work'], 'original_root': original,
                'contract_payload': {'opaque': 'shared validator owns this'},
                'expected_contract_sha256': 'a' * 64, 'expected_source_sha256': 'b' * 64}
    state = SimpleNamespace(roots=roots, original=original, contract=contract,
                            pins=[], checks=[], released=[], active=False, seen=None, alias=False)
    monkeypatch.setattr(mounts, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(mounts, 'os', SimpleNamespace(
        environ={'AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX': 'systemd',
                 'GOFLAGS': '-toolexec=/host/secret', 'CGO_CFLAGS': '-host',
                 'GIT_TOKEN': 'secret', 'HOME': '/host/home'},
        fstat=lambda fd: SimpleNamespace(st_dev=1, st_ino=40 if state.alias else fd),
    ))

    def pin(path: Path, stack: Any, pins: list[Any]) -> int:
        fd = 40 + len(state.pins)
        state.pins.append((path, fd))
        stack.callback(state.released.append, fd)
        pins.append(SimpleNamespace(check=lambda: state.checks.append(fd)))
        return fd

    @contextmanager
    def sealed_source(argv: tuple[str, ...], **arguments: object) -> Iterator[Any]:
        assert arguments == contract
        state.seen = argv
        position = argv.index('--bind')
        assert argv[position:position + 3] == ('--bind', str(roots['work']), '/workspace')
        prefix = argv[:position] + argv[position + 3:argv.index('--')]
        state.active = True
        try:
            yield SimpleNamespace(argv=(*prefix, '--tmpfs', '/workspace', '--ro-bind-data',
                '90', '/workspace/go.mod', '--remount-ro', '/workspace',
                '--', '/usr/bin/python3', 'source-launcher-placeholder'), pass_fds=(90,))
        finally:
            state.active = False

    monkeypatch.setattr(mounts, '_pin_directory', pin)
    monkeypatch.setattr(mounts, 'build_contract_mounts', sealed_source)
    return state


def build(mounts: Any, structure: Any, phase: str = 'build', **changes: Any) -> Any:
    options = {name + '_root': root for name, root in structure.roots.items()}
    options['contract_arguments'] = structure.contract
    options.update(changes)
    return mounts.build_go_phase_mounts(phase, **options)


@pytest.mark.parametrize('phase', ['packages', 'build', 'test'])
def test_plan_seals_source_and_exposes_only_fixed_rw_mounts(
    mounts: Any, structure: Any, phase: str,
) -> None:
    with build(mounts, structure, phase) as plan:
        assert structure.active
        argv = plan.argv
        boundary = argv.index('--')
        prefix = argv[:boundary]
        assert argv[0] == '/usr/bin/bwrap'
        for flag in ('--unshare-net', '--unshare-user', '--unshare-pid', '--unshare-ipc',
                     '--unshare-uts', '--die-with-parent', '--new-session', '--clearenv'):
            assert flag in prefix
        descriptors = dict(structure.pins)
        sdk, output, cache = (descriptors[structure.roots[name]]
                              for name in ('toolchain', 'output', 'cache'))
        triples = [prefix[index:index + 3] for index, option in enumerate(prefix)
                   if option in ('--bind', '--ro-bind')]
        assert ('--ro-bind', f'/proc/self/fd/{sdk}', '/sdk') in triples
        assert ('--bind', f'/proc/self/fd/{cache}', '/cache') in triples
        assert ('--bind' if phase == 'build' else '--ro-bind',
                f'/proc/self/fd/{output}', '/output') in triples
        assert sum(item[0] == '--bind' for item in triples) == (2 if phase == 'build' else 1)
        assert '--remount-ro' in prefix and '/workspace/go.mod' in prefix
        assert not any(str(root) in argv for root in (*structure.roots.values(), structure.original))
        assert not any(path in argv for path in ('/home', '/root', '/etc', '/host/home'))
        assert set(plan.pass_fds) == {90, sdk, output, cache}
        assert len(plan.pass_fds) == 4
        assert descriptors[structure.roots['work']] not in plan.pass_fds
        assert descriptors[structure.original] not in plan.pass_fds
        launcher = argv[boundary + 1:]
        assert launcher[:5] == ('/usr/bin/python3', '-I', '-S', '-B', '-c')
        assert set(map(int, launcher[6].split(','))) == set(plan.pass_fds)
        assert launcher[7:] == mounts.go_phase_command(phase)
        with pytest.raises(FrozenInstanceError):
            plan.argv = ()
    assert not structure.active
    assert set(structure.checks) == set(descriptors.values())
    assert all(structure.checks.count(fd) == 2 for fd in descriptors.values())
    assert set(structure.released) == set(descriptors.values())


def test_environment_is_cleared_and_go_is_entirely_offline(mounts: Any, structure: Any) -> None:
    with build(mounts, structure) as plan:
        prefix = plan.argv[:plan.argv.index('--')]
        env = {prefix[index + 1]: prefix[index + 2]
               for index, item in enumerate(prefix) if item == '--setenv'}
        assert env == {
            'PATH': '/sdk/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
            'GOROOT': '/sdk', 'GOTOOLCHAIN': 'local', 'GOWORK': 'off', 'GOENV': 'off',
            'GOTELEMETRY': 'off', 'GOFLAGS': '',
            'GOPROXY': 'off', 'GOSUMDB': 'off', 'GOVCS': '*:off', 'CGO_ENABLED': '0',
            'GOOS': 'linux', 'GOARCH': 'amd64', 'GOCACHE': '/cache/go-build',
            'GOMODCACHE': '/cache/mod', 'GOPATH': '/cache/gopath', 'GOTMPDIR': '/cache/tmp',
            'HOME': '/cache/home', 'TMPDIR': '/cache/tmp',
            'GOMAXPROCS': '1',
        }
        assert prefix.index('--clearenv') < prefix.index('--setenv')
        directories = {prefix[index + 1] for index, item in enumerate(prefix) if item == '--dir'}
        assert directories.issuperset({
            '/cache/go-build', '/cache/mod', '/cache/gopath', '/cache/tmp', '/cache/home',
        })


@pytest.mark.parametrize('parallelism', [1, 2, 3, 4])
def test_go_parallelism_is_explicit_not_host_cpu_count(
    mounts: Any, structure: Any, parallelism: int,
) -> None:
    with build(mounts, structure, parallelism=parallelism) as plan:
        prefix = plan.argv[:plan.argv.index('--')]
        env = {prefix[index + 1]: prefix[index + 2]
               for index, value in enumerate(prefix) if value == '--setenv'}
        assert env['GOMAXPROCS'] == str(parallelism)


@pytest.mark.parametrize('parallelism', [0, 5, True, 1.5, '4', None])
def test_invalid_parallelism_rejected_before_pinning(
    mounts: Any, structure: Any, parallelism: object,
) -> None:
    with pytest.raises(ValueError), build(mounts, structure, parallelism=parallelism):
        pytest.fail('unbounded compiler parallelism accepted')
    assert not structure.pins


def test_inspect_seals_only_application_fixed_helper(
    mounts: Any, structure: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    def seal(path: Path, stack: Any, pins: list[Any]) -> int:
        seen.append(path)
        stack.callback(structure.released.append, 91)
        return 91

    monkeypatch.setattr(mounts, '_seal_script', seal, raising=False)
    parameters = mounts.GoInspectionParameters('example.invalid/app', 5000, 8000000, 30000)
    with build(mounts, structure, 'inspect', inspection=parameters) as plan:
        prefix = plan.argv[:plan.argv.index('--')]
        assert seen == [Path(mounts.__file__).with_name('go_project_inspect.go')]
        assert ('--ro-bind-data', '91', '/trusted/go_project_inspect.go') in [
            prefix[index:index + 3] for index, value in enumerate(prefix)
            if value == '--ro-bind-data'
        ]
        assert '--dir' in prefix and '/trusted' in prefix
        assert plan.argv[-len(mounts.go_inspection_command(parameters)):] == (
            mounts.go_inspection_command(parameters)
        )
        assert set(plan.pass_fds) == {90, 91, 41, 42, 43}
        assert ('--bind', '/proc/self/fd/42', '/output') not in [
            prefix[index:index + 3] for index, value in enumerate(prefix) if value == '--bind'
        ]
    assert 91 in structure.released


@pytest.mark.parametrize('phase,inspection', [('inspect', None), ('test', 'valid'), ('build', {})])
def test_inspection_and_regular_phase_cannot_mix(
    mounts: Any, structure: Any, phase: str, inspection: object,
) -> None:
    if inspection == 'valid':
        inspection = mounts.GoInspectionParameters('example/app', 10, 100, 100)
    with pytest.raises(ValueError), build(mounts, structure, phase, inspection=inspection):
        pytest.fail('invalid inspection phase combination accepted')
    assert not structure.pins


def test_canary_overlay_files_are_sealed_without_writable_source(
    mounts: Any, structure: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.skills.sandbox.go_project_canary import GoOverlayCanary

    canary = GoOverlayCanary('project', 'canary', 'example.invalid/app',
                              'a' * 64, 'b' * 64, 'a' * 64)
    sealed: list[_File] = []

    def seal(file: Any, stack: Any) -> int:
        fd = 91 + len(sealed)
        sealed.append(file)
        stack.callback(structure.released.append, fd)
        return fd

    monkeypatch.setattr(mounts, '_seal', seal, raising=False)
    with build(mounts, structure, 'canary', canary=canary) as plan:
        prefix = plan.argv[:plan.argv.index('--')]
        assert [(file.path, file.data, file.executable) for file in sealed] == [
            ('go-canary.go', canary.source_bytes, False),
            ('go-canary-overlay.json', canary.overlay_bytes, False),
        ]
        triples = [prefix[index:index + 3] for index, value in enumerate(prefix)
                   if value == '--ro-bind-data']
        assert ('--ro-bind-data', '91', '/trusted/go-canary.go') in triples
        assert ('--ro-bind-data', '92', '/trusted/go-canary-overlay.json') in triples
        assert set(plan.pass_fds) == {90, 91, 92, 41, 42, 43}
        assert plan.argv[-len(canary.command):] == canary.command
        assert ('--bind', '/proc/self/fd/42', '/output') not in [
            prefix[index:index + 3] for index, value in enumerate(prefix) if value == '--bind'
        ]
    assert {91, 92} <= set(structure.released)


@pytest.mark.parametrize('phase,probe', [('canary', None), ('test', 'valid'), ('build', {})])
def test_canary_and_regular_phase_parameters_cannot_mix(
    mounts: Any, structure: Any, phase: str, probe: object,
) -> None:
    from agent_hub.skills.sandbox.go_project_canary import GoOverlayCanary

    if probe == 'valid':
        probe = GoOverlayCanary('project', 'canary', 'example/app', 'a' * 64, 'b' * 64, 'c' * 64)
    with pytest.raises(ValueError), build(mounts, structure, phase, canary=probe):
        pytest.fail('invalid canary phase combination accepted')
    assert not structure.pins


@pytest.mark.parametrize('source,protected', [('f' * 64, 'a' * 64), ('b' * 64, 'f' * 64)])
def test_canary_requires_same_measured_source_and_original_checks(
    mounts: Any, structure: Any, source: str, protected: str,
) -> None:
    from agent_hub.skills.sandbox.go_project_canary import GoOverlayCanary

    probe = GoOverlayCanary('project', 'canary', 'example/app', 'a' * 64, source, protected)
    with pytest.raises(ValueError), build(mounts, structure, 'canary', canary=probe):
        pytest.fail('unbound canary source or checks accepted')
    assert not structure.pins


@pytest.mark.parametrize('left,right', permutations(('work', 'toolchain', 'output', 'cache', 'original'), 2))
@pytest.mark.parametrize('overlap', [False, True])
def test_alias_and_nested_roots_are_rejected(
    mounts: Any, structure: Any, left: str, right: str, overlap: bool,
) -> None:
    roots = {**structure.roots, 'original': structure.original}
    value = roots[right] / 'nested' if overlap else roots[right]
    if left == 'original':
        structure.contract['original_root'] = value
        changes = {}
    else:
        changes = {left + '_root': value}
        if left == 'work':
            structure.contract['work_root'] = value
    with pytest.raises(ValueError), build(mounts, structure, **changes):
        pytest.fail('overlapping roots were accepted')
    assert not structure.pins


def test_distinct_paths_with_same_inode_are_rejected(mounts: Any, structure: Any) -> None:
    structure.alias = True
    with pytest.raises(ValueError), build(mounts, structure):
        pytest.fail('inode aliases were accepted')


@pytest.mark.parametrize('field,value', [
    ('ephemeral_dirs', ('build',)), ('ephemeral_dirs', []),
    ('work_root', Path('/different/work')),
])
def test_source_cannot_request_writable_exceptions_or_different_work(
    mounts: Any, structure: Any, field: str, value: object,
) -> None:
    structure.contract[field] = value
    with pytest.raises(ValueError), build(mounts, structure):
        pytest.fail('source exception was accepted')
    assert not structure.pins


@pytest.mark.parametrize('value', [Path('.'), Path('/'), Path('/safe/../other'), 'not-a-Path'])
def test_invalid_root_paths_are_rejected(mounts: Any, structure: Any, value: object) -> None:
    with pytest.raises(ValueError), build(mounts, structure, cache_root=value):
        pytest.fail('invalid root was accepted')
    assert not structure.pins


@pytest.mark.parametrize('platform,sandbox', [('win32', 'systemd'), ('linux', ''), ('linux', 'bwrap')])
def test_native_adapter_requires_linux_and_outer_systemd(
    mounts: Any, structure: Any, platform: str, sandbox: str,
) -> None:
    mounts.sys.platform = platform
    mounts.os.environ['AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX'] = sandbox
    with pytest.raises(ValueError), build(mounts, structure):
        pytest.fail('unsupported native execution was accepted')
    assert not structure.pins


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux FD/sealing only; Windows skip is not native PASS')
class TestNativeDescriptors:
    @pytest.fixture
    def native(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
        roots = {name: tmp_path / name for name in ('work', 'toolchain', 'output', 'cache')}
        original = tmp_path / 'original'
        files = {'go.mod': b'module example.invalid/app\n\ngo 1.23\n',
                 'main.go': b'package main\nfunc main() {}\n',
                 'main_test.go': b'package main\n'}
        members = {name: hashlib.sha256(b'f\0' + data).hexdigest() for name, data in files.items()}
        digest = hashlib.sha256(json.dumps(members, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        for root in (original, *roots.values()):
            root.mkdir(mode=0o700)
        for name, data in files.items():
            (original / name).write_bytes(data)
            (original / name).chmod(0o444)
            (roots['work'] / name).write_bytes(data)
        original.chmod(0o555)
        binding = {'schema_version': 1, 'tenant_id': '10000000-0000-4000-8000-000000000001',
                   'run_id': '10000000-0000-4000-8000-000000000002', 'project_id': 'project-a',
                   'session_id': 'session-a', 'input_source_sha256': digest,
                   'editable_paths': ['main.go'],
                   'protected_files': {name: members[name] for name in ('go.mod', 'main_test.go')}}
        contract_sha = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        contract = {'contract_payload': {**binding, 'protected_sha256': contract_sha},
                    'expected_contract_sha256': contract_sha, 'original_root': original,
                    'work_root': roots['work'], 'expected_source_sha256': digest}
        monkeypatch.setenv('AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX', 'systemd')
        try:
            yield SimpleNamespace(roots=roots, original=original, contract=contract)
        finally:
            original.chmod(0o700)

    def test_sealed_files_and_only_auxiliary_directory_fds_live_until_exit(self, mounts: Any, native: Any) -> None:
        if sys.platform != 'linux':
            pytest.skip('Linux FD/sealing only; not native PASS')
        import fcntl

        with build(mounts, native) as plan:
            descriptors = plan.pass_fds
            directories = []
            files = []
            for fd in descriptors:
                assert not os.get_inheritable(fd)
                info = os.fstat(fd)
                if stat.S_ISDIR(info.st_mode):
                    directories.append((info.st_dev, info.st_ino))
                else:
                    assert fcntl.fcntl(fd, 1034) & 15 == 15
                    files.append(fd)
            assert set(directories) == {(root.stat().st_dev, root.stat().st_ino)
                                       for name, root in native.roots.items() if name != 'work'}
            assert len(files) == 3
            (native.roots['work'] / 'main.go').write_bytes(b'late replacement')
            assert b'package main\nfunc main() {}\n' in [os.read(fd, 1000) for fd in files]
            for fd in files:
                with pytest.raises(OSError):
                    os.write(fd, b'cannot alter sealed source')
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)

    @pytest.mark.parametrize('name', ['work', 'toolchain', 'output', 'cache', 'original'])
    @pytest.mark.parametrize('ancestor', [False, True])
    def test_root_and_ancestor_symlinks_fail_closed(
        self, mounts: Any, native: Any, tmp_path: Path, name: str, ancestor: bool,
    ) -> None:
        root = native.original if name == 'original' else native.roots[name]
        alias = tmp_path / 'alias'
        alias.symlink_to(root.parent if ancestor else root, target_is_directory=True)
        value = alias / root.name if ancestor else alias
        changes: dict[str, Any] = {}
        if name == 'original':
            native.contract['original_root'] = value
        else:
            changes[name + '_root'] = value
            if name == 'work':
                native.contract['work_root'] = value
        with pytest.raises((ValueError, OSError)), build(mounts, native, **changes):
            pytest.fail('symlink accepted')

    def test_fd_cleanup_when_consumer_raises(self, mounts: Any, native: Any) -> None:
        with pytest.raises(RuntimeError, match='consumer failure'), build(mounts, native) as plan:
            descriptors = plan.pass_fds
            raise RuntimeError('consumer failure')
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
