from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelCapability, ModelMessage, ModelRequest


class MultimediaGenerationKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


@dataclass(frozen=True, slots=True)
class MultimediaGenerationResult:
    kind: MultimediaGenerationKind
    logical_model: str
    deployment_id: str
    text: str | None


class MultimediaGenerationGateway(Protocol):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion: ...


class MultimediaGenerationExecutor:
    """Controlled generation entry point with capability checks at model dispatch."""

    def __init__(self, gateway: MultimediaGenerationGateway) -> None:
        self._gateway = gateway

    async def generate(
        self,
        *,
        kind: MultimediaGenerationKind,
        logical_model: str,
        prompt: str,
    ) -> MultimediaGenerationResult:
        if type(kind) is not MultimediaGenerationKind:
            raise ValueError("generation kind is invalid")
        if type(prompt) is not str or not prompt.strip():
            raise ValueError("generation prompt must be nonblank")
        request = ModelRequest(
            logical_model=logical_model,
            messages=(ModelMessage(role="user", content=prompt.strip()),),
            required_capabilities=frozenset({_required_capability(kind)}),
            max_output_tokens=4096,
        )
        completion = await self._gateway.complete_with_context(request)
        return MultimediaGenerationResult(
            kind=kind,
            logical_model=completion.logical_model,
            deployment_id=completion.deployment_id,
            text=completion.response.text,
        )


def _required_capability(kind: MultimediaGenerationKind) -> ModelCapability:
    if kind is MultimediaGenerationKind.IMAGE:
        return ModelCapability.IMAGE_GENERATION
    if kind is MultimediaGenerationKind.VIDEO:
        return ModelCapability.VIDEO_GENERATION
    raise ValueError("generation kind is invalid")


__all__ = [
    "MultimediaGenerationExecutor",
    "MultimediaGenerationGateway",
    "MultimediaGenerationKind",
    "MultimediaGenerationResult",
]
