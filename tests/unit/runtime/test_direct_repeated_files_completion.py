"""Completion and bounded no-progress correction use acknowledged bytes, never replay authority."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime import direct
from agent_hub.runtime.contracts import EventKind, JsonValue
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError, _written_file_manifest
from tests.unit.runtime.test_direct_batch_protocol_recovery import Workspace, batch
from tests.unit.runtime.test_direct_generated_entry import Model, response
from tests.unit.runtime.test_generated_project_history import evidence


async def test_same_written_files_with_new_complete_control_verify_without_rewrite(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    workspace = Workspace(context, verified, bundle)
    files = {'src/app.py': 'def total(): return 12\n'}
    first = json.dumps({'workspace_batch': {'files': files, 'complete': False,
        'continuation': 'Implementation written; finish through real project verification.'}})
    completed = json.dumps({'workspace_batch': {'files': files, 'complete': True, 'continuation': ''},
        'summary': 'Ready for actual verification; repeated file bytes are unchanged.'})
    model = Model(response(first), response(completed))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace,
        harness_tool_gateway=workspace)

    try:
        events = [event async for event in runtime.run(context)]
    finally:
        # These hold on the RED too: the first write really reached the capability
        # fixture, and the repeated bytes never reached it a second time.
        assert workspace.files['src/app.py'] == files['src/app.py']
        assert sum(path == 'src/app.py' for path, _key in workspace.writes) == 1
        assert len(workspace.writes) == len(set(workspace.writes))
        assert len(model.requests) == 2 and not model.responses

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert workspace.verifications == 1
    assert workspace.calls[-2:] == ['project.verify', 'workspace.bundle']
    assert workspace.calls.count('workspace.bundle') == 1
    assert sum(event.kind is EventKind.MODEL_COMPLETED for event in events) == 2
    assert sum(cast(int, event.payload['total_tokens']) for event in events
        if event.kind is EventKind.MODEL_COMPLETED) == 360
    assert 0 < model.requests[1].timeout_seconds <= model.requests[0].timeout_seconds


def continuation_progress(request: ModelRequest) -> Mapping[str, object]:
    for message in reversed(request.messages):
        if isinstance(message.content, str) and 'Progress: ' in message.content:
            value, _end = json.JSONDecoder().raw_decode(message.content.rsplit('Progress: ', 1)[1])
            assert isinstance(value, Mapping)
            return cast(Mapping[str, object], value)
    raise AssertionError('continuation must contain acknowledged progress')


async def test_repeated_unfinished_batch_gets_one_correction_then_new_files_complete(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000, 'routing_decision': {
        **context.routing_decision, 'sandbox_profile': 'workspace_write'}})
    workspace = Workspace(context, verified, bundle)
    first = batch('src/app.py', 'ORIGINAL_CONTENT', complete=False)
    model = Model(response(first), response(first), response(batch('src/next.py', 'NEW_CONTENT', complete=True)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace, harness_tool_gateway=workspace)
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 3 and not model.responses
    assert sum(path == 'src/app.py' for path, _ in workspace.writes) == 1
    assert sum(path == 'src/next.py' for path, _ in workspace.writes) == 1
    assert workspace.verifications == 1 and workspace.calls.count('workspace.bundle') == 1
    progress = continuation_progress(model.requests[2])
    assert progress['validation_error'] == 'no_progress' and progress['written_file_count'] == 1
    written = progress['written_files']
    assert isinstance(written, Mapping)
    assert written['entries_complete'] is True and written['file_count'] == 1
    assert written['entries'] == [{'path': 'src/app.py', 'sha256': hashlib.sha256(b'ORIGINAL_CONTENT').hexdigest()}]
    prompt = '\n'.join(cast(str, msg.content) for msg in model.requests[2].messages)
    assert 'no progress' in prompt and 'ORIGINAL_CONTENT' not in prompt
    assert all(not req.tools and req.required_tool is None for req in model.requests)
    assert 0 < model.requests[2].timeout_seconds <= model.requests[1].timeout_seconds
    assert sum(cast(int, event.payload['total_tokens']) for event in events
        if event.kind is EventKind.MODEL_COMPLETED) == 540


async def test_same_source_second_nonproductive_batch_stops_after_one_correction(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    workspace = Workspace(context, verified, bundle)
    unchanged = batch('src/app.py', 'KEEP', complete=False)
    model = Model(response(unchanged), response(unchanged), response(unchanged))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace, harness_tool_gateway=workspace)
    with pytest.raises(RuntimeExecutionError, match='workspace batch made no progress'):
        _ = [event async for event in runtime.run(context)]
    assert len(model.requests) == 3 and not model.responses
    assert workspace.files == {'src/app.py': 'KEEP'} and len(workspace.writes) == 1
    assert workspace.verifications == 0 and 'workspace.bundle' not in workspace.calls


async def test_failed_source_repeat_completion_never_reverifies(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000, 'routing_decision': {
        **context.routing_decision, 'sandbox_profile': 'workspace_write'}})
    workspace = Workspace(context, verified, bundle, draft=True)
    complete = batch('src/app.py', 'KEEP', complete=True)
    model = Model(response(complete), response(complete))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace, harness_tool_gateway=workspace)
    with pytest.raises(RuntimeExecutionError, match='workspace verification repair made no progress'):
        _ = [event async for event in runtime.run(context)]
    assert workspace.verifications == 1 and 'workspace.bundle' not in workspace.calls
    assert len(model.requests) == 2 and not model.responses
    assert sum(path == 'src/app.py' for path, _ in workspace.writes) == 1
    assert 'generated_draft_rejection' in '\n'.join(cast(str, msg.content) for msg in model.requests[1].messages)


class UnknownWorkspace(Workspace):
    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        result = await super().invoke(tenant_id, request, user_id=user_id, role=role)
        if request.tool_name != 'project.verify':
            return result
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
            status='failed', failure_reason='verification outcome unknown', payload={})


async def test_unknown_verification_never_turns_into_completion_retry(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    workspace = UnknownWorkspace(context, verified, bundle)
    complete = batch('src/app.py', 'KEEP', complete=True)
    model = Model(response(complete), response(complete))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace, harness_tool_gateway=workspace)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(context)]
    assert workspace.verifications == 1 and 'workspace.bundle' not in workspace.calls
    assert len(model.requests) == 1 and len(model.responses) == 1


async def test_repeated_batch_usage_cannot_buy_extra_correction_budget(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    workspace = Workspace(context, verified, bundle)
    unchanged = batch('src/app.py', 'KEEP', complete=False)
    model = Model(response(unchanged), ModelResponse(text=unchanged, usage=TokenUsage(100, 49_721, 49_821)),
                  response(batch('src/app.py', 'NEW', complete=True)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace, harness_tool_gateway=workspace)
    with pytest.raises(RuntimeExecutionError, match='budget'):
        _ = [event async for event in runtime.run(context)]
    assert len(model.requests) == 2 and len(model.responses) == 1
    assert workspace.files == {'src/app.py': 'KEEP'} and workspace.verifications == 0


async def test_regular_continuation_has_written_path_and_digest(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    workspace = Workspace(context, verified, bundle)
    model = Model(response(batch('src/first.py', 'KEEP', complete=False)),
                  response(batch('src/app.py', 'NEW', complete=True)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace, harness_tool_gateway=workspace)
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    written = continuation_progress(model.requests[1])['written_files']
    assert isinstance(written, Mapping)
    assert written['entries_complete'] is True and written['file_count'] == 1
    assert written['entries'] == [{'path': 'src/first.py', 'sha256': hashlib.sha256(b'KEEP').hexdigest()}]


def test_large_written_manifest_is_bounded_recent_subset_with_full_digest() -> None:
    files = {f'src/file_{i:05}.py': hashlib.sha256(str(i).encode()).hexdigest() for i in range(1000)}
    value = _written_file_manifest(files, token_allowance=256)
    assert value is not None
    raw = json.dumps(value, ensure_ascii=True, separators=(',', ':'))
    assert direct._estimate_text_tokens(raw) <= 256
    assert value['file_count'] == 1000 and value['entries_complete'] is False
    entries = value['entries']
    assert isinstance(entries, tuple) and 0 < len(entries) < 1000
    assert entries[0] == {'path': 'src/file_00999.py', 'sha256': files['src/file_00999.py']}
    assert value['sha256'] == hashlib.sha256(json.dumps(files, ensure_ascii=True,
        sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()
    for entry in entries:
        assert isinstance(entry, Mapping)
        path = entry['path']
        assert isinstance(path, str) and files[path] == entry['sha256']
    assert _written_file_manifest(files, token_allowance=0) is None
    assert _written_file_manifest(files, token_allowance=1) is None


@pytest.mark.parametrize('cancel', [False, True])
async def test_no_progress_correction_unknown_and_cancel_never_replay(tmp_path: Path, cancel: bool) -> None:
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
    unchanged = batch('src/app.py', 'KEEP', complete=False)
    model = InterruptedModel(response(unchanged), response(unchanged))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace, harness_tool_gateway=workspace)
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
    assert len(model.requests) == 3 and workspace.verifications == 0
    assert workspace.files == {'src/app.py': 'KEEP'} and len(workspace.writes) == 1


class RejectedWriteWorkspace(Workspace):
    rejected = 0

    async def execute(self, *, tenant_id: UUID, run_id: UUID, actor: str, name: str,
                      arguments: Mapping[str, JsonValue], idempotency_key: str) -> Mapping[str, JsonValue]:
        if name == 'workspace.write_text' and arguments.get('path') == 'src/blocked.py':
            assert tenant_id == self.context.tenant_id and run_id == self.context.run_id
            assert actor == 'main_agent' and idempotency_key
            self.calls.append(name)
            self.rejected += 1
            raise ValueError('original project checks are protected from modification')
        return await super().execute(tenant_id=tenant_id, run_id=run_id, actor=actor, name=name,
                                     arguments=arguments, idempotency_key=idempotency_key)

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        if request.tool_name != 'read_context':
            return await super().invoke(tenant_id, request, user_id=user_id, role=role)
        assert tenant_id == self.context.tenant_id and request.run_id == self.context.run_id
        assert user_id == self.context.actor_id and role == self.context.actor_role
        assert request.actor == 'main_agent' and request.arguments == {'path': 'src/app.py'}
        self.calls.append(request.tool_name)
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name, status='succeeded',
            payload={'path': 'src/app.py', 'text': self.files['src/app.py'], 'truncated': False})


@pytest.mark.parametrize('read', [False, True])
async def test_rejected_write_cannot_be_erased_by_same_valid_files_complete(tmp_path: Path, read: bool) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    workspace = RejectedWriteWorkspace(context, verified, bundle)
    initial = json.dumps({'workspace_batch': {'files': {'src/app.py': 'KEEP', 'src/blocked.py': 'DENIED'},
        'complete': False, 'continuation': 'Only acknowledged files may finish.'}})
    inspect = json.dumps({'workspace_batch': {'files': {}, 'read_paths': ['src/app.py'],
        'complete': False, 'continuation': 'Reading does not clear the rejected write.'}})
    model = Model(response(initial), *((response(inspect),) if read else ()),
                  response(batch('src/app.py', 'KEEP', complete=True)),
                  response(batch('src/other.py', 'MUST_NOT_EXECUTE', complete=True)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace, harness_tool_gateway=workspace)
    with pytest.raises(RuntimeExecutionError, match='workspace batch is invalid'):
        _ = [event async for event in runtime.run(context)]
    assert workspace.files == {'src/app.py': 'KEEP'} and workspace.rejected == 1
    assert len(workspace.writes) == 1 and workspace.writes[0][0] == 'src/app.py'
    assert len(model.requests) == 2 + int(read) and len(model.responses) == 1
    assert workspace.calls.count('read_context') == int(read)
    assert workspace.verifications == 0
    assert 'project.verify' not in workspace.calls and 'workspace.bundle' not in workspace.calls


async def test_successful_changed_write_clears_rejection_before_unchanged_completion(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    workspace = RejectedWriteWorkspace(context, verified, bundle)
    initial = json.dumps({'workspace_batch': {'files': {'src/app.py': 'KEEP', 'src/blocked.py': 'DENIED'},
        'complete': False, 'continuation': 'Correct the allowed implementation.'}})
    model = Model(response(initial), response(batch('src/app.py', 'FIXED', complete=False)),
                  response(batch('src/app.py', 'FIXED', complete=True)))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace, harness_tool_gateway=workspace)
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 3 and not model.responses
    assert workspace.rejected == 1 and 'src/blocked.py' not in workspace.files
    assert workspace.files['src/app.py'] == 'FIXED'
    assert sum(path == 'src/app.py' for path, _ in workspace.writes) == 2
    assert len(workspace.writes) == len(set(workspace.writes))
    assert continuation_progress(model.requests[1])['validation_error'] == 'protected_control'
    assert 'validation_error' not in continuation_progress(model.requests[2])
    assert workspace.verifications == 1 and workspace.calls.count('workspace.bundle') == 1
    assert workspace.calls[-2:] == ['project.verify', 'workspace.bundle']


async def test_successful_update_moves_file_a_ahead_of_b_in_next_manifest(tmp_path: Path) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={'token_budget': 50_000})
    workspace = Workspace(context, verified, bundle)
    initial = json.dumps({'workspace_batch': {'files': {'src/a.py': 'A0', 'src/b.py': 'B'},
        'complete': False, 'continuation': 'Update A after B.'}})
    completed = json.dumps({'workspace_batch': {'files': {'src/a.py': 'A1', 'src/b.py': 'B'},
        'complete': True, 'continuation': ''}})
    model = Model(response(initial), response(batch('src/a.py', 'A1', complete=False)), response(completed))
    runtime = DirectRuntime(model, logical_model='fixture', capability_gateway=workspace, harness_tool_gateway=workspace)
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED and len(model.requests) == 3
    earlier = continuation_progress(model.requests[1])['written_files']
    assert isinstance(earlier, Mapping)
    assert earlier['entries'] == [
        {'path': 'src/b.py', 'sha256': hashlib.sha256(b'B').hexdigest()},
        {'path': 'src/a.py', 'sha256': hashlib.sha256(b'A0').hexdigest()},
    ]
    latest = continuation_progress(model.requests[2])['written_files']
    assert isinstance(latest, Mapping)
    assert latest['entries_complete'] is True and latest['file_count'] == 2
    assert latest['entries'] == [
        {'path': 'src/a.py', 'sha256': hashlib.sha256(b'A1').hexdigest()},
        {'path': 'src/b.py', 'sha256': hashlib.sha256(b'B').hexdigest()},
    ]
    assert sum(path == 'src/a.py' for path, _ in workspace.writes) == 2
    assert sum(path == 'src/b.py' for path, _ in workspace.writes) == 1
    assert len(workspace.writes) == len(set(workspace.writes))
    assert workspace.verifications == 1 and workspace.calls.count('workspace.bundle') == 1
