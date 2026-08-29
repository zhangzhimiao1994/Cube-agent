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
    primary_error: BaseException | None = None
    try:
        async for chunk in chunks:
            for delta in decoder.decode(chunk):
                for event in session.feed(delta):
                    yield event
        for event in session.finish():
            yield event
    except BaseException as error:
        primary_error = error
        raise
    finally:
        close_error = await _close_async_iterable(chunks)
        if close_error is not None and primary_error is None:
            raise close_error


async def transport_openai_compatible_stream_events(
    transport: OpenAICompatibleChunkTransport,
    deployment: Deployment,
    request: ModelRequest,
    api_key: str,
    *,
    provider: str,
) -> AsyncIterator[NormalizedProviderEvent]:
    chunks = transport.stream_openai_compatible_chunks(deployment, request, api_key)
    events = openai_compatible_stream_events(chunks, provider=provider)
    primary_error: BaseException | None = None
    try:
        while True:
            try:
                yield await anext(events)
            except StopAsyncIteration:
                return
    except BaseException as error:
        primary_error = error
        raise
    finally:
        close_error = await _close_async_iterable(events)
        if close_error is not None and primary_error is None:
            raise close_error


async def _close_async_iterable(stream: object) -> BaseException | None:
    aclose = getattr(stream, "aclose", None)
    if not callable(aclose):
        return None
    try:
        await cast(Any, stream).aclose()
    except BaseException as error:  # noqa: BLE001 - callers preserve primary failures
        return error
    return None


__all__ = [
    "OpenAICompatibleChunkTransport",
    "openai_compatible_stream_events",
    "transport_openai_compatible_stream_events",
]
