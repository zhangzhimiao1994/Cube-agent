"""Internal checkpoint handoff; only receiver acknowledgement releases a producer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from agent_hub.runtime.contracts import RuntimeCheckpoint


@dataclass(frozen=True, slots=True)
class PendingCheckpoint:
    build: Callable[[int], RuntimeCheckpoint]
    future: asyncio.Future[None]

    @property
    def cancelled(self) -> bool:
        return self.future.cancelled()

    def acknowledge(self) -> bool:
        if self.future.done():
            return False
        self.future.set_result(None)
        return True


class CheckpointPublications:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[PendingCheckpoint | None] = asyncio.Queue()
        self._futures: set[asyncio.Future[None]] = set()
        self._closed = False

    async def publish(self, build: Callable[[int], RuntimeCheckpoint]) -> None:
        if self._closed:
            raise asyncio.CancelledError
        future = asyncio.get_running_loop().create_future()
        self._futures.add(future)
        self._queue.put_nowait(PendingCheckpoint(build, future))
        try:
            await future
        finally:
            self._futures.discard(future)

    async def next(self) -> PendingCheckpoint:
        while True:
            if self._closed:
                raise asyncio.CancelledError
            pending = await self._queue.get()
            if pending is None:
                # Pass the close signal to any other receiver already waiting.
                self._queue.put_nowait(None)
                raise asyncio.CancelledError
            if not pending.future.done():
                return pending

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for future in self._futures:
            future.cancel()
        self._futures.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)
