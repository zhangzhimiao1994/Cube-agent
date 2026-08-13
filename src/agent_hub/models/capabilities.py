"""Conservative model capability inference and normalization."""

from __future__ import annotations

import re
from collections.abc import Iterable

from agent_hub.models.types import ModelCapability

_KNOWN_IMAGE_GENERATION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bgpt-image\b",
        r"\bdall[-_ ]?e\b",
        r"\bimagen\b",
        r"\bqwen[-_ ]?image\b",
        r"\bflux\b",
        r"\bstable[-_ ]?diffusion\b",
        r"\bsd3\b",
        r"\bminimax[-_ ]?image\b",
    )
)

_KNOWN_VIDEO_GENERATION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bsora\b",
        r"\bveo\b",
        r"\bkling\b",
        r"\bhailuo\b",
        r"\bminimax[-_ ]?video\b",
        r"\bvideo[-_ ]?0?[1-9]\b",
        r"\bwan(?:2(?:\.\d+)?)?\b",
        r"\bjimeng\b",
        r"\bseedance\b",
    )
)

_KNOWN_AUDIO_GENERATION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bspeech[-_ ]?(?:0?2|2(?:\.\d+)?)\b",
        r"\btts\b",
        r"\btext[-_ ]?to[-_ ]?(?:speech|audio)\b",
        r"\bcosyvoice\b",
        r"\bqwen[-_ ]?tts\b",
        r"\bsambert\b",
        r"\beleven[-_ ]?(?:multilingual|flash|turbo)\b",
        r"\baudio[-_ ]?generation\b",
        r"\bmusic[-_ ]?generation\b",
    )
)


def infer_model_capabilities(
    *,
    provider: str,
    upstream_model: str,
    declared: Iterable[str | ModelCapability],
) -> tuple[ModelCapability, ...]:
    """Return admin-declared capabilities plus conservative known-model inference."""

    capabilities = {ModelCapability(item) for item in declared}
    if is_known_image_generation_model(provider, upstream_model):
        capabilities.add(ModelCapability.IMAGE_GENERATION)
    if is_known_video_generation_model(provider, upstream_model):
        capabilities.add(ModelCapability.VIDEO_GENERATION)
    if is_known_audio_generation_model(provider, upstream_model):
        capabilities.add(ModelCapability.AUDIO_GENERATION)
    return tuple(sorted(capabilities, key=lambda capability: capability.value))


def is_known_image_generation_model(provider: str, upstream_model: str) -> bool:
    return _matches_any(
        f"{provider}/{upstream_model}",
        _KNOWN_IMAGE_GENERATION_PATTERNS,
    )


def is_known_video_generation_model(provider: str, upstream_model: str) -> bool:
    return _matches_any(
        f"{provider}/{upstream_model}",
        _KNOWN_VIDEO_GENERATION_PATTERNS,
    )


def is_known_audio_generation_model(provider: str, upstream_model: str) -> bool:
    return _matches_any(
        f"{provider}/{upstream_model}",
        _KNOWN_AUDIO_GENERATION_PATTERNS,
    )


def _matches_any(value: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    return any(pattern.search(value) is not None for pattern in patterns)


__all__ = [
    "infer_model_capabilities",
    "is_known_audio_generation_model",
    "is_known_image_generation_model",
    "is_known_video_generation_model",
]
