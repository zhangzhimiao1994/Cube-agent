"""Delivery references must reach real runtime context, without replacing history."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runs.repository import ConversationContextItem
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import Artifact, TaskContext
from agent_hub.runtime.direct import DirectRuntime, _serialized_prior_artifacts
from agent_hub.runtime.instruction_context import InstructionContextLoader


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_service_adds_bounded_delivery_reference_without_replacing_history(mode):
    source, tenant, run = uuid4(), uuid4(), uuid4()
    evidence = Artifact(id=uuid4(), type='text', producer='historical_delivery_evidence',
        content={'text': 'Historical archive bytes SHA256: ' + 'a' * 64})
    loader = SimpleNamespace(load_delivery_evidence=AsyncMock(return_value=(evidence,)))
    repository = SimpleNamespace(conversation_context=AsyncMock(return_value=(
        ConversationContextItem(source, 'Build the product', ()),)))
    service = object.__new__(RunService)
    service._repository = repository
    service._instruction_context_loader = loader
    service._main_agent_context_window_getter = None
    artifacts = await service._conversation_artifacts(tenant_id=tenant, run_id=run,
        current_request='Verify the previous delivery', routing_decision={'conversation_id': 'same'},
        runtime_token_budget=32000)
    assert evidence in artifacts and any(a.producer == 'conversation_history' for a in artifacts)
    call = loader.load_delivery_evidence.await_args.kwargs
    assert call['source_run_ids'] == (source,)
    assert call['tenant_id'] == tenant and call['run_id'] == run
    assert 0 < call['token_budget'] < 8000
    if mode is TaskMode.DIRECT:
        context = TaskContext(run_id=run, tenant_id=tenant, mode=mode,
            request='Verify the previous delivery', artifacts=artifacts, token_budget=32000)
        prompt = DirectRuntime(SimpleNamespace(), logical_model='fixed')._build_prompt(context)
        assert prompt.error_code is None
        assert prompt.messages is not None
        text = '\n'.join(str(item.content) for item in prompt.messages)
        assert 'a' * 64 in text and 'Build the product' in text


async def test_loader_failure_does_not_erase_prior_conversation():
    service = object.__new__(RunService)
    service._repository = SimpleNamespace(conversation_context=AsyncMock(return_value=(
        ConversationContextItem(uuid4(), 'Existing user goal', ()),)))
    service._instruction_context_loader = SimpleNamespace(load_delivery_evidence=AsyncMock(side_effect=OSError))
    service._main_agent_context_window_getter = None
    artifacts = await service._conversation_artifacts(tenant_id=uuid4(), run_id=uuid4(),
        current_request='Status?', routing_decision={'conversation_id': 'same'}, runtime_token_budget=32000)
    assert len(artifacts) == 1 and 'Existing user goal' in artifacts[0].content['text']


def test_delivery_evidence_is_not_cut_into_a_partial_hash():
    text = 'x' * 1000 + 'a' * 64
    evidence = Artifact(id=uuid4(), type='text', producer='historical_delivery_evidence', content={'text': text})
    other = Artifact(id=uuid4(), type='text', producer='conversation_history', content={'text': 'small'})
    encoded, _ = _serialized_prior_artifacts((evidence, other), token_budget=1000, byte_budget=1500)
    import json
    value = json.loads(encoded)
    assert 'text' not in value['artifacts'][0]


def test_existing_trusted_project_loader_has_separate_delivery_reference_entry():
    assert callable(getattr(InstructionContextLoader, 'load_delivery_evidence', None))
