import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
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
from agent_hub.models.capacity import CapacityLease, CapacityWaitTimeout
from agent_hub.models.gateway import CapacityController
from agent_hub.models.routing_policy import DeploymentRoutingConstraint
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
    _capability_inventory_payload,
    _deployment_constraints_payload,
    _discussion_plan,
    _dispatch_parallelism,
    _dispatch_plan,
    _model_execution_plan_payload,
    _role_model_routing_matrix_payload,
    _select_logical_model_for_role,
    _selected_config_role_assignments,
    configured_runtime_registry,
)
from agent_hub.runtime.direct import RuntimeExecutionError
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


class ManifestCapabilityGateway(FakeCapabilityAvailability):
    def capability_manifest(self, tenant_id: UUID) -> Mapping[str, JsonValue]:
        assert tenant_id == TENANT_ID
        return {
            "schema_version": 1,
            "capabilities": (
                {
                    "id": "docx",
                    "kind": "skill",
                    "adapter": "skill_sandbox",
                    "permission_class": "skill.use",
                    "sandbox_profile": "systemd_skill_sandbox",
                    "available": True,
                    "availability_reason": None,
                    "replay_safe": False,
                    "aliases": (),
                },
                {
                    "id": "filesystem.read_file",
                    "kind": "mcp",
                    "adapter": "mcp_server",
                    "permission_class": "mcp.invoke",
                    "sandbox_profile": "mcp_stdio",
                    "available": False,
                    "availability_reason": "mcp_server_not_discovered",
                    "replay_safe": False,
                    "aliases": (),
                },
            ),
        }


class AvailableMcpManifestCapabilityGateway(FakeCapabilityAvailability):
    def __init__(self) -> None:
        super().__init__({"search.web_search"})

    def capability_manifest(self, tenant_id: UUID) -> Mapping[str, JsonValue]:
        assert tenant_id == TENANT_ID
        return {
            "schema_version": 1,
            "capabilities": (
                {
                    "id": "search.web_search",
                    "kind": "mcp",
                    "adapter": "mcp_server",
                    "permission_class": "mcp.invoke",
                    "sandbox_profile": "mcp_remote",
                    "available": True,
                    "availability_reason": None,
                    "replay_safe": False,
                    "aliases": ("search_web",),
                },
            ),
        }


class AvailablePluginManifestCapabilityGateway(FakeCapabilityAvailability):
    def __init__(self) -> None:
        super().__init__({"calendar.create_event"})

    def capability_manifest(self, tenant_id: UUID) -> Mapping[str, JsonValue]:
        assert tenant_id == TENANT_ID
        return {
            "schema_version": 1,
            "capabilities": (
                {
                    "id": "calendar.create_event",
                    "kind": "plugin",
                    "adapter": "plugin_runtime",
                    "permission_class": "calendar.write",
                    "sandbox_profile": "remote_connector",
                    "available": True,
                    "availability_reason": None,
                    "replay_safe": False,
                    "aliases": ("calendar_create",),
                },
            ),
        }


class TenantPreparedPluginManifestCapabilityGateway(FakeCapabilityAvailability):
    def __init__(self) -> None:
        super().__init__(set())
        self.prepared_tenants: list[UUID] = []

    async def ensure_tenant_loaded(self, tenant_id: UUID) -> None:
        assert tenant_id == TENANT_ID
        self.prepared_tenants.append(tenant_id)
        self.available.add("calendar.create_event")

    def capability_manifest(self, tenant_id: UUID) -> Mapping[str, JsonValue]:
        assert tenant_id == TENANT_ID
        if tenant_id not in self.prepared_tenants:
            return {
                "schema_version": 1,
                "capabilities": (),
            }
        return AvailablePluginManifestCapabilityGateway().capability_manifest(tenant_id)


class BadManifestCapabilityGateway(FakeCapabilityAvailability):
    def __init__(self, manifest: Mapping[str, JsonValue] | Exception) -> None:
        super().__init__(set())
        self.manifest = manifest

    def capability_manifest(self, tenant_id: UUID) -> Mapping[str, JsonValue]:
        assert tenant_id == TENANT_ID
        if isinstance(self.manifest, Exception):
            raise self.manifest
        return self.manifest


class TruthyReplaySafeGateway(FakeCapabilityAvailability):
    def is_replay_safe(self, name: str) -> bool:
        del name
        return cast(bool, "false")


class RaisingReplaySafeGateway(FakeCapabilityAvailability):
    def is_replay_safe(self, name: str) -> bool:
        raise KeyError(name)


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


class TimeoutCapacity(ImmediateCapacity):
    async def acquire(
        self,
        candidates: Sequence[Deployment],
        wait_timeout: float,
        *,
        estimated_tokens: int,
    ) -> CapacityLease:
        self.wait_timeouts.append(wait_timeout)
        self.events = getattr(self, "events", [])
        self.events.append(tuple(deployment.provider_model for deployment in candidates))
        assert estimated_tokens > 0
        raise CapacityWaitTimeout("busy")


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
        harness_tool_gateway: object | None = None,
    ) -> None:
        del gateway, capability_gateway
        self.plan = plan
        self.harness_tool_gateway = harness_tool_gateway
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
        del direct
        self.dispatch = dispatch
        self.discussion = discussion
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
    assert secrets.resolved == [
        (TENANT_ID, "secret://22222222-2222-4222-8222-222222222222")
    ]
    assert capacities[0].recorded == [True]
    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.payload["requested_logical_model"] == "main"
    assert artifact_event.payload["logical_model"] == "main"
    assert artifact_event.payload["fallback_used"] is False
    assert artifact_event.payload["fallback_from_logical_model"] is None
    assert artifact_event.payload["fallback_reason"] is None
    assert artifact_event.payload["attempted_logical_models"] == ("main",)
    assert artifact_event.payload["fallback_attempt_count"] == 0
    completed_event = next(event for event in events if event.kind is EventKind.RUNTIME_COMPLETED)
    assert completed_event.payload["requested_logical_model"] == "main"
    assert completed_event.payload["logical_model"] == "main"
    assert completed_event.payload["fallback_used"] is False
    assert completed_event.payload["fallback_attempt_count"] == 0


@pytest.mark.asyncio
async def test_config_backed_direct_runtime_uses_harness_selected_logical_model() -> None:
    transport = FakeTransport()
    runtime = ConfigBackedDirectRuntime(
        config_service=FakeConfigService(
            {
                "models": {
                    "main": {
                        "deployments": [
                            {
                                "provider": "openai",
                                "model": "gpt-5.6-sol",
                                "api_base": "https://api.openai.com/v1",
                                "credential_ref": "secret://main",
                                "quota_scope_id": "openai_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            }
                        ]
                    },
                    "research": {
                        "deployments": [
                            {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://research",
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
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(tenant_id, deployments),
        transport=transport,
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DIRECT,
                request="summarize this research",
                routing_decision={
                    "harness_decision": {
                        "selected_provider": "deepseek",
                        "selected_model": "deepseek-chat",
                        "selected_logical_model": "research",
                    }
                },
            )
        )
    ]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    deployment, request, _api_key = transport.calls[0]
    assert deployment.provider_model == "deepseek/deepseek-chat"
    assert request.logical_model == "research"


@pytest.mark.asyncio
async def test_config_backed_direct_runtime_constrains_harness_selected_deployment() -> None:
    transport = FakeTransport()
    runtime = ConfigBackedDirectRuntime(
        config_service=FakeConfigService(
            {
                "models": {
                    "main": {
                        "deployments": [
                            {
                                "provider": "openai",
                                "model": "gpt-5.6-sol",
                                "api_base": "https://api.openai.com/v1",
                                "credential_ref": "secret://openai",
                                "quota_scope_id": "openai_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            },
                            {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://deepseek",
                                "quota_scope_id": "deepseek_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            },
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

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DIRECT,
                request="use the harness-selected model",
                routing_decision={
                    "harness_decision": {
                        "selected_provider": "deepseek",
                        "selected_model": "deepseek-chat",
                        "selected_logical_model": "main",
                    }
                },
            )
        )
    ]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    deployment, request, _api_key = transport.calls[0]
    assert deployment.provider_model == "deepseek/deepseek-chat"
    assert request.logical_model == "main"


@pytest.mark.asyncio
async def test_config_backed_direct_runtime_fails_closed_when_harness_deployment_is_missing() -> None:
    transport = FakeTransport()
    runtime = ConfigBackedDirectRuntime(
        config_service=FakeConfigService(
            {
                "models": {
                    "main": {
                        "deployments": [
                            {
                                "provider": "openai",
                                "model": "gpt-5.6-sol",
                                "api_base": "https://api.openai.com/v1",
                                "credential_ref": "secret://openai",
                                "quota_scope_id": "openai_account",
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

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DIRECT,
                request="use the harness-selected model",
                routing_decision={
                    "harness_decision": {
                        "selected_provider": "deepseek",
                        "selected_model": "deepseek-chat",
                        "selected_logical_model": "main",
                    }
                },
            )
        )
    ]

    assert [event.kind for event in events] == [EventKind.RUNTIME_FAILED]
    assert events[0].reason == "harness_model_unavailable"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_config_backed_direct_runtime_does_not_fallback_past_harness_selection() -> None:
    transport = FakeTransport()
    capacities: list[TimeoutCapacity] = []

    async def capacity_factory(
        tenant_id: UUID,
        deployments: tuple[Deployment, ...],
    ) -> TimeoutCapacity:
        assert tenant_id == TENANT_ID
        capacity = TimeoutCapacity(deployments)
        capacities.append(capacity)
        return capacity

    runtime = ConfigBackedDirectRuntime(
        config_service=FakeConfigService(
            {
                "models": {
                    "main": {
                        "fallback_model": "backup",
                        "deployments": [
                            {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://deepseek",
                                "quota_scope_id": "deepseek_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            }
                        ],
                    },
                    "backup": {
                        "deployments": [
                            {
                                "provider": "openai",
                                "model": "gpt-5.6-sol",
                                "api_base": "https://api.openai.com/v1",
                                "credential_ref": "secret://openai",
                                "quota_scope_id": "openai_account",
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
        capacity_factory=capacity_factory,
        transport=transport,
    )

    with pytest.raises(RuntimeExecutionError, match="model capacity unavailable"):
        _ = [
            event
            async for event in runtime.run(
                TaskContext(
                    run_id=uuid4(),
                    tenant_id=TENANT_ID,
                    mode=TaskMode.DIRECT,
                    request="use the harness-selected model",
                    routing_decision={
                        "harness_decision": {
                            "selected_provider": "deepseek",
                            "selected_model": "deepseek-chat",
                            "selected_logical_model": "main",
                        }
                    },
                )
            )
        ]

    assert capacities[0].events == [("deepseek/deepseek-chat",)]
    assert transport.calls == []


@pytest.mark.asyncio
async def test_config_backed_direct_runtime_disables_fallback_from_harness_policy() -> None:
    transport = FakeTransport()
    capacities: list[TimeoutCapacity] = []

    async def capacity_factory(
        tenant_id: UUID,
        deployments: tuple[Deployment, ...],
    ) -> TimeoutCapacity:
        assert tenant_id == TENANT_ID
        capacity = TimeoutCapacity(deployments)
        capacities.append(capacity)
        return capacity

    runtime = ConfigBackedDirectRuntime(
        config_service=FakeConfigService(
            {
                "models": {
                    "main": {
                        "fallback_model": "backup",
                        "deployments": [
                            {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://deepseek",
                                "quota_scope_id": "deepseek_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            }
                        ],
                    },
                    "backup": {
                        "deployments": [
                            {
                                "provider": "openai",
                                "model": "gpt-5.6-sol",
                                "api_base": "https://api.openai.com/v1",
                                "credential_ref": "secret://openai",
                                "quota_scope_id": "openai_account",
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
        capacity_factory=capacity_factory,
        transport=transport,
    )

    with pytest.raises(RuntimeExecutionError, match="model capacity unavailable"):
        _ = [
            event
            async for event in runtime.run(
                TaskContext(
                    run_id=uuid4(),
                    tenant_id=TENANT_ID,
                    mode=TaskMode.DIRECT,
                    request="use explicit harness fallback policy",
                    routing_decision={
                        "harness_policy": {
                            "fallback_policy": "disabled",
                        }
                    },
                )
            )
        ]

    assert capacities[0].events == [("deepseek/deepseek-chat",)]
    assert transport.calls == []


@pytest.mark.parametrize(
    "routing_decision",
    (
        {},
        {"harness_policy": {"fallback_policy": "configured"}},
    ),
)
@pytest.mark.asyncio
async def test_config_backed_direct_runtime_uses_configured_fallback(
    routing_decision: Mapping[str, JsonValue],
) -> None:
    class FailFirstCapacity(ImmediateCapacity):
        def __init__(self, deployments: tuple[Deployment, ...]) -> None:
            super().__init__(deployments)
            self.events: list[tuple[str, ...]] = []

        async def acquire(
            self,
            candidates: Sequence[Deployment],
            wait_timeout: float,
            *,
            estimated_tokens: int,
        ) -> CapacityLease:
            self.wait_timeouts.append(wait_timeout)
            self.events.append(tuple(deployment.provider_model for deployment in candidates))
            assert estimated_tokens > 0
            if len(self.events) == 1:
                raise CapacityWaitTimeout("busy")
            candidate = next(iter(candidates))
            return CapacityLease(
                id=str(uuid4()),
                deployment_id=candidate.id,
                quota_scope_id=candidate.quota_scope_id,
                expires_at=datetime.now(UTC) + timedelta(seconds=30),
                renew_after_seconds=30,
            )

    transport = FakeTransport()
    capacities: list[FailFirstCapacity] = []

    async def capacity_factory(
        tenant_id: UUID,
        deployments: tuple[Deployment, ...],
    ) -> FailFirstCapacity:
        assert tenant_id == TENANT_ID
        capacity = FailFirstCapacity(deployments)
        capacities.append(capacity)
        return capacity

    runtime = ConfigBackedDirectRuntime(
        config_service=FakeConfigService(
            {
                "models": {
                    "main": {
                        "fallback_model": "backup",
                        "deployments": [
                            {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://deepseek",
                                "quota_scope_id": "deepseek_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            }
                        ],
                    },
                    "backup": {
                        "deployments": [
                            {
                                "provider": "openai",
                                "model": "gpt-5.6-sol",
                                "api_base": "https://api.openai.com/v1",
                                "credential_ref": "secret://openai",
                                "quota_scope_id": "openai_account",
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
        capacity_factory=capacity_factory,
        transport=transport,
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DIRECT,
                request="use configured fallback policy",
                routing_decision=routing_decision,
            )
        )
    ]

    assert capacities[0].events == [
        ("deepseek/deepseek-chat",),
        ("openai/gpt-5.6-sol",),
    ]
    assert len(transport.calls) == 1
    assert transport.calls[0][0].logical_model == "backup"
    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.payload["requested_logical_model"] == "main"
    assert artifact_event.payload["logical_model"] == "backup"
    assert artifact_event.payload["provider"] == "openai"
    assert artifact_event.payload["upstream_model"] == "openai/gpt-5.6-sol"
    assert artifact_event.payload["fallback_used"] is True
    assert artifact_event.payload["fallback_from_logical_model"] == "main"
    assert artifact_event.payload["fallback_reason"] == "capacity_unavailable"
    assert artifact_event.payload["attempted_logical_models"] == ("main", "backup")
    assert artifact_event.payload["fallback_attempt_count"] == 1
    completed_event = next(event for event in events if event.kind is EventKind.RUNTIME_COMPLETED)
    assert completed_event.payload["requested_logical_model"] == "main"
    assert completed_event.payload["logical_model"] == "backup"
    assert completed_event.payload["fallback_used"] is True
    assert completed_event.payload["fallback_attempt_count"] == 1


def test_model_execution_plan_reports_disabled_harness_fallback_policy() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "main": {
                    "fallback_model": "backup",
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://deepseek",
                            "quota_scope_id": "deepseek_account",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text"],
                        }
                    ],
                },
                "backup": {
                    "deployments": [
                        {
                            "provider": "openai",
                            "model": "gpt-5.6-sol",
                            "api_base": "https://api.openai.com/v1",
                            "credential_ref": "secret://openai",
                            "quota_scope_id": "openai_account",
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
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="use explicit harness fallback policy",
        routing_decision={"harness_policy": {"fallback_policy": "disabled"}},
    )

    deployment_constraints = _deployment_constraints_payload(
        config,
        main_agent_model="main",
        roles=(),
        constraint=None,
        fallback_policy="disabled",
    )
    plan = _model_execution_plan_payload(
        context,
        main_agent_model="main",
        roles=(),
        deployment_constraints=deployment_constraints,
        deployment_constraint=None,
        fallback_policy="disabled",
    )

    main_agent = plan["main_agent"]
    assert isinstance(main_agent, Mapping)
    assert main_agent["fallback_policy"] == "disabled_by_harness_policy"
    assert deployment_constraints["items"] == (
        {
            "logical_model": "main",
            "total_deployments": 1,
            "eligible_deployments": 1,
            "harness_constrained": False,
            "selected_provider": None,
            "selected_model": None,
            "fallback_policy": "disabled_by_harness_policy",
        },
    )


def test_model_execution_plan_separates_scheduler_selection_from_gateway_policy() -> None:
    constraint = DeploymentRoutingConstraint(
        logical_model="main",
        provider="deepseek",
        model="deepseek-chat",
    )
    plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Draft a launch campaign.",
            routing_decision={
                "harness_decision": {
                    "selected_provider": "deepseek",
                    "selected_model": "deepseek-chat",
                    "selected_logical_model": "main",
                    "requires_approval": False,
                }
            },
        ),
        main_agent_model="main",
        roles=(),
        deployment_constraint=constraint,
        fallback_policy="configured",
    )

    assert plan["scheduler_selection"] == {
        "schema_version": 1,
        "source": "harness_decision",
        "selected_logical_model": "main",
        "selected_provider": "deepseek",
        "selected_model": "deepseek-chat",
        "applies_to_main_agent": True,
        "requires_approval": False,
    }
    assert plan["gateway_execution_policy"] == {
        "schema_version": 1,
        "capacity_boundary": "model_gateway",
        "deployment_constraint_applied": True,
        "constrained_logical_model": "main",
        "fallback_policy": "disabled_for_harness_selection",
        "fallback_mappings_enabled": False,
    }


def test_model_execution_plan_reports_gateway_policy_without_scheduler_selection() -> None:
    plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Draft a launch campaign.",
        ),
        main_agent_model="main",
        roles=(),
        deployment_constraint=None,
        fallback_policy="configured",
    )

    assert plan["scheduler_selection"] == {
        "schema_version": 1,
        "source": "runtime_default",
        "selected_logical_model": None,
        "selected_provider": None,
        "selected_model": None,
        "applies_to_main_agent": False,
        "requires_approval": False,
    }
    assert plan["gateway_execution_policy"] == {
        "schema_version": 1,
        "capacity_boundary": "model_gateway",
        "deployment_constraint_applied": False,
        "constrained_logical_model": None,
        "fallback_policy": "configured",
        "fallback_mappings_enabled": True,
    }


def test_model_execution_plan_reports_harness_policy_without_scheduler_selection() -> None:
    plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Draft a launch campaign.",
            routing_decision={"harness_policy": {"fallback_policy": "disabled"}},
        ),
        main_agent_model="main",
        roles=(),
        deployment_constraint=None,
        fallback_policy="disabled",
    )

    assert plan["scheduler_selection"] == {
        "schema_version": 1,
        "source": "runtime_default",
        "selected_logical_model": None,
        "selected_provider": None,
        "selected_model": None,
        "applies_to_main_agent": False,
        "requires_approval": False,
    }
    assert plan["gateway_execution_policy"] == {
        "schema_version": 1,
        "capacity_boundary": "model_gateway",
        "deployment_constraint_applied": False,
        "constrained_logical_model": None,
        "fallback_policy": "disabled_by_harness_policy",
        "fallback_mappings_enabled": False,
    }


def test_model_execution_plan_reports_explicit_main_agent_selection_source() -> None:
    plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Draft a launch campaign.",
            routing_decision={"main_agent_model": "creative"},
        ),
        main_agent_model="creative",
        roles=(),
        deployment_constraint=None,
        fallback_policy="configured",
    )

    assert plan["scheduler_selection"] == {
        "schema_version": 1,
        "source": "main_agent_model",
        "selected_logical_model": "creative",
        "selected_provider": None,
        "selected_model": None,
        "applies_to_main_agent": True,
        "requires_approval": False,
    }
    assert plan["gateway_execution_policy"] == {
        "schema_version": 1,
        "capacity_boundary": "model_gateway",
        "deployment_constraint_applied": False,
        "constrained_logical_model": None,
        "fallback_policy": "configured",
        "fallback_mappings_enabled": True,
    }


def test_model_execution_plan_sanitizes_scheduler_selection_fields() -> None:
    plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Draft a launch campaign.",
            routing_decision={
                "harness_decision": {
                    "selected_provider": "sk-secret-token",
                    "selected_model": "authorization bearer token",
                    "selected_logical_model": "main",
                    "requires_approval": True,
                }
            },
        ),
        main_agent_model="main",
        roles=(),
        deployment_constraint=None,
        fallback_policy="configured",
    )

    assert plan["scheduler_selection"] == {
        "schema_version": 1,
        "source": "harness_decision",
        "selected_logical_model": "main",
        "selected_provider": None,
        "selected_model": None,
        "applies_to_main_agent": True,
        "requires_approval": True,
    }


def test_model_execution_plan_reports_safe_orchestration_handoffs() -> None:
    plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Draft and review a launch campaign.",
        ),
        main_agent_model="main",
        roles=(
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
            {
                "id": "token_leak",
                "role": "Leaky",
                "purpose": "execute",
                "logical_model": "sk_secret",
                "tools": (),
            },
            {
                "id": "credential_ref",
                "role": "Credential",
                "purpose": "execute",
                "logical_model": "creative",
                "tools": (),
            },
            {
                "id": "capacity_pool",
                "role": "Capacity",
                "purpose": "execute",
                "logical_model": "creative",
                "tools": (),
            },
            {
                "id": "quota_scope_id",
                "role": "Quota",
                "purpose": "execute",
                "logical_model": "creative",
                "tools": (),
            },
            {
                "id": "lease_id",
                "role": "Lease",
                "purpose": "execute",
                "logical_model": "creative",
                "tools": (),
            },
            {
                "id": "api_base_role",
                "role": "Api Base",
                "purpose": "execute",
                "logical_model": "api_base",
                "tools": (),
            },
        ),
        steps=(
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
                "depends_on": (
                    "copywriter_step",
                    "token_leak_step",
                    "credential_ref_step",
                    "capacity_pool_step",
                    "quota_scope_id_step",
                    "lease_id_step",
                    "api_base_step",
                ),
                "final_synthesizer": True,
                "tools": (),
            },
            {
                "id": "token_leak_step",
                "agent": "token_leak",
                "depends_on": (),
                "final_synthesizer": False,
                "tools": (),
            },
            {
                "id": "credential_ref_step",
                "agent": "credential_ref",
                "depends_on": (),
                "final_synthesizer": False,
                "tools": (),
            },
            {
                "id": "capacity_pool_step",
                "agent": "capacity_pool",
                "depends_on": (),
                "final_synthesizer": False,
                "tools": (),
            },
            {
                "id": "quota_scope_id_step",
                "agent": "quota_scope_id",
                "depends_on": (),
                "final_synthesizer": False,
                "tools": (),
            },
            {
                "id": "lease_id_step",
                "agent": "lease_id",
                "depends_on": (),
                "final_synthesizer": False,
                "tools": (),
            },
            {
                "id": "api_base_step",
                "agent": "api_base_role",
                "depends_on": (),
                "final_synthesizer": False,
                "tools": (),
            },
        ),
    )

    assert plan["orchestration_handoffs"] == {
        "schema_version": 1,
        "items": (
            {
                "source_step_id": "copywriter_step",
                "target_step_id": "final_response_step",
                "source_role_id": "copywriter",
                "target_role_id": "final_synthesizer",
                "source_purpose": "execute",
                "target_purpose": "synthesize",
                "source_logical_model": "creative",
                "target_logical_model": "main",
                "handoff_kind": "step_dependency",
            },
        ),
        "truncated": False,
    }
    assert plan["orchestration_contracts"] == {
        "schema_version": 1,
        "items": (
            {
                "contract_id": "copywriter_step-to-final_response_step",
                "source_step_id": "copywriter_step",
                "target_step_id": "final_response_step",
                "source_role_id": "copywriter",
                "target_role_id": "final_synthesizer",
                "handoff_kind": "step_dependency",
                "status": "planned",
                "required_output_fields": (
                    "status",
                    "summary",
                    "evidence",
                    "risks",
                    "artifacts",
                    "verification",
                ),
                "ready_status": "done",
                "blocking_statuses": ("blocked", "needs_user"),
                "recovery_hint": "retry_blocked_contract_chain",
            },
        ),
        "truncated": False,
    }
    assert plan["orchestration_protocol"] == {
        "schema_version": 1,
        "protocol": "role_handoff_contract_v1",
        "mode": "dispatch",
        "role_count": 2,
        "handoff_count": 1,
        "contract_count": 1,
        "structured_output_schema": "dispatch_output_v1",
        "required_output_fields": (
            "status",
            "summary",
            "evidence",
            "risks",
            "artifacts",
            "verification",
        ),
        "ready_status": "done",
        "blocking_statuses": ("blocked", "needs_user"),
        "recovery_hints": ("retry_blocked_contract_chain",),
        "truncated": False,
    }
    serialized_handoffs = json.dumps(plan["orchestration_handoffs"], ensure_ascii=False)
    serialized_contracts = json.dumps(plan["orchestration_contracts"], ensure_ascii=False)
    serialized_protocol = json.dumps(plan["orchestration_protocol"], ensure_ascii=False)
    for unsafe_marker in (
        "sk_secret",
        "token_leak",
        "credential_ref",
        "capacity_pool",
        "quota_scope_id",
        "lease_id",
        "api_base",
    ):
        assert unsafe_marker not in serialized_handoffs
        assert unsafe_marker not in serialized_contracts
        assert unsafe_marker not in serialized_protocol


def test_model_execution_plan_reports_safe_model_capability_negotiation() -> None:
    plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Build and verify a small Python project.",
        ),
        main_agent_model="main",
        roles=(
            {
                "id": "builder",
                "role": "Builder",
                "purpose": "execute",
                "logical_model": "coder",
                "tools": ("run_safe_command",),
            },
            {
                "id": "reviewer",
                "role": "Reviewer",
                "purpose": "execute",
                "logical_model": "critic",
                "tools": (),
            },
            {
                "id": "token_leak",
                "role": "Leaky",
                "purpose": "execute",
                "logical_model": "sk_secret",
                "tools": ("run_safe_command",),
            },
        ),
        model_routing_matrix=(
            {
                "role_id": "builder",
                "purpose": "execute",
                "selected_logical_model": "coder",
                "candidate_count": 1,
                "truncated_candidates": False,
                "candidates": (
                    {
                        "logical_model": "coder",
                        "score": 42,
                        "adjusted_score": 42,
                        "eligible": True,
                        "selected": True,
                        "traits": ("code", "structured_output", "tool_calling"),
                        "reasons": ("capability:tool_role_supported",),
                    },
                ),
            },
            {
                "role_id": "reviewer",
                "purpose": "execute",
                "selected_logical_model": "critic",
                "candidate_count": 1,
                "truncated_candidates": False,
                "candidates": (
                    {
                        "logical_model": "critic",
                        "score": 9,
                        "adjusted_score": 9,
                        "eligible": True,
                        "selected": True,
                        "traits": ("review",),
                        "reasons": (),
                    },
                ),
            },
            {
                "role_id": "token_leak",
                "purpose": "execute",
                "selected_logical_model": "sk_secret",
                "candidate_count": 1,
                "truncated_candidates": False,
                "candidates": (
                    {
                        "logical_model": "sk_secret",
                        "score": 0,
                        "adjusted_score": 0,
                        "eligible": True,
                        "selected": True,
                        "traits": ("tool_calling",),
                        "reasons": ("capacity:configured:8",),
                    },
                ),
            },
        ),
    )

    assert plan["model_capability_negotiation"] == {
        "schema_version": 1,
        "items": (
            {
                "role_id": "builder",
                "logical_model": "coder",
                "required_capabilities": ("text", "structured_output", "tool_calling"),
                "matched_capabilities": ("structured_output", "tool_calling"),
                "missing_capabilities": ("text",),
                "status": "missing_capability",
            },
            {
                "role_id": "reviewer",
                "logical_model": "critic",
                "required_capabilities": ("text", "structured_output"),
                "matched_capabilities": (),
                "missing_capabilities": ("text", "structured_output"),
                "status": "missing_capability",
            },
        ),
        "role_count": 2,
        "satisfied_count": 0,
        "missing_count": 2,
        "unknown_count": 0,
        "truncated": False,
    }
    serialized_negotiation = json.dumps(
        plan["model_capability_negotiation"],
        ensure_ascii=False,
    )
    for unsafe_marker in (
        "sk_secret",
        "token_leak",
        "credential",
        "capacity",
        "quota",
        "lease",
        "api_base",
    ):
        assert unsafe_marker not in serialized_negotiation


def test_model_execution_plan_marks_handoffs_truncated_only_when_items_are_omitted() -> None:
    source_roles: tuple[Mapping[str, JsonValue], ...] = tuple(
        {
            "id": f"worker_{index}",
            "role": "Worker",
            "purpose": "execute",
            "logical_model": "creative",
            "tools": (),
        }
        for index in range(13)
    )
    target_role: Mapping[str, JsonValue] = {
        "id": "final_synthesizer",
        "role": "Final Synthesizer",
        "purpose": "synthesize",
        "logical_model": "main",
        "tools": (),
    }
    source_steps: tuple[Mapping[str, JsonValue], ...] = tuple(
        {
            "id": f"worker_{index}_step",
            "agent": f"worker_{index}",
            "depends_on": (),
            "final_synthesizer": False,
            "tools": (),
        }
        for index in range(13)
    )
    exact_target_step: Mapping[str, JsonValue] = {
        "id": "final_response_step",
        "agent": "final_synthesizer",
        "depends_on": tuple(f"worker_{index}_step" for index in range(12)),
        "final_synthesizer": True,
        "tools": (),
    }
    overflow_target_step: Mapping[str, JsonValue] = {
        "id": "final_response_step",
        "agent": "final_synthesizer",
        "depends_on": tuple(f"worker_{index}_step" for index in range(13)),
        "final_synthesizer": True,
        "tools": (),
    }

    exact_plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Draft a launch campaign.",
        ),
        main_agent_model="main",
        roles=(*source_roles[:12], target_role),
        steps=(
            *source_steps[:12],
            exact_target_step,
        ),
    )

    overflow_plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Draft a launch campaign.",
        ),
        main_agent_model="main",
        roles=(*source_roles, target_role),
        steps=(
            *source_steps,
            overflow_target_step,
        ),
    )

    exact_handoffs = exact_plan["orchestration_handoffs"]
    assert isinstance(exact_handoffs, Mapping)
    exact_items = exact_handoffs["items"]
    assert isinstance(exact_items, tuple)
    assert len(exact_items) == 12
    assert exact_handoffs["truncated"] is False
    exact_contracts = exact_plan["orchestration_contracts"]
    assert isinstance(exact_contracts, Mapping)
    exact_contract_items = exact_contracts["items"]
    assert isinstance(exact_contract_items, tuple)
    assert len(exact_contract_items) == 12
    assert exact_contracts["truncated"] is False
    exact_protocol = exact_plan["orchestration_protocol"]
    assert isinstance(exact_protocol, Mapping)
    assert exact_protocol["handoff_count"] == 12
    assert exact_protocol["contract_count"] == 12
    assert exact_protocol["truncated"] is False
    overflow_handoffs = overflow_plan["orchestration_handoffs"]
    assert isinstance(overflow_handoffs, Mapping)
    overflow_items = overflow_handoffs["items"]
    assert isinstance(overflow_items, tuple)
    assert len(overflow_items) == 12
    assert overflow_handoffs["truncated"] is True
    overflow_contracts = overflow_plan["orchestration_contracts"]
    assert isinstance(overflow_contracts, Mapping)
    overflow_contract_items = overflow_contracts["items"]
    assert isinstance(overflow_contract_items, tuple)
    assert len(overflow_contract_items) == 12
    assert overflow_contracts["truncated"] is True
    overflow_protocol = overflow_plan["orchestration_protocol"]
    assert isinstance(overflow_protocol, Mapping)
    assert overflow_protocol["handoff_count"] == 12
    assert overflow_protocol["contract_count"] == 12
    assert overflow_protocol["truncated"] is True


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
async def test_config_backed_dispatch_runtime_fails_closed_when_tool_roles_have_no_eligible_model() -> None:
    runtime = ConfigBackedDispatchRuntime(
        config_service=FakeConfigService(
            {
                "models": {
                    "main": {
                        "deployments": [
                            {
                                "provider": "anthropic",
                                "model": "claude-sonnet-4-5",
                                "api_base": "https://api.anthropic.com/v1/messages",
                                "credential_ref": "secret://main",
                                "quota_scope_id": "anthropic_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text", "tool_calling", "structured_output"],
                            }
                        ]
                    }
                },
                "agents": [],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id,
            deployments,
        ),
        transport=FakeTransport(),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="生成一个最简单的 hello world Python 项目，必须运行测试。",
                routing_decision={"main_agent_model": "main"},
            )
        )
    ]

    assert [event.kind for event in events] == [EventKind.RUNTIME_FAILED]
    assert events[0].reason == "harness_model_unavailable"


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_routes_inventory_skill_tools_to_capable_model(
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
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://main",
                                "quota_scope_id": "main_account",
                                "max_concurrency": 20,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text", "structured_output"],
                            }
                        ]
                    },
                    "qwen_tools": {
                        "deployments": [
                            {
                                "provider": "qwen",
                                "model": "qwen3-max",
                                "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                                "credential_ref": "secret://qwen",
                                "quota_scope_id": "qwen_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text", "tool_calling", "structured_output"],
                            }
                        ]
                    },
                },
                "agents": [
                    {
                        "id": "scheduler",
                        "role": "Scheduler",
                        "prompt": "Schedule events through the calendar plugin.",
                        "model": "main",
                        "skills": ["calendar_create"],
                    }
                ],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id,
            deployments,
        ),
        transport=FakeTransport(),
        capability_gateway=AvailablePluginManifestCapabilityGateway(),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Create a calendar event.",
                routing_decision={
                    "selected_agent_ids": ("scheduler",),
                    "main_agent_model": "main",
                },
            )
        )
    ]

    assert events[0].kind is EventKind.STEP_STARTED
    role_plan = cast(tuple[Mapping[str, JsonValue], ...], events[0].payload["roles"])
    assert role_plan[0]["id"] == "scheduler"
    assert role_plan[0]["logical_model"] == "qwen_tools"
    assert role_plan[0]["tools"] == ("calendar.create_event",)


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_keeps_role_models_with_harness_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDispatchRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "CrewDispatchRuntime", ProbeDispatchRuntime)
    capacities: list[ImmediateCapacity] = []
    runtime = ConfigBackedDispatchRuntime(
        config_service=FakeConfigService(
            {
                "models": {
                    "main": {
                        "deployments": [
                            {
                                "provider": "openai",
                                "model": "gpt-5.6-sol",
                                "api_base": "https://api.openai.com/v1",
                                "credential_ref": "secret://openai",
                                "quota_scope_id": "openai_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            },
                            {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://deepseek",
                                "quota_scope_id": "deepseek_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text"],
                            },
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
        capacity_factory=lambda tenant_id, deployments: _remember_capacity(
            capacities, tenant_id, deployments
        ),
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
                    "harness_decision": {
                        "selected_provider": "DeepSeek",
                        "selected_model": "deepseek-chat",
                        "selected_logical_model": "main",
                    },
                },
            )
        )
    ]

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
    assert {deployment.provider_model for deployment in capacities[0].deployments} == {
        "deepseek/deepseek-chat",
        "kimi/kimi-k2-latest",
    }
    model_execution_plan_raw = events[0].payload["model_execution_plan"]
    assert isinstance(model_execution_plan_raw, Mapping)
    model_execution_plan = model_execution_plan_raw
    assert model_execution_plan["schema_version"] == 1
    assert model_execution_plan["main_agent"] == {
        "logical_model": "main",
        "selection_source": "harness_decision",
        "harness_constrained": True,
        "selected_provider": "deepseek",
        "selected_model": "deepseek-chat",
        "fallback_policy": "disabled_for_harness_selection",
    }
    assert model_execution_plan["role_model_assignments"] == (
        {
            "role_id": "copywriter",
            "purpose": "execute",
            "logical_model": "creative",
        },
        {
            "role_id": "final_synthesizer",
            "purpose": "synthesize",
            "logical_model": "main",
        },
    )
    assert model_execution_plan["deployment_constraints"] == {
        "schema_version": 1,
        "items": (
            {
                "logical_model": "creative",
                "total_deployments": 1,
                "eligible_deployments": 1,
                "harness_constrained": False,
                "selected_provider": None,
                "selected_model": None,
                "fallback_policy": "disabled_for_harness_selection",
            },
            {
                "logical_model": "main",
                "total_deployments": 2,
                "eligible_deployments": 1,
                "harness_constrained": True,
                "selected_provider": "deepseek",
                "selected_model": "deepseek-chat",
                "fallback_policy": "disabled_for_harness_selection",
            },
        ),
    }
    routing_matrix = model_execution_plan["role_model_routing_matrix"]
    assert isinstance(routing_matrix, tuple)
    assert len(routing_matrix) == 1
    assert isinstance(routing_matrix[0], Mapping)
    copywriter_matrix = routing_matrix[0]
    assert copywriter_matrix["role_id"] == "copywriter"
    assert copywriter_matrix["purpose"] == "execute"
    assert copywriter_matrix["selected_logical_model"] == "creative"
    candidates = copywriter_matrix["candidates"]
    assert copywriter_matrix["candidate_count"] == 2
    assert copywriter_matrix["truncated_candidates"] is True
    assert isinstance(candidates, tuple)
    candidate_mappings: list[Mapping[str, JsonValue]] = []
    for candidate in candidates:
        assert isinstance(candidate, Mapping)
        candidate_mappings.append(candidate)
    selected_candidates = tuple(
        candidate for candidate in candidate_mappings if candidate["selected"] is True
    )
    assert len(selected_candidates) == 1
    assert selected_candidates[0]["logical_model"] == "creative"
    selected_reasons = selected_candidates[0]["reasons"]
    assert isinstance(selected_reasons, tuple)
    assert "preference:role_model" in selected_reasons
    assert {candidate["logical_model"] for candidate in candidate_mappings} == {"creative"}
    assert model_execution_plan["orchestration_handoffs"] == {
        "schema_version": 1,
        "items": (
            {
                "source_step_id": "copywriter_step",
                "target_step_id": "final_response_step",
                "source_role_id": "copywriter",
                "target_role_id": "final_synthesizer",
                "source_purpose": "execute",
                "target_purpose": "synthesize",
                "source_logical_model": "creative",
                "target_logical_model": "main",
                "handoff_kind": "step_dependency",
            },
        ),
        "truncated": False,
    }
    assert model_execution_plan["model_capability_negotiation"] == {
        "schema_version": 1,
        "items": (
            {
                "role_id": "copywriter",
                "logical_model": "creative",
                "required_capabilities": ("text", "structured_output"),
                "matched_capabilities": ("text",),
                "missing_capabilities": ("structured_output",),
                "status": "missing_capability",
            },
            {
                "role_id": "final_synthesizer",
                "logical_model": "main",
                "required_capabilities": ("text", "structured_output"),
                "matched_capabilities": (),
                "missing_capabilities": (),
                "status": "unknown",
            },
        ),
        "role_count": 2,
        "satisfied_count": 0,
        "missing_count": 1,
        "unknown_count": 1,
        "truncated": False,
    }
    assert model_execution_plan == {
        "schema_version": 1,
        "main_agent": {
            "logical_model": "main",
            "selection_source": "harness_decision",
            "harness_constrained": True,
            "selected_provider": "deepseek",
            "selected_model": "deepseek-chat",
            "fallback_policy": "disabled_for_harness_selection",
        },
        "role_model_assignments": (
            {
                "role_id": "copywriter",
                "purpose": "execute",
                "logical_model": "creative",
            },
            {
                "role_id": "final_synthesizer",
                "purpose": "synthesize",
                "logical_model": "main",
            },
        ),
        "deployment_constraints": model_execution_plan["deployment_constraints"],
        "scheduler_selection": {
            "schema_version": 1,
            "source": "harness_decision",
            "selected_logical_model": "main",
            "selected_provider": "deepseek",
            "selected_model": "deepseek-chat",
            "applies_to_main_agent": True,
            "requires_approval": False,
        },
        "gateway_execution_policy": {
            "schema_version": 1,
            "capacity_boundary": "model_gateway",
            "deployment_constraint_applied": True,
            "constrained_logical_model": "main",
            "fallback_policy": "disabled_for_harness_selection",
            "fallback_mappings_enabled": False,
        },
        "role_model_routing_matrix": model_execution_plan["role_model_routing_matrix"],
        "role_model_routing_matrix_truncated": False,
        "orchestration_handoffs": model_execution_plan["orchestration_handoffs"],
        "orchestration_contracts": model_execution_plan["orchestration_contracts"],
        "orchestration_protocol": model_execution_plan["orchestration_protocol"],
        "model_capability_negotiation": model_execution_plan[
            "model_capability_negotiation"
        ],
    }


def test_model_execution_plan_does_not_report_unrelated_constraint_as_main_agent() -> None:
    plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Draft a launch campaign.",
        ),
        main_agent_model="main",
        roles=(
            {
                "id": "copywriter",
                "purpose": "execute",
                "logical_model": "creative",
            },
        ),
        deployment_constraint=DeploymentRoutingConstraint(
            logical_model="creative",
            provider="kimi",
            model="kimi-k2-latest",
        ),
    )

    assert plan["main_agent"] == {
        "logical_model": "main",
        "selection_source": "runtime_default",
        "harness_constrained": False,
        "selected_provider": None,
        "selected_model": None,
        "fallback_policy": "disabled_for_harness_selection",
    }


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_exposes_capability_execution_plan(
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
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://deepseek",
                                "quota_scope_id": "deepseek_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text", "tool_calling", "structured_output"],
                            }
                        ]
                    },
                },
                "agents": [
                    {
                        "id": "writer",
                        "role": "Writer",
                        "prompt": "Draft a document.",
                        "model": "main",
                        "skills": ["read_context", "docx"],
                    }
                ],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id,
            deployments,
        ),
        transport=FakeTransport(),
        capability_gateway=FakeCapabilityAvailability({"docx"}),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Draft a document.",
                routing_decision={"selected_agent_ids": ("writer",)},
            )
        )
    ]

    assert events[0].payload["capability_execution_plan"] == {
        "schema_version": 1,
        "permission_boundary": "runtime_capability_gateway",
        "role_capability_assignments": (
            {
                "role_id": "writer",
                "capabilities": (
                    {
                        "name": "read_context",
                        "replay_safe": True,
                        "approval_policy": "not_required",
                    },
                    {
                        "name": "docx",
                        "replay_safe": False,
                        "approval_policy": "runtime_policy",
                    },
                ),
            },
            {
                "role_id": "final_synthesizer",
                "capabilities": (),
            },
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability_gateway",
    (
        TruthyReplaySafeGateway({"docx"}),
        RaisingReplaySafeGateway({"docx"}),
    ),
)
async def test_config_backed_dispatch_runtime_treats_uncertain_capabilities_as_policy_gated(
    monkeypatch: pytest.MonkeyPatch,
    capability_gateway: FakeCapabilityAvailability,
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
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://deepseek",
                                "quota_scope_id": "deepseek_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text", "tool_calling", "structured_output"],
                            }
                        ]
                    },
                },
                "agents": [
                    {
                        "id": "writer",
                        "role": "Writer",
                        "prompt": "Draft a document.",
                        "model": "main",
                        "skills": ["docx"],
                    }
                ],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id,
            deployments,
        ),
        transport=FakeTransport(),
        capability_gateway=capability_gateway,
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Draft a document.",
                routing_decision={"selected_agent_ids": ("writer",)},
            )
        )
    ]

    capability_plan = cast(
        Mapping[str, JsonValue],
        events[0].payload["capability_execution_plan"],
    )
    role_capability_assignments = cast(
        tuple[Mapping[str, JsonValue], ...],
        capability_plan["role_capability_assignments"],
    )
    capabilities = cast(
        tuple[Mapping[str, JsonValue], ...],
        role_capability_assignments[0]["capabilities"],
    )
    capability = capabilities[0]
    assert capability == {
        "name": "docx",
        "replay_safe": False,
        "approval_policy": "runtime_policy",
    }


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_exposes_capability_inventory(
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
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://deepseek",
                                "quota_scope_id": "deepseek_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text", "tool_calling", "structured_output"],
                            }
                        ]
                    },
                },
                "agents": [
                    {
                        "id": "writer",
                        "role": "Writer",
                        "prompt": "Draft a document.",
                        "model": "main",
                        "skills": ["docx"],
                    }
                ],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id,
            deployments,
        ),
        transport=FakeTransport(),
        capability_gateway=ManifestCapabilityGateway({"docx"}),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Draft a document.",
                routing_decision={"selected_agent_ids": ("writer",)},
            )
        )
    ]

    capability_plan = cast(
        Mapping[str, JsonValue],
        events[0].payload["capability_execution_plan"],
    )
    assert capability_plan["capability_inventory"] == {
        "schema_version": 1,
        "items": (
            {
                "id": "docx",
                "kind": "skill",
                "adapter": "skill_sandbox",
                "permission_class": "skill.use",
                "sandbox_profile": "systemd_skill_sandbox",
                "policy_effect": "inherit",
                "available": True,
                "availability_reason": None,
                "replay_safe": False,
                "aliases": (),
            },
            {
                "id": "filesystem.read_file",
                "kind": "mcp",
                "adapter": "mcp_server",
                "permission_class": "mcp.invoke",
                "sandbox_profile": "mcp_stdio",
                "policy_effect": "inherit",
                "available": False,
                "availability_reason": "mcp_server_not_discovered",
                "replay_safe": False,
                "aliases": (),
            },
        ),
        "truncated": False,
    }
    assignments = cast(
        tuple[Mapping[str, JsonValue], ...],
        capability_plan["role_capability_assignments"],
    )
    assigned_names = {
        capability["name"]
        for assignment in assignments
        for capability in cast(tuple[Mapping[str, JsonValue], ...], assignment["capabilities"])
    }
    assert "docx" in assigned_names
    assert "filesystem.read_file" not in assigned_names


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_omits_invalid_capability_inventory(
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
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://deepseek",
                                "quota_scope_id": "deepseek_account",
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
                        "id": "writer",
                        "role": "Writer",
                        "prompt": "Draft a document.",
                        "model": "main",
                        "skills": ["docx"],
                    }
                ],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id,
            deployments,
        ),
        transport=FakeTransport(),
        capability_gateway=BadManifestCapabilityGateway(RuntimeError("manifest unavailable")),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Draft a document.",
                routing_decision={"selected_agent_ids": ("writer",)},
            )
        )
    ]

    capability_plan = cast(
        Mapping[str, JsonValue],
        events[0].payload["capability_execution_plan"],
    )
    assert "capability_inventory" not in capability_plan


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_bounds_capability_inventory(
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
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://deepseek",
                                "quota_scope_id": "deepseek_account",
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
                        "id": "writer",
                        "role": "Writer",
                        "prompt": "Draft a document.",
                        "model": "main",
                        "skills": ["docx"],
                    }
                ],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id,
            deployments,
        ),
        transport=FakeTransport(),
        capability_gateway=BadManifestCapabilityGateway(
            {
                "schema_version": 1,
                "capabilities": (
                    {
                        "id": "bad tool",
                        "kind": "mcp",
                    },
                    *(
                        {
                            "id": f"plugin.tool_{index}",
                            "kind": "plugin" + ("x" * 100),
                            "adapter": "plugin_registry",
                            "permission_class": "plugin.use",
                            "sandbox_profile": "remote_connector",
                            "available": True,
                            "availability_reason": "x" * 200,
                            "replay_safe": False,
                            "aliases": (
                                *(f"alias_{alias_index}" for alias_index in range(40)),
                                "bad alias",
                            ),
                        }
                        for index in range(300)
                    ),
                ),
            }
        ),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Draft a document.",
                routing_decision={"selected_agent_ids": ("writer",)},
            )
        )
    ]

    capability_plan = cast(
        Mapping[str, JsonValue],
        events[0].payload["capability_execution_plan"],
    )
    inventory = cast(Mapping[str, JsonValue], capability_plan["capability_inventory"])
    items = cast(tuple[Mapping[str, JsonValue], ...], inventory["items"])
    assert inventory["truncated"] is True
    assert len(items) == 96
    assert items[0]["id"] == "plugin.tool_0"
    assert items[0]["kind"] == "unknown"
    assert items[0]["availability_reason"] is None
    assert items[0]["aliases"] == tuple(f"alias_{index}" for index in range(16))
    assert "bad tool" not in {item["id"] for item in items}


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_bounds_invalid_inventory_scan(
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
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://deepseek",
                                "quota_scope_id": "deepseek_account",
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
                        "id": "writer",
                        "role": "Writer",
                        "prompt": "Draft a document.",
                        "model": "main",
                        "skills": ["docx"],
                    }
                ],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id,
            deployments,
        ),
        transport=FakeTransport(),
        capability_gateway=BadManifestCapabilityGateway(
            {
                "schema_version": 1,
                "capabilities": tuple({"id": f"bad tool {index}"} for index in range(600)),
            }
        ),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Draft a document.",
                routing_decision={"selected_agent_ids": ("writer",)},
            )
        )
    ]

    capability_plan = cast(
        Mapping[str, JsonValue],
        events[0].payload["capability_execution_plan"],
    )
    inventory = cast(Mapping[str, JsonValue], capability_plan["capability_inventory"])
    assert inventory["items"] == ()
    assert inventory["truncated"] is True


@pytest.mark.parametrize(
    "manifest",
    (
        {"schema_version": 2, "capabilities": ()},
        {"schema_version": 1, "capabilities": {"id": "docx"}},
    ),
)
def test_capability_inventory_payload_omits_bad_manifest_shapes(
    manifest: Mapping[str, JsonValue],
) -> None:
    assert (
        _capability_inventory_payload(
            TENANT_ID,
            capability_gateway=BadManifestCapabilityGateway(manifest),
        )
        is None
    )


def test_capability_inventory_payload_sanitizes_manifest_tokens() -> None:
    inventory = _capability_inventory_payload(
        TENANT_ID,
        capability_gateway=BadManifestCapabilityGateway(
            {
                "schema_version": 1,
                "capabilities": (
                    "not a mapping",
                    {
                        "id": "plugin.safe_tool",
                        "kind": "secret_plugin",
                        "adapter": "adapter with spaces",
                        "permission_class": "plugin.use",
                        "sandbox_profile": "token_sandbox",
                        "available": True,
                        "availability_reason": "bearer_token",
                        "replay_safe": False,
                        "aliases": ("safe_alias", "secret_alias"),
                    },
                ),
            }
        ),
    )

    assert inventory is not None
    items = cast(tuple[Mapping[str, JsonValue], ...], inventory["items"])
    assert items == (
        {
            "id": "plugin.safe_tool",
            "kind": "unknown",
            "adapter": "unknown",
            "permission_class": "plugin.use",
            "sandbox_profile": "unknown",
            "policy_effect": "inherit",
            "available": True,
            "availability_reason": None,
            "replay_safe": False,
            "aliases": ("safe_alias",),
        },
    )


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_injects_harness_tool_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDispatchRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "CrewDispatchRuntime", ProbeDispatchRuntime)
    harness = object()
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
                                "capabilities": ["text", "tool_calling", "structured_output"],
                            }
                        ]
                    }
                },
                "agents": [],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id, deployments
        ),
        transport=FakeTransport(),
        harness_tool_gateway=harness,  # type: ignore[arg-type]
    )

    _ = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Use a runtime tool.",
            )
        )
    ]

    assert ProbeDispatchRuntime.instances[-1].harness_tool_gateway is harness


@pytest.mark.asyncio
async def test_config_backed_discussion_runtime_injects_harness_tool_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDiscussionRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "AutoGenDiscussionRuntime", ProbeDiscussionRuntime)
    harness = object()
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
                                "capabilities": ["text", "tool_calling", "structured_output"],
                            }
                        ]
                    }
                },
                "agents": [],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id, deployments
        ),
        transport=FakeTransport(),
        harness_tool_gateway=harness,  # type: ignore[arg-type]
    )

    _ = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISCUSS,
                request="Use a runtime tool.",
            )
        )
    ]

    assert ProbeDiscussionRuntime.instances[-1].harness_tool_gateway is harness


@pytest.mark.asyncio
async def test_config_backed_hybrid_runtime_injects_harness_tool_gateway_into_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDispatchRuntime.instances.clear()
    ProbeDiscussionRuntime.instances.clear()
    ProbeHybridRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "CrewDispatchRuntime", ProbeDispatchRuntime)
    monkeypatch.setattr(defaults_module, "AutoGenDiscussionRuntime", ProbeDiscussionRuntime)
    monkeypatch.setattr(defaults_module, "HybridRuntime", ProbeHybridRuntime)
    harness = object()
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
                                "capabilities": ["text", "tool_calling", "structured_output"],
                            }
                        ]
                    }
                },
                "agents": [],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id, deployments
        ),
        transport=FakeTransport(),
        harness_tool_gateway=harness,  # type: ignore[arg-type]
    )

    _ = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.HYBRID,
                request="Use runtime tools in each stage.",
            )
        )
    ]

    assert ProbeDispatchRuntime.instances[-1].harness_tool_gateway is harness
    assert ProbeDiscussionRuntime.instances[-1].harness_tool_gateway is harness
    hybrid = cast(ProbeHybridRuntime, ProbeHybridRuntime.instances[-1])
    assert hybrid.dispatch is ProbeDispatchRuntime.instances[-1]
    assert hybrid.discussion is ProbeDiscussionRuntime.instances[-1]


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
async def test_config_backed_discussion_runtime_prepares_tenant_before_inventory_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDiscussionRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "AutoGenDiscussionRuntime", ProbeDiscussionRuntime)
    capability_gateway = TenantPreparedPluginManifestCapabilityGateway()
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
                                "capabilities": ["text", "tool_calling", "structured_output"],
                            }
                        ]
                    },
                },
                "agents": [
                    {
                        "id": "scheduler",
                        "role": "Scheduler",
                        "prompt": "Schedule the work.",
                        "model": "main",
                        "skills": ["read_context"],
                    },
                    {
                        "id": "reviewer",
                        "role": "Reviewer",
                        "prompt": "Review the schedule.",
                        "model": "main",
                        "skills": [],
                    },
                ],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id,
            deployments,
        ),
        transport=FakeTransport(),
        capability_gateway=capability_gateway,
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISCUSS,
                request="Use calendar.create_event to schedule the review.",
                routing_decision={
                    "selected_agent_ids": ("scheduler", "reviewer"),
                    "main_agent_model": "main",
                },
            )
        )
    ]

    assert capability_gateway.prepared_tenants == [TENANT_ID]
    roles = cast(tuple[Mapping[str, JsonValue], ...], events[0].payload["roles"])
    scheduler = next(role for role in roles if role["id"] == "scheduler")
    assert scheduler["tools"] == ("read_context", "calendar.create_event")
    steps = cast(tuple[Mapping[str, JsonValue], ...], events[0].payload["steps"])
    scheduler_step = next(step for step in steps if step["agent"] == "scheduler")
    assert scheduler_step["tools"] == ("read_context", "calendar.create_event")


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
            initialized_scope_ids.append(
                tuple(deployment.id for deployment in self.deployments)
            )
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
            scoped_deployment_ids.append(
                tuple(deployment.id for deployment in configured)
            )
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


def test_dispatch_plan_includes_hermes_memory_context_in_steps() -> None:
    role = RoleAssignment(
        id="reviewer",
        role="Reviewer",
        purpose=RolePurpose.VERIFY,
        mission="Review output quality.",
        must_answer=("What risks remain?",),
        allowed_tools=(),
        forbidden_actions=("Do not perform dangerous operations.",),
        skills=(),
        output_schema={"summary": "string"},
        model="main",
    )
    routing_decision: dict[str, JsonValue] = {
        "hermes": {
            "injected_memories": (
                {
                    "summary": "reviewer 超时时先压缩上下文再分块审查。",
                    "memory_type": "error_handling",
                    "target": "reviewer",
                    "reason": "命中 reviewer 超时经验",
                },
            )
        }
    }
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="审查脚本",
        artifacts=(),
        timeout_seconds=60,
        token_budget=10_000,
        routing_decision=routing_decision,
    )

    plan = _dispatch_plan((role,), context, max_parallelism=1)

    assert any("HERMES_MEMORY_CONTEXT" in step.task for step in plan.steps)
    assert any("reviewer 超时时先压缩上下文再分块审查" in step.task for step in plan.steps)


def test_dispatch_plan_reserves_more_time_for_post_product_review_roles() -> None:
    roles = (
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
            request="Review this campaign proposal.",
            timeout_seconds=1200,
        ),
        max_parallelism=2,
    )

    steps = {step.id: step for step in plan.steps}
    assert steps["writer_step"].timeout_seconds == 300
    assert steps["quality_reviewer_step"].timeout_seconds > steps["writer_step"].timeout_seconds
    assert 540 <= steps["quality_reviewer_step"].timeout_seconds <= 600
    assert steps["final_response_step"].timeout_seconds >= 540
    assert steps["final_response_step"].timeout_seconds <= 600


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


def test_dispatch_plan_adds_explicitly_mentioned_available_mcp_tool() -> None:
    roles = (
        RoleAssignment(
            id="researcher",
            role="Researcher",
            purpose=RolePurpose.EXECUTE,
            mission="Research the answer.",
            must_answer=("What did the researcher find?",),
            allowed_tools=("read_context",),
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
            request="Use search.web_search for external research.",
        ),
        capability_gateway=AvailableMcpManifestCapabilityGateway(),
    )

    researcher = next(agent for agent in plan.agents if agent.id == "researcher")
    researcher_step = next(step for step in plan.steps if step.agent == "researcher")
    assert researcher.allowed_tools == ("read_context", "search.web_search")
    assert researcher_step.tools == ("read_context", "search.web_search")
    assert "search.web_search" in plan.allowed_tools


def test_dispatch_plan_adds_available_mcp_tool_when_role_requests_alias() -> None:
    roles = (
        RoleAssignment(
            id="researcher",
            role="Researcher",
            purpose=RolePurpose.EXECUTE,
            mission="Research the answer.",
            must_answer=("What did the researcher find?",),
            allowed_tools=("read_context",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=("search_web",),
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
            request="Research the topic.",
        ),
        capability_gateway=AvailableMcpManifestCapabilityGateway(),
    )

    researcher = next(agent for agent in plan.agents if agent.id == "researcher")
    assert researcher.allowed_tools == ("read_context", "search.web_search")


def test_dispatch_plan_does_not_add_mcp_tool_for_partial_task_text_match() -> None:
    roles = (
        RoleAssignment(
            id="researcher",
            role="Researcher",
            purpose=RolePurpose.EXECUTE,
            mission="Research the answer.",
            must_answer=("What did the researcher find?",),
            allowed_tools=("read_context",),
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
            request="Search the topic broadly.",
        ),
        capability_gateway=AvailableMcpManifestCapabilityGateway(),
    )

    researcher = next(agent for agent in plan.agents if agent.id == "researcher")
    assert researcher.allowed_tools == ("read_context",)


def test_dispatch_plan_adds_explicitly_mentioned_available_plugin_tool() -> None:
    roles = (
        RoleAssignment(
            id="scheduler",
            role="Scheduler",
            purpose=RolePurpose.EXECUTE,
            mission="Schedule the work.",
            must_answer=("What event was scheduled?",),
            allowed_tools=("read_context",),
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
            request="Use calendar.create_event to schedule the review.",
        ),
        capability_gateway=AvailablePluginManifestCapabilityGateway(),
    )

    scheduler = next(agent for agent in plan.agents if agent.id == "scheduler")
    scheduler_step = next(step for step in plan.steps if step.agent == "scheduler")
    assert scheduler.allowed_tools == ("read_context", "calendar.create_event")
    assert scheduler_step.tools == ("read_context", "calendar.create_event")
    assert "calendar.create_event" in plan.allowed_tools


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_prepares_tenant_before_inventory_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDispatchRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "CrewDispatchRuntime", ProbeDispatchRuntime)
    capability_gateway = TenantPreparedPluginManifestCapabilityGateway()
    runtime = ConfigBackedDispatchRuntime(
        config_service=FakeConfigService(
            {
                "models": {
                    "main": {
                        "deployments": [
                            {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://deepseek",
                                "quota_scope_id": "deepseek_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text", "tool_calling", "structured_output"],
                            }
                        ]
                    },
                },
                "agents": [
                    {
                        "id": "scheduler",
                        "role": "Scheduler",
                        "prompt": "Schedule the work.",
                        "model": "main",
                        "skills": ["read_context"],
                    }
                ],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
            tenant_id,
            deployments,
        ),
        transport=FakeTransport(),
        capability_gateway=capability_gateway,
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Use calendar.create_event to schedule the review.",
                routing_decision={"selected_agent_ids": ("scheduler",)},
            )
        )
    ]

    assert capability_gateway.prepared_tenants == [TENANT_ID]
    capability_plan = cast(
        Mapping[str, JsonValue],
        events[0].payload["capability_execution_plan"],
    )
    assignments = cast(
        tuple[Mapping[str, JsonValue], ...],
        capability_plan["role_capability_assignments"],
    )
    scheduler = next(
        assignment for assignment in assignments if assignment["role_id"] == "scheduler"
    )
    assert tuple(
        capability["name"]
        for capability in cast(tuple[Mapping[str, JsonValue], ...], scheduler["capabilities"])
    ) == ("read_context", "calendar.create_event")


def test_dispatch_plan_adds_available_plugin_tool_when_role_requests_alias() -> None:
    roles = (
        RoleAssignment(
            id="scheduler",
            role="Scheduler",
            purpose=RolePurpose.EXECUTE,
            mission="Schedule the work.",
            must_answer=("What event was scheduled?",),
            allowed_tools=("read_context",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=("calendar_create",),
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
            request="Schedule the review.",
        ),
        capability_gateway=AvailablePluginManifestCapabilityGateway(),
    )

    scheduler = next(agent for agent in plan.agents if agent.id == "scheduler")
    assert scheduler.allowed_tools == ("read_context", "calendar.create_event")


def test_dispatch_plan_does_not_add_plugin_tool_for_partial_task_text_match() -> None:
    roles = (
        RoleAssignment(
            id="scheduler",
            role="Scheduler",
            purpose=RolePurpose.EXECUTE,
            mission="Schedule the work.",
            must_answer=("What event was scheduled?",),
            allowed_tools=("read_context",),
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
            request="Put the review on the calendar.",
        ),
        capability_gateway=AvailablePluginManifestCapabilityGateway(),
    )

    scheduler = next(agent for agent in plan.agents if agent.id == "scheduler")
    assert scheduler.allowed_tools == ("read_context",)


def test_dispatch_plan_requires_verification_before_final_project_zip() -> None:
    roles = (
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Build the requested project.",
            must_answer=("What code was produced?",),
            allowed_tools=("run_safe_command", "project.generate_zip"),
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
            request="生成一个最简单的 hello world Node 项目，并给我可下载压缩包。",
        ),
        capability_gateway=FakeCapabilityAvailability({"run_safe_command", "project.generate_zip"}),
    )

    implementer_step = next(step for step in plan.steps if step.agent == "implementer")
    final_step = next(step for step in plan.steps if step.id == "final_response_step")
    assert "Run an available safe command smoke test before final packaging" in implementer_step.task
    assert "List every file path included in the ZIP" in implementer_step.task
    assert "project.generate_zip" in implementer_step.task
    assert "final_attachment" in implementer_step.task
    assert "Do not claim the project works without verification evidence" in final_step.task


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


def test_role_model_routing_matrix_explains_capacity_balanced_selection() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "deepseek": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://deepseek",
                            "quota_scope_id": "deepseek",
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
            id=f"planner_{index}",
            role="规划助手",
            purpose=RolePurpose.EXECUTE,
            mission="规划代码实现、测试和交付步骤。",
            must_answer=("计划是什么？",),
            allowed_tools=(),
            forbidden_actions=("不要执行危险操作。",),
            skills=(),
            output_schema={},
            model="deepseek",
        )
        for index in range(2)
    )

    assigned = _assign_models_to_roles(
        roles,
        config,
        default_model="deepseek",
        task="规划代码实现、测试和交付步骤。",
    )
    matrix, truncated = _role_model_routing_matrix_payload(
        roles,
        assigned,
        config,
        default_model="deepseek",
        task="规划代码实现、测试和交付步骤。",
    )

    assert truncated is False
    assert assigned[0].model == "qwen"
    assert assigned[1].model == "deepseek"
    second = matrix[1]
    assert second["selected_logical_model"] == "deepseek"
    candidates = second["candidates"]
    assert isinstance(candidates, tuple)
    assert len(candidates) == 1
    assert isinstance(candidates[0], Mapping)
    reasons = candidates[0]["reasons"]
    assert isinstance(reasons, tuple)
    assert "capacity_adjustment:selected_after_balance" in reasons


def test_role_model_routing_matrix_caps_event_payload_and_hides_unselected_models() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "main": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://main",
                            "quota_scope_id": "main",
                            "capabilities": ["text"],
                        }
                    ]
                },
                "private_reviewer": {
                    "deployments": [
                        {
                            "provider": "anthropic",
                            "model": "claude-sonnet-4-5",
                            "api_base": "https://api.anthropic.com/v1/messages",
                            "credential_ref": "secret://private",
                            "quota_scope_id": "private",
                            "capabilities": ["text"],
                        }
                    ]
                },
            },
            "agents": [],
        }
    )
    roles = tuple(
        RoleAssignment(
            id=f"agent_{index}",
            role="通用助手",
            purpose=RolePurpose.EXECUTE,
            mission="整理信息并给出建议。",
            must_answer=("建议是什么？",),
            allowed_tools=(),
            forbidden_actions=("不要执行危险操作。",),
            skills=(),
            output_schema={},
            model="main",
        )
        for index in range(25)
    )
    assigned = tuple(replace(role, model="main") for role in roles)

    matrix, truncated = _role_model_routing_matrix_payload(
        roles,
        assigned,
        config,
        default_model="main",
        task="整理普通问题。",
    )

    assert truncated is True
    assert len(matrix) == 24
    assert all(entry["candidate_count"] == 2 for entry in matrix)
    assert all(entry["truncated_candidates"] is True for entry in matrix)
    assert "private_reviewer" not in repr(matrix)


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


def test_role_model_assignment_fails_closed_when_tool_role_has_no_eligible_model() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "sonnet": {
                    "deployments": [
                        {
                            "provider": "anthropic",
                            "model": "claude-sonnet-4-5",
                            "api_base": "https://api.anthropic.com/v1/messages",
                            "credential_ref": "secret://sonnet",
                            "quota_scope_id": "sonnet",
                            "max_concurrency": 3,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
                "plain": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://plain",
                            "quota_scope_id": "plain",
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
    role = RoleAssignment(
        id="planner",
        role="Planner",
        purpose=RolePurpose.EXECUTE,
        mission="拆解任务并调用工具读取上下文。",
        must_answer=("步骤是什么？",),
        allowed_tools=("read_context",),
        forbidden_actions=("不要执行危险操作。",),
        skills=(),
        output_schema={},
        model="sonnet",
    )

    with pytest.raises(
        defaults_module.HarnessModelSelectionError,
        match="model capability unavailable",
    ):
        _assign_models_to_roles(
            (role,),
            config,
            default_model="sonnet",
            task="读取上下文后生成计划。",
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
