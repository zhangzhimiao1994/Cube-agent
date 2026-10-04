"""Deterministic closure for single-execution runtime event streams."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agent_hub.runtime.contracts import RunEvent


@asynccontextmanager
async def closing_runtime_events(
    stream: AsyncIterator[RunEvent],
) -> AsyncIterator[AsyncIterator[RunEvent]]:
    try:
        yield stream
    finally:
        close = getattr(stream, "aclose", None)
        if callable(close):
            await close()
