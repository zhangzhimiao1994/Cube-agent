"""FastAPI application factory and owned process resources."""

import asyncio
import hashlib
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_hub.api.errors import (
    PublicAPIError,
    error_payload,
    http_exception_handler,
    public_error_handler,
)
from agent_hub.api.middleware import RequestBodyLimitMiddleware, SafeExceptionMiddleware
from agent_hub.api.routers import admin, auth, config, runs, system, users
from agent_hub.auth.passwords import PasswordService
from agent_hub.auth.rate_limit import RedisAuthRateLimiter
from agent_hub.auth.service import AuthService
from agent_hub.auth.tokens import AccessTokenService
from agent_hub.auth.user_admin import PersistentUserAdminService
from agent_hub.channels.dedup import InboundDedupRepository
from agent_hub.channels.feishu.webhook import (
    ChannelGatewayProtocol,
    create_lazy_feishu_webhook_router,
)
from agent_hub.channels.gateway import ChannelGateway
from agent_hub.channels.generic_webhook import create_generic_channel_webhook_router
from agent_hub.channels.submitter import RunServiceInboundSubmitter, RunSubmissionService
from agent_hub.config.service import ConfigService
from agent_hub.db.models import TenantRow
from agent_hub.db.session import build_database
from agent_hub.hermes import PersistentHermesRunAdvisor
from agent_hub.observability.logging import configure_logging
from agent_hub.observability.metrics import default_metrics_registry
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import ModeRouterProtocol, RunService, TaskQueue
from agent_hub.runs.temporary_agents import AdminResourceTemporaryAgentPolicy
from agent_hub.runtime.defaults import configured_runtime_registry
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.security.secrets import SecretCipher, SecretService
from agent_hub.settings import Settings, get_settings

ReadinessProbe = Callable[[], Awaitable[None]]
CleanupCallback = tuple[str, Callable[[], Awaitable[None]]]
_LOGGER = logging.getLogger(__name__)


class ResourceCleanupError(RuntimeError):
    """Report cleanup failure types without exposing resource error details."""

    def __init__(self, error_types: tuple[str, ...]) -> None:
        self.error_types = error_types
        super().__init__(f"resource cleanup failed: {', '.join(error_types)}")


class DatabaseResource(Protocol):
    session_factory: Any

    async def dispose(self) -> None: ...


class RedisResource(Protocol):
    async def aclose(self) -> None: ...

    def ping(self, **kwargs: Any) -> Any: ...


class InProcessRunQueue:
    """Minimal queue adapter for single-process tests and explicit publisher wiring."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[UUID, str]] = []

    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        self.enqueued.append((run_id, idempotency_key))


async def _cleanup_owned_resources(
    cleanup_callbacks: list[CleanupCallback],
    *,
    primary_error: BaseException | None,
) -> None:
    first_cancellation: asyncio.CancelledError | None = None
    cleanup_error_types: list[str] = []
    for resource_name, cleanup in reversed(cleanup_callbacks):
        try:
            await cleanup()
        except asyncio.CancelledError as error:
            if first_cancellation is None:
                first_cancellation = error
            _LOGGER.error(
                "resource_cleanup_failed resource=%s error_type=CancelledError",
                resource_name,
            )
        except Exception as error:  # noqa: BLE001 -- all ordinary cleanups are attempted.
            error_type = type(error).__name__
            cleanup_error_types.append(error_type)
            _LOGGER.error(
                "resource_cleanup_failed resource=%s error_type=%s",
                resource_name,
                error_type,
            )
    if primary_error is not None:
        return
    if first_cancellation is not None:
        raise first_cancellation
    if cleanup_error_types:
        raise ResourceCleanupError(tuple(cleanup_error_types)) from None


async def ensure_bootstrap_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    slug: str,
    name: str,
) -> None:
    """Idempotently ensure the configured tenant exists once at process startup."""

    statement = (
        insert(TenantRow)
        .values(id=tenant_id, slug=slug, name=name)
        .on_conflict_do_update(
            index_elements=[TenantRow.id],
            set_={"slug": slug, "name": name},
        )
    )
    async with session_factory() as session, session.begin():
        await session.execute(statement)


def create_app(
    *,
    settings: Settings | None = None,
    database: DatabaseResource | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    redis_client: RedisResource | None = None,
    database_probe: ReadinessProbe | None = None,
    redis_probe: ReadinessProbe | None = None,
    readiness_timeout_seconds: float = 1.0,
    auth_service: object | None = None,
    rate_limiter: object | None = None,
    config_service: object | None = None,
    admin_resource_service: object | None = None,
    user_admin_service: object | None = None,
    run_service: object | None = None,
    runtime_registry: RuntimeRegistry | None = None,
    mode_router: ModeRouterProtocol | None = None,
    task_queue: TaskQueue | None = None,
    feishu_gateway: ChannelGatewayProtocol | None = None,
    database_factory: Callable[[str], DatabaseResource] = build_database,
    redis_factory: Callable[[str], RedisResource] = Redis.from_url,
) -> FastAPI:
    """Create an application without opening network resources at import time."""

    configured_settings = settings or Settings.model_construct()
    active_runtime_registry = runtime_registry
    configure_logging(level=configured_settings.log_level)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        nonlocal active_runtime_registry
        configured = settings or get_settings()
        active_database = database
        active_redis = redis_client
        active_sessions = session_factory
        cleanup_callbacks: list[CleanupCallback] = []
        token_service = (
            AccessTokenService(configured.jwt_signing_key_value())
            if auth_service is None
            else None
        )
        try:
            application.state.trusted_proxy_ips = configured.trusted_proxy_ips
            application.state.bootstrap_tenant_id = configured.bootstrap_tenant_id
            needs_sessions = (
                auth_service is None
                or config_service is None
                or run_service is None
                or admin_resource_service is None
                or user_admin_service is None
            )
            if active_sessions is None and active_database is not None:
                active_sessions = active_database.session_factory
            if active_sessions is None and needs_sessions:
                active_database = database_factory(configured.database_url_value())
                cleanup_callbacks.append(("database", active_database.dispose))
                active_sessions = active_database.session_factory

            needs_redis = rate_limiter is None or redis_probe is None or (
                run_service is None and active_runtime_registry is None
            )
            if active_redis is None and needs_redis:
                active_redis = redis_factory(configured.redis_url_value())
                cleanup_callbacks.append(("redis", active_redis.aclose))

            if auth_service is None:
                assert token_service is not None
                assert active_sessions is not None
                await ensure_bootstrap_tenant(
                    active_sessions,
                    configured.bootstrap_tenant_id,
                    configured.bootstrap_tenant_slug,
                    configured.bootstrap_tenant_name,
                )
                application.state.auth_service = AuthService(
                    active_sessions,
                    configured.bootstrap_tenant_id,
                    PasswordService(),
                    token_service,
                )
            if config_service is None:
                assert active_sessions is not None
                application.state.config_service = ConfigService(active_sessions)
            active_secret_service = None
            if active_sessions is not None:
                active_secret_service = SecretService(
                    active_sessions,
                    SecretCipher(configured.master_key_bytes()),
                )
            if admin_resource_service is None and active_sessions is not None:
                assert active_secret_service is not None
                application.state.admin_resource_service = admin.PersistentAdminResourceService(
                    config_service=ConfigService(active_sessions),
                    secret_service=active_secret_service,
                    run_repository=RunRepository(active_sessions),
                    tenant_id=configured.bootstrap_tenant_id,
                    actor_id=configured.bootstrap_tenant_id,
                    session_factory=active_sessions,
                    skill_store_dir=configured.skill_store_dir,
                )
            if user_admin_service is None and active_sessions is not None:
                application.state.user_admin_service = PersistentUserAdminService(
                    active_sessions
                )
            if run_service is None:
                assert active_sessions is not None
                if active_runtime_registry is None:
                    assert active_redis is not None
                    assert active_secret_service is not None
                    active_runtime_registry = configured_runtime_registry(
                        config_service=ConfigService(active_sessions),
                        secret_service=active_secret_service,
                        redis_client=active_redis,
                    )
                queue = task_queue if task_queue is not None else InProcessRunQueue()
                application.state.run_service = RunService(
                    RunRepository(active_sessions),
                    runtime_registry=active_runtime_registry,
                    router=mode_router,
                    task_queue=queue,
                    hermes_advisor=PersistentHermesRunAdvisor(active_sessions),
                    temporary_agent_policy=AdminResourceTemporaryAgentPolicy(active_sessions),
                    runtime_timeout_seconds=configured.runtime_timeout_seconds,
                    runtime_token_budget=configured.runtime_token_budget,
                )
                application.state.run_queue = queue
            if (
                feishu_gateway is None
                and active_sessions is not None
                and getattr(application.state, "run_service", None) is not None
            ):
                application.state.feishu_gateway = ChannelGateway(
                    submitter=RunServiceInboundSubmitter(
                        run_service=cast(
                            RunSubmissionService,
                            application.state.run_service,
                        ),
                        tenant_id=configured.bootstrap_tenant_id,
                    ),
                    deduplicator=InboundDedupRepository(active_sessions),
                )

            if rate_limiter is None:
                assert active_redis is not None
                hmac_key = hashlib.sha256(
                    configured.jwt_signing_key_value().encode("utf-8")
                ).digest()
                application.state.rate_limiter = RedisAuthRateLimiter(
                    active_redis, hmac_key
                )

            if database_probe is None and active_sessions is not None:
                application.state.database_probe = _database_probe(active_sessions)
            if redis_probe is None and active_redis is not None:
                application.state.redis_probe = _redis_probe(active_redis)
            if configured.litellm_health_url is not None:
                extra_checks = dict(application.state.extra_readiness_checks)
                extra_checks["litellm"] = _http_readiness_probe(
                    configured.litellm_health_url,
                    timeout_seconds=readiness_timeout_seconds,
                )
                application.state.extra_readiness_checks = extra_checks
            yield
        finally:
            await _cleanup_owned_resources(
                cleanup_callbacks,
                primary_error=sys.exception(),
            )

    application = FastAPI(
        title="Agent Hub",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.database_probe = database_probe
    application.state.redis_probe = redis_probe
    application.state.readiness_timeout_seconds = readiness_timeout_seconds
    application.state.auth_service = auth_service
    application.state.rate_limiter = rate_limiter
    application.state.config_service = config_service
    application.state.admin_resource_service = admin_resource_service
    application.state.user_admin_service = user_admin_service
    application.state.bootstrap_tenant_id = configured_settings.bootstrap_tenant_id
    application.state.run_service = run_service
    application.state.runtime_registry = active_runtime_registry
    application.state.mode_router = mode_router
    application.state.run_queue = task_queue
    application.state.feishu_gateway = feishu_gateway
    application.state.metrics_registry = default_metrics_registry()
    application.state.extra_readiness_checks = {}
    application.state.channel_runtime_config = {}
    application.state.trusted_proxy_ips = configured_settings.trusted_proxy_ips
    application.add_exception_handler(PublicAPIError, public_error_handler)
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)

    async def validation_error_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        del request
        assert isinstance(error, RequestValidationError)
        return JSONResponse(
            status_code=422,
            content=error_payload("request_validation", "request validation failed"),
        )

    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.add_middleware(RequestBodyLimitMiddleware)
    application.add_middleware(SafeExceptionMiddleware)
    application.router.routes.extend(system.router.routes)
    application.router.routes.extend(auth.router.routes)
    application.router.routes.extend(config.router.routes)
    application.router.routes.extend(runs.router.routes)
    application.router.routes.extend(admin.router.routes)
    application.router.routes.extend(users.router.routes)
    application.router.routes.extend(
        create_lazy_feishu_webhook_router(
            gateway_provider=_feishu_gateway_from_request,
        ).routes
    )
    application.router.routes.extend(
        create_generic_channel_webhook_router(
            env=os.environ,
            gateway_provider=_feishu_gateway_from_request,
            runtime_config_provider=_channel_runtime_config_from_request,
        ).routes
    )

    @application.get("/{path:path}", include_in_schema=False, response_model=None)
    async def web_ui(path: str) -> Response:
        if path == "api" or path.startswith("api/"):
            return JSONResponse(
                status_code=404,
                content=error_payload("not_found", "resource not found"),
            )
        configured = settings or get_settings()
        if configured.web_dir is None:
            return JSONResponse(
                status_code=404,
                content=error_payload("not_found", "resource not found"),
            )
        return _web_ui_response(configured.web_dir, path)

    return application


def _web_ui_response(web_dir: Path, path: str) -> Response:
    root = web_dir.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or target.is_dir() or not target.exists():
        target = root / "index.html"
    if not target.exists() or not target.is_file():
        return JSONResponse(
            status_code=404,
            content=error_payload("not_found", "resource not found"),
        )
    return FileResponse(target)


def _feishu_gateway_from_request(request: Request) -> ChannelGatewayProtocol | None:
    gateway = getattr(request.app.state, "feishu_gateway", None)
    if gateway is None:
        return None
    return cast(ChannelGatewayProtocol, gateway)


async def _channel_runtime_config_from_request(request: Request) -> Mapping[str, str]:
    cached = getattr(request.app.state, "channel_runtime_config", None)
    if isinstance(cached, dict):
        return cast(dict[str, str], cached)
    service = getattr(request.app.state, "admin_resource_service", None)
    if service is None:
        return {}
    provider = getattr(service, "channel_runtime_config", None)
    if provider is None:
        return {}
    config = await provider()
    if isinstance(config, dict):
        request.app.state.channel_runtime_config = config
        return cast(dict[str, str], config)
    return {}


def _database_probe(
    session_factory: async_sessionmaker[AsyncSession],
) -> ReadinessProbe:
    async def probe() -> None:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))

    return probe


def _redis_probe(redis_client: RedisResource) -> ReadinessProbe:
    async def probe() -> None:
        await redis_client.ping()

    return probe


def _http_readiness_probe(url: str, *, timeout_seconds: float) -> ReadinessProbe:
    async def probe() -> None:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url)
            response.raise_for_status()

    return probe


app = create_app()
