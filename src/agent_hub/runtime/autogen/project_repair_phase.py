"""Auditable same-scope repair boundaries; never permission or budget grants."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import cast
from uuid import UUID

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.autogen.engineering_delivery import (
    engineering_progress,
    engineering_work_progress,
)
from agent_hub.runtime.autogen.model_tool_correction import (
    TOOL_CORRECTION_POLICY,
    validate_rejected_model_rows,
)
from agent_hub.runtime.contracts import (
    Artifact,
    JsonValue,
    RuntimeCheckpoint,
    TaskContext,
    _mutable_json,
)

PHASE_KEY = 'project_repair_phase'
_FIELDS = frozenset({
    'version', 'phase', 'source_checkpoint_id', 'source_checkpoint_sha256', 'source_scope_sha256',
    'source_model_count', 'source_model_sha256', 'source_tool_count', 'source_tool_sha256',
    'model_floor', 'failure_tool_position', 'failure_artifact_id', 'failure_artifact_sha256',
    'failure_fingerprint', 'strategy', 'attempt',
    'source_checkpoint_metadata',
})
_BOOKKEEPING = frozenset({
    'source', 'approval_kind', 'decision_token', 'repair_proposal',
    'self_repair_accepted', 'self_repair_attempt', 'self_repair_max_attempts',
    'self_repair_source_run_id', 'self_repair_source_event_sequence',
    'self_repair_recovery_baseline_sequence', 'self_repair_fingerprint',
    'self_repair_decision_token_hash', 'self_repair_context', 'self_repair_context_history',
    'self_repair_episode_progress',
})
_PROGRESS_FIELDS = frozenset({'checkpoint_sha256', 'checkpoint_id', 'checkpoint_metadata',
    'tool_count', 'model_count', 'progress_sha256', 'usage', 'remaining_timeout_seconds',
    'remaining_absolute_timeout_seconds', 'failure_fingerprint', 'source_checkpoint_sha256',
    'strategy', 'attempt'})


def authority_digest(route: Mapping[str, object]) -> str:
    # Unknown/new route fields are bound by default, never implicitly treated as bookkeeping.
    return hashlib.sha256(json.dumps(_mutable_json(cast(Mapping[str, JsonValue], {k: v for k, v in route.items()
        if k not in _BOOKKEEPING})), sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def stopped_models(rows: Sequence[Mapping[str, JsonValue]], context: TaskContext) -> bool:
    enabled = (type(context.routing_decision.get(TOOL_CORRECTION_POLICY)) is int
               and context.routing_decision.get(TOOL_CORRECTION_POLICY) == 1)
    validate_rejected_model_rows(rows, run_id=context.run_id, tenant_id=context.tenant_id,
                                 repair_enabled=enabled)
    return all(row.get('status') == 'succeeded' or (
        row.get('status') == 'rejected' and enabled and i + 1 < len(rows)
        and rows[i + 1].get('status') == 'succeeded'
        and rows[i + 1].get('correction_for') == i) for i, row in enumerate(rows))


def prefix_digest(rows: Sequence[Mapping[str, JsonValue]]) -> str:
    return hashlib.sha256(json.dumps(_mutable_json(tuple(rows)), ensure_ascii=False,
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def _need(value: object) -> None:
    if not value:
        raise ValueError('project repair phase is invalid')


def selected(context: TaskContext) -> bool:
    route = context.routing_decision
    return (context.mode is TaskMode.DISCUSS
        and type(route.get('engineering_auto_repair_version')) is int
        and route.get('engineering_auto_repair_version') == 1
        and route.get('source') == 'self_repair' and route.get('self_repair_accepted') is True)


def _proposal(context: TaskContext) -> Mapping[str, JsonValue]:
    route = context.routing_decision
    proposal = route.get('repair_proposal')
    _need(selected(context) and isinstance(proposal, Mapping))
    assert isinstance(proposal, Mapping)
    _need(proposal.get('kind') == 'self_repair' and proposal.get('requires_approval') is False
        and proposal.get('automatic_execution') is True
        and proposal.get('source_run_id') == str(context.run_id)
        and proposal.get('failure_kind') in {'runtime_failure', 'step_failure', 'tool_failure'}
        and _digest(proposal.get('automatic_checkpoint_sha256'))
        and _digest(proposal.get('automatic_scope_sha256'))
        and _digest(proposal.get('fingerprint'))
        and type(proposal.get('attempt')) is int and type(route.get('self_repair_attempt')) is int
        and proposal.get('attempt') == route.get('self_repair_attempt')
        and route.get('self_repair_source_run_id') == str(context.run_id)
        and type(route.get('self_repair_max_attempts')) is int
        and route.get('self_repair_max_attempts') == proposal.get('max_attempts'))
    _need(proposal['automatic_scope_sha256'] == authority_digest(route))
    return proposal


def prepare(
    context: TaskContext, checkpoint: RuntimeCheckpoint,
    models: Sequence[Mapping[str, JsonValue]], tools: Sequence[Mapping[str, JsonValue]],
) -> dict[str, JsonValue]:
    proposal = _proposal(context)
    _need(checkpoint.run_id == context.run_id and checkpoint.tenant_id == context.tenant_id
        and checkpoint.mode is context.mode and checkpoint.state.get('terminal') is False
        and PHASE_KEY not in checkpoint.state and 'approval_batch' not in checkpoint.state
        and checkpoint.state_sha256 == checkpoint.recompute_state_sha256()
        and checkpoint.state_sha256 == proposal['automatic_checkpoint_sha256']
        and stopped_models(models, context)
        and tools and all(row.get('status') in {'succeeded', 'failed'} for row in tools))
    failure = next((i for i in reversed(range(len(tools))) if tools[i].get('status') == 'failed'), None)
    row = tools[failure] if failure is not None else {}
    return {
        'version': 1, 'phase': 'active', 'source_checkpoint_id': str(checkpoint.id),
        'source_checkpoint_sha256': checkpoint.state_sha256,
        'source_scope_sha256': proposal['automatic_scope_sha256'],
        'source_checkpoint_metadata': {
            key: value for key, value in checkpoint.state.items() if key not in {'model_ledger', 'tool_ledger'}
        },
        'source_model_count': len(models), 'source_model_sha256': prefix_digest(models),
        'source_tool_count': len(tools), 'source_tool_sha256': prefix_digest(tools),
        'model_floor': len(models), 'failure_tool_position': failure,
        'failure_artifact_id': row.get('artifact_id'), 'failure_artifact_sha256': row.get('artifact_sha256'),
        'failure_fingerprint': proposal['fingerprint'], 'strategy': 'same_scope_diagnose_and_reverify',
        'attempt': proposal['attempt'],
    }


def validate(
    value: object, *, context: TaskContext,
    models: Sequence[Mapping[str, JsonValue]], tools: Sequence[Mapping[str, JsonValue]],
    tool_prefix: int, state: Mapping[str, JsonValue], artifacts: Sequence[Artifact] | None = None,
) -> dict[str, JsonValue]:
    proposal = _proposal(context)
    _need(isinstance(value, Mapping) and set(value) == _FIELDS)
    assert isinstance(value, Mapping)
    _need(type(value['version']) is int and value['version'] == 1 and value['phase'] == 'active'
        and value['source_checkpoint_sha256'] == proposal['automatic_checkpoint_sha256']
        and value['source_scope_sha256'] == proposal['automatic_scope_sha256']
        and value['failure_fingerprint'] == proposal['fingerprint']
        and value['strategy'] == 'same_scope_diagnose_and_reverify'
        and type(value['attempt']) is int and value['attempt'] == proposal['attempt'])
    _need(isinstance(value['source_checkpoint_id'], str))
    _need(str(UUID(value['source_checkpoint_id'])) == value['source_checkpoint_id'])
    model_count, tool_count, floor = (value[k] for k in ('source_model_count', 'source_tool_count', 'model_floor'))
    _need(type(model_count) is int and type(tool_count) is int and type(floor) is int)
    assert isinstance(model_count, int) and isinstance(tool_count, int) and isinstance(floor, int)
    _need(0 <= model_count <= floor <= len(models) and 0 < tool_count <= tool_prefix <= len(tools)
        and value['source_model_sha256'] == prefix_digest(models[:model_count])
        and value['source_tool_sha256'] == prefix_digest(tools[:tool_count])
        and stopped_models(models[:floor], context)
        and all(row.get('status') in {'succeeded', 'failed'} for row in tools[:tool_prefix]))
    metadata = value['source_checkpoint_metadata']
    _need(isinstance(metadata, Mapping)
        and not {'model_ledger', 'tool_ledger', PHASE_KEY, 'approval_batch'} & metadata.keys())
    source = RuntimeCheckpoint(id=UUID(value['source_checkpoint_id']), runtime_type='autogen',
        runtime_version='2', run_id=context.run_id, tenant_id=context.tenant_id, mode=context.mode,
        state={**metadata, 'model_ledger': tuple(models[:model_count]), 'tool_ledger': tuple(tools[:tool_count])})
    _need(source.state_sha256 == value['source_checkpoint_sha256'])
    _budget_not_replenished(metadata, state,
        soft_progress=_soft_progress(context, state, metadata, tools, tool_count, artifacts))
    progress = context.routing_decision.get('self_repair_episode_progress')
    if progress is not None:
        _need(isinstance(progress, Mapping) and set(progress) == _PROGRESS_FIELDS)
        assert isinstance(progress, Mapping)
        _need(progress['failure_fingerprint'] == value['failure_fingerprint']
            and progress['source_checkpoint_sha256'] == value['source_checkpoint_sha256']
            and progress['strategy'] == value['strategy'] and progress['attempt'] == value['attempt'])
        pm, pt, metadata = progress['model_count'], progress['tool_count'], progress['checkpoint_metadata']
        _need(type(pm) is int and type(pt) is int and isinstance(metadata, Mapping))
        assert isinstance(pm, int) and isinstance(pt, int) and isinstance(metadata, Mapping)
        _need(model_count <= pm <= len(models) and tool_count <= pt <= len(tools)
              and not {'model_ledger', 'tool_ledger'} & metadata.keys())
        bound = RuntimeCheckpoint(id=UUID(cast(str, progress['checkpoint_id'])), runtime_type='autogen',
            runtime_version='2', run_id=context.run_id, tenant_id=context.tenant_id, mode=context.mode,
            state={**metadata, 'model_ledger': tuple(models[:pm]), 'tool_ledger': tuple(tools[:pt])})
        _need(bound.state_sha256 == progress['checkpoint_sha256']
            and progress['usage'] == metadata.get('usage')
            and progress['remaining_timeout_seconds'] == metadata.get('remaining_timeout_seconds')
            and progress['remaining_absolute_timeout_seconds'] == metadata.get('remaining_absolute_timeout_seconds'))
        _budget_not_replenished(progress, state,
            soft_progress=_soft_progress(context, state, progress, tools,
                cast(int, progress.get('tool_count')), artifacts))
    failure = value['failure_tool_position']
    if failure is None:
        _need(value['failure_artifact_id'] is None and value['failure_artifact_sha256'] is None
            and all(row.get('status') != 'failed' for row in tools[:tool_count]))
    else:
        _need(type(failure) is int and 0 <= failure < tool_count)
        row = tools[failure]
        _need(row.get('status') == 'failed' and row.get('artifact_id') == value['failure_artifact_id']
            and row.get('artifact_sha256') == value['failure_artifact_sha256'])
    return cast(dict[str, JsonValue], dict(value))


def _soft_progress(context: TaskContext, state: Mapping[str, JsonValue], baseline: Mapping[str, JsonValue],
                   tools: Sequence[Mapping[str, JsonValue]], lower: int,
                   artifacts: Sequence[Artifact] | None) -> bool:
    route = context.routing_decision
    if (type(lower) is not int or not 0 <= lower < len(tools)
            or route.get('runtime_timeout_source') != 'project_scale_soft_budget'
            or any(type(route.get(k)) is not int or route[k] != 1 or state.get(k) != 1 for k in (
                'engineering_progress_budget_version', 'engineering_checkpoint_ack_version',
                'engineering_soft_admission_version'))):
        return False
    soft, absolute, runway = (state.get('remaining_timeout_seconds'),
        state.get('remaining_absolute_timeout_seconds'), route.get('runtime_timeout_soft_seconds'))
    if any(type(v) not in (int, float) for v in (soft, absolute, runway)):
        return False
    if not 0 < cast(float, soft) <= min(cast(float, absolute), cast(float, runway)):
        return False
    # Structural validation precedes private hydration. It cannot itself establish work credit.
    if not any(row.get('status') in {'succeeded', 'failed'} and row.get('name') in {
            'workspace.write_text', 'project.verify', 'workspace.bundle'} for row in tools[lower:]):
        return False
    if artifacts is None:
        return True
    allowed: dict[str, tuple[str, ...]] = {}
    for row in tools:
        actor, name = row.get('actor'), row.get('name')
        if isinstance(actor, str) and isinstance(name, str):
            allowed[actor] = (*allowed.get(actor, ()), name)
    before = engineering_work_progress(context, tool_entries=tools[:lower], artifacts=artifacts,
        allowed_tools=allowed, include_draft_diagnoses=True)
    after = engineering_work_progress(context, tool_entries=tools, artifacts=artifacts,
        allowed_tools=allowed, include_draft_diagnoses=True)
    return bool(after - before)


def _budget_not_replenished(metadata: Mapping[str, JsonValue], state: Mapping[str, JsonValue],
                            *, soft_progress: bool = False) -> None:
    old_usage, usage = metadata.get('usage'), state.get('usage')
    _need(isinstance(old_usage, Mapping) and isinstance(usage, Mapping))
    assert isinstance(old_usage, Mapping) and isinstance(usage, Mapping)
    _need(type(usage.get('tokens')) is int and type(old_usage.get('tokens')) is int
          and cast(int, usage['tokens']) >= cast(int, old_usage['tokens']))
    old_cost, cost = Decimal(str(old_usage.get('cost_usd'))), Decimal(str(usage.get('cost_usd')))
    _need(old_cost.is_finite() and cost.is_finite() and 0 <= old_cost <= cost)
    for key in ('remaining_timeout_seconds', 'remaining_absolute_timeout_seconds'):
        old, current = metadata.get(key), state.get(key)
        _need(type(old) in (int, float) and type(current) in (int, float))
        assert isinstance(old, int | float) and isinstance(current, int | float)
        _need(math.isfinite(old) and math.isfinite(current) and 0 <= current
              and (current <= old or key == 'remaining_timeout_seconds' and soft_progress))


def productive_boundary(context: TaskContext, checkpoint: RuntimeCheckpoint,
                        artifacts: Sequence[Artifact]) -> dict[str, JsonValue]:
    """New confirmed write or delivery progress may continue an episode, never UNKNOWN."""
    state = checkpoint.state
    _need(checkpoint.runtime_type == 'autogen' and checkpoint.runtime_version == '2'
        and checkpoint.state_sha256 == checkpoint.recompute_state_sha256()
        and state.get('terminal') is False and 'approval_batch' not in state
        and checkpoint.run_id == context.run_id and checkpoint.tenant_id == context.tenant_id
        and checkpoint.mode is context.mode)
    models, tools = state.get('model_ledger'), state.get('tool_ledger')
    _need(isinstance(models, tuple) and isinstance(tools, tuple)
          and all(isinstance(row, Mapping) for row in (*models, *tools)))
    models = cast(tuple[Mapping[str, JsonValue], ...], models)
    tools = cast(tuple[Mapping[str, JsonValue], ...], tools)
    phase = validate(state.get(PHASE_KEY), context=context, models=models, tools=tools,
                     tool_prefix=cast(int, state.get('engineering_tool_prefix', 0)), state=state, artifacts=artifacts)
    _need(stopped_models(models, context)
          and all(row.get('status') in {'succeeded', 'failed'} for row in tools))
    route, usage = context.routing_decision, state['usage']
    _need(isinstance(usage, Mapping))
    assert isinstance(usage, Mapping)
    tokens, hard = route.get('runtime_plan_token_budget'), route.get('runtime_token_absolute_tokens')
    _need(type(tokens) is int and type(hard) is int and type(usage['tokens']) is int)
    assert isinstance(tokens, int) and isinstance(hard, int) and isinstance(usage['tokens'], int)
    _need(0 <= usage['tokens'] < tokens <= hard
          and cast(float, state['remaining_timeout_seconds']) > 0
          and cast(float, state['remaining_absolute_timeout_seconds']) > 0)
    previous = route.get('self_repair_episode_progress')
    lower = phase['source_tool_count'] if previous is None else previous.get('tool_count') if isinstance(previous, Mapping) else None
    _need(type(lower) is int and cast(int, phase['source_tool_count']) <= lower < len(tools))
    assert isinstance(lower, int)
    by_id = {str(a.id): a for a in artifacts}
    _need(len(by_id) == len(artifacts))
    observed: set[tuple[str, str]] = set()
    productive: list[Mapping[str, JsonValue]] = []
    for position, row in enumerate(tools):
        if row.get('name') != 'workspace.write_text' or row.get('status') != 'succeeded':
            continue
        artifact = by_id.get(cast(str, row.get('artifact_id')))
        _need(artifact is not None)
        assert artifact is not None
        _need(artifact.content_sha256 == row.get('artifact_sha256')
            and artifact.content_sha256 == artifact.recompute_content_sha256()
            and artifact.type == 'tool_result' and artifact.producer == row.get('actor')
            and artifact.content.get('tool_name') == row['name']
            and artifact.content.get('arguments_sha256') == row.get('arguments_hash')
            and type(row.get('position')) is int and row['position'] == position)
        _need(row.get('idempotency_key') == hashlib.sha256(
            f"{context.run_id}:{row['actor']}:{position}:{row['name']}:{row['arguments_hash']}".encode()).hexdigest())
        result = artifact.content.get('result')
        _need(isinstance(result, Mapping))
        assert isinstance(result, Mapping)
        files = result.get('workspace_files')
        _need(isinstance(files, tuple) and bool(files))
        assert isinstance(files, tuple)
        for file in files:
            _need(isinstance(file, Mapping))
            assert isinstance(file, Mapping)
            path, digest, size = file.get('path'), file.get('sha256'), file.get('size_bytes')
            _need(isinstance(path, str) and bool(path) and _digest(digest) and type(size) is int and size >= 0)
            pair = (cast(str, path), cast(str, digest))
            if position >= lower and pair not in observed:
                productive.append(file)
            observed.add(pair)
    if not productive:
        _need(phase['model_floor'] == len(models)
              and state.get('engineering_tool_prefix') == len(tools))
        allowed: dict[str, tuple[str, ...]] = {}
        for row in tools:
            actor, name = row.get('actor'), row.get('name')
            if isinstance(actor, str) and isinstance(name, str):
                allowed[actor] = (*allowed.get(actor, ()), name)
        current = engineering_progress(context, tool_entries=tools, artifacts=artifacts,
                                       allowed_tools=allowed)
        latest = next((row for row in reversed(tools) if row.get('name') in {
            'workspace.write_text', 'project.verify', 'workspace.bundle'}), {})
        name = latest.get('name')
        _need(not current.uncertain and latest.get('status') == 'succeeded' and (
            name == 'project.verify' and current.verification_current
            or name == 'workspace.bundle' and current.bundle_current))
        before = engineering_work_progress(context, tool_entries=tools[:lower], artifacts=artifacts,
                                           allowed_tools=allowed)
        after = engineering_work_progress(context, tool_entries=tools, artifacts=artifacts,
                                          allowed_tools=allowed)
        # Attempt ids and old phase receipts cannot buy another continuation.
        productive.extend({'work_progress': key} for key in sorted(after - before)
                          if key.startswith(f'{name}:'))
    _need(bool(productive))
    return {'checkpoint_sha256': checkpoint.state_sha256, 'checkpoint_id': str(checkpoint.id),
            'checkpoint_metadata': cast(JsonValue, _mutable_json({k: v for k, v in state.items()
                if k not in {'model_ledger', 'tool_ledger'}})), 'tool_count': len(tools),
            'model_count': len(models), 'progress_sha256': prefix_digest(productive),
            'failure_fingerprint': phase['failure_fingerprint'], 'strategy': phase['strategy'],
            'attempt': phase['attempt'], 'source_checkpoint_sha256': phase['source_checkpoint_sha256'],
            'usage': dict(usage), 'remaining_timeout_seconds': state['remaining_timeout_seconds'],
            'remaining_absolute_timeout_seconds': state['remaining_absolute_timeout_seconds']}
