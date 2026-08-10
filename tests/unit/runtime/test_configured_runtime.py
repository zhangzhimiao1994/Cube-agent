from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import CapacityLease
from agent_hub.models.gateway import CapacityController
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.contracts import EventKind, TaskContext
from agent_hub.runtime.defaults import (
    ConfigBackedDirectRuntime,
    ConfigBackedDiscussionRuntime,
    ConfigBackedDispatchRuntime,
    ConfigBackedHybridRuntime,
    UnavailableRuntime,
    _discussion_plan,
    _dispatch_plan,
    configured_runtime_registry,
)
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")


class FakeConfigService:
    def __init__(self, document: dict[str, object] | None) -> None:
        self.document = document

    async def get_current(self, tenant_id: UUID) -> ConfigRevision | None:
        assert tenant_id == TENANT_ID
        if self.document is None:
            return None
        return ConfigRevision(
            id=uuid4(),
            tenant_id=tenant_id,
            version=1,
            status=ConfigStatus.PUBLISHED,
            document=self.document,
            created_by=uuid4(),
            created_at=datetime.now(UTC),
        )


class FakeSecretService:
    def __init__(self) -> None:
        self.resolved: list[tuple[UUID, str]] = []

    async def resolve(self, tenant_id: UUID, reference: object) -> str:
        assert isinstance(reference, str)
        self.resolved.append((tenant_id, reference))
        return "sk-live"


class ImmediateCapacity:
    def __init__(self, deployments: tuple[Deployment, ...]) -> None:
        self.deployments = deployments
        self.recorded: list[bool] = []

    async def initialize(self) -> None:
        return None

    def validate_configuration(self, deployments: Sequence[Deployment]) -> None:
        assert tuple(deployments) == self.deployments

    async def acquire(
        self,
        candidates: Sequence[Deployment],
        wait_timeout: float,
        *,
        estimated_tokens: int,
    ) -> CapacityLease:
        del wait_timeout
        assert estimated_tokens > 0
        candidate = next(iter(candidates))
        assert isinstance(candidate, Deployment)
        return CapacityLease(
            id=str(uuid4()),
            deployment_id=candidate.id,
            quota_scope_id=candidate.quota_scope_id,
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
        del quota_scope_id, status_code, latency_seconds
        self.recorded.append(succeeded)


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[Deployment, ModelRequest, str]] = []

    async def complete(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> ModelResponse:
        self.calls.append((deployment, request, api_key))
        return ModelResponse(
            text="生产配置链路已接通",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


@pytest.mark.asyncio
async def test_config_backed_direct_runtime_uses_published_model_and_secret() -> None:
    transport = FakeTransport()
    secrets = FakeSecretService()
    capacities: list[ImmediateCapacity] = []
    runtime = ConfigBackedDirectRuntime(
        config_service=FakeConfigService(
            {
                "models": {
                    "main": {
                        "deployments": [
                            {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://22222222-2222-4222-8222-222222222222",
                                "quota_scope_id": "deepseek_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            }
                        ]
                    }
                },
                "agents": [],
            }
        ),  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        capacity_factory=lambda deployments: _remember_capacity(capacities, deployments),
        transport=transport,
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DIRECT,
                request="写一个生产环境 smoke test",
            )
        )
    ]

    assert [event.kind for event in events] == [
        EventKind.MODEL_STARTED,
        EventKind.ARTIFACT_CREATED,
        EventKind.CHECKPOINT_SAVED,
        EventKind.RUNTIME_COMPLETED,
    ]
    deployment, request, api_key = transport.calls[0]
    assert deployment.provider_model == "deepseek/deepseek-chat"
    assert request.logical_model == "main"
    assert api_key == "sk-live"
    assert secrets.resolved == [
        (TENANT_ID, "secret://22222222-2222-4222-8222-222222222222")
    ]
    assert capacities[0].recorded == [True]


@pytest.mark.asyncio
async def test_config_backed_direct_runtime_fails_explicitly_without_published_config() -> None:
    runtime = ConfigBackedDirectRuntime(
        config_service=FakeConfigService(None),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda deployments: ImmediateCapacity(deployments),
        transport=FakeTransport(),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DIRECT,
                request="hello",
            )
        )
    ]

    assert [(event.kind, event.reason) for event in events] == [
        (EventKind.RUNTIME_FAILED, "runtime_not_configured")
    ]


def _remember_capacity(
    capacities: list[ImmediateCapacity],
    deployments: tuple[Deployment, ...],
) -> CapacityController:
    capacity = ImmediateCapacity(deployments)
    capacities.append(capacity)
    return capacity


def test_dispatch_plan_accepts_localized_role_display_names_but_keeps_safe_ids() -> None:
    plan = _dispatch_plan(
        (
            RoleAssignment(
                id="director",
                role="导演",
                purpose=RolePurpose.EXPERTISE,
                mission="负责拆解目标、镜头语言和最终质量把关。",
                must_answer=("故事目标是什么？",),
                allowed_tools=(),
                forbidden_actions=("不要执行危险操作。",),
                skills=(),
                output_schema={"summary": "string"},
                model="main",
            ),
            RoleAssignment(
                id="copywriter",
                role="文案生成",
                purpose=RolePurpose.EXECUTE,
                mission="负责生成短剧文案和口播草稿。",
                must_answer=("文案是什么？",),
                allowed_tools=(),
                forbidden_actions=("不要执行危险操作。",),
                skills=(),
                output_schema={"summary": "string"},
                model="main",
            ),
        ),
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="写一个玄幻 AI 短剧文案",
        ),
    )

    assert [(agent.id, agent.role) for agent in plan.agents] == [
        ("director", "导演"),
        ("copywriter", "文案生成"),
        ("final_synthesizer", "Final Synthesizer"),
    ]
    assert [step.agent for step in plan.steps] == [
        "director",
        "copywriter",
        "final_synthesizer",
    ]


def test_discussion_plan_accepts_localized_role_display_names() -> None:
    plan = _discussion_plan(
        (
            RoleAssignment(
                id="director",
                role="导演",
                purpose=RolePurpose.EXPERTISE,
                mission="负责创意方向。",
                must_answer=("方向是什么？",),
                allowed_tools=(),
                forbidden_actions=("不要执行危险操作。",),
                skills=(),
                output_schema={"position": "string"},
                model="main",
            ),
            RoleAssignment(
                id="critic",
                role="审查员",
                purpose=RolePurpose.CRITIQUE,
                mission="负责审查风险。",
                must_answer=("风险是什么？",),
                allowed_tools=(),
                forbidden_actions=("不要执行危险操作。",),
                skills=(),
                output_schema={"position": "string"},
                model="main",
            ),
        ),
        "main",
    )

    assert [(participant.id, participant.role) for participant in plan.participants] == [
        ("director", "导演"),
        ("critic", "审查员"),
    ]


def test_configured_runtime_registry_registers_all_production_modes() -> None:
    registry = configured_runtime_registry(
        config_service=FakeConfigService(None),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        redis_client=object(),
        transport=FakeTransport(),
    )

    assert type(registry.get(TaskMode.DIRECT)) is ConfigBackedDirectRuntime
    assert type(registry.get(TaskMode.DISPATCH)) is ConfigBackedDispatchRuntime
    assert type(registry.get(TaskMode.DISCUSS)) is ConfigBackedDiscussionRuntime
    assert type(registry.get(TaskMode.HYBRID)) is ConfigBackedHybridRuntime
    assert not isinstance(registry.get(TaskMode.DISPATCH), UnavailableRuntime)
    assert not isinstance(registry.get(TaskMode.DISCUSS), UnavailableRuntime)
    assert not isinstance(registry.get(TaskMode.HYBRID), UnavailableRuntime)
