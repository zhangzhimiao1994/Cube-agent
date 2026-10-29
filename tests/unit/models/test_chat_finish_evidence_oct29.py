"""Real SDK + in-memory HTTP responses; no provider or tool execution."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.models import litellm_client as parser
from agent_hub.models.gateway import DeploymentPricing, GatewayRejectedOutput, ModelGateway
from agent_hub.models.litellm_client import (
    LiteLLMClient,
    ModelResponseCancelled,
    ModelResponseError,
    OpenAIClientFactory,
)
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    ModelCapability,
    RejectedOutputEvidence,
    TokenUsage,
    ToolDefinition,
)
from tests.contracts.test_litellm_client import API_KEY, PROMPT, deployment, request
from tests.unit.models.test_gateway import CapacityStub, SecretStub, lease

CAPABILITIES = frozenset({ModelCapability.TEXT,ModelCapability.TOOL_CALLING})


def native_request() -> Any:
    return request(native_tool_continuation=True,
        tools=(ToolDefinition(name='lookup',description='Synthetic approved lookup.',parameters={'type':'object'}),),
        required_capabilities=frozenset({ModelCapability.TOOL_CALLING}))


def wire(*, finish: str | None = 'tool_calls', content: str | None = None,
         arguments: str = '{broken') -> dict[str, Any]:
    return {'id':'chat_finish_test','object':'chat.completion','created':1,'model':'test-model',
        'choices':[{'index':0,'finish_reason':finish,'message':{'role':'assistant','content':content,
            'tool_calls':[{'id':'call_one','type':'function','function':{'name':'lookup','arguments':arguments}}]}}],
        'usage':{'prompt_tokens':2,'completion_tokens':3,'total_tokens':5}}


def harness(payload: dict[str, Any], *, before_response: asyncio.Event | None = None,
            close_entered: asyncio.Event | None = None, close_gate: asyncio.Event | None = None,
            ) -> tuple[LiteLLMClient, list[httpx.Request], list[httpx.AsyncClient]]:
    calls: list[httpx.Request] = []
    clients: list[httpx.AsyncClient] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if before_response is not None:
            before_response.set()
            await asyncio.Future[None]()
        return httpx.Response(200,json=payload)

    class ClosingClient(httpx.AsyncClient):
        async def aclose(self) -> None:
            if close_entered is not None:
                close_entered.set()
            if close_gate is not None:
                await close_gate.wait()
            await super().aclose()

    def factory(**kwargs: Any) -> AsyncOpenAI:
        client = ClosingClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return AsyncOpenAI(http_client=client,**kwargs)

    return LiteLLMClient(client_factory=cast(OpenAIClientFactory,factory)), calls, clients


async def rejected(payload: dict[str, Any]) -> RejectedOutputEvidence:
    client,calls,clients = harness(payload)
    with pytest.raises(ModelResponseError) as caught:
        await client.complete(deployment(capabilities=CAPABILITIES),native_request(),API_KEY)
    assert len(calls) == 1 and calls[0].url.path == '/v1/chat/completions'
    assert all(c.is_closed for c in clients)
    assert caught.value.evidence is not None
    return caught.value.evidence


@pytest.mark.parametrize('finish',['stop','tool_calls'])
async def test_completed_malformed_tool_without_text_still_has_completed_receipt(finish: str) -> None:
    evidence = await rejected(wire(finish=finish))
    assert evidence.status == 'completed'
    assert evidence.reason == 'invalid_tool'
    assert evidence.final_text is None
    assert evidence.usage == TokenUsage(2,3,5)
    assert not evidence.correction_eligible


@pytest.mark.parametrize('arguments',['{}','{broken'])
async def test_length_rejects_before_parsing_complete_or_partial_tools(arguments: str) -> None:
    with patch.object(parser,'_parse_tool_calls',side_effect=AssertionError('must not parse partial tools')) as parse:
        evidence = await rejected(wire(finish='length',arguments=arguments,content='partial text'))
    parse.assert_not_called()
    assert (evidence.status,evidence.reason,evidence.final_text) == ('incomplete','incomplete',None)
    assert evidence.usage == TokenUsage(2,3,5)
    assert not evidence.correction_eligible and not hasattr(evidence,'tool_calls')


async def test_length_cannot_accept_the_valid_prefix_of_a_tool_batch() -> None:
    payload = wire(finish='length',arguments='{}')
    payload['choices'][0]['message']['tool_calls'].append(
        {'id':'call_two','type':'function','function':{'name':'lookup','arguments':'{broken'}})
    with patch.object(parser,'_parse_tool_calls',side_effect=AssertionError('must not parse prefix')) as parse:
        evidence = await rejected(payload)
    parse.assert_not_called()
    assert evidence.status == 'incomplete' and evidence.reason == 'incomplete'


@pytest.mark.parametrize('finish',['stop','tool_calls'])
async def test_completed_valid_tool_only_response_still_accepts_native_message(finish: str) -> None:
    client,calls,clients = harness(wire(finish=finish,arguments='{}'))
    result = await client.complete(deployment(capabilities=CAPABILITIES),native_request(),API_KEY)
    assert result.text is None and result.usage == TokenUsage(2,3,5)
    assert len(result.tool_calls) == 1 and result.tool_calls[0].arguments == {}
    assert result.native_assistant is not None and len(result.native_assistant.tool_calls) == 1
    assert len(calls) == 1 and all(c.is_closed for c in clients)


@pytest.mark.parametrize('finish',[None,'unknown','content_filter'])
async def test_unconfirmed_or_filtered_finish_never_accepts_tools_or_completed_evidence(finish: str | None) -> None:
    with patch.object(parser,'_parse_tool_calls',side_effect=AssertionError('must not parse refused/unknown tools')) as parse:
        evidence = await rejected(wire(finish=finish,arguments='{}',content='ordinary text'))
    parse.assert_not_called()
    assert evidence.status == ('refused' if finish == 'content_filter' else 'unknown')
    assert evidence.reason == ('refusal' if finish == 'content_filter' else 'invalid_tool')
    assert evidence.final_text is None and evidence.usage == TokenUsage(2,3,5)
    assert not evidence.correction_eligible


async def test_explicit_message_refusal_overrides_stop_without_preserving_refusal_text() -> None:
    payload = wire(finish='stop',arguments='{}',content='ordinary text')
    payload['choices'][0]['message']['refusal'] = 'synthetic private refusal'
    with patch.object(parser,'_parse_tool_calls',side_effect=AssertionError('must not parse refused tools')) as parse:
        evidence = await rejected(payload)
    parse.assert_not_called()
    assert (evidence.status,evidence.reason,evidence.final_text) == ('refused','refusal',None)
    assert 'synthetic private refusal' not in repr(evidence)


@pytest.mark.parametrize('finish,raw_calls',[(API_KEY,None),(PROMPT,[])],ids=['key','prompt'])
async def test_legacy_plain_text_keeps_sensitive_finish_as_redacted_metadata_only(
    finish: str, raw_calls: object,
) -> None:
    payload = wire(finish=finish,content='hello')
    payload['choices'][0]['message']['tool_calls'] = raw_calls
    client,calls,clients = harness(payload)
    response = await client.complete(deployment(),request(),API_KEY)
    assert response.text == 'hello' and not response.tool_calls
    assert response.usage == TokenUsage(2,3,5)
    assert 'finish_reason' not in response.provider_metadata
    assert finish not in repr(response.provider_metadata)
    assert len(calls) == 1 and all(c.is_closed for c in clients)


@pytest.mark.parametrize('finish',[None,'unknown'])
async def test_required_tool_postcheck_cannot_upgrade_unknown_plain_text_to_completed(finish: str | None) -> None:
    payload = wire(finish=finish,content='hello')
    payload['choices'][0]['message']['tool_calls'] = None
    client,calls,clients = harness(payload)
    with pytest.raises(ModelResponseError) as caught:
        await client.complete(deployment(capabilities=CAPABILITIES),
            replace(native_request(),required_tool='lookup'),API_KEY)
    evidence = caught.value.evidence
    assert evidence is not None
    assert (evidence.status,evidence.reason,evidence.final_text) == ('unknown','invalid_tool',None)
    assert evidence.usage == TokenUsage(2,3,5) and not evidence.correction_eligible
    assert len(calls) == 1 and all(c.is_closed for c in clients)


@pytest.mark.parametrize('text',[API_KEY,PROMPT,'x'*65537],ids=['key','prompt','oversized'])
async def test_text_retention_is_independent_of_completed_status(text: str) -> None:
    evidence = await rejected(wire(content=text))
    assert (evidence.status,evidence.reason,evidence.final_text) == ('completed','invalid_tool',None)
    assert evidence.text_sha256 is None and evidence.usage == TokenUsage(2,3,5)
    assert text not in repr(evidence) and not evidence.correction_eligible


@pytest.mark.parametrize('usage,expected',[
    (None,'missing'),({'prompt_tokens':2,'completion_tokens':3,'total_tokens':99},'invalid')])
async def test_missing_or_invalid_usage_is_never_invented(usage: object, expected: str) -> None:
    payload = wire()
    payload['usage'] = usage
    evidence = await rejected(payload)
    assert evidence.usage is None and evidence.usage_status == expected
    assert evidence.final_text is None and not evidence.correction_eligible
    assert (evidence.status,evidence.reason) == (
        ('completed','invalid_tool') if expected == 'missing' else ('unknown','usage_invalid'))


async def test_invalid_usage_still_precedes_length_classification() -> None:
    payload = wire(finish='length',arguments='{}')
    payload['usage'] = {'prompt_tokens':-1,'completion_tokens':3,'total_tokens':2}
    evidence = await rejected(payload)
    assert (evidence.status,evidence.reason,evidence.usage_status,evidence.usage) == (
        'unknown','usage_invalid','invalid',None)


async def test_gateway_keeps_known_rejected_tool_usage_once_without_backup() -> None:
    client,calls,clients = harness(wire())
    selected = deployment(capabilities=CAPABILITIES)
    capacity = CapacityStub([lease(selected.id,selected.quota_scope_id),lease('backup','default-account')])
    gateway = ModelGateway(ModelRegistry([selected,deployment(id='backup',logical_model='backup',capabilities=CAPABILITIES)]),
        capacity,SecretStub(capacity.events),client,fallbacks={'primary':'backup'},
        pricing={selected.id:DeploymentPricing(Decimal(1),Decimal(2))})
    with pytest.raises(GatewayRejectedOutput) as caught:
        await gateway.complete_with_context(native_request())
    evidence = caught.value.evidence
    assert evidence is not None and (evidence.status,evidence.reason) == ('completed','invalid_tool')
    assert evidence.usage == TokenUsage(2,3,5) and evidence.final_text is None
    assert caught.value.cost_usd == Decimal('0.000008') and not caught.value.fallback_used
    assert len(calls) == len(capacity.releases) == len(capacity.records) == 1
    assert all(c.is_closed for c in clients)


async def test_cancellation_before_response_does_not_invent_completion_evidence() -> None:
    entered = asyncio.Event()
    client,calls,clients = harness(wire(),before_response=entered)
    task = asyncio.create_task(client.complete(deployment(capabilities=CAPABILITIES),native_request(),API_KEY))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert not isinstance(caught.value,ModelResponseCancelled) and not hasattr(caught.value,'receipt')
    assert len(calls) == 1 and all(c.is_closed for c in clients)


async def test_cancellation_after_rejection_keeps_accounting_but_never_returns_accepted_tools() -> None:
    entered,release = asyncio.Event(),asyncio.Event()
    client,calls,clients = harness(wire(),close_entered=entered,close_gate=release)
    task = asyncio.create_task(client.complete(deployment(capabilities=CAPABILITIES),native_request(),API_KEY))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(ModelResponseCancelled) as caught:
        await task
    receipt = caught.value.receipt
    assert isinstance(receipt,RejectedOutputEvidence)
    assert (receipt.status,receipt.reason,receipt.final_text) == ('completed','invalid_tool',None)
    assert receipt.usage == TokenUsage(2,3,5) and not receipt.correction_eligible
    assert len(calls) == 1 and all(c.is_closed for c in clients)
