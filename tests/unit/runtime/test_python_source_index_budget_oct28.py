"""Complete Python index proof using private local snapshots and fake native receipts."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.runs.project_inputs import python_original_source_index
from agent_hub.runtime.contracts import Artifact
from agent_hub.runtime.python_project_history import (
    _files,
    _history,
    build_python_history,
    python_initial_files,
)
from tests.unit.capabilities import test_python_project_service as service_cases
from tests.unit.capabilities.test_python_project_input_store import python_controls
from tests.unit.runtime.test_python_project_history import history_case


def index_artifact(count: int = 270, *, prefix: str = 'src/m') -> Artifact:
    return Artifact(id=uuid4(), type='original_project_observation', producer='original_project_preparer',
        content={'original_source_sha256': 'a' * 64, 'editable_paths': ('src',),
            'python_source_index': {'schema_version': 1, 'source_sha256': 'a' * 64, 'editable_files': (),
                'files': tuple({'path': f'{prefix}{i}.py', 'sha256': hashlib.sha256(b'x').hexdigest(),
                    'size_bytes': 1, 'source_file_sha256': hashlib.sha256(b'f\0x').hexdigest()}
                    for i in range(count))}})


def test_reader_accepts_complete_index_over_256_within_metadata_budget() -> None:
    artifact = index_artifact()
    files = _files(artifact)
    assert len(files) == 270
    assert {row['path'] for row in files} == {f'src/m{i}.py' for i in range(270)}


@pytest.fixture(scope='module')
async def large_case(tmp_path_factory: pytest.TempPathFactory) -> Any:
    controls = python_controls

    def populated(source: Path) -> None:
        controls(source)
        for i in range(268):
            (source / 'src' / f'module_number_{i:03}.py').write_bytes(b'x')

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(service_cases, 'python_controls', populated)
        return await history_case(tmp_path_factory.mktemp('large'))


async def test_real_snapshot_history_and_runtime_keep_all_270_files(large_case: Any) -> None:
    _, _, executor, original, context = large_case
    files = python_initial_files(context)
    assert len(files) == 270
    assert set(files) == {p for p in original.stored.source_files if p.endswith('.py')}
    assert len(cast(tuple[object, ...], _history(context)['initial_files'])) == 270
    assert len(executor.calls) == 1
    assert not original.inputs.contract.is_editable('tests/test_billing.py')
    for name, (_, digest, size) in files.items():
        raw = (original.stored.snapshot / name).read_bytes()
        assert digest == hashlib.sha256(raw).hexdigest() and size == len(raw)


def test_producer_uses_whole_native_file_budget_before_reading(large_case: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    original = large_case[3]
    from agent_hub.runs import project_inputs

    monkeypatch.setattr(project_inputs, '_read_no_links', lambda *a, **k: pytest.fail('over-budget IO'))
    with pytest.raises(ValueError, match='budget_exceeded'):
        python_original_source_index(original.stored, source_limit_bytes=original.invocation.source_limit_bytes,
                                     file_limit=original.stored.reference.file_count - 1)


def test_producer_rejects_metadata_nodes_before_opening_files(large_case: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.capabilities.project_baselines import _fingerprint
    from agent_hub.runs import project_inputs

    original = large_case[3]
    files = {f'src/m{i}.py': hashlib.sha256(b'f\0x').hexdigest() for i in range(600)}
    reference = replace(original.stored.reference, file_count=len(files), input_source_sha256=_fingerprint(files))
    stored = replace(original.stored, reference=reference, source_files=files)
    monkeypatch.setattr(project_inputs, '_read_no_links', lambda *a, **k: pytest.fail('metadata overflow IO'))
    with pytest.raises(ValueError, match='budget_exceeded'):
        python_original_source_index(stored, source_limit_bytes=original.invocation.source_limit_bytes,
                                     file_limit=original.invocation.file_limit)


def test_source_byte_budget_counts_every_file_and_never_returns_partial_index(large_case: Any) -> None:
    original, context = large_case[3:]
    expected = sum(item[2] for item in python_initial_files(context).values())
    exact = python_original_source_index(original.stored, source_limit_bytes=expected,
                                        file_limit=original.invocation.file_limit)
    assert len(cast(tuple[object, ...], exact['files'])) == 270
    with pytest.raises(RuntimeError, match='original project preparation is unavailable or changed'):
        python_original_source_index(original.stored, source_limit_bytes=expected - 1,
                                     file_limit=original.invocation.file_limit)


@pytest.mark.parametrize('budget', ['files', 'bytes'])
def test_reader_uses_explicit_limits_without_truncating(budget: str) -> None:
    kwargs = {'file_limit': 269} if budget == 'files' else {'source_limit_bytes': 269}
    with pytest.raises(ValueError):
        _files(index_artifact(), **kwargs)
    assert len(_files(index_artifact(), file_limit=270, source_limit_bytes=270)) == 270


def test_full_history_still_has_64k_limit_with_many_valid_references(large_case: Any) -> None:
    original, context = large_case[3:]
    reference = original.environment_references[0]
    references = tuple(reference.model_copy(update={'origin_execution_id': f'candidate_{i}_' + 'x' * 114})
                       for i in range(128))
    with pytest.raises(ValueError, match='budget_exceeded'):
        build_python_history(replace(original, environment_references=references), context.artifacts[0])


def test_complete_context_keeps_its_existing_node_budget(large_case: Any) -> None:
    context = large_case[4]
    before = context.to_payload()
    with pytest.raises((ValueError, TypeError)):
        context.model_copy(update={'routing_decision': {**context.routing_decision,
            'extra_context': ('x',) * 4096}}).validated_internal_clone()
    assert context.to_payload() == before


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'wrong_source_hash', 'protected_scope'])
def test_incomplete_or_corrupt_large_index_never_becomes_history(large_case: Any, defect: str) -> None:
    original, context = large_case[3:]
    artifact = context.artifacts[0]
    content = dict(artifact.content)
    source = dict(content['python_source_index'])
    rows = list(source['files'])
    if defect == 'missing':
        rows.pop()
    elif defect == 'duplicate':
        rows[-1] = rows[0]
    elif defect == 'wrong_source_hash':
        rows[0] = {**rows[0], 'source_file_sha256': '0' * 64}
    else:
        source['editable_files'] = ('tests/test_billing.py',)
    source['files'] = tuple(rows)
    content['python_source_index'] = source
    changed = Artifact(id=artifact.id, type=artifact.type, producer=artifact.producer, content=content)
    with pytest.raises(ValueError):
        build_python_history(original, changed)


@pytest.mark.parametrize('limit', [True, '1000', 0, 100_001])
def test_native_file_limit_cannot_be_coerced_or_grown(large_case: Any, limit: Any) -> None:
    original = large_case[3]
    with pytest.raises(ValueError, match='budget_exceeded'):
        python_original_source_index(original.stored, file_limit=limit,
                                     source_limit_bytes=original.invocation.source_limit_bytes)
