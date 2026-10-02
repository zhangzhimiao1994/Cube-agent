from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionChunk

from agent_hub.models.gateway import ModelGateway, ModelGatewayError
from agent_hub.models.litellm_client import (
    LiteLLMClient,
    ModelClientError,
    ModelTransportError,
    OpenAIClientFactory,
)
from agent_hub.models.registry import ModelRegistry
from tests.contracts.test_litellm_client import API_KEY, deployment, mock_transport, request
from tests.unit.models.test_gateway import CapacityStub, SecretStub, lease


def tool_sse() -> bytes:
    chunk = {
        "id": "chatcmpl-primary",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
        "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "call_primary",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"query":"safe"}'},
            }]},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()


class OfflineBody(httpx.AsyncByteStream):
    def __init__(self, failure: str | None = None, read_error: Exception | None = None) -> None:
        self.failure = failure
        self.read_error = read_error
        self.close_started = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.close_calls = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.read_error is not None:
            raise self.read_error
        yield tool_sse()

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        if self.failure == "network":
            raise httpx.ConnectError("private cleanup failure")
        if self.failure == "internal":
            raise RuntimeError("private cleanup failure")
        if self.failure == "timeout":
            raise TimeoutError("private cleanup failure")
        if self.failure == "blocked":
            try:
                await asyncio.Future[None]()
            except asyncio.CancelledError:
                self.close_cancelled.set()
                raise


class OfflineTransport(httpx.AsyncBaseTransport):
    def __init__(self, body: OfflineBody, close_failure: str | None = None) -> None:
        self.body = body
        self.cleanup = OfflineBody(close_failure)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if json.loads(request.content).get("stream"):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=self.body,
            )
        return httpx.Response(200, json={
            "id": "chatcmpl-primary",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        })

    async def aclose(self) -> None:
        await self.cleanup.aclose()


class SDKFactory:
    def __init__(
        self, stage: str = "response", failure: str | None = None,
        request_error: Exception | None = None, read_error: Exception | None = None,
    ) -> None:
        self.stage = stage
        self.failure = failure
        self.request_error = request_error
        self.read_error = read_error
        self.transports: list[OfflineTransport] = []

    def __call__(self, *, api_key: str, base_url: str, max_retries: int) -> AsyncOpenAI:
        first = not self.transports
        body = OfflineBody(
            self.failure if first and self.stage == "response" else None,
            self.read_error if first else None,
        )
        transport = OfflineTransport(
            body, self.failure if first and self.stage == "client" else None,
        )
        self.transports.append(transport)

        async def local_hook(request: httpx.Request) -> None:
            if first and self.request_error is not None:
                raise self.request_error

        return AsyncOpenAI(
            api_key=api_key, base_url=base_url, max_retries=max_retries,
            http_client=httpx.AsyncClient(
                transport=transport, event_hooks={"request": [local_hook]},
            ),
        )


def gateway(client: LiteLLMClient) -> tuple[ModelGateway, CapacityStub]:
    capacity = CapacityStub([
        lease("primary-1", "default-account"), lease("backup", "default-account"),
    ])
    return ModelGateway(
        ModelRegistry([deployment(), deployment(id="backup", logical_model="backup")]),
        capacity, SecretStub(capacity.events), client, fallbacks={"primary": "backup"},
    ), capacity


@pytest.mark.parametrize("stage", ["response", "client"])
@pytest.mark.parametrize("failure", ["network", "internal", "timeout", "blocked"])
async def test_sdk_eof_cleanup_keeps_completed_tool_response(stage: str, failure: str) -> None:
    factory = SDKFactory(stage, failure)
    client = LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory))
    selected, capacity = gateway(client)
    events = [
        event async for event in selected.stream_openai_compatible_events(request(timeout_seconds=4))
    ]

    assert [(event.kind, dict(event.payload)) for event in events] == [(
        "tool.requested", {"id": "call_primary", "name": "lookup", "arguments": {"query": "safe"}},
    )]
    assert len(factory.transports) == 1
    assert len(factory.transports[0].requests) == 1
    assert [(status, succeeded) for _, status, _, succeeded in capacity.records] == [(200, True)]
    assert len(capacity.releases) == 1
    assert factory.transports[0].body.close_calls == 1
    assert factory.transports[0].cleanup.close_calls == 1


@pytest.mark.parametrize("stage", ["response", "client"])
async def test_gateway_short_deadline_keeps_tools_after_blocking_eof_cleanup(stage: str) -> None:
    factory = SDKFactory(stage, "blocked")
    selected, capacity = gateway(LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)))
    events = [
        event async for event in selected.stream_openai_compatible_events(request(timeout_seconds=0.05))
    ]
    assert [(event.kind, dict(event.payload)) for event in events] == [(
        "tool.requested", {"id": "call_primary", "name": "lookup", "arguments": {"query": "safe"}},
    )]
    assert len(factory.transports) == 1
    assert [(status, succeeded) for _, status, _, succeeded in capacity.records] == [(200, True)]
    assert len(capacity.releases) == 1
    cleanup = factory.transports[0].body if stage == "response" else factory.transports[0].cleanup
    assert cleanup.close_cancelled.is_set()


@pytest.mark.parametrize("stage", ["response", "client"])
async def test_sdk_raw_chunks_keep_usage_when_eof_close_times_out(stage: str) -> None:
    factory = SDKFactory(stage, "blocked")
    client = LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory))
    async with asyncio.timeout(1):
        chunks = [
            chunk async for chunk in client.stream_openai_compatible_chunks(
                deployment(), request(timeout_seconds=0.05), API_KEY,
            )
        ]
    assert len(chunks) == 1
    assert isinstance(chunks[0], ChatCompletionChunk)
    usage = chunks[0].usage
    assert usage is not None
    assert usage.prompt_tokens == 2
    assert usage.completion_tokens == 3
    assert usage.total_tokens == 5
    cleanup = factory.transports[0].body if stage == "response" else factory.transports[0].cleanup
    assert cleanup.close_cancelled.is_set()


@pytest.mark.parametrize("stage", ["response", "client"])
@pytest.mark.parametrize("failure", ["network", "internal"])
async def test_sdk_explicit_close_still_reports_cleanup_failure(stage: str, failure: str) -> None:
    factory = SDKFactory(stage, failure)
    client = LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory))
    chunks = client.stream_openai_compatible_chunks(deployment(), request(), API_KEY)
    await anext(chunks)
    expected = ModelTransportError if failure == "network" else ModelClientError
    with pytest.raises(expected):
        await cast(Any, chunks).aclose()


@pytest.mark.parametrize("stage", ["response", "client"])
@pytest.mark.parametrize("explicit", [False, True])
async def test_sdk_close_preserves_external_cancellation(stage: str, explicit: bool) -> None:
    factory = SDKFactory(stage, "blocked")
    client = LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory))
    chunks = client.stream_openai_compatible_chunks(deployment(), request(timeout_seconds=0.05), API_KEY)
    await anext(chunks)
    async def close_or_exhaust() -> None:
        if explicit:
            await cast(Any, chunks).aclose()
        else:
            await anext(chunks)

    task = asyncio.create_task(close_or_exhaust())
    cleanup = factory.transports[0].body if stage == "response" else factory.transports[0].cleanup
    await asyncio.wait_for(cleanup.close_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure_type", [FileNotFoundError, PermissionError, OSError])
async def test_sdk_local_oserror_does_not_fallback(
    streaming: bool, failure_type: type[Exception],
) -> None:
    factory = SDKFactory(request_error=failure_type("private local file failure"))
    client = LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory))
    selected, capacity = gateway(client)
    with pytest.raises(ModelGatewayError, match="model client internal failure"):
        if streaming:
            _ = [event async for event in selected.stream_openai_compatible_events(request())]
        else:
            await selected.complete_with_context(request())
    assert len(factory.transports) == 1
    assert factory.transports[0].requests == []
    assert len(capacity.releases) == 1


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure_type", [httpx.ConnectError, ConnectionResetError, socket.gaierror])
async def test_sdk_real_connection_failure_still_falls_back(
    streaming: bool, failure_type: type[Exception],
) -> None:
    factory = SDKFactory(request_error=failure_type("network disconnected"))
    selected, capacity = gateway(LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)))
    if streaming:
        events = [event async for event in selected.stream_openai_compatible_events(request())]
        assert [event.kind for event in events] == ["model.fallback", "tool.requested"]
    else:
        completion = await selected.complete_with_context(request())
        assert completion.fallback_used
        assert completion.response.text == "hello"
    assert len(factory.transports) == 2
    assert len(capacity.releases) == 2


async def test_sdk_read_failure_before_response_still_falls_back() -> None:
    factory = SDKFactory(read_error=httpx.ReadError("response disconnected"))
    selected, capacity = gateway(LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)))
    events = [event async for event in selected.stream_openai_compatible_events(request())]
    assert [event.kind for event in events] == ["model.fallback", "tool.requested"]
    assert len(factory.transports) == 2
    assert len(capacity.releases) == 2


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("status, message", [
    (503, "private internal failure"),
    (404, "private internal failure"),
    (400, "unknown parameter: max_completion_tokens"),
])
async def test_unknown_status_error_cannot_trigger_fallback_or_compatibility_retry(
    streaming: bool, status: int, message: str,
) -> None:
    class InternalFailure(RuntimeError):
        status_code = status

    client, factory, create, _ = mock_transport(error=InternalFailure(message))
    capacity = CapacityStub([
        lease("primary-1", "default-account"), lease("backup", "default-account"),
    ])
    selected = ModelGateway(
        ModelRegistry([
            deployment(api_base="https://provider.example"),
            deployment(id="backup", logical_model="backup"),
        ]),
        capacity, SecretStub(capacity.events), client, fallbacks={"primary": "backup"},
    )
    with pytest.raises(ModelGatewayError, match="model client internal failure"):
        if streaming:
            _ = [event async for event in selected.stream_openai_compatible_events(request())]
        else:
            await selected.complete_with_context(request())
    assert create.await_count == 1
    assert factory.call_count == 1
    assert len(capacity.releases) == 1
