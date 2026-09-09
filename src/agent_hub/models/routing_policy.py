from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from agent_hub.config.schema import PlatformConfig
from agent_hub.models.types import Deployment

FallbackExecutionPolicy = Literal["configured", "disabled"]


class DeploymentRoutingConstraintError(RuntimeError):
    """Stable failure for unavailable harness deployment constraints."""


class FallbackExecutionPolicyError(RuntimeError):
    """Stable failure for invalid harness fallback execution policy."""


@dataclass(frozen=True, slots=True)
class DeploymentRoutingConstraint:
    logical_model: str
    provider: str
    model: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", self.provider.casefold())

    def matches(self, deployment: Deployment) -> bool:
        deployment_provider, _, provider_model = deployment.provider_model.partition("/")
        request_model = deployment.request_model or provider_model
        return self.matches_provider_model(
            logical_model=deployment.logical_model,
            provider=deployment_provider,
            model=request_model,
        )

    def matches_provider_model(
        self,
        *,
        logical_model: str,
        provider: str,
        model: str,
    ) -> bool:
        if logical_model != self.logical_model or provider.casefold() != self.provider:
            return False
        selected_provider, separator, selected_model = self.model.partition("/")
        if not separator:
            return self.model == model
        return selected_provider.casefold() == provider.casefold() and selected_model == model


def deployment_routing_constraint_from_decision(
    config: PlatformConfig,
    routing_decision: object | None,
) -> DeploymentRoutingConstraint | None:
    if not isinstance(routing_decision, Mapping):
        return None
    harness_decision = routing_decision.get("harness_decision")
    if not isinstance(harness_decision, Mapping):
        return None
    logical_model = harness_decision.get("selected_logical_model")
    provider = harness_decision.get("selected_provider")
    model = harness_decision.get("selected_model")
    if not (
        isinstance(logical_model, str)
        and isinstance(provider, str)
        and isinstance(model, str)
        and logical_model
        and provider
        and model
    ):
        return None
    if logical_model not in config.models:
        raise DeploymentRoutingConstraintError("harness logical model is unavailable")
    return DeploymentRoutingConstraint(
        logical_model=logical_model,
        provider=provider.casefold(),
        model=model,
    )


def constrain_deployments_for_routing(
    deployments: tuple[Deployment, ...],
    constraint: DeploymentRoutingConstraint | None,
) -> tuple[Deployment, ...]:
    if constraint is None:
        return deployments
    constrained: list[Deployment] = []
    matched = False
    for deployment in deployments:
        if deployment.logical_model != constraint.logical_model:
            constrained.append(deployment)
            continue
        if constraint.matches(deployment):
            constrained.append(deployment)
            matched = True
    if not matched:
        raise DeploymentRoutingConstraintError("harness model selection is unavailable")
    return tuple(constrained)


def fallback_execution_policy_from_decision(
    routing_decision: object | None,
) -> FallbackExecutionPolicy:
    if not isinstance(routing_decision, Mapping):
        return "configured"
    raw_policy = routing_decision.get("harness_policy")
    if raw_policy is None:
        return "configured"
    if not isinstance(raw_policy, Mapping):
        raise FallbackExecutionPolicyError("invalid fallback execution policy")
    value = raw_policy.get("fallback_policy")
    if value is None:
        return "configured"
    if isinstance(value, str) and value in {"configured", "disabled"}:
        return cast(FallbackExecutionPolicy, value)
    raise FallbackExecutionPolicyError("invalid fallback execution policy")


__all__ = [
    "DeploymentRoutingConstraint",
    "DeploymentRoutingConstraintError",
    "FallbackExecutionPolicy",
    "FallbackExecutionPolicyError",
    "constrain_deployments_for_routing",
    "deployment_routing_constraint_from_decision",
    "fallback_execution_policy_from_decision",
]
