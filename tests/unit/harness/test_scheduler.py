from uuid import UUID

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.harness.scheduler import CapabilityAwareHarnessScheduler, HarnessSchedulingError
from agent_hub.harness.types import (
    HarnessPolicy,
    HarnessTaskRequirements,
    HermesContextHint,
    ProviderCapabilityProfile,
    RuntimeHealth,
)

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


def test_scheduler_prefers_deepseek_for_reasoning_tool_tasks_with_prefix_cache() -> None:
    scheduler = CapabilityAwareHarnessScheduler(
        profiles=(
            ProviderCapabilityProfile.deepseek("deepseek-chat", logical_model="main"),
            ProviderCapabilityProfile.openai_codex("gpt-5", logical_model="main"),
        )
    )

    decision = scheduler.select(
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        requirements=HarnessTaskRequirements(
            required_capabilities=frozenset({"text", "tool_calling"}),
            needs_reasoning=True,
            needs_streamed_tool_calls=True,
            estimated_input_tokens=96_000,
            prefers_prefix_cache=True,
        ),
        policy=HarnessPolicy(allowed_providers=frozenset({"deepseek", "openai"})),
        hermes_hint=HermesContextHint(project_id="cube-agent", confidence=0.82),
    )

    assert decision.selected_provider == "deepseek"
    assert decision.selected_model == "deepseek-chat"
    assert decision.selected_logical_model == "main"
    assert "supports_prefix_cache" in decision.capability_reasons
    assert "hermes_context_match" in decision.context_reasons
    assert decision.policy_reasons == ("provider_allowed:deepseek",)
    assert decision.requires_approval is False


def test_scheduler_never_lets_hermes_override_policy() -> None:
    scheduler = CapabilityAwareHarnessScheduler(
        profiles=(
            ProviderCapabilityProfile.deepseek("deepseek-chat", logical_model="main"),
            ProviderCapabilityProfile.openai_codex("gpt-5", logical_model="main"),
        )
    )

    decision = scheduler.select(
        tenant_id=TENANT_ID,
        mode=TaskMode.DIRECT,
        requirements=HarnessTaskRequirements(required_capabilities=frozenset({"text"})),
        policy=HarnessPolicy(allowed_providers=frozenset({"openai"})),
        hermes_hint=HermesContextHint(
            project_id="cube-agent",
            preferred_provider="deepseek",
            confidence=0.99,
        ),
    )

    assert decision.selected_provider == "openai"
    assert "provider_blocked:deepseek" in decision.fallbacks_considered
    assert "hermes_preferred_provider_blocked:deepseek" in decision.context_reasons


def test_scheduler_requires_approval_for_sensitive_tasks_even_when_provider_matches() -> None:
    scheduler = CapabilityAwareHarnessScheduler(
        profiles=(ProviderCapabilityProfile.openai_codex("gpt-5", logical_model="main"),)
    )

    decision = scheduler.select(
        tenant_id=TENANT_ID,
        mode=TaskMode.HYBRID,
        requirements=HarnessTaskRequirements(
            required_capabilities=frozenset({"text", "tool_calling"}),
            privacy_sensitive=True,
            requires_sandbox=True,
        ),
        policy=HarnessPolicy(require_approval_for_sensitive=True),
        hermes_hint=None,
    )

    assert decision.selected_provider == "openai"
    assert decision.requires_approval is True
    assert "sensitive_task_requires_approval" in decision.policy_reasons
    assert "sandbox_required" in decision.capability_reasons


def test_scheduler_rejects_profiles_that_cannot_satisfy_hard_capabilities() -> None:
    scheduler = CapabilityAwareHarnessScheduler(
        profiles=(ProviderCapabilityProfile.openai_codex("gpt-5", logical_model="main"),)
    )

    with pytest.raises(HarnessSchedulingError, match="no harness provider"):
        scheduler.select(
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            requirements=HarnessTaskRequirements(
                required_capabilities=frozenset({"video_generation"})
            ),
            policy=HarnessPolicy(),
            hermes_hint=None,
        )


def test_runtime_health_can_demote_an_otherwise_good_provider() -> None:
    scheduler = CapabilityAwareHarnessScheduler(
        profiles=(
            ProviderCapabilityProfile.deepseek(
                "deepseek-chat",
                logical_model="main",
                runtime_health=RuntimeHealth(
                    recent_error_rate=0.95,
                    queue_pressure=0.95,
                    rate_limited=True,
                ),
            ),
            ProviderCapabilityProfile.openai_codex("gpt-5", logical_model="main"),
        )
    )

    decision = scheduler.select(
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        requirements=HarnessTaskRequirements(
            required_capabilities=frozenset({"text", "tool_calling"}),
            needs_reasoning=True,
            needs_streamed_tool_calls=True,
            prefers_prefix_cache=True,
        ),
        policy=HarnessPolicy(allowed_providers=frozenset({"deepseek", "openai"})),
        hermes_hint=None,
    )

    assert decision.selected_provider == "openai"
    assert "provider_health_degraded:deepseek" in decision.fallbacks_considered
