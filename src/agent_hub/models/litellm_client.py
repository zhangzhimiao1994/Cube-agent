import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from openai import AsyncOpenAI

from agent_hub.models.types import (
    Deployment,
    JsonScalar,
    JsonValue,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
    _freeze_json,
)

_SAFE_PROVIDER_VALUE = re.compile(r"^[A-Za-z0-9_./:-]{1,256}$")


class _Completions(Protocol):
    async def create(self, **kwargs: object) -> object: ...


class _Chat(Protocol):
    completions: _Completions


class _OpenAIClient(Protocol):
    chat: _Chat

    async def close(self) -> None: ...


class OpenAIClientFactory(Protocol):
    def __call__(
        self, *, api_key: str, base_url: str, max_retries: int
    ) -> _OpenAIClient: ...


class ModelTransportError(RuntimeError):
    """Stable, redacted model transport failure."""


class ModelResponseError(ModelTransportError):
    """Stable error for an invalid provider response contract."""


def _attribute(value: object, name: str) -> object:
    return cast(object, getattr(value, name, None))


def _json_mutable(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _json_mutable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_mutable(item) for item in value]
    return value


def _messages(request: ModelRequest) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for message in request.messages:
        content: object
        if isinstance(message.content, str):
            content = message.content
        else:
            content = [
                {key: _json_mutable(cast(JsonValue, item)) for key, item in part.items()}
                for part in message.content
            ]
        normalized.append({"role": message.role, "content": content})
    return normalized


def _contains_sensitive(value: str, sensitive_values: Sequence[str]) -> bool:
    return any(sensitive and sensitive in value for sensitive in sensitive_values)


def _safe_provider_string(
    value: object,
    sensitive_values: Sequence[str] = (),
) -> str | None:
    if (
        isinstance(value, str)
        and _SAFE_PROVIDER_VALUE.fullmatch(value) is not None
        and not _contains_sensitive(value, sensitive_values)
    ):
        return value
    return None


def _sensitive_values(request: ModelRequest, api_key: str) -> tuple[str, ...]:
    values = [api_key]
    for message in request.messages:
        if isinstance(message.content, str):
            values.append(message.content)
            continue
        for part in message.content:
            text = part.get("text")
            if isinstance(text, str):
                values.append(text)
            image = part.get("image_url")
            if isinstance(image, Mapping):
                url = image.get("url")
                if isinstance(url, str):
                    values.append(url)
    return tuple(dict.fromkeys(value for value in values if value))


def _parse_usage(raw_usage: object, deployment_id: str) -> TokenUsage | None:
    if raw_usage is None:
        return None
    values = (
        _attribute(raw_usage, "prompt_tokens"),
        _attribute(raw_usage, "completion_tokens"),
        _attribute(raw_usage, "total_tokens"),
    )
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in values):
        raise ModelResponseError(f"malformed model response for deployment {deployment_id!r}")
    prompt_tokens, completion_tokens, total_tokens = cast(tuple[int, int, int], values)
    try:
        return TokenUsage(prompt_tokens, completion_tokens, total_tokens)
    except ValueError:
        raise ModelResponseError(
            f"malformed model response for deployment {deployment_id!r}"
        ) from None


def _parse_tool_calls(raw_calls: object, deployment_id: str) -> tuple[ToolCall, ...]:
    if raw_calls is None:
        return ()
    if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, str | bytes):
        raise ModelResponseError(
            f"malformed tool call in response for deployment {deployment_id!r}"
        )

    parsed: list[ToolCall] = []
    for raw_call in raw_calls:
        identifier = _attribute(raw_call, "id")
        function = _attribute(raw_call, "function")
        name = _attribute(function, "name")
        raw_arguments = _attribute(function, "arguments")
        if not isinstance(identifier, str) or not isinstance(name, str) or not isinstance(
            raw_arguments, str
        ):
            raise ModelResponseError(
                f"malformed tool call in response for deployment {deployment_id!r}"
            )
        parsed_call: ToolCall | None = None
        try:
            loaded = cast(object, json.loads(raw_arguments))
            if isinstance(loaded, Mapping):
                frozen = _freeze_json(loaded)
                if isinstance(frozen, Mapping):
                    parsed_call = ToolCall(id=identifier, name=name, arguments=frozen)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        if parsed_call is None:
            raise ModelResponseError(
                f"malformed tool call in response for deployment {deployment_id!r}"
            )
        parsed.append(parsed_call)
    return tuple(parsed)


def _metadata(
    response: object,
    choice: object,
    sensitive_values: Sequence[str],
) -> Mapping[str, JsonScalar]:
    metadata: dict[str, JsonScalar] = {}
    raw_request_id = _attribute(response, "_request_id")
    if raw_request_id is None:
        raw_request_id = _attribute(response, "id")
    request_id = _safe_provider_string(raw_request_id, sensitive_values)
    if request_id is not None:
        metadata["request_id"] = request_id
    for output_name, attribute_name in (
        ("model", "model"),
        ("system_fingerprint", "system_fingerprint"),
    ):
        safe_value = _safe_provider_string(_attribute(response, attribute_name), sensitive_values)
        if safe_value is not None:
            metadata[output_name] = safe_value
    created = _attribute(response, "created")
    if isinstance(created, int) and not isinstance(created, bool) and created >= 0:
        metadata["created"] = created
    finish_reason = _safe_provider_string(_attribute(choice, "finish_reason"), sensitive_values)
    if finish_reason is not None:
        metadata["finish_reason"] = finish_reason
    return metadata


def _parse_response(
    response: object,
    deployment_id: str,
    sensitive_values: Sequence[str],
) -> ModelResponse:
    choices = _attribute(response, "choices")
    if not isinstance(choices, Sequence) or isinstance(choices, str | bytes) or not choices:
        raise ModelResponseError(f"malformed model response for deployment {deployment_id!r}")
    choice = choices[0]
    message = _attribute(choice, "message")
    if message is None:
        raise ModelResponseError(f"malformed model response for deployment {deployment_id!r}")
    content = _attribute(message, "content")
    if content is not None and not isinstance(content, str):
        raise ModelResponseError(f"malformed model response for deployment {deployment_id!r}")
    return ModelResponse(
        text=content,
        tool_calls=_parse_tool_calls(_attribute(message, "tool_calls"), deployment_id),
        usage=_parse_usage(_attribute(response, "usage"), deployment_id),
        provider_metadata=_metadata(response, choice, sensitive_values),
    )


def _transport_error(
    deployment_id: str,
    error: Exception,
    sensitive_values: Sequence[str],
) -> ModelTransportError:
    details: list[str] = []
    status = _attribute(error, "status_code")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        details.append(f"status={status}")
    request_id = _safe_provider_string(_attribute(error, "request_id"), sensitive_values)
    if request_id is not None:
        details.append(f"request_id={request_id}")
    suffix = f" ({', '.join(details)})" if details else ""
    return ModelTransportError(f"model transport failed for deployment {deployment_id!r}{suffix}")


async def _close_after_failure(client: _OpenAIClient | None) -> None:
    if client is None:
        return
    try:
        await client.close()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - cleanup must not replace the primary safe error
        return


class LiteLLMClient:
    """OpenAI-compatible Chat Completions transport for LiteLLM Proxy."""

    def __init__(self, client_factory: OpenAIClientFactory | None = None) -> None:
        self._client_factory = client_factory or cast(OpenAIClientFactory, AsyncOpenAI)

    async def complete(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> ModelResponse:
        if not api_key or not api_key.strip():
            raise ValueError("API key must not be blank")
        if request.response_schema is not None and (
            ModelCapability.STRUCTURED_OUTPUT not in deployment.capabilities
        ):
            raise ValueError("deployment lacks structured_output capability")

        sensitive_values = _sensitive_values(request, api_key)
        client: _OpenAIClient | None = None
        parsed: ModelResponse | None = None
        mapped_error: ModelTransportError | None = None
        try:
            client = self._client_factory(
                api_key=api_key,
                base_url=deployment.api_base,
                max_retries=0,
            )
            create_kwargs: dict[str, object] = {
                "model": deployment.provider_model,
                "messages": _messages(request),
                "timeout": request.timeout_seconds,
                "stream": False,
            }
            if request.response_schema is not None:
                create_kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.response_schema.name,
                        "schema": _json_mutable(cast(JsonValue, request.response_schema.schema)),
                        "strict": True,
                    },
                }
            response = await client.chat.completions.create(**create_kwargs)
            parsed = _parse_response(response, deployment.id, sensitive_values)
        except asyncio.CancelledError:
            await _close_after_failure(client)
            raise
        except ModelTransportError:
            await _close_after_failure(client)
            raise
        except Exception as error:  # noqa: BLE001 - redact every SDK/network failure
            await _close_after_failure(client)
            mapped_error = _transport_error(deployment.id, error, sensitive_values)

        if mapped_error is not None:
            raise mapped_error
        if client is None or parsed is None:  # pragma: no cover - defensive invariant
            raise AssertionError("transport completed without a client response")

        close_error: ModelTransportError | None = None
        try:
            await client.close()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - close failures are provider failures
            close_error = _transport_error(deployment.id, error, sensitive_values)
        if close_error is not None:
            raise close_error
        return parsed
