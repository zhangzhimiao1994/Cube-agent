"""FastAPI application factory and owned process resources."""

import hashlib
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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
from agent_hub.api.routers import auth, config, system
from agent_hub.auth.passwords import PasswordService
from agent_hub.auth.rate_limit import RedisAuthRateLimiter
from agent_hub.auth.service import AuthService
from agent_hub.auth.tokens import AccessTokenService
from agent_hub.config.service import ConfigService
from agent_hub.db.models import TenantRow
from agent_hub.db.session import build_database
from agent_hub.settings import Settings, get_settings

ReadinessProbe = Callable[[], Awaitable[None]]
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
    database_factory: Callable[[str], DatabaseResource] = build_database,
    redis_factory: Callable[[str], RedisResource] = Redis.from_url,
) -> FastAPI:
    """Create an application without opening network resources at import time."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configured = settings or get_settings()
        active_database = database
        active_redis = redis_client
        active_sessions = session_factory
        cleanup_callbacks: list[
            tuple[str, Callable[[], Awaitable[None]]]
        ] = []
        token_service = (
            AccessTokenService(configured.jwt_signing_key_value())
            if auth_service is None
            else None
        )
        try:
            application.state.trusted_proxy_ips = configured.trusted_proxy_ips
            needs_sessions = auth_service is None or config_service is None
            if active_sessions is None and active_database is not None:
                active_sessions = active_database.session_factory
            if active_sessions is None and needs_sessions:
                active_database = database_factory(configured.database_url_value())
                cleanup_callbacks.append(("database", active_database.dispose))
                active_sessions = active_database.session_factory

            needs_redis = rate_limiter is None or redis_probe is None
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
            yield
        finally:
            primary_error = sys.exception()
            cleanup_error_types: list[str] = []
            for resource_name, cleanup in reversed(cleanup_callbacks):
                try:
                    await cleanup()
                except BaseException as error:  # noqa: BLE001 -- all cleanups must be attempted.
                    error_type = type(error).__name__
                    cleanup_error_types.append(error_type)
                    _LOGGER.error(
                        "resource_cleanup_failed resource=%s error_type=%s",
                        resource_name,
                        error_type,
                    )
            if cleanup_error_types and primary_error is None:
                raise ResourceCleanupError(tuple(cleanup_error_types)) from None

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
    configured_settings = settings or Settings.model_construct()
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
    return application


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


app = create_app()
