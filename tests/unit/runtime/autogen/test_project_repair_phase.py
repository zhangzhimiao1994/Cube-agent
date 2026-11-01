"""A repair boundary commits to the source checkpoint, not just self-reported counts."""
from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from uuid import UUID, uuid4

import pytest

from agent_hub.runs.service import RunService
from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.autogen.engineering_delivery import engineering_progress
from agent_hub.runtime.autogen.project_repair_phase import (
    ROUND_INTENT,
    completed_round,
    prefix_digest,
    prepare,
    productive_boundary,
    validate,
)
from agent_hub.runtime.contracts import Artifact, JsonValue, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_project_auto_repair_runtime import (
    BoundaryFailure,
    ContinuationRepository,
    ProjectJourney,
)
from tests.unit.runtime.autogen.project_repair_phase_typed_fixture import (
    UnexpectedTaskQueue,
    fixture,
    integer,
    json_map,
    ledger,
    mapping,
    mutable,
    mutable_rows,
    number,
    pair,
    rows,
    sequence,
    string,
)


async def case() -> tuple[
    TaskContext, RuntimeCheckpoint, tuple[Mapping[str, JsonValue], ...],
    tuple[Mapping[str, JsonValue], ...], dict[str, object],
]:
    journey = ProjectJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=UnexpectedTaskQueue())
    await service.execute(repo.run_id)
    saved = repo.checkpoint
    context = journey.context.model_copy(update={'checkpoint': saved, 'routing_decision': repo.row.routing_decision})
    models, tools = ledger(saved.state['model_ledger']), ledger(saved.state['tool_ledger'])
    phase = mutable(prepare(context, saved, models, tools))
    return context, saved, models, tools, phase


@pytest.mark.asyncio
async def test_boundary_reconstructs_original_checkpoint_without_dropping_any_rows() -> None:
    context, saved, models, tools, phase = await case()
    before = saved.to_payload()
    assert validate(phase, context=context, models=ledger(models), tools=ledger(tools), tool_prefix=len(tools), state=saved.state) == phase
    assert phase['model_floor'] == len(models)
    assert phase['source_tool_count'] == len(tools)
    assert phase['failure_tool_position'] == len(tools) - 1
    assert saved.to_payload() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', [
    'source_sha', 'scope_sha', 'source_model_count', 'source_tool_count',
    'source_row_rehash', 'metadata', 'failure_artifact', 'floor', 'unknown_consumed',
    'fingerprint', 'strategy', 'version',
])
async def test_phase_tampering_and_consumed_unknown_cannot_be_restored(defect: str) -> None:
    context, saved, original_models, original_tools, original_phase = await case()
    models, tools = mutable_rows(original_models), mutable_rows(original_tools)
    phase = copy.deepcopy(original_phase)
    if defect == 'source_sha':
        phase['source_checkpoint_sha256'] = 'f' * 64
    elif defect == 'scope_sha':
        phase['source_scope_sha256'] = 'f' * 64
    elif defect == 'source_model_count':
        phase['source_model_count'] = 0
        phase['source_model_sha256'] = prefix_digest([])
    elif defect == 'source_tool_count':
        phase['source_tool_count'] = integer(phase['source_tool_count']) - 1
        phase['source_tool_sha256'] = prefix_digest(ledger(tools[:-1]))
    elif defect == 'source_row_rehash':
        models[0]['request_hash'] = 'e' * 64
        phase['source_model_sha256'] = prefix_digest(ledger(models))
    elif defect == 'metadata':
        mapping(mapping(phase['source_checkpoint_metadata'])['usage'])['tokens'] = 0
    elif defect == 'failure_artifact':
        phase['failure_artifact_sha256'] = 'f' * 64
    elif defect == 'floor':
        phase['model_floor'] = integer(phase['model_floor']) + 1
    elif defect == 'unknown_consumed':
        models.append({'status': 'running', 'request_hash': 'f' * 64})
        phase['model_floor'] = integer(phase['model_floor']) + 1
    elif defect == 'fingerprint':
        phase['failure_fingerprint'] = 'f' * 64
    elif defect == 'strategy':
        phase['strategy'] = 'raise_budget_and_retry'
    elif defect == 'version':
        phase['version'] = True
    with pytest.raises(ValueError):
        validate(phase, context=context, models=ledger(models), tools=ledger(tools), tool_prefix=len(tools), state=saved.state)
    assert integer(json_map(saved.state['usage'])['tokens']) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['permission', 'hard_tokens', 'tokens_reset', 'cost_reset', 'absolute_reset', 'soft_reset'])
async def test_current_authority_and_budget_cannot_diverge_from_source(defect: str) -> None:
    context, saved, models, tools, phase = await case()
    route, state = mutable(context.routing_decision), mutable(saved.state)
    if defect == 'permission':
        route['requested_permissions'] = ['host_write']
    elif defect == 'hard_tokens':
        route['runtime_token_absolute_tokens'] = integer(route['runtime_token_absolute_tokens']) + 1
    elif defect == 'tokens_reset':
        mapping(state['usage'])['tokens'] = 0
    elif defect == 'cost_reset':
        mapping(state['usage'])['cost_usd'] = '0'
    elif defect == 'absolute_reset':
        state['remaining_absolute_timeout_seconds'] = number(state['remaining_absolute_timeout_seconds']) + 1
    else:
        state['remaining_timeout_seconds'] = number(state['remaining_timeout_seconds']) + 1
    context = context.model_copy(update={'routing_decision': route})
    with pytest.raises(ValueError):
        validate(phase, context=context, models=ledger(models), tools=ledger(tools), tool_prefix=len(tools), state=json_map(state))
    from agent_hub.runtime.autogen.adapter import RuntimeExecutionError
    from agent_hub.runtime.contracts import RuntimeCheckpoint
    state['project_repair_phase'] = phase
    checkpoint = RuntimeCheckpoint(id=saved.id, runtime_type='autogen', runtime_version='2',
        run_id=context.run_id, tenant_id=context.tenant_id, mode=context.mode, state=json_map(state))
    with pytest.raises(RuntimeExecutionError, match='project repair phase is invalid'):
        ProjectJourney().runtime()._validate_checkpoint(checkpoint, context)


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['', 'unknown', 'wrong_scope', 'wrong_link', 'wrong_request'])
async def test_phase_retains_fully_bound_completed_model_correction(defect: str) -> None:
    context, saved, original, tools, phase = await case()
    route = mutable(context.routing_decision)
    # The correction policy is part of the source authority, not granted at restore.
    assert route['engineering_tool_correction_version'] == 1
    rows = pair()
    position = len(original)
    receipt = mapping(rows[0]['rejection'])
    receipt.update(run_id=str(context.run_id), tenant_id=str(context.tenant_id), position=position)
    rows[1]['correction_for'] = position
    if defect == 'unknown':
        rows[1]['status'] = 'running'
        rows[1].pop('artifact_id')
        rows[1].pop('artifact_sha256')
    elif defect == 'wrong_scope':
        receipt['run_id'] = receipt['tenant_id']
    elif defect == 'wrong_link':
        rows[1]['correction_for'] = 0
    elif defect == 'wrong_request':
        receipt['request_hash'] = 'd' * 64
    models = [*original, *rows]
    phase['model_floor'] = len(models)
    if defect:
        with pytest.raises(ValueError):
            validate(phase, context=context, models=ledger(models), tools=ledger(tools), tool_prefix=len(tools), state=saved.state)
    else:
        assert validate(phase, context=context, models=ledger(models), tools=ledger(tools), tool_prefix=len(tools), state=saved.state) == phase
        assert models[position]['status'] == 'rejected'


def test_final_report_retains_bound_correction_chain_without_hiding_unknown() -> None:
    evidence = fixture()
    route = {**evidence.context.routing_decision, 'engineering_auto_repair_version': 1,
             'source': 'self_repair', 'self_repair_accepted': True, 'engineering_tool_correction_version': 1}
    evidence.context = evidence.context.model_copy(update={'routing_decision': route})
    rows = pair()
    mapping(rows[0]['rejection']).update(run_id=str(evidence.context.run_id), tenant_id=str(evidence.context.tenant_id))
    evidence.models[:0] = rows
    metadata = evidence.metadata()
    assert metadata['model_position'] == 2
    assert evidence.models[0]['status'] == 'rejected'
    assert evidence.valid(evidence.report(metadata=metadata))
    evidence.models[1]['status'] = 'running'
    with pytest.raises(ValueError):
        evidence.metadata()


async def late_case(fault: str = 'after_bundle', *, include_models: bool = False) -> tuple[TaskContext, RuntimeCheckpoint, list[Artifact]]:
    journey = ProjectJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=UnexpectedTaskQueue())
    await service.execute(repo.run_id)
    runtime.second_fault = 'after_write'
    await service.execute(repo.run_id)
    route = mutable(repo.row.routing_decision)
    runtime.second_fault = fault
    await service.execute(repo.run_id)
    saved = repo.checkpoint
    context = journey.context.model_copy(update={'checkpoint': saved, 'routing_decision': route})
    refs = tuple(ArtifactReference(id=UUID(k), sha256=string(v)) for k, v in json_map(saved.state['artifact_registry']).items())
    artifacts = await journey.artifacts.get_many(context.tenant_id, context.run_id, refs)
    return context, saved, [a for a in artifacts if include_models or a.type == 'tool_result']


def boundary_checkpoint(saved: RuntimeCheckpoint, state: Mapping[str, object]) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(id=saved.id, runtime_type='autogen', runtime_version='2',
        run_id=saved.run_id, tenant_id=saved.tenant_id, mode=saved.mode, state=json_map(state))


def bind_tool_artifact(
    state: dict[str, object], artifacts: list[Artifact], position: int,
    content: Mapping[str, object], *, producer: str | None = None,
) -> None:
    row = rows(state['tool_ledger'])[position]
    original = next(a for a in artifacts if str(a.id) == row['artifact_id'])
    artifact = Artifact(id=original.id, type=original.type,
        producer=original.producer if producer is None else producer,
        content=json_map(content), source_ids=original.source_ids)
    artifacts[artifacts.index(original)] = artifact
    row['artifact_sha256'] = artifact.content_sha256
    mapping(state['artifact_registry'])[str(artifact.id)] = artifact.content_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', [
    'missing_receipt', 'artifact_hash', 'producer', 'arguments', 'idempotency',
    'verify_scope', 'zip_source', 'zip_receipt', 'zip_manifest', 'zip_presentation',
    'invalidated_zip', 'unknown_model', 'unknown_tool', 'permission', 'hard_tokens',
    'tokens_reset', 'cost_reset', 'absolute_reset', 'uncommitted_models', 'uncommitted_tools',
])
async def test_late_progress_rejects_forged_stale_or_unsafe_evidence(defect: str) -> None:
    context, saved, artifacts = await late_case()
    state = mutable(saved.state)
    route = mutable(context.routing_decision)
    tools = rows(state['tool_ledger'])
    last = tools[-1]
    artifact = next(a for a in artifacts if str(a.id) == last['artifact_id'])
    content = mutable(artifact.content)
    if defect == 'missing_receipt':
        artifacts.remove(artifact)
    elif defect == 'artifact_hash':
        last['artifact_sha256'] = 'f' * 64
    elif defect == 'producer':
        bind_tool_artifact(state, artifacts, len(tools) - 1, content, producer='reviewer')
    elif defect == 'arguments':
        content['arguments_sha256'] = 'f' * 64
        bind_tool_artifact(state, artifacts, len(tools) - 1, content)
    elif defect == 'idempotency':
        last['idempotency_key'] = 'f' * 64
    elif defect == 'verify_scope':
        verify = next(a for a in artifacts if str(a.id) == tools[-2]['artifact_id'])
        changed = mutable(verify.content)
        mapping(mapping(changed['result'])['generated_project_acceptance'])['run_id'] = str(uuid4())
        bind_tool_artifact(state, artifacts, len(tools) - 2, changed)
    elif defect.startswith('zip_'):
        result = mapping(content['result'])
        key = {'zip_source': 'verification_source_sha256', 'zip_receipt': 'verification_receipt_sha256',
               'zip_manifest': 'workspace_files', 'zip_presentation': 'presentation'}[defect]
        result[key] = [] if defect == 'zip_manifest' else 'f' * 64
        bind_tool_artifact(state, artifacts, len(tools) - 1, content)
    elif defect == 'invalidated_zip':
        write = next(a for a in artifacts if a.content.get('tool_name') == 'workspace.write_text')
        row = dict(tools[2])
        row.update(position=len(tools), artifact_id=str(uuid4()))
        row['idempotency_key'] = hashlib.sha256(
            f"{context.run_id}:{row['actor']}:{row['position']}:{row['name']}:{row['arguments_hash']}".encode()).hexdigest()
        stale = Artifact(id=UUID(string(row['artifact_id'])), type=write.type, producer=write.producer,
            content=write.content)
        row['artifact_sha256'] = stale.content_sha256
        tools.append(row)
        artifacts.append(stale)
        mapping(state['artifact_registry'])[str(stale.id)] = stale.content_sha256
        state['engineering_tool_prefix'] = len(tools)
    elif defect == 'unknown_model':
        rows(state['model_ledger'])[-1]['status'] = 'running'
    elif defect == 'unknown_tool':
        last['status'] = 'running'
    elif defect == 'permission':
        route['requested_permissions'] = ['host_write']
    elif defect == 'hard_tokens':
        route['runtime_token_absolute_tokens'] = integer(route['runtime_token_absolute_tokens']) + 1
    elif defect == 'tokens_reset':
        mapping(state['usage'])['tokens'] = 0
    elif defect == 'cost_reset':
        mapping(state['usage'])['cost_usd'] = '0'
    elif defect == 'uncommitted_models':
        phase = mapping(state['project_repair_phase'])
        phase['model_floor'] = integer(phase['model_floor']) - 1
    elif defect == 'uncommitted_tools':
        state['engineering_tool_prefix'] = integer(state['engineering_tool_prefix']) - 1
    else:
        state['remaining_absolute_timeout_seconds'] = number(state['remaining_absolute_timeout_seconds']) + 1
    context = context.model_copy(update={'routing_decision': route})
    with pytest.raises(ValueError):
        productive_boundary(context, boundary_checkpoint(saved, state), artifacts)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['after_verify', 'after_bundle'])
async def test_repeated_delivery_semantics_cannot_buy_another_continuation(fault: str) -> None:
    context, saved, artifacts = await late_case(fault)
    progress = productive_boundary(context, saved, artifacts)
    context = context.model_copy(update={'routing_decision': {
        **context.routing_decision, 'self_repair_episode_progress': progress}})
    state = mutable(saved.state)
    tools = rows(state['tool_ledger'])
    row = dict(tools[-1])
    original = next(a for a in artifacts if str(a.id) == row['artifact_id'])
    content = mutable(original.content)
    if fault == 'after_verify':
        checks = rows(mapping(mapping(content['result'])['verification'])['checks'])
        diagnostic = mapping(checks[0]['execution_diagnostic'])
        output = 'same source verified again\n'
        diagnostic['stdout'] = output
        mapping(diagnostic['stdout_metadata']).update(sanitized_bytes=len(output), returned_bytes=len(output),
            sha256=hashlib.sha256(output.encode()).hexdigest())
    else:
        mapping(content['result'])['artifact_id'] = str(uuid4())
        mapping(mapping(content['result'])['file'])['filename'] = 'another-attempt.zip'
    duplicate = Artifact(id=uuid4(), type=original.type, producer=original.producer, content=json_map(content))
    row.update(position=len(tools), artifact_id=str(duplicate.id), artifact_sha256=duplicate.content_sha256)
    row['idempotency_key'] = hashlib.sha256(
        f"{context.run_id}:{row['actor']}:{row['position']}:{row['name']}:{row['arguments_hash']}".encode()).hexdigest()
    tools.append(row)
    artifacts.append(duplicate)
    mapping(state['artifact_registry'])[str(duplicate.id)] = duplicate.content_sha256
    state['engineering_tool_prefix'] = len(tools)
    stopped = boundary_checkpoint(saved, state)
    current = engineering_progress(context, tool_entries=ledger(stopped.state['tool_ledger']), artifacts=artifacts,
        allowed_tools={'implementer': ('project.inspect', 'workspace.list', 'workspace.write_text',
                                      'project.verify', 'workspace.bundle')})
    assert not current.uncertain and current.verification_current
    assert fault == 'after_verify' or current.bundle_current
    with pytest.raises(ValueError):
        productive_boundary(context, stopped, artifacts)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['ack_verify', 'ack_bundle'])
async def test_complete_bound_round_is_pure_and_does_not_grant_repeat_credit(fault: str) -> None:
    context, saved, artifacts = await late_case(fault, include_models=True)
    original = saved.to_payload()
    assert completed_round(context, saved.state, artifacts)
    progress = productive_boundary(context, saved, artifacts)
    state = mutable(saved.state)
    mapping(state['project_repair_phase'])['model_floor'] = len(rows(state['model_ledger']))
    state['engineering_tool_prefix'] = len(rows(state['tool_ledger']))
    settled = boundary_checkpoint(saved, state)
    assert not completed_round(context, settled.state, artifacts)
    assert saved.to_payload() == original
    context = context.model_copy(update={'routing_decision': {
        **context.routing_decision, 'self_repair_episode_progress': progress}})
    with pytest.raises(ValueError):
        productive_boundary(context, settled, artifacts)


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', [
    'legacy', 'actor', 'request', 'tenant', 'run', 'model_position', 'tool_start',
    'logical_model', 'alias', 'missing_call', 'extra_call', 'arguments', 'call_name',
    'missing_model', 'model_hash', 'tool_hash', 'tool_actor', 'tool_position',
    'tool_idempotency', 'unknown_tool', 'failed_tool', 'unknown_model', 'approval',
    'selector_actor', 'selector_request', 'selector_text', 'selector_calls',
])
async def test_known_round_requires_exact_private_intent_and_complete_effect_batch(defect: str) -> None:
    context, saved, artifacts = await late_case('ack_verify', include_models=True)
    state = mutable(saved.state)
    models, tools = rows(state['model_ledger']), rows(state['tool_ledger'])
    row, tool = models[-1], tools[-1]
    artifact = next(a for a in artifacts if str(a.id) == row['artifact_id'])
    content = mutable(artifact.content)
    binding = mapping(content[ROUND_INTENT])
    if defect == 'legacy':
        del content[ROUND_INTENT]
    elif defect in {'actor', 'request', 'tenant', 'run', 'model_position', 'tool_start', 'logical_model'}:
        key = {'request': 'request_hash', 'tenant': 'tenant_id', 'run': 'run_id'}.get(defect, defect)
        binding[key] = {'actor': 'another_actor', 'request_hash': 'f' * 64,
            'tenant_id': str(uuid4()), 'run_id': str(uuid4()), 'model_position': 0,
            'tool_start': 0, 'logical_model': 'another_model'}[key]
    elif defect == 'alias':
        mapping(binding['tool_names'])['project_verify'] = 'workspace.bundle'
    elif defect == 'missing_call':
        content['tool_calls'] = []
    elif defect == 'extra_call':
        extra = copy.deepcopy(mapping(sequence(content['tool_calls'])[-1]))
        extra['id'] = 'unexecuted-tail'
        sequence(content['tool_calls']).append(extra)
    elif defect in {'arguments', 'call_name'}:
        call = mapping(sequence(content['tool_calls'])[-1])
        if defect == 'arguments':
            mapping(call['arguments'])['unexpected'] = True
        else:
            call['name'] = 'workspace_bundle'
    elif defect == 'missing_model':
        artifacts.remove(artifact)
    elif defect == 'model_hash':
        row['artifact_sha256'] = 'f' * 64
    elif defect == 'tool_hash':
        tool['artifact_sha256'] = 'f' * 64
    elif defect == 'tool_actor':
        tool['actor'] = 'another_actor'
    elif defect == 'tool_position':
        tool['position'] = integer(tool['position']) - 1
    elif defect == 'tool_idempotency':
        tool['idempotency_key'] = 'f' * 64
    elif defect == 'unknown_tool':
        tool['status'] = 'running'
        tool['replay_safe'] = True
    elif defect == 'failed_tool':
        tool['status'] = 'failed'
    elif defect == 'unknown_model':
        row['status'] = 'running'
    elif defect == 'approval':
        state['approval_batch'] = {}
    elif defect.startswith('selector_'):
        selector = models[integer(mapping(state['project_repair_phase'])['model_floor'])]
        source = next(a for a in artifacts if str(a.id) == selector['artifact_id'])
        changed = mutable(source.content)
        if defect == 'selector_actor':
            mapping(changed[ROUND_INTENT])['actor'] = 'implementer'
        elif defect == 'selector_request':
            mapping(changed[ROUND_INTENT])['request_hash'] = 'f' * 64
        elif defect == 'selector_text':
            changed['text'] = 'another_actor'
        else:
            changed['tool_calls'] = content['tool_calls']
        replacement = Artifact(id=source.id, type=source.type, producer=source.producer, content=json_map(changed))
        artifacts[artifacts.index(source)] = replacement
        selector['artifact_sha256'] = replacement.content_sha256
        mapping(state['artifact_registry'])[str(replacement.id)] = replacement.content_sha256
    if defect in {'legacy', 'actor', 'request', 'tenant', 'run', 'model_position', 'tool_start',
                  'logical_model', 'alias', 'missing_call', 'extra_call', 'arguments', 'call_name'}:
        replacement = Artifact(id=artifact.id, type=artifact.type, producer=artifact.producer, content=json_map(content))
        artifacts[artifacts.index(artifact)] = replacement
        row['artifact_sha256'] = replacement.content_sha256
        mapping(state['artifact_registry'])[str(replacement.id)] = replacement.content_sha256
    before = copy.deepcopy(state)
    with pytest.raises(ValueError):
        completed_round(context, boundary_checkpoint(saved, state).state, artifacts)
    assert state == before
