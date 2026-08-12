from pathlib import Path
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.responses import FileResponse

from agent_hub.api.routers.admin import (
    MainAgentConfigResponse,
    MainAgentModelConfig,
    ModelDeploymentResponse,
)
from agent_hub.app import _MainAgentModeRouter, _web_ui_response, create_app
from agent_hub.models.capacity import CapacityLease
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, TokenUsage
from agent_hub.routing.types import RiskLevel


TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")


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

    def validate_configuration(self, deployments: tuple[Deployment, ...]) -> None:
        assert len(deployments) == 1

    async def acquire(
        self,
        candidates: tuple[Deployment, ...],
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

    async def capacity_factory(_deployments: tuple[Deployment, ...]) -> ImmediateCapacity:
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

    async def capacity_factory(_deployments: tuple[Deployment, ...]) -> ImmediateCapacity:
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
    assert decision.mode.value == "dispatch"
    assert len(transport.requests) == 1
