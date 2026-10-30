"""Private, pure bounds and logical snapshots for a future durable adapter.

A configured context is NOT itself proof of total billed usage. Only server-owned
code with an independently verified provider/proxy contract may supply
TrustedContextBound: prompt + completion (including billed hidden/reasoning tokens)
must fit that context for ONE physical attempt. No estimator supplies that trust.
These values are not admission permits, receipts, or action authorization.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field, fields
from decimal import Decimal
from types import MappingProxyType
from typing import Any, cast

from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    StructuredResponseSchema,
    ToolDefinition,
)

_INVALID = "invalid admission bounds"
_INVALID_REQUEST = "invalid model request snapshot"
_MAX_TOKENS = 10_000_000
_MAX_USD = Decimal(1_000_000)


class UnsupportedAdmissionBounds(ValueError):
    """Fixed private-safe rejection: trustworthy bounds or prices unavailable."""

    def __init__(self) -> None:
        super().__init__("admission bounds unsupported")


def _tokens(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_TOKENS:
        raise ValueError(_INVALID)
    return value


def _rate(value: object) -> Decimal:
    if type(value) is not Decimal:
        raise ValueError(_INVALID)
    exponent = value.as_tuple().exponent
    if (
        not value.is_finite()
        or value < 0
        or value > _MAX_USD
        or not isinstance(exponent, int)
        or exponent < -6
    ):
        raise ValueError(_INVALID)
    return value


@dataclass(frozen=True, slots=True)
class KnownPricing:
    """Final server-resolved effective rates, including Gateway pricing overrides."""

    input_per_million_usd: Decimal = field(repr=False)
    output_per_million_usd: Decimal = field(repr=False)

    def __post_init__(self) -> None:
        _rate(self.input_per_million_usd)
        _rate(self.output_per_million_usd)


@dataclass(frozen=True, slots=True)
class TrustedContextBound:
    """Explicit SERVER assertion of the total-usage contract described above.

    Identity must match the resolved Deployment, never an agent-supplied alias.
    Construction is not verification of a provider contract or an issuer seal.
    The adapter must refuse to construct this without independent trusted policy.
    """

    deployment_id: str
    logical_model: str
    provider_model: str = field(repr=False)
    total_tokens: int

    def __post_init__(self) -> None:
        _tokens(self.total_tokens)
        # Reuse existing identity validators without network or credential lookup.
        Deployment(
            id=self.deployment_id,
            logical_model=self.logical_model,
            provider_model=self.provider_model,
        )


def _json(value: object) -> object:
    """Clone only finite, lossless JSON values; reject custom hidden-state objects."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(_INVALID_REQUEST)
        return value
    if type(value) in (dict, MappingProxyType):
        mapping = cast(dict[object, object], value)
        result: dict[str, object] = {}
        for key, item in mapping.items():
            if type(key) is not str:
                raise ValueError(_INVALID_REQUEST)
            result[key] = _json(item)
        return result
    if type(value) in (list, tuple):
        return [_json(item) for item in cast(list[object], value)]
    raise ValueError(_INVALID_REQUEST)


def _encode(value: object) -> bytes:
    return json.dumps(
        _json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _data(
    value: object,
    cls: (
        type[ModelRequest]
        | type[ModelMessage]
        | type[StructuredResponseSchema]
        | type[ToolDefinition]
        | type[NativeAssistantMessage]
        | type[NativeToolCall]
    ),
    names: str,
) -> dict[str, Any]:
    # New fields/types require a conscious codec update, never a partial hash.
    if type(value) is not cls or {f.name for f in fields(cls)} != set(names.split()):
        raise ValueError(_INVALID_REQUEST)
    return {name: getattr(value, name) for name in names.split()}


def _request_payload(request: ModelRequest) -> dict[str, Any]:
    data = _data(
        request,
        ModelRequest,
        "logical_model messages required_capabilities timeout_seconds allow_fallback "
        "max_output_tokens response_schema tools native_tool_continuation required_tool",
    )
    messages = []
    for message in request.messages:
        item = _data(message, ModelMessage, "role content native_assistant tool_call_id")
        native = message.native_assistant
        if native is not None:
            _data(
                native, NativeAssistantMessage, "deployment_id content tool_calls reasoning_content"
            )
            for call in native.tool_calls:
                _data(call, NativeToolCall, "id name arguments")
            # to_wire omits deployment_id; the full existing payload codec preserves it.
            item["native_assistant"] = native.to_payload()
        messages.append(item)
    data["messages"] = messages
    data["required_capabilities"] = sorted(
        ModelCapability(c).value for c in request.required_capabilities
    )
    schema = request.response_schema
    if schema is not None:
        data["response_schema"] = _data(schema, StructuredResponseSchema, "name schema")
        # Existing transports fix strict=True; it is not a ModelRequest field.
        data["response_schema"]["strict"] = True
    data["tools"] = [
        _data(tool, ToolDefinition, "name description parameters") for tool in request.tools
    ]
    return data


def _restore_request(encoded: bytes) -> ModelRequest:
    data: dict[str, Any] = json.loads(encoded)
    messages = []
    for item in data["messages"]:
        native = item["native_assistant"]
        if native is not None:
            item["native_assistant"] = NativeAssistantMessage.from_payload(native)
        messages.append(ModelMessage(**item))
    data["messages"] = tuple(messages)
    schema = data["response_schema"]
    if schema is not None:
        if schema.pop("strict") is not True:
            raise ValueError(_INVALID_REQUEST)
        data["response_schema"] = StructuredResponseSchema(**schema)
    data["tools"] = tuple(ToolDefinition(**tool) for tool in data["tools"])
    data["required_capabilities"] = frozenset(
        ModelCapability(c) for c in data["required_capabilities"]
    )
    return ModelRequest(**data)


@dataclass(frozen=True, slots=True)
class FrozenModelRequest:
    """Private canonical logical bytes; no deployment secrets or action digest.

    Use freeze_model_request before the first await. Each request() constructs all
    nested dataclasses anew; do not execute the caller's original mutable objects.
    canonical_bytes() is PRIVATE prompt material, never an audit/event projection.
    """

    _canonical: bytes = field(repr=False)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self._canonical).hexdigest()

    def canonical_bytes(self) -> bytes:
        return self._canonical

    def request(self) -> ModelRequest:
        try:
            return _restore_request(self._canonical)
        except (ValueError, TypeError, AttributeError, KeyError, RecursionError, OverflowError):
            raise ValueError(_INVALID_REQUEST) from None


def freeze_model_request(request: ModelRequest) -> FrozenModelRequest:
    """Lossless canonical UTF-8 JSON + validation, with fixed private-safe errors."""
    try:
        encoded = _encode(_request_payload(request))
        # Revalidate nested objects too: frozen=True can be bypassed by object.__setattr__.
        rebuilt = _restore_request(encoded)
        if _encode(_request_payload(rebuilt)) != encoded:
            raise ValueError(_INVALID_REQUEST)
        return FrozenModelRequest(encoded)
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, OverflowError):
        raise ValueError(_INVALID_REQUEST) from None


def _deployment_bytes(deployment: Deployment) -> bytes:
    if type(deployment) is not Deployment:
        raise ValueError(_INVALID)
    data = {f.name: getattr(deployment, f.name) for f in fields(Deployment)}
    for key in ("input_per_million_usd", "output_per_million_usd"):
        value = data[key]
        if value is not None:
            data[key] = str(_rate(value))
    data["capabilities"] = sorted(ModelCapability(c).value for c in deployment.capabilities)
    encoded = _encode(data)
    _restore_deployment(encoded)  # Validate a copy; do not mutate the caller in __post_init__.
    return encoded


def _restore_deployment(encoded: bytes) -> Deployment:
    data: dict[str, Any] = json.loads(encoded)
    for key in ("input_per_million_usd", "output_per_million_usd"):
        if data[key] is not None:
            data[key] = Decimal(data[key])
    data["capabilities"] = frozenset(ModelCapability(c) for c in data["capabilities"])
    return Deployment(**data)


@dataclass(frozen=True, slots=True)
class AdmissionBounds:
    """One-attempt conservative reservation, not a permit or actual usage receipt."""

    total_tokens: int
    cost_usd: Decimal
    known_zero: bool
    _deployment: bytes = field(repr=False)
    _pricing: bytes = field(repr=False)
    _request: bytes = field(repr=False)

    def deployment(self) -> Deployment:
        """Independent private resolved-deployment copy, including routing config."""
        return _restore_deployment(self._deployment)

    def pricing(self) -> KnownPricing:
        values = json.loads(self._pricing)
        return KnownPricing(Decimal(values[0]), Decimal(values[1]))

    def request(self) -> ModelRequest:
        return FrozenModelRequest(self._request).request()

    @property
    def request_digest(self) -> str:
        return hashlib.sha256(self._request).hexdigest()


def compute_admission_bounds(
    deployment: Deployment,
    request: ModelRequest,
    *,
    trusted_context: TrustedContextBound | None = None,
    effective_pricing: KnownPricing | None = None,
) -> AdmissionBounds:
    """Validate the selected EFFECTIVE (already clamped) request and resolved config.

    Final server-owned effective_pricing wins over Deployment prices. In particular,
    pass Gateway's resolved pricing override here; never use legacy _cost_usd's
    missing-pricing zero. None means use BOTH explicitly configured deployment rates.
    Unknown context/prices/contract reject before any I/O (this module performs none).
    """
    try:
        frozen = freeze_model_request(request)
        selected_bytes = _deployment_bytes(deployment)
        selected = _restore_deployment(selected_bytes)
        context = selected.context_window_tokens
        if context is None or trusted_context is None:
            raise UnsupportedAdmissionBounds()
        context = _tokens(context)
        if type(trusted_context) is not TrustedContextBound:
            raise ValueError(_INVALID)
        trust = TrustedContextBound(
            trusted_context.deployment_id,
            trusted_context.logical_model,
            trusted_context.provider_model,
            trusted_context.total_tokens,
        )
        if (
            trust.deployment_id != selected.id
            or trust.logical_model != selected.logical_model
            or trust.provider_model != selected.provider_model
            or trust.total_tokens != context
        ):
            raise ValueError(_INVALID)
        effective = frozen.request()
        if effective.max_output_tokens > context or (
            selected.max_output_tokens is not None
            and effective.max_output_tokens > selected.max_output_tokens
        ):
            raise ValueError(_INVALID)
        if effective_pricing is not None:
            if type(effective_pricing) is not KnownPricing:
                raise ValueError(_INVALID)
            pricing = KnownPricing(
                effective_pricing.input_per_million_usd, effective_pricing.output_per_million_usd
            )
        else:
            if selected.input_per_million_usd is None or selected.output_per_million_usd is None:
                raise UnsupportedAdmissionBounds()
            pricing = KnownPricing(selected.input_per_million_usd, selected.output_per_million_usd)
        # Integer microdollars avoid ambient Decimal precision/rounding/trap changes.
        # ceil(context * max(rate) / 1M USD * 1M microUSD) == ceil(context * max(rate)).
        numerator, denominator = max(
            pricing.input_per_million_usd, pricing.output_per_million_usd
        ).as_integer_ratio()
        micros = (context * numerator + denominator - 1) // denominator
        if micros > 1_000_000_000_000:
            raise ValueError(_INVALID)
        cost = Decimal(f"{micros // 1_000_000}.{micros % 1_000_000:06d}")
        return AdmissionBounds(
            context,
            cost,
            pricing.input_per_million_usd == 0 and pricing.output_per_million_usd == 0,
            selected_bytes,
            _encode([str(pricing.input_per_million_usd), str(pricing.output_per_million_usd)]),
            frozen.canonical_bytes(),
        )
    except UnsupportedAdmissionBounds:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, OverflowError):
        raise ValueError(_INVALID) from None
