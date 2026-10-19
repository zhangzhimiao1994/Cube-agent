from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineReference, ProjectBaselineStore
from agent_hub.capabilities.project_contracts import (
    ManagedProjectInput,
    build_project_check_contract,
)
from agent_hub.skills.sandbox import project_contract_broker as broker
from agent_hub.skills.sandbox.project import ProjectInvocation
from tests.unit.capabilities.test_project_baselines import source_tree


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def original_fixture() -> tuple[ProjectInvocation, ProjectBaselineReference, bytes, bytes]:
    from uuid import UUID

    invocation = ProjectInvocation(
        execution_id='stage_contract', tenant_id=UUID(int=1), run_id=UUID(int=2),
        project_id='project-a', session_id='session-a', sandbox_profile='workspace_write',
        timeout_seconds=30,
    )
    files = {'src/app.py': hashlib.sha256(b'f\0original').hexdigest(),
             'tests/check.py': hashlib.sha256(b'f\0assert original').hexdigest()}
    source = hashlib.sha256(encoded(files)).hexdigest()
    contract = build_project_check_contract(
        invocation.tenant_id, invocation.run_id, invocation.project_id, invocation.session_id,
        source, ('src',), files,
    )
    raw = encoded({'schema_version': 1, 'contract': contract.to_payload(), 'source_files': files})
    intent = encoded({
        'schema_version': 1, 'state': 'reserved', 'tenant_id': str(invocation.tenant_id),
        'run_id': str(invocation.run_id), 'project_id': invocation.project_id,
        'session_id': invocation.session_id, 'project_input': ManagedProjectInput(('src',)).to_payload(),
    })
    return invocation, ProjectBaselineReference(hashlib.sha256(raw).hexdigest(), source, 2), raw, intent


def test_native_envelope_decoder_binds_original_intent_and_distinguishes_contract_digest() -> None:
    invocation, reference, raw, intent = original_fixture()
    decode = getattr(broker, '_decode_original', None)
    assert callable(decode), 'trusted root broker original-contract staging is missing'
    contract, files = decode(raw, intent, invocation, reference)
    assert contract.input_source_sha256 == reference.input_source_sha256
    assert len(files) == reference.file_count
    assert contract.protected_sha256 != reference.contract_sha256
    assert set(contract.protected_files) == {'tests/check.py'}


@pytest.mark.parametrize('mutation', [
    'duplicate_envelope', 'duplicate_intent', 'extra_authority', 'missing_manifest',
    'widened_scope', 'wrong_scope', 'noncanonical', 'bad_version', 'intent_bool', 'nested_intent_bool',
])
def test_resigned_envelope_still_requires_exact_schema_scope_intent_and_complete_manifest(mutation: str) -> None:
    invocation, reference, raw, intent = original_fixture()
    body = json.loads(raw)
    if mutation == 'duplicate_intent':
        intent = intent.replace(b'"project_input":{', b'"project_input":{"schema_version":1,', 1)
    elif mutation in {'intent_bool', 'nested_intent_bool'}:
        selected = json.loads(intent)
        target = selected if mutation == 'intent_bool' else selected['project_input']
        target['schema_version'] = True
        intent = encoded(selected)
    elif mutation == 'extra_authority':
        body['install_allowed'] = True
    elif mutation == 'missing_manifest':
        del body['source_files']['tests/check.py']
    elif mutation == 'widened_scope':
        body['contract'] = build_project_check_contract(
            invocation.tenant_id, invocation.run_id, invocation.project_id, invocation.session_id,
            reference.input_source_sha256, ('src', 'tests'), body['source_files'],
        ).to_payload()
    elif mutation == 'wrong_scope':
        invocation = invocation.model_copy(update={'session_id': 'other'})
    elif mutation == 'bad_version':
        body['schema_version'] = True
    raw = encoded(body)
    if mutation == 'duplicate_envelope':
        raw = raw.replace(b'{"contract":', b'{"schema_version":1,"contract":', 1)
    elif mutation == 'noncanonical':
        raw += b'\n'
    resigned = ProjectBaselineReference(hashlib.sha256(raw).hexdigest(), reference.input_source_sha256,
                                        reference.file_count)
    with pytest.raises(ValueError):
        broker._decode_original(raw, intent, invocation, resigned)


@pytest.mark.parametrize('allowed_uid', [0, True, -1, '10001'])
def test_staging_rejects_invalid_or_root_service_identity_without_output(
    tmp_path: Path, allowed_uid: object,
) -> None:
    invocation, reference, _, _ = original_fixture()
    stage = tmp_path / 'stage'
    stage.mkdir()
    (stage / 'sentinel').write_bytes(b'keep')
    with pytest.raises(broker.ProjectContractStageError) as caught:
        broker.stage_original_contract(tmp_path / 'missing', invocation, reference, stage, cast(int, allowed_uid))
    assert str(caught.value) == 'project original contract staging unavailable or unsafe'
    assert (stage / 'sentinel').read_bytes() == b'keep'
    assert list(stage.iterdir()) == [stage / 'sentinel']


def linux_root() -> bool:
    if sys.platform != 'linux':
        return False
    return os.geteuid() == 0


linux_permissions = pytest.mark.skipif(not linux_root(), reason='requires real Linux root and chown; never mocked')


async def service_baseline(tmp_path: Path) -> tuple[Path, ProjectInvocation, ProjectBaselineReference, Path, int]:
    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    reference = await ProjectBaselineStore(root).capture_once(invocation, ManagedProjectInput(('src',)))
    allowed_uid = 65534
    if sys.platform == 'linux':
        for path in (root, *root.rglob('*')):
            os.chown(path, allowed_uid, allowed_uid, follow_symlinks=False)
    stage = tmp_path / 'broker-stage'
    stage.mkdir(mode=0o700)
    return root, invocation, reference, stage, allowed_uid


@linux_permissions
async def test_linux_root_copies_service_owned_baseline_not_current_workspace(tmp_path: Path) -> None:
    root, invocation, reference, stage, allowed_uid = await service_baseline(tmp_path)
    current = (root / str(invocation.tenant_id) / 'projects' / invocation.project_id
               / 'sessions' / invocation.session_id / 'tests/original.js')
    current.write_bytes(b'changed current workspace, not the captured original')
    (stage / 'execution.json').write_bytes(b'{"state":"STAGED"}')
    stored = broker.stage_original_contract(root, invocation, reference, stage, allowed_uid)
    assert stored.reference == reference
    assert stored.snapshot == stage / 'original_snapshot'
    assert (stored.snapshot / 'tests/original.js').read_bytes() == b'// immutable original user check\n'
    assert (stored.snapshot / 'tests/original.js').stat().st_uid == 0
    assert (stored.snapshot.stat().st_mode & 0o777) == 0o555
    assert json.loads((stage / 'check_contract.json').read_bytes()) == stored.contract.to_payload()
    assert stored.contract.protected_sha256 != reference.contract_sha256
    assert (stage / 'execution.json').read_bytes() == b'{"state":"STAGED"}'


@linux_permissions
@pytest.mark.parametrize('target', ['original_snapshot', 'check_contract.json'])
async def test_linux_existing_output_is_never_removed_or_overwritten(tmp_path: Path, target: str) -> None:
    root, invocation, reference, stage, allowed_uid = await service_baseline(tmp_path)
    existing = stage / target
    if target == 'original_snapshot':
        existing.mkdir()
        existing /= 'sentinel'
    existing.write_bytes(b'keep existing output')
    with pytest.raises(broker.ProjectContractStageError):
        broker.stage_original_contract(root, invocation, reference, stage, allowed_uid)
    assert existing.read_bytes() == b'keep existing output'


@linux_permissions
@pytest.mark.parametrize('mutation', ['root_owner', 'control_mode', 'file_owner', 'snapshot_writable', 'hardlink'])
async def test_linux_real_owner_mode_and_alias_violations_reject_before_pass(
    tmp_path: Path, mutation: str,
) -> None:
    root, invocation, reference, stage, allowed_uid = await service_baseline(tmp_path)
    controls = root / '.project-contracts' / str(invocation.tenant_id) / str(invocation.run_id)
    if sys.platform == 'linux':
        if mutation == 'root_owner':
            os.chown(root, 0, 0)
        elif mutation == 'control_mode':
            (controls / 'capture-intent.json').chmod(0o644)
        elif mutation == 'file_owner':
            os.chown(controls / 'baseline.json', 0, 0)
        elif mutation == 'snapshot_writable':
            (controls / 'snapshot/source/tests/original.js').chmod(0o644)
        else:
            (tmp_path / 'alias').hardlink_to(controls / 'baseline.json')
    with pytest.raises(broker.ProjectContractStageError) as caught:
        broker.stage_original_contract(root, invocation, reference, stage, allowed_uid)
    assert str(root) not in str(caught.value)


@linux_permissions
async def test_linux_directory_link_and_source_byte_limit_fail_closed(tmp_path: Path) -> None:
    root, invocation, reference, stage, allowed_uid = await service_baseline(tmp_path)
    alias = tmp_path / 'root-alias'
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(broker.ProjectContractStageError):
        broker.stage_original_contract(alias, invocation, reference, stage, allowed_uid)
    with pytest.raises(broker.ProjectContractStageError):
        broker.stage_original_contract(root, invocation.model_copy(update={'source_limit_bytes': 1}),
                                       reference, stage, allowed_uid)
    assert not (stage / 'check_contract.json').exists()


@linux_permissions
async def test_linux_late_snapshot_file_mutation_cannot_pass_fd_rechecks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, invocation, reference, stage, allowed_uid = await service_baseline(tmp_path)
    copy_file = broker._copy_file
    first: list[tuple[int, str]] = []

    def changed(parent_fd: int, name: str, *args: Any, **kwargs: Any) -> Any:
        if not first:
            first.append((parent_fd, name))
        elif len(first) == 1:
            first_fd, first_name = first[0]
            descriptor = os.open(first_name, os.O_WRONLY | os.O_TRUNC, dir_fd=first_fd)
            try:
                os.write(descriptor, b'PRIVATE mutation')
            finally:
                os.close(descriptor)
            first.append((parent_fd, name))
        return copy_file(parent_fd, name, *args, **kwargs)

    monkeypatch.setattr(broker, '_copy_file', changed)
    with pytest.raises(broker.ProjectContractStageError) as caught:
        broker.stage_original_contract(root, invocation, reference, stage, allowed_uid)
    assert 'PRIVATE' not in str(caught.value)
    assert not (stage / 'check_contract.json').exists()
