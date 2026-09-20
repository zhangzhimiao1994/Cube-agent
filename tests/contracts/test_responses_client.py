from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_hub.models.litellm_client import LiteLLMClient, ModelResponseError, ModelTransportError
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    StructuredResponseSchema,
    ToolDefinition,
)
from tests.contracts.test_litellm_client import (
    API_KEY,
    PROMPT,
    RAW_ERROR,
    AsyncChunkStream,
    sdk_response,
)


def deployment(**updates: Any) -> Deployment:
    return Deployment(
        **{
            "id": "native",
            "logical_model": "primary",
            "provider_model": "provider/original",
            "request_model": "original",
            "api_base": "https://provider.example/v1",
            "structured_output_api": "responses",
            "capabilities": frozenset(
                {
                    ModelCapability.TEXT,
                    ModelCapability.STRUCTURED_OUTPUT,
                    ModelCapability.TOOL_CALLING,
                }
            ),
            **updates,
        }
    )


def request(**updates: Any) -> ModelRequest:
    return ModelRequest(
        **{
            "logical_model": "primary",
            "messages": (
                ModelMessage(role="system", content="Exact JSON."),
                ModelMessage(role="user", content=PROMPT),
            ),
            "required_capabilities": frozenset({ModelCapability.STRUCTURED_OUTPUT}),
            "response_schema": StructuredResponseSchema(
                name="Verdict",
                schema={
                    "type": "object",
                    "properties": {"verdict": {"type": "string", "enum": ("approve",)}},
                    "required": ("verdict",),
                    "additionalProperties": False,
                },
            ),
            "timeout_seconds": 5,
            "max_output_tokens": 300,
            **updates,
        }
    )


def message(text: str = '{"verdict":"approve"}', **updates: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [SimpleNamespace(type="output_text", text=text)],
            **updates,
        }
    )


def response(**updates: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "id": "resp_safe",
            "status": "completed",
            "model": "original",
            "output": [message()],
            "error": None,
            "incomplete_details": None,
            "usage": SimpleNamespace(input_tokens=12, output_tokens=117, total_tokens=129),
            **updates,
        }
    )


def setup_client(
    result: object | None = None,
) -> tuple[LiteLLMClient, MagicMock, AsyncMock, AsyncMock, AsyncMock]:
    native = AsyncMock(return_value=response() if result is None else result)
    chat = AsyncMock(return_value=sdk_response())
    close = AsyncMock()
    factory = MagicMock(
        return_value=SimpleNamespace(
            responses=SimpleNamespace(create=native),
            chat=SimpleNamespace(completions=SimpleNamespace(create=chat)),
            close=close,
        )
    )
    return LiteLLMClient(client_factory=factory), factory, native, chat, close


async def test_native_wire_preserves_original_schema_messages_model_budget_and_usage() -> None:
    client, factory, native, chat, close = setup_client(
        response(
            output=[
                SimpleNamespace(type="reasoning", summary=[RAW_ERROR], content=API_KEY),
                message(),
            ]
        )
    )
    req = request()
    result = await client.complete(deployment(), req, API_KEY)
    factory.assert_called_once_with(
        api_key=API_KEY, base_url="https://provider.example/v1", max_retries=0
    )
    chat.assert_not_called()
    assert native.await_count == 1
    assert native.await_args is not None and req.response_schema is not None
    kwargs = native.await_args.kwargs
    assert kwargs["model"] == "original"
    assert kwargs["input"] == [
        {"role": "system", "content": "Exact JSON."},
        {"role": "user", "content": PROMPT},
    ]
    assert kwargs["max_output_tokens"] == 300 and kwargs["timeout"] == 5
    assert kwargs["stream"] is False
    assert kwargs["store"] is False
    assert kwargs["text"] == {
        "format": {
            "type": "json_schema",
            "name": "Verdict",
            "strict": True,
            "schema": json.loads(json.dumps(req.response_schema.schema, default=dict)),
        }
    }
    assert "response_format" not in kwargs and "max_completion_tokens" not in kwargs
    assert (
        "reasoning" not in kwargs
        and "previous_response_id" not in kwargs
        and "background" not in kwargs
    )
    assert result.text == '{"verdict":"approve"}'
    assert result.usage is not None
    assert (
        result.usage.prompt_tokens,
        result.usage.completion_tokens,
        result.usage.total_tokens,
    ) == (12, 117, 129)
    assert result.provider_metadata["api_protocol"] == "responses"
    assert result.provider_metadata["finish_reason"] == "stop"
    assert RAW_ERROR not in repr(result) and API_KEY not in repr(result)
    close.assert_awaited_once()


async def test_native_tools_use_call_id_and_preserve_current_runtime_continuation() -> None:
    client, _, native, chat, close = setup_client()
    native.side_effect = [
        response(
            output=[
                SimpleNamespace(
                    type="function_call",
                    id="item_not_call_id",
                    call_id="call_actual",
                    name="read_context",
                    arguments='{"query":"rules"}',
                    status="completed",
                )
            ]
        ),
        response(),
    ]
    req = request(
        tools=(
            ToolDefinition(
                name="read_context",
                description="Read authorized context",
                parameters={"type": "object"},
            ),
        ),
        required_capabilities=frozenset(
            {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING}
        ),
    )
    first = await client.complete(deployment(), req, API_KEY)
    assert first.text is None
    assert first.provider_metadata["finish_reason"] == "tool_calls"
    assert len(first.tool_calls) == 1 and first.tool_calls[0].id == "call_actual"
    assert first.tool_calls[0].arguments == {"query": "rules"}
    tool_result = ModelMessage(
        role="user",
        content='UNTRUSTED_CAPABILITY_RESULTS_JSON=[{"name":"read_context","result":"authorized rules"}]',
    )
    second = await client.complete(
        deployment(), replace(req, messages=(*req.messages, tool_result)), API_KEY
    )
    assert second.text == '{"verdict":"approve"}'
    first_wire, second_wire = [call.kwargs for call in native.await_args_list]
    assert first_wire["tools"] == [
        {
            "type": "function",
            "name": "read_context",
            "description": "Read authorized context",
            "parameters": {"type": "object"},
        }
    ]
    assert first_wire["tool_choice"] == "auto"
    assert second_wire["tools"] == first_wire["tools"]
    assert second_wire["text"] == first_wire["text"]
    assert second_wire["input"][-1] == {"role": "user", "content": tool_result.content}
    chat.assert_not_called()
    assert close.await_count == 2


@pytest.mark.parametrize(
    "text",
    [
        "prose",
        '```json\n{"verdict":"approve"}\n```',
        "{}",
        '{"verdict":"reject"}',
        '{"verdict":true}',
        '{"verdict":"approve","extra":1}',
        '{"verdict":"approve","verdict":"approve"}',
        '{"verdict":NaN}',
    ],
)
async def test_native_final_text_must_exactly_match_schema(text: str) -> None:
    client, _, native, chat, close = setup_client(response(output=[message(text)]))
    with pytest.raises(ModelResponseError):
        await client.complete(deployment(), request(), API_KEY)
    assert native.await_count == 1
    chat.assert_not_called()
    close.assert_awaited_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "incomplete"},
        {"status": "failed"},
        {"status": "in_progress"},
        {"error": {"message": RAW_ERROR}},
        {"incomplete_details": {"reason": "max_output_tokens"}},
        {"output": []},
        {"output": [message(content=[SimpleNamespace(type="refusal", refusal=RAW_ERROR)])]},
        {"output": [message(status="incomplete")]},
        {"output": [SimpleNamespace(type="web_search_call")]},
        {"usage": None},
        {"usage": SimpleNamespace(input_tokens=True, output_tokens=1, total_tokens=2)},
        {"usage": SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=3)},
        {
            "output": [
                SimpleNamespace(
                    type="function_call", id="item", call_id="", name="read_context", arguments="{}"
                )
            ]
        },
        {
            "output": [
                SimpleNamespace(
                    type="function_call", call_id="call_1", name="unapproved", arguments="{}"
                )
            ]
        },
    ],
)
async def test_native_rejects_nonfinal_malformed_refusal_and_unmetered_output(
    changes: dict[str, Any],
) -> None:
    client, _, _, chat, close = setup_client(response(**changes))
    with pytest.raises(ModelResponseError) as caught:
        await client.complete(deployment(), request(), API_KEY)
    assert RAW_ERROR not in str(caught.value)
    chat.assert_not_called()
    close.assert_awaited_once()


async def test_400_never_switches_protocol_or_exposes_provider_body() -> None:
    client, _, native, chat, close = setup_client()
    error = RuntimeError(RAW_ERROR + API_KEY + PROMPT)
    error.status_code = 400  # type: ignore[attr-defined]
    native.side_effect = error
    with pytest.raises(ModelTransportError) as caught:
        await client.complete(deployment(), request(), API_KEY)
    assert caught.value.status_code == 400
    assert all(value not in str(caught.value) for value in (RAW_ERROR, API_KEY, PROMPT))
    assert native.await_count == 1
    chat.assert_not_called()
    close.assert_awaited_once()


async def test_cancel_closes_native_client_and_propagates() -> None:
    client, _, native, chat, close = setup_client()
    entered = asyncio.Event()

    async def block(**kwargs: object) -> object:
        entered.set()
        return await asyncio.Future()

    native.side_effect = block
    pending = asyncio.create_task(client.complete(deployment(), request(), API_KEY))
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    chat.assert_not_called()
    close.assert_awaited_once()


async def test_native_stream_and_multimodal_input_fail_before_creating_client() -> None:
    client, factory, _, _, _ = setup_client()
    with pytest.raises(ValueError, match="stream"):
        client.stream_openai_compatible_chunks(deployment(), request(), API_KEY)
    req = request(
        messages=(
            ModelMessage(
                role="user",
                content=(
                    {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
                ),
            ),
        )
    )
    with pytest.raises(ValueError, match="input"):
        await client.complete(deployment(), req, API_KEY)
    factory.assert_not_called()


async def test_plain_requests_and_streams_keep_chat_path_with_responses_configured() -> None:
    client, _, native, chat, close = setup_client()
    req = request(response_schema=None, required_capabilities=frozenset({ModelCapability.TEXT}))
    result = await client.complete(deployment(), req, API_KEY)
    assert result.text == "hello"
    native.assert_not_called()
    chat.assert_awaited_once()
    close.assert_awaited_once()
    stream = client.stream_openai_compatible_chunks(deployment(), req, API_KEY)
    await stream.aclose()  # type: ignore[attr-defined]


async def test_legacy_schema_default_keeps_chat_completions() -> None:
    client, _, native, chat, _ = setup_client()
    await client.complete(deployment(structured_output_api="chat_completions"), request(), API_KEY)
    native.assert_not_called()
    assert chat.await_args is not None
    assert chat.await_args.kwargs["response_format"]["type"] == "json_schema"


@pytest.mark.parametrize(
    "arguments", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', "[]", "not-json"]
)
async def test_native_tool_arguments_are_strict_json(arguments: str) -> None:
    client, _, _, _, close = setup_client(
        response(
            output=[
                SimpleNamespace(
                    type="function_call",
                    call_id="call_1",
                    name="read_context",
                    arguments=arguments,
                )
            ]
        )
    )
    req = request(
        tools=(
            ToolDefinition(name="read_context", description="Read", parameters={"type": "object"}),
        ),
        required_capabilities=frozenset(
            {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING}
        ),
    )
    with pytest.raises(ModelResponseError):
        await client.complete(deployment(), req, API_KEY)
    close.assert_awaited_once()


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.com/remote.json"},
        {"$dynamicRef": "#internal"},
        {"properties": {"format": {"$ref": "https://example.com/remote.json"}}},
        {"items": {"$ref": "https://example.com/remote.json"}},
        {"type": "string", "format": "unknown-format"},
        {"type": "not-a-type"},
        {"$schema": "https://example.com/unknown-dialect", "type": "object"},
    ],
)
async def test_unsupported_schema_rejected_before_client_creation(schema: dict[str, Any]) -> None:
    client, factory, _, _, _ = setup_client()
    req = request(response_schema=StructuredResponseSchema(name="Invalid", schema=schema))
    with pytest.raises(ValueError):
        await client.complete(deployment(), req, API_KEY)
    factory.assert_not_called()


async def test_error_item_cannot_be_hidden_behind_valid_json() -> None:
    client, _, _, _, close = setup_client(
        response(
            output=[
                message(),
                SimpleNamespace(type="reasoning", status="failed", error=RAW_ERROR),
            ]
        )
    )
    with pytest.raises(ModelResponseError):
        await client.complete(deployment(), request(), API_KEY)
    close.assert_awaited_once()


async def test_timeout_and_close_failure_are_bounded_and_redacted() -> None:
    client, _, native, chat, close = setup_client()

    async def block(**kwargs: object) -> object:
        return await asyncio.Future()

    native.side_effect = block
    async with asyncio.timeout(1):
        with pytest.raises(ModelTransportError):
            await client.complete(deployment(), request(timeout_seconds=0.01), API_KEY)
    close.assert_awaited_once()
    chat.assert_not_called()
    native.side_effect = None
    close.side_effect = RuntimeError(RAW_ERROR + API_KEY)
    with pytest.raises(ModelTransportError) as caught:
        await client.complete(deployment(), request(), API_KEY)
    assert RAW_ERROR not in str(caught.value) and API_KEY not in str(caught.value)


def test_native_encoder_disables_server_storage_explicitly() -> None:
    from agent_hub.models.responses import response_create_kwargs

    kwargs = response_create_kwargs(deployment(), request())
    assert kwargs["store"] is False
    assert "background" not in kwargs and "previous_response_id" not in kwargs


def test_schema_business_field_names_are_not_schema_keywords() -> None:
    from agent_hub.models.responses import response_create_kwargs

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "format": {"type": "string"},
            "$ref": {"type": "string"},
            "$schema": {"type": "string"},
            "$dynamicRef": {"type": "string"},
        },
        "additionalProperties": False,
    }
    req = request(response_schema=StructuredResponseSchema(name="BusinessFields", schema=schema))
    payload = response_create_kwargs(deployment(), req)
    assert payload["text"] == {
        "format": {
            "type": "json_schema",
            "name": "BusinessFields",
            "schema": schema,
            "strict": True,
        }
    }


@pytest.mark.parametrize(
    "output",
    [
        [message('{"verdict":'), message('"approve"}')],
        [
            message("not a final result"),
            SimpleNamespace(
                type="function_call", call_id="call_1", name="read_context", arguments="{}"
            ),
        ],
    ],
)
async def test_split_json_and_mixed_text_calls_are_not_valid_results(output: list[object]) -> None:
    client, _, _, _, _ = setup_client(response(output=output))
    req = request(
        tools=(
            ToolDefinition(name="read_context", description="Read", parameters={"type": "object"}),
        ),
        required_capabilities=frozenset(
            {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING}
        ),
    )
    with pytest.raises(ModelResponseError):
        await client.complete(deployment(), req, API_KEY)


async def test_duplicate_native_call_ids_are_rejected() -> None:
    item = SimpleNamespace(
        type="function_call", call_id="same_call", name="read_context", arguments="{}"
    )
    client, _, _, _, _ = setup_client(response(output=[item, item]))
    req = request(
        tools=(
            ToolDefinition(name="read_context", description="Read", parameters={"type": "object"}),
        ),
        required_capabilities=frozenset(
            {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING}
        ),
    )
    with pytest.raises(ModelResponseError):
        await client.complete(deployment(), req, API_KEY)


async def test_close_timeout_is_bounded_and_does_not_replace_primary_error() -> None:
    client, _, native, _, close = setup_client()

    async def block() -> None:
        await asyncio.Future()

    close.side_effect = block
    native.side_effect = RuntimeError(RAW_ERROR)
    async with asyncio.timeout(1):
        with pytest.raises(ModelTransportError) as caught:
            await client.complete(deployment(), request(timeout_seconds=0.01), API_KEY)
    assert RAW_ERROR not in str(caught.value)
    close.assert_awaited_once()


async def test_no_schema_tool_stream_still_uses_existing_chat_decoder() -> None:
    client, _, native, chat, close = setup_client()
    chunk = SimpleNamespace(id="chunk_safe", choices=[])
    chat.return_value = AsyncChunkStream([chunk])
    req = request(
        response_schema=None,
        tools=(
            ToolDefinition(name="read_context", description="Read", parameters={"type": "object"}),
        ),
        required_capabilities=frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING}),
    )
    chunks = [
        item async for item in client.stream_openai_compatible_chunks(deployment(), req, API_KEY)
    ]
    assert chunks == [chunk]
    native.assert_not_called()
    assert chat.await_args is not None
    assert chat.await_args.kwargs["stream"] is True
    assert chat.await_args.kwargs["tools"][0]["function"]["name"] == "read_context"
    close.assert_awaited_once()


async def test_messages_endpoint_does_not_bypass_existing_schema_rejection() -> None:
    client, factory, _, _, _ = setup_client()
    with pytest.raises(ValueError, match="messages endpoint response schemas"):
        await client.complete(
            deployment(api_base="https://provider.example/v1/messages"), request(), API_KEY
        )
    factory.assert_not_called()
