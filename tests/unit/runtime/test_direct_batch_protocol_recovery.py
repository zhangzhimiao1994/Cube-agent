"""Offline Direct protocol recovery; Harness receipts are not native execution proof."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from tests.unit.runtime.test_direct_generated_entry import Model, response
from tests.unit.runtime.test_generated_project_history import evidence

INVALID = '{"workspace_batch":{"files":{"src/invalid.py":"NEVER_WRITE"},"complete":"yes"}}'


def batch(path: str, text: str, *, complete: bool) -> str:
    return json.dumps({'workspace_batch': {'files': {path: text}, 'complete': complete,
        'continuation': '' if complete else 'Implement the remaining source.'}})


class Workspace:
    def __init__(self, context: TaskContext, verified: Mapping[str, JsonValue],
                 bundle: Mapping[str, JsonValue], *, draft: bool = False) -> None:
        self.context, self.verified, self.bundle = context, verified, bundle
        self.draft = draft
        self.calls: list[str] = []
        self.files: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []
        self.verifications = 0

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        assert tenant_id == self.context.tenant_id and request.run_id == self.context.run_id
        assert user_id == self.context.actor_id and role == self.context.actor_role
        assert request.actor == 'main_agent'
        self.calls.append(request.tool_name)
        payload: Mapping[str, JsonValue] = self.verified
        if request.tool_name == 'project.inspect':
            payload = {'schema_version': 1, 'scope': {'tenant_id': str(tenant_id),
                'run_id': str(request.run_id), 'project_id': self.context.routing_decision['project_id'],
                'workspace_session_id': self.context.routing_decision['workspace_session_id']},
                'python_build_profile': {'status': 'unavailable', 'reason': 'offline_fixture'}}
        elif request.tool_name == 'project.verify':
            self.verifications += 1
            if self.draft and self.verifications == 1:
                rejection: dict[str, JsonValue] = {'schema_version': 1,
                    'kind': 'generated_project_draft_rejection', 'phase': 'before_capture',
                    'execution_started': False, 'tenant_id': str(tenant_id), 'user_id': str(user_id),
                    'run_id': str(request.run_id), 'project_id': self.context.routing_decision['project_id'],
                    'session_id': self.context.routing_decision['workspace_session_id'],
                    'request_sha256': hashlib.sha256(self.context.request.encode()).hexdigest(),
                    'proposer_actor': 'main_agent', 'observation_sha256': 'a' * 64,
                    'reason': 'generated_acceptance_scaffold_incomplete', 'issues': ('scaffold_incomplete',)}
                return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                    status='failed', failure_reason='generated project draft requires correction',
                    payload={'generated_draft_rejection': rejection, 'call_id': request.call_id,
                        'arguments_sha256': hashlib.sha256(b'{}').hexdigest()})
        else:
            raise AssertionError('unexpected Harness operation')
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
            status='succeeded', payload=payload)

    async def execute(self, *, tenant_id: UUID, run_id: UUID, actor: str, name: str,
                      arguments: Mapping[str, JsonValue], idempotency_key: str) -> Mapping[str, JsonValue]:
        assert tenant_id == self.context.tenant_id and run_id == self.context.run_id
        assert actor == 'main_agent' and idempotency_key
        self.calls.append(name)
        if name == 'workspace.bundle':
            assert self.verifications >= 1
            return self.bundle
        assert name == 'workspace.write_text'
        path, text = arguments['path'], arguments['content']
        assert isinstance(path, str) and isinstance(text, str)
        self.writes.append((path, idempotency_key))
        self.files[path] = text
        return {}

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        return tenant_id == self.context.tenant_id and name == 'project.verify'

    def is_replay_safe(self, name: str) -> bool:
        return False


@pytest.mark.parametrize('draft', [False, True])
async def test_later_invalid_batch_is_corrected_before_verified_delivery(tmp_path: Path, draft: bool) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000, 'routing_decision': {
        **context.routing_decision, 'sandbox_profile': 'workspace_write'}})
    workspace = Workspace(context, verified, bundle, draft=draft)
    first = batch('src/keep.py', 'KEEP\n', complete=draft)
    model = Model(response(first), response(INVALID), response(batch('src/app.py', 'FIXED\n', complete=True)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=workspace)
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 3 and workspace.files['src/keep.py'] == 'KEEP\n'
    assert workspace.files['src/app.py'] == 'FIXED\n' and 'src/invalid.py' not in workspace.files
    assert sum(path == 'src/keep.py' for path, _ in workspace.writes) == 1
    assert len(workspace.writes) == len(set(workspace.writes))
    assert workspace.calls[-2:] == ['project.verify', 'workspace.bundle']
    assert workspace.verifications == (2 if draft else 1)
    assert sum(event.kind is EventKind.MODEL_COMPLETED for event in events) == 3
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert sum(cast(int, event.payload['total_tokens']) for event in events
        if event.kind is EventKind.MODEL_COMPLETED) == 540
    starts = [event for event in events if event.kind is EventKind.MODEL_STARTED]
    completions = [event for event in events if event.kind is EventKind.MODEL_COMPLETED]
    costs = [event for event in events if event.kind is EventKind.COST_RECORDED]
    assert len(starts) == len(completions) == len(costs) == 3
    assert len({event.payload['call_id'] for event in starts}) == 3
    assert [(event.payload['call_id'], event.payload['request_sha256']) for event in starts] == [
        (event.payload['call_id'], event.payload['request_sha256']) for event in completions]
    assert [event.cost_usd for event in costs] == [Decimal('0.01')] * 3
    correction = model.requests[2]
    assert correction.logical_model == model.requests[0].logical_model
    assert not correction.tools and correction.required_tool is None
    assert 0 < correction.timeout_seconds <= model.requests[1].timeout_seconds
    prompt = '\n'.join(cast(str, message.content) for message in correction.messages)
    assert 'workspace_batch_is_invalid' in prompt and 'UNTRUSTED_PROJECT_INSPECTION_JSON' in prompt
    assert context.request in prompt
    for request in model.requests[1:]:
        message = cast(str, request.messages[-1].content)
        assert '"workspace_batch"' in message and '"complete"' in message and '"continuation"' in message
    if draft:
        assert 'UNTRUSTED_GENERATED_DRAFT_JSON' in prompt and 'scaffold_incomplete' in prompt


async def test_later_repeated_invalid_batch_stops_without_writing_rejected_files(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    workspace = Workspace(context, verified, bundle)
    model = Model(response(batch('src/keep.py', 'KEEP', complete=False)), response(INVALID), response(INVALID))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=workspace)
    with pytest.raises(RuntimeExecutionError, match='workspace batch is invalid'):
        _ = [event async for event in runtime.run(context)]
    assert len(model.requests) == 3 and workspace.files == {'src/keep.py': 'KEEP'}
    assert workspace.verifications == 0 and 'workspace.bundle' not in workspace.calls


async def test_rejected_batch_usage_cannot_buy_a_correction(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    workspace = Workspace(context, verified, bundle)
    model = Model(response(batch('src/keep.py', 'KEEP', complete=False)),
        ModelResponse(text=INVALID, usage=TokenUsage(100, 49_721, 49_821)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=workspace)
    with pytest.raises(RuntimeExecutionError, match='budget'):
        _ = [event async for event in runtime.run(context)]
    assert len(model.requests) == 2 and workspace.files == {'src/keep.py': 'KEEP'}
    assert not workspace.verifications


@pytest.mark.parametrize('cancel', [False, True])
async def test_correction_transport_is_never_replayed_and_cancellation_drains(
    tmp_path: Path, cancel: bool,
) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    workspace = Workspace(context, verified, bundle)
    started, stopped = asyncio.Event(), asyncio.Event()

    class InterruptedModel(Model):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) < 2:
                return await super().complete_with_context(request)
            self.requests.append(request)
            started.set()
            if not cancel:
                raise ModelTransportError('unknown outcome', status_code=503)
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            raise AssertionError('unreachable')

    model = InterruptedModel(response(batch('src/keep.py', 'KEEP', complete=False)), response(INVALID))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=workspace)

    async def consume() -> None:
        _ = [event async for event in runtime.run(context)]

    task = asyncio.create_task(consume())
    if cancel:
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
    else:
        with pytest.raises(RuntimeExecutionError):
            await task
    assert len(model.requests) == 3 and workspace.files == {'src/keep.py': 'KEEP'}
    assert not workspace.verifications and 'workspace.bundle' not in workspace.calls


async def test_new_productive_batch_can_correct_protocol_again(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    workspace = Workspace(context, verified, bundle)
    model = Model(response(batch('src/keep.py', 'KEEP', complete=False)), response(INVALID),
        response(batch('src/second.py', 'PROGRESS', complete=False)), response(INVALID),
        response(batch('src/app.py', 'FIXED', complete=True)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=workspace)
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED and len(model.requests) == 5
    assert workspace.files['src/second.py'] == 'PROGRESS' and 'src/invalid.py' not in workspace.files
    assert sum(path == 'src/keep.py' for path, _ in workspace.writes) == 1
    assert workspace.calls[-2:] == ['project.verify', 'workspace.bundle']


@pytest.mark.parametrize('unreported_large_arguments', [False, True])
@pytest.mark.parametrize('initial', [False, True])
async def test_native_requests_stay_data_and_missing_usage_accounts_arguments(
    tmp_path: Path, unreported_large_arguments: bool, initial: bool,
) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 10_000 if unreported_large_arguments else 50_000})
    workspace = Workspace(context, verified, bundle)
    rejected = ModelResponse(text='This prefix must not hide the tool arguments.', tool_calls=(ToolCall('ignored', 'command.run', {
        'command': 'x' * 40_000 if unreported_large_arguments else 'never execute this model request',
    }),), usage=None if unreported_large_arguments else TokenUsage(100, 80, 180))
    first = () if initial else (response(batch('src/keep.py', 'KEEP', complete=False)),)
    model = Model(*first, rejected,
        response(batch('src/app.py', 'FIXED', complete=True)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=workspace)
    if unreported_large_arguments:
        with pytest.raises(RuntimeExecutionError, match='budget'):
            _ = [event async for event in runtime.run(context)]
        assert len(model.requests) == (1 if initial else 2)
        assert workspace.files == ({} if initial else {'src/keep.py': 'KEEP'})
        assert not workspace.verifications and 'workspace.bundle' not in workspace.calls
    else:
        events = [event async for event in runtime.run(context)]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(model.requests) == (2 if initial else 3)
        assert workspace.calls[-2:] == ['project.verify', 'workspace.bundle']
    assert 'command.run' not in workspace.calls
