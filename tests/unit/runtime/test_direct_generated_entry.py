"""Direct's TEXT/batch adapter uses actual Harness results, never native proof."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.contracts import EventKind, JsonValue, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.direct import DirectRunStream, DirectRuntime, RuntimeExecutionError
from tests.unit.runtime.test_generated_project_history import evidence


class Harness:
    def __init__(self, context: Any, verified: Mapping[str, JsonValue], bundle: Mapping[str, JsonValue]) -> None:
        self.context, self.verified, self.bundle = context, verified, bundle
        self.calls: list[str] = []
        self.inspect_defect: str | None = None
        self.started, self.stopped = asyncio.Event(), asyncio.Event()
        self.profile: dict[str, JsonValue] = {'status': 'configured_not_probed', 'catalog_sha256': 'd' * 64,
            'interpreter': {'binary_sha256': 'e' * 64}, 'wheels': ({'sha256': 'f' * 64},)}

    async def invoke(self, tenant: Any, request: Any, **kw: Any) -> HarnessToolCallResult:
        assert tenant == self.context.tenant_id and request.run_id == self.context.run_id
        assert request.actor == 'main_agent' and kw['user_id'] == self.context.actor_id
        assert kw['role'] == self.context.actor_role
        self.calls.append(request.tool_name)
        payload = self.verified
        if request.tool_name == 'read_context':
            payload = {'path': request.arguments['path'], 'text': 'def total(): return 0\n', 'truncated': False}
        if request.tool_name == 'project.inspect':
            assert request.arguments == {}
            self.started.set()
            if self.inspect_defect == 'blocking':
                try:
                    await asyncio.Event().wait()
                finally:
                    self.stopped.set()
            payload = {'schema_version': 1, 'scope': {'tenant_id': str(tenant),
                'run_id': str(request.run_id), 'project_id': self.context.routing_decision['project_id'],
                'workspace_session_id': self.context.routing_decision['workspace_session_id']},
                'python_build_profile': self.profile}
            if self.inspect_defect == 'scope':
                payload = {**payload, 'scope': {}}
            elif self.inspect_defect == 'approval':
                payload = {'approval_id': 'not-approved'}
        return HarnessToolCallResult(call_id='wrong' if self.inspect_defect == 'call' else request.call_id,
            tool_name=request.tool_name, status='failed' if self.inspect_defect == 'approval' else 'succeeded',
            payload=payload, truncated=self.inspect_defect == 'truncated')

    async def execute(self, *, name: str, **kw: Any) -> Mapping[str, JsonValue]:
        self.calls.append(name)
        assert name in {'workspace.write_text', 'workspace.bundle'}
        return self.bundle if name == 'workspace.bundle' else {}

    def is_available(self, tenant: Any, name: str) -> bool:
        return True

    def is_replay_safe(self, name: str) -> bool:
        return False


class Model:
    def __init__(self, *responses: ModelResponse) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        assert self.responses, 'unbounded correction/model replay'
        return GatewayCompletion(response=self.responses.pop(0), deployment_id='fixture',
            logical_model=request.logical_model, provider_id='fixture', provider_model='fixture/model',
            attempted_logical_models=(request.logical_model,), cost_usd=Decimal('0.01'))


def response(text: str | None, *, tools: tuple[ToolCall, ...] = ()) -> ModelResponse:
    return ModelResponse(text=text, tool_calls=tools, usage=TokenUsage(100, 80, 180))


def batch() -> str:
    return json.dumps({'workspace_batch': {'files': {'src/app.py': 'def total(): return 12\n'},
        'complete': True, 'continuation': ''}, 'summary': 'Completed implementation.'})


@pytest.mark.parametrize('first', ['valid', 'planning', 'tool-json', 'native'])
async def test_generated_entry_inspects_before_model_and_recovers_protocol_once(tmp_path: Path, first: str) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    harness = Harness(context, verified, bundle)
    initial = {'valid': response(batch()), 'planning': response('I will inspect the workspace first.'),
        'tool-json': response('{"tool_calls":[{"name":"project.inspect","arguments":{}}]}'),
        'native': response(None, tools=(ToolCall('call-1', 'project.inspect', {}),))}[first]
    model = Model(initial, *(() if first == 'valid' else (response(batch()),)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert harness.calls == ['project.inspect', 'workspace.write_text', 'workspace.write_text',
        'project.verify', 'workspace.bundle']
    assert len(model.requests) == (1 if first == 'valid' else 2)
    first_prompt = '\n'.join(cast(str, message.content) for message in model.requests[0].messages)
    assert 'd' * 64 in first_prompt and 'f' * 64 in first_prompt
    assert 'UNTRUSTED_PROJECT_INSPECTION_JSON' in first_prompt
    assert 'structured workspace_batch' in first_prompt
    assert all(not request.tools for request in model.requests)
    starts = [event for event in events if event.kind is EventKind.MODEL_STARTED]
    completed = [event for event in events if event.kind is EventKind.MODEL_COMPLETED]
    costs = [event for event in events if event.kind is EventKind.COST_RECORDED]
    assert len(starts) == len(completed) == len(costs) == len(model.requests)
    assert len({event.payload['call_id'] for event in starts}) == len(starts)
    assert [(event.payload['call_id'], event.payload['request_sha256']) for event in starts] == [
        (event.payload['call_id'], event.payload['request_sha256']) for event in completed]
    assert all(event.cost_usd == Decimal('0.01') for event in costs)
    assert sum(cast(int, event.payload['total_tokens']) for event in completed) == 180 * len(model.requests)
    assert all(context.request in ''.join(cast(str, m.content) for m in req.messages) for req in model.requests)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    observations = [event.artifact for event in events if event.artifact is not None
        and event.artifact.content.get('tool_name') == 'project.inspect']
    assert len(observations) == 1
    observed = observations[0].content['result']
    assert isinstance(observed, Mapping) and observed['python_build_profile'] == harness.profile


@pytest.mark.parametrize('defect', ['scope', 'approval', 'call', 'truncated'])
async def test_inspect_failure_never_calls_model_or_writes(tmp_path: Path, defect: str) -> None:
    context, verified, bundle = await evidence(tmp_path)
    harness = Harness(context, verified, bundle)
    harness.inspect_defect = defect
    model = Model(response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(context)]
    assert not model.requests and harness.calls == ['project.inspect']


async def test_continuous_invalid_output_is_bounded_and_never_writes(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    harness = Harness(context, verified, bundle)
    model = Model(response('I will inspect.'), response('All done.'))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(context)]
    assert len(model.requests) == 2 and harness.calls == ['project.inspect']


async def test_inspect_cancellation_drains_before_any_model_or_write(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    harness = Harness(context, verified, bundle)
    harness.inspect_defect = 'blocking'
    model = Model(response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))

    async def consume() -> None:
        _ = [event async for event in runtime.run(context)]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(harness.started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert harness.stopped.is_set() and not model.requests and harness.calls == ['project.inspect']


async def test_generated_marker_does_not_inspect_or_force_chat_to_deliver() -> None:
    from uuid import uuid4

    from agent_hub.domain.runs import TaskMode

    context = TaskContext(tenant_id=uuid4(), run_id=uuid4(), mode=TaskMode.DIRECT,
        request='hello', routing_decision={'generated_project_contract_version': 1})
    model = Model(response('Hello.'))
    runtime = DirectRuntime(model, logical_model='fixture')
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not any(event.kind is EventKind.TOOL_STARTED for event in events)
    assert 'DIRECT_OUTPUT_CONTRACT' not in str(model.requests[0].messages)
    checkpoint = await runtime.save_checkpoint()
    restored_model = Model()
    restored = DirectRuntime(restored_model, logical_model='fixture')
    await restored.restore_checkpoint(checkpoint)
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    assert artifacts and all('workspace_delivery' not in artifact.content for artifact in artifacts)
    replayed = [event async for event in restored.run(context.model_copy(update={
        'checkpoint': checkpoint, 'artifacts': artifacts}))]
    assert len(replayed) == 1 and replayed[0].kind is EventKind.RUNTIME_COMPLETED
    assert not restored_model.requests


async def test_protocol_correction_cannot_buy_extra_token_budget(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    harness = Harness(context, verified, bundle)
    model = Model(ModelResponse(text='I will inspect.', usage=TokenUsage(100, 16284, 16384)), response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    with pytest.raises(RuntimeExecutionError, match='budget'):
        _ = [event async for event in runtime.run(context)]
    assert len(model.requests) == 1 and harness.calls == ['project.inspect']


async def test_generated_structured_read_paths_are_executed_not_tool_requests(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    harness = Harness(context, verified, bundle)
    read = json.dumps({'workspace_batch': {'files': {}, 'read_paths': ['src/app.py'],
        'complete': False, 'continuation': 'Read existing implementation, then implement totals.'}})
    model = Model(response(read), response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert harness.calls[:2] == ['project.inspect', 'read_context']
    assert 'UNTRUSTED_WORKSPACE_READ_JSON' in '\n'.join(
        cast(str, message.content) for message in model.requests[1].messages)
    assert sum(event.kind is EventKind.MODEL_COMPLETED for event in events) == 2


async def test_model_start_is_persist_boundary_before_any_generated_transport(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    harness = Harness(context, verified, bundle)
    model = Model(response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    stream = runtime.run(context)
    assert isinstance(stream, DirectRunStream)
    try:
        while (await anext(stream)).kind is not EventKind.MODEL_STARTED:
            pass
        await asyncio.sleep(0)
        assert model.requests == []
    finally:
        await stream.aclose()  # Simulates cancelled/lost lease before next generator drive.
    assert model.requests == [] and harness.calls == ['project.inspect']


async def test_unknown_transport_failure_has_no_automatic_generated_replay(tmp_path: Path) -> None:
    from agent_hub.models.litellm_client import ModelTransportError

    context, verified, bundle = await evidence(tmp_path)
    harness = Harness(context, verified, bundle)

    class UnknownModel(Model):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            raise ModelTransportError('unknown transport result', status_code=503)

    model = UnknownModel()
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(context)]
    assert len(model.requests) == 1 and harness.calls == ['project.inspect']


@pytest.mark.parametrize('corrected', [False, True])
async def test_completed_generated_checkpoint_restores_without_model_or_tool_replay(tmp_path: Path, corrected: bool) -> None:
    context, verified, bundle = await evidence(tmp_path)
    harness = Harness(context, verified, bundle)
    model = Model(*((response('I will inspect.'),) if corrected else ()), response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events = [event async for event in runtime.run(context)]
    checkpoint = await runtime.save_checkpoint()
    next_sequence = checkpoint.state['next_sequence']
    assert type(next_sequence) is int and next_sequence >= 7
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    recovered_context = context.model_copy(update={'checkpoint': checkpoint, 'artifacts': artifacts})
    fresh_model = Model()
    fresh = DirectRuntime(fresh_model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    before = tuple(harness.calls)
    await fresh.restore_checkpoint(checkpoint)
    replayed = [event async for event in fresh.run(recovered_context)]
    assert len(replayed) == 1 and replayed[0].kind is EventKind.RUNTIME_COMPLETED
    assert not fresh_model.requests and tuple(harness.calls) == before
    # Even a self-consistent forged offset must match the durable delivery layout.
    for defect in ('sequence', 'counts', 'missing', 'inspection'):
        state: Any = json.loads(json.dumps(checkpoint.state, default=dict))
        if defect == 'sequence':
            state['next_sequence'] += 1
        elif defect == 'counts':
            state['generated_event_layout']['model_call_count'] += 1
            state['next_sequence'] += 2
        elif defect == 'inspection':
            state['generated_event_layout']['inspection_artifact_sha256'] = '0' * 64
        else:
            state.pop('generated_event_layout')
            state.pop('model_scope_event_count', None)
            state['next_sequence'] = 4
        bad = RuntimeCheckpoint(id=checkpoint.id, runtime_type=checkpoint.runtime_type,
            runtime_version=checkpoint.runtime_version, run_id=context.run_id,
            tenant_id=context.tenant_id, mode=context.mode, state=state)
        rejected = DirectRuntime(fresh_model, logical_model='fixture', capability_gateway=harness,
            harness_tool_gateway=cast(Any, harness))
        with pytest.raises(RuntimeExecutionError):
            await rejected.restore_checkpoint(bad)
            _ = [event async for event in rejected.run(recovered_context.model_copy(update={'checkpoint': bad}))]
        assert not fresh_model.requests and tuple(harness.calls) == before


async def test_generated_inspect_timeout_drains_without_model_or_write(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'timeout_seconds': 0.05})
    harness = Harness(context, verified, bundle)
    harness.inspect_defect = 'blocking'
    model = Model(response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    with pytest.raises(RuntimeExecutionError, match='inspection failed'):
        _ = [event async for event in runtime.run(context)]
    assert harness.stopped.is_set() and not model.requests and harness.calls == ['project.inspect']


async def test_unchanged_generated_read_does_not_buy_another_model_round(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    harness = Harness(context, verified, bundle)
    read = json.dumps({'workspace_batch': {'files': {}, 'read_paths': ['src/app.py'],
        'complete': False, 'continuation': 'Inspect implementation.'}})
    model = Model(response(read), response(read), response(batch()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    with pytest.raises(RuntimeExecutionError, match='no progress'):
        _ = [event async for event in runtime.run(context)]
    assert len(model.requests) == 2 and harness.calls == ['project.inspect', 'read_context', 'read_context']
