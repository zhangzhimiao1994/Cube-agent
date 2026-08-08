from __future__ import annotations

import asyncio
import logging
import signal
from typing import Protocol
from uuid import UUID

from agent_hub.db.session import Database, build_database
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.defaults import default_runtime_registry
from agent_hub.settings import Settings, get_settings

_LOGGER = logging.getLogger(__name__)


class WorkerRunService(Protocol):
    async def publish_pending(self, limit: int = 100) -> int: ...

    async def execute(self, run_id: UUID) -> object: ...


class LocalRunQueue:
    """Process-local execution queue fed by the durable run outbox."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[UUID] = asyncio.Queue()
        self._queued: set[UUID] = set()

    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        del idempotency_key
        if run_id in self._queued:
            return
        self._queued.add(run_id)
        await self._queue.put(run_id)

    async def get(self) -> UUID:
        return await self._queue.get()

    def task_done(self, run_id: UUID) -> None:
        self._queued.discard(run_id)
        self._queue.task_done()

    def empty(self) -> bool:
        return self._queue.empty()


async def run_worker_loop(
    service: WorkerRunService,
    queue: LocalRunQueue,
    *,
    stop: asyncio.Event,
    poll_interval_seconds: float = 1.0,
    batch_limit: int = 100,
    max_idle_polls: int | None = None,
) -> None:
    idle_polls = 0
    while not stop.is_set():
        delivered = await service.publish_pending(batch_limit)
        while not queue.empty() and not stop.is_set():
            run_id = await queue.get()
            try:
                await service.execute(run_id)
            except Exception as error:  # noqa: BLE001 - keep worker alive after one bad run.
                _LOGGER.error(
                    "run_worker_execute_failed run_id=%s error_type=%s",
                    run_id,
                    type(error).__name__,
                )
            finally:
                queue.task_done(run_id)

        if delivered:
            idle_polls = 0
            continue
        idle_polls += 1
        if max_idle_polls is not None and idle_polls >= max_idle_polls:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval_seconds)
        except TimeoutError:
            pass


def build_worker_service(settings: Settings) -> tuple[Database, RunService, LocalRunQueue]:
    database = build_database(settings.database_url_value())
    queue = LocalRunQueue()
    service = RunService(
        RunRepository(database.session_factory),
        runtime_registry=default_runtime_registry(),
        router=None,
        task_queue=queue,
    )
    return database, service, queue


async def _run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass

    database, service, queue = build_worker_service(get_settings())
    try:
        await run_worker_loop(service, queue, stop=stop)
    finally:
        await database.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
