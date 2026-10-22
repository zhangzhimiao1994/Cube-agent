"""Binary observations only: no project code is executed by these tests."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import stat
import struct
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

MODULE = 'agent_hub.skills.sandbox.go_project_binary_export'
NATIVE = pytest.mark.skipif(sys.platform != 'linux', reason='requires native Linux FD semantics')
ROOT = pytest.mark.skipif(
    sys.platform != 'linux' or not hasattr(os, 'getuid') or os.getuid() != 0,
    reason='requires native Linux Root ownership; Windows is not native proof',
)


@pytest.fixture
def api() -> ModuleType:
    assert importlib.util.find_spec(MODULE) is not None, 'binary export helper is missing'
    return importlib.import_module(MODULE)


def _elf(payload: bytes = b'never execute this fixture') -> bytes:
    # A complete ELF64 little-endian x86-64 header; this is not runnable code.
    return struct.pack('<16sHHIQQQIHHHHHH', b'\x7fELF\x02\x01\x01' + b'\0' * 9,
                       2, 62, 1, 0, 0, 0, 0, 64, 0, 0, 0, 0, 0) + payload


def _metadata(raw: bytes) -> dict[str, object]:
    return {'filename': 'project', 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}


def _kwargs(raw: bytes) -> dict[str, Any]:
    return {'expected_sha256': hashlib.sha256(raw).hexdigest(), 'expected_bytes': len(raw),
            'maximum': len(raw), 'deadline': time.monotonic() + 10}


def test_module_exports_fail_closed_off_linux(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    assert importlib.util.find_spec(MODULE) is not None, 'binary export helper is missing'
    api = importlib.import_module(MODULE)
    monkeypatch.setattr(api, 'sys', type('Platform', (), {'platform': 'win32'}))
    functions: list[Callable[[], object]] = [
        lambda: api.seal_go_project_binary(tmp_path / 'output', maximum=100,
                                           deadline=time.monotonic() + 10),
        lambda: api.measure_go_project_binary(tmp_path / 'output', maximum=100,
                                              deadline=time.monotonic() + 10),
        lambda: api.export_go_project_binary(tmp_path / 'output', tmp_path / 'export',
                                             **_kwargs(_elf())),
        lambda: api.measure_go_project_export(tmp_path, allowed_gid=61112,
            expected_dynamic_uid=61111, service_uid=61112, **_kwargs(_elf())),
        lambda: api.observe_go_project_binary(0, gid=61112, service_uid=61112,
                                               maximum=100, deadline=time.monotonic() + 10),
    ]
    for call in functions:
        with pytest.raises(api.GoProjectBinaryExportError, match='^go_project_binary_export_rejected$'):
            call()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('maximum', [True, 0, -1, 63, 100.5])
def test_rejects_invalid_caller_budget(api: ModuleType, maximum: object) -> None:
    with pytest.raises(api.GoProjectBinaryExportError):
        api._limits(maximum, time.monotonic() + 10)


@pytest.mark.parametrize('deadline', [True, 0, -1, float('nan'), float('inf'), 'later', 10**1000])
def test_rejects_invalid_or_expired_deadline(api: ModuleType, deadline: object) -> None:
    with pytest.raises(api.GoProjectBinaryExportError):
        api._limits(100, deadline)


@pytest.mark.parametrize('osabi', [0, 3])
@pytest.mark.parametrize('kind', [2, 3])
def test_header_accepts_linux_exec_and_pie(api: ModuleType, osabi: int, kind: int) -> None:
    header = bytearray(_elf()[:64])
    header[7] = osabi
    header[16] = kind
    api._header(bytes(header))


@pytest.mark.parametrize('offset,value', [(0, 0), (4, 1), (5, 2), (6, 0), (7, 9), (8, 1),
                                         (16, 1), (18, 3), (20, 0), (52, 0)])
def test_header_rejects_other_formats_platforms_and_versions(api: ModuleType, offset: int,
                                                          value: int) -> None:
    header = bytearray(_elf()[:64])
    header[offset] = value
    with pytest.raises(api.GoProjectBinaryExportError):
        api._header(bytes(header))


@pytest.mark.parametrize('length', [0, 20, 63, 65])
def test_header_requires_all_64_bytes(api: ModuleType, length: int) -> None:
    with pytest.raises(api.GoProjectBinaryExportError):
        api._header(_elf()[:length])


@pytest.mark.parametrize('uid', [True, 0, -1, 2**32 - 1, float('inf'), 61111.0])
def test_uid_must_be_a_valid_nonroot_linux_uid(api: ModuleType, uid: object) -> None:
    with pytest.raises(api.GoProjectBinaryExportError):
        api._uid(uid)


@pytest.mark.parametrize('digest,size', [('0' * 63, 100), ('A' * 64, 100),
    ('g' * 64, 100), ('0' * 64, True), ('0' * 64, 63), ('0' * 64, 101)])
def test_expected_metadata_must_be_exact_and_within_budget(api: ModuleType, digest: str,
                                                        size: int) -> None:
    with pytest.raises(api.GoProjectBinaryExportError):
        api._expected(digest, size, 100)


@pytest.mark.parametrize('mode,uid,nlink,size,owner,service_uid', [
    (0o755, 61111, 1, 100, 61111, None), (0o555, 0, 1, 100, None, 61112),
    (0o555, 61112, 1, 100, None, 61112), (0o555, 61113, 1, 100, 61111, None),
    (0o555, 61111, 2, 100, 61111, None), (0o555, 61111, 1, 101, 61111, None),
    (0o555, 61111, 1, 63, 61111, None),
])
def test_file_policy_rejects_unsafe_sealed_metadata(api: ModuleType, mode: int, uid: int,
    nlink: int, size: int, owner: int | None, service_uid: int | None,
) -> None:
    info = os.stat_result((stat.S_IFREG | mode, 1, 1, nlink, uid, 61112, size, 0, 0, 0))
    with pytest.raises(api.GoProjectBinaryExportError):
        api._shape(info, owner=owner, maximum=100, sealed=True, service_uid=service_uid)


def test_file_policy_accepts_unknown_dynamic_uid_but_never_service_uid(api: ModuleType) -> None:
    info = os.stat_result((stat.S_IFREG | 0o555, 1, 1, 1, 65000, 65000, 100, 0, 0, 0))
    api._shape(info, owner=None, maximum=100, sealed=True, service_uid=61112)
    api._limits(10**100, time.monotonic() + 10)
    api._expected('0' * 64, 10**99, 10**100)


def test_directory_rechecks_rewind_a_shared_cursor(api: ModuleType,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    # A unit model of shared readdir offsets, not a native FD verification.
    exhausted = False

    @contextmanager
    def scandir(fd: int) -> Any:
        nonlocal exhausted
        assert fd == 73
        yield iter([] if exhausted else [SimpleNamespace(name='project')])
        exhausted = True

    def rewind(fd: int, offset: int, origin: int) -> int:
        nonlocal exhausted
        assert (fd, offset, origin) == (73, 0, os.SEEK_SET)
        exhausted = False
        return 0

    monkeypatch.setattr(api.os, 'scandir', scandir)
    monkeypatch.setattr(api.os, 'lseek', rewind)
    api._only(73, 'project', time.monotonic() + 10)
    api._only(73, 'project', time.monotonic() + 10)


def test_transfer_retries_partial_writes(api: ModuleType, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / 'transfer'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    real_write = os.write

    def short_write(fd: int, raw: bytes) -> int:
        return real_write(fd, raw[:3])

    monkeypatch.setattr(api.os, 'write', short_write)
    try:
        api._write_all(descriptor, b'complete bytes including last byte', time.monotonic() + 10)
    finally:
        os.close(descriptor)
    assert path.read_bytes() == b'complete bytes including last byte'


@pytest.mark.parametrize('count', [0, -1, 100])
def test_transfer_rejects_missing_or_impossible_progress(api: ModuleType, count: int,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.os, 'write', lambda fd, raw: count)
    with pytest.raises(api.GoProjectBinaryExportError):
        api._write_all(-1, b'no progress', time.monotonic() + 10)


@pytest.mark.parametrize('finished_at', [10.0, 11.0])
def test_seal_does_not_start_fsync_after_chmod_exhausts_deadline(
    api: ModuleType, monkeypatch: pytest.MonkeyPatch, finished_at: float,
) -> None:
    # A syscall timing model, not proof of native Linux chmod/fsync semantics.
    now = 9.0

    def chmod(descriptor: int, mode: int) -> None:
        nonlocal now
        assert (descriptor, mode) == (73, 0o555)
        now = finished_at

    def fsync(descriptor: int) -> None:
        pytest.fail('sealing started another syscall after the deadline')

    monkeypatch.setattr(api, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(api, 'os', SimpleNamespace(fchmod=chmod, fsync=fsync))
    monkeypatch.setattr(api, 'time', SimpleNamespace(monotonic=lambda: now))
    with pytest.raises(api.GoProjectBinaryExportError, match='^go_project_binary_export_rejected$'):
        api._seal(73, 10.0)


@pytest.fixture
def output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, bytes]:
    if sys.platform != 'linux' or os.getuid() == 0:
        pytest.skip('native runner fixture requires a non-root Linux uid')
    monkeypatch.setenv('AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX', 'systemd')
    root = tmp_path / 'output'
    root.mkdir(mode=0o700)
    raw = _elf()
    (root / 'project').write_bytes(raw)
    (root / 'project').chmod(0o755)
    return root, raw


@NATIVE
def test_seal_755_then_remeasure_returns_only_exact_metadata(api: ModuleType,
                                                         output: tuple[Path, bytes]) -> None:
    root, raw = output
    result = api.seal_go_project_binary(root, maximum=len(raw), deadline=time.monotonic() + 10)
    assert result == _metadata(raw)
    assert stat.S_IMODE((root / 'project').stat().st_mode) == 0o555
    assert api.measure_go_project_binary(root, maximum=len(raw), deadline=time.monotonic() + 10) == result


@NATIVE
def test_measure_rejects_unsealed_755(api: ModuleType, output: tuple[Path, bytes]) -> None:
    root, raw = output
    with pytest.raises(api.GoProjectBinaryExportError):
        api.measure_go_project_binary(root, maximum=len(raw), deadline=time.monotonic() + 10)


@NATIVE
@pytest.mark.parametrize('mode', [0o775, 0o757, 0o4755, 0o2755])
def test_seal_rejects_writable_or_special_bits(api: ModuleType, output: tuple[Path, bytes],
                                            mode: int) -> None:
    root, raw = output
    (root / 'project').chmod(mode)
    with pytest.raises(api.GoProjectBinaryExportError):
        api.seal_go_project_binary(root, maximum=len(raw), deadline=time.monotonic() + 10)


@NATIVE
@pytest.mark.parametrize('fault', ['symlink', 'hardlink', 'fifo', 'parent-symlink', 'oversize',
                                  'relative', 'dotdot', 'marker'])
def test_runner_rejects_unsafe_input(api: ModuleType, output: tuple[Path, bytes], fault: str,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != 'linux':
        pytest.skip('requires Linux FIFO and link semantics')
    root, raw = output
    leaf = root / 'project'
    maximum = len(raw)
    if fault == 'symlink':
        leaf.rename(root / 'other')
        leaf.symlink_to('other')
    elif fault == 'hardlink':
        os.link(leaf, root / 'alias')
    elif fault == 'fifo':
        leaf.unlink()
        os.mkfifo(leaf)
    elif fault == 'parent-symlink':
        alias = root.parent / 'alias'
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    elif fault == 'oversize':
        maximum -= 1
    elif fault == 'relative':
        root = Path('output')
    elif fault == 'dotdot':
        root = root / '..' / 'output'
    elif fault == 'marker':
        monkeypatch.delenv('AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX')
    with pytest.raises(api.GoProjectBinaryExportError):
        api.seal_go_project_binary(root, maximum=maximum, deadline=time.monotonic() + 10)


@NATIVE
@pytest.mark.parametrize('offset,value', [(0, 0), (4, 1), (5, 2), (6, 0), (7, 9), (8, 1),
                                         (16, 1), (18, 3), (20, 0), (52, 0)])
def test_seal_rejects_invalid_elf64_linux_amd64_header(api: ModuleType,
    output: tuple[Path, bytes], offset: int, value: int,
) -> None:
    root, raw = output
    data = bytearray(raw)
    data[offset] = value
    (root / 'project').write_bytes(data)
    with pytest.raises(api.GoProjectBinaryExportError):
        api.seal_go_project_binary(root, maximum=len(raw), deadline=time.monotonic() + 10)


@NATIVE
def test_seal_detects_mutation_during_chmod_remeasurement(api: ModuleType,
    output: tuple[Path, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != 'linux':
        pytest.skip('requires Linux fchmod')
    root, raw = output
    real_chmod = os.fchmod

    def chmod(descriptor: int, mode: int) -> None:
        (root / 'project').write_bytes(raw[:-1] + b'!')
        real_chmod(descriptor, mode)

    monkeypatch.setattr(api.os, 'fchmod', chmod)
    with pytest.raises(api.GoProjectBinaryExportError):
        api.seal_go_project_binary(root, maximum=len(raw), deadline=time.monotonic() + 10)


@NATIVE
def test_measure_detects_leaf_replacement_while_reading(api: ModuleType,
    output: tuple[Path, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, raw = output
    (root / 'project').chmod(0o555)
    real_read = os.read
    changed = False

    def read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, count)
        if not changed:
            changed = True
            (root / 'project').rename(root / 'old')
            (root / 'project').write_bytes(raw)
            (root / 'project').chmod(0o555)
        return chunk

    monkeypatch.setattr(api.os, 'read', read)
    with pytest.raises(api.GoProjectBinaryExportError):
        api.measure_go_project_binary(root, maximum=len(raw), deadline=time.monotonic() + 10)


@ROOT
@pytest.mark.parametrize('fault', [None, 'extra', 'hash', 'size', 'budget', 'root-owner',
    'service-owner', 'wrong-owner', 'mode', 'hardlink', 'symlink', 'fifo', 'directory',
    'export-mode', 'export-gid', 'stage-mode', 'export-symlink', 'stage-symlink', 'truncated'])
def test_root_independently_measures_fixed_export(api: ModuleType, tmp_path: Path,
                                                fault: str | None) -> None:
    if sys.platform != 'linux':
        pytest.skip('requires Linux ownership')
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    export = stage / 'export'
    export.mkdir(mode=0o700)
    os.chown(export, 0, 61112)
    export.chmod(0o770)
    raw = _elf()
    leaf = export / 'project'
    leaf.write_bytes(raw)
    os.chown(leaf, 61111, 61112)
    leaf.chmod(0o555)
    kwargs = _kwargs(raw)
    if fault == 'extra':
        (export / 'README').touch()
    elif fault == 'hash':
        kwargs['expected_sha256'] = '0' * 64
    elif fault == 'size':
        kwargs['expected_bytes'] -= 1
    elif fault == 'budget':
        kwargs['maximum'] -= 1
    elif fault in {'root-owner', 'service-owner', 'wrong-owner'}:
        os.chown(leaf, {'root-owner': 0, 'service-owner': 61112, 'wrong-owner': 61113}[fault], -1)
    elif fault == 'mode':
        leaf.chmod(0o755)
    elif fault == 'hardlink':
        os.link(leaf, stage / 'alias')
    elif fault in {'symlink', 'fifo', 'directory'}:
        leaf.unlink()
        if fault == 'symlink':
            leaf.symlink_to('/etc/passwd')
        elif fault == 'fifo':
            os.mkfifo(leaf)
        else:
            leaf.mkdir()
    elif fault == 'export-mode':
        export.chmod(0o777)
    elif fault == 'export-gid':
        os.chown(export, 0, 61113)
    elif fault == 'stage-mode':
        stage.chmod(0o755)
    elif fault == 'export-symlink':
        export.rename(stage / 'real')
        export.symlink_to('real', target_is_directory=True)
    elif fault == 'stage-symlink':
        real = tmp_path / 'real-stage'
        stage.rename(real)
        stage.symlink_to(real, target_is_directory=True)
    elif fault == 'truncated':
        leaf.write_bytes(raw[:30])
    call: Callable[[], dict[str, object]] = lambda: api.measure_go_project_export(stage, allowed_gid=61112,
        expected_dynamic_uid=61111, service_uid=61112, **kwargs)
    if fault is None:
        assert call() == _metadata(raw)
        assert sorted(p.name for p in stage.iterdir()) == ['export']
    else:
        with pytest.raises(api.GoProjectBinaryExportError):
            call()


@ROOT
@pytest.mark.parametrize('dynamic_uid,service_uid', [(0, 61112), (61112, 61112),
    (True, 61112), (-1, 61112), (2**32 - 1, 61112), (61111, 0)])
def test_root_rejects_untrusted_owner_policy_before_read(api: ModuleType, tmp_path: Path,
                                                      dynamic_uid: int, service_uid: int) -> None:
    with pytest.raises(api.GoProjectBinaryExportError):
        api.measure_go_project_export(tmp_path, allowed_gid=61112,
            expected_dynamic_uid=dynamic_uid, service_uid=service_uid, **_kwargs(_elf()))


@ROOT
@pytest.mark.parametrize('fault', [None, 'unknown-owner', 'root-owner', 'service-owner',
    'stage-mode', 'export-mode', 'export-gid', 'extra', 'parent-replaced', 'leaf-replaced',
    'late-mutation', 'expired', 'root-file', 'invalid-fd'])
def test_root_fd_observation_is_independent_and_pinned(api: ModuleType, tmp_path: Path,
    fault: str | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != 'linux':
        pytest.skip('requires Linux ownership and directory FDs')
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    export = stage / 'export'
    export.mkdir(mode=0o700)
    os.chown(export, 0, 61112)
    export.chmod(0o770)
    raw = _elf()
    leaf = export / 'project'
    leaf.write_bytes(raw)
    os.chown(leaf, 65000 if fault == 'unknown-owner' else 61111, 61112)
    leaf.chmod(0o555)
    descriptor = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        if fault in {'root-owner', 'service-owner'}:
            os.chown(leaf, 0 if fault == 'root-owner' else 61112, -1)
        elif fault == 'stage-mode':
            stage.chmod(0o755)
        elif fault == 'export-mode':
            export.chmod(0o2770)
        elif fault == 'export-gid':
            os.chown(export, 0, 61113)
        elif fault == 'extra':
            (export / 'extra').touch()
        elif fault in {'parent-replaced', 'leaf-replaced'}:
            real_read = os.read
            changed = False

            def read(fd: int, count: int) -> bytes:
                nonlocal changed
                chunk = real_read(fd, count)
                if not changed:
                    changed = True
                    if fault == 'parent-replaced':
                        stage.rename(tmp_path / 'old')
                        stage.mkdir(mode=0o700)
                    else:
                        leaf.rename(export / 'old')
                        leaf.write_bytes(raw)
                        os.chown(leaf, 61111, 61112)
                        leaf.chmod(0o555)
                return chunk

            monkeypatch.setattr(api.os, 'read', read)
        elif fault == 'root-file':
            os.close(descriptor)
            descriptor = os.open(leaf, os.O_RDONLY | os.O_CLOEXEC)
        deadline = 0.0 if fault == 'expired' else time.monotonic() + 10
        call: Callable[[], dict[str, object]] = lambda: api.observe_go_project_binary(
            -1 if fault == 'invalid-fd' else descriptor,
            gid=61112, service_uid=61112, maximum=len(raw), deadline=deadline)
        if fault in {None, 'unknown-owner', 'late-mutation'}:
            first = call()
            assert first == _metadata(raw)
            if fault == 'late-mutation':
                leaf.write_bytes(raw[:-1] + b'!')
                assert call() != first
            assert sorted(p.name for p in stage.iterdir()) == ['export']
        else:
            with pytest.raises(api.GoProjectBinaryExportError):
                call()
    finally:
        os.close(descriptor)


def _runner_child(api: ModuleType, root: Path, raw: bytes, fault: str | None) -> dict[str, object]:
    if sys.platform != 'linux':
        raise AssertionError('native fixture only')
    os.setgroups([61112])
    os.setgid(61112)
    os.setuid(61111)
    os.environ['AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX'] = 'systemd'
    output = root / 'work' / 'output'
    output.mkdir(mode=0o700)
    leaf = output / 'project'
    leaf.write_bytes(raw)
    leaf.chmod(0o755)
    sealed = api.seal_go_project_binary(output, maximum=len(raw), deadline=time.monotonic() + 10)
    assert sealed == _metadata(raw)
    assert api.measure_go_project_binary(output, maximum=len(raw),
                                         deadline=time.monotonic() + 10) == sealed
    export = root / 'export'
    kwargs = _kwargs(raw)
    real_write, real_fsync, real_read = os.write, os.fsync, os.read
    changed = False

    def write(fd: int, chunk: bytes) -> int:
        nonlocal changed
        count = real_write(fd, chunk[:1] if fault == 'short-write' else chunk)
        if not changed:
            changed = True
            if fault == 'extra-during-copy':
                (export / 'extra').touch()
            elif fault == 'source-during-copy':
                leaf.chmod(0o755)
                leaf.write_bytes(raw[:-1] + b'!')
                leaf.chmod(0o555)
        return count

    def read(fd: int, count: int) -> bytes:
        chunk = real_read(fd, count)
        if fault == 'deadline-during-read':
            api.time.monotonic = lambda: kwargs['deadline'] + 1
        return chunk

    def fsync(fd: int) -> None:
        if fault == 'fsync':
            raise OSError('private host pathname')
        real_fsync(fd)

    if fault == 'hash':
        kwargs['expected_sha256'] = '0' * 64
    elif fault == 'size':
        kwargs['expected_bytes'] -= 1
    elif fault == 'unsealed':
        leaf.chmod(0o755)
    elif fault == 'existing':
        (export / 'project').write_bytes(b'keep me')
    elif fault == 'extra':
        (export / 'extra').touch()
    elif fault == 'hardlink':
        os.link(leaf, output / 'alias')
    api.os.write, api.os.fsync, api.os.read = write, fsync, read
    try:
        result = api.export_go_project_binary(output, export, **kwargs)
        assert result == sealed
        assert sorted(p.name for p in export.iterdir()) == ['project']
        assert (export / 'project').read_bytes() == raw
        assert stat.S_IMODE((export / 'project').stat().st_mode) == 0o555
        return {'metadata': result}
    except api.GoProjectBinaryExportError as error:
        assert str(error) == 'go_project_binary_export_rejected'
        if fault == 'existing':
            assert (export / 'project').read_bytes() == b'keep me'
        return {'rejected': True}
    finally:
        api.os.write, api.os.fsync, api.os.read = real_write, real_fsync, real_read


@ROOT
@pytest.mark.parametrize('fault', [None, 'short-write', 'hash', 'size', 'unsealed', 'existing',
    'extra', 'extra-during-copy', 'source-during-copy', 'fsync', 'deadline-during-read',
    'hardlink', 'export-mode', 'export-owner', 'export-gid'])
def test_native_runner_export_and_root_observation(api: ModuleType, fault: str | None) -> None:
    if sys.platform != 'linux':
        pytest.skip('requires Linux fork and real uid ownership')
    import json
    import select
    import signal

    # Root provisions directories; a fork drops real/effective identities before
    # using the production runner API. No binary fixture is ever executed.
    with tempfile.TemporaryDirectory(prefix='go-binary-export-') as temporary:
        root = Path(temporary)
        root.chmod(0o711)
        work, export, stage = (root / name for name in ('work', 'export', 'stage'))
        work.mkdir(mode=0o700)
        os.chown(work, 61111, 61112)
        export.mkdir(mode=0o700)
        os.chown(export, 61111 if fault == 'export-owner' else 0,
                 61113 if fault == 'export-gid' else 61112)
        export.chmod(0o777 if fault == 'export-mode' else 0o770)
        stage.mkdir(mode=0o700)
        raw = _elf(b'multi-chunk-complete-hash\0' * 3000)
        reader, writer = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(reader)
            try:
                result = _runner_child(api, root, raw, fault)
            except BaseException as error:  # noqa: BLE001 - report fork failure before os._exit.
                result = {'unexpected': type(error).__name__}
            try:
                os.write(writer, json.dumps(result).encode('ascii'))
            finally:
                os.close(writer)
                os._exit(0)
        os.close(writer)
        try:
            readable, _, _ = select.select([reader], [], [], 15)
            if not readable:
                os.kill(child, signal.SIGKILL)
                pytest.fail('native helper did not finish within the test deadline')
            observed = json.loads(os.read(reader, 4096))
        finally:
            os.close(reader)
            os.waitpid(child, 0)
        if fault in {None, 'short-write'}:
            assert observed == {'metadata': _metadata(raw)}
            export.rename(stage / 'export')
            parent = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                assert api.observe_go_project_binary(parent, gid=61112, service_uid=61112,
                    maximum=len(raw), deadline=time.monotonic() + 10) == observed['metadata']
            finally:
                os.close(parent)
        else:
            assert observed == {'rejected': True}
