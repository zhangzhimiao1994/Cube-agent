"""Planning and execution share the schema's actual deployment context window."""

from dataclasses import replace
from decimal import Decimal

import pytest

from agent_hub.config.schema import DeploymentDefinition, LogicalModelDefinition, PlatformConfig
from agent_hub.harness.config import provider_profiles_from_config
from agent_hub.harness.types import HarnessTaskRequirements, assess_context_window


def deployment(provider: str, model: str, window: int | None = None) -> DeploymentDefinition:
    return DeploymentDefinition(
        provider=provider, model=model, credential_ref="model-key",
        quota_scope_id="shared-account", context_window_tokens=window,
        max_concurrency=4, rpm=100, tpm=100000,
        input_per_million_usd=Decimal("0.1"), output_per_million_usd=Decimal("0.2"),
        capabilities={"text", "tool_calling", "structured_output"},
    )


def config(*deployments: DeploymentDefinition) -> PlatformConfig:
    return PlatformConfig(models={"main": LogicalModelDefinition(deployments=list(deployments))},
                          agents=[])


@pytest.mark.parametrize(("provider", "model"), [
    ("deepseek", "deepseek-chat"), ("openai", "gpt-5-codex"),
    ("anthropic", "claude-sonnet-4"), ("qwen", "qwen-coder"),
    ("local", "kimi-coder"), ("custom", "unknown-model"),
])
@pytest.mark.parametrize("window", [None, 32768, 64000, 196608])
def test_profile_context_matches_runtime_deployment_not_model_name(
    provider: str, model: str, window: int | None,
) -> None:
    definition = deployment(provider, model, window)
    runtime = definition.to_deployment(deployment_id="main_1", logical_model="main")
    profile = provider_profiles_from_config(config(definition))[0]
    assert profile.max_context_tokens == runtime.context_window_tokens
    if window is not None:
        assert profile.max_context_tokens == window


def test_different_deployments_keep_their_own_authoritative_windows() -> None:
    definitions = (deployment("deepseek", "deepseek-chat"),
                   deployment("qwen", "qwen-coder", 98304),
                   deployment("openai", "gpt-5", 32768))
    profiles = provider_profiles_from_config(config(*definitions))
    assert len(profiles) == len(definitions)
    for index, (definition, profile) in enumerate(zip(definitions, profiles, strict=True), 1):
        runtime = definition.to_deployment(deployment_id=f"main_{index}", logical_model="main")
        assert profile.max_context_tokens == runtime.context_window_tokens
        assert profile.provider == definition.provider
        assert profile.model == definition.model


def test_only_context_changes_when_explicit_window_changes() -> None:
    definition = deployment("deepseek", "deepseek-chat")
    original = config(definition)
    snapshot = original.model_dump(mode="json")
    before = provider_profiles_from_config(original)[0]
    updated = definition.model_copy(update={"context_window_tokens": 24576})
    after = provider_profiles_from_config(config(updated))[0]
    assert after == replace(before, max_context_tokens=24576)
    assert original.model_dump(mode="json") == snapshot
    assert before.supports_prefix_cache is True
    assert before.supports_reasoning_delta is True
    assert before.supports_parallel_tool_calls is False
    assert before.cost_latency_tier == "low"
    assert "workspace_write" in before.sandbox_modes


def test_default_planning_rejects_context_that_default_runtime_cannot_fit() -> None:
    definition = deployment("deepseek", "deepseek-chat")
    runtime = definition.to_deployment(deployment_id="main_1", logical_model="main")
    assert runtime.context_window_tokens is not None
    profile = provider_profiles_from_config(config(definition))[0]
    assessment = assess_context_window(profile, HarnessTaskRequirements(
        required_capabilities=frozenset({"text"}),
        estimated_input_tokens=runtime.context_window_tokens + 1,
    ))
    assert assessment.fits is False
    assert assessment.max_context_tokens == runtime.context_window_tokens


def test_messages_endpoint_capability_filter_is_unchanged() -> None:
    definition = deployment("anthropic", "claude-sonnet-4", 24576).model_copy(update={
        "api_base": "https://model.invalid/v1/messages",
    })
    profile = provider_profiles_from_config(config(definition))[0]
    assert profile.max_context_tokens == 24576
    assert profile.capabilities == frozenset({"text", "structured_output"})
    assert profile.supports_parallel_tool_calls is False
    assert profile.supports_streamed_tool_call_delta is False
    assert profile.sandbox_modes == ("none",)
