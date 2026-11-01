"""Strong round receipts gate diagnostics by whole batch and causal work epoch."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from uuid import uuid4

import pytest

from agent_hub.runtime.autogen.project_repair_phase import (
    DIAGNOSTIC_WATERMARK,
    ROUND_INTENT,
    _known_batch,
    diagnostic_progress,
    validate_diagnostic_transition,
)
from agent_hub.runtime.contracts import (
    Artifact,
    JsonValue,
    RuntimeCheckpoint,
    TaskContext,
    _freeze_object,
)
from tests.unit.runs.project_auto_repair_typed_fixture import mapping as readonly_map
from tests.unit.runtime.autogen.diagnostic_batches_typed_fixture import changed, diagnostic_case
from tests.unit.runtime.autogen.project_repair_phase_typed_fixture import (
    integer,
    json_map,
    ledger,
    mapping,
    mutable,
    mutable_rows,
    number,
    sequence,
    string,
)

type Operation = tuple[str, Mapping[str, JsonValue], Mapping[str, JsonValue]]


def _digest(value: object) -> str:
    from agent_hub.runtime.contracts import _mutable_json
    return hashlib.sha256(json.dumps(
        _mutable_json(_freeze_object(value, name="diagnostic digest")), ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(',', ':'),
    ).encode()).hexdigest()


def _read(path: str, text: str) -> Operation:
    return 'workspace.read', {'path': path}, {'path': path, 'text': text, 'truncated': False}


def _progress(context: TaskContext, checkpoint: RuntimeCheckpoint, artifacts: Sequence[Artifact]) -> dict[str, int]:
    return diagnostic_progress(
        context, phase=json_map(checkpoint.state['project_repair_phase']),
        models=ledger(checkpoint.state['model_ledger']), tools=ledger(checkpoint.state['tool_ledger']),
        artifacts=artifacts,
    )


def _append_batch(context: TaskContext, checkpoint: RuntimeCheckpoint, artifacts: Sequence[Artifact], operations: Sequence[Operation]) -> tuple[RuntimeCheckpoint, tuple[Artifact, ...]]:
    models, tools = ledger(checkpoint.state['model_ledger']), ledger(checkpoint.state['tool_ledger'])
    start, actor = len(tools), string(tools[-1]['actor'])
    calls, rows, receipts = [], [], []
    names = {}
    for offset, (name, arguments, result) in enumerate(operations):
        position, arguments_hash = start + offset, _digest(arguments)
        alias = name.replace('.', '_')
        names.update({name: name, alias: name})
        calls.append({'id': f'batch-{start}-{offset}', 'name': alias, 'arguments': arguments})
        receipt = Artifact(id=uuid4(), type='tool_result', producer=actor, content={
            'tool_name': name, 'arguments_sha256': arguments_hash, 'result': result,
        })
        receipts.append(receipt)
        rows.append({
            'actor': actor, 'position': position, 'name': name, 'status': 'succeeded',
            'replay_safe': name == 'workspace.read', 'arguments_hash': arguments_hash,
            'idempotency_key': hashlib.sha256(
                f'{context.run_id}:{actor}:{position}:{name}:{arguments_hash}'.encode(),
            ).hexdigest(),
            'artifact_id': str(receipt.id), 'artifact_sha256': receipt.content_sha256,
        })
    prototype = next(item for item in artifacts if str(item.id) == models[-1]['artifact_id'])
    content = mutable(prototype.content)
    request_hash = _digest({
        'run_id': str(context.run_id), 'tenant_id': str(context.tenant_id), 'actor': actor,
        'model_position': len(models), 'tool_start': start, 'tool_calls': calls,
    })
    content.update({'text': None, 'tool_calls': calls, ROUND_INTENT: {
        'version': 1, 'run_id': str(context.run_id), 'tenant_id': str(context.tenant_id),
        'actor': actor, 'request_hash': request_hash, 'model_position': len(models),
        'tool_start': start, 'logical_model': content['logical_model'], 'tool_names': names,
    }})
    model = Artifact(id=uuid4(), type='model_result', producer='agent_hub', content=json_map(content))
    model_row = {'status': 'succeeded', 'request_hash': request_hash,
                 'artifact_id': str(model.id), 'artifact_sha256': model.content_sha256}
    artifacts = (*artifacts, model, *receipts)
    registry = {**json_map(checkpoint.state['artifact_registry']),
                **{str(item.id): item.content_sha256 for item in (model, *receipts)}}
    candidate = changed(checkpoint, model_ledger=(*models, model_row),
                        tool_ledger=(*tools, *rows), artifact_registry=registry)
    # Prove the positive protocol before deriving any negative: not merely bad hashes.
    assert _known_batch(context, ledger(candidate.state['model_ledger']),
                        ledger(candidate.state['tool_ledger']), len(models), start, artifacts)
    for receipt in (model, *receipts):
        assert receipt.content_sha256 == receipt.recompute_content_sha256()
    return candidate, artifacts


def _before_read(saved: RuntimeCheckpoint) -> RuntimeCheckpoint:
    return changed(saved,
        model_ledger=ledger(saved.state['model_ledger'])[:integer(readonly_map(saved.state['project_repair_phase'])['model_floor'])],
        tool_ledger=ledger(saved.state['tool_ledger'])[:integer(saved.state['engineering_tool_prefix'])],
        **{DIAGNOSTIC_WATERMARK: 0})


def _write(saved: RuntimeCheckpoint, artifacts: Sequence[Artifact], *, genuine: bool) -> Operation:
    row = next(row for row in reversed(ledger(saved.state['tool_ledger']))
               if row['name'] == 'workspace.write_text')
    receipt = next(item for item in artifacts if str(item.id) == row['artifact_id'])
    result = mutable(receipt.content['result'])
    metadata = mapping(sequence(result['workspace_files'])[0])
    text = 'fixed synthetic draft' if genuine else 'bad draft'
    assert genuine or hashlib.sha256(text.encode()).hexdigest() == metadata['sha256']
    metadata.update(sha256=hashlib.sha256(text.encode()).hexdigest(), size_bytes=len(text.encode()))
    return 'workspace.write_text', {'path': string(metadata['path']), 'text': text}, json_map(result)


@pytest.mark.asyncio
async def test_distinct_fresh_reads_in_same_epoch_cannot_regrant_a_window() -> None:
    context, source, saved, artifacts = await diagnostic_case()
    validate_diagnostic_transition(context, source, saved, artifacts)
    before = _progress(context, saved, artifacts)
    assert len(before) == 1
    later, artifacts = _append_batch(context, saved, artifacts, (
        _read('notes/first.txt', 'new observation A'),
        _read('notes/second.txt', 'different observation B'),
    ))
    assert _progress(context, later, artifacts) == before
    later = changed(later,
        remaining_timeout_seconds=max(0, number(saved.state['remaining_timeout_seconds']) - 1),
        remaining_absolute_timeout_seconds=number(saved.state['remaining_absolute_timeout_seconds']) - 1)
    validate_diagnostic_transition(context, saved, later, artifacts)
    exhausted = changed(saved, remaining_timeout_seconds=0.0)
    regrant = changed(later, remaining_timeout_seconds=1.0)
    with pytest.raises(ValueError):
        validate_diagnostic_transition(context, exhausted, regrant, artifacts)


@pytest.mark.asyncio
@pytest.mark.parametrize('incomplete', ['partial', 'UNKNOWN', 'waiting_approval'])
async def test_two_read_batch_grants_only_after_every_outcome_is_known(incomplete: str) -> None:
    context, _, saved, artifacts = await diagnostic_case()
    base = _before_read(saved)
    whole, artifacts = _append_batch(context, base, artifacts, (
        _read('notes/a.txt', 'complete A'), _read('notes/b.txt', 'complete B'),
    ))
    assert _progress(context, base, artifacts) == {}
    tools = mutable_rows(ledger(whole.state['tool_ledger']))
    if incomplete == 'partial':
        tools.pop()
    else:
        pending_row = tools[-1]
        pending_row['status'] = 'running' if incomplete == 'UNKNOWN' else 'waiting_approval'
        pending_row.pop('artifact_id')
        pending_row.pop('artifact_sha256')
        if incomplete == 'waiting_approval':
            pending_row['approval_id'] = str(uuid4())
    pending = changed(whole, tool_ledger=tools)
    assert _progress(context, pending, artifacts) == {}
    credits = _progress(context, whole, artifacts)
    assert len(credits) == 1 and set(credits.values()) == {len(ledger(whole.state['tool_ledger']))}
    assert whole.state['model_ledger'] == pending.state['model_ledger']


@pytest.mark.asyncio
@pytest.mark.parametrize('genuine', [False, True], ids=['noop-write', 'genuine-write'])
async def test_only_genuine_write_opens_a_new_diagnostic_work_epoch(genuine: bool) -> None:
    context, _, saved, artifacts = await diagnostic_case()
    before = _progress(context, saved, artifacts)
    written, artifacts = _append_batch(context, saved, artifacts, (
        _write(saved, artifacts, genuine=genuine),
    ))
    # A later productive effect must not contaminate the already-consumed read prefix.
    assert _progress(context, written, artifacts) == before
    observed, artifacts = _append_batch(context, written, artifacts, (
        _read('notes/after-write.txt', 'fresh post-write observation'),
    ))
    after = _progress(context, observed, artifacts)
    assert {key: after[key] for key in before} == before
    assert len(after) == 1 + int(genuine)
    if genuine:
        assert set(after.values()) == {*before.values(), len(ledger(observed.state['tool_ledger']))}
    else:
        assert after == before


@pytest.mark.asyncio
async def test_future_write_and_unacknowledged_tail_do_not_feed_back_into_read_prefix() -> None:
    context, _, saved, artifacts = await diagnostic_case()
    before = _progress(context, saved, artifacts)
    future, artifacts = _append_batch(context, saved, artifacts, (
        _write(saved, artifacts, genuine=True),
    ))
    # Even a fully persisted future write cannot change the older diagnostic key.
    assert _progress(context, saved, artifacts) == before
    assert _progress(context, future, artifacts) == before
    observed, artifacts = _append_batch(context, future, artifacts, (
        _read('notes/future.txt', 'new epoch, not yet ACKed'),
    ))
    tools = mutable_rows(ledger(observed.state['tool_ledger']))
    tools[-1]['status'] = 'running'
    tools[-1].pop('artifact_id')
    tools[-1].pop('artifact_sha256')
    unknown = changed(observed, tool_ledger=tools)
    assert _progress(context, unknown, artifacts) == before
    assert len(_progress(context, observed, artifacts)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['tenant_id', 'run_id', 'actor'])
async def test_rehashed_round_intent_cannot_cross_tool_scope(scope: str) -> None:
    context, _, saved, artifacts = await diagnostic_case()
    base = _before_read(saved)
    whole, artifacts = _append_batch(context, base, artifacts, (
        _read('notes/scoped.txt', 'legitimate scoped observation'),
    ))
    assert len(_progress(context, whole, artifacts)) == 1
    models = mutable_rows(ledger(whole.state['model_ledger']))
    original = next(item for item in artifacts if str(item.id) == models[-1]['artifact_id'])
    content = mutable(original.content)
    mapping(content[ROUND_INTENT])[scope] = 'other-actor' if scope == 'actor' else str(uuid4())
    foreign = Artifact(id=uuid4(), type='model_result', producer='agent_hub', content=json_map(content))
    models[-1].update(artifact_id=str(foreign.id), artifact_sha256=foreign.content_sha256)
    artifacts = (*artifacts, foreign)
    forged = changed(whole, model_ledger=models, artifact_registry={
        **json_map(whole.state['artifact_registry']), str(foreign.id): foreign.content_sha256,
    })
    assert foreign.content_sha256 == foreign.recompute_content_sha256()
    assert foreign.content_sha256 != original.content_sha256
    assert models[-1]['request_hash'] == readonly_map(foreign.content[ROUND_INTENT])['request_hash']
    assert forged.state['tool_ledger'] == whole.state['tool_ledger']
    with pytest.raises(ValueError):
        _known_batch(context, ledger(forged.state['model_ledger']), ledger(forged.state['tool_ledger']),
                     len(ledger(base.state['model_ledger'])), len(ledger(base.state['tool_ledger'])), artifacts)
    if scope == 'actor':
        assert _progress(context, forged, artifacts) == {}
    else:
        with pytest.raises(ValueError):
            _progress(context, forged, artifacts)
