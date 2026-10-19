from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from tests.unit.capabilities.test_project_baselines import source_tree


async def test_original_context_reads_immutable_input_not_current_repair(tmp_path: Path) -> None:
    from agent_hub.runs.project_source_context import original_source_context

    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    store = ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, ManagedProjectInput(('src',)))
    stored = store.load(invocation, reference)
    (source / 'src/index.js').write_text('changed current application')
    result = original_source_context(stored, byte_budget=4096)
    assert result['file_count'] == 3
    assert result['truncated'] is False
    assert result['files'][0]['path'] == 'src/index.js'
    assert result['files'][0]['content'] == 'module.exports = () => 0;\n'


async def test_original_context_is_bounded_and_tampering_never_supplies_model_input(tmp_path: Path) -> None:
    from agent_hub.runs.project_source_context import original_source_context

    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    store = ProjectBaselineStore(root)
    reference = await store.capture_once(invocation, ManagedProjectInput(('src',)))
    stored = store.load(invocation, reference)
    assert original_source_context(stored, byte_budget=1)['truncated'] is True
    original = stored.snapshot / 'src/index.js'
    original.chmod(0o600)
    original.write_text('tampered original')
    with pytest.raises(RuntimeError, match='original project'):
        original_source_context(stored, byte_budget=4096)
