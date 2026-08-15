"""Model trait inference for role routing.

Traits are routing hints, not hard request capabilities. Hard capabilities still come
from deployment configuration and gateway checks.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

DeploymentTraitsInput = tuple[str, str, Iterable[object]]

_CAPABILITY_TRAIT_ALIASES: dict[str, frozenset[str]] = {
    "text": frozenset({"text", "general"}),
    "tool_calling": frozenset({"tool_calling", "tool"}),
    "structured_output": frozenset({"structured_output", "structured"}),
    "vision": frozenset({"vision", "image", "multimodal"}),
    "audio": frozenset({"audio", "speech", "voice", "multimodal"}),
    "image_generation": frozenset({"image_generation", "image_output", "generation"}),
    "video_generation": frozenset({"video_generation", "video_output", "generation"}),
    "audio_generation": frozenset({"audio_generation", "speech_output", "generation"}),
}


def _rule(
    patterns: Iterable[str],
    traits: Iterable[str],
) -> tuple[tuple[re.Pattern[str], ...], frozenset[str]]:
    return (
        tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns),
        frozenset(traits),
    )


_PROFILE_RULES: tuple[tuple[tuple[re.Pattern[str], ...], frozenset[str]], ...] = (
    _rule(
        (r"\b(?:google|gemini)\b",),
        {
            "general",
            "reasoning",
            "analysis",
            "code",
            "structured",
            "vision",
            "multimodal",
            "long_context",
        },
    ),
    _rule(
        (r"\b(?:openai|gpt[-_ ]?(?:4o|4\.1|5)|o[1345](?:[-_ ]|$))",),
        {"general", "reasoning", "analysis", "writing", "code", "structured", "tool"},
    ),
    _rule(
        (r"\b(?:gpt[-_ ]?4o|gpt[-_ ]?5)\b",),
        {"vision", "audio", "multimodal"},
    ),
    _rule(
        (r"\b(?:anthropic|claude|sonnet|opus|haiku)\b",),
        {"reasoning", "analysis", "review", "writing", "code", "synthesis", "long_context"},
    ),
    _rule(
        (r"\b(?:qwen|通义|dashscope|bailian|百炼)\b",),
        {"chinese", "code", "tool", "structured", "analysis", "general"},
    ),
    _rule(
        (r"\b(?:qwen[-_ ]?vl|qvq|omni)\b",),
        {"vision", "multimodal"},
    ),
    _rule(
        (r"\b(?:qwen[-_ ]?audio|qwen[-_ ]?omni|omni)\b",),
        {"audio", "speech", "voice", "multimodal"},
    ),
    _rule(
        (r"\b(?:glm|zhipu|智谱)\b",),
        {"chinese", "reasoning", "analysis", "structured", "general"},
    ),
    _rule(
        (r"\b(?:glm[-_ ]?4v|glm[-_ ]?v|vision)\b",),
        {"vision", "multimodal"},
    ),
    _rule(
        (r"\b(?:analyst|analysis[-_ ]?model)\b",),
        {"analysis", "reasoning", "structured"},
    ),
    _rule(
        (r"\bdeepseek\b",),
        {"reasoning", "analysis", "code", "general"},
    ),
    _rule(
        (r"\b(?:minimax|abab|m3)\b",),
        {"chinese", "creative", "writing", "general"},
    ),
    _rule(
        (r"\b(?:kimi|moonshot)\b",),
        {"creative", "writing", "analysis", "long_context", "chinese", "general"},
    ),
    _rule(
        (r"\b(?:xai|grok)\b",),
        {"general", "reasoning", "analysis", "realtime", "fast"},
    ),
    _rule(
        (r"\b(?:mistral|mixtral|codestral)\b",),
        {"general", "code", "fast", "multilingual", "structured"},
    ),
    _rule(
        (r"\b(?:llama|meta[-_ ]?ai)\b",),
        {"general", "open_weights", "fast"},
    ),
    _rule(
        (r"\b(?:llama.*(?:code|coder)|code[-_ ]?llama)\b",),
        {"code"},
    ),
    _rule(
        (r"\b(?:command[-_ ]?r|cohere)\b",),
        {"retrieval", "tool", "structured", "general"},
    ),
    _rule(
        (r"\bperplexity\b",),
        {"web_research", "realtime", "analysis", "general"},
    ),
)


def infer_model_traits(
    *,
    logical_model: str,
    deployments: Iterable[DeploymentTraitsInput],
) -> frozenset[str]:
    """Infer routing traits from logical name, deployments, and declared capabilities."""

    traits: set[str] = {"text", "general"}
    haystack_parts = [logical_model]
    for provider, model, capabilities in deployments:
        haystack_parts.extend((provider, model))
        for capability in capabilities:
            value = str(capability).lower()
            traits.add(value)
            traits.update(_CAPABILITY_TRAIT_ALIASES.get(value, ()))
    haystack = " ".join(haystack_parts).lower()
    for patterns, profile_traits in _PROFILE_RULES:
        if any(pattern.search(haystack) is not None for pattern in patterns):
            traits.update(profile_traits)
    return frozenset(traits)


__all__ = ["infer_model_traits"]