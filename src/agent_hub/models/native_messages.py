"""Private, transport-native Chat messages. Never publish these as Artifacts."""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

_MAX_BYTES = 10_000_000
_MAX_CALLS = 16


def _text(value: object, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is not str:
        raise ValueError("native message text is invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError("native message text is invalid") from None
    if size > _MAX_BYTES:
        raise ValueError("native message text exceeds limit")


def _identifier(value: object) -> None:
    if (type(value) is not str or not 1 <= len(value) <= 512 or value != value.strip()
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise ValueError("native message identifier is invalid")


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("native arguments contain duplicate keys")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ValueError("native arguments contain invalid numbers")


@dataclass(frozen=True, slots=True, repr=False)
class NativeToolCall:
    id: str
    name: str
    arguments: str

    def __post_init__(self) -> None:
        _identifier(self.id)
        _identifier(self.name)
        _text(self.arguments)
        try:
            parsed = json.loads(self.arguments, object_pairs_hook=_object, parse_constant=_constant)
            if type(parsed) is not dict:
                raise ValueError
            stack: list[tuple[object, int]] = [(parsed, 0)]
            count = 0
            while stack:
                value, depth = stack.pop()
                count += 1
                if depth > 64 or count > 100_000:
                    raise ValueError
                if type(value) is float and not math.isfinite(value):
                    raise ValueError
                if isinstance(value, dict):
                    stack.extend((item, depth + 1) for item in value.values())
                elif isinstance(value, list):
                    stack.extend((item, depth + 1) for item in value)
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise ValueError("native tool arguments must be a bounded JSON object") from None

    def to_payload(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_payload(cls, value: object) -> "NativeToolCall":
        if not isinstance(value, Mapping) or set(value) != {"id", "name", "arguments"}:
            raise ValueError("native tool payload is invalid")
        return cls(value["id"], value["name"], value["arguments"])

    def to_wire(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}


@dataclass(frozen=True, slots=True, repr=False)
class NativeAssistantMessage:
    deployment_id: str
    content: str | None
    tool_calls: tuple[NativeToolCall, ...]
    reasoning_content: str | None = None

    @property
    def protocol(self) -> Literal["chat_completions"]:
        return "chat_completions"

    def __post_init__(self) -> None:
        if (type(self.deployment_id) is not str
                or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", self.deployment_id) is None):
            raise ValueError("native deployment identifier is invalid")
        _text(self.content, nullable=True)
        _text(self.reasoning_content, nullable=True)
        if (type(self.tool_calls) is not tuple or len(self.tool_calls) > _MAX_CALLS
                or not all(type(call) is NativeToolCall for call in self.tool_calls)
                or len({call.id for call in self.tool_calls}) != len(self.tool_calls)):
            raise ValueError("native assistant calls are invalid")
        if len(json.dumps(self.to_payload(), ensure_ascii=False).encode("utf-8")) > _MAX_BYTES:
            raise ValueError("native assistant exceeds limit")

    def to_payload(self) -> dict[str, Any]:
        return {"protocol": self.protocol, "deployment_id": self.deployment_id,
                "content": self.content, "tool_calls": [call.to_payload() for call in self.tool_calls],
                "reasoning_content": self.reasoning_content}

    @classmethod
    def from_payload(cls, value: object) -> "NativeAssistantMessage":
        if (not isinstance(value, Mapping) or set(value) != {
                "protocol", "deployment_id", "content", "tool_calls", "reasoning_content"}
                or value["protocol"] != "chat_completions"
                or not isinstance(value["tool_calls"], list | tuple)):
            raise ValueError("native assistant payload is invalid")
        return cls(value["deployment_id"], value["content"],
                   tuple(NativeToolCall.from_payload(call) for call in value["tool_calls"]),
                   value["reasoning_content"])

    def to_wire(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            result["tool_calls"] = [call.to_wire() for call in self.tool_calls]
        if self.reasoning_content is not None:
            result["reasoning_content"] = self.reasoning_content
        return result
