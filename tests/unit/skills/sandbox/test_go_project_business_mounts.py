"""Mocked command/FD boundaries; no host project execution or native PASS."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import stat
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.capabilities.go_project_business import GoBusinessCase


@pytest.fixture
def api() -> Any:
    name = 'agent_hub.skills.sandbox.go_project_business_mounts'
    assert importlib.util.find_spec(name) is not None, 'business mounts missing'
    return importlib.import_module(name)


@pytest.fixture
def structure(api: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    state = SimpleNamespace(work=tmp_path / 'work', binary=tmp_path / 'binary',
                            original=tmp_path / 'original', active=False, releases=[], sealed=[])
    state.case = GoBusinessCase('fixed', 'AC-001',
        ('--', '--ro-bind', '/host', '; echo bad', '\n', ''), 'fixture\n', '', '', 0)
    state.arguments = {'work_root': state.work, 'original_root': state.original, 'ephemeral_dirs': (),
                       'contract_payload': {'opaque': 'shared validator'},
                       'expected_source_sha256': 'a' * 64, 'expected_contract_sha256': 'b' * 64}
    monkeypatch.setattr(api, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(api, 'os', SimpleNamespace(
        environ={'AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX': 'systemd'},
        getuid=lambda: 40001, geteuid=lambda: 40001))
    monkeypatch.setattr(api, '_runtime_available', lambda: True)

    def binary(*args: Any, **kwargs: Any) -> int:
        stack = args[1]
        stack.callback(state.releases.append, 70)
        return 70

    def sealed(file: Any, stack: Any) -> int:
        state.sealed.append(file.data)
        stack.callback(state.releases.append, 71)
        return 71

    @contextmanager
    def source(argv: tuple[str, ...], **arguments: Any) -> Any:
        assert arguments == state.arguments
        state.prefix = argv
        at = argv.index('--bind')
        assert argv[at:at + 3] == ('--bind', str(state.work), '/workspace')
        state.active = True
        try:
            yield SimpleNamespace(argv=(*argv[:at], *argv[at + 3:argv.index('--')],
                '--tmpfs', '/workspace', '--ro-bind-data', '72', '/workspace/ACCEPTANCE.json',
                '--remount-ro', '/workspace', '--', 'discarded-source-launcher'), pass_fds=(72,))
        finally:
            state.active = False

    monkeypatch.setattr(api, '_sealed_binary', binary)
    monkeypatch.setattr(api, '_seal', sealed)
    monkeypatch.setattr(api, 'build_contract_mounts', source)
    state.options = {'case': state.case, 'work_root': state.work, 'binary_root': state.binary,
                     'expected_binary_sha256': 'c' * 64, 'expected_binary_bytes': 64,
                     'contract_arguments': state.arguments}
    return state


def test_fixed_program_sealed_stdin_and_case_arguments_are_only_exec_data(api: Any, structure: Any) -> None:
    with api.build_go_business_mounts(**structure.options) as plan:
        assert structure.active and not structure.releases
        at = plan.argv.index('--')
        prefix, launch = plan.argv[:at], plan.argv[at + 1:]
        for flag in ('--unshare-user', '--unshare-pid', '--unshare-ipc', '--unshare-uts',
                     '--unshare-net', '--die-with-parent', '--new-session', '--clearenv'):
            assert flag in prefix
        assert ('--cap-drop', 'ALL') == prefix[3:5]
        assert '--bind' not in prefix
        for path in ('/sdk', '/output', '/export', '/host', str(structure.binary), str(structure.work)):
            assert path not in prefix
        assert ('--perms', '0555', '--ro-bind-data', '70', '/program') == prefix[-10:-5]
        assert prefix[-5:] == ('--perms', '0444', '--ro-bind-data', '71', '/fixture-stdin')
        assert '/workspace/ACCEPTANCE.json' in prefix and '--remount-ro' in prefix
        assert plan.pass_fds == (72, 70, 71)
        assert launch[:5] == ('/usr/bin/python3', '-I', '-S', '-B', '-c')
        assert set(map(int, launch[6].split(','))) == set(plan.pass_fds)
        assert launch[7:] == structure.case.argv
        assert 'os.execv(\'/program\'' in launch[5] and 'dup2' in launch[5]
        assert all(value not in launch[5] for value in ('echo bad', '/host', 'fixture\n'))
        assert structure.sealed == [b'fixture\n']
        env = {prefix[i + 1]: prefix[i + 2] for i, v in enumerate(prefix) if v == '--setenv'}
        assert env == {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/tmp', 'TMPDIR': '/tmp'}
        with pytest.raises(FrozenInstanceError):
            plan.argv = ()
    assert not structure.active and set(structure.releases) == {70, 71}


@pytest.mark.parametrize('platform,marker,uid,euid', [
    ('win32', 'systemd', 40001, 40001), ('linux', '', 40001, 40001),
    ('linux', 'systemd', 0, 0), ('linux', 'systemd', 40001, 0),
])
def test_unavailable_environment_refuses_before_binary_open(
    api: Any, structure: Any, monkeypatch: pytest.MonkeyPatch,
    platform: str, marker: str, uid: int, euid: int,
) -> None:
    monkeypatch.setattr(api, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(api, 'os', SimpleNamespace(
        environ={'AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX': marker},
        getuid=lambda: uid, geteuid=lambda: euid))
    with (pytest.raises(api.GoBusinessNotAvailable, match='go_business_not_available'),
          api.build_go_business_mounts(**structure.options)):
        pytest.fail('unavailable boundary yielded')
    assert not structure.releases


@pytest.mark.parametrize('change', ['overlap', 'outputs', 'argv_nul', 'stdin_surrogate', 'bad_hash'])
def test_bad_inputs_fail_before_source_mount(api: Any, structure: Any, change: str) -> None:
    if change == 'overlap':
        structure.options['binary_root'] = structure.work / 'output'
    elif change == 'outputs':
        structure.arguments['ephemeral_dirs'] = ('build',)
    elif change == 'argv_nul':
        object.__setattr__(structure.case, 'argv', ('bad\0arg',))
    elif change == 'stdin_surrogate':
        object.__setattr__(structure.case, 'stdin', '\ud800')
    else:
        structure.options['expected_binary_sha256'] = 'X' * 64
    with pytest.raises(ValueError), api.build_go_business_mounts(**structure.options):
        pytest.fail('invalid plan yielded')


def test_uncertain_cleanup_retains_source_and_file_pins_until_outer_confirmation(
    api: Any, structure: Any,
) -> None:
    from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

    with (pytest.raises(api.GoBusinessCleanupIncomplete) as raised,
          api.build_go_business_mounts(**structure.options)):
        raise ProcessCleanupIncomplete('pipe EOF unknown')
    assert structure.active and not structure.releases
    raised.value.release_after_cleanup()
    assert not structure.active and set(structure.releases) == {70, 71}
    raised.value.release_after_cleanup()
    assert len(structure.releases) == 2


@pytest.mark.parametrize('error', [KeyboardInterrupt(), SystemExit()])
def test_clean_cancel_releases_all_fds_without_swallowing(
    api: Any, structure: Any, error: BaseException,
) -> None:
    with pytest.raises(type(error)) as raised, api.build_go_business_mounts(**structure.options):
        raise error
    assert raised.value is error and not structure.active
    assert set(structure.releases) == {70, 71}


def test_empty_arguments_still_exec_fixed_program(api: Any, structure: Any) -> None:
    structure.options['case'] = replace(structure.case, argv=(), stdin='')
    with api.build_go_business_mounts(**structure.options) as plan:
        launch = plan.argv[plan.argv.index('--') + 1:]
        assert launch[7:] == () and structure.sealed == [b'']


def test_missing_bwrap_or_sealing_support_is_explicitly_unavailable(
    api: Any, structure: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, '_runtime_available', lambda: False)
    with pytest.raises(api.GoBusinessNotAvailable), api.build_go_business_mounts(**structure.options):
        pytest.fail('missing runtime yielded a plan')
    assert not structure.releases


def test_source_contract_rejection_prevents_project_launch_and_releases_fds(
    api: Any, structure: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.skills.sandbox.project_contract_mounts import ContractMountError

    @contextmanager
    def reject(*args: Any, **arguments: Any) -> Any:
        assert arguments == structure.arguments
        raise ContractMountError('project_contract_mounts_rejected')
        yield

    monkeypatch.setattr(api, 'build_contract_mounts', reject)
    with pytest.raises(ContractMountError), api.build_go_business_mounts(**structure.options):
        pytest.fail('changed protected control accepted')
    assert set(structure.releases) == {70, 71}


def test_bootstrap_opens_only_fixture_dup2_then_closes_before_fixed_exec(api: Any) -> None:
    import builtins
    import errno

    events: list[Any] = []
    arguments = ['--ro-bind', '/host', '; echo bad', '\n', '', '-c', 'import os']

    def opened(path: str, flags: int) -> int:
        assert path == '/fixture-stdin' and flags == 7
        events.append(('open', path))
        return 80

    def closed(fd: int) -> None:
        events.append(('close', fd))
        if fd == 72:
            raise OSError(errno.EBADF, 'bwrap already consumed FD')

    fake_os = SimpleNamespace(O_RDONLY=1, O_NOFOLLOW=2, O_CLOEXEC=4, open=opened,
        dup2=lambda fd, target, **kw: events.append(('dup2', fd, target, kw)), close=closed,
        execv=lambda program, argv: events.append(('execv', program, argv)))
    modules = {'os': fake_os, 'sys': SimpleNamespace(argv=['-c', '70,71,72', *arguments]),
               'errno': errno}
    imports = dict(vars(builtins), __import__=lambda name, *args, **kwargs: modules[name])
    # Execute only the fixed trusted string with fake OS calls, never project code.
    exec(compile(api._STDIN_AND_EXEC, '<trusted-bootstrap>', 'exec'),  # noqa: S102
         {'__builtins__': imports})
    assert events == [('open', '/fixture-stdin'), ('dup2', 80, 0, {'inheritable': True}),
                      ('close', 80), ('close', 70), ('close', 71), ('close', 72),
                      ('execv', '/program', ['/program', *arguments])]


@pytest.fixture
def binary_boundary(api: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Fake kernel calls exercise measurement on Windows without loading ELF."""
    import io

    monkeypatch.setattr(api, 'sys', SimpleNamespace(platform='linux'))
    raw = bytearray(64)
    raw[:7] = b'\x7fELF\x02\x01\x01'
    raw[16:24] = b'\x02\x00\x3e\x00\x01\x00\x00\x00'
    info = SimpleNamespace(st_dev=1, st_ino=10, st_mode=stat.S_IFREG | 0o555,
        st_uid=40001, st_gid=40001, st_nlink=1, st_size=64, st_mtime_ns=1, st_ctime_ns=1)
    state = SimpleNamespace(raw=raw, info=info, data=bytearray(), closes=[], opens=[],
                            seals=0, source=None, swapped=False, stat_count=0, checks=[])

    def pin(root: Path, stack: Any, pins: list[Any]) -> int:
        pins.append(SimpleNamespace(check=lambda: state.checks.append('pin')))
        stack.callback(state.closes.append, 60)
        return 60

    def opened(path: str, flags: int, **kwargs: Any) -> int:
        state.opens.append((path, flags, kwargs))
        if path == 'project':
            state.source = io.BytesIO(state.raw)
            assert kwargs == {'dir_fd': 60}
            assert flags & os.O_RDONLY == os.O_RDONLY
            assert flags & 2 and flags & 4 and flags & 8
            return 61
        assert path == '/proc/self/fd/62' and not kwargs
        return 63

    def info_for(fd: int) -> Any:
        if fd == 61:
            return state.info
        return SimpleNamespace(**(vars(state.info) | {'st_ino': 20, 'st_nlink': 0,
                                                      'st_size': len(state.data)}))

    def checked_stat(name: str, **kwargs: Any) -> Any:
        assert name == 'project' and kwargs == {'dir_fd': 60, 'follow_symlinks': False}
        state.stat_count += 1
        if state.swapped and state.stat_count > 1:
            return SimpleNamespace(**(vars(state.info) | {'st_ino': 99}))
        return state.info

    def write(fd: int, data: Any) -> int:
        assert fd == 62
        take = min(7, len(data))
        state.data.extend(data[:take])
        return take

    def control(fd: int, command: int, value: int = 0) -> int:
        if command == 1033:
            state.seals = value
        return state.seals if command == 1034 else 0

    monkeypatch.setattr(api, '_pin_directory', pin)
    monkeypatch.setattr(api, 'os', SimpleNamespace(stat=checked_stat, open=opened, fstat=info_for,
        close=state.closes.append, O_RDONLY=0, O_NOFOLLOW=2, O_NONBLOCK=4, O_CLOEXEC=8,
        O_ACCMODE=3, MFD_CLOEXEC=1, MFD_ALLOW_SEALING=2,
        getuid=lambda: 40001, read=lambda fd, count: state.source.read(count), write=write,
        memfd_create=lambda name, flags: 62, fchmod=lambda fd, mode: None,
        get_inheritable=lambda fd: False))
    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(
        F_ADD_SEALS=1033, F_GET_SEALS=1034, F_GETFL=3, fcntl=control))
    state.digest = hashlib.sha256(raw).hexdigest()
    return state


def test_binary_is_read_hashed_and_sealed_with_short_writes(api: Any, binary_boundary: Any) -> None:
    state = binary_boundary
    with ExitStack() as stack:
        fd = api._sealed_binary(Path('/binary'), stack, expected_sha256=state.digest, expected_bytes=64)
        assert fd == 63 and not state.closes and state.seals == 15
        assert state.data == state.raw and state.checks == ['pin']
    assert state.closes == [63, 62, 61, 60]


@pytest.mark.parametrize('fault', ['sha', 'size', 'mode', 'owner', 'nlink', 'symlink',
                                  'header', 'arch', 'swapped', 'grown', 'short'])
def test_binary_substitution_and_unsafe_identity_fail_closed(
    api: Any, binary_boundary: Any, fault: str,
) -> None:
    state = binary_boundary
    if fault == 'mode':
        state.info.st_mode = stat.S_IFREG | 0o755
    elif fault == 'owner':
        state.info.st_uid = 999
    elif fault == 'nlink':
        state.info.st_nlink = 2
    elif fault == 'symlink':
        state.info.st_mode = stat.S_IFLNK | 0o555
    elif fault == 'header':
        state.raw[:4] = b'NOPE'
        state.digest = hashlib.sha256(state.raw).hexdigest()
    elif fault == 'arch':
        state.raw[18] = 3
        state.digest = hashlib.sha256(state.raw).hexdigest()
    elif fault == 'swapped':
        state.swapped = True
    elif fault == 'grown':
        state.raw.extend(b'x')
    elif fault == 'short':
        state.raw.pop()
    with ExitStack() as stack, pytest.raises(ValueError):
        api._sealed_binary(Path('/binary'), stack,
            expected_sha256='0' * 64 if fault == 'sha' else state.digest,
            expected_bytes=65 if fault == 'size' else 64)
    assert 60 in state.closes


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux sealed FD check; not native CLI proof')
def test_linux_binary_is_independently_measured_and_sealed(api: Any, tmp_path: Path) -> None:
    if sys.platform != 'linux':
        pytest.skip('Linux sealed FD test only; not native CLI proof')
    raw = bytearray(64)
    raw[:7] = b'\x7fELF\x02\x01\x01'
    raw[16:24] = b'\x02\x00\x3e\x00\x01\x00\x00\x00'
    root = tmp_path / 'binary'
    root.mkdir(mode=0o700)
    path = root / 'project'
    path.write_bytes(raw)
    path.chmod(0o555)
    digest = hashlib.sha256(raw).hexdigest()
    with ExitStack() as stack:
        descriptor = api._sealed_binary(root, stack, expected_sha256=digest, expected_bytes=64)
        import fcntl
        assert fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & 15 == 15
        assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        assert os.read(descriptor, 100) == raw
        assert stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o555
        with pytest.raises(OSError):
            os.write(descriptor, b'x')
    with pytest.raises(OSError):
        os.fstat(descriptor)
    with ExitStack() as stack, pytest.raises(ValueError):
        api._sealed_binary(root, stack, expected_sha256='0' * 64, expected_bytes=64)
