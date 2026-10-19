from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from tests.unit.capabilities.project_original_fixture import failed_observation
from tests.unit.capabilities.test_project_baselines import source_tree
from tests.unit.capabilities.test_scoped_read import FakeRunRepository, stored_run


async def configured(tmp_path: Path, *, prepared: bool) -> tuple[RuntimeCapabilityGateway, Path, Any]:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    selected = ManagedProjectInput(editable_paths=('src',))
    extra: dict[str, object] = {
        'project_input_contract_version': 1, 'project_input': selected.to_payload(),
        'requested_permissions': ['workspace.read', 'workspace.write', 'command.run'],
    }
    if prepared:
        reference = await ProjectBaselineStore(root).capture_once(invocation, selected)
        original = ProjectBaselineStore(root).load(invocation, reference)
        observation = failed_observation(reference.input_source_sha256, original.contract.protected_sha256)
        extra['project_input_preparation'] = {'schema_version': 1, 'phase': 'observed',
            'reference': reference.to_payload(), 'observation': observation,
            'observation_sha256': hashlib.sha256(json.dumps(observation, default=dict, ensure_ascii=True,
                sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()}
    record = stored_run(tenant=invocation.tenant_id, run=invocation.run_id)
    record = replace(record, routing_decision={**(record.routing_decision or {}), **extra})
    return RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / 'skills', project_workspace_dir=root,
        generated_artifact_dir=tmp_path / 'artifacts', run_repository=FakeRunRepository(record),
    ), source, invocation


async def execute(backend: RuntimeCapabilityGateway, invocation: Any, name: str, **arguments: Any) -> Any:
    return await backend.execute(
        tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor='engineer',
        name=name, arguments=arguments, idempotency_key='contract-gateway-test',
    )


async def test_no_project_write_before_original_baseline_preparation(tmp_path: Path) -> None:
    backend, source, invocation = await configured(tmp_path, prepared=False)
    with pytest.raises(RuntimeCapabilityError, match='original project'):
        await execute(backend, invocation, 'workspace.write_text', path='src/index.js', content='changed')
    assert (source / 'src/index.js').read_bytes() == b'module.exports = () => 0;\n'


@pytest.mark.parametrize('prepared', [False, True])
async def test_original_project_bundle_never_falls_back_to_unverified_zip_without_manifest(
    tmp_path: Path, prepared: bool,
) -> None:
    backend, source, invocation = await configured(tmp_path, prepared=prepared)
    (source / 'package.json').unlink()
    with pytest.raises(RuntimeCapabilityError, match='original project|project verification|execution backend'):
        await execute(backend, invocation, 'workspace.bundle')
    assert not list((tmp_path / 'artifacts').rglob('*.zip'))


@pytest.mark.parametrize('path', ['tests/original.js', 'package.json', 'new-test.js', 'src/package.json'])
async def test_original_checks_and_new_out_of_scope_checks_cannot_be_written(tmp_path: Path, path: str) -> None:
    backend, source, invocation = await configured(tmp_path, prepared=True)
    before = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    with pytest.raises(RuntimeCapabilityError, match='original project'):
        await execute(backend, invocation, 'workspace.write_text', path=path, content='weakened')
    assert {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*') if p.is_file()} == before


async def test_explicit_application_edit_preserves_original_snapshot(tmp_path: Path) -> None:
    backend, source, invocation = await configured(tmp_path, prepared=True)
    result = await execute(backend, invocation, 'workspace.write_text', path='src/index.js',
                           content='module.exports = () => 12;\n')
    assert result['workspace_files']
    assert (source / 'src/index.js').read_text() == 'module.exports = () => 12;\n'
    assert (source / 'tests/original.js').read_bytes() == b'// immutable original user check\n'


async def test_prune_cannot_remove_original_checks(tmp_path: Path) -> None:
    backend, source, invocation = await configured(tmp_path, prepared=True)
    with pytest.raises(RuntimeCapabilityError, match='original project'):
        await execute(backend, invocation, 'workspace.prune', keep_paths=['src/index.js'])
    assert (source / 'tests/original.js').exists()
    assert (source / 'package.json').exists()


@pytest.mark.parametrize('name,arguments', [
    ('project.generate_zip', {'title': 'bypass', 'project_id': 'project-a',
                              'workspace_session_id': 'session-a', 'files': {'tests/original.js': 'fake'}}),
    ('project.preflight_architecture', {'project_id': 'project-a', 'workspace_session_id': 'session-a',
                                       'request': 'overwrite original project'}),
])
async def test_alternate_project_writers_cannot_bypass_scoped_contract(
    tmp_path: Path, name: str, arguments: dict[str, Any],
) -> None:
    backend, source, invocation = await configured(tmp_path, prepared=True)
    with pytest.raises(RuntimeCapabilityError, match='original project'):
        await execute(backend, invocation, name, **arguments)
    assert (source / 'tests/original.js').read_bytes() == b'// immutable original user check\n'
