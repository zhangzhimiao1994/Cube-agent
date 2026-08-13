from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_hub.models.types import Deployment


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


__all__ = [
    "GeneratedVideoArtifact",
    "TextToVideoProvider",
    "TextToVideoProviderRouter",
    "VideoProviderGenerationError",
]
