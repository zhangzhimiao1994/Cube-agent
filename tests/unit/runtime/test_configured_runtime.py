import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, cast
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest

import agent_hub.runtime.defaults as defaults_module
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.config.schema import PlatformConfig
from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import CapacityLease
from agent_hub.models.gateway import CapacityController
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.defaults import (
    ConfigBackedDirectRuntime,
    ConfigBackedDiscussionRuntime,
    ConfigBackedDispatchRuntime,
    ConfigBackedHybridRuntime,
    UnavailableRuntime,
    _assign_models_to_roles,
    _discussion_plan,
    _dispatch_parallelism,
    _dispatch_plan,
    _select_logical_model_for_role,
    _selected_config_role_assignments,
    configured_runtime_registry,
)
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose, TaskProfile

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")


def test_python_project_zip_request_is_profiled_as_software() -> None:
    profiles = defaults_module._task_profiles(
        "生成一个最简单的 hello world Python 项目。必须产出可下载 zip，"
        "zip 内至少包含 main.py，main.py 运行后输出 hello world。"
    )

    assert TaskProfile.SOFTWARE in profiles


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


class FakeCapabilityAvailability:
    def __init__(self, available: set[str]) -> None:
        self.available = available

    def is_replay_safe(self, name: str) -> bool:
        return name in {"read_context", "calculator", "calculator_evaluate", "workspace_read"}

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        assert tenant_id == TENANT_ID
        return name in self.available

    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        del tenant_id, run_id, actor, name, arguments, idempotency_key
        return {}


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


class ProbeDispatchRuntime:
    instances: ClassVar[list["ProbeDispatchRuntime"]] = []

    def __init__(
        self,
        gateway: object,
        plan: object,
        *,
        capability_gateway: object | None = None,
    ) -> None:
        del gateway, capability_gateway
        self.plan = plan
        self.contexts: list[TaskContext] = []
        self.instances.append(self)

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.contexts.append(context)
        yield RunEvent(
            kind=EventKind.RUNTIME_COMPLETED,
            sequence=1,
            run_id=context.run_id,
            reason="probe_complete",
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError(f"not used: {checkpoint.id}")

    async def cancel(self) -> None:
        return None


class ProbeDiscussionRuntime(ProbeDispatchRuntime):
    instances: ClassVar[list["ProbeDispatchRuntime"]] = []

    @property
    def participant_ids(self) -> tuple[str, ...]:
        return tuple(participant.id for participant in self.plan.participants)  # type: ignore[attr-defined]


class ProbeHybridRuntime(ProbeDispatchRuntime):
    instances: ClassVar[list["ProbeDispatchRuntime"]] = []

    def __init__(self, dispatch: object, discussion: object, direct: object) -> None:
        del dispatch, discussion, direct
        self.contexts: list[TaskContext] = []
        self.instances.append(self)


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
    assert secrets.resolved == [(TENANT_ID, "secret://22222222-2222-4222-8222-222222222222")]
    assert capacities[0].recorded == [True]


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_emits_main_agent_role_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDispatchRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "CrewDispatchRuntime", ProbeDispatchRuntime)
    runtime = ConfigBackedDispatchRuntime(
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
                    "creative": {
                        "deployments": [
                            {
                                "provider": "kimi",
                                "model": "kimi-k2-latest",
                                "api_base": "https://api.moonshot.cn/v1",
                                "credential_ref": "secret://creative",
                                "quota_scope_id": "kimi_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            }
                        ]
                    },
                },
                "agents": [
                    {
                        "id": "copywriter",
                        "role": "Copywriter",
                        "prompt": "Draft campaign copy.",
                        "model": "creative",
                        "skills": [],
                    }
                ],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(tenant_id, deployments),
        transport=FakeTransport(),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Draft a launch campaign.",
                routing_decision={
                    "selected_agent_ids": ("copywriter",),
                    "main_agent_model": "main",
                },
            )
        )
    ]

    assert events[0].kind is EventKind.STEP_STARTED
    assert events[0].actor == "main_agent"
    assert events[0].step_id == "main_agent_plan"
    assert events[0].payload["mode"] == "dispatch"
    assert events[0].payload["main_agent_model"] == "main"
    assert events[0].payload["roles"] == (
        {
            "id": "copywriter",
            "role": "Copywriter",
            "purpose": "execute",
            "logical_model": "creative",
            "tools": (),
        },
        {
            "id": "final_synthesizer",
            "role": "Final Synthesizer",
            "purpose": "synthesize",
            "logical_model": "main",
            "tools": (),
        },
    )
    assert events[0].payload["steps"] == (
        {
            "id": "copywriter_step",
            "agent": "copywriter",
            "depends_on": (),
            "final_synthesizer": False,
            "tools": (),
        },
        {
            "id": "final_response_step",
            "agent": "final_synthesizer",
            "depends_on": ("copywriter_step",),
            "final_synthesizer": True,
            "tools": (),
        },
    )
    assert events[1].kind is EventKind.RUNTIME_COMPLETED
    assert events[1].sequence == 2


@pytest.mark.asyncio
async def test_config_backed_hybrid_runtime_emits_main_agent_role_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeHybridRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "HybridRuntime", ProbeHybridRuntime)
    runtime = ConfigBackedHybridRuntime(
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
                    "creative": {
                        "deployments": [
                            {
                                "provider": "kimi",
                                "model": "kimi-k2-latest",
                                "api_base": "https://api.moonshot.cn/v1",
                                "credential_ref": "secret://creative",
                                "quota_scope_id": "kimi_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            }
                        ]
                    },
                },
                "agents": [
                    {
                        "id": "copywriter",
                        "role": "Copywriter",
                        "prompt": "Draft copy.",
                        "model": "creative",
                        "skills": [],
                    },
                    {
                        "id": "reviewer",
                        "role": "Reviewer",
                        "prompt": "Review copy.",
                        "model": "main",
                        "skills": [],
                    },
                ],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(tenant_id, deployments),
        transport=FakeTransport(),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.HYBRID,
                request="Draft and review a campaign.",
                routing_decision={
                    "selected_agent_ids": ("copywriter", "reviewer"),
                    "main_agent_model": "main",
                },
            )
        )
    ]

    assert events[0].kind is EventKind.STEP_STARTED
    assert events[0].actor == "main_agent"
    assert events[0].step_id == "main_agent_plan"
    assert events[0].payload["mode"] == "hybrid"
    roles = events[0].payload["roles"]
    assert isinstance(roles, tuple)
    roles = cast(tuple[Mapping[str, JsonValue], ...], roles)
    assert {role["id"] for role in roles} >= {"copywriter", "reviewer", "final_synthesizer"}
    assert any(
        role["id"] == "copywriter"
        and role["purpose"] == "execute"
        and isinstance(role["logical_model"], str)
        and role["logical_model"]
        for role in roles
    )
    assert any(
        role["id"] == "reviewer"
        and role["purpose"] == "expertise"
        and isinstance(role["logical_model"], str)
        and role["logical_model"]
        for role in roles
    )
    assert events[1].kind is EventKind.RUNTIME_COMPLETED
    assert events[1].sequence == 2


@pytest.mark.asyncio
async def test_config_backed_discussion_runtime_emits_main_agent_role_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDiscussionRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "AutoGenDiscussionRuntime", ProbeDiscussionRuntime)
    runtime = ConfigBackedDiscussionRuntime(
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
                    "review": {
                        "deployments": [
                            {
                                "provider": "qwen",
                                "model": "qwen-max",
                                "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                                "credential_ref": "secret://review",
                                "quota_scope_id": "qwen_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            }
                        ]
                    },
                },
                "agents": [
                    {
                        "id": "strategist",
                        "role": "Strategist",
                        "prompt": "Find the strongest option.",
                        "model": "main",
                        "skills": [],
                    },
                    {
                        "id": "reviewer",
                        "role": "Reviewer",
                        "prompt": "Review risk and gaps.",
                        "model": "review",
                        "skills": [],
                    },
                ],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(tenant_id, deployments),
        transport=FakeTransport(),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISCUSS,
                request="Compare two launch options.",
                routing_decision={
                    "selected_agent_ids": ("strategist", "reviewer"),
                    "main_agent_model": "main",
                },
            )
        )
    ]

    assert events[0].kind is EventKind.STEP_STARTED
    assert events[0].actor == "main_agent"
    assert events[0].step_id == "main_agent_plan"
    assert events[0].payload["mode"] == "discuss"
    assert events[0].payload["roles"] == (
        {
            "id": "strategist",
            "role": "Strategist",
            "purpose": "expertise",
            "logical_model": "main",
            "tools": (),
        },
        {
            "id": "reviewer",
            "role": "Reviewer",
            "purpose": "expertise",
            "logical_model": "review",
            "tools": (),
        },
    )
    assert events[0].payload["steps"] == (
        {
            "id": "discussion",
            "agent": "strategist",
            "depends_on": (),
            "final_synthesizer": False,
            "tools": (),
        },
        {
            "id": "discussion",
            "agent": "reviewer",
            "depends_on": (),
            "final_synthesizer": False,
            "tools": (),
        },
    )
    assert events[1].kind is EventKind.RUNTIME_COMPLETED
    assert events[1].sequence == 2


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
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(tenant_id, deployments),
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
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(tenant_id, deployments),
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
    scoped_deployment_ids: list[tuple[str, ...]] = []
    initialized_scope_ids: list[tuple[str, ...]] = []
    secret_ref = "secret://33333333-3333-4333-8333-333333333333"
    unrelated_secret_ref = "secret://44444444-4444-4444-8444-444444444444"

    class ScopedCapacityPool(ImmediateCapacity):
        def __init__(
            self,
            deployments: tuple[Deployment, ...],
            fingerprint_resolver: Callable[[str], Awaitable[str]],
        ) -> None:
            super().__init__(deployments)
            self.fingerprint_resolver = fingerprint_resolver

        async def initialize(self) -> None:
            assert secrets.fingerprinted == []
            initialized_scope_ids.append(tuple(deployment.id for deployment in self.deployments))
            for reference in dict.fromkeys(
                deployment.secret_ref for deployment in self.deployments
            ):
                await self.fingerprint_resolver(reference)

    class SpyCapacityPool(ImmediateCapacity):
        def __init__(
            self,
            redis_client: object,
            *,
            deployments: tuple[Deployment, ...],
            credentials: object | None = None,
            fingerprint_resolver: Callable[[str], Awaitable[str]],
        ) -> None:
            del redis_client
            assert credentials is None
            assert secrets.fingerprinted == []
            self.fingerprint_resolver = fingerprint_resolver
            created.append(self)
            super().__init__(tuple(deployments))

        async def initialize(self) -> None:
            raise AssertionError("the unscoped capacity pool must not be initialized")

        def scoped(self, deployments: Sequence[Deployment]) -> ScopedCapacityPool:
            assert secrets.fingerprinted == []
            configured = tuple(deployments)
            scoped_deployment_ids.append(tuple(deployment.id for deployment in configured))
            return ScopedCapacityPool(configured, self.fingerprint_resolver)

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
                    },
                    "unrelated": {
                        "deployments": [
                            {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": unrelated_secret_ref,
                                "quota_scope_id": "deepseek_account",
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
    assert scoped_deployment_ids == [("main_1",)]
    assert initialized_scope_ids == [("main_1",)]


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


def test_dispatch_plan_runs_review_roles_after_producer_roles() -> None:
    roles = (
        RoleAssignment(
            id="product_manager",
            role="Product Manager",
            purpose=RolePurpose.EXECUTE,
            mission="Produce the product plan.",
            must_answer=("What is the plan?",),
            allowed_tools=(),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="writer",
            role="Writer",
            purpose=RolePurpose.EXECUTE,
            mission="Write the proposal.",
            must_answer=("What was written?",),
            allowed_tools=(),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="quality_reviewer",
            role="Quality Reviewer",
            purpose=RolePurpose.VERIFY,
            mission="Review the completed proposal.",
            must_answer=("Does the proposal pass review?",),
            allowed_tools=(),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
    )

    plan = _dispatch_plan(
        roles,
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="生成一个活动方案并进行质量审查。",
        ),
        max_parallelism=3,
    )

    steps = {step.id: step for step in plan.steps}
    assert steps["product_manager_step"].depends_on == ()
    assert steps["writer_step"].depends_on == ()
    assert steps["quality_reviewer_step"].depends_on == (
        "product_manager_step",
        "writer_step",
    )
    assert steps["final_response_step"].depends_on == (
        "product_manager_step",
        "writer_step",
        "quality_reviewer_step",
    )


def test_dispatch_plan_reserves_more_time_for_post_product_review_roles() -> None:
    roles = (
        RoleAssignment(
            id="product_manager",
            role="Product Manager",
            purpose=RolePurpose.EXECUTE,
            mission="Produce the product plan.",
            must_answer=("What is the plan?",),
            allowed_tools=(),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="writer",
            role="Writer",
            purpose=RolePurpose.EXECUTE,
            mission="Write the proposal.",
            must_answer=("What was written?",),
            allowed_tools=(),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="quality_reviewer",
            role="Quality Reviewer",
            purpose=RolePurpose.VERIFY,
            mission="Review the completed proposal.",
            must_answer=("Does the proposal pass review?",),
            allowed_tools=(),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
    )

    plan = _dispatch_plan(
        roles,
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="生成一个完整项目方案，包含前端、后端、测试、部署和质量审查。",
            timeout_seconds=600,
        ),
        max_parallelism=3,
    )

    steps = {step.id: step for step in plan.steps}
    producer_timeout = max(
        steps["product_manager_step"].timeout_seconds,
        steps["writer_step"].timeout_seconds,
    )
    reviewer_timeout = steps["quality_reviewer_step"].timeout_seconds
    final_timeout = steps["final_response_step"].timeout_seconds

    assert reviewer_timeout > producer_timeout
    assert final_timeout >= reviewer_timeout * 0.75
    assert reviewer_timeout <= 600
    assert final_timeout <= 600


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


def test_dispatch_plan_exposes_only_available_role_tools_and_skills() -> None:
    roles = (
        RoleAssignment(
            id="writer",
            role="Writer",
            purpose=RolePurpose.EXECUTE,
            mission="Draft the response.",
            must_answer=("What did the writer produce?",),
            allowed_tools=("read_context",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=("docx",),
            output_schema={"summary": "string"},
            model="main",
        ),
    )

    plan = _dispatch_plan(
        roles,
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Draft a document.",
        ),
        capability_gateway=FakeCapabilityAvailability({"docx"}),
    )

    writer = next(agent for agent in plan.agents if agent.id == "writer")
    writer_step = next(step for step in plan.steps if step.agent == "writer")
    assert writer.allowed_tools == ("read_context", "docx")
    assert writer_step.tools == ("read_context", "docx")
    assert set(plan.allowed_tools) >= {"read_context", "docx"}


def test_dispatch_plan_filters_unavailable_skills_from_executable_steps() -> None:
    roles = (
        RoleAssignment(
            id="writer",
            role="Writer",
            purpose=RolePurpose.EXECUTE,
            mission="Draft the response.",
            must_answer=("What did the writer produce?",),
            allowed_tools=("read_context",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=("docx",),
            output_schema={"summary": "string"},
            model="main",
        ),
    )

    plan = _dispatch_plan(
        roles,
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Draft a document.",
        ),
        capability_gateway=FakeCapabilityAvailability(set()),
    )

    writer = next(agent for agent in plan.agents if agent.id == "writer")
    writer_step = next(step for step in plan.steps if step.agent == "writer")
    assert writer.allowed_tools == ("read_context",)
    assert writer_step.tools == ("read_context",)
    assert plan.allowed_tools == ("read_context",)


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

    role_timeouts = [step.timeout_seconds for step in plan.steps if not step.final_synthesizer]
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


def test_role_model_assignment_balances_repeated_roles_across_available_capacity() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "deepseek": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-v4-flash",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://deepseek",
                            "quota_scope_id": "deepseek",
                            "max_concurrency": 10,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
                "qwen": {
                    "deployments": [
                        {
                            "provider": "qwen",
                            "model": "qwen3-max",
                            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            "credential_ref": "secret://qwen",
                            "quota_scope_id": "qwen",
                            "max_concurrency": 5,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
                "glm": {
                    "deployments": [
                        {
                            "provider": "zhipu",
                            "model": "glm-5.2",
                            "api_base": "https://open.bigmodel.cn/api/paas/v4",
                            "credential_ref": "secret://glm",
                            "quota_scope_id": "glm",
                            "max_concurrency": 3,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
                "sonnet5": {
                    "deployments": [
                        {
                            "provider": "claude-code-relay",
                            "model": "claude-sonnet-5",
                            "api_base": "https://relay.example/v1",
                            "credential_ref": "secret://sonnet",
                            "quota_scope_id": "sonnet",
                            "max_concurrency": 3,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
            },
            "agents": [],
        }
    )
    roles = tuple(
        RoleAssignment(
            id=f"analyst_{index}",
            role="分析师",
            purpose=RolePurpose.EXPERTISE,
            mission="分析调研材料、风险和执行建议。",
            must_answer=("关键判断是什么？",),
            allowed_tools=(),
            forbidden_actions=("不要执行危险操作。",),
            skills=(),
            output_schema={},
            model="deepseek",
        )
        for index in range(6)
    )

    assigned = _assign_models_to_roles(
        roles,
        config,
        default_model="deepseek",
        task="调研产品机会，分析市场、风险和执行路径。",
    )
    assigned_models = [role.model for role in assigned]

    assert len(set(assigned_models)) >= 3
    assert assigned_models.count("deepseek") < len(assigned_models)


def test_role_model_assignment_rewards_matching_model_capabilities() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "deepseek": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-v4-flash",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://deepseek",
                            "quota_scope_id": "deepseek",
                            "max_concurrency": 20,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
                "qwen_audio": {
                    "deployments": [
                        {
                            "provider": "qwen",
                            "model": "qwen-audio-plus",
                            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            "credential_ref": "secret://qwen",
                            "quota_scope_id": "qwen",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "audio", "structured_output"],
                        }
                    ]
                },
            },
            "agents": [],
        }
    )
    role = RoleAssignment(
        id="meeting_summarizer",
        role="会议纪要整理",
        purpose=RolePurpose.EXECUTE,
        mission="理解录音内容，整理会议纪要和待办事项。",
        must_answer=("会议结论和待办是什么？",),
        allowed_tools=(),
        forbidden_actions=("不要执行危险操作。",),
        skills=(),
        output_schema={},
        model="deepseek",
    )

    assigned = _assign_models_to_roles(
        (role,),
        config,
        default_model="deepseek",
        task="请分析这段语音录音并整理会议纪要。",
    )

    assert assigned[0].model == "qwen_audio"


def test_role_model_assignment_rewards_ordinary_model_characteristics() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "deepseek": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-v4-flash",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://deepseek",
                            "quota_scope_id": "deepseek",
                            "max_concurrency": 20,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
                "sonnet5": {
                    "deployments": [
                        {
                            "provider": "claude-code-relay",
                            "model": "claude-sonnet-5",
                            "api_base": "https://relay.example/v1",
                            "credential_ref": "secret://sonnet",
                            "quota_scope_id": "sonnet",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
            },
            "agents": [],
        }
    )
    role = RoleAssignment(
        id="quality_reviewer",
        role="质量审查",
        purpose=RolePurpose.EXPERTISE,
        mission="复核方案质量、证据链、风险和遗漏项。",
        must_answer=("是否通过质量审查？",),
        allowed_tools=(),
        forbidden_actions=("不要执行危险操作。",),
        skills=(),
        output_schema={},
        model="deepseek",
    )

    assigned = _assign_models_to_roles(
        (role,),
        config,
        default_model="deepseek",
        task="请对这个方案做质量审查、风险复核和遗漏检查。",
    )

    assert assigned[0].model == "sonnet5"


def test_general_role_model_assignment_uses_more_configured_text_models() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "deepseek": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-v4-flash",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://deepseek",
                            "quota_scope_id": "deepseek",
                            "max_concurrency": 20,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
                "qwen": {
                    "deployments": [
                        {
                            "provider": "qwen",
                            "model": "qwen3-max",
                            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            "credential_ref": "secret://qwen",
                            "quota_scope_id": "qwen",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
                "glm": {
                    "deployments": [
                        {
                            "provider": "zhipu",
                            "model": "glm-5.2",
                            "api_base": "https://open.bigmodel.cn/api/paas/v4",
                            "credential_ref": "secret://glm",
                            "quota_scope_id": "glm",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
                "sonnet5": {
                    "deployments": [
                        {
                            "provider": "claude-code-relay",
                            "model": "claude-sonnet-5",
                            "api_base": "https://relay.example/v1",
                            "credential_ref": "secret://sonnet",
                            "quota_scope_id": "sonnet",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
            },
            "agents": [],
        }
    )
    roles = tuple(
        RoleAssignment(
            id=f"general_{index}",
            role="通用助手",
            purpose=RolePurpose.EXECUTE,
            mission="整理信息并给出可执行建议。",
            must_answer=("建议是什么？",),
            allowed_tools=(),
            forbidden_actions=("不要执行危险操作。",),
            skills=(),
            output_schema={},
            model="deepseek",
        )
        for index in range(4)
    )

    assigned = _assign_models_to_roles(
        roles,
        config,
        default_model="deepseek",
        task="帮我整理这个普通问题的思路和建议。",
    )

    assert len({role.model for role in assigned}) == 4


def test_role_model_assignment_uses_inferred_mainstream_model_traits() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "deepseek": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-v4-flash",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://deepseek",
                            "quota_scope_id": "deepseek",
                            "max_concurrency": 20,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
                "gemini_pro": {
                    "deployments": [
                        {
                            "provider": "google",
                            "model": "gemini-2.5-pro",
                            "api_base": "https://generativelanguage.googleapis.com/v1beta/openai",
                            "credential_ref": "secret://gemini",
                            "quota_scope_id": "gemini",
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
    )
    role = RoleAssignment(
        id="vision_reviewer",
        role="截图理解",
        purpose=RolePurpose.EXPERTISE,
        mission="分析图片和截图中的界面问题。",
        must_answer=("截图里的问题是什么？",),
        allowed_tools=(),
        forbidden_actions=("不要执行危险操作。",),
        skills=(),
        output_schema={},
        model="deepseek",
    )

    assigned = _assign_models_to_roles(
        (role,),
        config,
        default_model="deepseek",
        task="请根据这张截图分析 UI 问题。",
    )

    assert assigned[0].model == "gemini_pro"


def test_role_model_assignment_avoids_messages_endpoint_for_tool_roles() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "sonnet5": {
                    "deployments": [
                        {
                            "provider": "claude-code-relay",
                            "model": "claude-sonnet-5",
                            "api_base": "https://gsykj.com/v1/messages",
                            "credential_ref": "secret://sonnet",
                            "quota_scope_id": "sonnet",
                            "max_concurrency": 3,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
                "qwen": {
                    "deployments": [
                        {
                            "provider": "qwen",
                            "model": "qwen3-max",
                            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            "credential_ref": "secret://qwen",
                            "quota_scope_id": "qwen",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
            },
            "agents": [],
        }
    )
    role = RoleAssignment(
        id="planner",
        role="Planner",
        purpose=RolePurpose.EXECUTE,
        mission="拆解任务、定义步骤和验收标准。",
        must_answer=("步骤是什么？",),
        allowed_tools=("read_context",),
        forbidden_actions=("不要执行危险操作。",),
        skills=(),
        output_schema={},
        model="sonnet5",
    )

    assigned = _assign_models_to_roles(
        (role,),
        config,
        default_model="sonnet5",
        task="用混合的模式，给我生成一个北京的防汛方案",
    )

    assert assigned[0].model == "qwen"


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


def test_discussion_plan_normalizes_hyphenated_ids_for_autogen() -> None:
    plan = _discussion_plan(
        (
            RoleAssignment(
                id="content-writer",
                role="文案",
                purpose=RolePurpose.EXPERTISE,
                mission="输出脚本。",
                must_answer=("脚本是什么？",),
                allowed_tools=(),
                forbidden_actions=("不要执行危险操作。",),
                skills=(),
                output_schema={"position": "string"},
                model="main",
            ),
            RoleAssignment(
                id="risk-reviewer",
                role="风险审查",
                purpose=RolePurpose.CRITIQUE,
                mission="检查风险。",
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

    assert [participant.id for participant in plan.participants] == [
        "content_writer",
        "risk_reviewer",
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


def test_selected_agent_ids_are_resolved_from_config_without_extra_roles() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
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
                "review": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-v4-flash",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://review",
                            "quota_scope_id": "deepseek",
                            "max_concurrency": 4,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text"],
                        }
                    ]
                },
            },
            "agents": [
                {
                    "id": "copywriter",
                    "role": "文案生成",
                    "prompt": "负责活动文案和脚本。",
                    "model": "creative",
                    "skills": ["docx"],
                },
                {
                    "id": "reviewer",
                    "role": "质量审查",
                    "prompt": "负责检查风险和遗漏。",
                    "model": "review",
                    "skills": [],
                },
                {
                    "id": "unused_director",
                    "role": "导演",
                    "prompt": "不应被本次选择带入。",
                    "model": "creative",
                    "skills": [],
                },
            ],
        }
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.HYBRID,
        request="写一个中秋活动方案。",
        routing_decision={"selected_agent_ids": ("copywriter", "reviewer")},
    )

    roles = _selected_config_role_assignments(
        context,
        config,
        purpose=RolePurpose.EXPERTISE,
        output_schema={"position": "string"},
    )

    assert [(role.id, role.role, role.model, role.skills) for role in roles] == [
        ("copywriter", "文案生成", "creative", ("docx",)),
        ("reviewer", "质量审查", "review", ()),
    ]


def test_selected_dispatch_reviewer_runs_after_selected_producers() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "creative": {
                    "deployments": [
                        {
                            "provider": "kimi",
                            "model": "kimi-k2-latest",
                            "api_base": "https://api.moonshot.cn/v1",
                            "credential_ref": "secret://creative",
                            "quota_scope_id": "kimi",
                            "max_concurrency": 4,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text"],
                        }
                    ]
                },
                "review": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-v4-flash",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://review",
                            "quota_scope_id": "deepseek",
                            "max_concurrency": 4,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text"],
                        }
                    ]
                },
            },
            "agents": [
                {
                    "id": "copywriter",
                    "role": "文案生成",
                    "prompt": "负责活动文案和脚本。",
                    "model": "creative",
                    "skills": [],
                },
                {
                    "id": "quality_reviewer",
                    "role": "质量审查",
                    "prompt": "负责检查风险、遗漏和验收标准。",
                    "model": "review",
                    "skills": [],
                },
            ],
        }
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="写一个中秋活动方案并进行质量审查。",
        routing_decision={"selected_agent_ids": ("copywriter", "quality_reviewer")},
    )

    roles = _selected_config_role_assignments(
        context,
        config,
        purpose=RolePurpose.EXECUTE,
        output_schema={"summary": "string"},
    )
    plan = _dispatch_plan(roles, context, max_parallelism=3)

    steps = {step.id: step for step in plan.steps}
    assert steps["copywriter_step"].depends_on == ()
    assert steps["quality_reviewer_step"].depends_on == ("copywriter_step",)
    assert steps["final_response_step"].depends_on == (
        "copywriter_step",
        "quality_reviewer_step",
    )


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
