from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.api.routers import admin
from agent_hub.capabilities.defaults import build_runtime_capability_stack
from agent_hub.config.service import ConfigService
from agent_hub.db.session import Database, build_database
from agent_hub.evolution_hooks import EvolutionExecutionIngestHook
from agent_hub.hermes import PersistentHermesRunAdvisor
from agent_hub.mcp.runtime import RuntimeMcpService
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.defaults import configured_runtime_registry
from agent_hub.security.secrets import SecretCipher, SecretService
from agent_hub.settings import Settings, get_settings

_LOGGER = logging.getLogger(__name__)


class WorkerRunService(Protocol):
    async def publish_pending(self, limit: int) -> int: ...

    async def execute(self, run_id: UUID) -> object: ...


async def _require_tool_approval_from_settings(
    get_settings: Callable[[], Awaitable[admin.SystemSettingsResponse]],
) -> bool:
    try:
        return (await get_settings()).require_approval_for_tools
    except Exception as error:  # noqa: BLE001 - tool approval must fail closed.
        _LOGGER.warning(
            "tool_approval_settings_unavailable error_type=%s",
            type(error).__name__,
        )
        return True


async def _tool_approval_mode_from_settings(
    get_settings: Callable[[], Awaitable[admin.SystemSettingsResponse]],
) -> str:
    try:
        mode = (await get_settings()).tool_approval_mode
    except Exception as error:  # noqa: BLE001 - approval review mode must fail closed.
        _LOGGER.warning(
            "tool_approval_mode_settings_unavailable error_type=%s",
            type(error).__name__,
        )
        return "ask"
    return mode if mode in {"ask", "auto_review"} else "ask"


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


@dataclass(frozen=True)
class WorkerResources:
    database: Database
    redis_client: Redis
    service: RunService
    queue: LocalRunQueue
    runtime_mcp_service: RuntimeMcpService


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
        try:
            delivered = await service.publish_pending(batch_limit)
        except Exception as error:
            delivered = 0
            _LOGGER.exception(
                "run_worker_publish_pending_failed error_type=%s",
                type(error).__name__,
            )
        while not queue.empty() and not stop.is_set():
            run_id = await queue.get()
            try:
                await service.execute(run_id)
            except Exception as error:
                _LOGGER.exception(
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


def build_worker_service(
    settings: Settings,
) -> WorkerResources:
    database = build_database(settings.database_url_value())
    redis_client = Redis.from_url(settings.redis_url_value())
    queue = LocalRunQueue()
    config_service = ConfigService(database.session_factory)
    run_repository = RunRepository(database.session_factory)
    secret_service = SecretService(
        database.session_factory,
        SecretCipher(settings.master_key_bytes()),
    )
    admin_service = admin.PersistentAdminResourceService(
        config_service=config_service,
        secret_service=secret_service,
        tenant_id=settings.bootstrap_tenant_id,
        actor_id=settings.bootstrap_tenant_id,
        run_repository=run_repository,
        session_factory=database.session_factory,
        skill_store_dir=settings.skill_store_dir,
        generated_artifact_dir=settings.generated_artifact_dir,
    )
    runtime_mcp_service = RuntimeMcpService(
        tenant_id=settings.bootstrap_tenant_id,
        admin_service=admin_service,
        run_repository=run_repository,
    )
    runtime_capability_stack = build_runtime_capability_stack(
        tenant_id=settings.bootstrap_tenant_id,
        run_repository=run_repository,
        skill_store_dir=settings.skill_store_dir,
        workspace_root=settings.attachment_store_dir,
        generated_artifact_dir=settings.generated_artifact_dir,
        project_workspace_dir=settings.project_workspace_dir,
        require_approval_for_tools=lambda _tenant_id: _require_tool_approval_from_settings(
            admin_service.get_settings
        ),
        tool_approval_mode=lambda _tenant_id: _tool_approval_mode_from_settings(
            admin_service.get_settings
        ),
        tool_registry=runtime_mcp_service.capability_manifest_source(),
    )
    service = RunService(
        run_repository,
        runtime_registry=configured_runtime_registry(
            config_service=config_service,
            secret_service=secret_service,
            redis_client=redis_client,
            capability_gateway=runtime_capability_stack.runtime_gateway,
            harness_tool_gateway=runtime_capability_stack.harness_tool_gateway,
        ),
        router=None,
        task_queue=queue,
        hermes_advisor=PersistentHermesRunAdvisor(database.session_factory),
        runtime_timeout_seconds=settings.runtime_timeout_seconds,
        runtime_token_budget=settings.runtime_token_budget,
        terminal_run_hooks=_evolution_terminal_hooks(
            config_service=config_service,
            secret_service=secret_service,
            tenant_id=settings.bootstrap_tenant_id,
            run_repository=run_repository,
            session_factory=database.session_factory,
            skill_store_dir=settings.skill_store_dir,
        ),
    )
    return WorkerResources(
        database=database,
        redis_client=redis_client,
        service=service,
        queue=queue,
        runtime_mcp_service=runtime_mcp_service,
    )


def _evolution_terminal_hooks(
    *,
    config_service: ConfigService,
    secret_service: SecretService,
    tenant_id: UUID,
    run_repository: RunRepository,
    session_factory: async_sessionmaker[AsyncSession],
    skill_store_dir: Path,
) -> tuple[EvolutionExecutionIngestHook, ...]:
    evolution_service = admin.PersistentAdminResourceService(
        config_service=config_service,
        secret_service=secret_service,
        tenant_id=tenant_id,
        actor_id=tenant_id,
        run_repository=run_repository,
        session_factory=session_factory,
        skill_store_dir=skill_store_dir,
    )
    return (EvolutionExecutionIngestHook(evolution_service),)


async def _run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass

    resources = build_worker_service(get_settings())
    try:
        await resources.runtime_mcp_service.start()
        await run_worker_loop(resources.service, resources.queue, stop=stop)
    finally:
        await resources.redis_client.aclose()
        await resources.database.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
