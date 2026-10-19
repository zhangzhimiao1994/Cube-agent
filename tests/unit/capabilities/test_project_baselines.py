from __future__ import annotations

import asyncio
import errno
import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import threading
from contextlib import suppress
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Self
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.project import ProjectInvocation


def load_baseline() -> ModuleType:
    assert importlib.util.find_spec('agent_hub.capabilities.project_baselines') is not None, (
        'immutable original project baseline storage is missing'
    )
    return importlib.import_module('agent_hub.capabilities.project_baselines')


@pytest.fixture
def baseline_module() -> ModuleType:
    return load_baseline()


def source_tree(root: Path, run: int = 2) -> tuple[ProjectInvocation, Path]:
    invocation = ProjectInvocation(
        execution_id='capture_only', tenant_id=UUID(int=1), run_id=UUID(int=run),
        project_id='project-a', session_id='session-a', sandbox_profile='workspace_write',
        timeout_seconds=30,
    )
    source = root / str(invocation.tenant_id) / 'projects/project-a/sessions/session-a'
    root.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        from agent_hub.plugins.handover_snapshots import _windows_security
        _windows_security(root, private=True, create=True)
    else:
        root.mkdir(mode=0o700, exist_ok=True)
    source.mkdir(parents=True)
    (source / 'src').mkdir()
    (source / 'tests').mkdir()
    (source / 'src/index.js').write_bytes(b'module.exports = () => 0;\n')
    (source / 'tests/original.js').write_bytes(b'// immutable original user check\n')
    (source / 'package.json').write_bytes(b'{"scripts":{"test":"node tests/original.js"}}')
    return invocation, source


def selection(module: ModuleType) -> Any:
    return module.ManagedProjectInput.from_payload({
        'schema_version': 1, 'kind': 'managed_workspace', 'editable_paths': ['src'],
    })


async def test_capture_preserves_original_bytes_and_restart_reference(
    tmp_path: Path,
) -> None:
    baseline_module = load_baseline()
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    try:
        reference = await store.capture_once(invocation, selection(baseline_module))
    except baseline_module.ProjectBaselineError as error:
        raise AssertionError(f'local fixture capture cause: {error.__context__!r}') from error
    assert reference.input_source_sha256
    assert reference.contract_sha256
    assert reference.file_count == 3
    assert 'snapshot' not in reference.to_payload()
    assert str(tmp_path) not in str(reference.to_payload())

    (source / 'src/index.js').write_bytes(b'module.exports = () => 12;\n')
    restored = baseline_module.ProjectBaselineStore(root).load(invocation, reference)
    assert (restored.snapshot / 'src/index.js').read_bytes() == b'module.exports = () => 0;\n'
    assert (restored.snapshot / 'tests/original.js').read_bytes() == b'// immutable original user check\n'
    assert 'tests/original.js' in restored.contract.protected_files
    assert 'src/index.js' not in restored.contract.protected_files


async def test_existing_capture_never_recaptures_current_workspace(
    baseline_module: ModuleType, tmp_path: Path,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, selection(baseline_module))
    (source / 'tests/original.js').write_bytes(b'weakened assertion')
    with pytest.raises(baseline_module.ProjectBaselineError, match='already reserved'):
        await store.capture_once(invocation, selection(baseline_module))
    restored = store.load(invocation, reference)
    assert (restored.snapshot / 'tests/original.js').read_bytes() == b'// immutable original user check\n'


@pytest.mark.parametrize('nested', [False, True])
async def test_boolean_versions_cannot_replace_capture_intent(
    baseline_module: ModuleType, tmp_path: Path, nested: bool,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, selection(baseline_module))
    path = root / '.project-contracts' / str(invocation.tenant_id) / str(invocation.run_id) / 'capture-intent.json'
    body = json.loads(path.read_bytes())
    target = body['project_input'] if nested else body
    target['schema_version'] = True
    path.write_bytes(json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii'))
    with pytest.raises(baseline_module.ProjectBaselineError):
        store.load(invocation, reference)


async def test_reference_cannot_cross_tenant_run_or_selected_workspace(
    baseline_module: ModuleType, tmp_path: Path,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, selection(baseline_module))
    for fields in ({'tenant_id': UUID(int=9)}, {'run_id': UUID(int=10)},
                   {'project_id': 'other'}, {'session_id': 'other'}):
        with pytest.raises(baseline_module.ProjectBaselineError):
            store.load(invocation.model_copy(update=fields), reference)


async def test_capture_rejects_links_without_following_or_success(
    baseline_module: ModuleType, tmp_path: Path,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    (source / 'outside.txt').hardlink_to(source / 'tests/original.js')
    store = baseline_module.ProjectBaselineStore(root)
    with pytest.raises(baseline_module.ProjectBaselineError):
        await store.capture_once(invocation, selection(baseline_module))
    with pytest.raises(baseline_module.ProjectBaselineError, match='already reserved'):
        await store.capture_once(invocation, selection(baseline_module))


async def test_corrupt_original_or_reference_never_becomes_valid_again(
    baseline_module: ModuleType, tmp_path: Path,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, selection(baseline_module))
    original = store.load(invocation, reference).snapshot / 'tests/original.js'
    original.chmod(0o600)
    original.write_bytes(b'mutated original')
    with pytest.raises(baseline_module.ProjectBaselineError):
        store.load(invocation, reference)


async def test_control_namespace_alias_rejected_before_source_read(
    baseline_module: ModuleType, tmp_path: Path,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    control = root / '.project-contracts'
    control.write_bytes(b'not a directory')
    with pytest.raises(baseline_module.ProjectBaselineError):
        await baseline_module.ProjectBaselineStore(root).capture_once(
            invocation, selection(baseline_module),
        )


def control_root(root: Path, invocation: ProjectInvocation) -> Path:
    return root / '.project-contracts' / str(invocation.tenant_id) / str(invocation.run_id)


async def test_load_rejects_late_file_mutation_even_when_parent_directory_is_unchanged(
    baseline_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, selection(baseline_module))
    read = baseline_module.read_control
    first: list[Path] = []
    changed = False

    def racing_read(path: Path, *, max_bytes: int) -> Any:
        nonlocal changed
        if not first:
            first.append(path)
        elif not changed:
            target = first[0]
            directory_before = target.parent.stat()
            before = target.stat()
            target.chmod(0o600)
            target.write_bytes(b'X' * before.st_size)
            target.chmod(0o444)
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert target.parent.stat().st_mtime_ns == directory_before.st_mtime_ns
            changed = True
        return read(path, max_bytes=max_bytes)

    monkeypatch.setattr(baseline_module, 'read_control', racing_read)
    with pytest.raises(baseline_module.ProjectBaselineError):
        store.load(invocation, reference)
    assert changed


@pytest.mark.parametrize('cancel', [False, True])
async def test_failed_or_cancelled_capture_cleans_only_its_partial_and_never_recaptures(
    baseline_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    copy = baseline_module.snapshot_project_source
    cleanup = store._remove_partial
    cleanup_errors: list[str] = []

    def traced_cleanup(*args: Any) -> None:
        try:
            cleanup(*args)
        except Exception as error:
            cleanup_errors.append(repr(error))
            raise

    async def interrupted(*args: Any, **kwargs: Any) -> str:
        await copy(*args, **kwargs)
        if cancel:
            raise asyncio.CancelledError
        raise OSError('PRIVATE project bytes and host details must not escape')

    monkeypatch.setattr(baseline_module, 'snapshot_project_source', interrupted)
    monkeypatch.setattr(store, '_remove_partial', traced_cleanup)
    expected = asyncio.CancelledError if cancel else baseline_module.ProjectBaselineError
    with pytest.raises(expected) as caught:
        await store.capture_once(invocation, selection(baseline_module))
    assert 'PRIVATE' not in str(caught.value)
    controls = control_root(root, invocation)
    assert (controls / 'capture-intent.json').is_file()
    assert not (controls / 'snapshot').exists(), cleanup_errors
    failure = json.loads((controls / 'capture-failure.json').read_bytes())
    assert failure['status'] == ('cancelled' if cancel else 'failed')
    assert failure['cleanup_confirmed'] is True
    assert 'PRIVATE' not in json.dumps(failure)
    with pytest.raises(baseline_module.ProjectBaselineError, match='already reserved'):
        await store.capture_once(invocation, selection(baseline_module))


async def test_historical_load_does_not_require_current_workspace_tree(
    baseline_module: ModuleType, tmp_path: Path,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, selection(baseline_module))
    shutil.rmtree(source)
    stored = store.load(invocation, reference)
    assert stored.reference == reference
    assert (stored.snapshot / 'tests/original.js').read_bytes() == b'// immutable original user check\n'


async def test_uncertain_publication_preserves_complete_baseline_and_failure_metadata(
    baseline_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    write = store._write_once

    def uncertain(parent: Any, name: str, data: bytes) -> None:
        write(parent, name, data)
        if name == 'baseline.json':
            raise OSError('PRIVATE publication acknowledgement lost')

    monkeypatch.setattr(store, '_write_once', uncertain)
    with pytest.raises(baseline_module.ProjectBaselineError):
        await store.capture_once(invocation, selection(baseline_module))
    controls = control_root(root, invocation)
    raw = (controls / 'baseline.json').read_bytes()
    body = json.loads(raw)
    reference = baseline_module.ProjectBaselineReference(
        hashlib.sha256(raw).hexdigest(), body['contract']['input_source_sha256'], 3,
    )
    assert store.load(invocation, reference).snapshot.is_dir()
    failure = json.loads((controls / 'capture-failure.json').read_bytes())
    assert failure['status'] == 'unknown'
    assert failure['snapshot_retained'] is True
    with pytest.raises(baseline_module.ProjectBaselineError, match='already reserved'):
        await store.capture_once(invocation, selection(baseline_module))


async def test_two_concurrent_captures_have_one_intent_and_one_original(
    baseline_module: ModuleType, tmp_path: Path,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    results = await asyncio.gather(
        store.capture_once(invocation, selection(baseline_module)),
        store.capture_once(invocation, selection(baseline_module)), return_exceptions=True,
    )
    references = [value for value in results if isinstance(value, baseline_module.ProjectBaselineReference)]
    errors = [value for value in results if isinstance(value, baseline_module.ProjectBaselineError)]
    assert len(references) == len(errors) == 1
    assert 'already reserved' in str(errors[0])
    assert store.load(invocation, references[0]).reference == references[0]
    assert not (control_root(root, invocation) / 'capture-failure.json').exists()


@pytest.mark.parametrize('stage', ['quota', 'copy'])
async def test_disk_exhaustion_is_consumed_but_does_not_retain_owned_partial(
    baseline_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    if stage == 'quota':
        monkeypatch.setattr(baseline_module.shutil, 'disk_usage', lambda path: SimpleNamespace(free=0))
    else:
        async def exhausted(*args: Any, destination: Path, **kwargs: Any) -> str:
            destination.mkdir(mode=0o700)
            (destination / 'partial.txt').write_bytes(b'actual partial input')
            raise OSError(errno.ENOSPC, 'PRIVATE source path')
        monkeypatch.setattr(baseline_module, 'snapshot_project_source', exhausted)
    with pytest.raises(baseline_module.ProjectBaselineError) as caught:
        await store.capture_once(invocation, selection(baseline_module))
    assert 'PRIVATE' not in str(caught.value)
    controls = control_root(root, invocation)
    assert not (controls / 'snapshot').exists()
    assert json.loads((controls / 'capture-failure.json').read_bytes())['status'] == 'failed'
    with pytest.raises(baseline_module.ProjectBaselineError, match='already reserved'):
        await store.capture_once(invocation, selection(baseline_module))


@pytest.mark.parametrize('limit', [{'source_limit_bytes': 128}, {'file_limit': 2}])
async def test_real_large_file_or_file_count_limit_never_leaves_partial_snapshot(
    baseline_module: ModuleType, tmp_path: Path, limit: dict[str, int],
) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    (source / 'src/large.bin').write_bytes(b'X' * 1024)
    invocation = invocation.model_copy(update=limit)
    with pytest.raises(baseline_module.ProjectBaselineError):
        await baseline_module.ProjectBaselineStore(root).capture_once(invocation, selection(baseline_module))
    controls = control_root(root, invocation)
    assert (controls / 'capture-intent.json').is_file()
    assert not (controls / 'snapshot').exists()


async def test_source_drift_after_copy_is_not_published_or_recaptured(
    baseline_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    copy = baseline_module.snapshot_project_source

    async def drifting(*args: Any, **kwargs: Any) -> str:
        digest: str = await copy(*args, **kwargs)
        (source / 'tests/original.js').write_bytes(b'changed during capture')
        return digest

    monkeypatch.setattr(baseline_module, 'snapshot_project_source', drifting)
    with pytest.raises(baseline_module.ProjectBaselineError, match='input changed'):
        await store.capture_once(invocation, selection(baseline_module))
    controls = control_root(root, invocation)
    assert not (controls / 'baseline.json').exists()
    assert not (controls / 'snapshot').exists()
    with pytest.raises(baseline_module.ProjectBaselineError, match='already reserved'):
        await store.capture_once(invocation, selection(baseline_module))


async def test_cancellation_drains_native_reader_before_removing_owned_snapshot(
    baseline_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    started, release = threading.Event(), threading.Event()
    scan = baseline_module._snapshot_files

    def held_reader(*args: Any) -> Any:
        started.set()
        assert release.wait(10), 'local reader fixture timed out'
        return scan(*args)

    monkeypatch.setattr(baseline_module, '_snapshot_files', held_reader)
    task = asyncio.create_task(store.capture_once(invocation, selection(baseline_module)))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert (control_root(root, invocation) / 'snapshot').is_dir()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (control_root(root, invocation) / 'snapshot').exists()
    finally:
        release.set()
        with suppress(asyncio.CancelledError, baseline_module.ProjectBaselineError):
            await task


async def test_cleanup_does_not_delete_a_replacement_or_relocated_owned_tree(
    baseline_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    controls = control_root(root, invocation)
    store = baseline_module.ProjectBaselineStore(root)
    copy = baseline_module.snapshot_project_source

    async def replaced(*args: Any, destination: Path, **kwargs: Any) -> str:
        await copy(*args, destination=destination, **kwargs)
        destination.parent.rename(controls / 'relocated')
        (controls / 'snapshot').mkdir()
        (controls / 'snapshot/sentinel.txt').write_bytes(b'not owned by cleanup')
        raise OSError('copy acknowledgement lost')

    monkeypatch.setattr(baseline_module, 'snapshot_project_source', replaced)
    with pytest.raises(baseline_module.ProjectBaselineError):
        await store.capture_once(invocation, selection(baseline_module))
    assert (controls / 'snapshot/sentinel.txt').read_bytes() == b'not owned by cleanup'
    assert (controls / 'relocated/source/tests/original.js').is_file()
    assert json.loads((controls / 'capture-failure.json').read_bytes())['status'] == 'unknown'


@pytest.mark.parametrize('mutation', ['duplicate', 'missing_manifest', 'omitted_protection', 'widened_scope'])
async def test_resigned_control_json_still_checks_original_intent_and_complete_snapshot(
    baseline_module: ModuleType, tmp_path: Path, mutation: str,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, selection(baseline_module))
    target = control_root(root, invocation) / 'baseline.json'
    body = json.loads(target.read_bytes())
    if mutation == 'missing_manifest':
        del body['source_files']['tests/original.js']
    elif mutation == 'omitted_protection':
        del body['contract']['protected_files']['tests/original.js']
        binding = {key: value for key, value in body['contract'].items() if key != 'protected_sha256'}
        body['contract']['protected_sha256'] = hashlib.sha256(baseline_module._encoded(binding)).hexdigest()
    elif mutation == 'widened_scope':
        body['contract'] = baseline_module.build_project_check_contract(
            invocation.tenant_id, invocation.run_id, invocation.project_id, invocation.session_id,
            reference.input_source_sha256, ('src', 'tests'), body['source_files'],
        ).to_payload()
    raw = baseline_module._encoded(body)
    if mutation == 'duplicate':
        raw = raw.replace(b'{"contract":', b'{"schema_version":1,"contract":', 1)
    target.write_bytes(raw)
    resigned = baseline_module.ProjectBaselineReference(
        hashlib.sha256(raw).hexdigest(), reference.input_source_sha256, reference.file_count,
    )
    with pytest.raises(baseline_module.ProjectBaselineError):
        store.load(invocation, resigned)


@pytest.mark.parametrize('target_name', ['baseline', 'snapshot'])
async def test_control_or_snapshot_hardlink_alias_never_loads(
    baseline_module: ModuleType, tmp_path: Path, target_name: str,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, selection(baseline_module))
    target = control_root(root, invocation) / 'baseline.json'
    if target_name == 'snapshot':
        target = store.load(invocation, reference).snapshot / 'tests/original.js'
    alias = tmp_path / 'file-alias'
    mode = target.stat().st_mode & 0o777
    alias.hardlink_to(target)
    try:
        with pytest.raises(baseline_module.ProjectBaselineError):
            store.load(invocation, reference)
    finally:
        if os.name == 'nt':
            alias.chmod(0o600)
        alias.unlink()
        target.chmod(mode)


async def test_load_rejects_root_alias_even_when_original_workspace_is_gone(
    baseline_module: ModuleType, tmp_path: Path,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, selection(baseline_module))
    shutil.rmtree(source)
    with pytest.raises(baseline_module.ProjectBaselineError):
        baseline_module.ProjectBaselineStore(root / '..' / root.name).load(invocation, reference)


async def test_real_directory_alias_cannot_relocate_the_baseline_store(
    baseline_module: ModuleType, tmp_path: Path,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, selection(baseline_module))
    alias = tmp_path / 'workspace-alias'
    if os.name == 'nt':
        await asyncio.to_thread(subprocess.run, ['cmd.exe', '/c', 'mklink', '/J', str(alias), str(root)],
                                check=True, capture_output=True)
    else:
        alias.symlink_to(root, target_is_directory=True)
    try:
        with pytest.raises(baseline_module.ProjectBaselineError):
            baseline_module.ProjectBaselineStore(alias).load(invocation, reference)
        assert (source / 'tests/original.js').read_bytes() == b'// immutable original user check\n'
    finally:
        if os.name == 'nt':
            alias.rmdir()
        else:
            alias.unlink()


async def test_real_control_permission_change_is_not_mocked_or_ignored(
    baseline_module: ModuleType, tmp_path: Path,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, selection(baseline_module))
    target = control_root(root, invocation) / 'baseline.json'
    if os.name == 'nt':
        await asyncio.to_thread(subprocess.run, ['icacls', str(target), '/grant', '*S-1-1-0:(R)'],
                                check=True, capture_output=True)
    else:
        target.chmod(0o644)
    try:
        with pytest.raises(baseline_module.ProjectBaselineError):
            store.load(invocation, reference)
    finally:
        if os.name == 'nt':
            await asyncio.to_thread(subprocess.run, ['icacls', str(target), '/remove:g', '*S-1-1-0'],
                                    check=True, capture_output=True)
        else:
            target.chmod(0o600)
    assert store.load(invocation, reference).reference == reference


async def test_half_written_manifest_is_not_published_and_does_not_pin_large_snapshot(
    baseline_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline_module.ProjectBaselineStore(root)
    open_file, fdopen = baseline_module._Directory.open, baseline_module.os.fdopen
    interrupted: set[int] = set()

    def select_manifest(parent: Any, name: str, flags: int) -> int:
        descriptor: int = open_file(parent, name, flags)
        if name in {'baseline.json', 'baseline-partial.json'} and flags & os.O_WRONLY:
            interrupted.add(descriptor)
        return descriptor

    class FullDiskWriter:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            self.stream.close()

        def write(self, raw: bytes) -> None:
            self.stream.write(raw[:len(raw) // 2])
            self.stream.flush()
            raise OSError(errno.ENOSPC, 'local fixture full disk')

    def full_disk(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        stream = fdopen(descriptor, *args, **kwargs)
        if descriptor in interrupted:
            interrupted.remove(descriptor)
            return FullDiskWriter(stream)
        return stream

    monkeypatch.setattr(baseline_module._Directory, 'open', select_manifest)
    monkeypatch.setattr(baseline_module.os, 'fdopen', full_disk)
    with pytest.raises(baseline_module.ProjectBaselineError):
        await store.capture_once(invocation, selection(baseline_module))
    controls = control_root(root, invocation)
    assert not (controls / 'snapshot').exists()
    assert not (controls / 'baseline.json').exists()
    assert (controls / 'capture-intent.json').is_file()
    assert json.loads((controls / 'capture-failure.json').read_bytes())['status'] == 'failed'
