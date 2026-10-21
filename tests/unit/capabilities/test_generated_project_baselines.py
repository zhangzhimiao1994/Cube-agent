"""Generated capture is post-model preservation, never original-input authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.capabilities import project_baselines as baseline
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.project import ProjectInvocation, snapshot_project_source
from tests.unit.capabilities.test_project_baselines import source_tree


def scaffold(root: Path) -> tuple[ProjectInvocation, Path, dict[str, JsonValue]]:
    invocation, source = source_tree(root)
    raw = baseline._encoded({
        'schema_version': 1, 'stack': 'node', 'editable_paths': ['src'],
        'test_files': ['tests/original.js'], 'criteria': [
            {'id': 'AC-001', 'description': 'The generated implementation meets the frozen check.',
             'tests': ['generated.check']}],
    })
    (source / 'ACCEPTANCE.json').write_bytes(raw)
    metadata: dict[str, JsonValue] = {
        'schema_version': 1, 'purpose': 'generated_acceptance', 'capture_phase': 'post_model',
        'user_id': 'abcdef01-0000-4000-8000-000000000003', 'request_sha256': 'a' * 64,
        'manifest_sha256': hashlib.sha256(raw).hexdigest(),
    }
    return invocation, source, metadata


def controls(root: Path, invocation: ProjectInvocation) -> Path:
    return root / '.project-contracts' / str(invocation.tenant_id) / str(invocation.run_id)


async def test_generated_roundtrip_is_explicit_v2_and_metadata_is_frozen(tmp_path: Path) -> None:
    root = tmp_path / 'workspaces'
    invocation, source, metadata = scaffold(root)
    store = baseline.ProjectBaselineStore(root)
    reference = await store.capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
    body = json.loads((controls(root, invocation) / 'baseline.json').read_bytes())
    assert set(body) == {'schema_version', 'purpose', 'generated_metadata', 'contract', 'source_files'}
    assert body['schema_version'] == 2 and body['purpose'] == 'generated_acceptance'
    assert body['generated_metadata'] == metadata
    intent = json.loads((controls(root, invocation) / 'capture-intent.json').read_bytes())
    assert intent['schema_version'] == 2 and intent['purpose'] == 'generated_acceptance'
    assert intent['generated_metadata'] == metadata and intent['editable_paths'] == ['src']
    assert 'project_input' not in intent
    stored = baseline.ProjectBaselineStore(root).load(invocation, reference)
    assert stored.generated_metadata == metadata
    assert {'ACCEPTANCE.json', 'tests/original.js', 'package.json'} <= stored.contract.protected_files.keys()
    assert reference.to_payload()['schema_version'] == 1  # Opaque locator, not origin authority.
    assert 'generated_metadata' not in reference.to_payload()
    assert stored.generated_metadata is not None
    with pytest.raises(TypeError):
        cast(dict[str, JsonValue], stored.generated_metadata)['user_id'] = 'other'
    metadata['user_id'] = str(UUID(int=99))
    assert stored.generated_metadata['user_id'] == 'abcdef01-0000-4000-8000-000000000003'
    (source / 'src/index.js').write_bytes(b'changed after frozen generated capture\n')
    assert store.load(invocation, reference).source_files == stored.source_files


async def test_original_v1_body_and_intent_byte_shapes_are_unchanged(tmp_path: Path) -> None:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = baseline.ProjectBaselineStore(root)
    selection = ManagedProjectInput(('src',))
    reference = await store.capture_once(invocation, selection)
    stored = store.load(invocation, reference)
    expected = baseline._encoded({'schema_version': 1, 'contract': stored.contract.to_payload(),
                                  'source_files': dict(stored.source_files)})
    assert (controls(root, invocation) / 'baseline.json').read_bytes() == expected
    assert (controls(root, invocation) / 'capture-intent.json').read_bytes() == baseline._encoded({
        'schema_version': 1, 'state': 'reserved', 'tenant_id': str(invocation.tenant_id),
        'run_id': str(invocation.run_id), 'project_id': invocation.project_id,
        'session_id': invocation.session_id, 'project_input': selection.to_payload(),
    })
    assert stored.generated_metadata is None


@pytest.mark.parametrize('field,value', [
    ('schema_version', True), ('schema_version', 2), ('purpose', 'managed_workspace'),
    ('capture_phase', 'pre_model'), ('user_id', 'ABCDEF01-0000-4000-8000-000000000003'),
    ('user_id', 'abcdef01000040008000000000000003'), ('user_id', UUID(int=3)),
    ('request_sha256', 'A' * 64), ('manifest_sha256', 'short'), ('request_sha256', None),
    ('install_allowed', True), ('authority', {'role': 'operator'}),
])
async def test_invalid_generated_metadata_is_rejected_before_reservation(
    tmp_path: Path, field: str, value: object,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _, metadata = scaffold(root)
    changed = cast(Mapping[str, JsonValue], metadata | {field: value})
    with pytest.raises(baseline.ProjectBaselineError):
        await baseline.ProjectBaselineStore(root).capture_generated_once(
            invocation, editable_paths=('src',), metadata=changed)
    assert not controls(root, invocation).exists()


@pytest.mark.parametrize('defect', ['missing_manifest', 'hash', 'invalid_json', 'missing_test',
                                   'different_editables', 'editable_tests', 'missing_metadata', 'list_editables'])
async def test_generated_manifest_and_frozen_paths_are_measured_not_claimed(tmp_path: Path, defect: str) -> None:
    root = tmp_path / 'workspaces'
    invocation, source, metadata = scaffold(root)
    paths: tuple[str, ...] = ('src',)
    target = source / 'ACCEPTANCE.json'
    if defect == 'missing_manifest':
        target.unlink()
    elif defect == 'hash':
        metadata['manifest_sha256'] = 'b' * 64
    elif defect == 'invalid_json':
        target.write_bytes(b'{"schema_version": true}')
        metadata['manifest_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
    elif defect == 'missing_test':
        (source / 'tests/original.js').unlink()
    elif defect == 'different_editables':
        paths = ('src/index.js',)
    elif defect == 'editable_tests':
        value = json.loads(target.read_bytes())
        value['editable_paths'] = ['src', 'tests']
        target.write_bytes(baseline._encoded(value))
        metadata['manifest_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
        paths = ('src', 'tests')
    elif defect == 'missing_metadata':
        del metadata['request_sha256']
    else:
        paths = cast(tuple[str, ...], ['src'])
    store = baseline.ProjectBaselineStore(root)
    with pytest.raises(baseline.ProjectBaselineError):
        await store.capture_generated_once(invocation, editable_paths=paths, metadata=metadata)
    assert not (controls(root, invocation) / 'baseline.json').exists()
    if (controls(root, invocation) / 'capture-intent.json').exists():
        with pytest.raises(baseline.ProjectBaselineError, match='already reserved'):
            await store.capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)


@pytest.mark.parametrize('first_generated', [False, True])
async def test_original_and_generated_cannot_recapture_each_others_scope(tmp_path: Path, first_generated: bool) -> None:
    root = tmp_path / 'workspaces'
    invocation, _, metadata = scaffold(root)
    store = baseline.ProjectBaselineStore(root)
    if first_generated:
        reference = await store.capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
        with pytest.raises(baseline.ProjectBaselineError, match='already reserved'):
            await store.capture_once(invocation, ManagedProjectInput(('src',)))
    else:
        reference = await store.capture_once(invocation, ManagedProjectInput(('src',)))
        with pytest.raises(baseline.ProjectBaselineError, match='already reserved'):
            await store.capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
    assert (store.load(invocation, reference).generated_metadata is not None) is first_generated


@pytest.mark.parametrize('field,value', [('tenant_id', UUID(int=9)), ('run_id', UUID(int=9)),
                                       ('project_id', 'other'), ('session_id', 'other')])
async def test_generated_reference_is_scope_bound(tmp_path: Path, field: str, value: object) -> None:
    root = tmp_path / 'workspaces'
    invocation, _, metadata = scaffold(root)
    store = baseline.ProjectBaselineStore(root)
    reference = await store.capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
    with pytest.raises(baseline.ProjectBaselineError):
        store.load(invocation.model_copy(update={field: value}), reference)


@pytest.mark.parametrize('mutation', ['downgrade', 'purpose', 'metadata', 'extra', 'bool_version', 'missing_file'])
async def test_resigned_generated_body_must_still_match_origin_intent_and_snapshot(
    tmp_path: Path, mutation: str,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _, metadata = scaffold(root)
    store = baseline.ProjectBaselineStore(root)
    reference = await store.capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
    target = controls(root, invocation) / 'baseline.json'
    body = json.loads(target.read_bytes())
    if mutation == 'downgrade':
        body.pop('purpose')
        body.pop('generated_metadata')
        body['schema_version'] = 1
    elif mutation == 'purpose':
        body['purpose'] = 'original'
    elif mutation == 'metadata':
        body['generated_metadata']['user_id'] = str(UUID(int=99))
    elif mutation == 'extra':
        body['original'] = True
    elif mutation == 'bool_version':
        body['schema_version'] = True
    else:
        del body['source_files']['ACCEPTANCE.json']
    raw = baseline._encoded(body)
    target.write_bytes(raw)
    resigned = baseline.ProjectBaselineReference(hashlib.sha256(raw).hexdigest(),
        reference.input_source_sha256, reference.file_count)
    with pytest.raises(baseline.ProjectBaselineError):
        store.load(invocation, resigned)


@pytest.mark.parametrize('cancel', [False, True])
async def test_failed_generated_capture_consumes_intent_and_drains_existing_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _, metadata = scaffold(root)
    store = baseline.ProjectBaselineStore(root)
    copy = snapshot_project_source

    async def interrupted(*args: Any, **kwargs: Any) -> str:
        await copy(*args, **kwargs)
        if cancel:
            raise asyncio.CancelledError
        raise OSError('PRIVATE bytes and host path')

    monkeypatch.setattr(baseline, 'snapshot_project_source', interrupted)
    expected = asyncio.CancelledError if cancel else baseline.ProjectBaselineError
    with pytest.raises(expected) as caught:
        await store.capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
    assert 'PRIVATE' not in str(caught.value)
    assert not (controls(root, invocation) / 'snapshot').exists()
    intent = json.loads((controls(root, invocation) / 'capture-intent.json').read_bytes())
    assert intent['generated_metadata'] == metadata
    failure = json.loads((controls(root, invocation) / 'capture-failure.json').read_bytes())
    assert failure['cleanup_confirmed'] is True
    with pytest.raises(baseline.ProjectBaselineError, match='already reserved'):
        await store.capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)


async def test_empty_generated_capture_is_rejected_and_cannot_be_recaptured(tmp_path: Path) -> None:
    root = tmp_path / 'workspaces'
    invocation, source, metadata = scaffold(root)
    for path in source.rglob('*'):
        if path.is_file():
            path.unlink()
    store = baseline.ProjectBaselineStore(root)
    with pytest.raises(baseline.ProjectBaselineError):
        await store.capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
    with pytest.raises(baseline.ProjectBaselineError, match='already reserved'):
        await store.capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)


async def test_generated_metadata_is_frozen_before_copy_can_yield(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, _, metadata = scaffold(root)
    expected = dict(metadata)

    async def mutate_metadata(*args: Any, **kwargs: Any) -> str:
        metadata['user_id'] = str(UUID(int=99))
        metadata['request_sha256'] = 'f' * 64
        return await snapshot_project_source(*args, **kwargs)

    monkeypatch.setattr(baseline, 'snapshot_project_source', mutate_metadata)
    store = baseline.ProjectBaselineStore(root)
    reference = await store.capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
    assert store.load(invocation, reference).generated_metadata == expected


@pytest.mark.parametrize('target_name', ['ACCEPTANCE.json', 'tests/original.js'])
async def test_generated_load_remeasures_actual_protected_file_bytes(tmp_path: Path, target_name: str) -> None:
    root = tmp_path / 'workspaces'
    invocation, _, metadata = scaffold(root)
    store = baseline.ProjectBaselineStore(root)
    reference = await store.capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
    stored = store.load(invocation, reference)
    path = stored.snapshot / target_name
    path.chmod(0o600)
    path.write_bytes(b'changed frozen generated test or manifest')
    path.chmod(0o444)
    with pytest.raises(baseline.ProjectBaselineError):
        store.load(invocation, reference)


async def test_original_cannot_be_upgraded_by_resigning_generated_body_only(tmp_path: Path) -> None:
    root = tmp_path / 'workspaces'
    invocation, _, metadata = scaffold(root)
    store = baseline.ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, ManagedProjectInput(('src',)))
    target = controls(root, invocation) / 'baseline.json'
    value = json.loads(target.read_bytes())
    value.update(schema_version=2, purpose='generated_acceptance', generated_metadata=metadata)
    raw = baseline._encoded(value)
    target.write_bytes(raw)
    changed = baseline.ProjectBaselineReference(hashlib.sha256(raw).hexdigest(),
        reference.input_source_sha256, reference.file_count)
    with pytest.raises(baseline.ProjectBaselineError):
        store.load(invocation, changed)


@pytest.mark.parametrize('defect', ['noncanonical', 'duplicate', 'intent_origin', 'missing_snapshot', 'wrong_reference'])
async def test_generated_load_requires_complete_canonical_origin_and_reference(tmp_path: Path, defect: str) -> None:
    root = tmp_path / 'workspaces'
    invocation, _, metadata = scaffold(root)
    store = baseline.ProjectBaselineStore(root)
    reference = await store.capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
    container = controls(root, invocation)
    raw = (container / 'baseline.json').read_bytes()
    value = json.loads(raw)
    if defect in {'noncanonical', 'duplicate'}:
        altered = (json.dumps(value, indent=2).encode() if defect == 'noncanonical'
                   else raw[:-1] + b',"schema_version":2}')
        (container / 'baseline.json').write_bytes(altered)
        reference = baseline.ProjectBaselineReference(hashlib.sha256(altered).hexdigest(),
            reference.input_source_sha256, reference.file_count)
    elif defect == 'intent_origin':
        target = container / 'capture-intent.json'
        intent = json.loads(target.read_bytes())
        intent['generated_metadata']['capture_phase'] = 'pre_model'
        target.write_bytes(baseline._encoded(intent))
    elif defect == 'missing_snapshot':
        path = container / 'snapshot/source/ACCEPTANCE.json'
        mode = path.parent.stat().st_mode & 0o777
        path.parent.chmod(0o700)
        try:
            path.chmod(0o600)
            path.unlink()
        finally:
            path.parent.chmod(mode)
    else:
        old_body = {key: value[key] for key in ('contract', 'source_files')} | {'schema_version': 1}
        reference = baseline.ProjectBaselineReference(hashlib.sha256(baseline._encoded(old_body)).hexdigest(),
            reference.input_source_sha256, reference.file_count)
    with pytest.raises(baseline.ProjectBaselineError):
        store.load(invocation, reference)
