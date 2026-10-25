"""A repair boundary commits to the source checkpoint, not just self-reported counts."""
from __future__ import annotations

import copy

import pytest

from agent_hub.runtime.autogen.project_repair_phase import prefix_digest, prepare, validate
from agent_hub.runtime.contracts import _mutable_json
from tests.unit.runs.test_project_auto_repair_runtime import (
    BoundaryFailure,
    ContinuationRepository,
    ProjectJourney,
    RunService,
    RuntimeRegistry,
)


async def case():
    journey = ProjectJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    await service.execute(repo.run_id)
    saved = repo.checkpoint
    context = journey.context.model_copy(update={'checkpoint': saved, 'routing_decision': repo.row.routing_decision})
    models, tools = saved.state['model_ledger'], saved.state['tool_ledger']
    phase = prepare(context, saved, models, tools)
    return context, saved, models, tools, phase


@pytest.mark.asyncio
async def test_boundary_reconstructs_original_checkpoint_without_dropping_any_rows() -> None:
    context, saved, models, tools, phase = await case()
    before = saved.to_payload()
    assert validate(phase, context=context, models=models, tools=tools, tool_prefix=len(tools), state=saved.state) == phase
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
    models, tools = _mutable_json(original_models), _mutable_json(original_tools)
    phase = copy.deepcopy(_mutable_json(original_phase))
    if defect == 'source_sha':
        phase['source_checkpoint_sha256'] = 'f' * 64
    elif defect == 'scope_sha':
        phase['source_scope_sha256'] = 'f' * 64
    elif defect == 'source_model_count':
        phase['source_model_count'] = 0
        phase['source_model_sha256'] = prefix_digest([])
    elif defect == 'source_tool_count':
        phase['source_tool_count'] -= 1
        phase['source_tool_sha256'] = prefix_digest(tools[:-1])
    elif defect == 'source_row_rehash':
        models[0]['request_hash'] = 'e' * 64
        phase['source_model_sha256'] = prefix_digest(models)
    elif defect == 'metadata':
        phase['source_checkpoint_metadata']['usage']['tokens'] = 0
    elif defect == 'failure_artifact':
        phase['failure_artifact_sha256'] = 'f' * 64
    elif defect == 'floor':
        phase['model_floor'] += 1
    elif defect == 'unknown_consumed':
        models.append({'status': 'running', 'request_hash': 'f' * 64})
        phase['model_floor'] += 1
    elif defect == 'fingerprint':
        phase['failure_fingerprint'] = 'f' * 64
    elif defect == 'strategy':
        phase['strategy'] = 'raise_budget_and_retry'
    elif defect == 'version':
        phase['version'] = True
    with pytest.raises(ValueError):
        validate(phase, context=context, models=models, tools=tools, tool_prefix=len(tools), state=saved.state)
    assert saved.state['usage']['tokens'] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['permission', 'hard_tokens', 'tokens_reset', 'cost_reset', 'absolute_reset', 'soft_reset'])
async def test_current_authority_and_budget_cannot_diverge_from_source(defect: str) -> None:
    context, saved, models, tools, phase = await case()
    route, state = _mutable_json(context.routing_decision), _mutable_json(saved.state)
    if defect == 'permission':
        route['requested_permissions'] = ['host_write']
    elif defect == 'hard_tokens':
        route['runtime_token_absolute_tokens'] += 1
    elif defect == 'tokens_reset':
        state['usage']['tokens'] = 0
    elif defect == 'cost_reset':
        state['usage']['cost_usd'] = '0'
    elif defect == 'absolute_reset':
        state['remaining_absolute_timeout_seconds'] += 1
    else:
        state['remaining_timeout_seconds'] += 1
    context = context.model_copy(update={'routing_decision': route})
    with pytest.raises(ValueError):
        validate(phase, context=context, models=models, tools=tools, tool_prefix=len(tools), state=state)
    from agent_hub.runtime.autogen.adapter import RuntimeExecutionError
    from agent_hub.runtime.contracts import RuntimeCheckpoint
    state['project_repair_phase'] = phase
    checkpoint = RuntimeCheckpoint(id=saved.id, runtime_type='autogen', runtime_version='2',
        run_id=context.run_id, tenant_id=context.tenant_id, mode=context.mode, state=state)
    with pytest.raises(RuntimeExecutionError, match='project repair phase is invalid'):
        ProjectJourney().runtime()._validate_checkpoint(checkpoint, context)


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['', 'unknown', 'wrong_scope', 'wrong_link', 'wrong_request'])
async def test_phase_retains_fully_bound_completed_model_correction(defect: str) -> None:
    from tests.unit.runtime.autogen.test_model_tool_correction import pair
    context, saved, original, tools, phase = await case()
    route = _mutable_json(context.routing_decision)
    # The correction policy is part of the source authority, not granted at restore.
    assert route['engineering_tool_correction_version'] == 1
    rows = pair()
    position = len(original)
    receipt = rows[0]['rejection']
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
    models = [*_mutable_json(original), *rows]
    phase['model_floor'] = len(models)
    if defect:
        with pytest.raises(ValueError):
            validate(phase, context=context, models=models, tools=tools, tool_prefix=len(tools), state=saved.state)
    else:
        assert validate(phase, context=context, models=models, tools=tools, tool_prefix=len(tools), state=saved.state) == phase
        assert models[position]['status'] == 'rejected'


def test_final_report_retains_bound_correction_chain_without_hiding_unknown() -> None:
    from tests.unit.runtime.autogen.test_delivery_report import fixture
    from tests.unit.runtime.autogen.test_model_tool_correction import pair
    evidence = fixture()
    route = {**evidence.context.routing_decision, 'engineering_auto_repair_version': 1,
             'source': 'self_repair', 'self_repair_accepted': True, 'engineering_tool_correction_version': 1}
    evidence.context = evidence.context.model_copy(update={'routing_decision': route})
    rows = pair()
    rows[0]['rejection'].update(run_id=str(evidence.context.run_id), tenant_id=str(evidence.context.tenant_id))
    evidence.models[:0] = rows
    metadata = evidence.metadata()
    assert metadata['model_position'] == 2
    assert evidence.models[0]['status'] == 'rejected'
    assert evidence.valid(evidence.report(metadata=metadata))
    evidence.models[1]['status'] = 'running'
    with pytest.raises(ValueError):
        evidence.metadata()
