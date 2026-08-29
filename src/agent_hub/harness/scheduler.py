from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import (
    HarnessDecision,
    HarnessPolicy,
    HarnessTaskRequirements,
    HermesContextHint,
    ProviderCapabilityProfile,
    assess_context_window,
)


class HarnessSchedulingError(RuntimeError):
    """Stable scheduling failure that does not expose provider internals."""


@dataclass(frozen=True, slots=True)
class _CandidateScore:
    profile: ProviderCapabilityProfile
    score: float
    capability_reasons: tuple[str, ...]
    policy_reasons: tuple[str, ...]
    context_reasons: tuple[str, ...]


class CapabilityAwareHarnessScheduler:
    """Select the execution provider from policy, capabilities, health, and context hints."""

    def __init__(self, profiles: tuple[ProviderCapabilityProfile, ...]) -> None:
        if not profiles:
            raise ValueError("at least one provider profile is required")
        if not all(isinstance(profile, ProviderCapabilityProfile) for profile in profiles):
            raise TypeError("profiles must be ProviderCapabilityProfile values")
        self._profiles = tuple(sorted(profiles, key=lambda item: (item.provider, item.model)))

    def select(
        self,
        *,
        tenant_id: UUID,
        mode: TaskMode,
        requirements: HarnessTaskRequirements,
        policy: HarnessPolicy,
        hermes_hint: HermesContextHint | None,
    ) -> HarnessDecision:
        if mode is TaskMode.AUTO:
            raise HarnessSchedulingError("harness mode must be executable")
        fallbacks: list[str] = []
        candidates: list[_CandidateScore] = []
        for profile in self._profiles:
            if profile.provider in policy.denied_providers:
                fallbacks.append(f"provider_blocked:{profile.provider}")
                continue
            if policy.allowed_providers and profile.provider not in policy.allowed_providers:
                fallbacks.append(f"provider_blocked:{profile.provider}")
                continue
            if (
                requirements.required_logical_model is not None
                and profile.logical_model != requirements.required_logical_model
            ):
                fallbacks.append(
                    f"logical_model_blocked:{profile.provider}:{profile.logical_model}"
                )
                continue
            missing = requirements.required_capabilities - profile.capabilities
            if missing:
                for capability in sorted(missing):
                    fallbacks.append(f"missing_capability:{profile.provider}:{capability}")
                continue
            context_window = assess_context_window(profile, requirements)
            if not context_window.fits:
                fallbacks.append(f"context_window_exceeded:{profile.provider}")
                continue
            candidates.append(
                self._score(
                    profile,
                    mode=mode,
                    requirements=requirements,
                    policy=policy,
                    hermes_hint=hermes_hint,
                    fallbacks=fallbacks,
                )
            )
        if not candidates:
            if any(item.startswith("context_window_exceeded:") for item in fallbacks):
                raise HarnessSchedulingError("no harness provider satisfies context window")
            raise HarnessSchedulingError("no harness provider satisfies policy and capabilities")
        selected = max(
            candidates,
            key=lambda item: (
                item.score,
                -_tier_rank(item.profile.cost_latency_tier),
                item.profile.provider,
                item.profile.model,
            ),
        )
        requires_approval = requirements.privacy_sensitive and policy.require_approval_for_sensitive
        policy_reasons = list(selected.policy_reasons)
        if requires_approval:
            policy_reasons.append("sensitive_task_requires_approval")
        return HarnessDecision(
            tenant_id=tenant_id,
            mode=mode.value,
            selected_provider=selected.profile.provider,
            selected_model=selected.profile.model,
            selected_logical_model=selected.profile.logical_model,
            requires_approval=requires_approval,
            capability_reasons=selected.capability_reasons,
            policy_reasons=tuple(policy_reasons),
            context_reasons=selected.context_reasons,
            fallbacks_considered=tuple(dict.fromkeys(fallbacks)),
        )

    def _score(
        self,
        profile: ProviderCapabilityProfile,
        *,
        mode: TaskMode,
        requirements: HarnessTaskRequirements,
        policy: HarnessPolicy,
        hermes_hint: HermesContextHint | None,
        fallbacks: list[str],
    ) -> _CandidateScore:
        del mode
        score = 0.0
        capability_reasons: list[str] = []
        policy_reasons = [f"provider_allowed:{profile.provider}"]
        context_reasons: list[str] = []
        if requirements.needs_reasoning and profile.supports_reasoning_delta:
            score += 4
            capability_reasons.append("supports_reasoning_delta")
        if requirements.needs_streamed_tool_calls and profile.supports_streamed_tool_call_delta:
            score += 4
            capability_reasons.append("supports_streamed_tool_call_delta")
        if requirements.needs_parallel_tool_calls and profile.supports_parallel_tool_calls:
            score += 2
            capability_reasons.append("supports_parallel_tool_calls")
        if requirements.needs_long_running and profile.supports_long_running_tasks:
            score += 3
            capability_reasons.append("supports_long_running_tasks")
        if requirements.requires_sandbox and "restricted" in profile.sandbox_modes:
            score += 2
            capability_reasons.append("sandbox_required")
        if requirements.prefers_prefix_cache and profile.supports_prefix_cache:
            score += 6
            capability_reasons.append("supports_prefix_cache")
        if requirements.estimated_input_tokens:
            context_window = assess_context_window(profile, requirements)
            if context_window.risk_level == "near_limit":
                fallbacks.append(f"context_window_near_limit:{profile.provider}")
                capability_reasons.append("context_window_near_limit")
            else:
                score += 1
                capability_reasons.append("context_window_fits")
        if policy.prefer_low_cost and profile.cost_latency_tier == "low":
            score += 1
            policy_reasons.append("low_cost_preferred")
        if hermes_hint is not None and hermes_hint.confidence >= 0.75:
            if hermes_hint.project_id or hermes_hint.conversation_id:
                score += 0.5
                context_reasons.append("hermes_context_match")
            if hermes_hint.preferred_provider == profile.provider:
                score += 2
                context_reasons.append(f"hermes_preferred_provider:{profile.provider}")
            elif (
                hermes_hint.preferred_provider is not None
                and (
                    profile.provider in policy.allowed_providers
                    or not policy.allowed_providers
                )
                and hermes_hint.preferred_provider not in {candidate.provider for candidate in self._profiles if _policy_allows(candidate, policy)}
            ):
                context_reasons.append(
                    f"hermes_preferred_provider_blocked:{hermes_hint.preferred_provider}"
                )
        if profile.runtime_health.degraded:
            score -= 20
            fallbacks.append(f"provider_health_degraded:{profile.provider}")
        return _CandidateScore(
            profile=profile,
            score=score,
            capability_reasons=tuple(dict.fromkeys(capability_reasons)),
            policy_reasons=tuple(dict.fromkeys(policy_reasons)),
            context_reasons=tuple(dict.fromkeys(context_reasons)),
        )


def _policy_allows(profile: ProviderCapabilityProfile, policy: HarnessPolicy) -> bool:
    if profile.provider in policy.denied_providers:
        return False
    return not policy.allowed_providers or profile.provider in policy.allowed_providers


def _tier_rank(value: str) -> int:
    return {"low": 0, "standard": 1, "premium": 2}.get(value, 1)
