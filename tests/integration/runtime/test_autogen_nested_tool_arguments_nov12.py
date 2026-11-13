from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import NoReturn, cast
from uuid import UUID, uuid4

import pytest
from autogen_core import FunctionCall
from autogen_core.models import UserMessage
from autogen_core.tools import ToolSchema

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    GatewayChatCompletionClient,
    _DiscussionDurability,
)
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue
from tests.integration.runtime.test_autogen_adapter import ScriptedGateway, context, plan


@pytest.mark.parametrize('arguments', [
    {'query': 'safe'},
    {'architecture': {'components': [{'name': 'safe', 'enabled': True}], 'optional': None}},
    {'items': [{'options': {'count': 1}}, {'options': {'count': 2}}]},
])
async def test_real_client_serializes_frozen_nested_tool_arguments_without_mutation(arguments: Mapping[str, JsonValue]) -> None:
    call = ToolCall(id='call_0', name='web_search', arguments=arguments)
    assert isinstance(call.arguments, MappingProxyType)

    class Gateway:
        calls = 0

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.calls += 1
            return GatewayCompletion(response=ModelResponse(text=None, tool_calls=(call,), usage=TokenUsage(1, 1, 2)),
                deployment_id='shared', logical_model=request.logical_model,
                provider_id='mock', provider_model='mock/local', cost_usd=Decimal(0))

    gateway = Gateway()
    usage = DiscussionUsage()
    client = GatewayChatCompletionClient(gateway, 'shared', usage, supports_tool_calls=True)
    schema: ToolSchema = {'name': 'web.search', 'description': 'local probe',
              'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': True}, 'strict': False}
    result = await client.create([UserMessage(content='safe', source='user')], tools=[schema])
    assert json.loads(cast(list[FunctionCall], result.content)[0].arguments) == arguments
    assert cast(list[FunctionCall], result.content)[0].name == 'web.search'
    assert isinstance(call.arguments, MappingProxyType)
    assert gateway.calls == 1 and usage.tokens == 2


async def test_actual_framework_executes_nested_arguments_once_after_known_completion() -> None:
    arguments: dict[str, object] = {'architecture': {'components': [{'name': 'safe'}]}}

    class Gateway(ScriptedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            count = len(self.requests)
            response = ModelResponse(text='analyst' if count == 1 else '[COMPLETE] verified',
                                     usage=TokenUsage(1, 1, 2))
            if count == 2:
                response = ModelResponse(text=None,
                    tool_calls=(ToolCall(id='call_0', name='web_search', arguments=cast(Mapping[str, JsonValue], arguments)),),
                    usage=TokenUsage(1, 1, 2))
            return GatewayCompletion(response=response, deployment_id='shared', logical_model=request.logical_model,
                provider_id='mock', provider_model='mock/local', cost_usd=Decimal(0))

    class Capabilities:
        def __init__(self) -> None:
            self.calls: list[Mapping[str, JsonValue]] = []

        def is_replay_safe(self, name: str) -> bool:
            return True

        async def execute(self, **kwargs: object) -> Mapping[str, JsonValue]:
            self.calls.append(cast(Mapping[str, JsonValue], kwargs['arguments']))
            return {'safe': True}

    gateway = Gateway([])
    capabilities = Capabilities()
    participant = DiscussionParticipant(id='analyst', role='Analyst', goal='Search',
                                       logical_model='shared', allowed_tools=('web.search',))
    runtime = AutoGenDiscussionRuntime(gateway, plan(participants=(participant, plan().participants[1])),
                                      capability_gateway=capabilities)
    events = [event async for event in runtime.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(capabilities.calls) == 1
    assert cast(tuple[Mapping[str, JsonValue], ...], cast(Mapping[str, JsonValue], capabilities.calls[0]['architecture'])['components'])[0]['name'] == 'safe'
    assert sum(event.kind is EventKind.TOOL_COMPLETED for event in events) == 1
    assert not any(event.kind is EventKind.RUNTIME_FAILED for event in events)


async def test_known_nested_completion_replays_without_new_gateway_call_or_usage() -> None:
    async def publish(*args: object) -> None:
        return None

    async def store(artifact: Artifact) -> UUID:
        return uuid4()

    async def abort(write_id: UUID) -> NoReturn:
        raise AssertionError('successful known completion must not be aborted')

    class Gateway:
        calls = 0

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.calls += 1
            assert self.calls == 1, 'known completion must not be sent again'
            return GatewayCompletion(response=ModelResponse(text=None, usage=TokenUsage(1, 1, 2),
                tool_calls=(ToolCall(id='call_0', name='web_search', arguments={'nested': {'safe': True}}),)),
                deployment_id='shared', logical_model=request.logical_model,
                provider_id='mock', provider_model='mock/local', cost_usd=Decimal(0))

    gateway = Gateway()
    run_id, tenant_id = uuid4(), uuid4()
    durable = _DiscussionDurability(publish, store, abort, lambda _: None, run_id=run_id, tenant_id=tenant_id)
    first = GatewayChatCompletionClient(gateway, 'shared', DiscussionUsage(), supports_tool_calls=True,
                                       durability=durable)
    schema: ToolSchema = {'name': 'web.search', 'description': 'local probe',
              'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': True}, 'strict': False}
    messages = [UserMessage(content='safe', source='user')]
    await first.create(messages, tools=[schema])
    assert durable.model_entries[0]['status'] == 'succeeded'
    restored = _DiscussionDurability(publish, store, abort, lambda _: None,
        run_id=run_id, tenant_id=tenant_id, model_entries=tuple(durable.model_entries), artifacts=durable.artifacts)
    usage = DiscussionUsage()
    second = GatewayChatCompletionClient(gateway, 'shared', usage, supports_tool_calls=True, durability=restored)
    result = await second.create(messages, tools=[schema])
    assert json.loads(cast(list[FunctionCall], result.content)[0].arguments) == {'nested': {'safe': True}}
    assert gateway.calls == 1 and usage.tokens == 0
    assert restored.model_entries == durable.model_entries
