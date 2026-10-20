from __future__ import annotations

import hashlib
import importlib
import os
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_hub.capabilities.python_project_recipe import PythonLockedRequirement
from agent_hub.capability_installer.python_attestation import ScanLimits


def cache() -> Any:
    name = 'agent_hub.skills.sandbox.python_project_wheel_cache'
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
        pytest.fail('read-only wheel cache observer is missing')


def pin(name: str = 'demo', raw: bytes = b'wheel bytes') -> PythonLockedRequirement:
    return PythonLockedRequirement(name, '1.0', (hashlib.sha256(raw).hexdigest(),))


def observe(root: Path, requirements: Any = None, **kwargs: Any) -> Any:
    options = {'limits': ScanLimits(8 * 1024**2), 'deadline': time.monotonic() + 30}
    options.update(kwargs)
    return cache().observe_python_wheel_cache(
        root, (pin(),) if requirements is None else requirements, **options,
    )


def rejected(code: str) -> Any:
    return pytest.raises(cache().PythonWheelCacheError, match=f'^{code}$')


@pytest.mark.parametrize('platform', ['win32', 'darwin', 'freebsd14'])
def test_unsupported_platform_never_opens_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str,
) -> None:
    module = cache()
    monkeypatch.setattr(module.sys, 'platform', platform)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail('unsupported platform accessed the catalog')

    monkeypatch.setattr(module.os, 'open', forbidden)
    with rejected('wheel_cache_unsupported') as caught:
        observe(tmp_path / 'private-host-path')
    assert caught.value.code == 'wheel_cache_unsupported'
    assert caught.value.args == ('wheel_cache_unsupported',)


@pytest.mark.parametrize('deadline', [0, -1, True, float('inf'), float('nan'), '30'])
def test_invalid_deadline_is_not_a_platform_or_lock_failure(tmp_path: Path, deadline: Any) -> None:
    with rejected('wheel_cache_invalid'):
        observe(tmp_path, deadline=deadline)


@pytest.mark.parametrize('field', ['max_bytes', 'max_entries', 'max_depth', 'max_path_bytes'])
@pytest.mark.parametrize('value', [0, -1, True, 1.5])
def test_invalid_scan_limits_are_rejected(tmp_path: Path, field: str, value: Any) -> None:
    with rejected('wheel_cache_invalid'):
        observe(tmp_path, limits=replace(ScanLimits(100), **{field: value}))


@pytest.mark.parametrize('requirements', [[pin()], (object(),), (pin(), pin())])
def test_invalid_or_duplicate_lock_entries_are_distinct(
    tmp_path: Path, requirements: Any,
) -> None:
    with rejected('wheel_cache_lock_invalid'):
        observe(tmp_path, requirements)


@pytest.mark.parametrize('root', [Path('relative'), Path('..'), 'private-host-path'])
def test_root_must_be_a_trusted_absolute_path(root: Any) -> None:
    with rejected('wheel_cache_invalid'):
        observe(root)


linux_root = pytest.mark.skipif(
    sys.platform != 'linux' or not hasattr(os, 'geteuid') or os.geteuid() != 0,
    reason='requires native Linux root; ownership and nofollow are never emulated',
)


@pytest.fixture
def catalog() -> Iterator[Path]:
    # /tmp is writable by others, including when sticky: use real /root ancestors.
    with tempfile.TemporaryDirectory(prefix='wheel-cache-observer-', dir='/root') as temporary:
        root = Path(temporary) / 'catalog'
        root.mkdir(mode=0o755)
        put(root, 'demo-1.0-py3-none-any.whl', b'wheel bytes')
        yield root


def put(root: Path, filename: str, raw: bytes) -> Path:
    result = root / filename
    result.write_bytes(raw)
    result.chmod(0o644)
    return result


@linux_root
def test_linux_returns_actual_hash_in_name_order_without_reading_siblings(
    catalog: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = cache()
    put(catalog, 'alpha_pkg-1.0-py3-none-any.whl', b'alpha')
    for filename in ('sibling-1.0-py3-none-any.whl', 'demo-2.0-py3-none-any.whl', 'README'):
        put(catalog, filename, b'never read this package body')
    original = module._regular

    def requested_only(parent: int, name: str, *args: Any) -> Any:
        assert name in {'demo-1.0-py3-none-any.whl', 'alpha_pkg-1.0-py3-none-any.whl'}
        return original(parent, name, *args)

    monkeypatch.setattr(module, '_regular', requested_only)
    result = observe(catalog, (pin(), pin('alpha-pkg', b'alpha')))
    assert [item.to_payload() for item in result] == [
        {'filename': 'alpha_pkg-1.0-py3-none-any.whl', 'name': 'alpha-pkg',
         'version': '1.0', 'sha256': hashlib.sha256(b'alpha').hexdigest()},
        {'filename': 'demo-1.0-py3-none-any.whl', 'name': 'demo',
         'version': '1.0', 'sha256': hashlib.sha256(b'wheel bytes').hexdigest()},
    ]
    assert str(catalog) not in repr(result)


@linux_root
def test_linux_never_accepts_lock_self_reported_hash(catalog: Path) -> None:
    with rejected('wheel_cache_hash_mismatch'):
        observe(catalog, (pin(raw=b'claimed but absent bytes'),))


@linux_root
def test_linux_selects_actual_digest_from_multiple_lock_hashes(catalog: Path) -> None:
    requirement = replace(pin(), hashes=tuple(sorted((pin().hashes[0], '0' * 64))))
    assert observe(catalog, (requirement,))[0].sha256 == pin().hashes[0]


@linux_root
def test_linux_missing_catalog_does_not_create_it(catalog: Path) -> None:
    absent = catalog / 'private-missing-cache' / 'nested'
    with rejected('wheel_cache_missing') as caught:
        observe(absent)
    assert not absent.parent.exists()
    assert str(catalog) not in str(caught.value)


@linux_root
def test_linux_missing_dependency_is_not_missing_catalog(catalog: Path) -> None:
    with rejected('wheel_cache_wheel_missing'):
        observe(catalog, (pin('absent'),))


@linux_root
@pytest.mark.parametrize('filename', [
    'demo-1.0-1-py3-none-any.whl', 'demo-1.0-py2.py3-none-any.whl',
    'demo-1.0-cp312-none-any.whl', 'demo-1.0-py3-abi3-any.whl',
    'demo-1.0-py3-none-linux_x86_64.whl', 'demo-1.0.tar.gz',
    'demo-1.0-py3-none-any.WHL', 'demo-1.00-py3-none-any.whl',
])
def test_linux_only_build_free_exact_version_universal_wheels_are_candidates(
    catalog: Path, monkeypatch: pytest.MonkeyPatch, filename: str,
) -> None:
    module = cache()
    (catalog / 'demo-1.0-py3-none-any.whl').unlink()
    put(catalog, filename, b'wheel bytes')

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail('read an ineligible wheel body')

    monkeypatch.setattr(module, '_regular', forbidden)
    with rejected('wheel_cache_wheel_missing'):
        observe(catalog)


@linux_root
def test_linux_rejects_multiple_hash_approved_alias_candidates(catalog: Path) -> None:
    put(catalog, 'DEMO-1.0-py3-none-any.whl', b'wheel bytes')
    with rejected('wheel_cache_ambiguous'):
        observe(catalog)


@linux_root
def test_linux_empty_lock_still_checks_catalog_safety(catalog: Path) -> None:
    assert observe(catalog, ()) == ()
    (catalog / 'escape').symlink_to('/etc/passwd')
    with rejected('wheel_cache_unsafe'):
        observe(catalog, ())


@linux_root
@pytest.mark.parametrize('mutation', [
    'root_mode', 'ancestor_mode', 'sticky_ancestor', 'root_owner', 'ancestor_owner',
    'file_mode', 'file_owner', 'setuid', 'setgid', 'hardlink', 'directory', 'fifo',
    'device', 'root_link', 'ancestor_link', 'escape', 'absolute', 'dangling',
])
def test_linux_rejects_unsafe_catalog_and_escape(catalog: Path, mutation: str) -> None:
    assert sys.platform == 'linux'
    unix: Any = os
    selected = catalog
    member = catalog / 'demo-1.0-py3-none-any.whl'
    extra = catalog / 'unrequested'
    if mutation in {'root_mode', 'ancestor_mode', 'sticky_ancestor'}:
        target = catalog if mutation == 'root_mode' else catalog.parent
        target.chmod(0o1777 if mutation == 'sticky_ancestor' else 0o775)
    elif mutation in {'root_owner', 'ancestor_owner', 'file_owner'}:
        target = {'root_owner': catalog, 'ancestor_owner': catalog.parent, 'file_owner': member}[
            mutation]
        unix.chown(target, 65534, 65534)
    elif mutation in {'file_mode', 'setuid', 'setgid'}:
        member.chmod({'file_mode': 0o666, 'setuid': 0o4644, 'setgid': 0o2644}[mutation])
    elif mutation == 'hardlink':
        extra.hardlink_to(member)
    elif mutation == 'directory':
        extra.mkdir()
    elif mutation == 'fifo':
        unix.mkfifo(extra)
    elif mutation == 'device':
        unix.mknod(extra, stat.S_IFCHR | 0o600, unix.makedev(1, 3))
    elif mutation in {'root_link', 'ancestor_link'}:
        alias = catalog.parent / 'alias'
        alias.symlink_to(catalog if mutation == 'root_link' else catalog.parent,
                         target_is_directory=True)
        selected = alias if mutation == 'root_link' else alias / catalog.name
    else:
        extra.symlink_to({'escape': '../outside', 'absolute': '/etc/passwd',
                          'dangling': 'missing'}[mutation])
    with rejected('wheel_cache_unsafe') as caught:
        observe(selected)
    assert str(catalog) not in str(caught.value)


@linux_root
@pytest.mark.parametrize('limits', [ScanLimits(10), ScanLimits(100, max_entries=1),
                                   ScanLimits(100, max_path_bytes=4)])
def test_linux_total_quotas_fail_closed(catalog: Path, limits: ScanLimits) -> None:
    put(catalog, 'alpha-1.0-py3-none-any.whl', b'alpha')
    with rejected('wheel_cache_limit'):
        observe(catalog, (pin(), pin('alpha', b'alpha')), limits=limits)


@linux_root
def test_linux_unrequested_entries_count_but_their_bytes_are_not_read(catalog: Path) -> None:
    put(catalog, 'unrequested', b'x' * 100_000)
    assert len(observe(catalog, limits=ScanLimits(len(b'wheel bytes'), max_entries=2))) == 1
    with rejected('wheel_cache_limit'):
        observe(catalog, limits=ScanLimits(100, max_entries=1))


@linux_root
def test_linux_streams_hash_with_bounded_reads(catalog: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = cache()
    raw = b'wheel\0' * 400_000
    put(catalog, 'demo-1.0-py3-none-any.whl', raw)
    read = module.os.read

    def bounded(descriptor: int, count: int) -> bytes:
        assert 0 < count <= 1024**2
        return bytes(read(descriptor, count))

    monkeypatch.setattr(module.os, 'read', bounded)
    assert observe(catalog, (pin(raw=raw),), limits=ScanLimits(len(raw)))[0].sha256 == (
        hashlib.sha256(raw).hexdigest())
    with rejected('wheel_cache_limit'):
        observe(catalog, (pin(raw=raw),), limits=ScanLimits(len(raw) - 1))


@linux_root
@pytest.mark.parametrize('mutation', ['selected', 'ancestor', 'catalog'])
def test_linux_final_verification_catches_changes_after_earlier_hash(
    catalog: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    module = cache()
    put(catalog, 'zebra-1.0-py3-none-any.whl', b'zebra')
    regular = module._regular

    def change_after_hash(parent: int, name: str, *args: Any) -> Any:
        result = regular(parent, name, *args)
        if name.startswith('zebra-'):
            if mutation == 'selected':
                put(catalog, 'demo-1.0-py3-none-any.whl', b'changed')
            elif mutation == 'ancestor':
                catalog.parent.chmod(0o777)
            else:
                put(catalog, 'new-member', b'new')
        return result

    monkeypatch.setattr(module, '_regular', change_after_hash)
    with rejected('wheel_cache_unsafe'):
        observe(catalog, (pin(), pin('zebra', b'zebra')))


@linux_root
@pytest.mark.parametrize('failure', ['timeout', 'read_error', 'replacement', 'growth'])
def test_linux_read_failures_are_redacted_and_close_descriptors(
    catalog: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    module = cache()
    read = module.os.read
    interrupted = False

    def changed_read(descriptor: int, count: int) -> bytes:
        nonlocal interrupted
        if failure == 'read_error':
            raise OSError('private host path and package contents')
        chunk = bytes(read(descriptor, count))
        if not interrupted:
            interrupted = True
            member = catalog / 'demo-1.0-py3-none-any.whl'
            if failure == 'replacement':
                member.unlink()
                put(catalog, member.name, b'replaced')
            elif failure == 'growth':
                with member.open('ab') as stream:
                    stream.write(b'!')
        return chunk

    if failure == 'timeout':
        monkeypatch.setattr(time, 'monotonic', lambda: 101.0 if interrupted else 10.0)
    monkeypatch.setattr(module.os, 'read', changed_read)
    before = set(os.listdir('/proc/self/fd'))
    code = {'timeout': 'wheel_cache_timeout', 'growth': 'wheel_cache_limit'}.get(
        failure, 'wheel_cache_unsafe')
    with rejected(code) as caught:
        observe(catalog, limits=ScanLimits(len(b'wheel bytes')), deadline=100.0
                if failure == 'timeout' else time.monotonic() + 30)
    assert caught.value.code == code
    assert caught.value.__suppress_context__
    assert set(os.listdir('/proc/self/fd')) == before


@linux_root
def test_linux_success_missing_hash_limit_and_timeout_close_all_descriptors(catalog: Path) -> None:
    before = set(os.listdir('/proc/self/fd'))
    observe(catalog)
    cases: list[tuple[str, dict[str, Any]]] = [
        ('wheel_cache_hash_mismatch', {'requirements': (pin(raw=b'absent'),)}),
        ('wheel_cache_wheel_missing', {'requirements': (pin('absent'),)}),
        ('wheel_cache_limit', {'limits': ScanLimits(1)}),
        ('wheel_cache_timeout', {'deadline': 1.0}),
    ]
    for code, options in cases:
        with rejected(code):
            observe(catalog, **options)
    assert set(os.listdir('/proc/self/fd')) == before
