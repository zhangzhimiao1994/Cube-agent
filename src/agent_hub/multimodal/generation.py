from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
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


class MultimediaDailyLimitExceeded(RuntimeError):
    """Raised before dispatch when a configured daily generation cap is exhausted."""


class MultimediaGenerationGateway(Protocol):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion: ...


class MultimediaGenerationExecutor:
    """Controlled generation entry point with capability checks at model dispatch."""

    def __init__(
        self,
        gateway: MultimediaGenerationGateway,
        *,
        daily_limit: int | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        if daily_limit is not None and (type(daily_limit) is not int or daily_limit <= 0):
            raise ValueError("daily_limit must be a positive integer or None")
        self._gateway = gateway
        self._daily_limit = daily_limit
        self._today = today
        self._usage_day: date | None = None
        self._daily_count = 0

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
        self._claim_daily_slot()
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

    def _claim_daily_slot(self) -> None:
        if self._daily_limit is None:
            return
        today = self._today()
        if self._usage_day != today:
            self._usage_day = today
            self._daily_count = 0
        if self._daily_count >= self._daily_limit:
            raise MultimediaDailyLimitExceeded("daily multimedia generation limit exceeded")
        self._daily_count += 1


def _required_capability(kind: MultimediaGenerationKind) -> ModelCapability:
    if kind is MultimediaGenerationKind.IMAGE:
        return ModelCapability.IMAGE_GENERATION
    if kind is MultimediaGenerationKind.VIDEO:
        return ModelCapability.VIDEO_GENERATION
    raise ValueError("generation kind is invalid")


__all__ = [
    "MultimediaDailyLimitExceeded",
    "MultimediaGenerationExecutor",
    "MultimediaGenerationGateway",
    "MultimediaGenerationKind",
    "MultimediaGenerationResult",
]
