"""Real pre-model source projection with private synthetic snapshots, no native execution."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineReference, ProjectBaselineStore
from agent_hub.capabilities.scoped_read import _read_no_links
from agent_hub.runs.project_source_context import OriginalSourceContext, original_source_context
from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext
from agent_hub.runtime.python_project_history import _history
from tests.unit.runs.test_original_project_preparation import setup
from tests.unit.runs.test_python_service_preparation import case


def encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, ensure_ascii=True, separators=(',', ':')).encode('ascii')


def populate(root: Path, invocation: Any, count: int, suffix: str) -> Path:
    source = (root / str(invocation.tenant_id) / 'projects' / str(invocation.project_id)
              / 'sessions' / str(invocation.session_id))
    for number in range(count):
        (source / 'src' / f'm{number:03}.{suffix}').write_bytes(b'x = 1\n')
    return source


def observation(context: TaskContext) -> Artifact:
    return next(a for a in context.artifacts if a.type == 'original_project_observation')


async def test_node_producer_includes_all_small_files_above_128(tmp_path: Path) -> None:
    preparer, repository, context, observer, _, root, invocation = await setup(tmp_path)
    source = populate(root, invocation, 180, 'js')
    before = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    prepared = await preparer(context.model_copy(update={'token_budget': 96_000}))
    preview = cast(OriginalSourceContext, observation(prepared).content['source_context'])
    assert preview['included_count'] == preview['file_count'] == 183
    assert preview['truncated'] is False
    assert {row['path'] for row in preview['files']} == set(before)
    assert len(encoded(preview)) <= 96_000
    state = repository.row.routing_decision['project_input_preparation']
    stored = ProjectBaselineStore(root).load(invocation, ProjectBaselineReference.from_payload(state['reference']))
    assert not stored.contract.is_editable('tests/original.js')
    assert not stored.contract.is_editable('package.json')
    for row in preview['files']:
        assert row['content'].encode() == before[row['path']]
        assert row['sha256'] == stored.source_files[row['path']]
    assert before == {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    assert observer.count == 1


async def test_python_real_service_fits_preview_and_complete_index_above_128(tmp_path: Path) -> None:
    service, preparer, repository, context, executor = await case(tmp_path)
    root = cast(Path, executor.root)
    project = context.routing_decision['project_id']
    session = context.routing_decision['workspace_session_id']
    source = root / str(context.tenant_id) / 'projects' / str(project) / 'sessions' / str(session)
    for number in range(180):
        (source / 'src' / f'm{number:03}.py').write_bytes(b'x = 1\n')
    prepared = await service._prepare_original_project_context(context.model_copy(update={
        'token_budget': 96_000, 'timeout_seconds': 600}))
    artifact = observation(prepared)
    preview = cast(OriginalSourceContext, artifact.content['source_context'])
    assert preview['included_count'] == preview['file_count'] > 180
    assert preview['truncated'] is False
    index = cast(Mapping[str, JsonValue], artifact.content['python_source_index'])
    assert len(cast(tuple[object, ...], index['files'])) == 182
    assert len(cast(tuple[object, ...], _history(prepared)['initial_files'])) == 182
    assert prepared.validated_internal_clone().to_payload() == prepared.to_payload()
    assert len(executor.calls) == 1
    assert repository.row.routing_decision['project_input_preparation']['phase'] == 'observed'
    assert preparer.root == root


async def test_full_index_wins_over_preview_and_result_remains_valid(tmp_path: Path) -> None:
    service, preparer, repository, context, executor = await case(tmp_path)
    source = (preparer.root / str(context.tenant_id) / 'projects'
              / str(context.routing_decision['project_id']) / 'sessions'
              / str(context.routing_decision['workspace_session_id']))
    for number in range(340):
        (source / 'src' / f'm{number:03}.py').write_bytes(b'x = 1\n')
    prepared = await service._prepare_original_project_context(context.model_copy(update={
        'token_budget': 96_000, 'timeout_seconds': 600}))
    artifact = observation(prepared)
    preview = cast(OriginalSourceContext, artifact.content['source_context'])
    assert 0 < preview['included_count'] < 128
    assert preview['included_count'] == len(preview['files']) < preview['file_count']
    assert preview['truncated'] is True
    index = cast(Mapping[str, JsonValue], artifact.content['python_source_index'])
    assert len(cast(tuple[object, ...], index['files'])) == 342
    assert len(cast(tuple[object, ...], _history(prepared)['initial_files'])) == 342
    assert prepared.validated_internal_clone().to_payload() == prepared.to_payload()
    state = repository.row.routing_decision['project_input_preparation']
    invocation = executor.calls[0]
    from agent_hub.skills.sandbox.python_project_protocol import base_python_invocation

    stored = ProjectBaselineStore(preparer.root).load(
        base_python_invocation(invocation), ProjectBaselineReference.from_payload(state['reference']))
    full = original_source_context(stored, byte_budget=96_000, fits=lambda candidate: True)
    assert full['truncated'] is False
    with pytest.raises(ValueError):
        Artifact(id=artifact.id, type=artifact.type, producer=artifact.producer,
                 content={**artifact.content, 'source_context': cast(Mapping[str, JsonValue], full)})


async def test_index_overflow_rejects_before_preview_source_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, preparer, repository, context, executor = await case(tmp_path)
    source = (preparer.root / str(context.tenant_id) / 'projects'
              / str(context.routing_decision['project_id']) / 'sessions'
              / str(context.routing_decision['workspace_session_id']))
    for number in range(460):
        (source / 'src' / f'm{number:03}.py').write_bytes(b'x = 1\n')
    from agent_hub.runs import project_source_context

    def unexpected(*args: Any, **kwargs: Any) -> bytes:
        pytest.fail('preview must not open files when the complete index is already over budget')

    monkeypatch.setattr(project_source_context, '_read_no_links', unexpected)
    with pytest.raises(ValueError, match='budget_exceeded'):
        await service._prepare_original_project_context(context.model_copy(update={
            'token_budget': 96_000, 'timeout_seconds': 600}))
    assert repository.row.routing_decision['project_input_preparation']['phase'] == 'observed'
    assert len(executor.calls) == 1


async def test_context_overflow_cannot_be_hidden_by_dropping_index(tmp_path: Path) -> None:
    service, preparer, repository, context, executor = await case(tmp_path)
    source = (preparer.root / str(context.tenant_id) / 'projects'
              / str(context.routing_decision['project_id']) / 'sessions'
              / str(context.routing_decision['workspace_session_id']))
    for number in range(270):
        (source / 'src' / f'm{number:03}.py').write_bytes(b'x = 1\n')
    context = context.model_copy(update={'routing_decision': {
        **context.routing_decision, 'extra_context': ('x',) * 2600},
        'token_budget': 96_000, 'timeout_seconds': 600}).validated_internal_clone()
    repository.row.routing_decision['extra_context'] = ['x'] * 2600
    with pytest.raises((ValueError, RuntimeError)):
        await service._prepare_original_project_context(context)
    assert repository.row.routing_decision['project_input_preparation']['phase'] == 'observed'
    assert len(executor.calls) == 1


async def test_preview_hash_mismatch_cannot_enter_producer_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparer, _, context, _, _, root, invocation = await setup(tmp_path)
    populate(root, invocation, 150, 'js')
    from agent_hub.runs import project_source_context

    def changed(path: Path, parts: tuple[str, ...], **kwargs: Any) -> bytes:
        if parts == ('src', 'm000.js'):
            return b'tampered'
        return _read_no_links(path, parts, **kwargs)

    monkeypatch.setattr(project_source_context, '_read_no_links', changed)
    with pytest.raises(RuntimeError, match='source context is unavailable or changed'):
        await preparer(context.model_copy(update={'token_budget': 96_000}))


async def test_preview_byte_budget_counts_json_escaping_and_truthful_omission(tmp_path: Path) -> None:
    preparer, repository, context, _, _, root, invocation = await setup(tmp_path)
    source = populate(root, invocation, 150, 'js')
    (source / 'src/m000.js').write_text('"\\\u4e2d"' * 500, encoding='utf-8')
    prepared = await preparer(context.model_copy(update={'token_budget': 4096}))
    preview = cast(OriginalSourceContext, observation(prepared).content['source_context'])
    assert preview['truncated'] is True
    assert preview['included_count'] == len(preview['files']) < preview['file_count']
    assert len(encoded(preview)) <= 4096
    state = repository.row.routing_decision['project_input_preparation']
    stored = ProjectBaselineStore(root).load(invocation, ProjectBaselineReference.from_payload(state['reference']))
    for row in cast(tuple[Mapping[str, JsonValue], ...], preview['files']):
        assert row['sha256'] == stored.source_files[str(row['path'])]
        assert hashlib.sha256(b'f\0' + str(row['content']).encode()).hexdigest() == row['sha256']
