"""Strict Direct action boundaries are drain receipts, never execution authority."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runtime.contracts import JsonValue, RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.hybrid import HybridPlan, HybridUpgrade
from tests.unit.runs import test_approval_checkpoint_service as drain
from tests.unit.runs.test_terminal_hooks import TENANT_ID

APPROVAL = 'approval_current'


def reference(total: int = 1) -> dict[str, Any]:
    count = (total + 65535) // 65536
    return {'version': 1, 'content_sha256': 'a' * 64, 'total_bytes': total,
            'segments': [{'id': str(uuid4()), 'sha256': 'b' * 64} for _ in range(count)]}


def action_checkpoint(
    run_id: UUID | None = None, approval_id: str = APPROVAL, **state: Any,
) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(id=uuid4(), runtime_type='direct_action', runtime_version='2',
        tenant_id=TENANT_ID, run_id=run_id or uuid4(), mode=TaskMode.DIRECT,
        state={'phase': 'concrete_action', 'completed': False, 'approval_id': approval_id,
               'tool_name': 'workspace.bundle', 'context_sha256': 'c' * 64,
               'reference': reference(), 'elapsed_seconds': 1.5, 'timeout_seconds': 60,
               **state})


def hybrid(child: RuntimeCheckpoint, *, version: str = '3', stage: Any = 2,
           **state: Any) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(id=uuid4(), runtime_type='hybrid', runtime_version=version,
        tenant_id=child.tenant_id, run_id=child.run_id, mode=TaskMode.HYBRID,
        state={'terminal': False, 'next_stage': stage,
               'child_checkpoint': cast(JsonValue, child.to_payload()), **state})


@pytest.mark.parametrize('version,stage', [(None, None), ('2', 2), ('2', 3), ('3', 2), ('3', 3)])
def test_only_top_level_direct_v2_matches_exact_approval(
    version: str | None, stage: int | None,
) -> None:
    checkpoint = action_checkpoint()
    if version is not None:
        checkpoint = hybrid(checkpoint, version=version, stage=stage)
    assert checkpoint_waits_for_approval(checkpoint, APPROVAL) is (version is None)
    assert not checkpoint_waits_for_approval(checkpoint, 'approval_stale')
    assert not checkpoint_waits_for_approval(checkpoint, APPROVAL + ' ')


@pytest.mark.parametrize('version', ['2', '3'])
@pytest.mark.parametrize('upgrade', tuple(HybridUpgrade))
@pytest.mark.parametrize('stage', [2, 3])
def test_hybrid_direct_plan_and_stage_are_unsupported_and_fail_closed(
    version: str, upgrade: HybridUpgrade, stage: int,
) -> None:
    # A normal plan's stage 3 is terminal; upgraded stage 2 is discussion.
    # Even the actual Direct stage stays unsupported until plan-aware drain exists.
    checkpoint = hybrid(action_checkpoint(), version=version, stage=stage,
                        plan_digest=HybridPlan(upgrade).digest)
    assert not checkpoint_waits_for_approval(checkpoint, APPROVAL)


@pytest.mark.parametrize('plan', [{}, {'plan_digest': '0' * 64},
                                  {'upgrade': 'discuss_dispatch_discuss'}])
def test_hybrid_direct_missing_unknown_or_ad_hoc_plan_cannot_grant_boundary(
    plan: dict[str, Any],
) -> None:
    assert not checkpoint_waits_for_approval(hybrid(action_checkpoint(), **plan), APPROVAL)


@pytest.mark.parametrize('total', [1, 65536, 65537, 64 * 1024 * 1024])
def test_reference_accepts_exact_segment_boundaries(total: int) -> None:
    from agent_hub.runtime.action_checkpoint import _reference_valid

    value = reference(total)
    assert _reference_valid(value)
    # RuntimeCheckpoint's independent aggregate node budget can be stricter than
    # the codec's reference ceiling; this change must not increase that budget.
    if total < 64 * 1024 * 1024:
        assert checkpoint_waits_for_approval(action_checkpoint(reference=value), APPROVAL)


@pytest.mark.parametrize('mutation', [
    'missing', 'extra', 'version_bool', 'version_float', 'version_old', 'bytes_bool',
    'bytes_float', 'bytes_zero', 'bytes_over', 'bytes_string', 'content_hash', 'content_hash_upper',
    'segments_empty', 'segments_object', 'count_short', 'count_long', 'segments_over',
    'segment_extra', 'segment_missing', 'uuid_upper', 'uuid_compact', 'uuid_bad', 'uuid_object',
    'duplicate_id', 'segment_hash', 'segment_nonobject', 'nested_reference',
])
def test_rehashed_reference_with_invalid_shape_or_bounds_is_rejected(mutation: str) -> None:
    from agent_hub.runtime.action_checkpoint import _reference_valid

    value = reference(65537)
    if mutation == 'missing':
        value.pop('segments')
    elif mutation == 'extra':
        value['unrelated'] = True
    elif mutation.startswith('version_'):
        value['version'] = {'bool': True, 'float': 1.0, 'old': 2}[mutation[8:]]
    elif mutation.startswith('bytes_'):
        value['total_bytes'] = {'bool': True, 'float': 65537.0, 'zero': 0,
            'over': 64 * 1024 * 1024 + 1, 'string': '65537'}[mutation[6:]]
    elif mutation.startswith('content_hash'):
        value['content_sha256'] = 'A' * 64 if mutation.endswith('upper') else 'a' * 63
    elif mutation == 'segments_empty':
        value['segments'] = []
    elif mutation == 'segments_object':
        value['segments'] = {'0': value['segments'][0]}
    elif mutation == 'count_short':
        value['segments'].pop()
    elif mutation == 'count_long':
        value['segments'].append({'id': str(uuid4()), 'sha256': 'b' * 64})
    elif mutation == 'segments_over':
        value = reference(64 * 1024 * 1024 + 1)
    elif mutation == 'segment_extra':
        value['segments'][0]['extra'] = 1
    elif mutation == 'segment_missing':
        value['segments'][0].pop('sha256')
    elif mutation.startswith('uuid_'):
        value['segments'][0]['id'] = {
            'upper': 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA',
            'compact': 'a' * 32, 'bad': 'not-a-uuid', 'object': {'id': str(uuid4())},
        }[mutation[5:]]
    elif mutation == 'duplicate_id':
        value['segments'][1]['id'] = value['segments'][0]['id']
    elif mutation == 'segment_hash':
        value['segments'][0]['sha256'] = 'g' * 64
    elif mutation == 'segment_nonobject':
        value['segments'][0] = 'ref'
    else:
        value = {'reference': value}
    assert not _reference_valid(value)
    if mutation != 'segments_over':
        assert not checkpoint_waits_for_approval(action_checkpoint(reference=value), APPROVAL)


@pytest.mark.parametrize('field,value', [
    ('phase', 'prepared'), ('completed', True), ('completed', 0),
    ('approval_id', 'approval-current'), ('approval_id', 'approval_'),
    ('approval_id', 'approval_' + 'a' * 101), ('approval_id', 'approval_current\n'),
    ('tool_name', 'command.run'), ('tool_name', {'name': 'workspace.bundle'}),
    ('context_sha256', 'C' * 64), ('context_sha256', 'c' * 63),
    ('elapsed_seconds', True), ('elapsed_seconds', -1), ('elapsed_seconds', 60),
    ('elapsed_seconds', '1'), ('timeout_seconds', False), ('timeout_seconds', 0),
    ('timeout_seconds', 3601), ('timeout_seconds', '60'),
    ('remaining_tokens', 100), ('timeout_seconds', 10**1000),
])
def test_rehashed_state_does_not_bypass_strict_schema(field: str, value: Any) -> None:
    assert not checkpoint_waits_for_approval(action_checkpoint(**{field: value}), APPROVAL)


@pytest.mark.parametrize('mutation', ['missing', 'hash', 'runtime_action_v1', 'direct_action_v1',
    'direct', 'dispatch_mode', 'missing_reference', 'nested_approval'])
def test_unrelated_or_unbound_checkpoints_do_not_satisfy_direct_approval(mutation: str) -> None:
    checkpoint = action_checkpoint()
    if mutation == 'missing':
        assert not checkpoint_waits_for_approval(None, APPROVAL)
        return
    elif mutation == 'hash':
        checkpoint = checkpoint.model_copy(update={'state_sha256': '0' * 64})
    elif mutation == 'dispatch_mode':
        checkpoint = checkpoint.model_copy(update={'mode': TaskMode.DISPATCH})
    elif mutation in {'runtime_action_v1', 'direct_action_v1', 'direct'}:
        runtime, version = {'runtime_action_v1': ('runtime_action', '1'),
            'direct_action_v1': ('direct_action', '1'), 'direct': ('direct', '2')}[mutation]
        checkpoint = checkpoint.model_copy(update={'runtime_type': runtime, 'runtime_version': version})
    else:
        payload = checkpoint.to_payload()
        state = cast(dict[str, Any], payload['state'])
        if mutation == 'missing_reference':
            state.pop('reference')
        else:
            state = {'arbitrary': dict(state)}
        checkpoint = RuntimeCheckpoint.from_payload({**payload, 'state': state, 'state_sha256': ''})
    assert not checkpoint_waits_for_approval(checkpoint, APPROVAL)


@pytest.mark.parametrize('mutation', ['tenant', 'run', 'child_hash', 'child_no_hash', 'outer_hash',
    'recursive', 'terminal', 'stage_bool', 'stage_wrong', 'version', 'outer_mode', 'arbitrary_ref'])
def test_hybrid_never_searches_arbitrary_state_or_accepts_wrong_child_scope(mutation: str) -> None:
    checkpoint = hybrid(action_checkpoint())
    payload = checkpoint.to_payload()
    state = cast(dict[str, Any], payload['state'])
    child = state['child_checkpoint']
    if mutation in {'tenant', 'run'}:
        child[mutation + '_id'] = str(uuid4())
    elif mutation == 'child_hash':
        child['state_sha256'] = '0' * 64
    elif mutation == 'child_no_hash':
        child.pop('state_sha256')
    elif mutation == 'outer_hash':
        assert not checkpoint_waits_for_approval(
            checkpoint.model_copy(update={'state_sha256': '0' * 64}), APPROVAL)
        return
    elif mutation == 'recursive':
        state['child_checkpoint'] = hybrid(action_checkpoint(checkpoint.run_id)).to_payload()
    elif mutation == 'terminal':
        state['terminal'] = True
    elif mutation == 'stage_bool':
        state['next_stage'] = True
    elif mutation == 'stage_wrong':
        state['next_stage'] = 0
    elif mutation == 'version':
        payload['runtime_version'] = '1'
    elif mutation == 'outer_mode':
        payload['mode'] = 'direct'
    else:
        state['reference'] = state.pop('child_checkpoint')
    payload['state_sha256'] = ''
    assert not checkpoint_waits_for_approval(RuntimeCheckpoint.from_payload(payload), APPROVAL)


def test_validator_import_does_not_load_action_runtime_or_storage_codec() -> None:
    result = subprocess.run([sys.executable, '-c',
        ('import sys; import agent_hub.runtime.action_checkpoint; '
        'assert "agent_hub.runtime.action_direct" not in sys.modules; '
        'assert "agent_hub.runtime.continuation_artifacts" not in sys.modules')],
        capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('wrapped', [False, True])
async def test_service_accepts_only_top_level_direct_boundary_and_closes_stream(
    monkeypatch: pytest.MonkeyPatch, wrapped: bool,
) -> None:
    monkeypatch.setattr(drain, 'APPROVAL_ID', APPROVAL)
    repository = drain.ApprovalRepository()

    class ActionRuntime(drain.QueuedApprovalRuntime):
        closed = False

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            try:
                async with aclosing(cast(AsyncGenerator[RunEvent, None], super().run(context))) as events:
                    async for event in events:
                        if event.checkpoint is not None:
                            old = event.checkpoint
                            if old.runtime_type == 'hybrid':
                                old = RuntimeCheckpoint.from_payload(
                                    cast(dict[str, Any], old.to_payload()['state'])['child_checkpoint'])
                            tools = cast(dict[str, Any], old.to_payload()['state'])['tools']
                            approval = tools['tool-key']['approval_id']
                            # The legacy Crew fixture's default ID predates Direct's wire pattern.
                            if approval == 'approval-current':
                                approval = APPROVAL
                            checkpoint = action_checkpoint(context.run_id, approval)
                            if wrapped:
                                checkpoint = hybrid(checkpoint)
                            event = event.model_copy(update={'checkpoint': checkpoint})
                        yield event
            finally:
                self.closed = True

    runtime = ActionRuntime(repository, hybrid=wrapped)
    if not wrapped:
        runtime.mode = TaskMode.DIRECT
    async with asyncio.timeout(2):
        result = await drain.make_service(repository, runtime).execute(repository.run_id)
    if wrapped:
        assert result.status is RunStatus.FAILED
        assert not any(repository.approval_observations)
        assert not checkpoint_waits_for_approval(repository.checkpoint, APPROVAL)
    else:
        assert result.status is RunStatus.WAITING_APPROVAL
        assert repository.approval_observations == [False, False, True]
        assert checkpoint_waits_for_approval(repository.checkpoint, APPROVAL)
        assert not runtime.emitted_after_boundary
    assert runtime.closed
