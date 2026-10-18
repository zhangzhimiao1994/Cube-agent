from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.models.litellm_client import (
    HTTPClientFactory,
    LiteLLMClient,
    ModelResponseError,
    OpenAIClientFactory,
)
from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    StructuredResponseSchema,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)

CAPABILITIES = frozenset({ModelCapability.TOOL_CALLING, ModelCapability.STRUCTURED_OUTPUT})


def deployment(protocol: str) -> Deployment:
    return Deployment(
        id="primary", logical_model="primary", provider_model="openai/test-model",
        request_model="test-model", api_base="https://mock.example/v1",
        capabilities=CAPABILITIES, structured_output_api=protocol,  # type: ignore[arg-type]
    )


def request(protocol: str, **updates: Any) -> ModelRequest:
    return ModelRequest(**{
        "logical_model": "primary",
        "messages": (ModelMessage("user", "Verify project"),),
        "required_capabilities": CAPABILITIES,
        "tools": (
            ToolDefinition("project_verify", "Verify project", {"type": "object"}),
            ToolDefinition("project_inspect", "Inspect project", {"type": "object"}),
        ),
        "response_schema": StructuredResponseSchema(
            "Result", {"type": "object", "properties": {}, "additionalProperties": False},
        ) if protocol == "responses" else None,
        **updates,
    })


def provider_response(protocol: str, name: str | None = "project_verify") -> dict[str, Any]:
    if protocol == "responses":
        output = [{
            "id": "fc_model", "type": "function_call", "call_id": "call_model",
            "name": name, "arguments": '{"target":"workspace"}', "status": "completed",
        }] if name is not None else [{
            "id": "msg_model", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "{}", "annotations": []}],
        }]
        return {
            "id": "resp_model", "object": "response", "created_at": 1,
            "model": "test-model", "status": "completed", "output": output,
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        }
    return {
        "id": "chat_model", "object": "chat.completion", "created": 1, "model": "test-model",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if name else "stop", "message": {
            "role": "assistant", "content": None if name else "done",
            "tool_calls": [{"id": "call_model", "type": "function", "function": {
                "name": name, "arguments": '{"target":"workspace"}',
            }}] if name else [],
        }}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }


def transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> LiteLLMClient:
    def factory(**kwargs: Any) -> AsyncOpenAI:
        assert kwargs["max_retries"] == 0
        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    def http_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(handler))

    return LiteLLMClient(
        client_factory=cast(OpenAIClientFactory, factory),
        http_client_factory=cast(HTTPClientFactory, http_factory),
    )


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("name", ["project_verify", "project_inspect"])
async def test_named_tool_choice_reaches_real_sdk_http_and_preserves_model_call(
    protocol: str, name: str,
) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return httpx.Response(200, json=provider_response(protocol, name))

    result = await transport(handle).complete(
        deployment(protocol), request(protocol, required_tool=name), "mock-key",
    )
    assert len(wire) == 1
    assert wire[0].url.path == (
        "/v1/responses" if protocol == "responses" else "/v1/chat/completions"
    )
    body = json.loads(wire[0].content)
    assert body["tool_choice"] == (
        {"type": "function", "name": name} if protocol == "responses"
        else {"type": "function", "function": {"name": name}}
    )
    assert "required_tool" not in body
    assert result.tool_calls == (ToolCall("call_model", name, {"target": "workspace"}),)
    assert result.text is None
    assert result.usage == TokenUsage(2, 3, 5)


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_default_none_preserves_legacy_wire_bytes(protocol: str) -> None:
    wire: list[bytes] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req.content)
        return httpx.Response(200, json=provider_response(protocol))

    client = transport(handle)
    req = request(protocol)
    await client.complete(deployment(protocol), req, "mock-key")
    await client.complete(deployment(protocol), replace(req, required_tool=None), "mock-key")
    # Golden bytes captured from the pre-feature SDK transport.
    expected = LEGACY_RESPONSES if protocol == "responses" else LEGACY_CHAT
    assert wire == [expected, expected]


LEGACY_CHAT = (
    b'{"messages":[{"role":"user","content":"Verify project"}],"model":"test-model",'
    b'"max_completion_tokens":4096,"stream":false,"tool_choice":"auto","tools":['
    b'{"type":"function","function":{"name":"project_verify","description":"Verify project",'
    b'"parameters":{"type":"object"}}},{"type":"function","function":{"name":"project_inspect",'
    b'"description":"Inspect project","parameters":{"type":"object"}}}]}'
)
LEGACY_RESPONSES = (
    b'{"input":[{"role":"user","content":"Verify project"}],"max_output_tokens":4096,'
    b'"model":"test-model","store":false,"stream":false,"text":{"format":{'
    b'"type":"json_schema","name":"Result","schema":{"type":"object","properties":{},'
    b'"additionalProperties":false},"strict":true}},"tool_choice":"auto","tools":['
    b'{"type":"function","name":"project_verify","description":"Verify project",'
    b'"parameters":{"type":"object"}},{"type":"function","name":"project_inspect",'
    b'"description":"Inspect project","parameters":{"type":"object"}}]}'
)


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("returned_name", [None, "project_inspect", "forged_name"])
async def test_required_call_cannot_be_replaced_by_text_or_another_tool_or_retried(
    protocol: str, returned_name: str | None,
) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return httpx.Response(200, json=provider_response(protocol, returned_name))

    with pytest.raises(ModelResponseError) as caught:
        await transport(handle).complete(
            deployment(protocol), request(protocol, required_tool="project_verify"), "mock-key",
        )
    assert len(wire) == 1
    assert caught.value.evidence is not None
    assert caught.value.evidence.reason == "invalid_tool"
    assert caught.value.evidence.usage == TokenUsage(2, 3, 5)
    assert not caught.value.evidence.correction_eligible


async def test_required_tool_rejects_messages_endpoint_before_transport() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        pytest.fail("unsupported required tool must not reach HTTP")

    selected = replace(deployment("chat_completions"), api_base="https://mock.example/v1/messages")
    with pytest.raises(ValueError, match="required_tool"):
        await transport(handle).complete(
            selected, request("chat_completions", required_tool="project_verify"), "mock-key",
        )


@pytest.mark.parametrize("text", ["not json", '{"unexpected":1}'])
async def test_responses_ignored_required_tool_is_not_json_correction_permission(text: str) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        response = provider_response("responses", None)
        response["output"][0]["content"][0]["text"] = text
        return httpx.Response(200, json=response)

    with pytest.raises(ModelResponseError) as caught:
        await transport(handle).complete(
            deployment("responses"), request("responses", required_tool="project_verify"),
            "mock-key",
        )
    assert len(wire) == 1
    assert caught.value.evidence is not None
    assert caught.value.evidence.reason == "invalid_tool"
    assert caught.value.evidence.usage == TokenUsage(2, 3, 5)
    assert not caught.value.evidence.correction_eligible


async def test_named_tool_choice_preserves_native_call_result_history() -> None:
    wire: list[dict[str, Any]] = []
    raw_arguments = '{ "target" : "workspace" }'
    previous = NativeAssistantMessage(
        "primary", None,
        (NativeToolCall("call_previous", "project_inspect", raw_arguments),),
        "private reasoning",
    )

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(json.loads(req.content))
        return httpx.Response(200, json=provider_response("chat_completions"))

    result = await transport(handle).complete(
        deployment("chat_completions"),
        request("chat_completions", required_tool="project_verify", native_tool_continuation=True,
                messages=(
                    ModelMessage("user", "Verify project"),
                    ModelMessage("assistant", "", native_assistant=previous),
                    ModelMessage("tool", "inspected", tool_call_id="call_previous"),
                )),
        "mock-key",
    )
    assert len(wire) == 1
    assert wire[0]["tool_choice"] == {"type": "function", "function": {"name": "project_verify"}}
    assert wire[0]["messages"][1] == {
        "role": "assistant", "content": None, "reasoning_content": "private reasoning",
        "tool_calls": [{"id": "call_previous", "type": "function", "function": {
            "name": "project_inspect", "arguments": raw_arguments,
        }}],
    }
    assert wire[0]["messages"][2] == {
        "role": "tool", "content": "inspected", "tool_call_id": "call_previous",
    }
    assert result.native_assistant is not None
    assert result.native_assistant.tool_calls == (
        NativeToolCall("call_model", "project_verify", '{"target":"workspace"}'),
    )


async def test_streaming_chat_sends_named_choice_and_returns_provider_chunks() -> None:
    wire: list[dict[str, Any]] = []
    chunk = {
        "id": "chunk_model", "object": "chat.completion.chunk", "created": 1,
        "model": "test-model", "choices": [{
            "index": 0, "delta": {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_model", "type": "function", "function": {
                    "name": "project_verify", "arguments": '{"target":"workspace"}',
                },
            }]}, "finish_reason": "tool_calls",
        }],
    }

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(json.loads(req.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

    chunks = [part async for part in transport(handle).stream_openai_compatible_chunks(
        deployment("chat_completions"),
        request("chat_completions", required_tool="project_verify"), "mock-key",
    )]
    assert len(wire) == 1
    assert wire[0]["tool_choice"] == {"type": "function", "function": {"name": "project_verify"}}
    assert wire[0]["stream"] is True
    assert len(chunks) == 1
    assert chunks[0].choices[0].delta.tool_calls[0].id == "call_model"  # type: ignore[attr-defined]
    assert chunks[0].choices[0].delta.tool_calls[0].function.name == "project_verify"  # type: ignore[attr-defined]
