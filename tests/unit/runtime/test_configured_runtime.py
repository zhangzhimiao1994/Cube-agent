from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

import agent_hub.runtime.defaults as defaults_module
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.config.schema import PlatformConfig
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
    _dispatch_parallelism,
    _dispatch_plan,
    _select_logical_model_for_role,
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
        self.fingerprinted: list[tuple[UUID, str]] = []

    async def resolve(self, tenant_id: UUID, reference: object) -> str:
        assert isinstance(reference, str)
        self.resolved.append((tenant_id, reference))
        return "sk-live"

    async def fingerprint(self, tenant_id: UUID, reference: object) -> str:
        assert isinstance(reference, str)
        self.fingerprinted.append((tenant_id, reference))
        return "a" * 64


class ImmediateCapacity:
    def __init__(self, deployments: tuple[Deployment, ...]) -> None:
        self.deployments = deployments
        self.recorded: list[bool] = []
        self.wait_timeouts: list[float] = []

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
        self.wait_timeouts.append(wait_timeout)
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
        capacity_factory=lambda tenant_id, deployments: _remember_capacity(
            capacities, tenant_id, deployments
        ),
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
    assert capacities[0].wait_timeouts == [60.0]
    assert secrets.resolved == [
        (TENANT_ID, "secret://22222222-2222-4222-8222-222222222222")
    ]
    assert capacities[0].recorded == [True]


@pytest.mark.asyncio
async def test_config_backed_direct_runtime_uses_per_run_direct_model_override() -> None:
    transport = FakeTransport()
    runtime = ConfigBackedDirectRuntime(
        config_service=FakeConfigService(
            {
                "models": {
                    "main": {
                        "deployments": [
                            {
                                "provider": "deepseek",
                                "model": "deepseek-v4-flash",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://main",
                                "quota_scope_id": "deepseek_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            }
                        ]
                    },
                    "coder": {
                        "deployments": [
                            {
                                "provider": "qwen",
                                "model": "qwen-max",
                                "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                                "credential_ref": "secret://coder",
                                "quota_scope_id": "qwen_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            }
                        ]
                    },
                },
                "agents": [],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id, deployments
        ),
        transport=transport,
    )

    [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DIRECT,
                request="直接回答",
                routing_decision={"direct_model": "coder"},
            )
        )
    ]

    deployment, request, _api_key = transport.calls[0]
    assert request.logical_model == "coder"
    assert deployment.provider_model == "qwen/qwen-max"


@pytest.mark.asyncio
async def test_config_backed_direct_runtime_fails_explicitly_without_published_config() -> None:
    runtime = ConfigBackedDirectRuntime(
        config_service=FakeConfigService(None),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id, deployments
        ),
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


async def _remember_capacity(
    capacities: list[ImmediateCapacity],
    tenant_id: UUID,
    deployments: tuple[Deployment, ...],
) -> CapacityController:
    assert tenant_id == TENANT_ID
    capacity = ImmediateCapacity(deployments)
    capacities.append(capacity)
    return capacity


async def _immediate_capacity(
    tenant_id: UUID,
    deployments: tuple[Deployment, ...],
) -> CapacityController:
    assert tenant_id == TENANT_ID
    return ImmediateCapacity(deployments)


@pytest.mark.asyncio
async def test_configured_runtime_registry_supplies_secret_fingerprints_to_capacity_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[object] = []
    secret_ref = "secret://33333333-3333-4333-8333-333333333333"

    class SpyCapacityPool(ImmediateCapacity):
        def __init__(
            self,
            redis_client: object,
            *,
            deployments: tuple[Deployment, ...],
            credentials: object | None = None,
        ) -> None:
            del redis_client
            if credentials is None:
                raise AssertionError("configured runtime must pass credential fingerprints")
            self.credentials = credentials
            created.append(self)
            super().__init__(tuple(deployments))

    monkeypatch.setattr(defaults_module, "CapacityPool", SpyCapacityPool)
    secrets = FakeSecretService()
    registry = configured_runtime_registry(
        config_service=FakeConfigService(
            {
                "models": {
                    "main": {
                        "deployments": [
                            {
                                "provider": "minimax",
                                "model": "MiniMax-M3",
                                "api_base": "https://api.minimax.chat/v1",
                                "credential_ref": secret_ref,
                                "quota_scope_id": "minimax_account",
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
        redis_client=object(),
        transport=FakeTransport(),
    )

    events = [
        event
        async for event in registry.get(TaskMode.DIRECT).run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DIRECT,
                request="hello",
            )
        )
    ]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert secrets.fingerprinted == [(TENANT_ID, secret_ref)]
    assert len(created) == 1
    credentials = created[0].credentials  # type: ignore[attr-defined]
    assert credentials.fingerprint_for(secret_ref) == "a" * 64


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
    assert plan.max_parallelism == 1


def test_dispatch_plan_preserves_selected_roles_and_controls_concurrency() -> None:
    roles = tuple(
        RoleAssignment(
            id=f"role_{index}",
            role=f"Role {index}",
            purpose=RolePurpose.EXECUTE,
            mission=f"Handle slice {index}",
            must_answer=(f"What did role {index} produce?",),
            allowed_tools=(),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        )
        for index in range(6)
    )

    plan = _dispatch_plan(
        roles,
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Answer briefly.",
        ),
    )

    assert [agent.id for agent in plan.agents] == [
        "role_0",
        "role_1",
        "role_2",
        "role_3",
        "role_4",
        "role_5",
        "final_synthesizer",
    ]
    assert plan.max_parallelism == 1
    assert all(step.token_budget == 16_384 for step in plan.steps)
    assert plan.total_token_budget == 16_384


def test_dispatch_plan_reserves_more_time_for_final_synthesis() -> None:
    roles = tuple(
        RoleAssignment(
            id=f"role_{index}",
            role=f"Role {index}",
            purpose=RolePurpose.EXECUTE,
            mission=f"Handle slice {index}",
            must_answer=(f"What did role {index} produce?",),
            allowed_tools=(),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        )
        for index in range(4)
    )

    plan = _dispatch_plan(
        roles,
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Create an execution plan.",
            timeout_seconds=300,
        ),
    )

    role_timeouts = [
        step.timeout_seconds for step in plan.steps if not step.final_synthesizer
    ]
    final_step = next(step for step in plan.steps if step.final_synthesizer)
    assert min(role_timeouts) >= 45
    assert final_step.timeout_seconds >= 120


def test_role_model_selection_uses_role_and_task_capabilities_not_user_choice() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "main": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-v4-flash",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://main",
                            "quota_scope_id": "deepseek",
                            "max_concurrency": 4,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text"],
                        }
                    ]
                },
                "coder": {
                    "deployments": [
                        {
                            "provider": "qwen",
                            "model": "qwen-coder-plus",
                            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            "credential_ref": "secret://coder",
                            "quota_scope_id": "qwen",
                            "max_concurrency": 8,
                            "target_utilization": 0.8,
                            "reserved_slots": 1,
                            "capabilities": ["text", "tool_calling"],
                        }
                    ]
                },
                "creative": {
                    "deployments": [
                        {
                            "provider": "kimi",
                            "model": "kimi-k2-latest",
                            "api_base": "https://api.moonshot.cn/v1",
                            "credential_ref": "secret://creative",
                            "quota_scope_id": "kimi",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text"],
                        }
                    ]
                },
                "analyst": {
                    "deployments": [
                        {
                            "provider": "anthropic",
                            "model": "claude-sonnet-4-5",
                            "api_base": "https://api.anthropic.com/v1/messages",
                            "credential_ref": "secret://analyst",
                            "quota_scope_id": "claude",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "structured_output"],
                        }
                    ]
                },
            },
            "agents": [],
        }
    )

    assert (
        _select_logical_model_for_role(
            RoleAssignment(
                id="web_engineer",
                role="网页工程师",
                purpose=RolePurpose.EXECUTE,
                mission="把调研结论落地成可部署网页代码。",
                must_answer=("实现了什么？",),
                allowed_tools=(),
                forbidden_actions=("不要执行危险操作。",),
                skills=(),
                output_schema={},
                model="main",
            ),
            config,
            default_model="main",
            task="调研产品并制作一个网页原型。",
        )
        == "coder"
    )
    assert (
        _select_logical_model_for_role(
            RoleAssignment(
                id="copywriter",
                role="文案生成",
                purpose=RolePurpose.EXECUTE,
                mission="生成短视频口播脚本和即梦提示词。",
                must_answer=("文案是什么？",),
                allowed_tools=(),
                forbidden_actions=("不要执行危险操作。",),
                skills=(),
                output_schema={},
                model="main",
            ),
            config,
            default_model="main",
            task="生成玄幻 AI 短剧提示词。",
        )
        == "creative"
    )
    assert (
        _select_logical_model_for_role(
            RoleAssignment(
                id="economic_analyst",
                role="经济分析师",
                purpose=RolePurpose.EXPERTISE,
                mission="分析市场、成本和风险。",
                must_answer=("风险是什么？",),
                allowed_tools=(),
                forbidden_actions=("不要执行危险操作。",),
                skills=(),
                output_schema={},
                model="main",
            ),
            config,
            default_model="main",
            task="调研产品机会并给出市场分析。",
        )
        == "analyst"
    )


def test_dispatch_parallelism_uses_model_capacity_without_unbounded_fanout() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "main": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-v4-flash",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "deepseek-key",
                            "quota_scope_id": "deepseek-account",
                            "max_concurrency": 32,
                            "target_utilization": 0.75,
                            "reserved_slots": 2,
                        }
                    ]
                }
            },
            "agents": [],
        }
    )

    assert _dispatch_parallelism(config, "main") == 16


def test_dispatch_parallelism_stays_serial_for_single_slot_model() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "main": {
                    "deployments": [
                        {
                            "provider": "openai-compatible",
                            "model": "custom-model",
                            "api_base": "https://example.com/v1",
                            "credential_ref": "relay-key",
                            "quota_scope_id": "relay-account",
                            "max_concurrency": 1,
                        }
                    ]
                }
            },
            "agents": [],
        }
    )

    assert _dispatch_parallelism(config, "main") == 1


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


def test_discussion_plan_uses_bounded_generation_limits() -> None:
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
                model="creative",
            ),
            RoleAssignment(
                id="reviewer",
                role="审查员",
                purpose=RolePurpose.CRITIQUE,
                mission="负责审查风险。",
                must_answer=("风险是什么？",),
                allowed_tools=(),
                forbidden_actions=("不要执行危险操作。",),
                skills=(),
                output_schema={"position": "string"},
                model="review",
            ),
        ),
        "main",
    )

    assert all(participant.max_output_tokens <= 1536 for participant in plan.participants)
    assert plan.selector_max_output_tokens <= 512


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
