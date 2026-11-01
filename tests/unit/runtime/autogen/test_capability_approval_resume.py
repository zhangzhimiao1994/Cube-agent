"""Policy pauses are known pre-execution boundaries, never unknown outcomes."""
import copy
import json
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest
from autogen_core import CancellationToken

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    GatewayCapabilityTool,
    RuntimeExecutionError,
    _DiscussionDurability,
    _pending_approval_call,
    _ToolRecord,
)
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, TaskContext
from tests.unit.runs.project_auto_repair_typed_fixture import (
    json_map,
    ledger,
    mapping,
    string,
)
from tests.unit.runs.reviewed_report_revision_typed_fixture import object_array, object_map
from tests.unit.runtime.approval_failure_trace_typed_fixture import (
    HarnessOnlyExecution,
    approval_result,
    event_stream,
)
from tests.unit.runtime.autogen.test_model_tool_bridge import _durability
from tests.unit.runtime.autogen.test_ordered_tool_calls import (
    ReplayPolicy,
    arguments,
    context_for,
    restored_journal,
)
from tests.unit.runtime.autogen.test_soft_model_admission import Journey
from tests.unit.runtime.test_generated_draft_autogen import draft_artifact

APPROVAL = 'approval_verified_action'


class ApprovalHarness:
    def __init__(self, context: TaskContext) -> None:
        self.context = context
        self.approved = False
        self.calls: list[HarnessToolCallRequest] = []
        self.executions = 0
        self.overrides: dict[str, JsonValue] = {}

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        assert tenant_id == self.context.tenant_id and request.run_id == self.context.run_id
        assert user_id == self.context.actor_id and role == self.context.actor_role
        self.calls.append(request)
        if not self.approved:
            return approval_result(request, APPROVAL, self.overrides)
        self.executions += 1
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
            status='succeeded', payload={'summary': 'verified'})


def tool(durable: _DiscussionDurability, backend: ApprovalHarness, records: list[_ToolRecord], *,
         name: str = 'workspace.write_text', replay_safe: bool = False) -> GatewayCapabilityTool:
    class Policy(ReplayPolicy, HarnessOnlyExecution):
        def is_replay_safe(self, name: str) -> bool:
            return self.replay_safe
    ctx = backend.context
    return GatewayCapabilityTool(Policy(replay_safe), tenant_id=ctx.tenant_id,
        run_id=ctx.run_id, actor='implementer', name=name, records=records,
        durability=durable, harness_tool_gateway=backend, user_id=ctx.actor_id,
        role=ctx.actor_role, server_context=ctx, sandbox_profile='workspace_write',
        serialize_tool_calls=True)


@pytest.mark.parametrize('name', ['workspace.write_text', 'project.verify', 'workspace.bundle'])
async def test_approval_pause_can_resume_once_even_for_nonreplayable_effect(name: str) -> None:
    durable = _durability()
    backend = ApprovalHarness(context_for(durable))
    records: list[_ToolRecord] = []
    args = arguments(0) if name == 'workspace.write_text' else {}
    with pytest.raises(RuntimeExecutionError):
        await tool(durable, backend, records, name=name).run_json(
            args, CancellationToken(), call_id='same-model-call')
    row = durable.tool_entries[0]
    assert row['status'] == 'waiting_approval', 'policy pause must not remain an unknown running effect'
    assert row['approval_id'] == APPROVAL and 'artifact_id' not in row
    assert durable._tool_cursor == 0 and backend.executions == 0 and not durable.artifacts
    assert records[-1].payload['status'] == 'waiting_approval'
    assert records[-1].payload['approval_id'] == APPROVAL

    restored = restored_journal(durable)
    backend.approved = True
    await tool(restored, backend, [], name=name).run_json(
        args, CancellationToken(), call_id='same-model-call')
    assert backend.executions == 1 and restored._tool_cursor == 1
    assert len(backend.calls) == 2
    assert backend.calls[0].idempotency_key == backend.calls[1].idempotency_key
    assert restored.tool_entries[0]['status'] == 'succeeded'
    assert 'approval_id' not in restored.tool_entries[0]
    cold = restored_journal(restored)
    await tool(cold, backend, [], name=name).run_json(args, CancellationToken(), call_id='same-model-call')
    assert backend.executions == 1 and len(backend.calls) == 2


@pytest.mark.parametrize('override', [
    {'call_id': 'foreign'}, {'tool_name': 'foreign'}, {'truncated': True},
    {'payload': {'approval_id': 'not a valid id'}}, {'payload': {'approval_id': APPROVAL, 'extra': True}},
    {'failure_reason': 'capability denied'},
])
async def test_malformed_or_denied_result_cannot_become_resumable(override: dict[str, JsonValue]) -> None:
    durable = _durability()
    backend = ApprovalHarness(context_for(durable))
    backend.overrides = override
    with pytest.raises(RuntimeExecutionError):
        await tool(durable, backend, []).run_json(arguments(0), CancellationToken(), call_id='write')
    assert durable.tool_entries[0]['status'] == 'running'
    assert backend.executions == 0


async def test_unapproved_resumption_rechecks_policy_without_executing() -> None:
    durable = _durability()
    backend = ApprovalHarness(context_for(durable))
    with pytest.raises(RuntimeExecutionError):
        await tool(durable, backend, []).run_json(arguments(0), CancellationToken(), call_id='write')
    restored = restored_journal(durable)
    with pytest.raises(RuntimeExecutionError):
        await tool(restored, backend, []).run_json(arguments(0), CancellationToken(), call_id='write')
    assert backend.executions == 0 and len(backend.calls) == 2
    assert restored.tool_entries[0]['status'] == 'waiting_approval'


async def test_waiting_checkpoint_failure_rolls_back_to_uncertain_not_resumable(monkeypatch: pytest.MonkeyPatch) -> None:
    durable = _durability()
    backend = ApprovalHarness(context_for(durable))
    original = durable._checkpoint
    count = 0

    async def checkpoint() -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError('checkpoint storage unavailable')
        await original()

    monkeypatch.setattr(durable, '_checkpoint', checkpoint)
    with pytest.raises(RuntimeError):
        await tool(durable, backend, []).run_json(arguments(0), CancellationToken(), call_id='write')
    assert count == 2
    assert durable.tool_entries[0]['status'] == 'running'
    assert 'approval_id' not in durable.tool_entries[0]
    assert backend.executions == 0


@pytest.mark.parametrize('field,value', [
    ('approval_id', ''), ('replay_safe', 1), ('position', True),
    ('idempotency_key', 'a' * 64), ('artifact_id', 'unexpected'),
])
async def test_tampered_waiting_row_never_crosses_policy(field: str, value: JsonValue) -> None:
    durable = _durability()
    backend = ApprovalHarness(context_for(durable))
    with pytest.raises(RuntimeExecutionError):
        await tool(durable, backend, []).run_json(arguments(0), CancellationToken(), call_id='write')
    row = copy.deepcopy(durable.tool_entries[0])
    row.update(status='waiting_approval', approval_id=APPROVAL)
    row[field] = value
    restored = restored_journal(durable)
    restored.tool_entries = [row]
    backend.approved = True
    with pytest.raises(RuntimeExecutionError):
        await tool(restored, backend, []).run_json(arguments(0), CancellationToken(), call_id='write')
    assert len(backend.calls) == 1 and backend.executions == 0


class RealDiscussionApproval(Journey, HarnessOnlyExecution):
    def __init__(self) -> None:
        super().__init__()
        self.approved = False
        self.executions = 0
        self.context = self.context.model_copy(update={'timeout_seconds': 10.0, 'routing_decision': {
            **self.context.routing_decision, 'runtime_timeout_soft_seconds': 10.0,
            'runtime_timeout_absolute_seconds': 20.0}})
        self.runner = AutoGenDiscussionRuntime(self, replace(self.runner._plan, wall_time_seconds=10),
            capability_gateway=self, harness_tool_gateway=self)

    async def cross_soft(self) -> None:
        pass

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        if request.tool_name != 'project.verify':
            return await super().invoke(tenant_id, request, user_id=user_id, role=role)
        self.calls.append(request)
        if not self.approved:
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status='failed', failure_reason='capability requires approval',
                payload={'approval_id': APPROVAL})
        self.executions += 1
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                    status='succeeded', payload={'summary': 'verified'})

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if request.required_tool == 'project_verify' or (
                request.logical_model == 'writer' and request.required_tool is None
                and any(call.tool_name == 'workspace.write_text' for call in self.calls)):
            self.requests.append(request)
            return GatewayCompletion(response=ModelResponse(text=None, tool_calls=(ToolCall(
                id='actual-verify', name='project_verify', arguments={}),), usage=TokenUsage(1, 1, 2)),
                deployment_id='test', logical_model=request.logical_model, provider_id='test',
                provider_model='test/model', cost_usd=Decimal(0))
        return await super().complete_with_context(request)


async def test_actual_autogen_checkpoint_cold_resume_without_replaying_model_or_write() -> None:
    case = RealDiscussionApproval()
    first = [event async for event in case.runner.run(case.context)]
    saved = await case.runner.save_checkpoint()
    assert checkpoint_waits_for_approval(saved, APPROVAL), [(e.kind, e.reason) for e in first[-3:]]
    assert not any(e.kind is EventKind.RUNTIME_COMPLETED for e in first)
    request_count = len(case.requests)
    writes = sum(call.tool_name == 'workspace.write_text' for call in case.calls)
    assert writes == 1 and case.executions == 0
    case.approved = True
    restored = AutoGenDiscussionRuntime(case, case.runner._plan, capability_gateway=case,
        harness_tool_gateway=case, artifact_repository=case.runner._repository)
    await restored.restore_checkpoint(saved)
    stream = event_stream( restored.run(case.context.model_copy(update={'checkpoint': saved})))
    resumed = None
    events = []
    try:
        async for event in stream:
            events.append(event)
            if event.checkpoint is not None:
                rows = event.checkpoint.state['tool_ledger']
                if (isinstance(rows, tuple) and rows and isinstance(rows[-1], Mapping)
                        and rows[-1].get('name') == 'project.verify' and rows[-1].get('status') == 'succeeded'):
                    resumed = event.checkpoint
                    break
    finally:
        await stream.aclose()
    assert resumed is not None, [(e.kind, e.reason) for e in events[-4:]]
    assert case.executions == 1
    assert len(case.requests) == request_count
    assert sum(call.tool_name == 'workspace.write_text' for call in case.calls) == writes


class BatchDiscussionApproval(RealDiscussionApproval):
    def __init__(self, approvals: set[int]) -> None:
        super().__init__()
        self.needed = approvals
        self.granted: set[int] = set()
        self.effects: list[int] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if request.logical_model == 'writer' and any(
                call.tool_name == 'workspace.write_text' for call in self.calls):
            self.requests.append(request)
            calls = (
                ToolCall(id='batch-0', name='workspace_write_text', arguments=arguments(1)),
                ToolCall(id='batch-1', name='project_verify', arguments={}),
                ToolCall(id='batch-2', name='workspace_bundle', arguments={}),
            )
            return GatewayCompletion(response=ModelResponse(text=None, tool_calls=calls,
                usage=TokenUsage(1, 1, 2)), deployment_id='test', logical_model=request.logical_model,
                provider_id='test', provider_model='test/model', cost_usd=Decimal(0))
        return await super().complete_with_context(request)

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        if not request.call_id.startswith('batch-'):
            return await super().invoke(tenant_id, request, user_id=user_id, role=role)
        self.calls.append(request)
        index = int(request.call_id[-1])
        if index in self.needed and index not in self.granted:
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status='failed', failure_reason='capability requires approval',
                payload={'approval_id': f'approval_batch_{index}'})
        self.effects.append(index)
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                    status='succeeded', payload={'summary': 'effect complete'})


@pytest.mark.parametrize('positions', [{0}, {1}, {2}, {0, 1}, {0, 1, 2}])
async def test_ordered_batch_approval_at_first_middle_last_and_repeated_positions(positions: set[int]) -> None:
    case = BatchDiscussionApproval(positions)
    events = [event async for event in case.runner.run(case.context)]
    saved = await case.runner.save_checkpoint()
    request_count = len(case.requests)
    for position in sorted(positions):
        assert checkpoint_waits_for_approval(saved, f'approval_batch_{position}'), [
            (event.kind, event.reason) for event in events[-3:]]
        assert case.effects == list(range(position))
        assert mapping(saved.state['approval_batch'])['next_index'] == position
        case.granted.add(position)
        runner = AutoGenDiscussionRuntime(case, case.runner._plan, capability_gateway=case,
            harness_tool_gateway=case, artifact_repository=case.runner._repository)
        await runner.restore_checkpoint(saved)
        stream = event_stream( runner.run(case.context.model_copy(update={'checkpoint': saved})))
        events = []
        try:
            async for event in stream:
                events.append(event)
                if (event.checkpoint is not None and mapping(event.checkpoint.state.get('approval_batch', {})).get('next_index') == 3):
                    # Resume once to acknowledge this committed result, but do not
                    # commit the next publication: emulate a crash at that boundary.
                    saved = event.checkpoint
                    await anext(stream)
                    break
        finally:
            await stream.aclose()
        if case.effects != [0, 1, 2]:
            saved = await runner.save_checkpoint()
    assert case.effects == [0, 1, 2], [(e.kind, e.reason) for e in events[-3:]]
    assert len(case.requests) == request_count
    cold = AutoGenDiscussionRuntime(case, case.runner._plan, capability_gateway=case,
        harness_tool_gateway=case, artifact_repository=case.runner._repository)
    await cold.restore_checkpoint(saved)
    stream = event_stream( cold.run(case.context.model_copy(update={'checkpoint': saved})))
    compacted = False
    cold_events = []
    try:
        async for event in stream:
            cold_events.append(event)
            if event.checkpoint is not None and 'approval_batch' not in event.checkpoint.state:
                compacted = True
                break
    finally:
        await stream.aclose()
    assert compacted and case.effects == [0, 1, 2] and len(case.requests) == request_count, [
        (e.kind, e.reason) for e in cold_events[-3:]]


async def test_cold_restore_after_one_success_ack_keeps_next_batch_approval() -> None:
    case = BatchDiscussionApproval({0, 1})
    _ = [event async for event in case.runner.run(case.context)]
    saved = await case.runner.save_checkpoint()
    case.granted.add(0)
    runner = AutoGenDiscussionRuntime(case, case.runner._plan, capability_gateway=case,
        harness_tool_gateway=case, artifact_repository=case.runner._repository)
    await runner.restore_checkpoint(saved)
    stream = event_stream( runner.run(case.context.model_copy(update={'checkpoint': saved})))
    try:
        async for event in stream:
            if (event.checkpoint is not None
                    and mapping(event.checkpoint.state.get('approval_batch', {})).get('next_index') == 1):
                saved = event.checkpoint
                await anext(stream)
                break
    finally:
        await stream.aclose()
    assert case.effects == [0]

    cold = AutoGenDiscussionRuntime(case, case.runner._plan, capability_gateway=case,
        harness_tool_gateway=case, artifact_repository=case.runner._repository)
    await cold.restore_checkpoint(saved)
    cold_events = [event async for event in cold.run(case.context.model_copy(update={'checkpoint': saved}))]
    pending = await cold.save_checkpoint()
    assert checkpoint_waits_for_approval(pending, 'approval_batch_1'), [(e.kind, e.reason) for e in cold_events[-3:]]
    assert case.effects == [0]


class FailedBatchCheck(BatchDiscussionApproval):
    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        if request.call_id != 'batch-1':
            return await super().invoke(tenant_id, request, user_id=user_id, role=role)
        self.calls.append(request)
        artifact = draft_artifact(self.context, call=request.call_id)
        result = json_map(artifact.content['result'])
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
            status='failed', failure_reason='generated project draft requires correction',
            payload={'generated_draft_rejection': result['generated_draft_rejection'],
                     'call_id': request.call_id, 'arguments_sha256': artifact.content['arguments_sha256']})


async def test_failed_check_aborts_bundle_suffix_including_cold_restore_after_failure_ack() -> None:
    case = FailedBatchCheck({0})
    _ = [event async for event in case.runner.run(case.context)]
    saved = await case.runner.save_checkpoint()
    case.granted.add(0)
    runner = AutoGenDiscussionRuntime(case, case.runner._plan, capability_gateway=case,
        harness_tool_gateway=case, artifact_repository=case.runner._repository)
    await runner.restore_checkpoint(saved)
    stream = event_stream( runner.run(case.context.model_copy(update={'checkpoint': saved})))
    failed = None
    try:
        async for event in stream:
            if event.checkpoint is not None and mapping(event.checkpoint.state.get('approval_batch', {})).get('next_index') == 2:
                failed = event.checkpoint
                await anext(stream)
                break
    finally:
        await stream.aclose()
    assert failed is not None and case.effects == [0]
    cold = AutoGenDiscussionRuntime(case, case.runner._plan, capability_gateway=case,
        harness_tool_gateway=case, artifact_repository=case.runner._repository)
    await cold.restore_checkpoint(failed)
    stream = event_stream( cold.run(case.context.model_copy(update={'checkpoint': failed})))
    compacted = False
    try:
        async for event in stream:
            if event.checkpoint is not None and 'approval_batch' not in event.checkpoint.state:
                compacted = True
                break
    finally:
        await stream.aclose()
    assert compacted and case.effects == [0]
    assert not any(call.tool_name == 'workspace.bundle' for call in case.calls)


@pytest.mark.parametrize('defect', ['provider_missing', 'logical_missing', 'unrelated_model',
    'invalid_fallback', 'duplicate_call', 'wrong_arguments', 'unknown_tool_prefix', 'unknown_model_prefix'])
async def test_cold_batch_binding_rejects_metadata_corruption_and_unknown_prefix(defect: str) -> None:
    case = BatchDiscussionApproval({1})
    _ = [event async for event in case.runner.run(case.context)]
    saved = await case.runner.save_checkpoint()
    artifacts = await case.runner._hydrate_checkpoint(saved, case.context)
    durable = _durability(artifacts=artifacts)
    durable._context_run_id = case.context.run_id
    durable.tool_entries = list(ledger(saved.state['tool_ledger']))
    durable.model_entries = list(ledger(saved.state['model_ledger']))
    durable.approval_batch = dict(json_map(saved.state['approval_batch']))
    entry = durable.model_entries[-1]
    artifact = durable._artifacts[string(entry['artifact_id'])]
    content = object_map(object_map(json.loads(artifact.model_dump_json()))['content'])
    if defect == 'provider_missing':
        content.pop('provider_id')
    elif defect == 'logical_missing':
        content.pop('logical_model')
    elif defect == 'unrelated_model':
        content['logical_model'] = 'unrelated'
    elif defect == 'invalid_fallback':
        content.update(fallback_used=True, fallback_from_logical_model='unrelated')
    elif defect == 'duplicate_call':
        object_map(object_array(content['tool_calls'])[1])['id'] = object_map(object_array(content['tool_calls'])[0])['id']
    elif defect == 'wrong_arguments':
        object_map(object_array(content['tool_calls'])[0])['arguments'] = {'path': 'foreign', 'content': 'changed'}
    elif defect == 'unknown_tool_prefix':
        durable.tool_entries[0]['status'] = 'running'
    else:
        durable.model_entries.insert(0, {'status': 'running'})
    changed = Artifact(id=artifact.id, type=artifact.type, producer=artifact.producer, content=json_map(content))
    durable._artifacts[str(changed.id)] = changed
    entry['artifact_sha256'] = changed.content_sha256
    durable.approval_batch['model_artifact_sha256'] = changed.content_sha256
    with pytest.raises(RuntimeExecutionError):
        _pending_approval_call(durable, {'implementer': 'writer', 'reviewer': 'reviewer'})
