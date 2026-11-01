"""Exact typed input surfaces for the pure admission-bounds tests."""
from decimal import Decimal
from typing import Protocol, TypedDict

from agent_hub.models.admission_bounds import (
    AdmissionBounds,
    FrozenModelRequest,
)
from agent_hub.models.admission_bounds import (
    KnownPricing as PricingValue,
)
from agent_hub.models.admission_bounds import (
    TrustedContextBound as ContextValue,
)
from agent_hub.models.admission_bounds import (
    UnsupportedAdmissionBounds as UnsupportedValue,
)
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    StructuredResponseSchema,
    ToolDefinition,
)


class BoundsAPI(Protocol):
    KnownPricing: type[PricingValue]
    TrustedContextBound: type[ContextValue]
    UnsupportedAdmissionBounds: type[UnsupportedValue]

    def freeze_model_request(self, request: ModelRequest) -> FrozenModelRequest: ...

    def compute_admission_bounds(
        self, deployment: Deployment, request: ModelRequest, *,
        trusted_context: ContextValue | None = None,
        effective_pricing: PricingValue | None = None,
    ) -> AdmissionBounds: ...


class DeploymentOverrides(TypedDict, total=False):
    id: str
    api_base: str
    secret_ref: str
    context_window_tokens: int | None
    max_output_tokens: int | None
    input_per_million_usd: Decimal | None
    output_per_million_usd: Decimal | None


class RequestOverrides(TypedDict, total=False):
    logical_model: str
    messages: tuple[ModelMessage, ...]
    timeout_seconds: float
    allow_fallback: bool
    max_output_tokens: int
    required_tool: str | None
    required_capabilities: frozenset[ModelCapability]
    response_schema: StructuredResponseSchema | None
    native_tool_continuation: bool
    tools: tuple[ToolDefinition, ...]


class ReserveOverrides(TypedDict, total=False):
    effective_pricing: PricingValue | None


class EnumValues(TypedDict):
    enum: list[str]


class MutableEnumSchema(TypedDict):
    type: str
    properties: dict[str, EnumValues]
