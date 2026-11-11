from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

import pytest
from autogen_core.models import UserMessage

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelResponse, TokenUsage, ToolCall
from agent_hub.runs.repository import _public_event_payload
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    GatewayChatCompletionClient,
    RuntimeExecutionError,
    _DiscussionDurability,
)
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import EventKind, RunEvent
from tests.integration.runtime.test_autogen_adapter import ScriptedGateway, context, plan


@pytest.mark.parametrize('kind,phase', [
    ('tool_call_limit', 'model_response_validation'),
    ('invalid_response', 'model_response_validation'),
    ('unavailable_capability', 'model_response_validation'),
    ('type_error', 'model_client'),
])
async def test_actual_framework_preserves_local_failure_without_another_attempt(kind, phase) -> None:
    class Gateway(ScriptedGateway):
        async def complete_with_context(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                response = ModelResponse(text='analyst', usage=TokenUsage(1, 1, 2))
            else:
                if phase == 'model_client':
                    raise TypeError('PRIVATE_PROMPT Bearer sk-PRIVATE_SDK_BODY')
                calls = tuple(ToolCall(id=f'call_{i}', name='web_search', arguments={})
                              for i in range(17 if kind == 'tool_call_limit' else 1))
                if kind == 'unavailable_capability':
                    calls = (ToolCall(id='call_0', name='unavailable_tool', arguments={}),)
                response = ModelResponse(text=None, tool_calls=() if kind == 'invalid_response' else calls,
                                         usage=TokenUsage(1, 1, 2))
            return GatewayCompletion(response=response, deployment_id='shared', logical_model=request.logical_model,
                provider_id='mock', provider_model='mock/local', cost_usd=Decimal(0))

    class Capabilities:
        calls = 0

        def is_replay_safe(self, name):
            return True

        async def execute(self, **kwargs):
            self.calls += 1
            raise AssertionError('invalid/serialization result must not execute a tool')

    gateway = Gateway([])
    capabilities = Capabilities()
    participant = DiscussionParticipant(id='analyst', role='Analyst', goal='Search',
                                       logical_model='shared', allowed_tools=('web.search',))
    runtime = AutoGenDiscussionRuntime(gateway, plan(participants=(participant, plan().participants[1])),
                                      capability_gateway=capabilities)
    events = [event async for event in runtime.run(context())]
    failure = events[-1]
    assert failure.kind is EventKind.RUNTIME_FAILED
    assert failure.reason == 'discussion_failed'
    assert failure.payload['discussion_failure_phase'] == phase
    assert failure.payload['discussion_failure_kind'] == kind
    assert failure.payload['error_code'] == f'runtime.discussion_failed.{phase}.{kind}'
    assert failure.payload['retryable'] is True
    assert RunEvent.model_validate_json(failure.model_dump_json()).payload == failure.payload
    assert _public_event_payload(failure.to_payload())['payload'] == failure.to_payload()['payload']
    assert len(gateway.requests) == 2 and capabilities.calls == 0
    assert sum(event.kind is EventKind.RUNTIME_FAILED for event in events) == 1
    assert 'PRIVATE' not in json.dumps([event.to_payload() for event in events])


@pytest.mark.parametrize('kind', ['required_tool_missing', 'acceptance_write_missing'])
async def test_required_tool_postcheck_preserves_known_succeeded_ledger_and_specific_kind(kind) -> None:
    snapshots = []
    artifacts = []

    async def publish(saved_artifacts, models, tools):
        snapshots.append(tuple(row['status'] for row in models))
        assert not tools

    async def store(artifact):
        artifacts.append(artifact)
        return uuid4()

    async def abort(write_id):
        raise AssertionError('known completed publication must not be aborted')

    durability = _DiscussionDurability(publish, store, abort, lambda _: None,
                                       run_id=uuid4(), tenant_id=uuid4())

    class Gateway:
        calls = 0

        async def complete_with_context(self, request):
            self.calls += 1
            assert request.required_tool == 'workspace_write_text'
            response = ModelResponse(text='safe text', usage=TokenUsage(1, 1, 2), tool_calls=(
                ToolCall(id='call_0', name='workspace_write_text',
                         arguments={'path': 'other.txt', 'content': 'safe'}),
            ) if kind == 'acceptance_write_missing' else ())
            return GatewayCompletion(response=response, deployment_id='shared', logical_model=request.logical_model,
                                     provider_id='mock', provider_model='mock/local', cost_usd=Decimal(0))

    gateway = Gateway()
    usage = DiscussionUsage()
    client = GatewayChatCompletionClient(gateway, 'shared', usage, supports_tool_calls=True,
        durability=durability, engineering_action=lambda _: 'workspace.write_text',
        engineering_budget=lambda: (100, 5.0), acceptance_first=kind == 'acceptance_write_missing')
    schema = {'name': 'workspace.write_text', 'description': 'local probe',
              'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': True}, 'strict': False}
    with pytest.raises(RuntimeExecutionError):
        await client.create([UserMessage(content='safe', source='user')], tools=[schema])
    assert durability.failure_reason == 'discussion_failed'
    assert durability.failure_payload['discussion_failure_kind'] == kind
    assert durability.failure_payload['discussion_failure_phase'] == 'model_response_validation'
    assert snapshots == [('running',), ('succeeded',)]
    assert len(artifacts) == 1 and durability.model_entries[0]['status'] == 'succeeded'
    assert durability._model_cursor == 1 and not durability.tool_entries
    assert usage.tokens == 2 and gateway.calls == 1
