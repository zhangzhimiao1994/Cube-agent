"""Deterministic closure for single-execution runtime event streams."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

from agent_hub.runtime.contracts import RunEvent

_protected_owner: ContextVar[asyncio.Task[None] | None] = ContextVar(
    "runtime_cancel_protected_stream_owner", default=None
)


@contextmanager
def _protect_runtime_stream_owner(owner: asyncio.Task[None]) -> Iterator[None]:
    """A cancellation callback may stop work while its driver drains naturally."""
    token = _protected_owner.set(owner)
    try:
        yield
    finally:
        _protected_owner.reset(token)


def _is_protected_stream_owner(task: object) -> bool:
    return task is not None and task is _protected_owner.get()


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
