"""Pure v2 decoding plus real-Linux-only Root staging; never fake native IO."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineReference, ProjectBaselineStore
from agent_hub.skills.sandbox import project_contract_broker as broker
from tests.unit.capabilities.test_generated_project_baselines import controls, scaffold
from tests.unit.skills.sandbox.test_project_contract_broker import linux_permissions


async def test_root_v2_decoder_preserves_generated_origin_without_original_alias(tmp_path: Path) -> None:
    root = tmp_path / 'workspaces'
    invocation, _, metadata = scaffold(root)
    reference = await ProjectBaselineStore(root).capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
    path = controls(root, invocation)
    raw, intent = (path / 'baseline.json').read_bytes(), (path / 'capture-intent.json').read_bytes()
    contract, files, generated = broker._decode_baseline(raw, intent, invocation, reference)
    assert generated == metadata and 'ACCEPTANCE.json' in contract.protected_files
    assert len(files) == reference.file_count
    with pytest.raises(ValueError):
        broker._decode_original(raw, intent, invocation, reference)


@pytest.mark.parametrize('defect', ['origin', 'phase', 'intent', 'uuid', 'scope', 'bool', 'extra'])
async def test_v2_decode_fails_closed_on_resigned_metadata_or_scope(tmp_path: Path, defect: str) -> None:
    root = tmp_path / 'workspaces'
    invocation, _, metadata = scaffold(root)
    reference = await ProjectBaselineStore(root).capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
    path = controls(root, invocation)
    value = json.loads((path / 'baseline.json').read_bytes())
    intent = (path / 'capture-intent.json').read_bytes()
    if defect == 'origin':
        value['purpose'] = 'original'
    elif defect == 'phase':
        value['generated_metadata']['capture_phase'] = 'pre_model'
    elif defect == 'intent':
        changed = json.loads(intent)
        changed['generated_metadata']['request_sha256'] = 'f' * 64
        intent = broker._encoded(changed)
    elif defect == 'uuid':
        value['generated_metadata']['user_id'] = value['generated_metadata']['user_id'].upper()
    elif defect == 'scope':
        invocation = invocation.model_copy(update={'session_id': 'other'})
    elif defect == 'bool':
        value['schema_version'] = True
    else:
        value['install_allowed'] = True
    raw = broker._encoded(value)
    resigned = ProjectBaselineReference(hashlib.sha256(raw).hexdigest(), reference.input_source_sha256, reference.file_count)
    with pytest.raises(ValueError):
        broker._decode_baseline(raw, intent, invocation, resigned)


@linux_permissions
async def test_real_root_staging_transfers_generated_metadata_and_remeasures_manifest(tmp_path: Path) -> None:
    root = tmp_path / 'workspaces'
    invocation, _, metadata = scaffold(root)
    reference = await ProjectBaselineStore(root).capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
    allowed_uid = 65534
    for path in (root, *root.rglob('*')):
        getattr(os, 'chown')(path, allowed_uid, allowed_uid, follow_symlinks=False)  # noqa: B009 - Windows stubs omit Linux API
    stage = tmp_path / 'root-stage'
    stage.mkdir(mode=0o700)
    stored = broker.stage_original_contract(root, invocation, reference, stage, allowed_uid)
    assert stored.generated_metadata == metadata
    assert (stored.snapshot / 'ACCEPTANCE.json').stat().st_uid == 0
    assert 'ACCEPTANCE.json' in stored.contract.protected_files
    assert stored.snapshot.stat().st_mode & 0o777 == 0o555


@linux_permissions
async def test_root_stage_rejects_misbound_manifest_even_if_envelope_resigned(tmp_path: Path) -> None:
    root = tmp_path / 'workspaces'
    invocation, _, metadata = scaffold(root)
    reference = await ProjectBaselineStore(root).capture_generated_once(invocation, editable_paths=('src',), metadata=metadata)
    path = controls(root, invocation)
    intent, body = (json.loads((path / n).read_bytes()) for n in ('capture-intent.json', 'baseline.json'))
    for value in (intent, body):
        value['generated_metadata']['manifest_sha256'] = 'f' * 64
    raw = broker._encoded(body)
    (path / 'baseline.json').write_bytes(raw)
    (path / 'capture-intent.json').write_bytes(broker._encoded(intent))
    changed = ProjectBaselineReference(hashlib.sha256(raw).hexdigest(), reference.input_source_sha256, reference.file_count)
    for member in (root, *root.rglob('*')):
        getattr(os, 'chown')(member, 65534, 65534, follow_symlinks=False)  # noqa: B009 - Windows stubs omit Linux API
    stage = tmp_path / 'root-stage'
    stage.mkdir(mode=0o700)
    with pytest.raises(broker.ProjectContractStageError):
        broker.stage_original_contract(root, invocation, changed, stage, 65534)
