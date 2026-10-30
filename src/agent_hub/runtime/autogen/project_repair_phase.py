"""Auditable same-scope repair boundaries; never authority or hard-budget grants."""
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
from agent_hub.runtime.execution_failure import execution_failure_result
from agent_hub.runtime.generated_draft_feedback import generated_draft_artifact_feedback
from agent_hub.runtime.progress import workspace_progress_key

PHASE_KEY = 'project_repair_phase'
ROUND_INTENT = 'project_repair_round'
DIAGNOSTIC_POLICY = 'engineering_diagnostic_allowance_version'
DIAGNOSTIC_WATERMARK = 'engineering_diagnostic_consumed_through'
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


def _matches_authority(context: TaskContext, expected: JsonValue) -> bool:
    from agent_hub.runtime.repair_projection import _matches_projection

    route = context.routing_decision
    if authority_digest(route) == expected:
        return True
    # A projected route is data from the trusted current worker, not a second
    # public-route candidate or a grant. No token-only fallback after revocation.
    return _matches_projection(context, expected)


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
    _need(_matches_authority(context, proposal['automatic_scope_sha256']))
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
    diagnostic_candidate = (type(route.get(DIAGNOSTIC_POLICY)) is int and route.get(DIAGNOSTIC_POLICY) == 1
        and state.get(DIAGNOSTIC_POLICY) == 1 and any(row.get('status') == 'succeeded'
        and row.get('name') in {'workspace.read', 'workspace_read'} for row in tools[lower:]))
    if not diagnostic_candidate and not any(row.get('status') in {'succeeded', 'failed'} and row.get('name') in {
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
    if after - before:
        return True
    if diagnostic_candidate:
        phase, models = state.get(PHASE_KEY), state.get('model_ledger')
        if isinstance(phase, Mapping) and isinstance(models, tuple):
            keys = diagnostic_progress(context, phase=phase,
                models=cast(Sequence[Mapping[str, JsonValue]], models), tools=tools, artifacts=artifacts)
            return any(boundary > lower for boundary in keys.values())
    return False


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


def _round_model(run_id: UUID, tenant_id: UUID, row: Mapping[str, JsonValue], position: int,
                 prefix: int, by_id: Mapping[str, Artifact]) -> tuple[Artifact, Mapping[str, JsonValue]]:
    _need(row.get('status') == 'succeeded' and 'report_binding' not in row)
    model = by_id.get(cast(str, row.get('artifact_id')))
    _need(model is not None)
    assert model is not None
    _need(model.type == 'model_result' and model.producer == 'agent_hub'
          and model.content_sha256 == row.get('artifact_sha256')
          and model.recompute_content_sha256() == model.content_sha256)
    content, binding = model.content, model.content.get(ROUND_INTENT)
    _need(isinstance(binding, Mapping) and set(binding) == {
        'version', 'run_id', 'tenant_id', 'actor', 'request_hash', 'model_position',
        'tool_start', 'logical_model', 'tool_names'})
    assert isinstance(binding, Mapping)
    _need(type(binding['version']) is int and binding['version'] == 1
          and binding['run_id'] == str(run_id) and binding['tenant_id'] == str(tenant_id)
          and _digest(row.get('request_hash')) and binding['request_hash'] == row['request_hash']
          and type(binding['model_position']) is int and binding['model_position'] == position
          and type(binding['tool_start']) is int and binding['tool_start'] == prefix
          and type(content.get('fallback_used', False)) is bool)
    origin = content.get('fallback_from_logical_model') if content.get('fallback_used') is True else content.get('logical_model')
    _need(binding['logical_model'] == origin and all(isinstance(content.get(key), str)
          and cast(str, content[key]).strip() for key in ('deployment_id', 'logical_model', 'provider_id', 'provider_model')))
    actor, names = binding['actor'], binding['tool_names']
    _need(isinstance(actor, str) and (actor == '__selector__'
          or re.fullmatch('[a-z0-9][a-z0-9_.-]{0,127}', actor)) and isinstance(names, Mapping))
    assert isinstance(names, Mapping)
    canonical: dict[str, str] = {}
    for name in names.values():
        _need(isinstance(name, str) and re.fullmatch('[A-Za-z0-9_.-]{1,128}', name))
        assert isinstance(name, str)
        alias = name.replace('.', '_')
        if len(alias) > 64:
            alias = alias[:47] + '_' + hashlib.sha256(name.encode()).hexdigest()[:16]
        for key in {name, alias}:
            _need(key not in canonical or canonical[key] == name)
            canonical[key] = name
    _need(dict(names) == canonical and (actor != '__selector__'
          or not names and content.get('tool_calls') == ()))
    usage, cost = content.get('usage'), Decimal(str(content.get('cost_usd')))
    _need(isinstance(usage, Mapping) and set(usage) == {'prompt_tokens', 'completion_tokens', 'total_tokens'}
          and all(type(value) is int and value >= 0 for value in usage.values())
          and usage['total_tokens'] == cast(int, usage['prompt_tokens']) + cast(int, usage['completion_tokens'])
          and cost.is_finite() and cost >= 0)
    return model, binding


def pending_round_intents(*, run_id: UUID, tenant_id: UUID,
                          models: Sequence[Mapping[str, JsonValue]], floor: int, prefix: int,
                          artifacts: Sequence[Artifact]) -> tuple[Mapping[str, JsonValue] | None, ...]:
    """Legacy receipts lack intent; present intent must still validate in every row."""
    _need(type(floor) is int and 0 <= floor <= len(models))
    by_id = {str(item.id): item for item in artifacts}
    _need(len(by_id) == len(artifacts))
    intents: list[Mapping[str, JsonValue] | None] = []
    for position, row in enumerate(models[floor:], floor):
        if row.get('status') != 'succeeded':
            continue
        model = by_id.get(cast(str, row.get('artifact_id')))
        _need(model is not None)
        assert model is not None
        _need(model.type == 'model_result' and model.producer == 'agent_hub'
              and model.content_sha256 == row.get('artifact_sha256')
              and model.recompute_content_sha256() == model.content_sha256)
        intents.append(None if ROUND_INTENT not in model.content else
            _round_model(run_id, tenant_id, row, position, prefix, by_id)[1])
    return tuple(intents)


def completed_round(context: TaskContext, state: Mapping[str, JsonValue],
                    artifacts: Sequence[Artifact]) -> bool:
    """Prove a whole acknowledged batch; never infer intent from legacy effects."""
    models, tools, phase = state.get('model_ledger'), state.get('tool_ledger'), state.get(PHASE_KEY)
    _need(isinstance(models, tuple) and isinstance(tools, tuple) and isinstance(phase, Mapping)
          and 'approval_batch' not in state)
    assert isinstance(models, tuple) and isinstance(tools, tuple) and isinstance(phase, Mapping)
    floor, prefix = phase.get('model_floor'), state.get('engineering_tool_prefix')
    _need(type(floor) is int and type(prefix) is int)
    assert isinstance(floor, int) and isinstance(prefix, int)
    if floor == len(models) and prefix == len(tools):
        return False
    validate(phase, context=context, models=cast(Sequence[Mapping[str, JsonValue]], models),
             tools=cast(Sequence[Mapping[str, JsonValue]], tools), tool_prefix=prefix,
             state=state, artifacts=artifacts)
    return _known_batch(context, cast(Sequence[Mapping[str, JsonValue]], models),
                        cast(Sequence[Mapping[str, JsonValue]], tools), floor, prefix, artifacts)


def _known_batch(context: TaskContext, models: Sequence[Mapping[str, JsonValue]],
                 tools: Sequence[Mapping[str, JsonValue]], floor: int, prefix: int,
                 artifacts: Sequence[Artifact]) -> bool:
    _need(stopped_models(models, context)
          and all(isinstance(row, Mapping) and row.get('status') in {'succeeded', 'failed'} for row in tools)
          and 0 <= prefix < len(tools) and 0 <= floor < len(models))
    pending = models[floor:]
    statuses = [row.get('status') if isinstance(row, Mapping) else None for row in pending]
    _need(bool(statuses) and statuses[-1] == 'succeeded' and (
        all(status == 'succeeded' for status in statuses[:-1])
        or len(statuses) >= 2 and statuses[-2] == 'rejected'
            and all(status == 'succeeded' for status in statuses[:-2])))
    by_id = {str(item.id): item for item in artifacts}
    _need(len(by_id) == len(artifacts))
    bound = [_round_model(context.run_id, context.tenant_id, row, floor + offset, prefix, by_id)
             for offset, row in enumerate(pending) if isinstance(row, Mapping) and row.get('status') == 'succeeded']
    model, binding = bound[-1]
    content = model.content
    actor, names = binding['actor'], binding['tool_names']
    _need(isinstance(actor, str) and re.fullmatch('[a-z0-9][a-z0-9_.-]{0,127}', actor)
          and isinstance(names, Mapping) and bool(names))
    assert isinstance(names, Mapping)
    for selection, selection_binding in bound[:-1]:
        _need(selection_binding['actor'] == '__selector__' and selection_binding['tool_names'] == {}
              and selection.content.get('tool_calls') == () and isinstance(selection.content.get('text'), str))
    if len(bound) > 1:
        _need(cast(str, bound[-2][0].content['text']).strip() == actor)
    calls = content.get('tool_calls')
    _need(isinstance(calls, tuple) and len(calls) == len(tools) - prefix)
    assert isinstance(calls, tuple)
    identifiers: set[str] = set()
    used: set[str] = set()
    for offset, call in enumerate(calls):
        _need(isinstance(call, Mapping) and set(call) == {'id', 'name', 'arguments'})
        assert isinstance(call, Mapping)
        identifier, arguments = call['id'], call['arguments']
        _need(isinstance(identifier, str) and re.fullmatch('[A-Za-z0-9_.:-]{1,128}', identifier)
              and identifier not in identifiers and isinstance(arguments, Mapping))
        identifiers.add(cast(str, identifier))
        name = names.get(cast(str, call['name']))
        current = tools[prefix + offset]
        assert isinstance(current, Mapping)
        digest = hashlib.sha256(json.dumps(_mutable_json(arguments), ensure_ascii=False,
            allow_nan=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        _need(current.get('status') == 'succeeded' and current.get('actor') == actor
              and isinstance(name, str) and current.get('name') == name
              and type(current.get('position')) is int and current['position'] == prefix + offset
              and current.get('arguments_hash') == digest
              and current.get('idempotency_key') == hashlib.sha256(
                  f'{context.run_id}:{actor}:{prefix + offset}:{name}:{digest}'.encode()).hexdigest())
        artifact = by_id.get(cast(str, current.get('artifact_id')))
        _need(artifact is not None and str(artifact.id) not in used)
        assert artifact is not None
        used.add(str(artifact.id))
        _need(artifact.type == 'tool_result' and artifact.producer == actor
              and artifact.content_sha256 == current.get('artifact_sha256')
              and artifact.recompute_content_sha256() == artifact.content_sha256
              and artifact.content.get('tool_name') == name
              and artifact.content.get('arguments_sha256') == digest
              and isinstance(artifact.content.get('result'), Mapping))
    return True


def diagnostic_progress(
    context: TaskContext, *, phase: Mapping[str, JsonValue] | None,
    models: Sequence[Mapping[str, JsonValue]], tools: Sequence[Mapping[str, JsonValue]],
    artifacts: Sequence[Artifact],
) -> dict[str, int]:
    """One complete novel observation per failed-source/work epoch, never delivery credit."""
    version = context.routing_decision.get(DIAGNOSTIC_POLICY)
    if type(version) is not int or version != 1 or phase is None:
        return {}
    _need(selected(context) and isinstance(phase, Mapping))
    source, position = phase.get('source_tool_count'), phase.get('failure_tool_position')
    _need(type(source) is int and 0 < source <= len(tools))
    assert isinstance(source, int)
    if position is None:
        return {}
    _need(type(position) is int and 0 <= position < source)
    assert isinstance(position, int)
    by_id = {str(item.id): item for item in artifacts}
    _need(len(by_id) == len(artifacts))
    failure_row = tools[position]
    failure = by_id.get(cast(str, failure_row.get('artifact_id')))
    _need(failure is not None and failure_row.get('status') == 'failed'
          and failure_row.get('artifact_id') == phase.get('failure_artifact_id')
          and failure_row.get('artifact_sha256') == phase.get('failure_artifact_sha256'))
    assert failure is not None
    result = failure.content.get('result')
    _need(isinstance(result, Mapping) and (
        execution_failure_result(result, tool_name=failure_row.get('name')) == result
        or generated_draft_artifact_feedback(failure, context=context, actor=failure.producer) == result))
    allowed: dict[str, tuple[str, ...]] = {}
    for row in tools:
        actor, name = row.get('actor'), row.get('name')
        if isinstance(actor, str) and isinstance(name, str):
            allowed[actor] = (*allowed.get(actor, ()), name)
    end = next((index for index, row in enumerate(tools)
                if row.get('status') not in {'succeeded', 'failed'}), len(tools))
    _need(not engineering_progress(context, tool_entries=tools[:end], artifacts=artifacts,
                                   allowed_tools=allowed).uncertain)
    seen: set[str] = set()
    credits: dict[str, int] = {}
    readonly = {'read_context', 'project.inspect', 'workspace.read', 'workspace_read',
                'workspace.list', 'workspace.search'}
    for index, row in enumerate(tools[:end]):
        if row.get('status') != 'succeeded' or row.get('name') not in {'workspace.read', 'workspace_read'}:
            continue
        artifact = by_id[cast(str, row['artifact_id'])]
        result = artifact.content['result']
        assert isinstance(result, Mapping)
        path, text = result.get('path'), result.get('text')
        if (not isinstance(path, str) or not path or path.startswith('/') or '\\' in path
                or ':' in path or any(part in {'', '.', '..'} for part in path.split('/'))
                or not isinstance(text, str) or result.get('truncated') is not False):
            continue
        semantic = workspace_progress_key(cast(str, row['name']), '', result)
        if semantic is None or semantic in seen:
            continue
        seen.add(semantic)
        if index < source:
            continue
        for model_position, model_row in enumerate(models):
            if model_row.get('status') != 'succeeded':
                continue
            model = by_id.get(cast(str, model_row.get('artifact_id')))
            binding = model.content.get(ROUND_INTENT) if model is not None else None
            if not isinstance(binding, Mapping) or binding.get('actor') != row.get('actor'):
                continue
            start, calls = binding.get('tool_start'), model.content.get('tool_calls') if model is not None else None
            if type(start) is not int or not isinstance(calls, tuple) or not start <= index < start + len(calls):
                continue
            stop = start + len(calls)
            if start < source or stop > end or any(item.get('name') not in readonly for item in tools[start:stop]):
                continue
            _known_batch(context, models[:model_position + 1], tools[:stop], model_position, start, artifacts)
            work = engineering_work_progress(context, tool_entries=tools[:start], artifacts=artifacts,
                                              allowed_tools=allowed)
            key = prefix_digest([{'tenant_id': str(context.tenant_id), 'run_id': str(context.run_id),
                'scope': phase.get('source_scope_sha256'), 'failure': phase.get('failure_artifact_sha256'),
                'work': tuple(sorted(work))}])
            credits.setdefault(f'diagnostic:{key}', stop)
            break
    return credits


def validate_diagnostic_transition(
    context: TaskContext, previous: RuntimeCheckpoint | None, checkpoint: RuntimeCheckpoint,
    artifacts: Sequence[Artifact],
) -> None:
    """Validate against the committed predecessor, not just the episode's older source."""
    route, state = context.routing_decision, checkpoint.state
    _need(type(route.get(DIAGNOSTIC_POLICY)) is int and route.get(DIAGNOSTIC_POLICY) == 1)
    absolute_limit, initial = route.get('runtime_timeout_absolute_seconds'), route.get('runtime_timeout_seconds')
    token_limit, plan_tokens = route.get('runtime_token_absolute_tokens'), route.get('runtime_plan_token_budget')
    _need(type(absolute_limit) in (int, float) and math.isfinite(cast(float, absolute_limit))
          and cast(float, absolute_limit) > 0 and type(initial) in (int, float)
          and math.isfinite(cast(float, initial)) and 0 < cast(float, initial) <= cast(float, absolute_limit)
          and type(token_limit) is int and type(plan_tokens) is int
          and 0 < plan_tokens <= token_limit)
    def progress(current: RuntimeCheckpoint) -> tuple[set[str], int]:
        value = current.state
        _need(current.run_id == context.run_id and current.tenant_id == context.tenant_id
              and current.mode is context.mode and current.runtime_type == 'autogen'
              and current.runtime_version == '2' and current.state_sha256 == current.recompute_state_sha256()
              and type(value.get(DIAGNOSTIC_POLICY)) is int and value.get(DIAGNOSTIC_POLICY) == 1)
        soft, absolute, usage = (value.get('remaining_timeout_seconds'),
            value.get('remaining_absolute_timeout_seconds'), value.get('usage'))
        _need(type(soft) in (int, float) and type(absolute) in (int, float)
              and math.isfinite(cast(float, soft)) and math.isfinite(cast(float, absolute))
              and 0 <= cast(float, soft) <= cast(float, absolute) <= cast(float, absolute_limit)
              and isinstance(usage, Mapping) and set(usage) == {'tokens', 'cost_usd'})
        assert isinstance(usage, Mapping)
        cost = Decimal(str(usage.get('cost_usd')))
        _need(type(usage.get('tokens')) is int and 0 <= cast(int, usage['tokens']) <= cast(int, token_limit)
              and cost.is_finite() and cost >= 0)
        models, tools, phase = value.get('model_ledger'), value.get('tool_ledger'), value.get(PHASE_KEY)
        _need(isinstance(models, tuple) and isinstance(tools, tuple)
              and all(isinstance(item, Mapping) for item in (*models, *tools))
              and (phase is None or isinstance(phase, Mapping)))
        models = cast(tuple[Mapping[str, JsonValue], ...], models)
        tools = cast(tuple[Mapping[str, JsonValue], ...], tools)
        if phase is not None:
            validate(phase, context=context, models=models, tools=tools,
                tool_prefix=cast(int, value.get('engineering_tool_prefix', 0)), state=value, artifacts=artifacts)
        diagnostic = diagnostic_progress(context, phase=cast(Mapping[str, JsonValue] | None, phase),
            models=models, tools=tools, artifacts=artifacts)
        watermark = max(diagnostic.values(), default=0)
        _need(type(value.get(DIAGNOSTIC_WATERMARK)) is int and value.get(DIAGNOSTIC_WATERMARK) == watermark)
        allowed: dict[str, tuple[str, ...]] = {}
        for row in tools:
            actor, name = row.get('actor'), row.get('name')
            if isinstance(actor, str) and isinstance(name, str):
                allowed[actor] = (*allowed.get(actor, ()), name)
        work = engineering_work_progress(context, tool_entries=tools, artifacts=artifacts,
            allowed_tools=allowed, include_draft_diagnoses=True, include_bootstrap_observations=True)
        return set(work) | set(diagnostic), watermark
    keys, watermark = progress(checkpoint)
    if previous is None:
        _need(watermark == 0 and cast(float, state['remaining_timeout_seconds']) <= cast(float, initial))
        return
    before, old_watermark = progress(previous)
    _need(watermark >= old_watermark and keys >= before)
    _budget_not_replenished(previous.state, state, soft_progress=bool(keys - before))
    old_abs, absolute = (cast(float, item['remaining_absolute_timeout_seconds']) for item in (previous.state, state))
    old_soft, soft = (cast(float, item['remaining_timeout_seconds']) for item in (previous.state, state))
    decayed = max(0.0, old_soft - (old_abs - absolute))
    runway = route.get('runtime_timeout_soft_seconds')
    _need(type(runway) in (int, float) and math.isfinite(cast(float, runway)) and cast(float, runway) > 0)
    maximum = max(decayed, min(absolute, cast(float, runway))) if keys - before else decayed
    _need(soft <= maximum + 1e-6)


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
        _need(phase['model_floor'] == len(models) and state.get('engineering_tool_prefix') == len(tools)
              or completed_round(context, state, artifacts))
        allowed: dict[str, tuple[str, ...]] = {}
        for row in tools:
            actor, name = row.get('actor'), row.get('name')
            if isinstance(actor, str) and isinstance(name, str):
                allowed[actor] = (*allowed.get(actor, ()), name)
        diagnostic = diagnostic_progress(context, phase=phase, models=models, tools=tools, artifacts=artifacts)
        productive.extend({'diagnostic': key} for key, boundary in diagnostic.items() if boundary > lower)
        current = engineering_progress(context, tool_entries=tools, artifacts=artifacts,
                                       allowed_tools=allowed)
        latest = next((row for row in reversed(tools) if row.get('name') in {
            'workspace.write_text', 'project.verify', 'workspace.bundle'}), {})
        name = latest.get('name')
        _need(bool(productive) or not current.uncertain and latest.get('status') == 'succeeded' and (
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
