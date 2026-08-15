"""FastAPI application factory and owned process resources."""

import asyncio
import contextlib
import hashlib
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
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
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.channels.base import InboundMessage
from agent_hub.channels.dedup import InboundDedupRepository
from agent_hub.channels.directives import (
    command_help_text,
    directive_summary,
    parse_channel_directives,
    parse_command_aliases,
)
from agent_hub.channels.feishu.media import FeishuOpenAPIMediaClient
from agent_hub.channels.feishu.media_factory import build_feishu_media_service_factory
from agent_hub.channels.feishu.reply import (
    FeishuOpenAPIReplySender,
    FeishuRunReplyDispatcher,
    log_feishu_reply_failure,
)
from agent_hub.channels.feishu.sdk_client import create_lark_oapi_feishu_websocket_client
from agent_hub.channels.feishu.settings import FeishuSettings, FeishuTransport
from agent_hub.channels.feishu.skill_install import FeishuSkillCommandHandler
from agent_hub.channels.feishu.webhook import (
    ChannelGatewayProtocol,
    _feishu_settings_from_runtime_config,
    create_lazy_feishu_webhook_router,
)
from agent_hub.channels.feishu.websocket import (
    FeishuWebSocketClient,
    FeishuWebSocketConnector,
    build_feishu_websocket_receiver,
)
from agent_hub.channels.gateway import ChannelGateway
from agent_hub.channels.generic_webhook import create_generic_channel_webhook_router
from agent_hub.channels.submitter import (
    ChannelSettingsService,
    RunServiceInboundSubmitter,
    RunSubmissionService,
)
from agent_hub.config.service import ConfigService
from agent_hub.db.models import TenantRow
from agent_hub.db.session import build_database
from agent_hub.domain.runs import TaskMode
from agent_hub.hermes import PersistentHermesRunAdvisor
from agent_hub.models.capabilities import is_known_video_generation_model
from agent_hub.models.capacity import CapacityPool, CredentialDescriptor, CredentialRegistry
from agent_hub.models.gateway import CapacityController, ModelGateway, ModelTransport
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.registry import ModelRegistry, NoCapableDeployment
from agent_hub.models.types import Deployment, ModelCapability
from agent_hub.multimodal.generation import (
    InMemoryMultimediaGenerationJobStore,
    MultimediaArtifact,
    MultimediaDailyLimitExceeded,
    MultimediaGenerationExecutor,
    MultimediaGenerationJob,
    MultimediaGenerationKind,
    MultimediaGenerationResult,
)
from agent_hub.multimodal.minimax import MiniMaxVideoGenerationClient
from agent_hub.multimodal.video_providers import TextToVideoProvider, TextToVideoProviderRouter
from agent_hub.observability.logging import configure_logging
from agent_hub.observability.metrics import default_metrics_registry
from agent_hub.routing.classifier import GatewayRouteClassifier
from agent_hub.routing.service import ModeRouter, RoutingPolicy
from agent_hub.routing.types import (
    InMemoryDecisionTokenStore,
    RiskLevel,
    RouteDecision,
    RouteSource,
)
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import ModeRouterProtocol, RunService, TaskQueue
from agent_hub.runs.temporary_agents import AdminResourceTemporaryAgentPolicy
from agent_hub.runtime.defaults import TenantSecretResolver, configured_runtime_registry
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.scheduler.service import SchedulerService
from agent_hub.scheduler.types import TaskRequest
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


RouterCapacityFactory = Callable[
    [tuple[Deployment, ...]],
    Awaitable[CapacityController | CapacityPool],
]
MultimediaCapacityFactory = Callable[
    [tuple[Deployment, ...]],
    Awaitable[CapacityController | CapacityPool],
]
MainAgentConfigGetter = Callable[[], Awaitable[admin.MainAgentConfigResponse]]
RegisteredModelListGetter = Callable[[], Awaitable[tuple[admin.ModelDeploymentResponse, ...]]]
FeishuWebSocketClientFactoryForSettings = Callable[
    [FeishuSettings], Awaitable[FeishuWebSocketClient]
]


class _MainAgentModeRouter:
    """Lazy production router backed by the separately configured main Agent model."""

    def __init__(
        self,
        *,
        get_config: MainAgentConfigGetter,
        list_models: RegisteredModelListGetter | None = None,
        secret_service: SecretService,
        tenant_id: UUID,
        redis_client: object,
        transport: ModelTransport | None = None,
        capacity_factory: RouterCapacityFactory | None = None,
    ) -> None:
        self._get_config = get_config
        self._list_models = list_models
        self._secret_service = secret_service
        self._tenant_id = tenant_id
        self._redis_client = redis_client
        self._transport = transport or LiteLLMClient()
        self._capacity_factory = capacity_factory

    async def route(self, task_text: object) -> RouteDecision:
        try:
            config = await self._get_config()
            if config.model is None:
                return _waiting_route_decision("main_agent_not_configured")
            deployment = await self._deployment_from_config(config.model)
            capacity = (
                await self._capacity_factory((deployment,))
                if self._capacity_factory is not None
                else await self._default_capacity((deployment,))
            )
            gateway = ModelGateway(
                ModelRegistry((deployment,)),
                capacity,
                TenantSecretResolver(self._secret_service, self._tenant_id),
                self._transport,
                capacity_wait_timeout=60,
            )
            router = ModeRouter(
                GatewayRouteClassifier(
                    gateway,
                    logical_model="main_agent",
                    source=RouteSource.CLASSIFIER,
                    prefer_plain_json=True,
                ),
                GatewayRouteClassifier(
                    gateway,
                    logical_model="main_agent",
                    source=RouteSource.VERIFIER,
                    prefer_plain_json=True,
                ),
                token_store=InMemoryDecisionTokenStore(),
                policy=RoutingPolicy(
                    confidence_threshold=0.65,
                    parallel_classifiers=False,
                    allow_single_classifier_decision=True,
                ),
            )
            return await router.route(task_text)
        except Exception as error:  # noqa: BLE001 - auto routing must degrade safely.
            _LOGGER.warning("main_agent_router_unavailable error_type=%s", type(error).__name__)
            return _waiting_route_decision("main_agent_router_unavailable")

    async def _deployment_from_config(self, model: admin.MainAgentModelConfig) -> Deployment:
        if self._list_models is None:
            return admin._main_agent_model_deployment(model)
        matched: admin.ModelDeploymentResponse | None = None
        try:
            for registered in await self._list_models():
                if (
                    registered.provider == model.provider
                    and registered.api_base == model.api_base
                    and registered.api_protocol == model.api_protocol
                    and registered.upstream_model == model.upstream_model
                    and registered.credential_ref == model.credential_ref
                ):
                    matched = registered
                    break
        except Exception as error:  # noqa: BLE001 - routing should still use explicit config.
            _LOGGER.warning(
                "main_agent_model_capability_lookup_failed error_type=%s",
                type(error).__name__,
            )
        if matched is None:
            return admin._main_agent_model_deployment(model)
        capabilities = set(model.capabilities)
        capabilities.update(matched.capabilities)
        parsed_capabilities = frozenset(ModelCapability(item) for item in capabilities)
        return Deployment(
            id="main_agent_1",
            logical_model="main_agent",
            provider_model=f"{model.provider}/{model.upstream_model}",
            request_model=model.upstream_model,
            api_base=model.api_base,
            secret_ref=model.credential_ref,
            quota_scope_id=matched.quota_scope,
            max_concurrency=matched.max_concurrency,
            target_utilization=matched.target_utilization,
            reserved_slots=matched.reserved_capacity,
            rpm=matched.rpm,
            tpm=matched.tpm,
            weight=matched.weight,
            capabilities=parsed_capabilities,
        )

    async def _default_capacity(
        self,
        deployments: tuple[Deployment, ...],
    ) -> CapacityPool:
        credentials = CredentialRegistry(
            [
                CredentialDescriptor(
                    deployment.secret_ref,
                    await self._secret_service.fingerprint(self._tenant_id, deployment.secret_ref),
                )
                for deployment in deployments
            ]
        )
        return CapacityPool(self._redis_client, deployments=deployments, credentials=credentials)


class _MainAgentContextWindowGetter:
    def __init__(self, get_config: MainAgentConfigGetter) -> None:
        self._get_config = get_config

    async def __call__(self) -> int | None:
        config = await self._get_config()
        if config.model is None:
            return None
        return _infer_main_agent_context_window_tokens(
            config.model.provider,
            config.model.upstream_model,
        )


class _ConfigBackedMultimediaGenerationExecutor:
    """Build a generation gateway from the current registered model resources."""

    def __init__(
        self,
        *,
        list_models: RegisteredModelListGetter,
        secret_service: SecretService,
        tenant_id: UUID,
        redis_client: object,
        transport: ModelTransport | None = None,
        capacity_factory: MultimediaCapacityFactory | None = None,
        media_store_dir: Path | None = None,
        video_provider_router: TextToVideoProviderRouter | None = None,
    ) -> None:
        self._list_models = list_models
        self._secret_service = secret_service
        self._tenant_id = tenant_id
        self._redis_client = redis_client
        self._transport = transport or LiteLLMClient()
        self._capacity_factory = capacity_factory
        self._media_store_dir = (media_store_dir or Path("/var/lib/agent-hub/media")).resolve()
        self._video_provider_router = video_provider_router or TextToVideoProviderRouter(
            (("minimax", MiniMaxVideoGenerationClient()),)
        )
        self._daily_usage: dict[tuple[date, str, str], int] = {}
        self._job_store = InMemoryMultimediaGenerationJobStore()

    def submit(
        self,
        *,
        kind: MultimediaGenerationKind,
        logical_model: str,
        prompt: str,
    ) -> MultimediaGenerationJob:
        return self._job_store.create(
            kind=kind,
            logical_model=logical_model,
            prompt=prompt.strip(),
        )

    def get_job(self, job_id: str) -> MultimediaGenerationJob:
        return self._job_store.get(job_id)

    async def run_job(
        self,
        job_id: str,
        *,
        executor_id: str,
    ) -> MultimediaGenerationJob:
        job = self._job_store.start(job_id, executor_id=executor_id)
        try:
            result = await self.generate(
                kind=job.kind,
                logical_model=job.logical_model,
                prompt=job.prompt,
            )
        except Exception as error:
            self._job_store.fail(job_id, error=str(error))
            raise
        return self._job_store.succeed(
            job_id,
            artifacts=(
                MultimediaArtifact(
                    kind=result.kind,
                    uri=result.text,
                    text=result.text,
                ),
            ),
        )

    async def generate(
        self,
        *,
        kind: MultimediaGenerationKind,
        logical_model: str,
        prompt: str,
    ) -> MultimediaGenerationResult:
        deployments = tuple(
            _deployment_from_model_resource(model) for model in await self._list_models()
        )
        registry = ModelRegistry(deployments)
        candidates = registry.candidates(logical_model, {_multimedia_required_capability(kind)})
        _require_supported_multimedia_generation(
            kind=kind,
            logical_model=logical_model,
            candidates=candidates,
        )
        daily_limit = _multimedia_daily_limit(kind, deployments, logical_model)
        self._claim_daily_slot(
            kind=kind,
            logical_model=logical_model,
            daily_limit=daily_limit,
        )
        direct_result = await self._generate_with_direct_provider(
            kind=kind,
            prompt=prompt,
            candidates=candidates,
        )
        if direct_result is not None:
            return direct_result
        capacity = (
            await self._capacity_factory(deployments)
            if self._capacity_factory is not None
            else await self._default_capacity(deployments)
        )
        gateway = ModelGateway(
            registry,
            capacity,
            TenantSecretResolver(self._secret_service, self._tenant_id),
            self._transport,
            capacity_wait_timeout=60,
        )
        return await MultimediaGenerationExecutor(gateway).generate(
            kind=kind,
            logical_model=logical_model,
            prompt=prompt,
        )

    async def _generate_with_direct_provider(
        self,
        *,
        kind: MultimediaGenerationKind,
        prompt: str,
        candidates: tuple[Deployment, ...],
    ) -> MultimediaGenerationResult | None:
        if kind is not MultimediaGenerationKind.VIDEO:
            return None
        selected: tuple[Deployment, TextToVideoProvider] | None = None
        for candidate in candidates:
            provider = self._video_provider_router.provider_for(candidate)
            if provider is not None:
                selected = (candidate, provider)
                break
        if selected is None:
            return None
        deployment, provider = selected
        api_key = await self._secret_service.resolve(self._tenant_id, deployment.secret_ref)
        artifact = await provider.generate_text_to_video(
            api_key=api_key,
            api_base=deployment.api_base,
            model=deployment.request_model or deployment.provider_model,
            prompt=prompt,
            output_dir=self._media_store_dir / str(self._tenant_id),
            duration=6,
            resolution="768P",
        )
        return MultimediaGenerationResult(
            kind=kind,
            logical_model=deployment.logical_model,
            deployment_id=deployment.id,
            text=artifact.uri,
        )

    def _claim_daily_slot(
        self,
        *,
        kind: MultimediaGenerationKind,
        logical_model: str,
        daily_limit: int | None,
    ) -> None:
        if daily_limit is None:
            return
        today = datetime.now(UTC).date()
        self._daily_usage = {
            key: count for key, count in self._daily_usage.items() if key[0] == today
        }
        key = (today, kind.value, logical_model)
        current = self._daily_usage.get(key, 0)
        if current >= daily_limit:
            raise MultimediaDailyLimitExceeded("daily multimedia generation limit exceeded")
        self._daily_usage[key] = current + 1

    async def _default_capacity(
        self,
        deployments: tuple[Deployment, ...],
    ) -> CapacityPool:
        credentials = CredentialRegistry(
            [
                CredentialDescriptor(
                    secret_ref,
                    await self._secret_service.fingerprint(self._tenant_id, secret_ref),
                )
                for secret_ref in dict.fromkeys(deployment.secret_ref for deployment in deployments)
            ]
        )
        return CapacityPool(self._redis_client, deployments=deployments, credentials=credentials)


def _deployment_from_model_resource(model: admin.ModelDeploymentResponse) -> Deployment:
    return Deployment(
        id=str(model.id),
        logical_model=model.logical_model,
        provider_model=f"{model.provider}/{model.upstream_model}",
        request_model=model.upstream_model,
        api_base=model.api_base,
        secret_ref=model.credential_ref,
        quota_scope_id=model.quota_scope,
        max_concurrency=model.max_concurrency,
        target_utilization=model.target_utilization,
        reserved_slots=model.reserved_capacity,
        rpm=model.rpm,
        tpm=model.tpm,
        weight=model.weight,
        capabilities=frozenset(ModelCapability(item) for item in model.capabilities),
    )


def _multimedia_required_capability(kind: MultimediaGenerationKind) -> ModelCapability:
    if kind is MultimediaGenerationKind.IMAGE:
        return ModelCapability.IMAGE_GENERATION
    if kind is MultimediaGenerationKind.VIDEO:
        return ModelCapability.VIDEO_GENERATION
    if kind is MultimediaGenerationKind.AUDIO:
        return ModelCapability.AUDIO_GENERATION
    raise ValueError("generation kind is invalid")


def _multimedia_daily_limit(
    kind: MultimediaGenerationKind,
    deployments: tuple[Deployment, ...],
    logical_model: str,
) -> int | None:
    if kind is not MultimediaGenerationKind.VIDEO:
        return None
    matching = [
        deployment for deployment in deployments if deployment.logical_model == logical_model
    ]
    if any(_is_minimax_video_deployment(deployment) for deployment in matching):
        return 3
    return None


def _require_supported_multimedia_generation(
    *,
    kind: MultimediaGenerationKind,
    logical_model: str,
    candidates: tuple[Deployment, ...],
) -> None:
    if kind is not MultimediaGenerationKind.VIDEO:
        return
    if any(_is_supported_video_generation_deployment(deployment) for deployment in candidates):
        return
    raise NoCapableDeployment(
        f"no supported video generation deployment for logical model {logical_model!r}: "
        "video_generation"
    )


def _is_supported_video_generation_deployment(deployment: Deployment) -> bool:
    provider, upstream_model = _deployment_provider_and_model(deployment)
    return (
        ModelCapability.VIDEO_GENERATION in deployment.capabilities
        and is_known_video_generation_model(provider, upstream_model)
    )


def _is_minimax_video_deployment(deployment: Deployment) -> bool:
    provider, _upstream_model = _deployment_provider_and_model(deployment)
    return "minimax" in provider.casefold() and _is_supported_video_generation_deployment(
        deployment
    )


def _deployment_provider_and_model(deployment: Deployment) -> tuple[str, str]:
    provider, separator, upstream_model = deployment.provider_model.partition("/")
    if not separator:
        return "", deployment.request_model or deployment.provider_model
    return provider, upstream_model


def _infer_main_agent_context_window_tokens(provider: str, upstream_model: str) -> int:
    normalized = f"{provider}/{upstream_model}".casefold()
    if "gemini" in normalized:
        return 1_000_000
    if "gpt-5" in normalized or "gpt-4.1" in normalized:
        return 400_000
    if "claude" in normalized:
        return 200_000
    if any(marker in normalized for marker in ("deepseek", "qwen", "kimi", "gpt-4o")):
        return 128_000
    return 32_768


def _waiting_route_decision(reason: str) -> RouteDecision:
    return RouteDecision(
        mode=None,
        needs_user_choice=True,
        status="waiting_user_mode",
        assessments=(),
        clarification_reason=reason,
        options=(TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID),
        decision_token=None,
        version=1,
        risk=RiskLevel.LOW,
        requires_approval=False,
        permissions_still_apply=True,
    )


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
    feishu_websocket_client_factory: FeishuWebSocketClientFactoryForSettings | None = None,
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
        active_mode_router = mode_router
        cleanup_callbacks: list[CleanupCallback] = []
        token_service = (
            AccessTokenService(configured.jwt_signing_key_value()) if auth_service is None else None
        )
        try:
            application.state.settings = configured
            application.state.trusted_proxy_ips = configured.trusted_proxy_ips
            application.state.bootstrap_tenant_id = configured.bootstrap_tenant_id
            application.state.attachment_store_dir = configured.attachment_store_dir
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

            needs_redis = (
                rate_limiter is None
                or redis_probe is None
                or (run_service is None and active_runtime_registry is None)
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
                application.state.user_admin_service = PersistentUserAdminService(active_sessions)
            if run_service is None:
                assert active_sessions is not None
                if active_runtime_registry is None:
                    assert active_redis is not None
                    assert active_secret_service is not None
                    runtime_capabilities = RuntimeCapabilityGateway(
                        skill_store_dir=configured.skill_store_dir,
                        workspace_root=configured.attachment_store_dir,
                    )
                    active_runtime_registry = configured_runtime_registry(
                        config_service=ConfigService(active_sessions),
                        secret_service=active_secret_service,
                        redis_client=active_redis,
                        capability_gateway=runtime_capabilities,
                    )
                if active_mode_router is None and active_secret_service is not None:
                    assert active_redis is not None
                    admin_service_for_router = cast(
                        admin.AdminResourceService,
                        admin_resource_service
                        if admin_resource_service is not None
                        else application.state.admin_resource_service,
                    )
                    active_mode_router = _MainAgentModeRouter(
                        get_config=admin_service_for_router.get_main_agent_config,
                        list_models=admin_service_for_router.list_models,
                        secret_service=active_secret_service,
                        tenant_id=configured.bootstrap_tenant_id,
                        redis_client=active_redis,
                    )
                queue = task_queue if task_queue is not None else InProcessRunQueue()
                application.state.run_service = RunService(
                    RunRepository(active_sessions),
                    runtime_registry=active_runtime_registry,
                    router=active_mode_router,
                    task_queue=queue,
                    hermes_advisor=PersistentHermesRunAdvisor(active_sessions),
                    temporary_agent_policy=AdminResourceTemporaryAgentPolicy(active_sessions),
                    runtime_timeout_seconds=configured.runtime_timeout_seconds,
                    runtime_token_budget=configured.runtime_token_budget,
                    main_agent_context_window_getter=_MainAgentContextWindowGetter(
                        cast(
                            admin.AdminResourceService,
                            admin_resource_service
                            if admin_resource_service is not None
                            else application.state.admin_resource_service,
                        ).get_main_agent_config
                    ),
                )
                application.state.run_queue = queue
                application.state.mode_router = active_mode_router
            if (
                getattr(application.state, "schedule_service", None) is None
                and getattr(application.state, "run_service", None) is not None
            ):
                application.state.schedule_service = SchedulerService(
                    lambda task: _submit_scheduled_task(application, task)
                )
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
                        settings_service=cast(
                            ChannelSettingsService,
                            admin_resource_service
                            if admin_resource_service is not None
                            else application.state.admin_resource_service,
                        ),
                    ),
                    deduplicator=InboundDedupRepository(active_sessions),
                )
            if active_sessions is not None:
                application.state.feishu_reply_dispatcher = FeishuRunReplyDispatcher(
                    run_repository=RunRepository(active_sessions),
                    sender=FeishuOpenAPIReplySender(),
                )
            if active_secret_service is not None and active_redis is not None:
                admin_service_for_generation = cast(
                    admin.AdminResourceService,
                    admin_resource_service
                    if admin_resource_service is not None
                    else application.state.admin_resource_service,
                )
                application.state.multimedia_generation_executor = (
                    _ConfigBackedMultimediaGenerationExecutor(
                        list_models=admin_service_for_generation.list_models,
                        secret_service=active_secret_service,
                        tenant_id=configured.bootstrap_tenant_id,
                        redis_client=active_redis,
                    )
                )
                media_factory = build_feishu_media_service_factory(
                    config_service=cast(ConfigService, application.state.config_service),
                    secret_service=active_secret_service,
                    redis_client=active_redis,
                    tenant_id=configured.bootstrap_tenant_id,
                    attachment_store_dir=configured.attachment_store_dir,
                    log_service=application.state.admin_resource_service,
                    environment=configured.environment,
                )
                application.state.feishu_media_service_factory = media_factory
                application.state.feishu_skill_command_handler = FeishuSkillCommandHandler(
                    admin_service=cast(
                        Any,
                        admin_resource_service
                        if admin_resource_service is not None
                        else application.state.admin_resource_service,
                    ),
                    media_client_factory=lambda settings: FeishuOpenAPIMediaClient(
                        settings=settings
                    ),
                )
                cleanup_callbacks.append(("feishu_media_service_factory", media_factory.aclose))

            await _start_feishu_websocket_connector_if_configured(
                application,
                client_factory=feishu_websocket_client_factory,
            )

            if rate_limiter is None:
                assert active_redis is not None
                hmac_key = hashlib.sha256(
                    configured.jwt_signing_key_value().encode("utf-8")
                ).digest()
                application.state.rate_limiter = RedisAuthRateLimiter(active_redis, hmac_key)

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
            await _stop_feishu_websocket_connector(application)
            await _cancel_background_tasks(application.state.feishu_reply_tasks)
            await _cleanup_owned_resources(
                cleanup_callbacks,
                primary_error=sys.exception(),
            )

    application = FastAPI(
        title="魔方agent",
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
    application.state.schedule_service = None
    application.state.feishu_gateway = feishu_gateway
    application.state.feishu_reply_dispatcher = None
    application.state.feishu_reply_tasks = set()
    application.state.feishu_websocket_connector = None
    application.state.feishu_websocket_task = None
    application.state.multimedia_generation_executor = None

    async def refresh_channel_runtime_config(runtime_config: Mapping[str, str]) -> None:
        application.state.channel_runtime_config = dict(runtime_config)
        await _restart_feishu_websocket_connector(
            application,
            client_factory=feishu_websocket_client_factory,
        )

    application.state.refresh_channel_runtime_config = refresh_channel_runtime_config
    application.state.metrics_registry = default_metrics_registry()
    application.state.extra_readiness_checks = {}
    application.state.channel_runtime_config = None
    application.state.trusted_proxy_ips = configured_settings.trusted_proxy_ips
    application.add_exception_handler(PublicAPIError, public_error_handler)
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)

    async def validation_error_handler(request: Request, error: Exception) -> JSONResponse:
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
            runtime_config_provider=_channel_runtime_config_from_request,
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


async def _cancel_background_tasks(tasks: set[asyncio.Task[object]]) -> None:
    if not tasks:
        return
    for task in tuple(tasks):
        task.cancel()
    await asyncio.gather(*tuple(tasks), return_exceptions=True)
    tasks.clear()


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


async def _start_feishu_websocket_connector_if_configured(
    application: FastAPI,
    *,
    client_factory: FeishuWebSocketClientFactoryForSettings | None,
) -> None:
    runtime_config = await _channel_runtime_config_from_app(application)
    application.state.channel_runtime_config = runtime_config
    settings = _feishu_settings_from_runtime_config(FeishuSettings(), runtime_config)
    if not _should_start_feishu_websocket(settings, runtime_config):
        return
    gateway = getattr(application.state, "feishu_gateway", None)
    if gateway is None:
        return
    receiver = build_feishu_websocket_receiver(
        settings,
        gateway=cast(ChannelGatewayProtocol, gateway),
        submission_handler=_feishu_websocket_submission_handler(application, settings),
            help_handler=_feishu_websocket_help_handler(application, settings),
    )
    resolved_factory = client_factory or create_lark_oapi_feishu_websocket_client

    async def create_client() -> FeishuWebSocketClient:
        return await resolved_factory(settings)

    connector = FeishuWebSocketConnector(
        receiver=receiver,
        client_factory=create_client,
        reconnect_min_seconds=settings.websocket_reconnect_min_seconds,
        reconnect_max_seconds=settings.websocket_reconnect_max_seconds,
    )
    task: asyncio.Task[object] = asyncio.create_task(
        connector.run_forever(), name="feishu-websocket-connector"
    )
    application.state.feishu_websocket_connector = connector
    application.state.feishu_websocket_task = task
    _LOGGER.info("feishu_websocket_connector_started")


def _feishu_websocket_submission_handler(
    application: FastAPI,
    settings: FeishuSettings,
) -> Callable[[InboundMessage, object], Awaitable[None]]:
    async def handle(message: InboundMessage, submission: object) -> None:
        _schedule_feishu_websocket_reply(application, settings, message, submission)

    return handle



def _feishu_websocket_help_handler(
    application: FastAPI,
    settings: FeishuSettings,
) -> Callable[[InboundMessage], Awaitable[None]]:
    async def handle(message: InboundMessage) -> None:
        sender = getattr(application.state, "feishu_reply_sender", None)
        if sender is None:
            dispatcher = getattr(application.state, "feishu_reply_dispatcher", None)
            if isinstance(dispatcher, FeishuRunReplyDispatcher):
                sender = dispatcher.sender
        if sender is None or getattr(sender, "reply_text", None) is None:
            return
        aliases = parse_command_aliases(settings.command_aliases)
        try:
            await sender.reply_text(
                settings=settings,
                message_id=message.message_id,
                text=command_help_text(aliases),
            )
        except Exception as error:  # noqa: BLE001 - best-effort channel delivery boundary
            await log_feishu_reply_failure(
                log_service=getattr(application.state, "admin_resource_service", None),
                run_id=None,
                message_id=message.message_id,
                error=error,
            )

    return handle


def _schedule_feishu_websocket_reply(
    application: FastAPI,
    settings: FeishuSettings,
    message: InboundMessage,
    submission: object,
) -> None:
    if bool(getattr(submission, "duplicate", False)):
        return
    dispatcher = getattr(application.state, "feishu_reply_dispatcher", None)
    if not isinstance(dispatcher, FeishuRunReplyDispatcher):
        return
    tenant_id = getattr(application.state, "bootstrap_tenant_id", None)
    if tenant_id is None:
        return
    tasks = getattr(application.state, "feishu_reply_tasks", None)
    if not isinstance(tasks, set):
        return
    run_id = getattr(submission, "run_id", None)
    log_service = getattr(application.state, "admin_resource_service", None)
    aliases = parse_command_aliases(settings.command_aliases)

    async def task() -> None:
        try:
            await dispatcher.sender.reply_text(
                settings=settings,
                message_id=message.message_id,
                text=directive_summary(parse_channel_directives(message.text, aliases), aliases),
            )
            if run_id is not None:
                await dispatcher.reply_when_terminal(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    source_message_id=message.message_id,
                    settings=settings,
                )
        except Exception as error:  # noqa: BLE001 - best-effort channel delivery boundary
            await log_feishu_reply_failure(
                log_service=log_service,
                run_id=run_id,
                message_id=message.message_id,
                error=error,
            )

    created = asyncio.create_task(task())
    tasks.add(created)
    created.add_done_callback(tasks.discard)


async def _stop_feishu_websocket_connector(application: FastAPI) -> None:
    connector = getattr(application.state, "feishu_websocket_connector", None)
    task = getattr(application.state, "feishu_websocket_task", None)
    if isinstance(connector, FeishuWebSocketConnector):
        connector.request_shutdown()
    if isinstance(task, asyncio.Task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    application.state.feishu_websocket_connector = None
    application.state.feishu_websocket_task = None


async def _restart_feishu_websocket_connector(
    application: FastAPI,
    *,
    client_factory: FeishuWebSocketClientFactoryForSettings | None,
) -> None:
    await _stop_feishu_websocket_connector(application)
    await _start_feishu_websocket_connector_if_configured(
        application,
        client_factory=client_factory,
    )


def _should_start_feishu_websocket(
    settings: FeishuSettings, runtime_config: Mapping[str, str]
) -> bool:
    raw_transport = (
        runtime_config.get("FEISHU_TRANSPORT")
        or os.environ.get("FEISHU_TRANSPORT")
        or FeishuTransport.WEBSOCKET.value
    )
    try:
        transport = FeishuTransport(raw_transport)
    except ValueError:
        transport = FeishuTransport.WEBSOCKET
    if FeishuTransport.WEBSOCKET not in (
        {FeishuTransport.WEBHOOK, FeishuTransport.WEBSOCKET}
        if transport is FeishuTransport.BOTH
        else {transport}
    ):
        return False
    has_app_id = bool(runtime_config.get("FEISHU_APP_ID") or os.environ.get("FEISHU_APP_ID"))
    has_app_secret = bool(
        runtime_config.get("FEISHU_APP_SECRET") or os.environ.get("FEISHU_APP_SECRET")
    )
    return has_app_id and has_app_secret and bool(settings.app_id and settings.app_secret_value())


async def _channel_runtime_config_from_app(application: FastAPI) -> dict[str, str]:
    cached = getattr(application.state, "channel_runtime_config", None)
    if isinstance(cached, dict):
        return cast(dict[str, str], cached)
    service = getattr(application.state, "admin_resource_service", None)
    if service is None:
        return {}
    provider = getattr(service, "channel_runtime_config", None)
    if provider is None:
        return {}
    try:
        config = await provider()
    except Exception as error:  # noqa: BLE001 - channel startup must not break app lifespan.
        _LOGGER.warning("channel_runtime_config_unavailable error_type=%s", type(error).__name__)
        return {}
    if isinstance(config, dict):
        return cast(dict[str, str], config)
    return {}


async def _submit_scheduled_task(application: FastAPI, request: TaskRequest) -> object:
    run_service = getattr(application.state, "run_service", None)
    if run_service is None or not hasattr(run_service, "submit"):
        raise RuntimeError("run service is unavailable")
    metadata = {str(key): str(value) for key, value in request.metadata.items()}
    return await cast(Any, run_service).submit(
        tenant_id=request.tenant_id,
        actor_id=request.actor_id,
        message=request.message,
        mode=request.mode,
        workflow_id=request.workflow,
        channel_context=metadata,
        idempotency_key=request.idempotency_key,
    )


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
    try:
        config = await provider()
    except Exception as error:  # noqa: BLE001 - webhook config lookup degrades to env settings.
        _LOGGER.warning("channel_runtime_config_unavailable error_type=%s", type(error).__name__)
        return {}
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
