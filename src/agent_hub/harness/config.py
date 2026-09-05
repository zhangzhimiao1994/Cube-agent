from __future__ import annotations

from agent_hub.config.schema import PlatformConfig
from agent_hub.harness.scheduler import CapabilityAwareHarnessScheduler
from agent_hub.harness.types import ProviderCapabilityProfile

_GENERATION_CAPABILITIES = frozenset(
    {"image_generation", "video_generation", "audio_generation"}
)


def provider_profiles_from_config(config: PlatformConfig) -> tuple[ProviderCapabilityProfile, ...]:
    profiles: list[ProviderCapabilityProfile] = []
    for logical_model, definition in sorted(config.models.items()):
        for deployment in definition.deployments:
            provider = deployment.provider.casefold()
            capabilities = frozenset(str(capability) for capability in deployment.capabilities)
            is_generation_only = bool(capabilities & _GENERATION_CAPABILITIES) and "tool_calling" not in capabilities
            profiles.append(
                ProviderCapabilityProfile(
                    provider=provider,
                    model=deployment.model,
                    logical_model=logical_model,
                    capabilities=capabilities,
                    supports_reasoning_delta=_supports_reasoning_delta(provider, deployment.model)
                    and not is_generation_only,
                    supports_streamed_tool_call_delta=_supports_tool_stream(provider)
                    and "tool_calling" in capabilities
                    and not is_generation_only,
                    supports_parallel_tool_calls=_supports_parallel_tools(provider)
                    and "tool_calling" in capabilities
                    and not is_generation_only,
                    supports_prefix_cache=_supports_prefix_cache(provider, deployment.model)
                    and not is_generation_only,
                    supports_long_running_tasks=_supports_long_running(provider)
                    and not is_generation_only,
                    max_context_tokens=_max_context_tokens(provider, deployment.model),
                    cost_latency_tier=_cost_latency_tier(provider),
                    sandbox_modes=_sandbox_modes(provider, capabilities),
                )
            )
    return tuple(profiles)


def harness_scheduler_from_config(config: PlatformConfig) -> CapabilityAwareHarnessScheduler | None:
    profiles = tuple(
        profile for profile in provider_profiles_from_config(config) if "text" in profile.capabilities
    )
    if not profiles:
        return None
    return CapabilityAwareHarnessScheduler(profiles)


def _supports_reasoning_delta(provider: str, model: str) -> bool:
    value = f"{provider}/{model}".casefold()
    return any(marker in value for marker in ("deepseek", "openai", "gpt", "claude", "qwen"))


def _supports_tool_stream(provider: str) -> bool:
    return provider in {"anthropic", "deepseek", "local", "openai", "qwen"}


def _supports_parallel_tools(provider: str) -> bool:
    return provider in {"anthropic", "openai", "qwen"}


def _supports_prefix_cache(provider: str, model: str) -> bool:
    value = f"{provider}/{model}".casefold()
    return "deepseek" in value or "claude" in value or "kimi" in value


def _supports_long_running(provider: str) -> bool:
    return provider in {"anthropic", "openai"}


def _max_context_tokens(provider: str, model: str) -> int:
    value = f"{provider}/{model}".casefold()
    if "gpt-5" in value or "codex" in value:
        return 400_000
    if "deepseek" in value:
        return 128_000
    if "claude" in value:
        return 200_000
    if "kimi" in value:
        return 128_000
    return 64_000


def _cost_latency_tier(provider: str) -> str:
    if provider in {"deepseek", "local"}:
        return "low"
    if provider in {"openai", "anthropic"}:
        return "premium"
    return "standard"


def _sandbox_modes(provider: str, capabilities: frozenset[str]) -> tuple[str, ...]:
    if "tool_calling" not in capabilities:
        return ("none",)
    if provider == "openai":
        return ("none", "read_only", "restricted", "workspace_write")
    return ("none", "read_only", "restricted")


__all__ = ["harness_scheduler_from_config", "provider_profiles_from_config"]
