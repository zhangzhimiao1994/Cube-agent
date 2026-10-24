"""Ordered, private Direct observations are data, not execution authority."""
from collections.abc import Mapping
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall
from agent_hub.models.types import ModelMessage, ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.direct_completion_journal import DirectCompletionJournal, DirectJournalError


def request() -> ModelRequest:
    return ModelRequest(logical_model='main', messages=(ModelMessage(role='user', content='Build'),),
                        max_output_tokens=8192, timeout_seconds=60)


def completion(text: str | None = 'Done', *, native: bool = False) -> GatewayCompletion:
    response = ModelResponse(text=text, usage=TokenUsage(10, 20, 30),
        tool_calls=(ToolCall(id='call-1', name='workspace.write_text', arguments={'path': 'a.txt'}),) if native else (),
        native_assistant=NativeAssistantMessage('fixture', None,
            (NativeToolCall('call-1', 'workspace.write_text', '{"path":"a.txt"}'),),
            reasoning_content='PRIVATE_REASONING_MUST_NOT_BE_PERSISTED') if native else None)
    return GatewayCompletion(response=response, deployment_id='fixture', logical_model='main',
        provider_id='fixture', provider_model='fixture/model', attempted_logical_models=('main',))


async def test_rejected_native_response_and_protocol_correction_keep_separate_positions() -> None:
    repo, tenant, run = InMemoryArtifactRepository(), uuid4(), uuid4()
    journal = DirectCompletionJournal(repo, tenant_id=tenant, run_id=run)
    responses = [completion(None, native=True), completion('Fixed')]
    calls = []
    async def provider(req: ModelRequest) -> GatewayCompletion:
        calls.append(req)
        return responses[len(calls)-1]
    first = await journal.complete(request(), provider)
    corrected = replace(request(), messages=(ModelMessage(role='user', content='Correct protocol'),))
    second = await journal.complete(corrected, provider)
    assert first.response.tool_calls and second.response.text == 'Fixed'
    refs = journal.references
    assert len(refs) == 2
    loaded = DirectCompletionJournal(repo, tenant_id=tenant, run_id=run, references=refs)
    async def forbidden(req: ModelRequest) -> GatewayCompletion:
        pytest.fail('Cold replay must not resubmit a paid model request')
    restored = await loaded.complete(replace(request(), timeout_seconds=30), forbidden)
    assert restored.response.tool_calls == first.response.tool_calls
    assert restored.response.native_assistant is None
    assert (await loaded.complete(corrected, forbidden)).response.text == 'Fixed'
    assert loaded.replay_complete
    assert len(calls) == 2
    from agent_hub.runtime.continuation_artifacts import load_continuation
    reference = refs[0]
    assert isinstance(reference, Mapping)
    saved = await load_continuation(repo, tenant_id=tenant, run_id=run, reference=reference)
    assert isinstance(saved, Mapping)
    response = saved['response']
    assert isinstance(response, Mapping)
    assert 'native_assistant' not in response
    assert 'reasoning_content' not in response
    assert response['text'] is None


async def test_large_legal_response_lives_in_segments_not_checkpoint_strings() -> None:
    import json

    from agent_hub.domain.runs import TaskMode
    from agent_hub.runtime.contracts import RuntimeCheckpoint
    repo, tenant, run = InMemoryArtifactRepository(), uuid4(), uuid4()
    journal = DirectCompletionJournal(repo, tenant_id=tenant, run_id=run)
    text = json.dumps({'files': {f'file-{n}.txt': 'x'*30_000 for n in range(18)}})
    async def provider(req: ModelRequest) -> GatewayCompletion:
        return completion(text)
    await journal.complete(request(), provider)
    checkpoint = RuntimeCheckpoint(id=uuid4(), runtime_type='direct', runtime_version='2',
        run_id=run, tenant_id=tenant, mode=TaskMode.DIRECT, state={'models': journal.references})
    assert len(json.dumps(checkpoint.to_payload())) < 10_000
    restored = DirectCompletionJournal(repo, tenant_id=tenant, run_id=run, references=journal.references)
    assert (await restored.complete(request(), provider)).response.text == text


@pytest.mark.parametrize('change', ['prompt', 'output_budget', 'tenant', 'run', 'order'])
async def test_replay_fails_closed_for_changed_scope_or_request(change: str) -> None:
    repo, tenant, run = InMemoryArtifactRepository(), uuid4(), uuid4()
    journal = DirectCompletionJournal(repo, tenant_id=tenant, run_id=run)
    async def provider(req: ModelRequest) -> GatewayCompletion:
        return completion()
    await journal.complete(request(), provider)
    second = replace(request(), messages=(ModelMessage(role='user', content='Second'),))
    await journal.complete(second, provider)
    refs = journal.references
    if change == 'order': refs = tuple(reversed(refs))
    resumed = DirectCompletionJournal(repo, tenant_id=uuid4() if change == 'tenant' else tenant,
        run_id=uuid4() if change == 'run' else run, references=refs)
    changed = replace(request(), messages=second.messages) if change == 'prompt' else request()
    if change == 'output_budget': changed = replace(changed, max_output_tokens=4096)
    with pytest.raises(DirectJournalError, match='direct continuation is unavailable'):
        await resumed.complete(changed, provider)


async def test_failed_persistence_does_not_advance_cursor_or_return_unrecorded_response() -> None:
    class Broken(InMemoryArtifactRepository):
        async def put(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError('PRIVATE_STORAGE_DETAIL')
    journal = DirectCompletionJournal(Broken(), tenant_id=uuid4(), run_id=uuid4())
    async def provider(req: ModelRequest) -> GatewayCompletion:
        return completion()
    with pytest.raises(DirectJournalError, match='direct continuation is unavailable') as error:
        await journal.complete(request(), provider)
    assert 'PRIVATE_STORAGE_DETAIL' not in str(error.value)
    assert error.value.__context__ is None
    assert journal.references == ()


async def test_cancellation_is_not_relabelled_as_recoverable_failure() -> None:
    import asyncio
    journal = DirectCompletionJournal(InMemoryArtifactRepository(), tenant_id=uuid4(), run_id=uuid4())
    async def provider(req: ModelRequest) -> GatewayCompletion:
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await journal.complete(request(), provider)
    assert journal.references == ()
