import re
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_hub.models.types import Deployment, ModelCapability

SAFE_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
MAX_IDENTIFIER_LENGTH = 128
Capability = Literal["text", "vision", "tool_calling", "structured_output"]


def _default_capabilities() -> set[Capability]:
    return {"text"}


def _unpadded_non_blank(value: str) -> str:
    if not value:
        raise ValueError("must not be blank")
    if value != value.strip():
        raise ValueError("must not have leading or trailing whitespace")
    return value


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DeploymentDefinition(StrictConfigModel):
    provider: str = Field(max_length=512)
    model: str = Field(max_length=512)
    api_base: str | None = Field(default=None, max_length=2048)
    secret_ref: str = Field(alias="credential_ref", max_length=512)
    quota_scope_id: str = Field(max_length=MAX_IDENTIFIER_LENGTH)
    max_concurrency: int = Field(default=1, ge=1, le=1000)
    target_utilization: float = Field(
        default=0.8, ge=0.5, le=0.9, allow_inf_nan=False
    )
    reserved_slots: int = Field(default=0, ge=0)
    rpm: int | None = Field(default=None, gt=0)
    tpm: int | None = Field(default=None, gt=0)
    input_per_million_usd: Decimal | None = None
    output_per_million_usd: Decimal | None = None
    capabilities: set[Capability] = Field(
        default_factory=_default_capabilities, min_length=1, max_length=4
    )

    @field_validator("provider", "model", "api_base", "secret_ref", "quota_scope_id")
    @classmethod
    def strings_are_unpadded_and_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _unpadded_non_blank(value)

    @model_validator(mode="after")
    def reserved_capacity_is_below_maximum(self) -> Self:
        if self.reserved_slots >= self.max_concurrency:
            raise ValueError("reserved_slots must be less than max_concurrency")
        if (self.input_per_million_usd is None) is not (
            self.output_per_million_usd is None
        ):
            raise ValueError("deployment pricing must provide both input and output rates")
        return self

    @field_validator("input_per_million_usd", "output_per_million_usd", mode="before")
    @classmethod
    def pricing_is_a_bounded_decimal(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) not in {str, Decimal}:
            raise ValueError("pricing must be a decimal string")
        try:
            parsed = value if isinstance(value, Decimal) else Decimal(str(value))
        except Exception as error:
            raise ValueError("pricing must be a bounded USD decimal") from error
        exponent = parsed.as_tuple().exponent
        if (
            not parsed.is_finite()
            or parsed < 0
            or parsed > Decimal(1000000)
            or (isinstance(exponent, int) and exponent < -6)
        ):
            raise ValueError("pricing must be a bounded USD decimal")
        return parsed

    def to_deployment(self, *, deployment_id: str, logical_model: str) -> Deployment:
        values: dict[str, object] = {
            "id": deployment_id,
            "logical_model": logical_model,
            "provider_model": f"{self.provider}/{self.model}",
            "secret_ref": self.secret_ref,
            "quota_scope_id": self.quota_scope_id,
            "max_concurrency": self.max_concurrency,
            "target_utilization": self.target_utilization,
            "reserved_slots": self.reserved_slots,
            "rpm": self.rpm,
            "tpm": self.tpm,
            "capabilities": frozenset(ModelCapability(item) for item in self.capabilities),
            "input_per_million_usd": self.input_per_million_usd,
            "output_per_million_usd": self.output_per_million_usd,
        }
        if self.api_base is not None:
            values["api_base"] = self.api_base
        return Deployment(**values)  # type: ignore[arg-type]


class LogicalModelDefinition(StrictConfigModel):
    deployments: list[DeploymentDefinition] = Field(min_length=1, max_length=128)
    fallback_model: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)

    @field_validator("fallback_model")
    @classmethod
    def fallback_is_unpadded_and_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _unpadded_non_blank(value)


class AgentDefinition(StrictConfigModel):
    id: str = Field(pattern=SAFE_IDENTIFIER.pattern, max_length=MAX_IDENTIFIER_LENGTH)
    role: str = Field(max_length=256)
    prompt: str = Field(max_length=100_000)
    model: str = Field(max_length=MAX_IDENTIFIER_LENGTH)
    skills: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("role", "prompt", "model")
    @classmethod
    def strings_are_unpadded_and_not_blank(cls, value: str) -> str:
        return _unpadded_non_blank(value)

    @field_validator("skills")
    @classmethod
    def skills_are_unique_safe_identifiers(cls, skills: list[str]) -> list[str]:
        if len(skills) != len(set(skills)):
            raise ValueError("skills must be unique")
        invalid = [
            skill
            for skill in skills
            if len(skill) > MAX_IDENTIFIER_LENGTH or SAFE_IDENTIFIER.fullmatch(skill) is None
        ]
        if invalid:
            raise ValueError(f"invalid skill identifier: {invalid}")
        return skills


class PlatformConfig(StrictConfigModel):
    models: dict[str, LogicalModelDefinition] = Field(max_length=256)
    agents: list[AgentDefinition] = Field(max_length=1024)

    @model_validator(mode="after")
    def references_are_valid(self) -> Self:
        unsafe_keys = sorted(
            key
            for key in self.models
            if len(key) > MAX_IDENTIFIER_LENGTH or SAFE_IDENTIFIER.fullmatch(key) is None
        )
        if unsafe_keys:
            raise ValueError(f"invalid logical model key: {unsafe_keys}")

        agent_ids = [agent.id for agent in self.agents]
        duplicate_ids = sorted(
            identifier for identifier in set(agent_ids) if agent_ids.count(identifier) > 1
        )
        if duplicate_ids:
            raise ValueError(f"duplicate agent id: {duplicate_ids}")

        missing_agent_models = sorted(
            {agent.model for agent in self.agents if agent.model not in self.models}
        )
        if missing_agent_models:
            raise ValueError(f"unknown logical model: {missing_agent_models}")

        for name, definition in self.models.items():
            fallback = definition.fallback_model
            if fallback is None:
                continue
            if fallback == name:
                raise ValueError(f"logical model {name!r} cannot fall back to itself")
            if fallback not in self.models:
                raise ValueError(f"unknown fallback model {fallback!r} for {name!r}")
        return self
