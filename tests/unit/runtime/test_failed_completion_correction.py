"""A known failed verification needs a correction, not a false completion or replay."""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime import direct
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from agent_hub.runtime.project_scale_artifact import HarnessToolInvoker
from tests.unit.capabilities.test_python_generated_service import approve, configured, request
from tests.unit.runtime.test_direct_generated_entry import Harness, Model, batch, response
from tests.unit.runtime.test_generated_project_history import evidence


def complete() -> str:
    return json.dumps({'workspace_batch': {'files': {}, 'complete': True, 'continuation': ''}})


async def failed_workspace(tmp_path: Path, *, failed_verifications: int = 1) -> tuple[TaskContext, Harness]:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    failure_root = tmp_path / 'failure'
    failure_root.mkdir()
    service, _, repo, executor, _, generated = await configured(
        importlib.import_module('agent_hub.capabilities.python_generated_service'), failure_root)
    approve(repo, await service.authorization_request(request(), generated))
    executor.failure_index = 2
    with pytest.raises(ProjectVerificationError) as caught:
        await service.verify(generated, 'engineer')
    failure, diagnostic = caught.value.python_project_failure, caught.value.diagnostic
    assert failure is not None and diagnostic is not None and failure.environment_reference is not None
    payload: dict[str, JsonValue] = {'python_verification_failure': failure.to_payload(),
        'execution_diagnostic': diagnostic.to_payload(),
        'python_environment_reference': failure.environment_reference.model_dump(mode='json')}

    class Workspace(Harness):
        verification_count = 0

        async def invoke(self, tenant: UUID, request: HarnessToolCallRequest,
                         **kwargs: object) -> HarnessToolCallResult:
            if request.tool_name == 'project.verify':
                self.verification_count += 1
                if self.verification_count <= failed_verifications:
                    self.calls.append(request.tool_name)
                    return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                        status='failed', failure_reason='python project checks failed', payload=payload)
            return await super().invoke(tenant, request, **kwargs)

    return context, Workspace(context, verified, bundle)


def prompt(request: ModelRequest) -> str:
    return '\n'.join(cast(str, message.content) for message in request.messages)


async def test_failed_completion_gets_correction_then_changed_source_verifies(tmp_path: Path) -> None:
    context, workspace = await failed_workspace(tmp_path)
    repaired = json.dumps({'workspace_batch': {'files': {'src/app.py': 'def total(): return 13\n'},
        'complete': True, 'continuation': ''}})
    model = Model(response(batch()), response(complete()), response(repaired))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=cast(HarnessToolInvoker, workspace))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 3 and not model.responses
    assert workspace.calls.count('project.verify') == 2
    assert workspace.calls.count('workspace.bundle') == 1
    correction = prompt(model.requests[2])
    assert 'previously failed source' in correction
    assert 'python_verification_failure' in correction
    assert 'UNTRUSTED_PROJECT_VERIFICATION_JSON' in correction
    assert 'changed file contents' in correction and 'protected' in correction
    assert all(not r.tools for r in model.requests)
    assert 0 < model.requests[2].timeout_seconds <= model.requests[1].timeout_seconds
    assert sum(cast(int, e.payload['total_tokens']) for e in events
        if e.kind is EventKind.MODEL_COMPLETED) == 540


async def test_second_unchanged_completion_stops_without_reverification(tmp_path: Path) -> None:
    context, workspace = await failed_workspace(tmp_path)
    model = Model(response(batch()), response(complete()), response(complete()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=cast(HarnessToolInvoker, workspace))
    with pytest.raises(RuntimeExecutionError, match='verification repair made no progress'):
        _ = [event async for event in runtime.run(context)]
    assert len(model.requests) == 3 and not model.responses
    assert workspace.calls.count('project.verify') == 1
    assert 'workspace.bundle' not in workspace.calls


async def test_failed_completion_can_inspect_before_repairing(tmp_path: Path) -> None:
    context, workspace = await failed_workspace(tmp_path)
    context = context.model_copy(update={'routing_decision': {**context.routing_decision,
        'engineering_diagnostic_reads_version': 1, 'sandbox_profile': 'workspace_write'}})
    read = json.dumps({'workspace_batch': {'files': {}, 'read_paths': ['src/app.py'],
        'complete': False, 'continuation': 'Inspect the failing implementation before editing.'}})
    repaired = json.dumps({'workspace_batch': {'files': {'src/app.py': 'def total(): return 13\n'},
        'complete': True, 'continuation': ''}})
    model = Model(response(batch()), response(complete()), response(read), response(repaired))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=cast(HarnessToolInvoker, workspace))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 4 and not model.responses
    assert workspace.calls.count('read_context') == 1
    assert workspace.calls.count('project.verify') == 2
    assert 'UNTRUSTED_WORKSPACE_READ_JSON' in prompt(model.requests[3])
    assert 'python_verification_failure' in prompt(model.requests[3])


async def test_failed_completion_cannot_buy_extra_token_budget(tmp_path: Path) -> None:
    context, workspace = await failed_workspace(tmp_path)
    model = Model(response(batch()), ModelResponse(text=complete(), usage=TokenUsage(100, 49_721, 49_821)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=cast(HarnessToolInvoker, workspace))
    with pytest.raises(RuntimeExecutionError, match='budget'):
        _ = [event async for event in runtime.run(context)]
    assert len(model.requests) == 2
    assert workspace.calls.count('project.verify') == 1
    assert 'workspace.bundle' not in workspace.calls


async def test_reverting_failed_source_earns_no_progress_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, workspace = await failed_workspace(tmp_path)
    original_limit = direct._workspace_delivery_token_limit
    observed_batches: list[int] = []

    def limit(ctx: TaskContext, **kwargs: int) -> int:
        observed_batches.append(kwargs['completed_batches'])
        return original_limit(ctx, **kwargs)

    monkeypatch.setattr(direct, '_workspace_delivery_token_limit', limit)
    def update(text: str, *, done: bool) -> str:
        return json.dumps({'workspace_batch': {'files': {'src/app.py': text}, 'complete': done,
            'continuation': '' if done else 'Still repairing.'}})

    model = Model(response(batch()), response(update('def total(): return 10\n', done=False)),
        response(batch()), response(update('def total(): return 13\n', done=True)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=cast(HarnessToolInvoker, workspace))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 4 and not model.responses
    assert observed_batches == [0, 1, 2, 3]
    assert workspace.calls.count('project.verify') == 2
    assert 'previously failed source' in prompt(model.requests[3])


async def test_revert_to_older_failure_cannot_use_latest_other_source_receipt(tmp_path: Path) -> None:
    context, workspace = await failed_workspace(tmp_path, failed_verifications=2)
    other = json.dumps({'workspace_batch': {'files': {'src/app.py': 'def total(): return 10\n'},
        'complete': True, 'continuation': ''}})
    # The adapter fixture returns validated failures for two acknowledged source
    # states. A's correction must not borrow the latest observation from B.
    model = Model(response(batch()), response(other), response(batch()), response(complete()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=cast(HarnessToolInvoker, workspace))
    with pytest.raises(RuntimeExecutionError, match='verification repair made no progress'):
        _ = [event async for event in runtime.run(context)]
    assert len(model.requests) == 3 and len(model.responses) == 1
    assert workspace.calls.count('project.verify') == 2
    assert 'workspace.bundle' not in workspace.calls


@pytest.mark.parametrize('cancel', [False, True])
async def test_failed_completion_correction_unknown_or_cancel_is_not_replayed(
    tmp_path: Path, cancel: bool,
) -> None:
    context, workspace = await failed_workspace(tmp_path)
    started, drained = asyncio.Event(), asyncio.Event()

    class Interrupted(Model):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) < 2:
                return await super().complete_with_context(request)
            self.requests.append(request)
            started.set()
            if not cancel:
                raise ModelTransportError('unknown transport outcome', status_code=503)
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
            raise AssertionError('unreachable')

    model = Interrupted(response(batch()), response(complete()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=cast(HarnessToolInvoker, workspace))

    async def consume() -> None:
        _ = [event async for event in runtime.run(context)]

    task = asyncio.create_task(consume())
    if cancel:
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert drained.is_set()
    else:
        with pytest.raises(RuntimeExecutionError):
            await task
    assert len(model.requests) == 3 and not model.responses
    assert workspace.calls.count('project.verify') == 1
    assert 'workspace.bundle' not in workspace.calls
