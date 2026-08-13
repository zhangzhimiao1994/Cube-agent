import base64
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

import pytest
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

from agent_hub.api.routers.admin import (
    InMemoryAdminResourceService,
    MainAgentConfigResponse,
    MainAgentModelConfig,
    ModelDeploymentResponse,
)
from agent_hub.app import (
    _ConfigBackedMultimediaGenerationExecutor,
    _infer_main_agent_context_window_tokens,
    _MainAgentContextWindowGetter,
    _MainAgentModeRouter,
    _web_ui_response,
    create_app,
)
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.channels.feishu.media import FeishuMediaService
from agent_hub.channels.feishu.media_factory import build_feishu_media_service_factory
from agent_hub.channels.feishu.settings import FeishuSettings
from agent_hub.models.capacity import CapacityLease
from agent_hub.models.gateway import CapacityController
from agent_hub.models.registry import NoCapableDeployment
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, TokenUsage
from agent_hub.multimodal.generation import MultimediaGenerationKind
from agent_hub.routing.types import RiskLevel
from agent_hub.settings import Settings

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")


class StubAuthService:
    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid-token":
            raise InvalidCredentials("bad token")
        return AuthenticatedPrincipal(uuid4(), TENANT_ID, Role.SUPER_ADMIN)


class StubRateLimiter:
    pass


class StubConfigService:
    async def get_current(self, tenant_id: UUID) -> None:
        assert tenant_id == TENANT_ID


class FakeSession:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeDatabase:
    def __init__(self) -> None:
        self.session_factory = FakeSession

    async def dispose(self) -> None:
        return None


class FakeRedis:
    async def aclose(self) -> None:
        return None

    async def ping(self, **kwargs: object) -> bool:
        del kwargs
        return True


def valid_settings(attachment_store_dir: Path | None = None) -> Settings:
    key = base64.urlsafe_b64encode(b"x" * 32).decode("ascii").rstrip("=")
    values: dict[str, object] = {"jwt_signing_key": "base64url:" + key}
    if attachment_store_dir is not None:
        values["attachment_store_dir"] = attachment_store_dir
    return Settings.model_validate(values)


def test_web_ui_rejects_sibling_path_with_shared_prefix(tmp_path: Path) -> None:
    web_root = tmp_path / "web"
    web_root.mkdir()
    (web_root / "index.html").write_text("index", encoding="utf-8")
    sibling = tmp_path / "web-evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret", encoding="utf-8")

    response = _web_ui_response(web_root, "../web-evil/secret.txt")

    assert isinstance(response, FileResponse)
    assert Path(response.path) == web_root / "index.html"


def test_create_app_mounts_feishu_webhook_on_main_api() -> None:
    application = create_app(
        auth_service=object(),
        rate_limiter=object(),
        config_service=object(),
        run_service=object(),
    )

    paths = {getattr(route, "path", "") for route in application.routes}

    assert "/channels/feishu/events" in paths
    assert application.state.channel_runtime_config is None


def test_create_app_wires_production_feishu_media_service_factory(tmp_path: Path) -> None:
    application = create_app(
        settings=valid_settings(tmp_path),
        database=FakeDatabase(),
        redis_client=FakeRedis(),
        auth_service=StubAuthService(),
        rate_limiter=StubRateLimiter(),
        config_service=StubConfigService(),
        admin_resource_service=InMemoryAdminResourceService(),
        user_admin_service=object(),
        run_service=object(),
    )

    with TestClient(application):
        factory = getattr(application.state, "feishu_media_service_factory", None)
        assert callable(factory)
        service = factory(
            FeishuSettings.model_validate(
                {
                    "app_id": "cli_runtime",
                    "app_secret": "secret",
                    "verification_token": "token",
                    "encrypt_key": "encrypt-key",
                    "transport": "webhook",
                }
            )
        )

    assert isinstance(service, FeishuMediaService)


def test_create_app_wires_production_multimedia_generation_executor(tmp_path: Path) -> None:
    application = create_app(
        settings=valid_settings(tmp_path),
        database=FakeDatabase(),
        redis_client=FakeRedis(),
        auth_service=StubAuthService(),
        rate_limiter=StubRateLimiter(),
        config_service=StubConfigService(),
        admin_resource_service=InMemoryAdminResourceService(),
        user_admin_service=object(),
        run_service=object(),
    )

    with TestClient(application):
        executor = getattr(application.state, "multimedia_generation_executor", None)

    assert isinstance(executor, _ConfigBackedMultimediaGenerationExecutor)


def test_feishu_media_factory_uses_memory_store_in_development(tmp_path: Path) -> None:
    factory = build_feishu_media_service_factory(
        config_service=StubConfigService(),
        secret_service=FakeSecretService(),
        redis_client=FakeRedis(),
        tenant_id=TENANT_ID,
        attachment_store_dir=tmp_path,
        environment="development",
    )

    assert factory.object_store.store_id == "memory-image-store"


@pytest.mark.asyncio
async def test_multimedia_executor_rejects_video_without_capacity_or_secret_lookup() -> None:
    capacity_calls = 0

    async def list_models() -> tuple[ModelDeploymentResponse, ...]:
        return (
            ModelDeploymentResponse(
                provider="deepseek",
                api_base="https://api.deepseek.com/v1",
                api_protocol="openai_compatible",
                upstream_model="deepseek-v4-flash",
                logical_model="video_primary",
                capabilities=["text"],
                credential_ref="secret://video-primary",
                quota_scope="deepseek-account",
                max_concurrency=1,
                target_utilization=0.8,
                reserved_capacity=0,
                id=uuid4(),
                effective_slots=1,
                saturation_policy="queue_first_then_fallback",
            ),
        )

    async def capacity_factory(
        _deployments: tuple[Deployment, ...],
    ) -> CapacityController:
        nonlocal capacity_calls
        capacity_calls += 1
        return ImmediateCapacity()

    executor = _ConfigBackedMultimediaGenerationExecutor(
        list_models=list_models,
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        redis_client=object(),
        capacity_factory=capacity_factory,
    )

    with pytest.raises(NoCapableDeployment, match="video_generation"):
        await executor.generate(
            kind=MultimediaGenerationKind.VIDEO,
            logical_model="video_primary",
            prompt="make a short launch video",
        )

    assert capacity_calls == 0


@pytest.mark.asyncio
async def test_multimedia_executor_rejects_unknown_video_model_even_if_declared() -> None:
    capacity_calls = 0

    async def list_models() -> tuple[ModelDeploymentResponse, ...]:
        return (
            ModelDeploymentResponse(
                provider="minimax",
                api_base="https://api.minimax.chat/v1",
                api_protocol="openai_compatible",
                upstream_model="MiniMax-M3",
                logical_model="video_primary",
                capabilities=["text", "video_generation"],
                credential_ref="secret://video-primary",
                quota_scope="minimax-account",
                max_concurrency=1,
                target_utilization=0.8,
                reserved_capacity=0,
                id=uuid4(),
                effective_slots=1,
                saturation_policy="queue_first_then_fallback",
            ),
        )

    async def capacity_factory(
        _deployments: tuple[Deployment, ...],
    ) -> CapacityController:
        nonlocal capacity_calls
        capacity_calls += 1
        return ImmediateCapacity()

    executor = _ConfigBackedMultimediaGenerationExecutor(
        list_models=list_models,
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        redis_client=object(),
        capacity_factory=capacity_factory,
    )

    with pytest.raises(NoCapableDeployment, match="supported video generation"):
        await executor.generate(
            kind=MultimediaGenerationKind.VIDEO,
            logical_model="video_primary",
            prompt="make a short launch video",
        )

    assert capacity_calls == 0


@pytest.mark.asyncio
async def test_multimedia_executor_limits_minimax_video_to_three_daily_requests() -> None:
    transport = FakeTransport()

    async def list_models() -> tuple[ModelDeploymentResponse, ...]:
        return (
            ModelDeploymentResponse(
                provider="minimax",
                api_base="https://api.minimax.chat/v1",
                api_protocol="openai_compatible",
                upstream_model="MiniMax-Hailuo-02",
                logical_model="video_primary",
                capabilities=["text", "video_generation"],
                credential_ref="secret://main-agent",
                quota_scope="minimax-account",
                max_concurrency=1,
                target_utilization=0.8,
                reserved_capacity=0,
                id=uuid4(),
                effective_slots=1,
                saturation_policy="queue_first_then_fallback",
            ),
        )

    async def capacity_factory(_deployments: tuple[Deployment, ...]) -> CapacityController:
        return ImmediateCapacity()

    executor = _ConfigBackedMultimediaGenerationExecutor(
        list_models=list_models,
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        redis_client=object(),
        transport=transport,
        capacity_factory=capacity_factory,
    )

    for index in range(3):
        await executor.generate(
            kind=MultimediaGenerationKind.VIDEO,
            logical_model="video_primary",
            prompt=f"make minimax video {index}",
        )

    with pytest.raises(RuntimeError, match="daily multimedia generation limit"):
        await executor.generate(
            kind=MultimediaGenerationKind.VIDEO,
            logical_model="video_primary",
            prompt="make minimax video 4",
        )

    assert len(transport.requests) == 3


class FakeSecretService:
    async def resolve(self, tenant_id: UUID, reference: object) -> str:
        assert tenant_id == TENANT_ID
        assert reference == "secret://main-agent"
        return "sk-live"

    async def fingerprint(self, tenant_id: UUID, reference: object) -> str:
        assert tenant_id == TENANT_ID
        assert reference == "secret://main-agent"
        return "a" * 64


class ImmediateCapacity:
    async def initialize(self) -> None:
        return None

    def validate_configuration(self, deployments: Sequence[Deployment]) -> None:
        assert len(deployments) == 1

    async def acquire(
        self,
        candidates: Sequence[Deployment],
        wait_timeout: float,
        *,
        estimated_tokens: int,
    ) -> CapacityLease:
        assert candidates
        assert wait_timeout > 0
        assert estimated_tokens > 0
        return CapacityLease(
            id=str(uuid4()),
            deployment_id=candidates[0].id,
            quota_scope_id=candidates[0].quota_scope_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
            renew_after_seconds=30,
        )

    async def renew(self, lease: CapacityLease) -> CapacityLease | None:
        return lease

    async def release(self, lease: CapacityLease) -> bool:
        del lease
        return True

    async def record_outcome(
        self,
        quota_scope_id: str,
        *,
        status_code: int | None,
        latency_seconds: float,
        succeeded: bool,
    ) -> None:
        del quota_scope_id, status_code, latency_seconds, succeeded


class FakeTransport:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> ModelResponse:
        del deployment, api_key
        self.requests.append(request)
        return ModelResponse(
            text=(
                '{"mode":"dispatch","confidence":0.92,"reason":"task can be split",'
                '"roles":["writer","reviewer"],"estimated_seconds":30,'
                '"estimated_cost_usd":"0.01","risk":"low"}'
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=6, total_tokens=16),
        )


@pytest.mark.asyncio
async def test_main_agent_mode_router_uses_configured_main_agent_model() -> None:
    transport = FakeTransport()

    async def get_main_agent_config() -> MainAgentConfigResponse:
        return MainAgentConfigResponse(
            model=MainAgentModelConfig(
                provider="deepseek",
                api_base="https://api.deepseek.com/v1",
                api_protocol="openai_compatible",
                upstream_model="deepseek-v4-flash",
                credential_ref="secret://main-agent",
                capabilities=["text", "structured_output"],
            )
        )

    async def capacity_factory(_deployments: tuple[Deployment, ...]) -> CapacityController:
        return ImmediateCapacity()

    router = _MainAgentModeRouter(
        get_config=get_main_agent_config,
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        redis_client=object(),
        transport=transport,
        capacity_factory=capacity_factory,
    )

    decision = await router.route("写一个活动策划，需要分工。")

    assert decision.status == "ready"
    assert decision.mode is not None
    assert decision.mode.value == "dispatch"
    assert decision.risk is RiskLevel.LOW
    assert len(transport.requests) == 1
    assert {request.logical_model for request in transport.requests} == {"main_agent"}


@pytest.mark.asyncio
async def test_main_agent_mode_router_inherits_registered_model_capabilities() -> None:
    transport = FakeTransport()

    async def get_main_agent_config() -> MainAgentConfigResponse:
        return MainAgentConfigResponse(
            model=MainAgentModelConfig(
                provider="minimax",
                api_base="https://api.minimax.chat/v1",
                api_protocol="openai_compatible",
                upstream_model="MiniMax-M3",
                credential_ref="secret://main-agent",
                capabilities=["text"],
            )
        )

    async def list_models() -> tuple[ModelDeploymentResponse, ...]:
        return (
            ModelDeploymentResponse(
                provider="minimax",
                api_base="https://api.minimax.chat/v1",
                api_protocol="openai_compatible",
                upstream_model="MiniMax-M3",
                logical_model="minimax",
                capabilities=["text", "structured_output", "tool_calling"],
                credential_ref="secret://main-agent",
                quota_scope="minimax-account",
                max_concurrency=1,
                target_utilization=0.8,
                reserved_capacity=0,
                id=uuid4(),
                effective_slots=1,
                saturation_policy="queue_first_then_fallback",
            ),
        )

    async def capacity_factory(_deployments: tuple[Deployment, ...]) -> CapacityController:
        assert _deployments[0].quota_scope_id == "minimax-account"
        assert _deployments[0].max_concurrency == 1
        return ImmediateCapacity()

    router = _MainAgentModeRouter(
        get_config=get_main_agent_config,
        list_models=list_models,
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        redis_client=object(),
        transport=transport,
        capacity_factory=capacity_factory,
    )

    decision = await router.route("写一份活动方案，需要策划和审核。")

    assert decision.status == "ready"
    assert decision.mode is not None
    assert decision.mode.value == "dispatch"
    assert len(transport.requests) == 1


def test_main_agent_context_window_is_inferred_from_model_family() -> None:
    assert _infer_main_agent_context_window_tokens("deepseek", "deepseek-v4-flash") == 128_000
    assert _infer_main_agent_context_window_tokens("anthropic", "claude-sonnet-4") == 200_000
    assert _infer_main_agent_context_window_tokens("openai", "gpt-5") == 400_000
    assert _infer_main_agent_context_window_tokens("custom", "unknown-small") == 32_768


@pytest.mark.asyncio
async def test_main_agent_context_window_getter_reads_configured_model() -> None:
    async def get_main_agent_config() -> MainAgentConfigResponse:
        return MainAgentConfigResponse(
            model=MainAgentModelConfig(
                provider="deepseek",
                api_base="https://api.deepseek.com/v1",
                api_protocol="openai_compatible",
                upstream_model="deepseek-v4-flash",
                credential_ref="secret://main-agent",
                capabilities=["text"],
            )
        )

    assert await _MainAgentContextWindowGetter(get_main_agent_config)() == 128_000
