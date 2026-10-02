from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import APIConnectionError, APIResponseValidationError, AuthenticationError

from agent_hub.models.gateway import (
    DeploymentPricing,
    GatewayRejectedOutput,
    ModelGateway,
    ModelGatewayError,
)
from agent_hub.models.litellm_client import (
    HTTPClientFactory,
    LiteLLMClient,
    ModelResponseCancelled,
    ModelResponseError,
    ModelTransportError,
    OpenAIClientFactory,
)
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import Deployment, ModelRequest, TokenUsage
from tests.contracts.test_litellm_client import (
    API_KEY,
    RAW_ERROR,
    captured_traceback,
    deployment,
    mock_transport,
    request,
)
from tests.contracts.test_responses_client import deployment as native_deployment
from tests.contracts.test_responses_client import request as native_request
from tests.contracts.test_responses_client import setup_client
from tests.unit.models.test_gateway import (
    CapacityStub,
    PerCallStreamingTransportStub,
    SecretStub,
    TransportStub,
    lease,
)


def client_surface(
    surface: str,
) -> tuple[LiteLLMClient, Deployment, ModelRequest, AsyncMock, AsyncMock]:
    if surface == "responses":
        client, _, create, _, close = setup_client()
        return client, native_deployment(), native_request(timeout_seconds=0.05), create, close
    if surface == "chat":
        client, _, create, close = mock_transport()
        return client, deployment(), request(timeout_seconds=0.05), create, close
    create = AsyncMock(
        return_value=SimpleNamespace(
            status_code=200,
            json=lambda: {
                "content": [{"type": "text", "text": "hello"}],
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        )
    )
    close = AsyncMock()
    factory = MagicMock(return_value=SimpleNamespace(post=create, aclose=close))
    client = LiteLLMClient(http_client_factory=cast(HTTPClientFactory, factory))
    return (
        client,
        deployment(api_base="https://provider.example/v1/messages"),
        request(timeout_seconds=0.05),
        create,
        close,
    )


@pytest.mark.parametrize("surface", ["chat", "responses", "messages"])
@pytest.mark.parametrize("cleanup", ["error", "timeout"])
async def test_received_success_survives_close_failure(surface: str, cleanup: str) -> None:
    client, selected, req, create, close = client_surface(surface)
    if cleanup == "error":
        close.side_effect = RuntimeError(RAW_ERROR + API_KEY)
    else:

        async def block() -> None:
            await asyncio.Future[None]()

        close.side_effect = block
    capacity = CapacityStub(
        [lease(selected.id, selected.quota_scope_id), lease("backup", "default-account")]
    )
    gateway = ModelGateway(
        ModelRegistry([selected, deployment(id="backup", logical_model="backup")]),
        capacity,
        SecretStub(capacity.events),
        client,
        fallbacks={"primary": "backup"},
        pricing={selected.id: DeploymentPricing(Decimal(1), Decimal(2))},
    )
    completion = await gateway.complete_with_context(replace(req, timeout_seconds=3))
    expected = TokenUsage(12, 117, 129) if surface == "responses" else TokenUsage(2, 3, 5)
    assert completion.response.usage == expected
    assert completion.response.text == (
        '{"verdict":"approve"}' if surface == "responses" else "hello"
    )
    assert completion.cost_usd == (
        Decimal("0.000246") if surface == "responses" else Decimal("0.000008")
    )
    assert not completion.fallback_used
    assert create.await_count == 1
    assert len(capacity.releases) == 1
    assert capacity.records[0][1::2] == (200, True)


@pytest.mark.parametrize("surface", ["chat", "responses", "messages"])
@pytest.mark.parametrize("cleanup", ["release", "error", "timeout"])
async def test_cancellation_during_close_preserves_received_usage(
    surface: str, cleanup: str
) -> None:
    client, selected, req, _, close = client_surface(surface)
    entered, release = asyncio.Event(), asyncio.Event()

    async def block() -> None:
        entered.set()
        await release.wait()
        if cleanup == "error":
            raise RuntimeError(RAW_ERROR + API_KEY)

    close.side_effect = block
    task = asyncio.create_task(client.complete(selected, req, API_KEY))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    if cleanup != "timeout":
        release.set()
    with pytest.raises(ModelResponseCancelled) as caught:
        await task
    expected = TokenUsage(12, 117, 129) if surface == "responses" else TokenUsage(2, 3, 5)
    assert caught.value.receipt.usage == expected


async def test_messages_invalid_json_is_response_failure_without_fallback() -> None:
    client, selected, req, create, _ = client_surface("messages")
    create.return_value.json = MagicMock(side_effect=json.JSONDecodeError(RAW_ERROR, API_KEY, 0))
    with pytest.raises(ModelResponseError) as caught:
        await client.complete(selected, req, API_KEY)
    assert caught.value.status_code is None
    assert RAW_ERROR not in str(caught.value) and API_KEY not in str(caught.value)
    capacity = CapacityStub(
        [lease(selected.id, selected.quota_scope_id), lease("backup", "default-account")]
    )
    gateway = ModelGateway(
        ModelRegistry([selected, deployment(id="backup", logical_model="backup")]),
        capacity,
        SecretStub(capacity.events),
        client,
        fallbacks={"primary": "backup"},
    )
    with pytest.raises(GatewayRejectedOutput):
        await gateway.complete_with_context(req)
    assert len(capacity.releases) == 1
    assert create.await_count == 2


@pytest.mark.parametrize("surface", ["chat", "responses", "messages", "stream"])
@pytest.mark.parametrize(
    "failure_type", [RuntimeError, httpx.LocalProtocolError, httpx.UnsupportedProtocol]
)
async def test_client_internal_failure_is_not_network_retry(
    surface: str, failure_type: type[Exception]
) -> None:
    if surface == "stream":
        client, _, create, _ = mock_transport(error=failure_type(RAW_ERROR + API_KEY))
        with pytest.raises(RuntimeError) as caught:
            _ = [
                chunk
                async for chunk in client.stream_openai_compatible_chunks(
                    deployment(), request(), API_KEY
                )
            ]
    else:
        client, selected, req, create, _ = client_surface(surface)
        create.side_effect = failure_type(RAW_ERROR + API_KEY)
        with pytest.raises(RuntimeError) as caught:
            await client.complete(selected, req, API_KEY)
    assert not isinstance(caught.value, ModelTransportError)
    assert str(caught.value) == "model client internal failure"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    rendered = captured_traceback(caught.value)
    assert RAW_ERROR not in rendered and API_KEY not in rendered
    assert create.await_count == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError(RAW_ERROR),
        APIConnectionError(request=httpx.Request("POST", "https://provider.example/v1")),
        httpx.HTTPStatusError(
            RAW_ERROR,
            request=httpx.Request("POST", "https://provider.example/v1"),
            response=httpx.Response(401),
        ),
        AuthenticationError(
            RAW_ERROR,
            response=httpx.Response(
                401, request=httpx.Request("POST", "https://provider.example/v1")
            ),
            body=None,
        ),
    ],
)
@pytest.mark.parametrize("surface", ["chat", "responses", "messages"])
async def test_client_preserves_network_errors_and_auth_status(
    surface: str, failure: Exception
) -> None:
    client, selected, req, create, _ = client_surface(surface)
    create.side_effect = failure
    with pytest.raises(ModelTransportError) as caught:
        await client.complete(selected, req, API_KEY)
    assert caught.value.status_code == (
        401 if isinstance(failure, (httpx.HTTPStatusError, AuthenticationError)) else None
    )
    assert RAW_ERROR not in str(caught.value)


@pytest.mark.parametrize("streaming", [False, True])
async def test_gateway_unknown_failure_stops_before_backup(
    streaming: bool, caplog: pytest.LogCaptureFixture
) -> None:
    capacity = CapacityStub(
        [lease("primary-1", "default-account"), lease("backup", "default-account")]
    )
    failure = RuntimeError(RAW_ERROR + API_KEY)
    transport = (
        PerCallStreamingTransportStub(
            capacity.events, [[failure], [{"choices": [{"delta": {"content": "backup"}}]}]]
        )
        if streaming
        else TransportStub(capacity.events, failure=failure)
    )
    gateway = ModelGateway(
        ModelRegistry([deployment(), deployment(id="backup", logical_model="backup")]),
        capacity,
        SecretStub(capacity.events),
        transport,
        fallbacks={"primary": "backup"},
    )
    with pytest.raises(ModelGatewayError) as caught:
        if streaming:
            _ = [event async for event in gateway.stream_openai_compatible_events(request())]
        else:
            await gateway.complete_with_context(request())
    assert str(caught.value) == "model client internal failure"
    assert len(capacity.releases) == 1
    assert RAW_ERROR not in caplog.text and API_KEY not in caplog.text


@pytest.mark.parametrize("streaming", [False, True])
async def test_gateway_raw_network_failure_still_uses_backup(streaming: bool) -> None:
    capacity = CapacityStub(
        [lease("primary-1", "default-account"), lease("backup", "default-account")]
    )
    failure = httpx.ConnectError(RAW_ERROR + API_KEY)
    transport: TransportStub
    if streaming:
        transport = PerCallStreamingTransportStub(
            capacity.events, [[failure], [{"choices": [{"delta": {"content": "backup"}}]}]]
        )
    else:
        transport = TransportStub(capacity.events, failure=failure)
    gateway = ModelGateway(
        ModelRegistry([deployment(), deployment(id="backup", logical_model="backup")]),
        capacity,
        SecretStub(capacity.events),
        transport,
        fallbacks={"primary": "backup"},
    )
    if streaming:
        events = [event async for event in gateway.stream_openai_compatible_events(request())]
        assert any(event.kind == "model.fallback" for event in events)
    else:
        with pytest.raises(ModelTransportError):
            await gateway.complete_with_context(request())
    assert len(capacity.releases) == 2


async def test_messages_client_construction_failure_is_redacted() -> None:
    client = LiteLLMClient(
        http_client_factory=cast(
            HTTPClientFactory,
            MagicMock(side_effect=RuntimeError(RAW_ERROR + API_KEY)),
        )
    )
    with pytest.raises(RuntimeError) as caught:
        await client.complete(
            deployment(api_base="https://provider.example/v1/messages"), request(), API_KEY
        )
    assert str(caught.value) == "model client internal failure"
    assert not isinstance(caught.value, ModelTransportError)
    rendered = captured_traceback(caught.value)
    assert RAW_ERROR not in rendered and API_KEY not in rendered


async def test_chat_retry_client_construction_failure_is_redacted() -> None:
    client, factory, _, _ = mock_transport(error=ModelTransportError("not found", status_code=404))
    original = factory.return_value
    factory.side_effect = [original, RuntimeError(RAW_ERROR + API_KEY)]
    client = LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory))
    with pytest.raises(RuntimeError) as caught:
        await client.complete(deployment(api_base="https://provider.example"), request(), API_KEY)
    assert str(caught.value) == "model client internal failure"
    rendered = captured_traceback(caught.value)
    assert RAW_ERROR not in rendered and API_KEY not in rendered


async def test_messages_invalid_usage_is_response_failure() -> None:
    client, selected, req, create, _ = client_surface("messages")
    create.return_value.json = lambda: {
        "content": [{"type": "text", "text": "hello"}],
        "usage": {"input_tokens": -1, "output_tokens": 3},
    }
    with pytest.raises(ModelResponseError):
        await client.complete(selected, req, API_KEY)


async def test_openai_response_validation_failure_is_not_transport() -> None:
    client, _, create, _ = mock_transport(
        error=APIResponseValidationError(
            response=httpx.Response(
                200, request=httpx.Request("POST", "https://provider.example/v1")
            ),
            body=RAW_ERROR + API_KEY,
        )
    )
    with pytest.raises(ModelResponseError) as caught:
        await client.complete(deployment(), request(), API_KEY)
    assert caught.value.status_code == 200
    assert RAW_ERROR not in str(caught.value) and API_KEY not in str(caught.value)
    assert create.await_count == 1


@pytest.mark.parametrize(
    "cause_type", [RuntimeError, httpx.LocalProtocolError, httpx.UnsupportedProtocol]
)
async def test_openai_connection_wrapper_does_not_make_internal_cause_retryable(
    cause_type: type[Exception],
) -> None:
    error = APIConnectionError(request=httpx.Request("POST", "https://provider.example/v1"))
    error.__cause__ = cause_type(RAW_ERROR + API_KEY)
    client, _, _, _ = mock_transport(error=error)
    with pytest.raises(RuntimeError) as caught:
        await client.complete(deployment(), request(), API_KEY)
    assert not isinstance(caught.value, ModelTransportError)
    assert str(caught.value) == "model client internal failure"


async def test_openai_connection_wrapper_preserves_real_network_cause() -> None:
    error = APIConnectionError(request=httpx.Request("POST", "https://provider.example/v1"))
    error.__cause__ = httpx.ConnectError(RAW_ERROR + API_KEY)
    client, _, _, _ = mock_transport(error=error)
    with pytest.raises(ModelTransportError) as caught:
        await client.complete(deployment(), request(), API_KEY)
    assert caught.value.status_code is None
    assert RAW_ERROR not in str(caught.value) and API_KEY not in str(caught.value)


@pytest.mark.parametrize("streaming", [False, True])
async def test_gateway_raw_response_validation_failure_remains_response_error(
    streaming: bool,
) -> None:
    capacity = CapacityStub(
        [lease("primary-1", "default-account"), lease("backup", "default-account")]
    )
    failure = APIResponseValidationError(
        response=httpx.Response(200, request=httpx.Request("POST", "https://provider.example/v1")),
        body=RAW_ERROR + API_KEY,
    )
    transport = (
        PerCallStreamingTransportStub(capacity.events, [[failure]])
        if streaming
        else TransportStub(capacity.events, failure=failure)
    )
    gateway = ModelGateway(
        ModelRegistry([deployment(), deployment(id="backup", logical_model="backup")]),
        capacity,
        SecretStub(capacity.events),
        transport,
        fallbacks={"primary": "backup"},
    )
    if streaming:
        with pytest.raises(GatewayRejectedOutput):
            _ = [event async for event in gateway.stream_openai_compatible_events(request())]
    else:
        with pytest.raises(GatewayRejectedOutput):
            await gateway.complete_with_context(request())
    assert len(capacity.releases) == 1
