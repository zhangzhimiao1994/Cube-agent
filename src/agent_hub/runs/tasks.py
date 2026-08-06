"""Queue task adapters for durable runs.

The project can register this adapter with Celery during worker bootstrap
without importing Celery from the domain or API layers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol, cast
from uuid import UUID


class ExecutableRunService(Protocol):
    async def execute(self, run_id: UUID) -> object: ...


class CeleryRunQueue:
    """Outbox publisher adapter that uses deterministic Celery task ids."""

    def __init__(self, celery_app: Any) -> None:
        self._celery_app = celery_app

    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        self._celery_app.send_task(
            "agent_hub.runs.execute",
            args=[str(run_id)],
            task_id=idempotency_key,
        )


def run_celery_task(service: ExecutableRunService, run_id: str) -> None:
    """Synchronous queue entry point used by Celery workers."""

    asyncio.run(service.execute(UUID(run_id)))


def register_celery_task(celery_app: Any, service: ExecutableRunService) -> Callable[..., Any]:
    """Register the run execution task on a Celery-compatible app object."""

    def execute_run_task(self: object, run_id: str) -> None:
        del self
        run_celery_task(service, run_id)

    task = celery_app.task(
        name="agent_hub.runs.execute",
        bind=True,
        acks_late=True,
        reject_on_worker_lost=True,
        autoretry_for=(Exception,),
        retry_backoff=True,
        retry_kwargs={"max_retries": 3},
    )(execute_run_task)
    return cast(Callable[..., Any], task)
