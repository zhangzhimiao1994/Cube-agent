"""Bounded native Responses encoding and exact structured-result validation."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, Protocol, cast

from jsonschema import Draft202012Validator, SchemaError  # type: ignore[import-untyped]

from agent_hub.models.types import (
    Deployment,
    ModelRequest,
    ModelResponse,
    RejectedOutputEvidence,
    RejectedOutputReason,
    RejectedOutputStatus,
    RejectedUsageStatus,
    TokenUsage,
    ToolCall,
)

_MAX_BYTES = 262_144
_MAX_DEPTH = 64
_MAX_NODES = 16_384


class ResponsesContractError(ValueError):
    """Safe diagnostic without provider text, schema contents, or credentials."""

    def __init__(self, message: str, *, evidence: RejectedOutputEvidence | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence


class _RejectOutput(Protocol):
    def __call__(
        self, reason: RejectedOutputReason, *, status: RejectedOutputStatus = "unknown",
        final_text: str | None = None,
    ) -> ResponsesContractError: ...


def _get(value: object, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _plain_json(value: object) -> Any:
    remaining = _MAX_NODES

    def walk(item: object, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > _MAX_DEPTH:
            raise ResponsesContractError("Responses JSON exceeds structural limit")
        if isinstance(item, Mapping):
            if not all(type(key) is str for key in item):
                raise ResponsesContractError("Responses JSON has invalid keys")
            return {key: walk(child, depth + 1) for key, child in item.items()}
        if isinstance(item, tuple | list):
            return [walk(child, depth + 1) for child in item]
        if item is None or type(item) in {str, bool, int}:
            return item
        if type(item) is float and math.isfinite(item):
            return item
        raise ResponsesContractError("Responses JSON has invalid values")

    result = walk(value, 0)
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > _MAX_BYTES:
        raise ResponsesContractError("Responses JSON exceeds byte limit")
    return result


def _schema(request: ModelRequest) -> dict[str, Any]:
    if request.response_schema is None:
        raise ResponsesContractError("Responses request requires a schema")
    schema = _plain_json(request.response_schema.schema)

    def check(item: object) -> None:
        if isinstance(item, dict):
            # No resolver/network I/O and no silently ignored format assertions.
            if any(key in item for key in ("$ref", "$dynamicRef", "$recursiveRef", "format")):
                raise ResponsesContractError(
                    "Responses schema uses unsupported references or format"
                )
            if (
                "$schema" in item
                and item["$schema"] != "https://json-schema.org/draft/2020-12/schema"
            ):
                raise ResponsesContractError("Responses schema dialect is unsupported")
            for key in (
                "properties",
                "patternProperties",
                "$defs",
                "definitions",
                "dependentSchemas",
            ):
                children = item.get(key)
                if isinstance(children, dict):
                    for child in children.values():
                        check(child)
            for key in (
                "additionalProperties",
                "unevaluatedProperties",
                "propertyNames",
                "items",
                "unevaluatedItems",
                "contains",
                "not",
                "if",
                "then",
                "else",
                "contentSchema",
            ):
                check(item.get(key))
            for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
                children = item.get(key)
                if isinstance(children, list):
                    for child in children:
                        check(child)

    check(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ResponsesContractError("Responses schema is invalid") from None
    return cast(dict[str, Any], schema)


def response_create_kwargs(deployment: Deployment, request: ModelRequest) -> dict[str, object]:
    schema = _schema(request)
    assert request.response_schema is not None
    if not 1 <= len(request.messages) <= 64:
        raise ResponsesContractError("Responses input message count is invalid")
    messages: list[dict[str, object]] = []
    for message in request.messages:
        if type(message.content) is not str or message.role not in {"system", "user", "assistant"}:
            raise ResponsesContractError("Responses input combination is unsupported")
        messages.append({"role": message.role, "content": message.content})
    _plain_json(messages)
    if len(request.tools) > 128 or len({tool.name for tool in request.tools}) != len(request.tools):
        raise ResponsesContractError("Responses input tools are invalid")
    payload: dict[str, object] = {
        "model": deployment.request_model or deployment.provider_model,
        "input": messages,
        "max_output_tokens": request.max_output_tokens,
        "timeout": request.timeout_seconds,
        "stream": False,
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": request.response_schema.name,
                "schema": schema,
                "strict": True,
            }
        },
    }
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": _plain_json(tool.parameters),
            }
            for tool in request.tools
        ]
        payload["tool_choice"] = "auto"
    return payload


def _loads(text: str) -> object:
    if len(text.encode()) > 65_536:
        raise ResponsesContractError("Responses output exceeds byte limit")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ResponsesContractError("Responses JSON contains duplicate keys")
            result[key] = value
        return result

    def reject_constant(_: str) -> object:
        raise ResponsesContractError("Responses JSON contains nonfinite values")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
        return _plain_json(value)
    except (ValueError, RecursionError, OverflowError):
        raise ResponsesContractError("Responses output is not bounded strict JSON") from None


def parse_response(response: object, request: ModelRequest) -> ModelResponse:
    raw_usage = _get(response, "usage")
    usage: TokenUsage | None = None
    usage_status: RejectedUsageStatus = "missing" if raw_usage is None else "invalid"
    counts = [_get(raw_usage, name) for name in ("input_tokens", "output_tokens", "total_tokens")]
    if (
        all(type(value) is int and value >= 0 for value in counts)
        and counts[0] + counts[1] == counts[2]
    ):
        usage = TokenUsage(*counts)
        usage_status = "known"

    def rejected(
        reason: RejectedOutputReason, *, status: RejectedOutputStatus = "unknown",
        final_text: str | None = None,
    ) -> ResponsesContractError:
        return ResponsesContractError(
            "Responses output rejected",
            evidence=RejectedOutputEvidence(
                final_text=final_text, usage=usage, usage_status=usage_status,
                status=status, reason=reason,
            ),
        )

    try:
        schema = _schema(request)
    except ResponsesContractError:
        raise rejected("configuration_error") from None
    try:
        return _parse_output(response, request, usage, schema, rejected)
    except ResponsesContractError as error:
        if error.evidence is not None:
            raise
        raise rejected("invalid_output") from None
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise rejected("invalid_output") from None


def _parse_output(
    response: object, request: ModelRequest, usage: TokenUsage | None,
    schema: dict[str, Any], rejected: _RejectOutput,
) -> ModelResponse:
    if (
        _get(response, "status") != "completed"
        or _get(response, "error") is not None
        or _get(response, "incomplete_details") is not None
    ):
        if _get(response, "status") == "incomplete":
            raise rejected("incomplete", status="incomplete")
        raise rejected("invalid_output")
    output = _get(response, "output")
    if not isinstance(output, list | tuple) or not 1 <= len(output) <= 128:
        raise ResponsesContractError("Responses output items are invalid")
    texts: list[str] = []
    calls: list[ToolCall] = []
    call_ids: set[str] = set()
    permitted_names = {tool.name for tool in request.tools}
    message_count = 0
    for item in output:
        if (
            _get(item, "error") is not None
            or _get(item, "incomplete_details") is not None
            or _get(item, "status") not in {None, "completed"}
        ):
            raise rejected("incomplete", status="incomplete")
        kind = _get(item, "type")
        if kind == "reasoning":
            continue
        if kind == "message":
            message_count += 1
            if message_count > 1:
                raise ResponsesContractError("Responses output has multiple final messages")
            if _get(item, "role") != "assistant" or _get(item, "status") != "completed":
                raise rejected("incomplete", status="incomplete")
            content = _get(item, "content")
            if not isinstance(content, list | tuple) or not 1 <= len(content) <= 128:
                raise ResponsesContractError("Responses message content is invalid")
            for part in content:
                text = _get(part, "text")
                if _get(part, "type") == "refusal":
                    raise rejected("refusal", status="refused")
                if _get(part, "type") != "output_text" or type(text) is not str:
                    raise ResponsesContractError("Responses output is refused or unsupported")
                texts.append(text)
        elif kind == "function_call":
            call_id, name, arguments = (_get(item, key) for key in ("call_id", "name", "arguments"))
            if (
                type(call_id) is not str
                or not call_id
                or len(call_id) > 256
                or call_id in call_ids
                or type(name) is not str
                or name not in permitted_names
                or type(arguments) is not str
                or len(arguments.encode()) > 32_768
                or _get(item, "status") not in {None, "completed"}
                or len(calls) >= 16
            ):
                raise rejected("invalid_tool")
            try:
                parsed = _loads(arguments)
                if not isinstance(parsed, dict):
                    raise rejected("invalid_tool")
                calls.append(ToolCall(id=call_id, name=name, arguments=parsed))
            except (ValueError, TypeError):
                raise rejected("invalid_tool") from None
            call_ids.add(call_id)
        else:
            raise ResponsesContractError("Responses output item type is unsupported")
    if sum(len(text.encode()) for text in texts) > 65_536:
        raise rejected("output_limit", status="completed")
    text = "".join(texts) if texts else None
    if calls and texts:
        raise ResponsesContractError("Responses output mixes final text and function calls")
    if not calls:
        if text is None:
            raise ResponsesContractError("Responses output has no final result")
        try:
            instance = _loads(text)
        except ResponsesContractError:
            raise rejected("invalid_json", status="completed", final_text=text) from None
        if not Draft202012Validator(schema).is_valid(instance):
            raise rejected("schema_mismatch", status="completed", final_text=text)
    if usage is None:
        reason: RejectedOutputReason = (
            "usage_missing" if _get(response, "usage") is None else "usage_invalid"
        )
        raise rejected(reason, status="completed", final_text=text)
    return ModelResponse(
        text=text,
        tool_calls=tuple(calls),
        usage=usage,
        provider_metadata={
            "api_protocol": "responses",
            "finish_reason": "tool_calls" if calls else "stop",
        },
    )
