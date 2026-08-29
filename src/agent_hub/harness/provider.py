from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, cast

_SAFE_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_SAFE_TOOL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,512}$")
_SECRET_PATTERN = re.compile(
    r"(authorization|bearer|api[_ -]?key|secret|password|token|sk-[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_MAX_JSON_STRING_BYTES = 65_536
_MAX_JSON_NODES = 4_096
_MAX_JSON_DEPTH = 20
_MAX_TOOL_ARGUMENT_BYTES = 65_536

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class ProviderStreamDelta:
    content: str | None = None
    reasoning_content: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments_delta: str | None = None

    def __post_init__(self) -> None:
        for name in ("content", "reasoning_content", "tool_arguments_delta"):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not str or len(value.encode("utf-8")) > _MAX_JSON_STRING_BYTES
            ):
                raise ValueError(f"{name} must be bounded text")
        for name in ("tool_call_id", "tool_name"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or _SAFE_TOOL_ID.fullmatch(value) is None):
                raise ValueError(f"{name} must be safe text")


@dataclass(frozen=True, slots=True)
class NormalizedProviderEvent:
    kind: Literal["model.reasoning_delta", "model.text_delta", "tool.requested"]
    payload: Mapping[str, JsonValue] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class PrefixCacheEstimate:
    reusable_prefix_bytes: int
    total_current_bytes: int
    reuse_ratio: float
    useful: bool

    def __post_init__(self) -> None:
        if type(self.reusable_prefix_bytes) is not int or self.reusable_prefix_bytes < 0:
            raise ValueError("reusable_prefix_bytes must be nonnegative")
        if type(self.total_current_bytes) is not int or self.total_current_bytes < 0:
            raise ValueError("total_current_bytes must be nonnegative")
        if (
            isinstance(self.reuse_ratio, bool)
            or not isinstance(self.reuse_ratio, int | float)
            or not math.isfinite(self.reuse_ratio)
            or not 0 <= self.reuse_ratio <= 1
        ):
            raise ValueError("reuse_ratio must be between 0 and 1")
        if type(self.useful) is not bool:
            raise ValueError("useful must be a boolean")


@dataclass(frozen=True, slots=True)
class ProviderErrorEnvelope:
    provider: str
    safe_message: str
    status_code: int | None = None

    def __post_init__(self) -> None:
        _require_safe_provider(self.provider)
        if self.safe_message != "provider request failed":
            raise ValueError("safe_message must be normalized")
        if self.status_code is not None and (
            type(self.status_code) is not int or not 100 <= self.status_code <= 599
        ):
            raise ValueError("status_code must be HTTP-like")

    @classmethod
    def from_exception(
        cls,
        *,
        provider: str,
        error: BaseException,
        status_code: int | None = None,
    ) -> ProviderErrorEnvelope:
        raw = f"{error!s} {error!r}"
        if _SECRET_PATTERN.search(raw) is not None:
            safe_message = "provider request failed"
        else:
            safe_message = "provider request failed"
        error.__traceback__ = None
        error.__context__ = None
        error.__cause__ = None
        return cls(provider=provider, safe_message=safe_message, status_code=status_code)


class ProviderStreamNormalizer:
    """Normalize provider-specific streaming fragments into harness events."""

    def __init__(self, *, provider: str) -> None:
        _require_safe_provider(provider)
        self._provider = provider

    def normalize(
        self,
        deltas: Iterable[ProviderStreamDelta],
    ) -> Iterable[NormalizedProviderEvent]:
        builders: dict[str, _ToolCallBuilder] = {}
        for delta in deltas:
            if not isinstance(delta, ProviderStreamDelta):
                raise TypeError("stream deltas must be ProviderStreamDelta values")
            if delta.reasoning_content is not None:
                yield NormalizedProviderEvent(
                    kind="model.reasoning_delta",
                    payload={"text": delta.reasoning_content},
                )
            if delta.content is not None:
                yield NormalizedProviderEvent(
                    kind="model.text_delta",
                    payload={"text": delta.content},
                )
            if delta.tool_call_id is not None:
                builder = builders.setdefault(delta.tool_call_id, _ToolCallBuilder(delta.tool_call_id))
                if delta.tool_name is not None:
                    builder.name = delta.tool_name
                if delta.tool_arguments_delta is not None:
                    builder.append_arguments(delta.tool_arguments_delta)
        for tool_call_id in sorted(builders):
            yield builders[tool_call_id].event()


def openai_compatible_stream_deltas(
    chunks: Iterable[object],
) -> Iterable[ProviderStreamDelta]:
    """Convert OpenAI-compatible stream chunks into provider deltas."""

    tool_ids_by_index: dict[int, str] = {}
    for chunk in chunks:
        delta = _first_choice_delta(chunk)
        if delta is None:
            continue
        reasoning_content = _optional_text_field(delta, "reasoning_content")
        if reasoning_content is not None:
            yield ProviderStreamDelta(reasoning_content=reasoning_content)
        content = _optional_text_field(delta, "content")
        if content is not None:
            yield ProviderStreamDelta(content=content)

        raw_tool_calls = _field(delta, "tool_calls")
        if raw_tool_calls is None:
            continue
        if not isinstance(raw_tool_calls, Sequence) or isinstance(raw_tool_calls, str | bytes):
            raise TypeError("stream tool_calls must be a sequence")
        for raw_tool_call in raw_tool_calls:
            index = _tool_call_index(raw_tool_call)
            tool_call_id = _optional_text_field(raw_tool_call, "id")
            if tool_call_id is not None:
                tool_ids_by_index[index] = tool_call_id
            else:
                tool_call_id = tool_ids_by_index.get(index)
            if tool_call_id is None:
                raise ValueError("streamed tool call is missing an id")
            function = _field(raw_tool_call, "function")
            yield ProviderStreamDelta(
                tool_call_id=tool_call_id,
                tool_name=_optional_text_field(function, "name"),
                tool_arguments_delta=_optional_text_field(function, "arguments"),
            )


@dataclass(slots=True)
class _ToolCallBuilder:
    id: str
    name: str | None = None
    argument_fragments: list[str] = field(default_factory=list)

    @property
    def argument_bytes(self) -> int:
        return sum(len(fragment.encode("utf-8")) for fragment in self.argument_fragments)

    def append_arguments(self, value: str) -> None:
        if self.argument_bytes + len(value.encode("utf-8")) > _MAX_TOOL_ARGUMENT_BYTES:
            raise ValueError("streamed tool call arguments exceed size limit")
        self.argument_fragments.append(value)

    def event(self) -> NormalizedProviderEvent:
        if self.name is None:
            raise ValueError("streamed tool call is missing a name")
        raw_arguments = "".join(self.argument_fragments) or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            raise ValueError("streamed tool call arguments are invalid") from None
        if not isinstance(arguments, dict):
            raise TypeError("streamed tool call arguments must be an object")
        return NormalizedProviderEvent(
            kind="tool.requested",
            payload={
                "id": self.id,
                "name": self.name,
                "arguments": cast(Mapping[str, JsonValue], arguments),
            },
        )


def estimate_prefix_cache_reuse(previous_prompt: str, current_prompt: str) -> PrefixCacheEstimate:
    if type(previous_prompt) is not str or type(current_prompt) is not str:
        raise TypeError("prompts must be strings")
    previous_bytes = previous_prompt.encode("utf-8")
    current_bytes = current_prompt.encode("utf-8")
    common = 0
    max_common = min(len(previous_bytes), len(current_bytes))
    while common < max_common and previous_bytes[common] == current_bytes[common]:
        common += 1
    total = len(current_bytes)
    ratio = 0.0 if total == 0 else common / total
    return PrefixCacheEstimate(
        reusable_prefix_bytes=common,
        total_current_bytes=total,
        reuse_ratio=ratio,
        useful=common >= 64 and ratio >= 0.25,
    )


def _require_safe_provider(value: str) -> None:
    if type(value) is not str or _SAFE_PROVIDER.fullmatch(value) is None:
        raise ValueError("provider must be a safe identifier")


def _first_choice_delta(chunk: object) -> object | None:
    choices = _field(chunk, "choices")
    if choices is None:
        return None
    if not isinstance(choices, Sequence) or isinstance(choices, str | bytes) or not choices:
        raise TypeError("stream choices must be a nonempty sequence")
    return _field(choices[0], "delta")


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _optional_text_field(value: object, name: str) -> str | None:
    field_value = _field(value, name)
    if field_value is None:
        return None
    if type(field_value) is not str:
        raise TypeError(f"stream {name} must be text")
    return field_value


def _tool_call_index(value: object) -> int:
    raw_index = _field(value, "index")
    if type(raw_index) is not int:
        raise TypeError("stream tool call index must be an integer")
    if raw_index < 0 or raw_index > 1024:
        raise ValueError("stream tool call index is out of bounds")
    return raw_index


def _freeze_json_object(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError("payload must be a JSON object")
    nodes = 0

    def visit(item: object, depth: int) -> JsonValue:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ValueError("payload exceeds structural limits")
        if item is None or type(item) is bool or type(item) is int:
            return cast(JsonScalar, item)
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            return item
        if type(item) is str:
            if len(item.encode("utf-8")) > _MAX_JSON_STRING_BYTES:
                raise ValueError("JSON string exceeds size limit")
            return item
        if isinstance(item, Mapping):
            frozen: dict[str, JsonValue] = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise TypeError("JSON keys must be strings")
                frozen[key] = visit(child, depth + 1)
            return MappingProxyType(frozen)
        if isinstance(item, list | tuple):
            return tuple(visit(child, depth + 1) for child in item)
        raise TypeError("value is not JSON-compatible")

    frozen = visit(value, 0)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise TypeError("payload must be a JSON object")
    return frozen


__all__ = [
    "NormalizedProviderEvent",
    "PrefixCacheEstimate",
    "ProviderErrorEnvelope",
    "ProviderStreamDelta",
    "ProviderStreamNormalizer",
    "estimate_prefix_cache_reuse",
    "openai_compatible_stream_deltas",
]
