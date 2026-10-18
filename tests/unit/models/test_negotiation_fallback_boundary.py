from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from dataclasses import replace
from typing import Any, cast

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.models.failure_receipt import get_gateway_failure_receipt
from agent_hub.models.gateway import (
    GatewayRejectedOutput,
    ModelGateway,
    ModelGatewayError,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.litellm_client import (
    LiteLLMClient,
    ModelTransportError,
    OpenAIClientFactory,
)
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import StructuredResponseSchema, TokenUsage, ToolCall
from tests.contracts.test_required_tool_transport import deployment, provider_response, request
from tests.unit.models.test_gateway import CapacityStub, SecretStub, lease

REJECTION = (
    "The tool_choice parameter does not support being set to required or object in thinking mode"
)
Handler = (
    Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]
)


def rejected(message: str = REJECTION, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={"error": {
        "message": message,
        "type": "invalid_request_error",
        "code": "invalid_parameter_error",
    }})


def gateway_for(handler: Handler) -> tuple[ModelGateway, CapacityStub, SecretStub]:
    primary = replace(
        deployment("chat_completions"),
        secret_ref="secret://primary", quota_scope_id="scope-primary",
    )
    backup = replace(
        primary, id="backup", logical_model="backup", request_model="backup-model",
        secret_ref="secret://backup", quota_scope_id="scope-backup",
    )
    capacity = CapacityStub([lease("primary"), lease("backup")])
    secret = SecretStub(capacity.events)

    def factory(**kwargs: Any) -> AsyncOpenAI:
        return AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), trust_env=False,
            ),
        )

    gateway = ModelGateway(
        ModelRegistry([primary, backup]), capacity, secret,
        LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)),
        fallbacks={"primary": "backup"},
    )
    return gateway, capacity, secret


def assert_selected_only(
    wire: list[httpx.Request], capacity: CapacityStub, secret: SecretStub,
) -> None:
    assert len(wire) == 2, "must negotiate once, then stop before third HTTP or backup"
    bodies = [json.loads(item.content) for item in wire]
    assert bodies[0]["tool_choice"] == {
        "type": "function", "function": {"name": "project_inspect"},
    }
    assert bodies[1] == {**bodies[0], "tool_choice": "auto"}
    assert [item.url.path for item in wire] == ["/v1/chat/completions"] * 2
    assert [item.deployment_id for item in capacity.releases] == ["primary"]
    assert secret.references == ["secret://primary"]
    assert len([
        event for event in capacity.events if isinstance(event, tuple) and event[0] == "acquire"
    ]) == 1


@pytest.mark.parametrize("second", ["network", "503", "400", "legacy_parameter"])
async def test_negotiated_transport_failure_stops_before_third_http_or_backup(second: str) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        if len(wire) == 1:
            return rejected()
        if len(wire) > 2:
            return httpx.Response(200, json=provider_response("chat_completions", "project_inspect"))
        if second == "network":
            raise httpx.ConnectError("unknown provider outcome", request=req)
        if second == "legacy_parameter":
            return rejected("Unsupported parameter: max_completion_tokens")
        return rejected(status=int(second))

    gateway, capacity, secret = gateway_for(handle)
    # Keep a successful third response observable even if gateway erroneously falls back.
    result: object = None
    error: Exception | None = None
    try:
        result = await gateway.complete_with_context(
            request("chat_completions", required_tool="project_inspect", allow_fallback=True,
                    timeout_seconds=3),
        )
    except (ModelTransportError, ModelGatewayError) as caught:
        error = caught

    assert len(wire) >= 2, "first native 400 must actually reach the negotiated second HTTP"
    assert_selected_only(wire, capacity, secret)
    assert result is None
    assert isinstance(error, ModelTransportError)
    status = None if second == "network" else 400 if second == "legacy_parameter" else int(second)
    assert error.status_code == status
    assert [(record[1], record[3]) for record in capacity.records] == [(status, False)]
    receipt = get_gateway_failure_receipt(error)
    assert receipt is not None
    assert receipt.attempted_logical_models == ("primary",)
    assert receipt.history_complete is (status is not None)
    assert receipt.attempts[-1].status_code == status
    assert all(attempt.status_code != 200 for attempt in receipt.attempts)
    diagnostic = get_gateway_scope_diagnostic(error)
    if status is None:
        assert diagnostic is not None
        assert diagnostic.reason.value == "missing_status"
    else:
        assert diagnostic is None


async def test_negotiated_unknown_internal_error_keeps_unknown_scope_without_backup() -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        if len(wire) == 1:
            return rejected()
        raise RuntimeError("private unknown adapter failure")

    gateway, capacity, secret = gateway_for(handle)
    with pytest.raises(Exception) as caught:
        await gateway.complete_with_context(request(
            "chat_completions", required_tool="project_inspect", timeout_seconds=3,
        ))
    assert_selected_only(wire, capacity, secret)
    assert isinstance(caught.value, ModelGatewayError)
    assert not isinstance(caught.value, ModelTransportError)
    assert str(caught.value) == "model client internal failure"
    assert [(record[1], record[3]) for record in capacity.records] == [(None, False)]
    receipt = get_gateway_failure_receipt(caught.value)
    assert receipt is None or not receipt.history_complete
    diagnostic = get_gateway_scope_diagnostic(caught.value)
    assert diagnostic is not None
    assert diagnostic.phase.value == "unknown_adapter"
    assert diagnostic.reason.value == "unknown_failure"


async def test_cancelling_negotiated_http_remains_cancelled_without_backup_or_fake_status() -> None:
    wire: list[httpx.Request] = []
    entered = asyncio.Event()

    async def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        if len(wire) == 1:
            return rejected()
        entered.set()
        await asyncio.Future[None]()
        raise AssertionError("cancelled HTTP cannot deliver a response")

    gateway, capacity, secret = gateway_for(handle)
    pending = asyncio.create_task(gateway.complete_with_context(request(
        "chat_completions", required_tool="project_inspect", timeout_seconds=3,
    )))
    entered_waiter = asyncio.create_task(entered.wait())
    try:
        await asyncio.wait(
            {pending, entered_waiter}, timeout=2, return_when=asyncio.FIRST_COMPLETED,
        )
        assert len(wire) == 2, "cancellation must target the actual negotiated second HTTP"
        assert entered.is_set()
        assert not pending.done()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await pending
        assert_selected_only(wire, capacity, secret)
        assert capacity.records == []
        assert get_gateway_failure_receipt(caught.value) is None
        diagnostic = get_gateway_scope_diagnostic(caught.value)
        assert diagnostic is not None
        assert diagnostic.phase.value == "cancellation"
        assert diagnostic.reason.value == "cancelled"
    finally:
        pending.cancel()
        entered_waiter.cancel()
        await asyncio.gather(pending, entered_waiter, return_exceptions=True)


@pytest.mark.parametrize("returned_name", [None, "project_verify", "wrong_tool"])
async def test_negotiated_text_or_wrong_tool_is_rejected_locally_without_backup(
    returned_name: str | None,
) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return rejected() if len(wire) == 1 else httpx.Response(
            200, json=provider_response("chat_completions", returned_name),
        )

    gateway, capacity, secret = gateway_for(handle)
    with pytest.raises(Exception) as caught:
        await gateway.complete_with_context(request(
            "chat_completions", required_tool="project_inspect", timeout_seconds=3,
        ))
    assert_selected_only(wire, capacity, secret)
    assert isinstance(caught.value, GatewayRejectedOutput)
    assert caught.value.deployment_id == "primary"
    assert not caught.value.fallback_used
    assert caught.value.evidence is not None
    assert caught.value.evidence.reason == "invalid_tool"
    assert caught.value.evidence.usage == TokenUsage(2, 3, 5)
    assert not caught.value.evidence.correction_eligible
    assert all(not record[3] for record in capacity.records)
    receipt = get_gateway_failure_receipt(caught.value)
    assert receipt is None or not receipt.history_complete
    diagnostic = get_gateway_scope_diagnostic(caught.value)
    assert diagnostic is not None
    assert diagnostic.reason.value == "rejected_output"


@pytest.mark.parametrize("schema", [None, StructuredResponseSchema("Result", {
    "type": "object", "properties": {}, "additionalProperties": False,
})])
async def test_negotiated_real_tool_call_and_usage_are_delivered_without_mutating_contract(
    schema: StructuredResponseSchema | None,
) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return rejected() if len(wire) == 1 else httpx.Response(
            200, json=provider_response("chat_completions", "project_inspect"),
        )

    gateway, capacity, secret = gateway_for(handle)
    model_request = request(
        "chat_completions", required_tool="project_inspect", response_schema=schema,
        timeout_seconds=3,
    )
    completion = await gateway.complete_with_context(model_request)
    assert_selected_only(wire, capacity, secret)
    assert completion.deployment_id == "primary"
    assert completion.logical_model == "primary"
    assert completion.attempted_logical_models == ("primary",)
    assert not completion.fallback_used
    assert completion.response.text is None
    assert completion.response.tool_calls == (
        ToolCall("call_model", "project_inspect", {"target": "workspace"}),
    )
    assert completion.response.usage == TokenUsage(2, 3, 5)
    assert [(record[1], record[3]) for record in capacity.records] == [(200, True)]
    assert get_gateway_scope_diagnostic(completion) is None
    assert model_request.required_tool == "project_inspect"
    assert model_request.response_schema is schema
    assert all(item.capabilities == model_request.required_capabilities
               for item in capacity.configured)
    body = json.loads(wire[0].content)
    assert body["model"] == "test-model"
    assert body["messages"] == [{"role": "user", "content": "Verify project"}]
    assert [tool["function"]["name"] for tool in body["tools"]] == [
        "project_verify", "project_inspect",
    ]
    if schema is not None:
        assert body["response_format"] == {"type": "json_schema", "json_schema": {
            "name": "Result", "strict": True,
            "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        }}


@pytest.mark.parametrize("first", ["network", "503", "empty"])
async def test_ordinary_legacy_gateway_fallback_still_delivers_backup(first: str) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        if len(wire) == 1:
            if first == "network":
                raise httpx.ConnectError("legacy unavailable", request=req)
            if first == "503":
                return rejected("legacy unavailable", status=503)
            payload = provider_response("chat_completions", None)
            payload["choices"][0]["message"]["content"] = ""
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=provider_response("chat_completions", None))

    gateway, capacity, secret = gateway_for(handle)
    completion = await gateway.complete_with_context(request("chat_completions", timeout_seconds=3))
    assert len(wire) == 2
    assert [json.loads(item.content)["model"] for item in wire] == ["test-model", "backup-model"]
    assert secret.references == ["secret://primary", "secret://backup"]
    assert [item.deployment_id for item in capacity.releases] == ["primary", "backup"]
    assert completion.response.text == "done"
    assert completion.response.usage == TokenUsage(2, 3, 5)
    assert completion.deployment_id == "backup"
    assert completion.fallback_used
    assert completion.attempted_logical_models == ("primary", "backup")
    assert [record[3] for record in capacity.records] == [False, True]
