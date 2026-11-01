"""Real Hybrid/AutoGen waiting checkpoints; model/tool transports stay in memory."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import aclosing
from copy import deepcopy
from uuid import uuid4

import pytest
import pytest_asyncio

from agent_hub.domain.runs import TaskMode
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
from agent_hub.runtime.contracts import Artifact, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.hybrid import HybridPlan, HybridRuntime, HybridUpgrade
from tests.unit.runs.conversation_authenticated_input_typed_fixture import mutable
from tests.unit.runs.snapshot_approval_types_fixture import closable, json_state, mutable_list
from tests.unit.runtime.autogen.test_capability_approval_resume import (
    APPROVAL,
    RealDiscussionApproval,
)
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime, UnusedRuntime


def _child(checkpoint: RuntimeCheckpoint) -> RuntimeCheckpoint:
    return RuntimeCheckpoint.from_payload(mutable(checkpoint.to_payload()['state'])['child_checkpoint'])


async def _real_waiting_case() -> tuple[RealDiscussionApproval, TaskContext, RuntimeCheckpoint]:
    case = RealDiscussionApproval()
    parent = case.context.model_copy(update={
        'mode': TaskMode.HYBRID,
        'routing_decision': {**case.context.routing_decision, 'engineering_action_admission_version': 1},
    })
    dispatch = MultiArtifactRuntime(TaskMode.DISPATCH, (
        Artifact(id=uuid4(), type='text', producer='planner', content={'text': 'Plan the same task.'}),
    ))
    hybrid = HybridRuntime(dispatch, case.runner, UnusedRuntime(TaskMode.DIRECT, 'unused'),
                           artifact_repository=case.runner._repository)
    saved = None
    async with asyncio.timeout(5), aclosing(closable(hybrid.run(parent))) as stream:
        async for event in stream:
            checkpoint = event.checkpoint
            if (checkpoint is None or checkpoint.state.get('next_stage') != 1
                    or not isinstance(checkpoint.state.get('child_checkpoint'), Mapping)):
                continue
            child = _child(checkpoint)
            if checkpoint_waits_for_approval(child, APPROVAL):
                saved = checkpoint
                break
    assert saved is not None, 'actual Hybrid must publish the actual AutoGen waiting receipt'
    assert saved.runtime_version == '3' and saved.state['plan_digest'] == HybridPlan().digest
    assert _child(saved).runtime_type == 'autogen' and _child(saved).runtime_version == '2'
    assert len(case.requests) > 0 and case.executions == 0
    assert sum(call.tool_name == 'workspace.write_text' for call in case.calls) == 1
    assert hybrid._active_task is None and case.runner._active_task is None
    return case, parent, saved


@pytest_asyncio.fixture(scope='module')
async def waiting_checkpoint() -> RuntimeCheckpoint:
    _, _, checkpoint = await _real_waiting_case()
    return checkpoint


def test_real_default_hybrid_stage_one_discussion_receipt_matches(
    waiting_checkpoint: RuntimeCheckpoint,
) -> None:
    assert checkpoint_waits_for_approval(waiting_checkpoint, APPROVAL)
    assert not checkpoint_waits_for_approval(waiting_checkpoint, 'approval_other')


@pytest.mark.asyncio
async def test_real_hybrid_cold_resume_executes_only_pending_action_without_replaying_models() -> None:
    case, parent, saved = await _real_waiting_case()
    assert checkpoint_waits_for_approval(saved, APPROVAL)
    before_requests, before_calls = len(case.requests), list(case.calls)
    case.approved = True
    discussion = AutoGenDiscussionRuntime(
        case, case.runner._plan, capability_gateway=case, harness_tool_gateway=case,
        artifact_repository=case.runner._repository,
    )
    resumed = HybridRuntime(
        UnusedRuntime(TaskMode.DISPATCH, 'unused'), discussion,
        UnusedRuntime(TaskMode.DIRECT, 'unused'), artifact_repository=case.runner._repository,
    )
    await resumed.restore_checkpoint(saved)
    completed = None
    async with asyncio.timeout(5), aclosing(closable(resumed.run(parent.model_copy(
        update={'checkpoint': saved},
    )))) as stream:
        async for event in stream:
            if (event.checkpoint is None or event.checkpoint.state.get('next_stage') != 1
                    or not isinstance(event.checkpoint.state.get('child_checkpoint'), Mapping)):
                continue
            child = _child(event.checkpoint)
            ledger = child.state.get('tool_ledger')
            if (isinstance(ledger, tuple) and ledger and isinstance(ledger[-1], Mapping)
                    and ledger[-1].get('name') == 'project.verify'
                    and ledger[-1].get('status') == 'succeeded'):
                completed = event.checkpoint
                break
    assert completed is not None and case.executions == 1
    assert len(case.requests) == before_requests, 'paid model history must not be replayed'
    assert case.calls[:-1] == before_calls and case.calls[-1].tool_name == 'project.verify'
    assert case.calls[-1].idempotency_key == before_calls[-1].idempotency_key
    assert not checkpoint_waits_for_approval(completed, APPROVAL)
    assert resumed._active_task is None and discussion._active_task is None


@pytest.mark.parametrize('defect', [
    'wrong_plan', 'upgraded_plan', 'missing_plan', 'outer_version', 'stage_zero', 'stage_two',
    'bool_stage', 'outer_terminal', 'outer_mode', 'outer_hash', 'outer_tenant', 'outer_run',
    'child_version', 'child_mode', 'child_terminal', 'child_hash', 'missing_child_hash',
    'child_tenant', 'child_run', 'direct', 'nested', 'wrong_approval', 'waiting_not_last',
    'unknown', 'wrong_key',
])
def test_rehashed_nondefault_or_unbound_waiting_checkpoint_is_rejected(
    waiting_checkpoint: RuntimeCheckpoint, defect: str,
) -> None:
    payload = deepcopy(waiting_checkpoint.to_payload())
    state = mutable(payload['state'])
    child = mutable(state['child_checkpoint'])
    if defect == 'wrong_plan':
        state['plan_digest'] = HybridPlan(HybridUpgrade.DIRECT_TO_DISPATCH).digest
    elif defect == 'upgraded_plan':
        state['plan_digest'] = HybridPlan(HybridUpgrade.DISCUSS_DISPATCH_DISCUSS).digest
    elif defect == 'missing_plan':
        state.pop('plan_digest')
    elif defect == 'outer_version':
        payload['runtime_version'] = '2'
    elif defect.startswith('stage_'):
        state['next_stage'] = 0 if defect == 'stage_zero' else 2
    elif defect == 'bool_stage':
        state['next_stage'] = True
    elif defect == 'outer_terminal':
        state['terminal'] = True
    elif defect == 'outer_mode':
        payload['mode'] = 'dispatch'
    elif defect == 'outer_hash':
        assert not checkpoint_waits_for_approval(waiting_checkpoint.model_copy(
            update={'state_sha256': '0' * 64},
        ), APPROVAL)
        return
    elif defect in {'outer_tenant', 'outer_run', 'child_tenant', 'child_run'}:
        target = payload if defect.startswith('outer_') else child
        target['tenant_id' if defect.endswith('tenant') else 'run_id'] = str(uuid4())
    elif defect == 'child_version':
        child['runtime_version'] = '1'
    elif defect == 'child_mode':
        child['mode'] = 'direct'
    elif defect == 'child_terminal':
        mutable(child['state'])['terminal'] = True
    elif defect == 'direct':
        child['runtime_type'], child['mode'] = 'direct_action', 'direct'
    elif defect == 'nested':
        state['child_checkpoint'] = waiting_checkpoint.to_payload()
    elif defect == 'wrong_approval':
        mutable(mutable_list(mutable(child['state'])['tool_ledger'])[-1])['approval_id'] = 'approval_foreign'
    elif defect == 'waiting_not_last':
        mutable_list(mutable(child['state'])['tool_ledger']).append(dict(mutable(mutable_list(mutable(child['state'])['tool_ledger'])[-2])))
    elif defect == 'unknown':
        mutable(mutable_list(mutable(child['state'])['tool_ledger'])[-2])['status'] = 'running'
    elif defect == 'wrong_key':
        mutable(mutable_list(mutable(child['state'])['tool_ledger'])[-1])['idempotency_key'] = 'f' * 64
    if defect == 'missing_child_hash':
        child.pop('state_sha256')
    elif defect == 'child_hash':
        child['state_sha256'] = '0' * 64
    elif defect != 'nested':
        child['state_sha256'] = ''
        state['child_checkpoint'] = RuntimeCheckpoint.from_payload(child).to_payload()
    payload['state_sha256'] = ''
    candidate = RuntimeCheckpoint.from_payload(payload)
    assert not checkpoint_waits_for_approval(candidate, APPROVAL)


@pytest.mark.parametrize('version', ['2', '3'])
@pytest.mark.parametrize('stage', [0, 1])
def test_legacy_crew_receipt_behavior_is_unchanged(version: str, stage: int) -> None:
    from tests.unit.runs.test_approval_checkpoint_service import crew_checkpoint

    child = crew_checkpoint(uuid4(), APPROVAL)
    outer = RuntimeCheckpoint(
        id=uuid4(), runtime_type='hybrid', runtime_version=version,
        run_id=child.run_id, tenant_id=child.tenant_id, mode=TaskMode.HYBRID,
        state=json_state({'terminal': False, 'next_stage': stage,
               'plan_digest': HybridPlan(HybridUpgrade.DISCUSS_DISPATCH_DISCUSS).digest,
               'child_checkpoint': child.to_payload()}),
    )
    assert checkpoint_waits_for_approval(outer, APPROVAL)
