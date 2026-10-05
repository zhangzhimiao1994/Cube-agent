import asyncio
from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent_hub import app as app_module
from agent_hub.api.routers.admin import (
    InMemoryAdminResourceService,
    MainAgentConfigResponse,
    MainAgentModelConfig,
)
from agent_hub.app import _MainAgentModeRouter, create_app
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse
from agent_hub.runs.service import RunService, _safe_route
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from tests.api.test_foundation_api import (
    unwritable_default_workspace,  # noqa: F401 -- pytest fixture
)
from tests.unit.models.test_capacity import InMemoryCapacityRedis
from tests.unit.runs.test_conversation_mode import (
    ConversationModeRepository,
    RecordingQueue,
    WaitingRouter,
)
from tests.unit.test_app_wiring import (
    OTHER_TENANT_ID,
    TENANT_ID,
    FakeDatabase,
    FakeRedis,
    ImmediateCapacity,
    PublishedConfigService,
    valid_settings,
)

TASK = "Compare the alternatives and prepare a recommendation."


def platform_document() -> dict[str, Any]:
    def deployment(provider: str, model: str) -> dict[str, Any]:
        return {
            "provider": provider,
            "model": model,
            "credential_ref": f"secret://{provider}",
            "quota_scope_id": f"{provider}_account",
            "capabilities": ["text", "structured_output"],
        }

    return {
        "models": {
            "deepseek": {
                "deployments": [deployment("deepseek", "deepseek-chat")],
                "fallback_model": "sonnet",
            },
            "sonnet": {"deployments": [deployment("anthropic", "claude-sonnet-4-5")]},
        },
        "agents": [{"id": "reviewer", "role": "Reviewer", "prompt": "Review.", "model": "sonnet"}],
    }


class Secrets:
    async def resolve(self, tenant_id: UUID, reference: object) -> str:
        assert tenant_id == TENANT_ID
        assert reference in {"secret://deepseek", "secret://anthropic", "secret://main-agent"}
        return "test-key"


class Capacity(ImmediateCapacity):
    def validate_configuration(self, deployments: Sequence[Deployment]) -> None:
        assert deployments


class Transport:
    def __init__(self, *, failing: bool = False, expected_api_key: str = "test-key") -> None:
        self.calls: list[tuple[Deployment, ModelRequest]] = []
        self.api_keys: list[str] = []
        self.failing = failing
        self.expected_api_key = expected_api_key

    async def complete(
        self, deployment: Deployment, request: ModelRequest, api_key: str
    ) -> ModelResponse:
        assert api_key == self.expected_api_key
        self.api_keys.append(api_key)
        self.calls.append((deployment, request))
        if self.failing:
            raise ModelTransportError("unavailable", status_code=503)
        return ModelResponse(
            text=(
                '{"mode":"dispatch","confidence":0.92,"reason":"compare alternatives",'
                '"roles":["writer","reviewer"],"estimated_seconds":30,'
                '"estimated_cost_usd":"0.01","risk":"low"}'
            )
        )


class RouterFixture:
    def __init__(self, *, failing: bool = False) -> None:
        self.document = platform_document()
        self.config_service = PublishedConfigService(self.document)
        self.secrets = Secrets()
        self.transport = Transport(failing=failing)
        self.global_reads = 0
        self.capacity_models: list[tuple[str, ...]] = []
        self.main_config = MainAgentConfigResponse(
            model=MainAgentModelConfig(
                provider="anthropic",
                api_protocol="openai_compatible",
                api_base="https://global.invalid/v1",
                upstream_model="claude-sonnet-4-5",
                credential_ref="secret://main-agent",
                capabilities=["text", "structured_output"],
            )
        )

    async def get_main_config(self) -> MainAgentConfigResponse:
        self.global_reads += 1
        return self.main_config

    async def capacity(self, deployments: tuple[Deployment, ...]) -> Capacity:
        self.capacity_models.append(tuple(item.logical_model for item in deployments))
        return Capacity()

    def router(self, *, scoped: bool = True) -> _MainAgentModeRouter:
        kwargs: dict[str, Any] = {}
        if scoped:
            kwargs["get_current_config"] = self.config_service.get_current
        return _MainAgentModeRouter(
            get_config=self.get_main_config,
            secret_service=self.secrets,  # type: ignore[arg-type]
            tenant_id=TENANT_ID,
            redis_client=object(),
            transport=self.transport,
            capacity_factory=self.capacity,
            **kwargs,
        )


class TenantSecrets(Secrets):
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, object]] = []
        self.fingerprint_calls: list[tuple[UUID, object]] = []

    async def resolve(self, tenant_id: UUID, reference: object) -> str:
        assert isinstance(reference, str)
        self.calls.append((tenant_id, reference))
        return {
            (TENANT_ID, "secret://deepseek"): "tenant-a-key",
            (OTHER_TENANT_ID, "secret://tenant-b-deepseek"): "tenant-b-key",
        }[(tenant_id, reference)]

    async def fingerprint(self, tenant_id: UUID, reference: object) -> str:
        assert isinstance(reference, str)
        self.fingerprint_calls.append((tenant_id, reference))
        return {
            (TENANT_ID, "secret://deepseek"): "a" * 64,
            (OTHER_TENANT_ID, "secret://tenant-b-deepseek"): "b" * 64,
        }[(tenant_id, reference)]


class TenantRouterFixture(RouterFixture):
    def __init__(self) -> None:
        super().__init__()
        other_document = platform_document()
        deployment = other_document["models"]["deepseek"]["deployments"][0]
        deployment.update(
            model="tenant-b-chat",
            credential_ref="secret://tenant-b-deepseek",
            quota_scope_id="tenant_b_account",
        )
        self.documents = {TENANT_ID: self.document, OTHER_TENANT_ID: other_document}
        self.config_reads: list[UUID] = []
        self.config_service = self  # type: ignore[assignment]
        self.secrets: TenantSecrets = TenantSecrets()
        self.transport = Transport(expected_api_key="tenant-b-key")

    async def get_current(self, tenant_id: UUID) -> ConfigRevision:
        self.config_reads.append(tenant_id)
        return ConfigRevision(
            id=uuid4(),
            tenant_id=tenant_id,
            version=1,
            status=ConfigStatus.PUBLISHED,
            document=self.documents[tenant_id],
            created_by=tenant_id,
            created_at=datetime.now(UTC),
        )


@pytest.mark.parametrize("through_submit", [False, True], ids=["router", "auto-submit"])
@pytest.mark.parametrize("default_capacity", [False, True], ids=["custom-pool", "default-pool"])
async def test_scoped_routing_uses_only_request_tenant_config_and_credentials(
    through_submit: bool,
    default_capacity: bool,
) -> None:
    fixture = TenantRouterFixture()
    snapshots = deepcopy(fixture.documents)
    redis = InMemoryCapacityRedis()
    router = (
        _MainAgentModeRouter(
            get_config=fixture.get_main_config,
            get_current_config=fixture.get_current,
            secret_service=fixture.secrets,  # type: ignore[arg-type]
            tenant_id=TENANT_ID,
            redis_client=redis,
            transport=fixture.transport,
        )
        if default_capacity
        else fixture.router()
    )
    if through_submit:
        repository = ConversationModeRepository(None)
        service = RunService(
            repository,  # type: ignore[arg-type]
            runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
            router=router,
            task_queue=RecordingQueue(),
        )
        submitted = await service.submit(
            tenant_id=OTHER_TENANT_ID,
            actor_id=uuid4(),
            message=TASK,
            mode=TaskMode.AUTO,
            allowed_models=("deepseek",),
            direct_model="deepseek",
        )
        assert submitted.status is RunStatus.QUEUED
        assert submitted.mode is TaskMode.DISPATCH
        assert repository.record is not None
        assert repository.record.tenant_id == OTHER_TENANT_ID
        routing = repository.created[0]["routing_decision"]
        assert isinstance(routing, dict)
        assert {item["logical_model"] for item in routing["assessments"]} == {"deepseek"}
    else:
        decision = await router.route_scoped(
            TASK,
            tenant_id=OTHER_TENANT_ID,
            allowed_models=("deepseek",),
            logical_model="deepseek",
        )
        assert decision.status == "ready"
        assert decision.mode is TaskMode.DISPATCH
        assert {item.logical_model for item in decision.assessments} == {"deepseek"}

    assert fixture.config_reads == [OTHER_TENANT_ID]
    assert fixture.secrets.calls == [(OTHER_TENANT_ID, "secret://tenant-b-deepseek")]
    assert fixture.transport.api_keys == ["tenant-b-key"]
    assert [(d.logical_model, d.provider_model) for d, _ in fixture.transport.calls] == [
        ("deepseek", "deepseek/tenant-b-chat"),
    ]
    assert fixture.transport.calls[0][0].quota_scope_id == "tenant_b_account"
    if default_capacity:
        assert fixture.secrets.fingerprint_calls == [
            (OTHER_TENANT_ID, "secret://tenant-b-deepseek"),
        ]
        assert redis.acquire_calls == 1
        assert fixture.capacity_models == []
    else:
        assert fixture.secrets.fingerprint_calls == []
        assert fixture.capacity_models == [("deepseek",)]
    assert fixture.global_reads == 0
    assert fixture.documents == snapshots


async def test_scoped_router_requires_explicit_tenant_id() -> None:
    fixture = RouterFixture()
    with pytest.raises(TypeError, match="tenant_id"):
        await fixture.router().route_scoped(  # type: ignore[call-arg]
            TASK,
            allowed_models=("deepseek",),
            logical_model=None,
        )
    assert fixture.transport.calls == []
    assert fixture.global_reads == 0


@pytest.mark.parametrize("logical_model", [None, "deepseek"])
async def test_scoped_router_uses_deepseek_without_mutating_global_config(
    logical_model: str | None,
) -> None:
    fixture = RouterFixture()
    document_snapshot = deepcopy(fixture.document)
    main_snapshot = fixture.main_config.model_dump()
    router = fixture.router()

    decision = await router.route_scoped(
        TASK,
        tenant_id=TENANT_ID,
        allowed_models=("deepseek",),
        logical_model=logical_model,
    )

    assert decision.status == "ready"
    assert decision.mode is TaskMode.DISPATCH
    assert {item.logical_model for item in decision.assessments} == {"deepseek"}
    assert {item.deployment_id for item in decision.assessments} == {"deepseek_1"}
    assert fixture.capacity_models == [("deepseek",)]
    assert [(d.logical_model, d.provider_model) for d, _ in fixture.transport.calls] == [
        ("deepseek", "deepseek/deepseek-chat"),
    ]
    assert fixture.global_reads == 0
    assert fixture.document == document_snapshot
    assert fixture.main_config.model_dump() == main_snapshot

    legacy = await router.route(TASK)
    assert legacy.status == "ready"
    assert {item.logical_model for item in legacy.assessments} == {"main_agent"}
    assert fixture.transport.calls[-1][0].provider_model == "anthropic/claude-sonnet-4-5"
    assert fixture.global_reads == 1


async def test_scoped_router_uses_requested_logical_model_and_current_revision() -> None:
    fixture = RouterFixture()
    router = fixture.router()
    first = await router.route_scoped(
        TASK,
        tenant_id=TENANT_ID,
        allowed_models=("deepseek", "sonnet"),
        logical_model="sonnet",
    )
    fixture.document["models"]["deepseek"]["deployments"][0]["model"] = "deepseek-new"
    second = await router.route_scoped(
        TASK,
        tenant_id=TENANT_ID,
        allowed_models=("deepseek",),
        logical_model=None,
    )

    assert {item.logical_model for item in first.assessments} == {"sonnet"}
    assert {item.logical_model for item in second.assessments} == {"deepseek"}
    assert [d.provider_model for d, _ in fixture.transport.calls] == [
        "anthropic/claude-sonnet-4-5",
        "deepseek/deepseek-new",
    ]
    assert fixture.global_reads == 0


@pytest.mark.parametrize(
    "allowed_models,logical_model",
    [((), None), (("unknown",), None), (("deepseek",), "sonnet")],
)
async def test_invalid_scope_fails_soft_before_capacity_or_global_lookup(
    allowed_models: tuple[str, ...],
    logical_model: str | None,
) -> None:
    fixture = RouterFixture()
    decision = await fixture.router().route_scoped(
        TASK,
        tenant_id=TENANT_ID,
        allowed_models=allowed_models,
        logical_model=logical_model,
    )

    assert decision.status == "waiting_user_mode"
    assert decision.assessments == ()
    assert fixture.capacity_models == []
    assert fixture.transport.calls == []
    assert fixture.global_reads == 0


async def test_scoped_router_failure_never_uses_excluded_fallback_or_global_model() -> None:
    fixture = RouterFixture(failing=True)
    snapshot = deepcopy(fixture.document)
    decision = await fixture.router().route_scoped(
        TASK,
        tenant_id=TENANT_ID,
        allowed_models=("deepseek",),
        logical_model="deepseek",
    )

    assert decision.status == "waiting_user_mode"
    assert decision.assessments == ()
    assert fixture.transport.calls
    assert {d.logical_model for d, _ in fixture.transport.calls} == {"deepseek"}
    assert fixture.capacity_models == [("deepseek",)]
    assert fixture.global_reads == 0
    assert fixture.document == snapshot


async def test_scoped_router_without_config_getter_does_not_read_global_config() -> None:
    fixture = RouterFixture()
    decision = await fixture.router(scoped=False).route_scoped(
        TASK,
        tenant_id=TENANT_ID,
        allowed_models=("deepseek",),
        logical_model=None,
    )
    assert decision.status == "waiting_user_mode"
    assert decision.assessments == ()
    assert fixture.global_reads == 0
    assert fixture.transport.calls == []


@pytest.mark.parametrize("state", ["missing", "invalid", "failed"])
async def test_scoped_config_read_failure_never_uses_global_config(state: str) -> None:
    fixture = RouterFixture()

    async def get_current(tenant_id: UUID) -> object | None:
        assert tenant_id == TENANT_ID
        if state == "failed":
            raise RuntimeError("config unavailable")
        return None if state == "missing" else object()

    fixture.config_service.get_current = get_current  # type: ignore[method-assign, assignment]
    decision = await fixture.router().route_scoped(
        TASK,
        tenant_id=TENANT_ID,
        allowed_models=("deepseek",),
        logical_model=None,
    )
    assert decision.status == "waiting_user_mode"
    assert decision.assessments == ()
    assert fixture.capacity_models == []
    assert fixture.global_reads == 0
    assert fixture.transport.calls == []


async def test_safe_route_without_scoped_support_never_calls_legacy_router() -> None:
    router = WaitingRouter()
    result = await _safe_route(
        router,
        TASK,
        timeout_seconds=1,
        tenant_id=TENANT_ID,
        allowed_models=("deepseek",),
        logical_model="deepseek",
    )
    assert result is None
    assert router.calls == 0


@pytest.mark.parametrize("error", [RuntimeError("failed"), TimeoutError("timed out")])
async def test_safe_route_scoped_failure_never_calls_legacy_router(error: Exception) -> None:
    class FailingScopedRouter(WaitingRouter):
        async def route_scoped(
            self,
            task_text: object,
            *,
            tenant_id: UUID,
            allowed_models: tuple[str, ...],
            logical_model: str | None,
        ) -> Any:
            assert task_text == TASK
            assert tenant_id == TENANT_ID
            assert allowed_models == ("deepseek",)
            assert logical_model == "deepseek"
            raise error

    router = FailingScopedRouter()
    result = await _safe_route(
        router,
        TASK,
        timeout_seconds=1,
        tenant_id=TENANT_ID,
        allowed_models=("deepseek",),
        logical_model="deepseek",
    )
    assert result is None
    assert router.calls == 0


async def test_safe_route_scoped_timeout_never_calls_legacy_router() -> None:
    class BlockingScopedRouter(WaitingRouter):
        async def route_scoped(
            self,
            task_text: object,
            *,
            tenant_id: UUID,
            allowed_models: tuple[str, ...],
            logical_model: str | None,
        ) -> Any:
            await asyncio.Future()

    router = BlockingScopedRouter()
    result = await _safe_route(
        router,
        TASK,
        timeout_seconds=0,
        tenant_id=TENANT_ID,
        allowed_models=("deepseek",),
        logical_model=None,
    )
    assert result is None
    assert router.calls == 0


@pytest.mark.parametrize("explicit_none", [False, True], ids=["omitted", "explicit-none"])
async def test_safe_route_without_tenant_never_invokes_scoped_or_legacy_router(
    explicit_none: bool,
) -> None:
    router = WaitingRouter()
    scoped = AsyncMock(return_value=None)
    router.route_scoped = scoped  # type: ignore[attr-defined]
    kwargs: dict[str, Any] = {"tenant_id": None} if explicit_none else {}
    result = await _safe_route(
        router,
        TASK,
        timeout_seconds=1,
        allowed_models=("deepseek",),
        logical_model="deepseek",
        **kwargs,
    )
    assert result is None
    scoped.assert_not_called()
    assert router.calls == 0


async def test_safe_route_empty_scope_preserves_legacy_route() -> None:
    router = WaitingRouter()
    result = await _safe_route(
        router,
        TASK,
        timeout_seconds=1,
        allowed_models=(),
        logical_model="deepseek",
    )
    assert result is not None
    assert result.clarification_reason == "classification_unavailable"
    assert router.calls == 1


@pytest.mark.parametrize(
    "allowed_models,logical_model,expected_model",
    [(("deepseek",), "deepseek", "deepseek"), (("deepseek", "sonnet"), "sonnet", "sonnet")],
)
async def test_auto_submission_routes_scoped_and_persists_actual_model_metadata(
    allowed_models: tuple[str, ...],
    logical_model: str,
    expected_model: str,
) -> None:
    fixture = RouterFixture()
    repository = ConversationModeRepository(None)
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=fixture.router(),
        task_queue=RecordingQueue(),
    )
    submitted = await service.submit(
        tenant_id=TENANT_ID,
        actor_id=uuid4(),
        message=TASK,
        mode=TaskMode.AUTO,
        allowed_models=allowed_models,
        direct_model=logical_model,
    )
    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DISPATCH
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert {item["logical_model"] for item in routing["assessments"]} == {expected_model}
    assert fixture.capacity_models == [allowed_models]
    assert fixture.global_reads == 0


@pytest.mark.parametrize("failing", [False, True])
async def test_auto_submission_unavailable_scope_keeps_local_resolution_metadata(
    failing: bool,
) -> None:
    fixture = RouterFixture(failing=True)
    repository = ConversationModeRepository(None)
    legacy_router = WaitingRouter()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=fixture.router() if failing else legacy_router,
        task_queue=RecordingQueue(),
    )
    submitted = await service.submit(
        tenant_id=TENANT_ID,
        actor_id=uuid4(),
        message=TASK,
        mode=TaskMode.AUTO,
        allowed_models=("deepseek",),
        direct_model="deepseek",
    )
    assert submitted.status is RunStatus.QUEUED
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["reason"] == "main_agent_local_resolution"
    assert "assessments" not in routing
    assert legacy_router.calls == 0
    assert fixture.global_reads == 0
    assert all(d.logical_model == "deepseek" for d, _ in fixture.transport.calls)


@pytest.mark.usefixtures("unwritable_default_workspace")
def test_app_wires_same_tenant_current_config_to_scoped_router(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = RouterFixture()
    monkeypatch.setattr(app_module, "SecretService", lambda *args: Secrets())
    monkeypatch.setattr(app_module, "LiteLLMClient", lambda: fixture.transport)

    async def capacity(
        self: object,
        deployments: tuple[Deployment, ...],
        *,
        tenant_id: UUID | None = None,
    ) -> Capacity:
        assert tenant_id == TENANT_ID
        return await fixture.capacity(deployments)

    monkeypatch.setattr(_MainAgentModeRouter, "_default_capacity", capacity)
    application = create_app(
        settings=valid_settings(tmp_path, project_workspace_dir=tmp_path / "workspaces"),
        database=FakeDatabase(),
        redis_client=FakeRedis(),
        auth_service=object(),
        rate_limiter=object(),
        config_service=fixture.config_service,
        admin_resource_service=InMemoryAdminResourceService(),
        user_admin_service=object(),
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        task_queue=RecordingQueue(),
    )
    with TestClient(application) as client:
        assert client.portal is not None
        decision = client.portal.call(
            partial(
                application.state.mode_router.route_scoped,
                TASK,
                tenant_id=TENANT_ID,
                allowed_models=("deepseek",),
                logical_model="deepseek",
            )
        )
    assert decision.status == "ready"
    assert {item.logical_model for item in decision.assessments} == {"deepseek"}
    assert fixture.capacity_models == [("deepseek",)]
