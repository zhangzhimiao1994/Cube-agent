"""Real workspace/snapshot selection; no project execution, network or paid model."""
from __future__ import annotations

import hashlib
import json
import os
import runpy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent_hub.api.routers.runs import ManagedProjectInputRequest
from agent_hub.capabilities import project_contracts as contracts
from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.runs import automatic_project_inputs as automatic
from agent_hub.runs.project_inputs import python_original_source_index
from agent_hub.runtime.contracts import Artifact
from agent_hub.runtime.python_project_history import _files
from agent_hub.skills.sandbox.project import ProjectInvocation
from tests.unit.runs.conversation_authenticated_input_typed_fixture import mutable, run
from tests.unit.runs.test_automatic_project_inputs_oct28 import (
    STATE,
    memory_threads,  # noqa: F401
    selector,
)


async def test_real_large_original_selection_snapshot_and_history_keep_exact_files(tmp_path: Path) -> None:
    # Baseline capture requires a private root on Windows too; keep the actual ACL guard.
    root = tmp_path / 'workspaces'
    if os.name == 'nt':
        from agent_hub.plugins.handover_snapshots import _windows_security
        _windows_security(root, private=True, create=True)
    else:
        root.mkdir(mode=0o700)
    preparer, repository, context, path = selector(tmp_path)
    for index in range(160):
        (path / 'src' / f'module_{index}.py').write_text('value = 1\n')
    selected = await preparer.select_existing(context)
    selection = contracts.ManagedProjectInput.from_payload(selected.routing_decision['project_input'])
    assert len(selection.editable_paths) == 164
    assert all(f'src/module_{index}.py' in selection.editable_paths for index in range(160))
    assert 'src' not in selection.editable_paths
    assert 'tests/test_original.py' not in selection.editable_paths
    assert 'src/config.py' not in selection.editable_paths
    assert 'src/ticket_summary/__init__.py' not in selection.editable_paths
    assert mutable(mutable(repository.row.routing_decision)[STATE])['kind'] == 'original'
    before = repository.row.version
    restored = await preparer.select_existing(selected)
    assert restored.routing_decision == selected.routing_decision and repository.row.version == before
    inv = ProjectInvocation(execution_id='large_original', tenant_id=context.tenant_id, run_id=context.run_id,
        project_id='existing', session_id='session-a', sandbox_profile='workspace_write', timeout_seconds=30)
    store = ProjectBaselineStore(preparer.root)
    reference = await store.capture_once(inv, selection)
    stored = store.load(inv, reference)
    source_index = python_original_source_index(stored, source_limit_bytes=inv.source_limit_bytes)
    artifact = Artifact(id=uuid4(), type='original_project_observation', producer='original_project_preparer', content={
        'python_source_index': source_index, 'editable_paths': selection.editable_paths,
        'original_source_sha256': reference.input_source_sha256,
    })
    assert len(_files(artifact)) == 165
    native = runpy.run_path(contracts.__file__)['ProjectCheckContract'].from_payload(stored.contract.to_payload())
    native.require_current(stored.source_files)
    assert not native.is_editable('src/unlisted.py')
    assert not native.is_editable('tests/test_original.py')
    assert not native.is_editable('src/package.json')


def test_api_and_bound_contract_allow_more_short_paths_within_same_byte_envelope() -> None:
    paths = tuple(f'src/module_{index}.py' for index in range(160))
    request = ManagedProjectInputRequest.model_validate({'editable_paths': list(paths)})
    assert request.to_domain().editable_paths == tuple(sorted(paths))
    files = {path: 'a' * 64 for path in (*paths, 'tests/test_original.py', 'pyproject.toml')}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    from uuid import UUID
    contract = contracts.build_project_check_contract(UUID(int=1), UUID(int=2), 'demo', 'one', digest, paths, files)
    assert contract.protected_files == {'tests/test_original.py': 'a' * 64, 'pyproject.toml': 'a' * 64}
    contract.require_current(files)
    with pytest.raises(ValueError):
        contract.require_current({**files, 'src/unlisted.py': 'a' * 64})


def test_dynamic_selection_still_reserves_new_files_under_native_file_budget() -> None:
    paths = ('pyproject.toml', *(f'src/module_{i}.py' for i in range(160)))
    from uuid import UUID
    with pytest.raises(ValueError, match='scope_limit'):
        automatic._editables(paths, 'Please add output', UUID(int=2), True, file_limit=162)
    assert len(automatic._editables(paths, 'Please add output', UUID(int=2), True, file_limit=163)) == 162


def test_oversized_path_metadata_remains_blocked() -> None:
    paths = tuple(f'{i:03}/' + '/'.join(('a' * 255,) * 3) + '/' + 'b' * 252 for i in range(129))
    assert all(len(path) == 1024 for path in paths)
    assert len(contracts.ManagedProjectInput(paths[:128]).editable_paths) == 128
    with pytest.raises(ValueError):
        contracts.ManagedProjectInput(paths)


@pytest.mark.parametrize('extra', ['../escape.py', 'src\\evil.py', 'src/package.json',
                                  'src/module_0.py', 'SRC/module_0.py', 'src'])
def test_large_selection_never_weakens_original_path_guards(extra: str) -> None:
    paths = tuple(f'src/module_{i}.py' for i in range(160))
    with pytest.raises(ValueError):
        contracts.ManagedProjectInput((*paths, extra))


def test_context_node_budget_blocks_before_selected_state_is_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from agent_hub.files.workspace import ProjectWorkspaceStore

    preparer, repository, context, _ = selector(tmp_path)
    paths = ('pyproject.toml', *(f'src/m_{i}.py' for i in range(2200)))
    monkeypatch.setattr(ProjectWorkspaceStore, 'list_files', lambda *a, **k: [
        SimpleNamespace(path=path) for path in paths])
    calls = []

    async def manifest(*args: Any, **kwargs: Any) -> dict[str, str]:
        calls.append(True)
        return {path: 'a' * 64 for path in paths}

    monkeypatch.setattr(automatic, 'project_source_manifest', manifest)
    with pytest.raises(RuntimeError, match='automatic project'):
        run(preparer.select_existing(context))
    assert calls == [True, True]
    assert mutable(mutable(repository.row.routing_decision)[STATE])['kind'] == 'blocked'
    assert mutable(mutable(repository.row.routing_decision)[STATE])['reason'] == 'scope_limit'
    assert 'editable_paths' not in mutable(mutable(repository.row.routing_decision)[STATE])
    assert 'project_input' not in mutable(repository.row.routing_decision)
