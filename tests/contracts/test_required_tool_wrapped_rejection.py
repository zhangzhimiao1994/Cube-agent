from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import httpx
import pytest
from openai import BadRequestError

from agent_hub.models import litellm_client
from agent_hub.models.litellm_client import ModelResponseError, ModelTransportError
from agent_hub.models.types import StructuredResponseSchema, TokenUsage, ToolCall
from tests.contracts.test_required_tool_negotiation import (
    REJECTION,
    rejected,
)
from tests.contracts.test_required_tool_transport import (
    deployment,
    provider_response,
    request,
    transport,
)

WRAPPED = "<400> InternalError.Algo.InvalidParameter: " + REJECTION


@pytest.mark.parametrize("period", ["", "."])
@pytest.mark.parametrize("name", ["project_inspect", "project_verify"])
@pytest.mark.parametrize("schema", [None, StructuredResponseSchema("Result", {
    "type": "object", "properties": {}, "additionalProperties": False,
})])
async def test_exact_public_wrapper_negotiates_once_through_native_sdk(
    period: str, name: str, schema: StructuredResponseSchema | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire: list[dict[str, Any]] = []
    errors: list[BadRequestError] = []
    negotiate = litellm_client._should_negotiate_auto_tool_choice

    def observe(error: Exception, model_request: Any, kwargs: Any) -> bool:
        assert type(error) is BadRequestError
        assert error.status_code == error.response.status_code == 400
        assert type(error.body) is dict
        body = cast(dict[str, object], error.body)
        assert body["code"] == "invalid_parameter_error"
        assert body["message"] == WRAPPED + period
        errors.append(error)
        return negotiate(error, model_request, kwargs)

    monkeypatch.setattr(litellm_client, "_should_negotiate_auto_tool_choice", observe)

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(json.loads(req.content))
        return rejected(WRAPPED + period) if len(wire) == 1 else httpx.Response(
            200, json=provider_response("chat_completions", name),
        )

    model_request = request("chat_completions", required_tool=name, response_schema=schema)
    result = await transport(handle).complete(deployment("chat_completions"), model_request, "mock-key")
    assert len(errors) == 1 and len(wire) == 2
    assert wire[0]["tool_choice"] == {"type": "function", "function": {"name": name}}
    assert wire[1] == {**wire[0], "tool_choice": "auto"}
    assert "required_tool" not in wire[0]
    if schema is None:
        assert "response_format" not in wire[0]
    else:
        assert wire[0]["response_format"] == {"type": "json_schema", "json_schema": {
            "name": "Result", "strict": True,
            "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        }}
    assert result.tool_calls == (ToolCall("call_model", name, {"target": "workspace"}),)
    assert result.usage == TokenUsage(2, 3, 5)
    assert model_request.required_tool == name and model_request.response_schema is schema


@pytest.mark.parametrize("returned_name", [None, "project_verify", "wrong_name"])
async def test_wrapped_negotiation_still_requires_the_selected_tool(returned_name: str | None) -> None:
    wire: list[dict[str, Any]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(json.loads(req.content))
        return rejected(WRAPPED) if len(wire) == 1 else httpx.Response(
            200, json=provider_response("chat_completions", returned_name),
        )

    with pytest.raises(ModelResponseError) as caught:
        await transport(handle).complete(deployment("chat_completions"),
            request("chat_completions", required_tool="project_inspect"), "mock-key")
    assert len(wire) == 2 and wire[1] == {**wire[0], "tool_choice": "auto"}
    assert caught.value.evidence is not None
    assert caught.value.evidence.reason == "invalid_tool"
    assert caught.value.evidence.usage == TokenUsage(2, 3, 5)
    assert not caught.value.evidence.correction_eligible


@pytest.mark.parametrize("message", [
    "private prefix " + WRAPPED,
    WRAPPED + " private suffix",
    "echoed private prompt: '" + WRAPPED + "'",
    WRAPPED + "\nprivate prompt",
    WRAPPED + "..",
    WRAPPED.replace("<400>", "<503>"),
    WRAPPED.replace("InternalError.Algo.InvalidParameter", "BadRequestError"),
    WRAPPED.replace("InternalError.Algo.InvalidParameter", "InternalError.Algo.InvalidParameterError"),
    WRAPPED.replace("InternalError.Algo.InvalidParameter", "InternalError.Other.InvalidParameter"),
    WRAPPED.replace(": ", ":", 1),
    "<400> InternalError.Algo.InvalidParameter: ",
    REJECTION + " <400> InternalError.Algo.InvalidParameter",
])
async def test_wrapper_lookalikes_and_private_echo_never_negotiate(message: str) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return rejected(message)

    with pytest.raises(ModelTransportError):
        await transport(handle).complete(deployment("chat_completions"),
            request("chat_completions", required_tool="project_inspect"), "mock-key")
    assert len(wire) == 1


@pytest.mark.parametrize("code", [None, "unknown", "invalid_request_error", "INVALID_PARAMETER_ERROR"])
async def test_wrapped_rejection_requires_exact_body_code(code: object) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        body = rejected(WRAPPED).json()
        body["error"]["code"] = code
        return httpx.Response(400, json=body)

    with pytest.raises(ModelTransportError):
        await transport(handle).complete(deployment("chat_completions"),
            request("chat_completions", required_tool="project_inspect"), "mock-key")
    assert len(wire) == 1


@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_non_400_wrapped_rejection_never_negotiates(status: int) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return rejected(WRAPPED, status)

    with pytest.raises(ModelTransportError):
        await transport(handle).complete(deployment("chat_completions"),
            request("chat_completions", required_tool="project_inspect"), "mock-key")
    assert len(wire) == 1


@pytest.mark.parametrize("message", [REJECTION, WRAPPED])
async def test_forged_native_bad_request_with_response_503_never_negotiates(
    message: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = rejected(message, 503)
    response.request = httpx.Request("POST", "https://mock.example/v1/chat/completions")
    error = BadRequestError(message, response=response, body=response.json()["error"])
    error.status_code = 400
    assert error.status_code == 400 and error.response.status_code == 503
    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise error

    def handle(req: httpx.Request) -> httpx.Response:
        pytest.fail("forged SDK exception must not reach HTTP")

    client = transport(handle)
    native_factory = client._client_factory

    def factory(**kwargs: Any) -> Any:
        sdk = native_factory(**kwargs)
        monkeypatch.setattr(sdk.chat.completions, "create", create)
        return sdk

    monkeypatch.setattr(client, "_client_factory", factory)
    with pytest.raises(ModelTransportError):
        await client.complete(deployment("chat_completions"),
            request("chat_completions", required_tool="project_inspect"), "mock-key")
    assert len(calls) == 1
    assert calls[0]["tool_choice"] == {"type": "function", "function": {"name": "project_inspect"}}


async def test_wrapped_rejection_without_required_tool_never_negotiates() -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return rejected(WRAPPED)

    with pytest.raises(ModelTransportError):
        await transport(handle).complete(deployment("chat_completions"), request("chat_completions"), "mock-key")
    assert len(wire) == 1 and json.loads(wire[0].content)["tool_choice"] == "auto"


@pytest.mark.parametrize("first_kind", ["legacy_tokens", "root_base"])
async def test_prior_token_or_v1_negotiation_cannot_chain_wrapped_auto(first_kind: str) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        if len(wire) == 1:
            return rejected("Unsupported parameter: max_completion_tokens") if first_kind == "legacy_tokens" else rejected("Not found", 404)
        return rejected(WRAPPED)

    selected = deployment("chat_completions")
    if first_kind == "root_base":
        selected = replace(selected, api_base="https://mock.example")
    with pytest.raises(ModelTransportError):
        await transport(handle).complete(selected,
            request("chat_completions", required_tool="project_inspect"), "mock-key")
    assert len(wire) == 2
    bodies = [json.loads(req.content) for req in wire]
    assert all(body["tool_choice"] == {
        "type": "function", "function": {"name": "project_inspect"},
    } for body in bodies)
    if first_kind == "legacy_tokens":
        expected = dict(bodies[0])
        expected["max_tokens"] = expected.pop("max_completion_tokens")
        assert bodies[1] == expected
    else:
        assert bodies[1] == bodies[0]
        assert [req.url.path for req in wire] == ["/chat/completions", "/v1/chat/completions"]


@pytest.mark.parametrize("second_kind", ["legacy_tokens", "root_base", "wrapped", "429", "503"])
async def test_wrapped_auto_negotiation_cannot_enable_a_third_request(second_kind: str) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        if len(wire) == 1:
            return rejected(WRAPPED)
        if second_kind == "legacy_tokens":
            return rejected("Unsupported parameter: max_completion_tokens")
        if second_kind == "root_base":
            return rejected("Not found", 404)
        return rejected(WRAPPED, int(second_kind) if second_kind in {"429", "503"} else 400)

    selected = replace(deployment("chat_completions"), api_base="https://mock.example")
    with pytest.raises(ModelTransportError):
        await transport(handle).complete(selected,
            request("chat_completions", required_tool="project_inspect"), "mock-key")
    assert len(wire) == 2
    bodies = [json.loads(req.content) for req in wire]
    assert bodies[1] == {**bodies[0], "tool_choice": "auto"}
    assert [req.url.path for req in wire] == ["/chat/completions"] * 2
