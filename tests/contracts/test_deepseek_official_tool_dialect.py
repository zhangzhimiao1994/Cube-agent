"""Real SDK framing with in-memory HTTP only; never calls a provider or a tool."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from agent_hub.models.litellm_client import ModelResponseError
from agent_hub.models.types import Deployment, ModelRequest, TokenUsage
from tests.contracts.test_required_tool_transport import (
    deployment,
    provider_response,
    request,
    transport,
)


def official_deployment(
    *, base: str = "https://api.deepseek.com/v1", model: str = "deepseek-v4-flash",
) -> Deployment:
    return replace(
        deployment("responses"),
        api_base=base,
        provider_model="deepseek/deepseek-v4-flash",
        request_model=model,
    )


def required_request() -> ModelRequest:
    original = request("chat_completions", required_tool="project_inspect", max_output_tokens=24576,
                       timeout_seconds=17, allow_fallback=False)
    return replace(original, tools=tuple(tool for tool in original.tools if tool.name == "project_inspect"))


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-flash"])
@pytest.mark.parametrize("path", ["", "/", "/v1", "/v1/"])
async def test_official_flash_uses_auto_on_first_send_without_changing_logical_request(model: str, path: str) -> None:
    wire: list[dict[str, Any]] = []
    timeouts: list[float] = []
    original = required_request()

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(json.loads(req.content))
        timeouts.append(req.extensions["timeout"]["read"])
        return httpx.Response(200, json=provider_response("chat_completions", "project_inspect"))

    result = await transport(handle).complete(
        official_deployment(base="https://api.deepseek.com" + path, model=model), original, "mock-key",
    )
    assert wire == [{
        "messages": [{"role": "user", "content": "Verify project"}], "model": model,
        "max_completion_tokens": 24576, "stream": False, "tool_choice": "auto",
        "tools": [{"type": "function", "function": {
            "name": "project_inspect", "description": "Inspect project", "parameters": {"type": "object"},
        }}],
    }]
    assert timeouts == [17]
    assert original.required_tool == "project_inspect" and original.allow_fallback is False
    assert original.max_output_tokens == 24576 and len(original.tools) == 1
    assert result.tool_calls[0].name == "project_inspect" and result.usage == TokenUsage(2, 3, 5)


@pytest.mark.parametrize("base", [
    "https://proxy.example/v1", "https://api.deepseek.com.attacker.example/v1",
    "https://api.deepseek.com./v1", "http://api.deepseek.com/v1",
    "https://api.deepseek.com:8443/v1", "https://api.deepseek.com/v1?mode=test",
    "https://api.deepseek.com/v1#fragment", "https://api.deepseek.com/beta",
    "https://api.deepseek.com/custom/v1", "https://api.deepseek.com/%76%31",
])
async def test_noncanonical_endpoint_preserves_named_choice(base: str) -> None:
    wire: list[dict[str, Any]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(json.loads(req.content))
        return httpx.Response(200, json=provider_response("chat_completions", "project_inspect"))

    await transport(handle).complete(official_deployment(base=base), required_request(), "mock-key")
    assert len(wire) == 1
    assert wire[0]["tool_choice"] == {"type": "function", "function": {"name": "project_inspect"}}


@pytest.mark.parametrize("base", [
    "https://user@api.deepseek.com/v1", "https://user:password@api.deepseek.com/v1",
])
async def test_userinfo_remains_rejected_before_any_send(base: str) -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        pytest.fail("userinfo must be rejected before HTTP")

    with pytest.raises(ValueError, match="api_base"):
        await transport(handle).complete(official_deployment(base=base), required_request(), "mock-key")


@pytest.mark.parametrize("model", ["another-model", "deepseek-v4-flash-extra", "deepseek/deepseek-v4-flash"])
async def test_effective_wire_model_override_not_provider_label_selects_dialect(model: str) -> None:
    wire: list[dict[str, Any]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(json.loads(req.content))
        return httpx.Response(200, json=provider_response("chat_completions", "project_inspect"))

    await transport(handle).complete(official_deployment(model=model), required_request(), "mock-key")
    assert len(wire) == 1 and wire[0]["model"] == model
    assert wire[0]["tool_choice"] == {"type": "function", "function": {"name": "project_inspect"}}


@pytest.mark.parametrize("returned_tool", [None, "project_verify", "unoffered_tool"])
async def test_auto_dialect_still_rejects_missing_or_wrong_required_tool_with_usage(returned_tool: str | None) -> None:
    wire: list[dict[str, Any]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(json.loads(req.content))
        return httpx.Response(200, json=provider_response("chat_completions", returned_tool))

    with pytest.raises(ModelResponseError) as caught:
        await transport(handle).complete(official_deployment(), required_request(), "mock-key")
    assert len(wire) == 1 and wire[0]["tool_choice"] == "auto"
    assert caught.value.evidence is not None and caught.value.evidence.reason == "invalid_tool"
    assert caught.value.evidence.usage_status == "known"
    assert caught.value.evidence.usage == TokenUsage(2, 3, 5)
    assert caught.value.evidence.correction_eligible is False


async def test_explicit_https_default_port_uses_official_dialect() -> None:
    wire: list[dict[str, Any]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(json.loads(req.content))
        return httpx.Response(200, json=provider_response("chat_completions", "project_inspect"))

    await transport(handle).complete(
        official_deployment(base="https://api.deepseek.com:443/v1"), required_request(), "mock-key",
    )
    assert len(wire) == 1 and wire[0]["tool_choice"] == "auto"


async def test_official_responses_keeps_named_choice_and_schema() -> None:
    wire: list[dict[str, Any]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/responses"
        wire.append(json.loads(req.content))
        return httpx.Response(200, json=provider_response("responses", "project_inspect"))

    result = await transport(handle).complete(
        official_deployment(), request("responses", required_tool="project_inspect"), "mock-key",
    )
    assert len(wire) == 1 and wire[0]["tool_choice"] == {"type": "function", "name": "project_inspect"}
    assert wire[0]["text"]["format"]["type"] == "json_schema"
    assert result.tool_calls[0].name == "project_inspect"


async def test_official_streaming_keeps_original_named_choice() -> None:
    wire: list[dict[str, Any]] = []
    chunk = {"id": "chunk_static", "object": "chat.completion.chunk", "created": 1,
             "model": "deepseek-v4-flash", "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(json.loads(req.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    chunks = [item async for item in transport(handle).stream_openai_compatible_chunks(
        official_deployment(), required_request(), "mock-key",
    )]
    assert len(chunks) == 1 and len(wire) == 1
    assert wire[0]["stream"] is True
    assert wire[0]["tool_choice"] == {"type": "function", "function": {"name": "project_inspect"}}
