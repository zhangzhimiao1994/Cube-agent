import asyncio
import traceback
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_hub.models.litellm_client import (
    LiteLLMClient,
    ModelResponseError,
    ModelTransportError,
)
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    StructuredResponseSchema,
    ToolCall,
)

API_KEY = "runtime-" + "sentinel-secret"
PROMPT = "private-" + "prompt-sentinel"
RAW_ERROR = "raw-" + "provider-body-sentinel"
MULTIMODAL_TEXT = "multimodal-" + "text-sentinel"
IMAGE_URL = "https://images.example.com/private-" + "image-sentinel"


def sdk_response(
    *,
    content: str | None = "hello",
    tool_calls: list[object] | None = None,
    choices: list[object] | None = None,
) -> object:
    if choices is None:
        choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason="stop",
            )
        ]
    return SimpleNamespace(
        id="req_safe123",
        _request_id="sdk_req_safe123",
        model="openai/gpt-4o-mini",
        created=123456,
        system_fingerprint="fp_safe",
        choices=choices,
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5),
        forbidden=API_KEY,
    )


def mock_transport(
    *,
    result: object | None = None,
    error: BaseException | None = None,
    close_error: BaseException | None = None,
) -> tuple[LiteLLMClient, MagicMock, AsyncMock, AsyncMock]:
    create = AsyncMock()
    if error is not None:
        create.side_effect = error
    else:
        create.return_value = sdk_response() if result is None else result
    close = AsyncMock()
    if close_error is not None:
        close.side_effect = close_error
    sdk_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=close,
    )
    factory = MagicMock(return_value=sdk_client)
    return LiteLLMClient(client_factory=factory), factory, create, close


def deployment(**overrides: object) -> Deployment:
    values: dict[str, object] = {
        "id": "primary-1",
        "logical_model": "primary",
        "provider_model": "deepseek/deepseek-chat",
        "api_base": "https://proxy.example.com/v1",
    }
    values.update(overrides)
    return Deployment(**values)  # type: ignore[arg-type]


def request(**overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "logical_model": "primary",
        "messages": [ModelMessage(role="user", content=PROMPT)],
        "timeout_seconds": 12,
    }
    values.update(overrides)
    return ModelRequest(**values)  # type: ignore[arg-type]


def sensitive_request() -> ModelRequest:
    return request(
        messages=[
            ModelMessage(role="user", content=PROMPT),
            ModelMessage(
                role="user",
                content=[
                    {"type": "text", "text": MULTIMODAL_TEXT},
                    {"type": "image_url", "image_url": {"url": IMAGE_URL}},
                ],  # type: ignore[arg-type]
            ),
        ]
    )


def malformed_sensitive_response() -> object:
    tool = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(
            name="lookup",
            arguments=RAW_ERROR + API_KEY + PROMPT + " {not-json",
        ),
    )
    return sdk_response(tool_calls=[tool])


def captured_traceback(error: BaseException) -> str:
    return "".join(
        traceback.TracebackException.from_exception(error, capture_locals=True).format()
    )


async def test_uses_exact_openai_compatible_chat_completions_surface() -> None:
    transport, factory, create, close = mock_transport()

    result = await transport.complete(deployment(), request(), API_KEY)

    factory.assert_called_once_with(
        api_key=API_KEY,
        base_url="https://proxy.example.com/v1",
        max_retries=0,
    )
    create.assert_awaited_once_with(
        model="deepseek/deepseek-chat",
        messages=[{"role": "user", "content": PROMPT}],
        timeout=12,
        stream=False,
    )
    close.assert_awaited_once_with()
    assert result.text == "hello"


async def test_normalizes_multimodal_parts_without_mutating_caller_data() -> None:
    transport, _, create, _ = mock_transport()
    caller_parts: list[dict[str, Any]] = [
        {"type": "text", "text": "inspect"},
        {
            "type": "image_url",
            "image_url": {"url": "https://images.example.com/item.png", "detail": "low"},
        },
    ]
    message = ModelMessage(role="user", content=caller_parts)  # type: ignore[arg-type]

    await transport.complete(deployment(), request(messages=[message]), API_KEY)

    assert caller_parts == [
        {"type": "text", "text": "inspect"},
        {
            "type": "image_url",
            "image_url": {"url": "https://images.example.com/item.png", "detail": "low"},
        },
    ]
    assert create.await_args is not None
    assert create.await_args.kwargs["messages"] == [
        {"role": "user", "content": caller_parts}
    ]
    assert create.await_args.kwargs["messages"][0]["content"] is not caller_parts


async def test_sends_current_json_schema_response_format() -> None:
    transport, _, create, _ = mock_transport()
    caller_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    response_schema = StructuredResponseSchema(name="answer_contract", schema=caller_schema)
    caller_schema["properties"]["answer"]["type"] = "integer"
    with pytest.raises(TypeError):
        response_schema.schema["type"] = "array"  # type: ignore[index]

    await transport.complete(
        deployment(capabilities={ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT}),
        request(
            required_capabilities={ModelCapability.STRUCTURED_OUTPUT},
            response_schema=response_schema,
        ),
        API_KEY,
    )

    assert create.await_args is not None
    assert create.await_args.kwargs["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "answer_contract",
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    }


def test_structured_schema_requires_capability_and_safe_name() -> None:
    with pytest.raises(ValueError, match="structured_output capability"):
        request(response_schema=StructuredResponseSchema(name="answer", schema={}))
    with pytest.raises(ValueError):
        StructuredResponseSchema(name="bad name", schema={})
    with pytest.raises(ValueError):
        StructuredResponseSchema(name="a" * 65, schema={})


async def test_parses_first_choice_tool_calls_usage_and_allowlisted_metadata() -> None:
    first_tool = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="lookup", arguments='{"query":"safe"}'),
    )
    ignored_choice = SimpleNamespace(
        message=SimpleNamespace(content="ignored", tool_calls=None), finish_reason="stop"
    )
    transport, _, _, _ = mock_transport(
        result=sdk_response(
            content=None,
            tool_calls=[first_tool],
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None, tool_calls=[first_tool]),
                    finish_reason="tool_calls",
                ),
                ignored_choice,
            ],
        )
    )

    result = await transport.complete(deployment(), request(), API_KEY)

    assert result.text is None
    assert result.tool_calls[0].id == "call_1"
    assert result.tool_calls[0].name == "lookup"
    assert result.tool_calls[0].arguments == {"query": "safe"}
    assert result.usage is not None
    assert result.usage.prompt_tokens == 2
    assert result.usage.completion_tokens == 3
    assert result.usage.total_tokens == 5
    assert dict(result.provider_metadata) == {
        "request_id": "sdk_req_safe123",
        "model": "openai/gpt-4o-mini",
        "created": 123456,
        "system_fingerprint": "fp_safe",
        "finish_reason": "tool_calls",
    }
    assert "forbidden" not in result.provider_metadata
    with pytest.raises(TypeError):
        result.tool_calls[0].arguments["query"] = "changed"


async def test_drops_provider_metadata_containing_runtime_secrets_or_prompt_content() -> None:
    poisoned = sdk_response()
    poisoned._request_id = "req-" + API_KEY  # type: ignore[attr-defined]
    poisoned.model = "model/" + PROMPT  # type: ignore[attr-defined]
    poisoned.system_fingerprint = "fp-" + IMAGE_URL  # type: ignore[attr-defined]
    poisoned.choices[0].finish_reason = "finish-" + MULTIMODAL_TEXT  # type: ignore[attr-defined]
    transport, _, _, _ = mock_transport(result=poisoned)

    result = await transport.complete(deployment(), sensitive_request(), API_KEY)

    assert dict(result.provider_metadata) == {"created": 123456}
    rendered = f"{result!s} {result!r} {result.provider_metadata!r}"
    for sensitive in (API_KEY, PROMPT, MULTIMODAL_TEXT, IMAGE_URL):
        assert sensitive not in rendered


async def test_drops_provider_metadata_that_is_a_fragment_of_sensitive_input() -> None:
    poisoned = sdk_response()
    poisoned._request_id = "secret"  # type: ignore[attr-defined]
    poisoned.model = "prompt-sentinel"  # type: ignore[attr-defined]
    poisoned.system_fingerprint = "text-sentinel"  # type: ignore[attr-defined]
    poisoned.choices[0].finish_reason = "image-sentinel"  # type: ignore[attr-defined]
    transport, _, _, _ = mock_transport(result=poisoned)

    result = await transport.complete(deployment(), sensitive_request(), API_KEY)

    assert dict(result.provider_metadata) == {"created": 123456}
    rendered = repr(result)
    for fragment in ("secret", "prompt-sentinel", "text-sentinel", "image-sentinel"):
        assert fragment not in rendered


@pytest.mark.parametrize(
    "fragment",
    ["secret", "prompt-sentinel", "text-sentinel", "image-sentinel"],
)
async def test_drops_error_request_id_that_is_a_fragment_of_sensitive_input(
    fragment: str,
) -> None:
    class ProviderFailure(RuntimeError):
        request_id = fragment

    transport, _, _, _ = mock_transport(error=ProviderFailure(RAW_ERROR))

    with pytest.raises(ModelTransportError) as caught:
        await transport.complete(deployment(), sensitive_request(), API_KEY)

    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert fragment not in str(caught.value)
    assert fragment not in repr(caught.value)
    assert fragment not in rendered


@pytest.mark.parametrize("leaked_value", [API_KEY, PROMPT, MULTIMODAL_TEXT, IMAGE_URL])
async def test_drops_sensitive_provider_error_request_id_without_retaining_context(
    leaked_value: str,
) -> None:
    class ProviderFailure(RuntimeError):
        status_code = 429
        request_id = "req-" + leaked_value

    transport, _, _, close = mock_transport(error=ProviderFailure(RAW_ERROR + leaked_value))

    with pytest.raises(ModelTransportError) as caught:
        await transport.complete(deployment(), sensitive_request(), API_KEY)

    close.assert_awaited_once_with()
    rendered = " ".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(
                traceback.format_exception(
                    type(caught.value), caught.value, caught.value.__traceback__
                )
            ),
        )
    )
    for sensitive in (API_KEY, PROMPT, MULTIMODAL_TEXT, IMAGE_URL, RAW_ERROR):
        assert sensitive not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_provider_failure_traceback_locals_do_not_retain_sensitive_values() -> None:
    class ProviderFailure(RuntimeError):
        request_id = "req-" + API_KEY

    transport, _, _, _ = mock_transport(
        error=ProviderFailure(RAW_ERROR + API_KEY + PROMPT + MULTIMODAL_TEXT + IMAGE_URL)
    )

    with pytest.raises(ModelTransportError) as caught:
        await transport.complete(deployment(), sensitive_request(), API_KEY)

    rendered = captured_traceback(caught.value)
    for sensitive in (API_KEY, PROMPT, MULTIMODAL_TEXT, IMAGE_URL, RAW_ERROR):
        assert sensitive not in rendered


async def test_parse_failure_traceback_locals_do_not_retain_raw_response() -> None:
    tool = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(
            name="lookup",
            arguments=RAW_ERROR + API_KEY + PROMPT + " {not-json",
        ),
    )
    poisoned = sdk_response(tool_calls=[tool])
    poisoned.model = PROMPT  # type: ignore[attr-defined]
    transport, _, _, _ = mock_transport(result=poisoned)
    del tool, poisoned

    with pytest.raises(ModelResponseError) as caught:
        await transport.complete(deployment(), sensitive_request(), API_KEY)

    rendered = captured_traceback(caught.value)
    for sensitive in (API_KEY, PROMPT, MULTIMODAL_TEXT, IMAGE_URL, RAW_ERROR):
        assert sensitive not in rendered


def test_content_bearing_contract_reprs_are_safe() -> None:
    message = ModelMessage(
        role="user",
        content=[
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": IMAGE_URL}},
        ],  # type: ignore[arg-type]
    )
    schema = StructuredResponseSchema(
        name="safe_contract",
        schema={"description": PROMPT},
    )
    model_request = ModelRequest(
        logical_model="primary",
        messages=(message,),
        required_capabilities={ModelCapability.STRUCTURED_OUTPUT},  # type: ignore[arg-type]
        response_schema=schema,
    )
    tool_call = ToolCall(id="call_safe", name="lookup", arguments={"secret": PROMPT})
    response = ModelResponse(
        text=PROMPT,
        tool_calls=(tool_call,),
        provider_metadata={"model": PROMPT},
    )

    rendered = " ".join(map(repr, (message, schema, model_request, tool_call, response)))
    assert PROMPT not in rendered
    assert IMAGE_URL not in rendered


@pytest.mark.parametrize(
    "response",
    [
        sdk_response(choices=[]),
        SimpleNamespace(choices=[SimpleNamespace(message=None)]),
    ],
)
async def test_rejects_empty_or_malformed_responses_safely(response: object) -> None:
    transport, _, _, close = mock_transport(result=response)

    with pytest.raises(ModelResponseError, match="deployment 'primary-1'") as caught:
        await transport.complete(deployment(), request(), API_KEY)

    close.assert_awaited_once_with()
    rendered = repr(caught.value)
    assert API_KEY not in rendered
    assert PROMPT not in rendered


async def test_rejects_malformed_or_nonobject_tool_arguments_without_leaking_them() -> None:
    bad_arguments = RAW_ERROR + API_KEY + PROMPT + " {not-json"
    tool = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="lookup", arguments=bad_arguments),
    )
    transport, _, _, _ = mock_transport(result=sdk_response(tool_calls=[tool]))

    with pytest.raises(ModelResponseError, match="malformed tool call") as caught:
        await transport.complete(deployment(), request(), API_KEY)

    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    for sensitive in (bad_arguments, API_KEY, PROMPT, RAW_ERROR):
        assert sensitive not in rendered
        assert sensitive not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
async def test_rejects_nonfinite_tool_argument_json_constants(constant: str) -> None:
    tool = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="lookup", arguments=f'{{"value":{constant}}}'),
    )
    transport, _, _, _ = mock_transport(result=sdk_response(tool_calls=[tool]))

    with pytest.raises(ModelResponseError, match="malformed tool call") as caught:
        await transport.complete(deployment(), request(), API_KEY)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_closes_on_provider_error_and_redacts_runtime_secrets() -> None:
    class ProviderFailure(RuntimeError):
        status_code = 429
        request_id = "req_safe429"

    transport, _, _, close = mock_transport(error=ProviderFailure(RAW_ERROR + API_KEY + PROMPT))

    with pytest.raises(ModelTransportError) as caught:
        await transport.complete(deployment(), request(), API_KEY)

    close.assert_awaited_once_with()
    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert "status=429" in rendered
    assert "request_id=req_safe429" in rendered
    assert API_KEY not in rendered
    assert PROMPT not in rendered
    assert RAW_ERROR not in rendered


async def test_propagates_cancellation_and_still_closes() -> None:
    transport, _, _, close = mock_transport(error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await transport.complete(deployment(), request(), API_KEY)

    close.assert_awaited_once_with()


async def test_successful_call_close_failure_maps_to_safe_transport_error() -> None:
    class CloseFailure(RuntimeError):
        status_code = 503
        request_id = "req-close-safe"

    transport, _, _, close = mock_transport(
        close_error=CloseFailure(RAW_ERROR + API_KEY + PROMPT)
    )

    with pytest.raises(ModelTransportError) as caught:
        await transport.complete(deployment(), sensitive_request(), API_KEY)

    close.assert_awaited_once_with()
    assert "status=503" in str(caught.value)
    assert "request_id=req-close-safe" in str(caught.value)
    assert caught.value.__context__ is None
    rendered = captured_traceback(caught.value)
    for sensitive in (RAW_ERROR, API_KEY, PROMPT, MULTIMODAL_TEXT, IMAGE_URL):
        assert sensitive not in rendered


async def test_provider_error_close_failure_keeps_primary_safe_error() -> None:
    class ProviderFailure(RuntimeError):
        status_code = 429
        request_id = "req-primary-safe"

    transport, _, _, close = mock_transport(
        error=ProviderFailure(RAW_ERROR + API_KEY),
        close_error=RuntimeError("close-" + RAW_ERROR + PROMPT),
    )

    with pytest.raises(ModelTransportError) as caught:
        await transport.complete(deployment(), sensitive_request(), API_KEY)

    close.assert_awaited_once_with()
    assert "status=429" in str(caught.value)
    assert "request_id=req-primary-safe" in str(caught.value)
    assert "close-" not in str(caught.value)


async def test_parse_error_close_failure_keeps_parse_error() -> None:
    transport, _, _, close = mock_transport(
        result=sdk_response(choices=[]),
        close_error=RuntimeError("close-" + RAW_ERROR + API_KEY),
    )

    with pytest.raises(ModelResponseError, match="malformed model response") as caught:
        await transport.complete(deployment(), sensitive_request(), API_KEY)

    close.assert_awaited_once_with()
    assert caught.value.__context__ is None
    assert RAW_ERROR not in captured_traceback(caught.value)
    assert API_KEY not in captured_traceback(caught.value)


async def test_cancellation_close_failure_keeps_cancellation() -> None:
    transport, _, _, close = mock_transport(
        error=asyncio.CancelledError(),
        close_error=RuntimeError("close-" + RAW_ERROR + API_KEY + PROMPT),
    )

    with pytest.raises(asyncio.CancelledError) as caught:
        await transport.complete(deployment(), sensitive_request(), API_KEY)

    close.assert_awaited_once_with()
    rendered = captured_traceback(caught.value)
    for sensitive in (RAW_ERROR, API_KEY, PROMPT, MULTIMODAL_TEXT, IMAGE_URL):
        assert sensitive not in rendered


@pytest.mark.parametrize("failure_kind", ["provider", "parse"])
async def test_external_task_cancellation_during_error_cleanup_is_preserved(
    failure_kind: str,
) -> None:
    close_started = asyncio.Event()
    close_blocker = asyncio.Event()

    async def blocking_close() -> None:
        close_started.set()
        await close_blocker.wait()

    if failure_kind == "provider":
        transport, _, _, close = mock_transport(
            error=RuntimeError(RAW_ERROR + API_KEY + PROMPT)
        )
    else:
        transport, _, _, close = mock_transport(result=malformed_sensitive_response())
    close.side_effect = blocking_close

    task = asyncio.create_task(transport.complete(deployment(), sensitive_request(), API_KEY))
    await close_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert task.cancelled()
    close.assert_awaited_once_with()
    rendered = captured_traceback(caught.value)
    for sensitive in (RAW_ERROR, API_KEY, PROMPT, MULTIMODAL_TEXT, IMAGE_URL):
        assert sensitive not in rendered


async def test_rejects_blank_runtime_key_before_constructing_sdk_client() -> None:
    transport, factory, _, _ = mock_transport()

    with pytest.raises(ValueError, match="API key must not be blank"):
        await transport.complete(deployment(), request(), "   ")

    factory.assert_not_called()
    assert API_KEY not in repr(transport)
