"""Bounded stopped-output metadata; never accepted content or retry authority."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_hub.models.failure_receipt import get_gateway_failure_receipt
from agent_hub.models.gateway import (
    GatewayNegotiatedRejectedOutput,
    GatewayRejectedOutput,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.types import (
    ModelRequest,
    RejectedOutputEvidence,
    RejectedOutputReason,
    RejectedOutputStatus,
    RejectedUsageStatus,
    TokenUsage,
    _require_safe_identifier,
)
from agent_hub.runtime.contracts import GatewayProvenance, JsonValue, _mutable_json
from agent_hub.runtime.failure_reason import safe_model_gateway_failure_reason


class _Usage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    prompt_tokens: int = Field(ge=0, le=1_000_000_000)
    completion_tokens: int = Field(ge=0, le=1_000_000_000)
    total_tokens: int = Field(ge=0, le=1_000_000_000)

    @model_validator(mode="after")
    def total(self) -> _Usage:
        if self.prompt_tokens + self.completion_tokens != self.total_tokens:
            raise ValueError("rejection usage total is invalid")
        return self


class RejectedModelRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: int = Field(ge=1, le=1)
    run_id: str
    tenant_id: str
    position: int = Field(ge=0)
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    requested_logical_model: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")
    evidence_present: bool
    output_status: RejectedOutputStatus
    reason: RejectedOutputReason | None
    usage_status: RejectedUsageStatus
    usage: _Usage | None
    cost_usd: str | None
    provenance: GatewayProvenance
    fallback_used: bool
    fallback_from_logical_model: str | None
    fallback_reason: str | None
    negotiation_exhausted: bool
    scope_diagnostic_present: bool
    prior_failure_attempts: int | None = Field(ge=1, le=64)

    @model_validator(mode="after")
    def coherent(self) -> RejectedModelRecord:
        if (type(self.schema_version) is not int
                or str(UUID(self.run_id)) != self.run_id or str(UUID(self.tenant_id)) != self.tenant_id
                or (self.usage_status == "known") != (self.usage is not None)
                or self.evidence_present != (self.reason is not None)):
            raise ValueError("rejection identity or evidence is invalid")
        if not self.evidence_present and (self.output_status != "unknown" or self.usage_status != "missing"):
            raise ValueError("absent rejection evidence was invented")
        if self.cost_usd is not None:
            try:
                cost = Decimal(self.cost_usd)
            except ArithmeticError:
                raise ValueError("rejection cost is invalid") from None
            if not cost.is_finite() or not 0 <= cost <= 1_000_000_000:
                raise ValueError("rejection cost is invalid")
        if self.fallback_used:
            if (self.fallback_from_logical_model != self.requested_logical_model
                    or self.provenance.logical_model == self.requested_logical_model
                    or not self.fallback_reason):
                raise ValueError("rejection fallback is invalid")
            # Reuse the shared identifier validator without admitting arbitrary reason text.
            _require_safe_identifier("fallback reason", self.fallback_reason)
        elif (self.provenance.logical_model != self.requested_logical_model
                or self.fallback_from_logical_model is not None or self.fallback_reason is not None):
            raise ValueError("rejection model provenance is invalid")
        return self

    def to_payload(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self.model_dump(mode="json"))

    def failure_reason(self) -> str:
        evidence = None if not self.evidence_present else RejectedOutputEvidence(
            final_text=None, usage=None if self.usage is None else TokenUsage(**self.usage.model_dump()),
            usage_status=self.usage_status, status=self.output_status, reason=cast(RejectedOutputReason, self.reason),
        )
        kind = GatewayNegotiatedRejectedOutput if self.negotiation_exhausted else GatewayRejectedOutput
        error = kind(evidence=evidence, logical_model=self.provenance.logical_model,
            deployment_id=self.provenance.deployment_id, provider_id=self.provenance.provider_id,
            provider_model=self.provenance.provider_model)
        return safe_model_gateway_failure_reason(error) or "model gateway failed: model response rejected"


def rejection_record(
    error: GatewayRejectedOutput, *, run_id: UUID, tenant_id: UUID,
    position: int, request: ModelRequest, request_hash: str,
) -> RejectedModelRecord:
    if type(error) not in {GatewayRejectedOutput, GatewayNegotiatedRejectedOutput}:
        raise ValueError("rejection error type is not trusted")
    evidence = error.evidence
    if evidence is not None and type(evidence) is not RejectedOutputEvidence:
        raise ValueError("rejection evidence type is invalid")
    usage = None if evidence is None or evidence.usage is None else _Usage(
        prompt_tokens=evidence.usage.prompt_tokens, completion_tokens=evidence.usage.completion_tokens,
        total_tokens=evidence.usage.total_tokens,
    )
    history = get_gateway_failure_receipt(error)
    return RejectedModelRecord(schema_version=1, run_id=str(run_id), tenant_id=str(tenant_id),
        position=position, request_hash=request_hash, requested_logical_model=request.logical_model,
        evidence_present=evidence is not None, output_status="unknown" if evidence is None else evidence.status,
        reason=None if evidence is None else evidence.reason,
        usage_status="missing" if evidence is None else evidence.usage_status, usage=usage,
        cost_usd=None if error.cost_usd is None else str(error.cost_usd),
        provenance=GatewayProvenance(logical_model=error.logical_model, deployment_id=error.deployment_id,
            provider_id=error.provider_id, provider_model=error.provider_model),
        fallback_used=error.fallback_used, fallback_from_logical_model=error.fallback_from_logical_model,
        fallback_reason=error.fallback_reason, negotiation_exhausted=type(error) is GatewayNegotiatedRejectedOutput,
        scope_diagnostic_present=get_gateway_scope_diagnostic(error) is not None,
        prior_failure_attempts=None if history is None else len(history.attempts))


def validate_rejection(
    value: Mapping[str, JsonValue], *, run_id: UUID, tenant_id: UUID, position: int, request_hash: object,
) -> RejectedModelRecord:
    record = RejectedModelRecord.model_validate(_mutable_json(value))
    if (record.run_id != str(run_id) or record.tenant_id != str(tenant_id)
            or record.position != position or record.request_hash != request_hash):
        raise ValueError("rejection scope is invalid")
    return record
