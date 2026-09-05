from uuid import UUID

from agent_hub.config.schema import (
    AgentDefinition,
    DeploymentDefinition,
    LogicalModelDefinition,
    PlatformConfig,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.config import harness_scheduler_from_config, provider_profiles_from_config
from agent_hub.harness.types import HarnessPolicy, HarnessTaskRequirements

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


def config_with_models() -> PlatformConfig:
    return PlatformConfig(
        models={
            "main": LogicalModelDefinition(
                deployments=[
                    DeploymentDefinition(
                        provider="deepseek",
                        model="deepseek-chat",
                        credential_ref="deepseek-key",
                        quota_scope_id="deepseek-account",
                        max_concurrency=8,
                        capabilities={"text", "tool_calling", "structured_output"},
                    ),
                    DeploymentDefinition(
                        provider="openai",
                        model="gpt-5",
                        credential_ref="openai-key",
                        quota_scope_id="openai-account",
                        max_concurrency=4,
                        capabilities={"text", "tool_calling", "structured_output", "vision"},
                    ),
                ]
            ),
            "review": LogicalModelDefinition(
                deployments=[
                    DeploymentDefinition(
                        provider="anthropic",
                        model="claude-sonnet-4",
                        credential_ref="anthropic-key",
                        quota_scope_id="anthropic-account",
                        capabilities={"text"},
                    )
                ]
            ),
            "local_fast": LogicalModelDefinition(
                deployments=[
                    DeploymentDefinition(
                        provider="local",
                        model="private-coder",
                        api_base="http://localhost:8001/v1",
                        credential_ref="local-key",
                        quota_scope_id="local-account",
                        capabilities={"text", "tool_calling"},
                    )
                ]
            ),
            "video": LogicalModelDefinition(
                deployments=[
                    DeploymentDefinition(
                        provider="minimax",
                        model="hailuo-video",
                        credential_ref="minimax-key",
                        quota_scope_id="minimax-account",
                        capabilities={"text", "video_generation"},
                    )
                ]
            ),
        },
        agents=[
            AgentDefinition(
                id="main-agent",
                role="Main Agent",
                prompt="Help the user complete coding tasks.",
                model="main",
            )
        ],
    )


def test_provider_profiles_are_derived_only_from_real_configured_deployments() -> None:
    profiles = provider_profiles_from_config(config_with_models())

    providers = {profile.provider for profile in profiles}
    assert providers == {"anthropic", "deepseek", "local", "minimax", "openai"}
    assert "qwen" not in providers
    deepseek = next(profile for profile in profiles if profile.provider == "deepseek")
    assert deepseek.logical_model == "main"
    assert deepseek.model == "deepseek-chat"
    assert deepseek.supports_reasoning_delta is True
    assert deepseek.supports_prefix_cache is True
    openai = next(profile for profile in profiles if profile.provider == "openai")
    assert openai.supports_long_running_tasks is True
    assert openai.supports_parallel_tool_calls is True
    assert "workspace_write" in openai.sandbox_modes
    assert "workspace_write" not in deepseek.sandbox_modes


def test_multimodal_generation_profile_does_not_gain_reasoning_strengths() -> None:
    profiles = provider_profiles_from_config(config_with_models())
    video = next(profile for profile in profiles if profile.provider == "minimax")

    assert "video_generation" in video.capabilities
    assert video.supports_reasoning_delta is False
    assert video.supports_streamed_tool_call_delta is False
    assert video.supports_long_running_tasks is False


def test_scheduler_factory_returns_none_without_usable_text_models() -> None:
    config = PlatformConfig(
        models={
            "video": LogicalModelDefinition(
                deployments=[
                    DeploymentDefinition(
                        provider="minimax",
                        model="hailuo-video",
                        credential_ref="minimax-key",
                        quota_scope_id="minimax-account",
                        capabilities={"video_generation"},
                    )
                ]
            )
        },
        agents=[],
    )

    assert harness_scheduler_from_config(config) is None


def test_scheduler_factory_uses_generated_profiles_for_selection() -> None:
    scheduler = harness_scheduler_from_config(config_with_models())

    assert scheduler is not None
    decision = scheduler.select(
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        requirements=HarnessTaskRequirements(
            required_capabilities=frozenset({"text", "tool_calling"}),
            needs_reasoning=True,
            needs_streamed_tool_calls=True,
            prefers_prefix_cache=True,
        ),
        policy=HarnessPolicy(allowed_providers=frozenset({"deepseek", "openai", "local"})),
        hermes_hint=None,
    )

    assert decision.selected_provider == "deepseek"
    assert decision.selected_logical_model == "main"
