from __future__ import annotations

import math
import re
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_hub.domain.runs import TaskMode

_EXECUTABLE_MODES = frozenset(
    {TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID}
)
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_MAX_COST = Decimal(10000)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RouteSource(StrEnum):
    EXPLICIT = "explicit"
    RULE = "rule"
    CLASSIFIER = "classifier"
    VERIFIER = "verifier"
    USER = "user"


def _safe_text(value: str, *, name: str) -> str:
    if value != value.strip() or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must be unpadded printable text")
    return value


class RouteAssessment(BaseModel):
    """A bounded recommendation, never hidden model reasoning."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    mode: TaskMode
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=512, repr=False)
    roles: tuple[str, ...] = Field(default=(), max_length=8)
    estimated_seconds: int = Field(ge=0, le=604_800)
    estimated_cost_usd: Decimal = Field(ge=0, le=_MAX_COST, allow_inf_nan=False)
    risk: RiskLevel
    source: RouteSource
    logical_model: str
    deployment_id: str
    provider_id: str

    @field_validator("mode")
    @classmethod
    def executable_mode_only(cls, value: TaskMode) -> TaskMode:
        if value not in _EXECUTABLE_MODES:
            raise ValueError("assessment mode must be executable")
        return value

    @field_validator("confidence")
    @classmethod
    def finite_confidence(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("confidence must be finite")
        return value

    @field_validator("estimated_cost_usd")
    @classmethod
    def finite_cost(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("estimated cost must be finite")
        return value

    @field_validator("reason")
    @classmethod
    def bounded_reason(cls, value: str) -> str:
        return _safe_text(value, name="reason")

    @field_validator("roles")
    @classmethod
    def safe_roles(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("roles must be unique")
        if any(_SAFE_ID.fullmatch(value) is None for value in values):
            raise ValueError("roles must contain safe identifiers")
        return values

    @field_validator("logical_model", "deployment_id", "provider_id")
    @classmethod
    def safe_provenance(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("provenance must be a safe identifier")
        return value


class RouteDecision(BaseModel):
    """Pure routing result; it does not create or authorize a runtime job."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    mode: TaskMode | None
    needs_user_choice: bool
    status: Literal["ready", "waiting_user_mode"]
    assessments: tuple[RouteAssessment, ...] = Field(max_length=4)
    clarification_reason: str | None = Field(default=None, max_length=128)
    options: tuple[TaskMode, ...]
    decision_token: str | None = Field(default=None, repr=False)
    version: int = Field(ge=1, le=2**31 - 1)
    risk: RiskLevel
    requires_approval: bool
    permissions_still_apply: bool

    @field_validator("options")
    @classmethod
    def executable_options(cls, values: tuple[TaskMode, ...]) -> tuple[TaskMode, ...]:
        if any(value not in _EXECUTABLE_MODES for value in values):
            raise ValueError("clarification options must be executable")
        if len(set(values)) != len(values):
            raise ValueError("clarification options must be unique")
        return values

    @field_validator("decision_token")
    @classmethod
    def safe_decision_token(cls, value: str | None) -> str | None:
        if value is not None and _TOKEN.fullmatch(value) is None:
            raise ValueError("decision token is invalid")
        return value

    @model_validator(mode="after")
    def state_invariants(self) -> RouteDecision:
        if not self.permissions_still_apply:
            raise ValueError("mode routing cannot bypass permissions")
        if self.status == "ready":
            if self.mode not in _EXECUTABLE_MODES or self.needs_user_choice:
                raise ValueError("ready decisions require one executable mode")
            if self.clarification_reason is not None or self.options or self.decision_token is not None:
                raise ValueError("ready decisions cannot carry an active clarification")
        else:
            if self.mode is not None or not self.needs_user_choice:
                raise ValueError("waiting decisions cannot select a mode")
            if self.clarification_reason is None:
                raise ValueError("waiting decisions require a clarification reason")
            if self.options != tuple(_EXECUTABLE_MODE_ORDER):
                raise ValueError("waiting decisions require the canonical options")
            if self.decision_token is None:
                raise ValueError("waiting decisions require a decision token")
        if self.risk is RiskLevel.HIGH and not self.requires_approval:
            raise ValueError("high risk decisions must preserve approval")
        return self


_EXECUTABLE_MODE_ORDER = (
    TaskMode.DIRECT,
    TaskMode.DISPATCH,
    TaskMode.DISCUSS,
    TaskMode.HYBRID,
)
EXECUTABLE_MODES = _EXECUTABLE_MODE_ORDER
