from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from agent_hub.models.types import Deployment

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class GeneratedVideoArtifact:
    path: Path
    uri: str
    provider: str
    model: str
    task_id: str
    file_id: str | None
    mime_type: str
    kind: str = "video"


class VideoProviderGenerationError(RuntimeError):
    """Safe provider-level multimedia generation failure."""

    def __init__(self, message: str, *, provider_code: str | None = None) -> None:
        super().__init__(message)
        self.provider_code = provider_code


class TextToVideoProvider(Protocol):
    async def generate_text_to_video(
        self,
        *,
        api_key: str,
        api_base: str,
        model: str,
        prompt: str,
        output_dir: Path,
        duration: int,
        resolution: str,
    ) -> GeneratedVideoArtifact: ...


class TextToVideoProviderRouter:
    def __init__(self, providers: tuple[tuple[str, TextToVideoProvider], ...]) -> None:
        self._providers = providers

    def provider_for(self, deployment: Deployment) -> TextToVideoProvider | None:
        provider_name = deployment.provider_model.partition("/")[0].casefold()
        for marker, provider in self._providers:
            if marker.casefold() in provider_name:
                return provider
        return None


def media_filename_for_model(model: str, *, suffix: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(tz=UTC)).strftime("%Y%m%d-%H%M%S")
    model_name = _UNSAFE_FILENAME_CHARS.sub("_", model.strip()).strip("._-")
    if not model_name:
        model_name = "model"
    extension = suffix if suffix.startswith(".") else f".{suffix}"
    return f"{model_name}_{timestamp}{extension.lower()}"


def unique_media_path(output_dir: Path, filename: str) -> Path:
    root = output_dir.resolve()
    target = (output_dir / filename).resolve()
    if root not in target.parents:
        raise VideoProviderGenerationError("Media file target path is unsafe")
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 10_000):
        candidate = (output_dir / f"{stem}_{index}{suffix}").resolve()
        if root not in candidate.parents:
            raise VideoProviderGenerationError("Media file target path is unsafe")
        if not candidate.exists():
            return candidate
    raise VideoProviderGenerationError("Unable to allocate unique media filename")


__all__ = [
    "GeneratedVideoArtifact",
    "TextToVideoProvider",
    "TextToVideoProviderRouter",
    "VideoProviderGenerationError",
    "media_filename_for_model",
    "unique_media_path",
]
