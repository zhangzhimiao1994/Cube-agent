from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any

import httpx
import pytest

from agent_hub.models.litellm_client import ModelResponseError, ModelTransportError
from agent_hub.models.types import StructuredResponseSchema, TokenUsage
from tests.contracts.test_required_tool_transport import (
    deployment,
    provider_response,
    request,
    transport,
)

REJECTION = 'The tool_choice parameter does not support being set to required or object in thinking mode'


def rejected(message: object = REJECTION, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={'error': {
        'message': message, 'type': 'invalid_request_error', 'code': 'invalid_parameter_error',
    }})


@pytest.mark.parametrize('name', ['project_inspect', 'project_verify'])
@pytest.mark.parametrize('schema', [None, StructuredResponseSchema('Result', {
    'type': 'object', 'properties': {}, 'additionalProperties': False,
})])
async def test_explicit_named_choice_rejection_negotiates_once_without_weakening_required_result(
    name: str, schema: StructuredResponseSchema | None,
) -> None:
    wire: list[dict[str, Any]] = []
    read_timeouts: list[float] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(json.loads(req.content))
        read_timeouts.append(req.extensions['timeout']['read'])
        if len(wire) == 1:
            time.sleep(0.02)
        return rejected() if len(wire) == 1 else httpx.Response(
            200, json=provider_response('chat_completions', name),
        )

    result = await transport(handle).complete(deployment('chat_completions'),
        request('chat_completions', required_tool=name, response_schema=schema), 'mock-key')
    assert len(wire) == 2
    assert wire[0]['tool_choice'] == {'type': 'function', 'function': {'name': name}}
    assert wire[1] == {**wire[0], 'tool_choice': 'auto'}
    assert 0 < read_timeouts[1] < read_timeouts[0]
    assert result.tool_calls[0].name == name and result.usage == TokenUsage(2, 3, 5)


@pytest.mark.parametrize('message,status', [
    ('tool_choice unsupported', 400), (REJECTION + ' private prompt', 400),
    ('private prompt ' + REJECTION, 400), (None, 400), ({'message': REJECTION}, 400),
    (REJECTION + '..', 400), (REJECTION + '.' * 1000, 400),
    (REJECTION, 401), (REJECTION, 403), (REJECTION, 429), (REJECTION, 503),
])
async def test_unconfirmed_or_non_400_rejection_never_negotiates(message: object, status: int) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return rejected(message, status)

    with pytest.raises(ModelTransportError):
        await transport(handle).complete(deployment('chat_completions'),
            request('chat_completions', required_tool='project_inspect'), 'mock-key')
    assert len(wire) == 1


@pytest.mark.parametrize('returned_name', [None, 'project_verify', 'wrong_name'])
async def test_negotiated_response_still_requires_the_actual_selected_tool(returned_name: str | None) -> None:
    count = 0

    def handle(req: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return rejected() if count == 1 else httpx.Response(
            200, json=provider_response('chat_completions', returned_name),
        )

    with pytest.raises(ModelResponseError) as caught:
        await transport(handle).complete(deployment('chat_completions'),
            request('chat_completions', required_tool='project_inspect'), 'mock-key')
    assert count == 2
    assert caught.value.evidence is not None and caught.value.evidence.reason == 'invalid_tool'
    assert caught.value.evidence.usage == TokenUsage(2, 3, 5)


async def test_no_required_tool_cannot_trigger_parameter_negotiation() -> None:
    count = 0

    def handle(req: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return rejected()

    with pytest.raises(ModelTransportError):
        await transport(handle).complete(deployment('chat_completions'),
            request('chat_completions'), 'mock-key')
    assert count == 1


async def test_negotiation_cannot_reset_an_exhausted_request_deadline() -> None:
    count = 0

    def handle(req: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        time.sleep(0.04)
        return rejected()

    with pytest.raises(ModelTransportError):
        await transport(handle).complete(deployment('chat_completions'),
            replace(request('chat_completions', required_tool='project_inspect'), timeout_seconds=0.02),
            'mock-key')
    assert count == 1


@pytest.mark.parametrize('first_kind', ['legacy_tokens', 'root_base'])
async def test_existing_parameter_compatibility_cannot_chain_an_auto_negotiation(first_kind: str) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        if len(wire) == 1:
            return rejected('Unsupported parameter: max_completion_tokens', 400) if first_kind == 'legacy_tokens' else rejected('Not found', 404)
        return rejected()

    selected = deployment('chat_completions')
    if first_kind == 'root_base':
        selected = replace(selected, api_base='https://mock.example')
    with pytest.raises(ModelTransportError) as caught:
        await transport(handle).complete(selected,
            request('chat_completions', required_tool='project_inspect'), 'mock-key')
    assert len(wire) == 2 and caught.value.status_code == 400
    assert all(json.loads(req.content)['tool_choice'] != 'auto' for req in wire)
