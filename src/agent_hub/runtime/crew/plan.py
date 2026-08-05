"""Strict immutable plans for bounded CrewAI-style dispatch execution."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from decimal import Decimal
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from yaml.nodes import MappingNode
from yaml.tokens import AliasToken, AnchorToken, TagToken

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_MAX_TEXT_BYTES = 65_536
_MAX_PLAN_BYTES = 262_144
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,6})?")
_INTRINSICALLY_DANGEROUS = frozenset(
    {"shell.exec", "docker.socket", "host.write", "privilege.escalate"}
)


class _UniqueSafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate keys at every mapping depth."""


def _construct_unique_mapping(
    loader: _UniqueSafeLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark, "unhashable key", key_node.start_mark
            ) from None
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark, "duplicate key", key_node.start_mark
            ) from None
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


class InvalidDispatchPlan(ValueError):
    """Stable failure at the untrusted plan boundary."""


class _PlanModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


def _identifier(value: str, name: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe identifier")
    return value


def _text(value: str, name: str, *, limit: int = _MAX_TEXT_BYTES) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or len(value.encode("utf-8")) > limit
        or unicodedata.normalize("NFC", value) != value
    ):
        raise ValueError(f"{name} must be nonblank normalized bounded text")
    if any(
        unicodedata.category(character) == "Cf"
        or (unicodedata.category(character) == "Cc" and character not in "\n\t")
        for character in value
    ):
        raise ValueError(f"{name} contains unsafe controls")
    return value


def _id_tuple(value: object, name: str, *, maximum: int = 64) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise TypeError(f"{name} must be a list or tuple")
    items = tuple(value)
    if len(items) > maximum or not all(type(item) is str for item in items):
        raise ValueError(f"{name} is invalid")
    validated = tuple(_identifier(item, name) for item in items)
    if len(set(validated)) != len(validated):
        raise ValueError(f"{name} must be unique")
    return validated


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is bool or type(value) not in {Decimal, int, float, str}:
        raise ValueError(f"{name} must be a bounded decimal")
    text = str(value)
    if len(text) > 32 or _DECIMAL.fullmatch(text) is None:
        raise ValueError(f"{name} must be a bounded decimal")
    number = Decimal(text)
    if not number.is_finite():
        raise ValueError(f"{name} must be finite")
    return Decimal(0) if number.is_zero() else number.normalize()


class AgentSpec(_PlanModel):
    """Immutable generation of one role; no CrewAI object is persisted here."""

    id: str
    role: str
    goal: str = Field(repr=False)
    logical_model: str
    allowed_tools: tuple[str, ...] = ()
    max_output_tokens: int = Field(default=4096, ge=1, le=1_000_000)

    @field_validator("id", "role", "logical_model")
    @classmethod
    def identifiers(cls, value: str) -> str:
        return _identifier(value, "agent identifier")

    @field_validator("goal")
    @classmethod
    def bounded_goal(cls, value: str) -> str:
        return _text(value, "agent goal", limit=16_384)

    @field_validator("allowed_tools", mode="before")
    @classmethod
    def tools(cls, value: object) -> tuple[str, ...]:
        return _id_tuple(value, "agent tools")

    @field_validator("max_output_tokens")
    @classmethod
    def strict_tokens(cls, value: int) -> int:
        if type(value) is not int:
            raise TypeError("max_output_tokens must be an integer")
        return value


class DispatchStep(_PlanModel):
    id: str
    agent: str
    task: str = Field(repr=False)
    depends_on: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    reviewer: str | None = None
    reviewer_retries: int = Field(default=0, ge=0, le=8)
    final_synthesizer: bool = False
    token_budget: int = Field(default=4096, ge=1, le=1_000_000)
    timeout_seconds: float = Field(default=60.0, gt=0, le=3600, allow_inf_nan=False)
    cost_budget_usd: Decimal = Field(default=Decimal(0), ge=0, le=Decimal(1000000))

    @field_validator("id", "agent")
    @classmethod
    def identifiers(cls, value: str) -> str:
        return _identifier(value, "step identifier")

    @field_validator("reviewer")
    @classmethod
    def optional_reviewer(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, "reviewer")

    @field_validator("task")
    @classmethod
    def bounded_task(cls, value: str) -> str:
        return _text(value, "step task")

    @field_validator("depends_on", mode="before")
    @classmethod
    def dependencies(cls, value: object) -> tuple[str, ...]:
        return _id_tuple(value, "dependencies")

    @field_validator("tools", mode="before")
    @classmethod
    def step_tools(cls, value: object) -> tuple[str, ...]:
        return _id_tuple(value, "step tools")

    @field_validator("reviewer_retries", "token_budget")
    @classmethod
    def strict_integers(cls, value: int) -> int:
        if type(value) is not int:
            raise TypeError("step budget and retry values must be integers")
        return value

    @field_validator("final_synthesizer")
    @classmethod
    def strict_boolean(cls, value: bool) -> bool:
        if type(value) is not bool:
            raise TypeError("final_synthesizer must be a boolean")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def finite_timeout(cls, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise TypeError("timeout_seconds must be finite")
        return float(value)

    @field_validator("cost_budget_usd", mode="before")
    @classmethod
    def bounded_cost(cls, value: object) -> Decimal:
        return _decimal(value, "step cost budget")

    @model_validator(mode="after")
    def review_invariants(self) -> DispatchStep:
        if self.reviewer is None and self.reviewer_retries:
            raise ValueError("review retries require a reviewer")
        if self.reviewer == self.agent:
            raise ValueError("reviewer must be independent")
        if self.id in self.depends_on:
            raise ValueError("step cannot depend on itself")
        return self


class DispatchPlan(_PlanModel):
    agents: tuple[AgentSpec, ...]
    steps: tuple[DispatchStep, ...]
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = tuple(sorted(_INTRINSICALLY_DANGEROUS))
    max_steps: int = Field(default=64, ge=1, le=64)
    max_parallelism: int = Field(default=4, ge=1, le=64)
    total_token_budget: int = Field(default=1_000_000, ge=1, le=10_000_000)
    total_timeout_seconds: float = Field(default=3600.0, gt=0, le=86400, allow_inf_nan=False)
    total_cost_usd: Decimal = Field(default=Decimal(0), ge=0, le=Decimal(1000000))

    @field_validator("agents", mode="before")
    @classmethod
    def freeze_agents(cls, value: object) -> tuple[AgentSpec, ...]:
        if not isinstance(value, list | tuple):
            raise TypeError("agents must be a list or tuple")
        result = tuple(value)
        if not 1 <= len(result) <= 128:
            raise ValueError("agent count is invalid")
        return result

    @field_validator("steps", mode="before")
    @classmethod
    def freeze_steps(cls, value: object) -> tuple[DispatchStep, ...]:
        if not isinstance(value, list | tuple):
            raise TypeError("steps must be a list or tuple")
        result = tuple(value)
        if not result or len(result) > 64:
            raise ValueError("step count is invalid")
        return result

    @field_validator("allowed_tools", "denied_tools", mode="before")
    @classmethod
    def plan_tools(cls, value: object) -> tuple[str, ...]:
        return _id_tuple(value, "plan tools", maximum=256)

    @field_validator("max_steps", "max_parallelism", "total_token_budget")
    @classmethod
    def strict_integers(cls, value: int) -> int:
        if type(value) is not int:
            raise TypeError("plan limits must be integers")
        return value

    @field_validator("total_timeout_seconds")
    @classmethod
    def finite_timeout(cls, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise TypeError("total_timeout_seconds must be finite")
        return float(value)

    @field_validator("total_cost_usd", mode="before")
    @classmethod
    def bounded_cost(cls, value: object) -> Decimal:
        return _decimal(value, "total cost budget")

    @model_validator(mode="after")
    def graph_invariants(self) -> DispatchPlan:
        estimated_text_bytes = sum(
            len(agent.goal.encode("utf-8")) for agent in self.agents
        ) + sum(len(step.task.encode("utf-8")) for step in self.steps)
        if estimated_text_bytes > _MAX_PLAN_BYTES:
            raise InvalidDispatchPlan("dispatch plan exceeds size limit")
        self.validate_graph()
        return self

    def validate_graph(self) -> None:
        if len(self.steps) > self.max_steps:
            raise InvalidDispatchPlan("step count exceeds maximum")
        agent_ids = [agent.id for agent in self.agents]
        if len(set(agent_ids)) != len(agent_ids):
            raise InvalidDispatchPlan("agent ids must be unique")
        agents = {agent.id: agent for agent in self.agents}
        step_ids = [step.id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise InvalidDispatchPlan("step ids must be unique")
        known_steps = set(step_ids)
        allowed = set(self.allowed_tools)
        denied = set(self.denied_tools) | _INTRINSICALLY_DANGEROUS
        for step in self.steps:
            if step.agent not in agents or (step.reviewer is not None and step.reviewer not in agents):
                raise InvalidDispatchPlan("step references an unknown agent")
            if any(dependency not in known_steps for dependency in step.depends_on):
                raise InvalidDispatchPlan("step references an unknown dependency")
            agent_tools = set(agents[step.agent].allowed_tools)
            if set(step.tools) - (agent_tools & allowed) or set(step.tools) & denied:
                raise InvalidDispatchPlan("step tool policy is not permitted")
        layers = self._topological_layers()
        if sum(len(layer) for layer in layers) != len(self.steps):
            raise InvalidDispatchPlan("dispatch plan contains a cycle")
        final = [step for step in self.steps if step.final_synthesizer]
        if len(final) != 1:
            raise InvalidDispatchPlan("plan requires exactly one final synthesizer")
        final_step = final[0]
        if any(final_step.id in step.depends_on for step in self.steps):
            raise InvalidDispatchPlan("final synthesizer must be terminal")
        ancestors = self._ancestors(final_step.id)
        leaves = {
            step.id
            for step in self.steps
            if step.id != final_step.id
            and not any(step.id in candidate.depends_on for candidate in self.steps if candidate.id != final_step.id)
        }
        if not leaves <= ancestors:
            raise InvalidDispatchPlan("final synthesizer must cover every required leaf")
        required_tokens = sum(
            step.token_budget
            * (
                1
                if step.reviewer is None
                else 2 + (2 * step.reviewer_retries)
            )
            for step in self.steps
        )
        if required_tokens > self.total_token_budget:
            raise InvalidDispatchPlan("total token budget is insufficient")
        required_timeout = sum(
            step.timeout_seconds
            * (1 if step.reviewer is None else 2 + (2 * step.reviewer_retries))
            for step in self.steps
        )
        if required_timeout > self.total_timeout_seconds:
            raise InvalidDispatchPlan("total timeout budget is insufficient")
        step_cost = sum(
            (
                step.cost_budget_usd
                * (1 if step.reviewer is None else 2 + (2 * step.reviewer_retries))
                for step in self.steps
            ),
            Decimal(0),
        )
        if step_cost > self.total_cost_usd:
            raise InvalidDispatchPlan("total cost budget is insufficient")

    def _topological_layers(self) -> tuple[tuple[str, ...], ...]:
        remaining = {step.id: set(step.depends_on) for step in self.steps}
        layers: list[tuple[str, ...]] = []
        completed: set[str] = set()
        while remaining:
            ready = tuple(sorted(step_id for step_id, deps in remaining.items() if deps <= completed))
            if not ready:
                break
            layers.append(ready)
            completed.update(ready)
            for step_id in ready:
                del remaining[step_id]
        return tuple(layers)

    def _ancestors(self, step_id: str) -> set[str]:
        by_id = {step.id: step for step in self.steps}
        found: set[str] = set()
        stack = list(by_id[step_id].depends_on)
        while stack:
            current = stack.pop()
            if current in found:
                continue
            found.add(current)
            stack.extend(by_id[current].depends_on)
        return found

    @property
    def layers(self) -> tuple[tuple[str, ...], ...]:
        return self._topological_layers()

    @property
    def final_step(self) -> DispatchStep:
        return next(step for step in self.steps if step.final_synthesizer)

    @property
    def digest(self) -> str:
        data = json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        validated: Self | None = None
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > _MAX_PLAN_BYTES:
                raise ValueError
            validated = cls.model_validate_json(encoded, strict=True)
        except Exception as error:  # noqa: BLE001 - hostile serialization boundary
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            del error
        if validated is None:
            raise InvalidDispatchPlan("invalid dispatch plan") from None
        return validated

    @classmethod
    def from_yaml(cls, source: str) -> Self:
        """Load one bounded YAML document without aliases, anchors, or custom tags."""
        if type(source) is not str or not source.strip() or len(source.encode("utf-8")) > _MAX_PLAN_BYTES:
            raise InvalidDispatchPlan("invalid dispatch plan")
        validated: Self | None = None
        try:
            tokens = yaml.scan(source)
            for index, token in enumerate(tokens):
                if index >= 4096 or isinstance(token, AliasToken | AnchorToken | TagToken):
                    raise ValueError
            payload = yaml.load(source, Loader=_UniqueSafeLoader)
            if type(payload) is not dict:
                raise ValueError
            validated = cls.from_payload(payload)
        except Exception as error:  # noqa: BLE001 - hostile YAML boundary
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            del error
        if validated is None:
            raise InvalidDispatchPlan("invalid dispatch plan") from None
        return validated

    @classmethod
    def revalidate(cls, plan: DispatchPlan) -> DispatchPlan:
        if type(plan) is not cls:
            raise InvalidDispatchPlan("invalid dispatch plan")
        try:
            return cls.from_payload(plan.to_payload())
        except (InvalidDispatchPlan, ValidationError):
            raise InvalidDispatchPlan("invalid dispatch plan") from None


# Compact compatibility names used in configuration loaders.
Step = DispatchStep
