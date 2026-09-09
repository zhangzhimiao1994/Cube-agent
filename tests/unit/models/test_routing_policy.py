from __future__ import annotations

import pytest

from agent_hub.config.schema import PlatformConfig
from agent_hub.models.routing_policy import (
    DeploymentRoutingConstraint,
    DeploymentRoutingConstraintError,
    constrain_deployments_for_routing,
    deployment_routing_constraint_from_decision,
)
from agent_hub.models.types import Deployment


def platform_config() -> PlatformConfig:
    return PlatformConfig.model_validate(
        {
            "models": {
                "main": {
                    "deployments": [
                        {
                            "provider": "openai",
                            "model": "gpt-5.6-sol",
                            "credential_ref": "secret://openai",
                            "quota_scope_id": "openai_account",
                        },
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "credential_ref": "secret://deepseek",
                            "quota_scope_id": "deepseek_account",
                        },
                    ]
                },
                "creative": {
                    "deployments": [
                        {
                            "provider": "kimi",
                            "model": "kimi-k2-latest",
                            "credential_ref": "secret://kimi",
                            "quota_scope_id": "kimi_account",
                        }
                    ]
                },
            },
            "agents": [],
        }
    )


def deployments() -> tuple[Deployment, ...]:
    return (
        Deployment(
            id="main_1",
            logical_model="main",
            provider_model="openai/gpt-5.6-sol",
            request_model="gpt-5.6-sol",
        ),
        Deployment(
            id="main_2",
            logical_model="main",
            provider_model="deepseek/deepseek-chat",
            request_model="deepseek-chat",
        ),
        Deployment(
            id="creative_1",
            logical_model="creative",
            provider_model="kimi/kimi-k2-latest",
            request_model="kimi-k2-latest",
        ),
    )


def test_deployment_routing_constraint_parses_harness_decision() -> None:
    constraint = deployment_routing_constraint_from_decision(
        platform_config(),
        {
            "harness_decision": {
                "selected_logical_model": "main",
                "selected_provider": "DeepSeek",
                "selected_model": "deepseek-chat",
            }
        },
    )

    assert constraint is not None
    assert constraint.logical_model == "main"
    assert constraint.provider == "deepseek"
    assert constraint.model == "deepseek-chat"


def test_deployment_routing_constraint_normalizes_provider_when_constructed_directly() -> None:
    constraint = DeploymentRoutingConstraint(
        logical_model="main",
        provider="DeepSeek",
        model="deepseek-chat",
    )

    assert constraint.provider == "deepseek"
    assert constraint.matches(deployments()[1])


def test_deployment_routing_constraint_ignores_incomplete_decision() -> None:
    assert (
        deployment_routing_constraint_from_decision(
            platform_config(),
            {"harness_decision": {"selected_provider": "deepseek"}},
        )
        is None
    )


def test_constrain_deployments_keeps_selected_provider_and_other_logical_models() -> None:
    constraint = deployment_routing_constraint_from_decision(
        platform_config(),
        {
            "harness_decision": {
                "selected_logical_model": "main",
                "selected_provider": "deepseek",
                "selected_model": "deepseek-chat",
            }
        },
    )

    constrained = constrain_deployments_for_routing(deployments(), constraint)

    assert tuple(deployment.provider_model for deployment in constrained) == (
        "deepseek/deepseek-chat",
        "kimi/kimi-k2-latest",
    )


def test_constrain_deployments_matches_full_provider_model_name() -> None:
    constraint = deployment_routing_constraint_from_decision(
        platform_config(),
        {
            "harness_decision": {
                "selected_logical_model": "main",
                "selected_provider": "DeepSeek",
                "selected_model": "DeepSeek/deepseek-chat",
            }
        },
    )

    constrained = constrain_deployments_for_routing(deployments(), constraint)

    assert tuple(deployment.id for deployment in constrained) == ("main_2", "creative_1")


def test_deployment_routing_constraint_fails_closed_for_unknown_logical_model() -> None:
    with pytest.raises(
        DeploymentRoutingConstraintError,
        match="harness logical model is unavailable",
    ):
        deployment_routing_constraint_from_decision(
            platform_config(),
            {
                "harness_decision": {
                    "selected_logical_model": "missing",
                    "selected_provider": "deepseek",
                    "selected_model": "deepseek-chat",
                }
            },
        )


def test_constrain_deployments_fails_closed_when_selected_deployment_is_missing() -> None:
    constraint = deployment_routing_constraint_from_decision(
        platform_config(),
        {
            "harness_decision": {
                "selected_logical_model": "main",
                "selected_provider": "deepseek",
                "selected_model": "missing-model",
            }
        },
    )

    with pytest.raises(
        DeploymentRoutingConstraintError,
        match="harness model selection is unavailable",
    ):
        constrain_deployments_for_routing(deployments(), constraint)
