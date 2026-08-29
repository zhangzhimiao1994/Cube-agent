from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from typing import Any, Protocol, cast

from agent_hub.harness.provider import (
    NormalizedProviderEvent,
    OpenAICompatibleStreamDecoder,
    ProviderStreamSession,
)
from agent_hub.models.types import Deployment, ModelRequest


class OpenAICompatibleChunkTransport(Protocol):
    def stream_openai_compatible_chunks(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> AsyncIterator[object]: ...


async def openai_compatible_stream_events(
    chunks: AsyncIterable[object],
    *,
    provider: str,
) -> AsyncIterator[NormalizedProviderEvent]:
    decoder = OpenAICompatibleStreamDecoder()
    session = ProviderStreamSession(provider=provider)
    try:
        async for chunk in chunks:
            for delta in decoder.decode(chunk):
                for event in session.feed(delta):
                    yield event
        for event in session.finish():
            yield event
    finally:
        aclose = getattr(chunks, "aclose", None)
        if callable(aclose):
            await cast(Any, chunks).aclose()


async def transport_openai_compatible_stream_events(
    transport: OpenAICompatibleChunkTransport,
    deployment: Deployment,
    request: ModelRequest,
    api_key: str,
    *,
    provider: str,
) -> AsyncIterator[NormalizedProviderEvent]:
    chunks = transport.stream_openai_compatible_chunks(deployment, request, api_key)
    try:
        async for event in openai_compatible_stream_events(chunks, provider=provider):
            yield event
    finally:
        aclose = getattr(chunks, "aclose", None)
        if callable(aclose):
            await cast(Any, chunks).aclose()


__all__ = [
    "OpenAICompatibleChunkTransport",
    "openai_compatible_stream_events",
    "transport_openai_compatible_stream_events",
]
