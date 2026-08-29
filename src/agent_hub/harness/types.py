from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, cast
from uuid import UUID

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SAFE_PROVIDER_MODEL = re.compile(r"^[A-Za-z0-9_.:/@ -]{1,512}$")
_KNOWN_CAPABILITIES = frozenset(
    {
        "text",
        "vision",
        "audio",
        "tool_calling",
        "structured_output",
        "image_generation",
        "video_generation",
        "audio_generation",
    }
)
_COST_TIERS = frozenset({"low", "standard", "premium"})
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "secret_ref",
        "password",
        "token",
        "authorization",
        "raw_prompt",
        "hidden_reasoning",
        "reasoning",
        "chain_of_thought",
        "cot",
    }
)
_MAX_JSON_STRING_BYTES = 65_536
_MAX_JSON_PAYLOAD_BYTES = 65_536
_MAX_RESULT_STRING_BYTES = 60_000
_MAX_JSON_NODES = 4_096
_MAX_JSON_DEPTH = 20

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    recent_error_rate: float = 0.0
    queue_pressure: float = 0.0
    rate_limited: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("recent_error_rate", self.recent_error_rate),
            ("queue_pressure", self.queue_pressure),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be between 0 and 1")
        if type(self.rate_limited) is not bool:
            raise ValueError("rate_limited must be a boolean")

    @property
    def degraded(self) -> bool:
        return self.rate_limited or self.recent_error_rate >= 0.5 or self.queue_pressure >= 0.9


@dataclass(frozen=True, slots=True)
class ProviderCapabilityProfile:
    provider: str
    model: str
    logical_model: str
    capabilities: frozenset[str]
    supports_reasoning_delta: bool = False
    supports_streamed_tool_call_delta: bool = False
    supports_parallel_tool_calls: bool = False
    supports_prefix_cache: bool = False
    supports_long_running_tasks: bool = False
    max_context_tokens: int = 16_384
    cost_latency_tier: str = "standard"
    sandbox_modes: tuple[str, ...] = ("none",)
    runtime_health: RuntimeHealth = field(default_factory=RuntimeHealth)

    def __post_init__(self) -> None:
        _require_safe_id("provider", self.provider)
        _require_safe_id("logical_model", self.logical_model)
        _require_unpadded_model("model", self.model)
        if type(self.max_context_tokens) is not int or not 1 <= self.max_context_tokens <= 10_000_000:
            raise ValueError("max_context_tokens must be a positive bounded integer")
        if self.cost_latency_tier not in _COST_TIERS:
            raise ValueError("cost_latency_tier is invalid")
        capabilities = _capability_set(self.capabilities)
        if not capabilities:
            raise ValueError("capabilities must not be empty")
        sandbox_modes = tuple(self.sandbox_modes)
        if not sandbox_modes:
            raise ValueError("sandbox_modes must not be empty")
        for mode in sandbox_modes:
            _require_safe_id("sandbox mode", mode)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "sandbox_modes", sandbox_modes)

    @classmethod
    def deepseek(
        cls,
        model: str,
        *,
        logical_model: str,
        runtime_health: RuntimeHealth | None = None,
    ) -> ProviderCapabilityProfile:
        return cls(
            provider="deepseek",
            model=model,
            logical_model=logical_model,
            capabilities=frozenset({"text", "tool_calling", "structured_output"}),
            supports_reasoning_delta=True,
            supports_streamed_tool_call_delta=True,
            supports_prefix_cache=True,
            max_context_tokens=128_000,
            cost_latency_tier="low",
            sandbox_modes=("none", "read_only", "restricted"),
            runtime_health=runtime_health or RuntimeHealth(),
        )

    @classmethod
    def openai_codex(
        cls,
        model: str,
        *,
        logical_model: str,
        runtime_health: RuntimeHealth | None = None,
    ) -> ProviderCapabilityProfile:
        return cls(
            provider="openai",
            model=model,
            logical_model=logical_model,
            capabilities=frozenset({"text", "tool_calling", "structured_output", "vision"}),
            supports_reasoning_delta=True,
            supports_streamed_tool_call_delta=True,
            supports_parallel_tool_calls=True,
            supports_long_running_tasks=True,
            max_context_tokens=400_000,
            cost_latency_tier="premium",
            sandbox_modes=("none", "read_only", "restricted", "workspace_write"),
            runtime_health=runtime_health or RuntimeHealth(),
        )


@dataclass(frozen=True, slots=True)
class ContextWindowAssessment:
    estimated_input_tokens: int
    max_context_tokens: int
    headroom_tokens: int
    usage_ratio: float
    risk_level: Literal["unknown", "fits", "near_limit", "exceeded"]
    fits: bool

    def __post_init__(self) -> None:
        for name in ("estimated_input_tokens", "max_context_tokens", "headroom_tokens"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
        if self.estimated_input_tokens < 0:
            raise ValueError("estimated_input_tokens must be nonnegative")
        if self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        if (
            isinstance(self.usage_ratio, bool)
            or not isinstance(self.usage_ratio, int | float)
            or not math.isfinite(self.usage_ratio)
            or self.usage_ratio < 0
        ):
            raise ValueError("usage_ratio must be finite and nonnegative")
        if self.risk_level not in {"unknown", "fits", "near_limit", "exceeded"}:
            raise ValueError("risk_level is invalid")
        if type(self.fits) is not bool:
            raise ValueError("fits must be a boolean")
        if self.fits and self.risk_level == "exceeded":
            raise ValueError("exceeded context windows must not fit")


def assess_context_window(
    profile: ProviderCapabilityProfile,
    requirements: HarnessTaskRequirements,
) -> ContextWindowAssessment:
    if not isinstance(profile, ProviderCapabilityProfile):
        raise TypeError("profile must be ProviderCapabilityProfile")
    if not isinstance(requirements, HarnessTaskRequirements):
        raise TypeError("requirements must be HarnessTaskRequirements")
    estimated = requirements.estimated_input_tokens
    maximum = profile.max_context_tokens
    headroom = maximum - estimated
    if estimated == 0:
        risk_level: Literal["unknown", "fits", "near_limit", "exceeded"] = "unknown"
        ratio = 0.0
    else:
        ratio = estimated / maximum
        if estimated > maximum:
            risk_level = "exceeded"
        elif ratio >= 0.9:
            risk_level = "near_limit"
        else:
            risk_level = "fits"
    return ContextWindowAssessment(
        estimated_input_tokens=estimated,
        max_context_tokens=maximum,
        headroom_tokens=headroom,
        usage_ratio=ratio,
        risk_level=risk_level,
        fits=risk_level != "exceeded",
    )


@dataclass(frozen=True, slots=True)
class HarnessTaskRequirements:
    required_capabilities: frozenset[str]
    required_logical_model: str | None = None
    needs_reasoning: bool = False
    needs_streamed_tool_calls: bool = False
    needs_parallel_tool_calls: bool = False
    needs_long_running: bool = False
    requires_sandbox: bool = False
    privacy_sensitive: bool = False
    estimated_input_tokens: int = 0
    prefers_prefix_cache: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_capabilities", _capability_set(self.required_capabilities))
        if self.required_logical_model is not None:
            _require_safe_id("required_logical_model", self.required_logical_model)
        for name in (
            "needs_reasoning",
            "needs_streamed_tool_calls",
            "needs_parallel_tool_calls",
            "needs_long_running",
            "requires_sandbox",
            "privacy_sensitive",
            "prefers_prefix_cache",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if type(self.estimated_input_tokens) is not int or not 0 <= self.estimated_input_tokens <= 10_000_000:
            raise ValueError("estimated_input_tokens must be a bounded integer")


@dataclass(frozen=True, slots=True)
class HarnessPolicy:
    allowed_providers: frozenset[str] = field(default_factory=frozenset)
    denied_providers: frozenset[str] = field(default_factory=frozenset)
    prefer_low_cost: bool = False
    require_approval_for_sensitive: bool = True

    def __post_init__(self) -> None:
        allowed = _safe_id_set("allowed provider", self.allowed_providers)
        denied = _safe_id_set("denied provider", self.denied_providers)
        if allowed & denied:
            raise ValueError("provider policy is contradictory")
        if type(self.prefer_low_cost) is not bool:
            raise ValueError("prefer_low_cost must be a boolean")
        if type(self.require_approval_for_sensitive) is not bool:
            raise ValueError("require_approval_for_sensitive must be a boolean")
        object.__setattr__(self, "allowed_providers", allowed)
        object.__setattr__(self, "denied_providers", denied)


@dataclass(frozen=True, slots=True)
class HermesContextHint:
    project_id: str | None = None
    conversation_id: str | None = None
    preferred_provider: str | None = None
    confidence: float = 0.0

    def __post_init__(self) -> None:
        for name in ("project_id", "conversation_id", "preferred_provider"):
            value = getattr(self, name)
            if value is not None:
                _require_safe_id(name, value)
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class HarnessDecision:
    tenant_id: UUID
    mode: str
    selected_provider: str
    selected_model: str
    selected_logical_model: str
    requires_approval: bool
    capability_reasons: tuple[str, ...]
    policy_reasons: tuple[str, ...]
    context_reasons: tuple[str, ...] = ()
    fallbacks_considered: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.tenant_id) is not UUID:
            raise TypeError("tenant_id must be UUID")
        _require_safe_id("mode", self.mode)
        _require_safe_id("selected_provider", self.selected_provider)
        _require_unpadded_model("selected_model", self.selected_model)
        _require_safe_id("selected_logical_model", self.selected_logical_model)
        if type(self.requires_approval) is not bool:
            raise ValueError("requires_approval must be a boolean")
        for name in (
            "capability_reasons",
            "policy_reasons",
            "context_reasons",
            "fallbacks_considered",
        ):
            object.__setattr__(self, name, _safe_reason_tuple(name, getattr(self, name)))

    def to_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "tenant_id": str(self.tenant_id),
                "mode": self.mode,
                "selected_provider": self.selected_provider,
                "selected_model": self.selected_model,
                "selected_logical_model": self.selected_logical_model,
                "requires_approval": self.requires_approval,
                "capability_reasons": list(self.capability_reasons),
                "policy_reasons": list(self.policy_reasons),
                "context_reasons": list(self.context_reasons),
                "fallbacks_considered": list(self.fallbacks_considered),
            }
        )


@dataclass(frozen=True, slots=True)
class HarnessToolCallRequest:
    run_id: UUID
    actor: str
    tool_name: str
    arguments: Mapping[str, JsonValue]
    approval_required: bool
    sandbox: str
    idempotency_key: str
    call_id: str = ""

    def __post_init__(self) -> None:
        if type(self.run_id) is not UUID:
            raise TypeError("run_id must be UUID")
        _require_safe_id("actor", self.actor)
        _require_safe_id("tool_name", self.tool_name)
        _require_safe_id("sandbox", self.sandbox)
        _require_safe_text("idempotency_key", self.idempotency_key, max_length=160)
        if type(self.approval_required) is not bool:
            raise ValueError("approval_required must be a boolean")
        arguments = _freeze_json_object(self.arguments, reject_sensitive=True)
        object.__setattr__(self, "arguments", arguments)
        call_id = self.call_id or _tool_call_id(
            run_id=self.run_id,
            actor=self.actor,
            tool_name=self.tool_name,
            arguments=arguments,
            idempotency_key=self.idempotency_key,
        )
        _require_safe_id("call_id", call_id)
        object.__setattr__(self, "call_id", call_id)

    def to_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "run_id": str(self.run_id),
                "actor": self.actor,
                "tool_name": self.tool_name,
                "arguments": _mutable_json(self.arguments),
                "approval_required": self.approval_required,
                "sandbox": self.sandbox,
                "idempotency_key": self.idempotency_key,
                "call_id": self.call_id,
            }
        )


@dataclass(frozen=True, slots=True)
class HarnessToolCallResult:
    call_id: str
    tool_name: str
    status: Literal["succeeded", "failed"]
    payload: Mapping[str, JsonValue]
    failure_reason: str | None = None
    truncated: bool = False

    def __post_init__(self) -> None:
        _require_safe_id("call_id", self.call_id)
        _require_safe_id("tool_name", self.tool_name)
        if self.status not in {"succeeded", "failed"}:
            raise ValueError("tool result status is invalid")
        if self.status == "failed" and self.failure_reason is None:
            raise ValueError("failed tool results require a failure reason")
        if self.failure_reason is not None:
            _require_safe_text("failure_reason", self.failure_reason, max_length=512)
        payload, truncated = _bounded_result_payload(self.payload)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "truncated", bool(self.truncated or truncated))

    def to_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "call_id": self.call_id,
                "tool_name": self.tool_name,
                "status": self.status,
                "payload": _mutable_json(self.payload),
                "failure_reason": self.failure_reason,
                "truncated": self.truncated,
            }
        )


def _require_safe_id(name: str, value: str) -> None:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe identifier")


def _require_safe_text(name: str, value: str, *, max_length: int) -> None:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{name} must be bounded printable text")


def _require_unpadded_model(name: str, value: str) -> None:
    if type(value) is not str or value != value.strip() or _SAFE_PROVIDER_MODEL.fullmatch(value) is None:
        raise ValueError(f"{name} must be bounded printable text")


def _safe_id_set(name: str, values: Iterable[str]) -> frozenset[str]:
    result = frozenset(values)
    for value in result:
        _require_safe_id(name, value)
    return result


def _mutable_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(item) for item in value]
    return value


def _freeze_json_object(
    value: Mapping[str, JsonValue],
    *,
    reject_sensitive: bool,
) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError("payload must be a JSON object")
    nodes = 0

    def visit(item: object, depth: int, key_name: str | None = None) -> JsonValue:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ValueError("payload exceeds structural limits")
        if reject_sensitive and key_name is not None and _is_sensitive_key(key_name):
            raise ValueError("tool arguments contain sensitive data")
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
                    raise TypeError("JSON object keys must be strings")
                frozen[key] = visit(child, depth + 1, key)
            return MappingProxyType(frozen)
        if isinstance(item, list | tuple):
            return tuple(visit(child, depth + 1) for child in item)
        raise TypeError("value is not JSON-compatible")

    frozen = visit(value, 0)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise TypeError("payload must be a JSON object")
    return frozen


def _bounded_result_payload(value: Mapping[str, JsonValue]) -> tuple[Mapping[str, JsonValue], bool]:
    bounded, truncated = _truncate_large_strings(value)
    payload = _freeze_json_object(cast(Mapping[str, JsonValue], bounded), reject_sensitive=False)
    mutable = _mutable_json(payload)
    encoded = json.dumps(
        mutable,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) <= _MAX_JSON_PAYLOAD_BYTES:
        return payload, truncated
    fallback: Mapping[str, JsonValue] = MappingProxyType(
        {
            "_truncated": True,
            "original_size_bytes": len(encoded),
            "message": "tool result payload exceeded replay limit",
        }
    )
    return fallback, True


def _truncate_large_strings(value: object) -> tuple[object, bool]:
    if type(value) is str:
        encoded = value.encode("utf-8")
        if len(encoded) <= _MAX_RESULT_STRING_BYTES:
            return value, False
        return encoded[:_MAX_RESULT_STRING_BYTES].decode("utf-8", errors="ignore"), True
    if isinstance(value, dict):
        truncated = False
        result: dict[object, object] = {}
        for key, item in value.items():
            bounded, item_truncated = _truncate_large_strings(item)
            result[key] = bounded
            truncated = truncated or item_truncated
        return result, truncated
    if isinstance(value, list):
        truncated = False
        list_result: list[object] = []
        for item in value:
            bounded, item_truncated = _truncate_large_strings(item)
            list_result.append(bounded)
            truncated = truncated or item_truncated
        return list_result, truncated
    if isinstance(value, tuple):
        truncated = False
        tuple_result: list[object] = []
        for item in value:
            bounded, item_truncated = _truncate_large_strings(item)
            tuple_result.append(bounded)
            truncated = truncated or item_truncated
        return tuple(tuple_result), truncated
    return value, False


def _is_sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        ("_api_key", "_secret", "_password", "_access_token", "_refresh_token")
    )


def _tool_call_id(
    *,
    run_id: UUID,
    actor: str,
    tool_name: str,
    arguments: Mapping[str, JsonValue],
    idempotency_key: str,
) -> str:
    encoded = json.dumps(
        {
            "run_id": str(run_id),
            "actor": actor,
            "tool_name": tool_name,
            "arguments": _mutable_json(arguments),
            "idempotency_key": idempotency_key,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"call-{hashlib.sha256(encoded).hexdigest()[:24]}"


def _capability_set(values: Iterable[str]) -> frozenset[str]:
    result = frozenset(values)
    if not all(type(value) is str for value in result):
        raise TypeError("capabilities must contain strings")
    unknown = result - _KNOWN_CAPABILITIES
    if unknown:
        raise ValueError(f"unknown capabilities: {', '.join(sorted(unknown))}")
    return result


def _safe_reason_tuple(name: str, values: Iterable[str]) -> tuple[str, ...]:
    result = tuple(values)
    for value in result:
        if type(value) is not str or not value or value != value.strip() or len(value) > 256:
            raise ValueError(f"{name} contains an invalid reason")
    return result
