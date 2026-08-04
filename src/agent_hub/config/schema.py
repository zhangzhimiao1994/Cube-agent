import re
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

SAFE_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class DeploymentDefinition(BaseModel):
    provider: str
    model: str
    api_base: str | None = None
    secret_ref: str
    quota_scope_id: str
    max_concurrency: int = Field(default=1, ge=1, le=1000)
    target_utilization: float = Field(default=0.8, ge=0.5, le=0.9)
    reserved_slots: int = Field(default=0, ge=0)
    rpm: int | None = Field(default=None, gt=0)
    tpm: int | None = Field(default=None, gt=0)
    capabilities: set[str] = Field(default_factory=lambda: {"text"})

    @field_validator("provider", "model", "secret_ref", "quota_scope_id")
    @classmethod
    def critical_strings_are_not_blank(cls, value: str) -> str:
        return _non_blank(value)

    @model_validator(mode="after")
    def reserved_capacity_is_below_maximum(self) -> Self:
        if self.reserved_slots >= self.max_concurrency:
            raise ValueError("reserved_slots must be less than max_concurrency")
        return self


class LogicalModelDefinition(BaseModel):
    deployments: list[DeploymentDefinition] = Field(min_length=1)
    fallback_model: str | None = None


class AgentDefinition(BaseModel):
    id: str = Field(pattern=SAFE_IDENTIFIER.pattern)
    role: str
    prompt: str
    model: str
    skills: list[str] = Field(default_factory=list)

    @field_validator("role", "prompt", "model")
    @classmethod
    def critical_strings_are_not_blank(cls, value: str) -> str:
        return _non_blank(value)


class PlatformConfig(BaseModel):
    models: dict[str, LogicalModelDefinition]
    agents: list[AgentDefinition]

    @model_validator(mode="after")
    def references_are_valid(self) -> Self:
        unsafe_keys = sorted(key for key in self.models if SAFE_IDENTIFIER.fullmatch(key) is None)
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
