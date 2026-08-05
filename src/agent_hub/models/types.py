import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import cast
from urllib.parse import urlsplit

_SAFE_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SAFE_SCHEMA_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_IDENTIFIER_LENGTH = 128
_PROVIDER_METADATA_KEYS = frozenset({
    "request_id",
    "model",
    "created",
    "system_fingerprint",
    "finish_reason",
})
_SAFE_PROVIDER_METADATA_STRING = re.compile(r"^[A-Za-z0-9_./:-]{1,256}$")

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | tuple[JsonValue, ...] | Mapping[str, JsonValue]
type ContentPart = Mapping[str, object]
type MessageContent = str | tuple[ContentPart, ...]


class ModelCapability(StrEnum):
    TEXT = "text"
    VISION = "vision"
    TOOL_CALLING = "tool_calling"
    STRUCTURED_OUTPUT = "structured_output"


def _require_safe_identifier(name: str, value: str) -> None:
    if len(value) > _MAX_IDENTIFIER_LENGTH or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe identifier")


def _require_unpadded(name: str, value: str, *, max_length: int) -> None:
    if not value or value != value.strip() or len(value) > max_length:
        raise ValueError(f"{name} must be nonblank, unpadded, and at most {max_length} characters")


def _is_int(value: object) -> bool:
    return type(value) is int


def _freeze_json(value: object) -> JsonValue:
    if value is None:
        return None
    if type(value) is str:
        return value
    if type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError("value is not JSON-compatible")


def _normalize_content_part(value: Mapping[str, object]) -> ContentPart:
    part_type = value.get("type")
    if part_type == "text":
        if set(value) != {"type", "text"} or not isinstance(value.get("text"), str):
            raise ValueError("text content parts require only type and text strings")
    elif part_type == "image_url":
        if set(value) != {"type", "image_url"}:
            raise ValueError("image_url content parts require only type and image_url")
        image_url = value.get("image_url")
        if not isinstance(image_url, Mapping):
            raise ValueError("image_url must be an object")
        if not set(image_url).issubset({"url", "detail"}) or "url" not in image_url:
            raise ValueError("image_url requires url and optional detail")
        url = image_url.get("url")
        if not isinstance(url, str) or not url or url != url.strip():
            raise ValueError("image url must be a nonblank, unpadded string")
        detail = image_url.get("detail")
        if detail is not None and detail not in {"auto", "low", "high"}:
            raise ValueError("image detail must be auto, low, or high")
    else:
        raise ValueError("content part type must be text or image_url")

    frozen = _freeze_json(value)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guaranteed by input type
        raise TypeError("content part normalization produced a non-object")
    return frozen


@dataclass(frozen=True, slots=True, repr=False)
class Deployment:
    id: str
    logical_model: str
    provider_model: str = "openai/gpt-4o-mini"
    api_base: str = "http://litellm:4000/v1"
    secret_ref: str = field(default="litellm-internal-key", repr=False)
    quota_scope_id: str = "default-account"
    max_concurrency: int = 1
    target_utilization: float = 0.8
    reserved_slots: int = 0
    rpm: int | None = None
    tpm: int | None = None
    weight: int = 100
    capabilities: frozenset[ModelCapability] = field(
        default_factory=lambda: frozenset({ModelCapability.TEXT})
    )

    def __post_init__(self) -> None:
        _require_safe_identifier("deployment id", self.id)
        _require_safe_identifier("logical_model", self.logical_model)
        _require_unpadded("provider_model", self.provider_model, max_length=512)
        _require_unpadded("secret_ref", self.secret_ref, max_length=512)
        _require_safe_identifier("quota_scope_id", self.quota_scope_id)

        if any(ord(character) < 32 or ord(character) == 127 for character in self.api_base):
            raise ValueError("api_base must be a valid HTTP(S) URL")
        try:
            parsed_base = urlsplit(self.api_base)
            _ = parsed_base.port
        except ValueError:
            raise ValueError("api_base must be a valid HTTP(S) URL") from None
        if (
            len(self.api_base) > 2048
            or self.api_base != self.api_base.strip()
            or parsed_base.scheme not in {"http", "https"}
            or not parsed_base.netloc
            or parsed_base.hostname is None
            or any(character.isspace() for character in parsed_base.netloc)
            or parsed_base.username is not None
            or parsed_base.password is not None
        ):
            raise ValueError("api_base must be a valid HTTP(S) URL")
        if not _is_int(self.max_concurrency) or not 1 <= self.max_concurrency <= 1000:
            raise ValueError("max_concurrency must be between 1 and 1000")
        if (
            isinstance(self.target_utilization, bool)
            or not isinstance(self.target_utilization, int | float)
            or not math.isfinite(self.target_utilization)
            or not 0.5 <= self.target_utilization <= 0.9
        ):
            raise ValueError("target_utilization must be finite and between 0.5 and 0.9")
        if (
            not _is_int(self.reserved_slots)
            or self.reserved_slots < 0
            or self.reserved_slots >= self.max_concurrency
        ):
            raise ValueError("reserved_slots must be nonnegative and below max_concurrency")
        if self.rpm is not None and (not _is_int(self.rpm) or self.rpm <= 0):
            raise ValueError("rpm must be positive")
        if self.tpm is not None and (not _is_int(self.tpm) or self.tpm <= 0):
            raise ValueError("tpm must be positive")
        if not _is_int(self.weight) or self.weight <= 0:
            raise ValueError("weight must be positive")

        capabilities = frozenset(ModelCapability(item) for item in self.capabilities)
        if not capabilities:
            raise ValueError("capabilities must not be empty")
        object.__setattr__(self, "capabilities", capabilities)

    def __repr__(self) -> str:
        return f"Deployment(id={self.id!r}, logical_model={self.logical_model!r})"


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: MessageContent = field(repr=False)

    def __post_init__(self) -> None:
        _require_unpadded("role", self.role, max_length=64)
        if isinstance(self.content, str):
            return
        if not isinstance(self.content, list | tuple):
            raise TypeError("multimodal content must be a list or tuple")
        parts = tuple(self.content)
        if not parts:
            raise ValueError("multimodal content must not be empty")
        if not all(isinstance(part, Mapping) for part in parts):
            raise ValueError("multimodal content parts must be objects")
        object.__setattr__(
            self,
            "content",
            tuple(_normalize_content_part(part) for part in parts),
        )


@dataclass(frozen=True, slots=True)
class StructuredResponseSchema:
    name: str
    schema: Mapping[str, JsonValue] = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.name) > 64 or _SAFE_SCHEMA_NAME.fullmatch(self.name) is None:
            raise ValueError("schema name must be 1-64 safe characters")
        frozen = _freeze_json(self.schema)
        if not isinstance(frozen, Mapping):  # pragma: no cover - guaranteed by annotation
            raise TypeError("schema must be a JSON object")
        object.__setattr__(self, "schema", frozen)


@dataclass(frozen=True, slots=True)
class ModelRequest:
    logical_model: str
    messages: tuple[ModelMessage, ...] = field(repr=False)
    required_capabilities: frozenset[ModelCapability] = field(default_factory=frozenset)
    timeout_seconds: float = 60
    allow_fallback: bool = True
    response_schema: StructuredResponseSchema | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _require_safe_identifier("logical_model", self.logical_model)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")
        if type(self.allow_fallback) is not bool:
            raise ValueError("allow_fallback must be a boolean")
        if self.response_schema is not None and not isinstance(
            self.response_schema, StructuredResponseSchema
        ):
            raise ValueError("response_schema must be StructuredResponseSchema or None")
        messages = tuple(self.messages)
        if not all(isinstance(message, ModelMessage) for message in messages):
            raise ValueError("messages must contain only ModelMessage values")
        object.__setattr__(self, "messages", messages)
        object.__setattr__(
            self,
            "required_capabilities",
            frozenset(ModelCapability(item) for item in self.required_capabilities),
        )
        if (
            self.response_schema is not None
            and ModelCapability.STRUCTURED_OUTPUT not in self.required_capabilities
        ):
            raise ValueError("response schema requires structured_output capability")


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str = field(repr=False)
    name: str = field(repr=False)
    arguments: Mapping[str, JsonValue] = field(repr=False)

    def __post_init__(self) -> None:
        _require_unpadded("tool call id", self.id, max_length=512)
        _require_unpadded("tool name", self.name, max_length=512)
        frozen = _freeze_json(self.arguments)
        if not isinstance(frozen, Mapping):  # pragma: no cover - guaranteed by annotation
            raise TypeError("tool arguments must be a JSON object")
        object.__setattr__(self, "arguments", frozen)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        values = (self.prompt_tokens, self.completion_tokens, self.total_tokens)
        if not all(_is_int(value) and value >= 0 for value in values):
            raise ValueError("token counts must be nonnegative")


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str | None = field(repr=False)
    tool_calls: tuple[ToolCall, ...] = field(default=(), repr=False)
    usage: TokenUsage | None = None
    provider_metadata: Mapping[str, JsonScalar] = field(
        default_factory=lambda: MappingProxyType({}), repr=False
    )

    def __post_init__(self) -> None:
        if self.text is not None and not isinstance(self.text, str):
            raise TypeError("response text must be a string or None")
        tool_calls = tuple(self.tool_calls)
        if not all(isinstance(tool_call, ToolCall) for tool_call in tool_calls):
            raise TypeError("tool_calls must contain only ToolCall values")
        if self.usage is not None and not isinstance(self.usage, TokenUsage):
            raise TypeError("usage must be TokenUsage or None")

        metadata: dict[str, JsonScalar] = {}
        for key, value in self.provider_metadata.items():
            if key not in _PROVIDER_METADATA_KEYS:
                raise ValueError("provider metadata contains an unknown key")
            if key == "created":
                if not _is_int(value) or not 0 <= cast(int, value) < 2**63:
                    raise ValueError("provider metadata created must be a bounded integer")
            elif (
                type(value) is not str
                or _SAFE_PROVIDER_METADATA_STRING.fullmatch(value) is None
            ):
                raise ValueError("provider metadata strings must be bounded safe values")
            metadata[key] = value

        object.__setattr__(self, "tool_calls", tool_calls)
        object.__setattr__(
            self,
            "provider_metadata",
            MappingProxyType(metadata),
        )


ModelMessage.__hash__ = None  # type: ignore[assignment]
StructuredResponseSchema.__hash__ = None  # type: ignore[assignment]
ModelRequest.__hash__ = None  # type: ignore[assignment]
ToolCall.__hash__ = None  # type: ignore[assignment]
ModelResponse.__hash__ = None  # type: ignore[assignment]
