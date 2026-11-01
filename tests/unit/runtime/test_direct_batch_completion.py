"""Completion controls cannot erase rejected writes or failed verification."""
from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, JsonValue
from agent_hub.runtime.direct import (
    DirectRuntime,
    RuntimeExecutionError,
    _workspace_batch_from_model_text,
)
from agent_hub.runtime.direct_completion_journal import DirectCompletionJournal
from agent_hub.runtime.generated_project_history import generated_failure_current
from agent_hub.runtime.project_scale_artifact import HarnessToolInvoker
from tests.unit.capabilities.test_python_generated_service import approve, configured, request
from tests.unit.runtime.test_direct_generated_entry import Harness, Model, batch, response
from tests.unit.runtime.test_generated_project_history import evidence


def completion() -> str:
    return json.dumps({'workspace_batch': {'files': {}, 'complete': True, 'continuation': ''}})


def unfinished() -> str:
    return json.dumps({'workspace_batch': {'files': {'src/app.py': 'def total(): return 12\n'},
        'complete': False, 'continuation': 'Finish after the acknowledged writes.'}})


@pytest.mark.parametrize('reason', ['workspace file is too large',
    'original project checks are protected from modification'])
async def test_partial_write_rejection_cannot_be_cleared_by_empty_completion(
    tmp_path: Path, reason: str,
) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})

    class RejectSecondWrite(Harness):
        async def execute(self, *, name: str, **kwargs: object) -> Mapping[str, JsonValue]:
            if name == 'workspace.write_text' and self.calls.count(name) == 1:
                self.calls.append(name)
                raise ValueError(reason)
            return await super().execute(name=name, **kwargs)

    harness = RejectSecondWrite(context, verified, bundle)
    first = json.dumps({'workspace_batch': {'files': {
        'src/app.py': 'def total(): return 12\n', 'src/second.py': 'missing = True\n'},
        'complete': False, 'continuation': 'Continue.'}})
    model = Model(response(first), response(completion()), response(completion()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(HarnessToolInvoker, harness))
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(context)]
    assert harness.calls == ['project.inspect', 'workspace.write_text', 'workspace.write_text']
    assert len(model.requests) == 2


async def test_failed_verify_then_repeated_empty_never_reverifies_or_bundles(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    (tmp_path / 'failure').mkdir()
    service, _, repo, executor, _, generated = await configured(
        importlib.import_module('agent_hub.capabilities.python_generated_service'), tmp_path / 'failure')
    approve(repo, await service.authorization_request(request(), generated))
    executor.failure_index = 2
    with pytest.raises(ProjectVerificationError) as caught:
        await service.verify(generated, 'engineer')
    failure = caught.value.python_project_failure
    assert failure is not None and failure.environment_reference is not None
    diagnostic = caught.value.diagnostic
    assert diagnostic is not None
    payload: dict[str, JsonValue] = {'python_verification_failure': failure.to_payload(),
        'execution_diagnostic': diagnostic.to_payload(),
        'python_environment_reference': failure.environment_reference.model_dump(mode='json')}
    assert generated_failure_current(context, payload)

    class FailedVerification(Harness):
        async def invoke(self, tenant: UUID, request: HarnessToolCallRequest,
            **kwargs: object) -> HarnessToolCallResult:
            if request.tool_name == 'project.verify':
                self.calls.append(request.tool_name)
                return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                    status='failed', failure_reason='python project checks failed', payload=payload)
            return await super().invoke(tenant, request, **kwargs)

    harness = FailedVerification(context, verified, bundle)
    model = Model(response(batch()), response(completion()), response(completion()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(HarnessToolInvoker, harness))
    with pytest.raises(RuntimeExecutionError, match='verification repair made no progress'):
        _ = [event async for event in runtime.run(context)]
    assert harness.calls == ['project.inspect', 'workspace.write_text', 'workspace.write_text',
        'project.verify']
    assert len(model.requests) == 2


@pytest.mark.parametrize('terminal', [
    {'workspace_batch': {'files': {}, 'complete': True, 'continuation': '', 'actions': []}},
    {'workspace_batch': {'files': {}, 'complete': True, 'continuation': ''}, 'actions': []},
    {'workspace_bundle': {'files': {}}},
    {'files': {}, 'complete': True, 'continuation': ''},
    {'workspace_batch': {'files': {'../escape.py': ''}, 'complete': True, 'continuation': ''}},
    {'workspace_batch': {'files': {}, 'complete': 1, 'continuation': ''}},
    {'workspace_batch': {'files': {}, 'complete': True, 'continuation': {}}},
    {'workspace_batch': {'files': {}, 'complete': True, 'continuation': 'still working'}},
    {'workspace_batch': {'files': {}, 'complete': True, 'continuation': '', 'read_paths': []}},
])
def test_completion_parser_rejects_extra_actions_unsafe_or_nonstrict_controls(
    terminal: dict[str, object],
) -> None:
    assert _workspace_batch_from_model_text(json.dumps(terminal), allow_completion=True) is None


@pytest.mark.parametrize('suffix', ['\n' + completion(), '\n{"actions": []}', '\nextra prose'])
def test_completion_parser_requires_one_envelope(suffix: str) -> None:
    assert _workspace_batch_from_model_text(completion() + suffix, allow_completion=True) is None


async def test_completion_budget_is_charged_before_verification(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    harness = Harness(context, verified, bundle)
    expensive = ModelResponse(text=completion(), usage=TokenUsage(100, 49_901, 50_001))
    model = Model(response(unfinished()), expensive)
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(HarnessToolInvoker, harness))
    with pytest.raises(RuntimeExecutionError, match='budget'):
        _ = [event async for event in runtime.run(context)]
    assert harness.calls == ['project.inspect', 'workspace.write_text']
    assert len(model.requests) == 2


async def test_completion_cannot_restore_an_expired_deadline(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000, 'timeout_seconds': 0.1})
    harness = Harness(context, verified, bundle)

    class LateCompletion(Model):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if self.requests:
                await asyncio.sleep(0.2)
            return await super().complete_with_context(request)

    model = LateCompletion(response(unfinished()), response(completion()))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=harness,
        harness_tool_gateway=cast(HarnessToolInvoker, harness))
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(context)]
    assert harness.calls == ['project.inspect', 'workspace.write_text']
    assert len(model.requests) == 1


async def test_action_approved_completion_journal_and_terminal_replay_have_no_extra_io(
    tmp_path: Path,
) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})

    class ActionHarness(Harness):
        async def invoke(self, tenant: UUID, request: HarnessToolCallRequest,
            **kwargs: object) -> HarnessToolCallResult:
            if request.tool_name in {'workspace.write_text', 'workspace.bundle'}:
                result = await self.execute(name=request.tool_name)
                return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                    status='succeeded', payload=result)
            return await super().invoke(tenant, request, **kwargs)

    harness = ActionHarness(context, verified, bundle)
    repository = InMemoryArtifactRepository()
    model = Model(response(unfinished()), response(completion()))
    runtime = ActionApprovedDirectRuntime(model, logical_model='fixture', artifact_repository=repository,
        capability_gateway=harness, harness_tool_gateway=cast(HarnessToolInvoker, harness))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert harness.calls == ['project.inspect', 'workspace.write_text', 'workspace.write_text',
        'project.verify', 'workspace.bundle']
    journal = runtime._journal
    assert journal is not None and journal.replay_complete and len(journal.references) == 2
    replay = DirectCompletionJournal(repository, tenant_id=context.tenant_id, run_id=context.run_id,
        references=journal.references)
    no_model = Model()
    before = tuple(harness.calls)
    for original, expected in zip(model.requests, (unfinished(), completion()), strict=True):
        restored = await replay.complete(original, no_model.complete_with_context)
        assert restored.response.text == expected
    assert replay.replay_complete and not no_model.requests and tuple(harness.calls) == before
    checkpoint = await runtime.save_checkpoint()
    restored_runtime = ActionApprovedDirectRuntime(no_model, logical_model='fixture',
        artifact_repository=repository, capability_gateway=harness,
        harness_tool_gateway=cast(HarnessToolInvoker, harness))
    await restored_runtime.restore_checkpoint(checkpoint)
    restored_context = context.model_copy(update={'checkpoint': checkpoint,
        'artifacts': tuple(event.artifact for event in events if event.artifact is not None)})
    restored_events = [event async for event in restored_runtime.run(restored_context)]
    assert len(restored_events) == 1 and restored_events[0].kind is EventKind.RUNTIME_COMPLETED
    assert not no_model.requests and tuple(harness.calls) == before
