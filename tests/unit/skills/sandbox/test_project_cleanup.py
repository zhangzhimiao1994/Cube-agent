from __future__ import annotations

import importlib
import importlib.util
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def api() -> Any:
    name = 'agent_hub.skills.sandbox.project_cleanup'
    assert importlib.util.find_spec(name) is not None, 'private-stage cleanup helper missing'
    return importlib.import_module(name)


def info(*, mode: int = stat.S_IFREG | 0o444, uid: int = 10001,
         links: int = 1, device: int = 7, inode: int = 12) -> Any:
    return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=10001,
        st_nlink=links, st_dev=device, st_ino=inode, st_mtime_ns=123)


@pytest.fixture
def native_api(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = api()
    # FD tests below use fake os primitives, not a claim of native Windows support.
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='linux'))
    return module


@pytest.mark.parametrize('foreign', [False, True])
def test_fd_permission_repair_chowns_only_foreign_uid_before_chmod(
    monkeypatch: pytest.MonkeyPatch, foreign: bool, native_api: Any,
) -> None:
    module = native_api
    observed = info(uid=10001 if foreign else 1234)
    events: list[tuple[object, ...]] = []

    def chown(fd: int, uid: int, gid: int) -> None:
        events.append(('chown', fd, uid, gid))
        observed.st_uid = uid

    def chmod(fd: int, mode: int) -> None:
        assert observed.st_uid == 1234
        events.append(('chmod', fd, mode))
        observed.st_mode = stat.S_IFREG | mode

    monkeypatch.setattr(module, 'os', SimpleNamespace(
        fstat=lambda _: observed, fchown=chown, fchmod=chmod))
    module._prepare_fd(41, observed, owner=1234, device=7)
    assert events == ([('chown', 41, 1234, -1)] if foreign else []) + [('chmod', 41, 0o600)]


@pytest.mark.parametrize('defect', ['hardlink', 'fifo', 'device', 'inode', 'mtime'])
def test_fd_refuses_external_or_changed_inode_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch, defect: str, native_api: Any,
) -> None:
    module = native_api
    expected = info()
    observed = info()
    if defect == 'hardlink':
        expected.st_nlink = observed.st_nlink = 2
    elif defect == 'fifo':
        expected.st_mode = observed.st_mode = stat.S_IFIFO | 0o600
    elif defect == 'device':
        expected.st_dev = observed.st_dev = 8
    elif defect == 'inode':
        observed.st_ino += 1
    else:
        observed.st_mtime_ns += 1

    def forbidden(*_: object) -> None:
        pytest.fail('unsafe inode was mutated')

    monkeypatch.setattr(module, 'os', SimpleNamespace(
        fstat=lambda _: observed, fchown=forbidden, fchmod=forbidden))
    with pytest.raises(module.ProjectStageCleanupError):
        module._prepare_fd(41, expected, owner=1234, device=7)


def test_chown_failure_is_preserved_and_never_followed_by_chmod(monkeypatch: pytest.MonkeyPatch, native_api: Any) -> None:
    module = native_api
    expected = info()
    failure = PermissionError('missing CAP_CHOWN')

    def chown(*_: object) -> None:
        raise failure

    def chmod(*_: object) -> None:
        pytest.fail('chmod attempted after failed chown')

    monkeypatch.setattr(module, 'os', SimpleNamespace(fstat=lambda _: expected, fchown=chown, fchmod=chmod))
    with pytest.raises(PermissionError) as caught:
        module._prepare_fd(41, expected, owner=1234, device=7)
    assert caught.value is failure


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'cross_device'])
def test_fd_walk_never_opens_link_or_unsafe_entry(monkeypatch: pytest.MonkeyPatch, kind: str, native_api: Any) -> None:
    module = native_api
    root = info(mode=stat.S_IFDIR | 0o700, uid=1234)
    member = info(mode=stat.S_IFLNK | 0o777) if kind == 'symlink' else info()
    if kind == 'hardlink':
        member.st_nlink = 2
    elif kind == 'fifo':
        member.st_mode = stat.S_IFIFO | 0o600
    elif kind == 'cross_device':
        member.st_dev = 8
    prepared: list[int] = []

    def forbidden(*_: object, **__: object) -> None:
        pytest.fail('unsafe entry was opened')

    monkeypatch.setattr(module, '_prepare_fd', lambda fd, *a, **kw: prepared.append(fd))
    monkeypatch.setattr(module, 'os', SimpleNamespace(
        listdir=lambda _: ['entry'], stat=lambda *a, **kw: member, fstat=lambda _: root,
        open=forbidden))
    if kind == 'symlink':
        module._walk_fd(41, root, owner=1234, device=7)
    else:
        with pytest.raises(module.ProjectStageCleanupError):
            module._walk_fd(41, root, owner=1234, device=7)
    assert prepared == [41]


def test_fd_walk_checks_member_identity_before_permission_mutation(monkeypatch: pytest.MonkeyPatch, native_api: Any) -> None:
    module = native_api
    root = info(mode=stat.S_IFDIR | 0o700, uid=1234)
    member = info(mode=stat.S_IFDIR | 0o555, inode=13)
    replacement = info(mode=stat.S_IFDIR | 0o555, inode=14)
    opened = False
    prepared: list[int] = []
    closed: list[int] = []

    def open_child(*_: object, **__: object) -> int:
        nonlocal opened
        opened = True
        return 42

    monkeypatch.setattr(module, '_directory_flags', lambda: 1)
    monkeypatch.setattr(module, '_prepare_fd', lambda fd, *a, **kw: prepared.append(fd))
    monkeypatch.setattr(module, 'os', SimpleNamespace(
        listdir=lambda fd: ['entry'] if fd == 41 else [],
        stat=lambda *a, **kw: replacement if opened else member,
        fstat=lambda fd: root if fd == 41 else member, open=open_child, close=closed.append))
    with pytest.raises(module.ProjectStageCleanupError):
        module._walk_fd(41, root, owner=1234, device=7)
    assert prepared == [41] and closed == [42]


@pytest.mark.parametrize('defect', ['root_owner', 'parent_owner', 'root_public', 'parent_public', 'root_device'])
def test_linux_stage_anchor_must_be_private_owned_and_same_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str, native_api: Any,
) -> None:
    module = native_api
    parent = info(mode=stat.S_IFDIR | 0o700, uid=1234)
    root = info(mode=stat.S_IFDIR | 0o700, uid=1234)
    if defect.endswith('owner'):
        (root if defect.startswith('root') else parent).st_uid = 10001
    elif defect.endswith('public'):
        (root if defect.startswith('root') else parent).st_mode |= 0o020
    else:
        root.st_dev = 8
    closed: list[int] = []

    def forbidden(*_: object, **__: object) -> None:
        pytest.fail('unsafe stage root was opened')

    monkeypatch.setattr(module, '_open_directory', lambda _: 41)
    monkeypatch.setattr(module, 'os', SimpleNamespace(
        geteuid=lambda: 1234, fstat=lambda _: parent, stat=lambda *a, **kw: root,
        close=closed.append, open=forbidden))
    with pytest.raises(module.ProjectStageCleanupError):
        module._linux_writable(tmp_path / 'stage')
    assert closed == [41]


def test_fd_operations_are_unavailable_outside_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    module = api()

    def forbidden(*_: object) -> None:
        pytest.fail('non-Linux FD operation attempted')

    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(module, 'os', SimpleNamespace(fstat=forbidden))
    with pytest.raises(module.ProjectStageCleanupError):
        module._prepare_fd(41, info(), owner=1234, device=7)


def test_windows_fixture_skips_reparse_directory_and_all_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = api()
    stage = tmp_path / 'stage'
    stage.mkdir()
    junction = stage / 'junction'
    junction.mkdir()
    sentinel = junction / 'outside'
    sentinel.write_bytes(b'outside')
    sentinel.chmod(0o400)
    before = sentinel.stat()
    original = Path.lstat
    visited: list[Path] = []
    iterdir = Path.iterdir

    def lstat(path: Path) -> Any:
        observed = original(path)
        if path == junction:
            return SimpleNamespace(st_mode=observed.st_mode,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return observed

    def members(path: Path) -> Any:
        visited.append(path)
        return iterdir(path)

    monkeypatch.setattr(Path, 'lstat', lstat)
    monkeypatch.setattr(Path, 'iterdir', members)
    try:
        module._windows_fixture_writable(stage)
        assert junction not in visited and sentinel.stat().st_mode == before.st_mode
    finally:
        sentinel.chmod(0o600)


def test_owned_readonly_fixture_becomes_writable_without_deletion(tmp_path: Path) -> None:
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    nested = stage / 'export'
    nested.mkdir()
    output = nested / 'receipt.json'
    output.write_bytes(b'private fixture')
    output.chmod(0o400)
    nested.chmod(0o500)
    api().make_private_stage_writable(stage)
    assert output.read_bytes() == b'private fixture'
    assert output.stat().st_mode & stat.S_IWRITE
    assert nested.stat().st_mode & stat.S_IWRITE


def test_hardlinked_external_file_is_never_chmodded(tmp_path: Path) -> None:
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    external = tmp_path / 'outside'
    external.write_bytes(b'outside')
    external.chmod(0o400)
    try:
        os.link(external, stage / 'alias')
        before = external.stat()
        with pytest.raises(api().ProjectStageCleanupError):
            api().make_private_stage_writable(stage)
        after = external.stat()
        assert (after.st_mode, after.st_uid, after.st_mtime_ns) == (
            before.st_mode, before.st_uid, before.st_mtime_ns)
    finally:
        external.chmod(0o600)


def test_links_are_skipped_without_touching_targets(tmp_path: Path) -> None:
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    external = tmp_path / 'outside'
    external.mkdir()
    target = external / 'sentinel'
    target.write_bytes(b'outside')
    try:
        (stage / 'alias').symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip('local symlink privilege unavailable')
    target.chmod(0o400)
    before = target.stat()
    try:
        api().make_private_stage_writable(stage)
        assert (stage / 'alias').is_symlink()
        after = target.stat()
        assert (after.st_mode, after.st_uid, after.st_mtime_ns) == (
            before.st_mode, before.st_uid, before.st_mtime_ns)
    finally:
        target.chmod(0o600)


@pytest.mark.skipif(sys.platform != 'linux', reason='real Linux FD traversal only')
def test_linux_rejects_fifo_without_open_or_mutation(tmp_path: Path) -> None:
    if sys.platform == 'win32':
        pytest.skip('FIFO fixtures require a POSIX runtime')
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    fifo = stage / 'pipe'
    os.mkfifo(fifo)
    before = fifo.lstat()
    with pytest.raises(api().ProjectStageCleanupError):
        api().make_private_stage_writable(stage)
    assert fifo.lstat().st_mode == before.st_mode
