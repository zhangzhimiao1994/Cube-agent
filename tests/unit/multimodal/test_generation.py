from decimal import Decimal

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelCapability, ModelRequest, ModelResponse
from agent_hub.multimodal.generation import MultimediaGenerationExecutor, MultimediaGenerationKind


class GatewayStub:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        return GatewayCompletion(
            response=ModelResponse(text="artifact://generated-video"),
            deployment_id="video-primary-1",
            logical_model=request.logical_model,
            provider_id="minimax",
            provider_model="minimax/MiniMax-Hailuo-02",
            cost_usd=Decimal("0.010000"),
        )


async def test_video_generation_request_requires_video_capability() -> None:
    gateway = GatewayStub()
    executor = MultimediaGenerationExecutor(gateway)

    result = await executor.generate(
        kind=MultimediaGenerationKind.VIDEO,
        logical_model="video-primary",
        prompt="generate a short product video",
    )

    assert result.text == "artifact://generated-video"
    assert gateway.requests[0].logical_model == "video-primary"
    assert gateway.requests[0].required_capabilities == frozenset({
        ModelCapability.VIDEO_GENERATION
    })


async def test_generation_prompt_is_required() -> None:
    executor = MultimediaGenerationExecutor(GatewayStub())

    with pytest.raises(ValueError, match="prompt"):
        await executor.generate(
            kind=MultimediaGenerationKind.IMAGE,
            logical_model="image-primary",
            prompt=" ",
        )
