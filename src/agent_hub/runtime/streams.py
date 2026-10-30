"""Deterministic closure for single-execution runtime event streams."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from weakref import WeakSet

from agent_hub.runtime.contracts import RunEvent

_protected_owner: ContextVar[asyncio.Task[None] | None] = ContextVar(
    "runtime_cancel_protected_stream_owner", default=None
)
_worker_stream_owners: WeakSet[asyncio.Task[None]] = WeakSet()


@contextmanager
def _register_runtime_stream_owner() -> Iterator[None]:
    """Only the fresh worker owner registers itself, through awaited closure."""
    owner = asyncio.current_task()
    assert owner is not None
    _worker_stream_owners.add(owner)
    try:
        yield
    finally:
        _worker_stream_owners.discard(owner)


@contextmanager
def _protect_runtime_stream_owner(owner: asyncio.Task[None]) -> Iterator[None]:
    """A cancellation callback may stop work while its driver drains naturally."""
    token = _protected_owner.set(owner)
    try:
        yield
    finally:
        _protected_owner.reset(token)


def _is_protected_stream_owner(task: object) -> bool:
    # Heartbeats and user-stop callbacks run in independent Contexts. Protection
    # belongs to this exact live owner, never to its model/capability children.
    return task is not None and (
        task is _protected_owner.get()
        or isinstance(task, asyncio.Task)
        and task in _worker_stream_owners
    )


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
