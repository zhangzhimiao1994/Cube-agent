import asyncio
import json
import logging
import re
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    AsyncOpenAI,
    AsyncStream,
)

from agent_hub.models.responses import (
    ResponsesContractError,
    parse_response,
    response_create_kwargs,
)
from agent_hub.models.types import (
    Deployment,
    JsonScalar,
    JsonValue,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    RejectedOutputEvidence,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    _freeze_json,
)

_SAFE_PROVIDER_VALUE = re.compile(r"^[A-Za-z0-9_./:-]{1,256}$")
_LEGACY_MAX_TOKENS_MARKERS = (
    "max_completion_tokens",
    "max completion tokens",
)
_UNSUPPORTED_PARAMETER_MARKERS = (
    "unknown parameter",
    "unrecognized parameter",
    "unsupported parameter",
    "extra_forbidden",
    "invalid parameter",
    "not support",
    "not supported",
)
_LOGGER = logging.getLogger(__name__)
_NETWORK_ERRORS = (
    httpx.NetworkError,
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
    TimeoutError,
    ConnectionError,
    socket.gaierror,
)


class _Completions(Protocol):
    async def create(self, **kwargs: object) -> object: ...


class _Chat(Protocol):
    completions: _Completions


class _OpenAIClient(Protocol):
    chat: _Chat
    responses: _Completions

    async def close(self) -> None: ...


class _HTTPResponse(Protocol):
    status_code: int

    def json(self) -> object: ...


class _HTTPClient(Protocol):
    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
    ) -> _HTTPResponse: ...

    async def aclose(self) -> None: ...


class OpenAIClientFactory(Protocol):
    def __call__(
        self, *, api_key: str, base_url: str, max_retries: int
    ) -> _OpenAIClient: ...


class HTTPClientFactory(Protocol):
    def __call__(self, *, timeout: float) -> _HTTPClient: ...


class ModelTransportError(RuntimeError):
    """Stable, redacted model transport failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        if status_code is not None and (
            type(status_code) is not int or not 100 <= status_code <= 599
        ):
            raise ValueError("status_code must be None or between 100 and 599")
        super().__init__(message)
        self.status_code = status_code


class ModelClientError(RuntimeError):
    """Stable, non-network failure without internal exception details."""


class ModelResponseError(ModelTransportError):
    """Stable error for an invalid provider response contract."""

    def __init__(
        self, message: str, *, status_code: int | None = None,
        evidence: RejectedOutputEvidence | None = None,
    ) -> None:
        if evidence is not None and not isinstance(evidence, RejectedOutputEvidence):
            raise TypeError("evidence must be RejectedOutputEvidence or None")
        super().__init__(
            "model response rejected" if evidence is not None else message,
            status_code=status_code,
        )
        self.evidence = evidence


class ModelResponseCancelled(asyncio.CancelledError):
    """Cancellation with a received result for accounting only, never continuation."""

    def __init__(self, *, receipt: ModelResponse | RejectedOutputEvidence) -> None:
        if not isinstance(receipt, ModelResponse | RejectedOutputEvidence):
            raise TypeError("cancellation receipt must be a received model result")
        super().__init__("model response cancelled")
        self.receipt = receipt

    def __str__(self) -> str:
        return "model response cancelled"

    def __repr__(self) -> str:
        return "ModelResponseCancelled('model response cancelled')"


def _safe_response_error(error: ModelResponseError) -> ModelResponseError:
    return ModelResponseError(
        "model response rejected", status_code=error.status_code, evidence=error.evidence,
    )


class _CancelledOutcome:
    def __init__(self, receipt: ModelResponse | RejectedOutputEvidence | None = None) -> None:
        self.receipt = receipt


def _cancelled_outcome(outcome: object) -> _CancelledOutcome:
    if isinstance(outcome, ModelResponse):
        return _CancelledOutcome(outcome)
    if isinstance(outcome, ModelResponseError):
        return _CancelledOutcome(outcome.evidence)
    if isinstance(outcome, _CancelledOutcome):
        return outcome
    return _CancelledOutcome()


async def _close_received_client(
    close: Callable[[], Awaitable[None]], timeout: float,
) -> BaseException | None:
    async def bounded_close() -> None:
        async with asyncio.timeout(min(2.0, timeout)):
            await close()

    task = asyncio.create_task(bounded_close())
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
        except Exception:  # noqa: BLE001 - return cleanup failure without SDK logging
            break
    try:
        task.result()
    except asyncio.CancelledError as error:
        return cancellation or error
    except Exception as error:  # noqa: BLE001 - return sanitized cleanup outcome to caller
        return cancellation or error
    return cancellation


def _reject_json_constant(_: str) -> object:
    raise ValueError("non-finite JSON constant")


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


def _tools(tools: Sequence[ToolDefinition]) -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": _json_mutable(cast(JsonValue, tool.parameters)),
            },
        }
        for tool in tools
    ]


def _contains_sensitive(value: str, sensitive_values: Sequence[str]) -> bool:
    return any(
        sensitive and (sensitive in value or value in sensitive)
        for sensitive in sensitive_values
    )


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


def _validate_transport_request(
    deployment: Deployment,
    request: ModelRequest,
    api_key: str,
) -> Exception | None:
    if not api_key or not api_key.strip():
        return ValueError("API key must not be blank")
    if request.response_schema is not None and (
        ModelCapability.STRUCTURED_OUTPUT not in deployment.capabilities
    ):
        return ValueError("deployment lacks structured_output capability")
    if request.tools and ModelCapability.TOOL_CALLING not in deployment.capabilities:
        return ValueError("deployment lacks tool_calling capability")
    if request.response_schema is not None and _is_messages_endpoint(deployment.api_base):
        return ValueError("messages endpoint response schemas are not supported")
    return None


def _chat_completion_create_kwargs(
    deployment: Deployment,
    request: ModelRequest,
    *,
    stream: bool,
) -> dict[str, object]:
    create_kwargs: dict[str, object] = {
        "model": deployment.request_model or deployment.provider_model,
        "messages": _messages(request),
        "max_completion_tokens": request.max_output_tokens,
        "timeout": request.timeout_seconds,
        "stream": stream,
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
    if request.tools:
        create_kwargs["tools"] = _tools(request.tools)
        create_kwargs["tool_choice"] = "auto"
    return create_kwargs


def _parse_usage(raw_usage: object, deployment_id: str) -> TokenUsage | None:
    if raw_usage is None:
        return None
    prompt_tokens = _coerce_usage_count(
        _attribute(raw_usage, "prompt_tokens"),
        field_name="usage.prompt_tokens",
        deployment_id=deployment_id,
    )
    completion_tokens = _coerce_usage_count(
        _attribute(raw_usage, "completion_tokens"),
        field_name="usage.completion_tokens",
        deployment_id=deployment_id,
    )
    total_tokens = _coerce_usage_count(
        _attribute(raw_usage, "total_tokens"),
        field_name="usage.total_tokens",
        deployment_id=deployment_id,
    )
    try:
        return TokenUsage(prompt_tokens, completion_tokens, total_tokens)
    except ValueError:
        raise ModelResponseError(
            f"malformed usage totals in model response for deployment {deployment_id!r}"
        ) from None


def _coerce_usage_count(
    value: object,
    *,
    field_name: str,
    deployment_id: str,
) -> int:
    parsed: int | None = None
    if type(value) is int:
        parsed = value
    elif (type(value) is float and value.is_integer()) or (
        type(value) is str and value.isdecimal()
    ):
        parsed = int(value)
    if parsed is None or parsed < 0:
        raise ModelResponseError(
            f"malformed {field_name} in model response for deployment {deployment_id!r}"
        )
    return parsed


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
            loaded = cast(
                object,
                json.loads(raw_arguments, parse_constant=_reject_json_constant),
            )
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


def safe_model_client_error(
    deployment_id: str,
    error: Exception,
    sensitive_values: Sequence[str],
) -> ModelTransportError | ModelClientError:
    """Classify provider failures without retaining raw exception text or context."""
    if isinstance(error, ModelResponseError):
        return _safe_response_error(error)
    if isinstance(error, APIResponseValidationError):
        return ModelResponseError("model response rejected", status_code=error.status_code)
    if (
        isinstance(error, APIConnectionError)
        and error.__cause__ is not None
        and not isinstance(error.__cause__, _NETWORK_ERRORS)
    ):
        return ModelClientError("model client internal failure")
    details: list[str] = []
    status = _attribute(error, "status_code")
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
    if not isinstance(
        error,
        _NETWORK_ERRORS + (ModelTransportError, APIConnectionError, APIStatusError, httpx.HTTPStatusError),
    ):
        return ModelClientError("model client internal failure")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        details.append(f"status={status}")
    request_id = _safe_provider_string(_attribute(error, "request_id"), sensitive_values)
    if request_id is not None:
        details.append(f"request_id={request_id}")
    suffix = f" ({', '.join(details)})" if details else ""
    safe_status = status if isinstance(status, int) and not isinstance(status, bool) else None
    if safe_status is not None and not 100 <= safe_status <= 599:
        safe_status = None
    return ModelTransportError(
        f"model transport failed for deployment {deployment_id!r}{suffix}",
        status_code=safe_status,
    )


async def _close_ignoring_failures(client: _OpenAIClient | None) -> bool:
    if client is None:
        return False
    try:
        await client.close()
    except asyncio.CancelledError:
        task = asyncio.current_task()
        return task is not None and task.cancelling() > 0
    except Exception as error:  # noqa: BLE001 - cleanup must not replace the primary safe error
        _LOGGER.warning(
            "litellm_client_close_failed error_type=%s",
            type(error).__name__,
        )
        return False
    return False


async def _async_chunks(stream: object) -> AsyncIterator[object]:
    if not hasattr(stream, "__aiter__"):
        raise TypeError("stream response is not async iterable")
    async for chunk in cast(AsyncIterator[object], stream):
        yield chunk


class _StreamResponseBody(httpx.AsyncByteStream):
    """Keep SDK iteration failures separate from its automatic response cleanup."""

    def __init__(self, body: httpx.AsyncByteStream, cleanup_timeout: Callable[[], float]) -> None:
        self._body = body
        self._cleanup_timeout = cleanup_timeout
        self.automatic_cleanup = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._body:
            yield chunk

    async def aclose(self) -> None:
        if not self.automatic_cleanup:
            await self._body.aclose()
            return
        close_error = await _close_received_client(self._body.aclose, self._cleanup_timeout())
        if isinstance(close_error, asyncio.CancelledError):
            raise asyncio.CancelledError from None


class _OpenAICompatibleChunkStream:
    def __init__(
        self,
        start: Callable[[], Awaitable[tuple[_OpenAIClient, object]]],
        *,
        deployment_id: str,
        sensitive_values: Sequence[str],
        timeout: float,
    ) -> None:
        self._start: Callable[[], Awaitable[tuple[_OpenAIClient, object]]] | None = start
        self._deployment_id = deployment_id
        self._sensitive_values = tuple(sensitive_values)
        self._timeout = timeout
        self._deadline: float | None = None
        self._client: _OpenAIClient | None = None
        self._iterator: AsyncIterator[object] | None = None
        self._sdk_stream: AsyncStream[object] | None = None
        self._response_body: _StreamResponseBody | None = None
        self._closed = False

    def __aiter__(self) -> "_OpenAICompatibleChunkStream":
        return self

    async def __anext__(self) -> object:
        if self._closed:
            raise StopAsyncIteration
        if self._deadline is None:
            self._deadline = asyncio.get_running_loop().time() + self._timeout
        exhausted = False
        try:
            if self._iterator is None:
                if self._start is None:  # pragma: no cover - defensive invariant
                    raise StopAsyncIteration
                client, stream = await self._start()
                self._start = None
                self._client = client
                if isinstance(stream, AsyncStream):
                    self._sdk_stream = cast(AsyncStream[object], stream)
                    body = stream.response.stream
                    if isinstance(body, httpx.AsyncByteStream):
                        self._response_body = _StreamResponseBody(body, self._cleanup_timeout)
                        stream.response.stream = self._response_body
                self._iterator = _async_chunks(stream).__aiter__()
            if self._response_body is not None:
                self._response_body.automatic_cleanup = True
            return await self._iterator.__anext__()
        except StopAsyncIteration:
            exhausted = True
        except asyncio.CancelledError:
            if await self._close_client_ignoring_failure():
                raise
            raise
        except Exception as error:  # noqa: BLE001 - redact every SDK/network failure
            safe_error = safe_model_client_error(
                self._deployment_id,
                error,
                self._sensitive_values,
            )
        finally:
            if self._response_body is not None:
                self._response_body.automatic_cleanup = False
        if exhausted:
            try:
                await self.aclose()
            except Exception as error:  # noqa: BLE001 - EOF cleanup preserves received chunks
                _LOGGER.warning("litellm_stream_eof_cleanup_failed error_type=%s", type(error).__name__)
            raise StopAsyncIteration
        if await self._close_client_ignoring_failure():
            raise asyncio.CancelledError
        raise safe_error from None

    async def aclose(self) -> None:
        if self._closed:
            return
        close_failure: ModelTransportError | ModelClientError | asyncio.CancelledError | None = None
        try:
            closers: list[Callable[[], Awaitable[None]]] = []
            if self._iterator is not None:
                closers.append(cast(Any, self._iterator).aclose)
            if self._sdk_stream is not None:
                closers.append(self._sdk_stream.close)
            if self._client is not None:
                closers.append(self._client.close)
            for close in closers:
                close_error = await _close_received_client(close, self._cleanup_timeout())
                if isinstance(close_error, asyncio.CancelledError):
                    close_failure = asyncio.CancelledError()
                elif isinstance(close_error, Exception) and close_failure is None:
                    close_failure = safe_model_client_error(
                        self._deployment_id, close_error, self._sensitive_values,
                    )
                del close_error
        finally:
            self._closed = True
            self._start = None
            self._client = None
            self._iterator = None
            self._sdk_stream = None
            self._response_body = None
        if close_failure is not None:
            raise close_failure from None

    def _cleanup_timeout(self) -> float:
        remaining = self._timeout if self._deadline is None else max(
            0.0, self._deadline - asyncio.get_running_loop().time(),
        )
        # Leave time to deliver buffered tool events and settle the gateway lease.
        return min(2.0, remaining / 2)

    async def _close_client_ignoring_failure(self) -> bool:
        try:
            if self._client is None:
                return False
            close_error = await _close_received_client(self._client.close, self._cleanup_timeout())
            return isinstance(close_error, asyncio.CancelledError)
        finally:
            self._closed = True
            self._start = None
            self._client = None
            self._iterator = None
            self._sdk_stream = None
            self._response_body = None


async def _aclose_ignoring_failures(client: _HTTPClient | None) -> bool:
    if client is None:
        return False
    try:
        await client.aclose()
    except asyncio.CancelledError:
        task = asyncio.current_task()
        return task is not None and task.cancelling() > 0
    except Exception as error:  # noqa: BLE001 - cleanup must not replace the primary safe error
        _LOGGER.warning(
            "http_client_close_failed error_type=%s",
            type(error).__name__,
        )
        return False
    return False


class LiteLLMClient:
    """OpenAI-compatible Chat Completions transport for LiteLLM Proxy."""

    def __init__(
        self,
        client_factory: OpenAIClientFactory | None = None,
        http_client_factory: HTTPClientFactory | None = None,
    ) -> None:
        self._client_factory = client_factory or cast(OpenAIClientFactory, AsyncOpenAI)
        self._http_client_factory = http_client_factory or cast(
            HTTPClientFactory,
            httpx.AsyncClient,
        )

    async def complete(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> ModelResponse:
        try:
            outcome = await self._complete_outcome(deployment, request, api_key)
        except Exception as error:  # noqa: BLE001 - include client construction and retry setup
            outcome = safe_model_client_error(deployment.id, error, _sensitive_values(request, api_key))
        del deployment, request, api_key
        if isinstance(outcome, _CancelledOutcome):
            if outcome.receipt is not None:
                raise ModelResponseCancelled(receipt=outcome.receipt)
            raise asyncio.CancelledError
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def stream_openai_compatible_chunks(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> AsyncIterator[object]:
        validation_error = _validate_transport_request(deployment, request, api_key)
        if validation_error is not None:
            raise validation_error
        if _is_messages_endpoint(deployment.api_base):
            raise ValueError("messages endpoint streaming is not supported")
        if request.response_schema is not None and deployment.structured_output_api == "responses":
            raise ValueError("structured Responses streaming is not supported")
        sensitive_values = _sensitive_values(request, api_key)
        return _OpenAICompatibleChunkStream(
            lambda: self._start_stream(deployment, request, api_key, sensitive_values),
            deployment_id=deployment.id,
            sensitive_values=sensitive_values,
            timeout=request.timeout_seconds,
        )

    async def _complete_outcome(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> ModelResponse | Exception | _CancelledOutcome:
        validation_error = _validate_transport_request(deployment, request, api_key)
        if validation_error is not None:
            return validation_error
        if _is_messages_endpoint(deployment.api_base):
            return await self._messages_outcome(deployment, request, api_key)
        if request.response_schema is not None and deployment.structured_output_api == "responses":
            return await self._responses_outcome(deployment, request, api_key)

        sensitive_values = _sensitive_values(request, api_key)
        client: _OpenAIClient | None = None
        parsed: ModelResponse | None = None
        create_kwargs: dict[str, object] | None = None
        response: object | None = None
        safe_failure: ModelTransportError | ModelClientError | None = None
        try:
            client = self._client_factory(
                api_key=api_key,
                base_url=deployment.api_base,
                max_retries=0,
            )
            create_kwargs = _chat_completion_create_kwargs(
                deployment,
                request,
                stream=False,
            )
            response = await client.chat.completions.create(**create_kwargs)
            parsed = _parse_response(response, deployment.id, sensitive_values)
        except asyncio.CancelledError:
            await _close_ignoring_failures(client)
            return _CANCELLED
        except ModelResponseError as error:
            safe_failure = _safe_response_error(error)
        except Exception as error:  # noqa: BLE001 - redact every SDK/network failure
            if (
                client is not None
                and create_kwargs is not None
                and _should_retry_with_legacy_max_tokens(error)
            ):
                legacy_kwargs = dict(create_kwargs)
                legacy_kwargs["max_tokens"] = legacy_kwargs.pop("max_completion_tokens")
                try:
                    response = await client.chat.completions.create(**legacy_kwargs)
                    parsed = _parse_response(response, deployment.id, sensitive_values)
                except asyncio.CancelledError:
                    await _close_ignoring_failures(client)
                    return _CANCELLED
                except ModelResponseError as retry_error:
                    safe_failure = _safe_response_error(retry_error)
                except Exception as retry_error:  # noqa: BLE001 - redact retry failure
                    safe_failure = safe_model_client_error(
                        deployment.id,
                        retry_error,
                        sensitive_values,
                    )
            elif (
                client is not None
                and create_kwargs is not None
                and _should_retry_root_base_with_v1(error, deployment.api_base)
            ):
                if await _close_ignoring_failures(client):
                    return _CANCELLED
                client = self._client_factory(
                    api_key=api_key,
                    base_url=cast(str, _api_base_with_v1(deployment.api_base)),
                    max_retries=0,
                )
                try:
                    response = await client.chat.completions.create(**create_kwargs)
                    parsed = _parse_response(response, deployment.id, sensitive_values)
                except asyncio.CancelledError:
                    await _close_ignoring_failures(client)
                    return _CANCELLED
                except ModelResponseError as retry_error:
                    safe_failure = _safe_response_error(retry_error)
                except Exception as retry_error:  # noqa: BLE001 - redact retry failure
                    safe_failure = safe_model_client_error(
                        deployment.id,
                        retry_error,
                        sensitive_values,
                    )
            else:
                safe_failure = safe_model_client_error(deployment.id, error, sensitive_values)

        if safe_failure is not None:
            if client is not None:
                close_error = await _close_received_client(client.close, request.timeout_seconds)
                if isinstance(close_error, asyncio.CancelledError):
                    return _cancelled_outcome(safe_failure)
            return safe_failure
        if client is None or parsed is None:  # pragma: no cover - defensive invariant
            return ModelClientError("model client internal failure")

        close_error = await _close_received_client(client.close, request.timeout_seconds)
        if isinstance(close_error, asyncio.CancelledError):
            return _cancelled_outcome(parsed)
        # Cleanup failure cannot discard an already received response or its usage.
        return parsed

    async def _responses_outcome(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> ModelResponse | Exception | _CancelledOutcome:
        try:
            kwargs = response_create_kwargs(deployment, request)
        except ResponsesContractError:
            return ModelResponseError("model response contract configuration rejected")
        sensitive_values = _sensitive_values(request, api_key)
        client: _OpenAIClient | None = None
        outcome: ModelResponse | Exception | _CancelledOutcome
        try:
            async with asyncio.timeout(request.timeout_seconds):
                client = self._client_factory(
                    api_key=api_key,
                    base_url=deployment.api_base,
                    max_retries=0,
                )
                raw = await client.responses.create(**kwargs)
                outcome = parse_response(raw, request)
        except asyncio.CancelledError:
            outcome = _CANCELLED
        except ResponsesContractError as error:
            outcome = ModelResponseError("model response rejected", evidence=error.evidence)
        except Exception as error:  # noqa: BLE001 - preserve the safe transport boundary
            outcome = safe_model_client_error(deployment.id, error, sensitive_values)
        if client is not None:
            close_error = await _close_received_client(client.close, request.timeout_seconds)
            if isinstance(close_error, asyncio.CancelledError):
                outcome = _cancelled_outcome(outcome)
        return outcome

    async def _start_stream(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
        sensitive_values: Sequence[str],
    ) -> tuple[_OpenAIClient, object]:
        client = self._client_factory(
            api_key=api_key,
            base_url=deployment.api_base,
            max_retries=0,
        )
        create_kwargs = _chat_completion_create_kwargs(deployment, request, stream=True)
        try:
            stream = await client.chat.completions.create(**create_kwargs)
            return client, stream
        except asyncio.CancelledError:
            await _close_ignoring_failures(client)
            raise
        except Exception as error:  # noqa: BLE001 - startup failures need compatibility retries
            if _should_retry_with_legacy_max_tokens(error):
                legacy_kwargs = dict(create_kwargs)
                legacy_kwargs["max_tokens"] = legacy_kwargs.pop("max_completion_tokens")
                try:
                    stream = await client.chat.completions.create(**legacy_kwargs)
                    return client, stream
                except asyncio.CancelledError:
                    await _close_ignoring_failures(client)
                    raise
                except Exception as retry_error:  # noqa: BLE001 - redact retry failure
                    if await _close_ignoring_failures(client):
                        raise asyncio.CancelledError
                    raise safe_model_client_error(
                        deployment.id,
                        retry_error,
                        sensitive_values,
                    ) from None
            if _should_retry_root_base_with_v1(error, deployment.api_base):
                if await _close_ignoring_failures(client):
                    raise asyncio.CancelledError
                client = self._client_factory(
                    api_key=api_key,
                    base_url=cast(str, _api_base_with_v1(deployment.api_base)),
                    max_retries=0,
                )
                try:
                    stream = await client.chat.completions.create(
                        **create_kwargs
                    )
                    return client, stream
                except asyncio.CancelledError:
                    await _close_ignoring_failures(client)
                    raise
                except Exception as retry_error:  # noqa: BLE001 - redact retry failure
                    if await _close_ignoring_failures(client):
                        raise asyncio.CancelledError
                    raise safe_model_client_error(
                        deployment.id,
                        retry_error,
                        sensitive_values,
                    ) from None
            if await _close_ignoring_failures(client):
                raise asyncio.CancelledError
            raise safe_model_client_error(deployment.id, error, sensitive_values) from None

    async def _messages_outcome(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> ModelResponse | Exception | _CancelledOutcome:
        sensitive_values = _sensitive_values(request, api_key)
        client = self._http_client_factory(timeout=request.timeout_seconds)
        outcome: ModelResponse | Exception | _CancelledOutcome
        try:
            response = await client.post(
                deployment.api_base,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=_messages_endpoint_payload(deployment, request),
            )
            if response.status_code >= 400:
                outcome = ModelTransportError(
                    f"messages endpoint failed for deployment {deployment.id!r}",
                    status_code=response.status_code,
                )
            else:
                try:
                    payload = response.json()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise ModelResponseError("model response rejected") from None
                outcome = _parse_messages_endpoint_response(
                    payload,
                    deployment.id,
                    sensitive_values,
                )
        except asyncio.CancelledError:
            outcome = _CANCELLED
        except ModelResponseError as error:
            outcome = _safe_response_error(error)
        except Exception as error:  # noqa: BLE001 - redact direct HTTP/provider failures
            outcome = safe_model_client_error(deployment.id, error, sensitive_values)
        close_error = await _close_received_client(client.aclose, request.timeout_seconds)
        if isinstance(close_error, asyncio.CancelledError):
            return _cancelled_outcome(outcome)
        return outcome


_CANCELLED = _CancelledOutcome()


def _is_messages_endpoint(api_base: str) -> bool:
    return urlsplit(api_base).path.rstrip("/").endswith("/messages")


def _messages_endpoint_payload(
    deployment: Deployment,
    request: ModelRequest,
) -> Mapping[str, object]:
    messages: list[dict[str, object]] = []
    system_messages: list[str] = []
    for message in request.messages:
        content = message.content
        if message.role == "system" and isinstance(content, str):
            system_messages.append(content)
            continue
        role = "assistant" if message.role == "assistant" else "user"
        messages.append({"role": role, "content": _messages_endpoint_content(content)})
    payload: dict[str, object] = {
        "model": deployment.request_model or deployment.provider_model,
        "max_tokens": request.max_output_tokens,
        "messages": messages,
    }
    if system_messages:
        payload["system"] = "\n\n".join(system_messages)
    if request.tools:
        payload["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": _json_mutable(cast(JsonValue, tool.parameters)),
            }
            for tool in request.tools
        ]
    return payload


def _messages_endpoint_content(content: object) -> object:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, str | bytes):
        parts: list[dict[str, object]] = []
        for part in content:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append({"type": "text", "text": part["text"]})
        if parts:
            return parts
    return ""


def _parse_messages_endpoint_response(
    payload: object,
    deployment_id: str,
    sensitive_values: Sequence[str],
) -> ModelResponse:
    if not isinstance(payload, Mapping):
        raise ModelResponseError(f"malformed messages response for deployment {deployment_id!r}")
    content = payload.get("content")
    text = _messages_endpoint_text(content)
    tool_calls = _messages_endpoint_tool_calls(content, deployment_id)
    if text is None and not tool_calls:
        raise ModelResponseError(f"malformed messages response for deployment {deployment_id!r}")
    usage = _messages_endpoint_usage(payload.get("usage"))
    metadata: dict[str, JsonScalar] = {}
    request_id = _safe_provider_string(payload.get("id"), sensitive_values)
    if request_id is not None:
        metadata["request_id"] = request_id
    model = _safe_provider_string(payload.get("model"), sensitive_values)
    if model is not None:
        metadata["model"] = model
    return ModelResponse(
        text=text,
        tool_calls=tool_calls,
        usage=usage,
        provider_metadata=metadata,
    )


def _messages_endpoint_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, Sequence) or isinstance(content, str | bytes):
        return None
    texts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(cast(str, block["text"]))
    return "\n".join(texts) if texts else None


def _messages_endpoint_tool_calls(
    content: object,
    deployment_id: str,
) -> tuple[ToolCall, ...]:
    if not isinstance(content, Sequence) or isinstance(content, str | bytes):
        return ()
    calls: list[ToolCall] = []
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "tool_use":
            continue
        call_id = block.get("id")
        name = block.get("name")
        arguments = block.get("input")
        if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(
            arguments, Mapping
        ):
            raise ModelResponseError(
                f"malformed messages tool call for deployment {deployment_id!r}"
            )
        try:
            calls.append(
                ToolCall(
                    id=call_id,
                    name=name,
                    arguments=cast(Mapping[str, JsonValue], arguments),
                )
            )
        except (TypeError, ValueError):
            raise ModelResponseError(
                f"malformed messages tool call for deployment {deployment_id!r}"
            ) from None
    return tuple(calls)


def _messages_endpoint_usage(usage: object) -> TokenUsage | None:
    if not isinstance(usage, Mapping):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if type(input_tokens) is not int or type(output_tokens) is not int:
        return None
    try:
        return TokenUsage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )
    except (TypeError, ValueError):
        raise ModelResponseError("model response rejected") from None


def _should_retry_with_legacy_max_tokens(error: Exception) -> bool:
    if isinstance(error, ModelResponseError) or not isinstance(
        error, (ModelTransportError, APIStatusError, httpx.HTTPStatusError)
    ):
        return False
    message = str(error).lower()
    return any(marker in message for marker in _LEGACY_MAX_TOKENS_MARKERS) and any(
        marker in message for marker in _UNSUPPORTED_PARAMETER_MARKERS
    )


def _should_retry_root_base_with_v1(error: Exception, api_base: str) -> bool:
    if isinstance(error, ModelResponseError) or not isinstance(
        error, (ModelTransportError, APIStatusError, httpx.HTTPStatusError)
    ):
        return False
    return _safe_status_code(error) in {404, 405} and _api_base_with_v1(api_base) is not None


def _safe_status_code(error: Exception) -> int | None:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        return status_code
    return None


def _api_base_with_v1(api_base: str) -> str | None:
    parsed = urlsplit(api_base)
    if parsed.path not in {"", "/"}:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, "/v1", "", ""))
