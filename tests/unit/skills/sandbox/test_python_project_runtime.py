from __future__ import annotations

import hashlib
import importlib
import os
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from agent_hub.capability_installer.python_attestation import ScanLimits


def runtime() -> Any:
    name = 'agent_hub.skills.sandbox.python_project_runtime'
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
        pytest.fail('read-only Python distribution observer is missing')


def observe(root: Path, **kwargs: Any) -> Any:
    options = {
        'interpreter_relative': 'bin/python3.12',
        'limits': ScanLimits(max_bytes=1024 * 1024),
        'deadline': time.monotonic() + 30,
    }
    options.update(kwargs)
    return runtime().observe_python_distribution(root, **options)


@pytest.mark.parametrize('platform', ['win32', 'darwin', 'freebsd14'])
def test_unsupported_platform_refuses_without_opening_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str,
) -> None:
    module = runtime()
    monkeypatch.setattr(module.sys, 'platform', platform)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail('unsupported platform attempted filesystem access')

    monkeypatch.setattr(module.os, 'open', forbidden)
    with pytest.raises(module.PythonDistributionUnsupportedError) as caught:
        observe(tmp_path / 'private-path')
    assert 'private-path' not in str(caught.value)


@pytest.mark.parametrize('deadline', [0, -1, True, float('inf'), float('nan'), '30'])
def test_deadline_must_be_explicit_finite_positive_number(tmp_path: Path, deadline: Any) -> None:
    module = runtime()
    with pytest.raises(module.PythonDistributionError) as caught:
        observe(tmp_path, deadline=deadline)
    assert not isinstance(caught.value, module.PythonDistributionUnsupportedError)


@pytest.mark.parametrize('field', ['max_bytes', 'max_entries', 'max_depth', 'max_path_bytes'])
@pytest.mark.parametrize('value', [0, -1, True, 1.5])
def test_all_scan_limits_require_positive_integers(tmp_path: Path, field: str, value: Any) -> None:
    module = runtime()
    limits = replace(ScanLimits(max_bytes=1024), **{field: value})
    with pytest.raises(module.PythonDistributionError) as caught:
        observe(tmp_path, limits=limits)
    assert not isinstance(caught.value, module.PythonDistributionUnsupportedError)


@pytest.mark.parametrize('relative', ['bin/python', '/bin/python3.12', '../bin/python3.12',
                                     'bin/./python3.12', 'bin/python3.13', True])
def test_interpreter_is_fixed_trusted_configuration(tmp_path: Path, relative: Any) -> None:
    module = runtime()
    with pytest.raises(module.PythonDistributionError) as caught:
        observe(tmp_path, interpreter_relative=relative)
    assert not isinstance(caught.value, module.PythonDistributionUnsupportedError)


def test_semantic_digest_excludes_snapshot_identity_and_includes_every_content_field() -> None:
    module = runtime()
    file = module._Content('file', 0o111, 3, hashlib.sha256(b'abc').hexdigest())
    entries = {
        (): module._Content('directory', 0o111),
        ('bin',): module._Content('directory', 0o111),
        ('bin', 'python3.12'): file,
        ('alias',): module._Content('symlink', 0o111, target=b'bin/python3.12'),
    }
    digest = module._content_digest(entries, time.monotonic() + 30)
    assert digest == module._content_digest(dict(reversed(list(entries.items()))),
                                             time.monotonic() + 30)
    for key, updated in [
        (('bin', 'python3.12'), replace(file, sha256=hashlib.sha256(b'abd').hexdigest())),
        (('bin', 'python3.12'), replace(file, executable=0o101)),
        (('alias',), replace(entries[('alias',)], target=b'./bin/python3.12')),
        (('bin',), module._Content('file', 0o111)),
    ]:
        changed = dict(entries)
        changed[key] = updated
        assert module._content_digest(changed, time.monotonic() + 30) != digest
    renamed = dict(entries)
    renamed[('Bin',)] = renamed.pop(('bin',))
    assert module._content_digest(renamed, time.monotonic() + 30) != digest


def link_tree() -> dict[tuple[str, ...], Any]:
    module = runtime()
    return {
        (): module._Content('directory', 0o111),
        ('share',): module._Content('directory', 0o111),
        ('share', 'terminfo'): module._Content('directory', 0o111),
        ('share', 'terminfo', 'A'): module._Content('directory', 0o111),
        ('share', 'terminfo', 'a'): module._Content('directory', 0o111),
        ('share', 'terminfo', 'A', 'ansi+'): module._Content('file', 0),
        ('share', 'terminfo', 'a', 'ansi'): module._Content(
            'symlink', 0o111, target=b'../A/ansi+'),
        ('alias',): module._Content('symlink', 0o111, target=b'share/terminfo/a'),
    }


def test_internal_link_resolution_preserves_case_plus_and_directory_dotdot_semantics() -> None:
    module = runtime()
    entries = link_tree()
    assert module._resolve(('alias', 'ansi'), entries, time.monotonic() + 30) == (
        'share', 'terminfo', 'A', 'ansi+')
    assert module._resolve(('alias', '..', 'a', 'ansi'), entries,
                           time.monotonic() + 30) == ('share', 'terminfo', 'A', 'ansi+')
    assert module._resolve(('alias', '..', 'a', '..', 'a', 'ansi'), entries,
                           time.monotonic() + 30) == ('share', 'terminfo', 'A', 'ansi+')


@pytest.mark.parametrize('target', [b'/etc/passwd', b'../outside', b'missing', b'loop',
                                    b'loop/child', b'share/terminfo/A/ansi+/..', b''])
def test_link_resolution_rejects_escape_dangling_loops_and_nondirectory_components(
    target: bytes,
) -> None:
    module = runtime()
    entries = link_tree()
    entries[('loop',)] = module._Content('symlink', 0o111, target=target)
    with pytest.raises(module.PythonDistributionError):
        module._resolve(('loop',), entries, time.monotonic() + 30)


def test_resolver_and_digest_respect_absolute_monotonic_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = runtime()
    monkeypatch.setattr(module.time, 'monotonic', lambda: 10.0)
    for operation in (module._resolve, module._content_digest):
        args = (('alias',), link_tree(), 1.0) if operation is module._resolve else (link_tree(), 1.0)
        with pytest.raises(module.PythonDistributionTimeoutError):
            operation(*args)


linux_root = pytest.mark.skipif(
    sys.platform != 'linux' or not hasattr(os, 'geteuid') or os.geteuid() != 0,
    reason='requires native Linux root; ownership and nofollow are never emulated',
)


@pytest.fixture
def distribution() -> Iterator[Path]:
    # /tmp is deliberately forbidden even when sticky. Keep fixtures under /root.
    with tempfile.TemporaryDirectory(prefix='python-observer-', dir='/root') as temporary:
        root = Path(temporary) / 'runtime'
        (root / 'bin').mkdir(parents=True, mode=0o755)
        root.chmod(0o755)
        (root / 'lib').mkdir(mode=0o755)
        (root / 'bin/python3.12').write_bytes(b'not an executable program\0')
        (root / 'bin/python3.12').chmod(0o755)
        (root / 'lib/module.pyc').write_bytes(b'\0compiled bytes\xff')
        (root / 'lib/module.pyc').chmod(0o644)
        yield root


@linux_root
def test_linux_reads_all_bytes_without_execution_and_returns_frozen_snapshot(distribution: Path) -> None:
    module = runtime()
    result = observe(distribution)
    assert result.root == distribution
    assert result.interpreter == distribution / 'bin/python3.12'
    assert result.binary_sha256 == hashlib.sha256(b'not an executable program\0').hexdigest()
    assert len(result.content_sha256) == 64
    assert result.file_count == 2
    assert result.total_bytes == len(b'not an executable program\0\0compiled bytes\xff')
    assert isinstance(result, module.PythonDistributionObservation)
    with pytest.raises(FrozenInstanceError):
        result.total_bytes = 0


@linux_root
def test_linux_digest_is_independent_of_root_inode_and_clocks(distribution: Path) -> None:
    import shutil

    first = observe(distribution)
    replica = distribution.with_name('different-root-uuid')
    shutil.copytree(distribution, replica)
    for member in [replica, *replica.rglob('*')]:
        os.utime(member, ns=(1_000_000_000, 2_000_000_000))
    assert (replica / 'bin/python3.12').stat().st_ino != (distribution / 'bin/python3.12').stat().st_ino
    assert observe(replica).content_sha256 == first.content_sha256
    (replica / 'lib/module.pyc').write_bytes(b'\0compiled bytes\xfe')
    changed = observe(replica)
    assert changed.binary_sha256 == first.binary_sha256
    assert changed.content_sha256 != first.content_sha256


@linux_root
def test_linux_accepts_terminfo_aliases_without_traversing_them(distribution: Path) -> None:
    terminfo = distribution / 'share/terminfo'
    (terminfo / 'A').mkdir(parents=True)
    (terminfo / 'a').mkdir()
    (terminfo / 'A/ansi+').write_bytes(b'terminal')
    for member in (terminfo.parent, terminfo, terminfo / 'A', terminfo / 'a'):
        member.chmod(0o755)
    (terminfo / 'A/ansi+').chmod(0o644)
    (terminfo / 'a/ansi').symlink_to('../A/ansi+')
    (distribution / 'alias').symlink_to('share/terminfo/a', target_is_directory=True)
    result = observe(distribution)
    assert result.file_count == 3
    assert result.total_bytes == (len(b'not an executable program\0\0compiled bytes\xffterminal')
                                  + len(b'../A/ansi+share/terminfo/a'))


@linux_root
@pytest.mark.parametrize('mutation', [
    'root_mode', 'ancestor_mode', 'sticky_ancestor', 'root_owner', 'ancestor_owner',
    'file_mode', 'directory_mode', 'file_owner', 'directory_owner', 'setuid', 'setgid',
    'hardlink', 'fifo', 'device', 'socket', 'root_link', 'ancestor_link', 'link_owner',
    'absolute', 'escape', 'dangling', 'loop', 'cycle', 'not_executable', 'no_interpreter',
])
def test_linux_rejects_unsafe_identity_and_links(distribution: Path, mutation: str) -> None:
    import socket

    assert sys.platform == 'linux'
    unix: Any = os
    unix_socket: Any = socket
    file = distribution / 'lib/module.pyc'
    link = distribution / 'alias'
    selected = distribution
    sock: socket.socket | None = None
    if mutation == 'root_mode':
        distribution.chmod(0o775)
    elif mutation in {'ancestor_mode', 'sticky_ancestor'}:
        distribution.parent.chmod(0o1777 if mutation == 'sticky_ancestor' else 0o775)
    elif mutation in {'root_owner', 'ancestor_owner', 'file_owner', 'directory_owner'}:
        target = {'root_owner': distribution, 'ancestor_owner': distribution.parent,
                  'file_owner': file, 'directory_owner': distribution / 'lib'}[mutation]
        unix.chown(target, 65534, 65534)
    elif mutation in {'file_mode', 'directory_mode', 'setuid', 'setgid'}:
        target = distribution / 'lib' if mutation == 'directory_mode' else file
        target.chmod({'file_mode': 0o666, 'directory_mode': 0o777,
                      'setuid': 0o4644, 'setgid': 0o2644}[mutation])
    elif mutation == 'hardlink':
        link.hardlink_to(file)
    elif mutation == 'fifo':
        unix.mkfifo(link)
    elif mutation == 'device':
        unix.mknod(link, stat.S_IFCHR | 0o600, unix.makedev(1, 3))
    elif mutation == 'socket':
        sock = socket.socket(unix_socket.AF_UNIX)
        sock.bind(str(link))
    elif mutation == 'root_link':
        selected = distribution.with_name('alias')
        selected.symlink_to(distribution, target_is_directory=True)
    elif mutation == 'ancestor_link':
        alias = distribution.parent / 'parent-alias'
        alias.symlink_to(distribution.parent, target_is_directory=True)
        selected = alias / distribution.name
    elif mutation == 'not_executable':
        (distribution / 'bin/python3.12').chmod(0o644)
    elif mutation == 'no_interpreter':
        (distribution / 'bin/python3.12').unlink()
    else:
        link_target = {'link_owner': 'lib/module.pyc', 'absolute': '/etc/passwd',
                       'escape': '../outside', 'dangling': 'missing', 'loop': 'alias',
                       'cycle': 'second'}[mutation]
        link.symlink_to(link_target)
        if mutation == 'cycle':
            (distribution / 'second').symlink_to('alias')
        if mutation == 'link_owner':
            unix.chown(link, 65534, 65534, follow_symlinks=False)
    try:
        with pytest.raises(runtime().PythonDistributionError) as caught:
            observe(selected)
        assert str(distribution) not in str(caught.value)
    finally:
        if sock is not None:
            sock.close()


@linux_root
@pytest.mark.parametrize('limits', [ScanLimits(1), ScanLimits(1024, max_entries=1),
                                   ScanLimits(1024, max_depth=1),
                                   ScanLimits(1024, max_path_bytes=4)])
def test_linux_budgets_fail_closed(distribution: Path, limits: ScanLimits) -> None:
    with pytest.raises(runtime().PythonDistributionLimitError):
        observe(distribution, limits=limits)


@linux_root
@pytest.mark.parametrize('mutation', ['file', 'link', 'directory', 'ancestor'])
def test_linux_final_snapshot_detects_changes_to_earlier_members(
    distribution: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    module = runtime()
    (distribution / 'alias').symlink_to('lib/module.pyc')
    original = module._content_digest

    def change_after_capture(*args: Any, **kwargs: Any) -> str:
        result = original(*args, **kwargs)
        if mutation == 'file':
            (distribution / 'lib/module.pyc').write_bytes(b'changed')
        elif mutation == 'link':
            (distribution / 'alias').unlink()
            (distribution / 'alias').symlink_to('bin/python3.12')
        elif mutation == 'directory':
            (distribution / 'lib/new.pyc').write_bytes(b'new')
        else:
            distribution.parent.chmod(0o777)
        return str(result)

    monkeypatch.setattr(module, '_content_digest', change_after_capture)
    with pytest.raises(module.PythonDistributionError):
        observe(distribution)


@linux_root
def test_linux_fd_cleanup_on_success_limits_and_timeout(distribution: Path) -> None:
    module = runtime()
    before = set(os.listdir('/proc/self/fd'))
    observe(distribution)
    with pytest.raises(module.PythonDistributionLimitError):
        observe(distribution, limits=ScanLimits(1))
    with pytest.raises(module.PythonDistributionTimeoutError):
        observe(distribution, deadline=1.0)
    assert set(os.listdir('/proc/self/fd')) == before


@linux_root
@pytest.mark.parametrize('failure', ['timeout', 'read_error', 'replacement'])
def test_linux_fd_cleanup_and_redaction_for_failures_during_read(
    distribution: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    module = runtime()
    read = module.os.read
    expired = False

    def interrupted(descriptor: int, count: int) -> bytes:
        nonlocal expired
        if failure == 'read_error':
            raise OSError('private-path and private file contents')
        chunk: bytes = read(descriptor, count)
        if failure == 'timeout':
            expired = True
        elif not expired:
            expired = True
            selected = distribution / 'bin/python3.12'
            selected.unlink()
            selected.write_bytes(b'replacement')
            selected.chmod(0o755)
        return chunk

    if failure == 'timeout':
        monkeypatch.setattr(module.time, 'monotonic', lambda: 101.0 if expired else 10.0)
    monkeypatch.setattr(module.os, 'read', interrupted)
    before = set(os.listdir('/proc/self/fd'))
    exception = (module.PythonDistributionTimeoutError if failure == 'timeout'
                 else module.PythonDistributionError)
    with pytest.raises(exception) as caught:
        observe(distribution, deadline=100.0 if failure == 'timeout' else time.monotonic() + 30)
    assert 'private' not in str(caught.value)
    assert str(distribution) not in str(caught.value)
    if failure == 'read_error':
        assert caught.value.__suppress_context__
    assert set(os.listdir('/proc/self/fd')) == before


@linux_root
def test_linux_streams_large_binary_and_accepts_exact_byte_budget(
    distribution: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = runtime()
    raw = b'binary\0' * 400_000
    (distribution / 'bin/python3.12').write_bytes(raw)
    total = len(raw) + len(b'\0compiled bytes\xff')
    read = module.os.read

    def bounded(descriptor: int, count: int) -> bytes:
        assert 0 < count <= 1024**2
        result: bytes = read(descriptor, count)
        return result

    monkeypatch.setattr(module.os, 'read', bounded)
    result = observe(distribution, limits=ScanLimits(total))
    assert result.total_bytes == total
    assert result.binary_sha256 == hashlib.sha256(raw).hexdigest()
    with pytest.raises(module.PythonDistributionLimitError):
        observe(distribution, limits=ScanLimits(total - 1))
