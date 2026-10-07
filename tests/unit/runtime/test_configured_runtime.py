import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest
from pydantic import ValidationError

import agent_hub.runtime.defaults as defaults_module
from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.config.schema import PlatformConfig
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.project_scale import build_project_scale_run_plan
from agent_hub.harness.project_scale_runner import _discussion_trace_payload_passes
from agent_hub.models.capacity import CapacityLease, CapacityWaitTimeout
from agent_hub.models.gateway import CapacityController
from agent_hub.models.routing_policy import DeploymentRoutingConstraint
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    EventKind,
    ExecutionRuntime,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.plan import DispatchPlan
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
    _PlannedRuntime,
    _prepare_capability_gateway_for_tenant,
    _role_model_fallbacks_by_id,
    _role_model_routing_matrix_payload,
    _select_logical_model_for_role,
    _selected_config_role_assignments,
    configured_runtime_registry,
)
from agent_hub.runtime.direct import RuntimeExecutionError
from agent_hub.runtime.role_planner import (
    RoleAssignment,
    RolePlanner,
    RolePlanningRequest,
    RolePurpose,
    TaskProfile,
)

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")


def test_python_project_zip_request_is_profiled_as_software() -> None:
    profiles = defaults_module._task_profiles(
        "生成一个最简单的 hello world Python 项目。必须产出可下载 zip，"
        "zip 内至少包含 main.py，main.py 运行后输出 hello world。"
    )

    assert TaskProfile.SOFTWARE in profiles


def test_natural_chinese_website_request_is_profiled_as_software() -> None:
    profiles = defaults_module._task_profiles("编写一个网盘网站")

    assert TaskProfile.SOFTWARE in profiles


def test_project_scale_medium_capability_task_is_bounded_for_role_planning() -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("medium",),
        flows=("hybrid",),
        execute=True,
    )
    message = str(plan.requests[0].body["message"])

    planning_task = defaults_module._role_planning_task(message)

    assert len(message) > 2_000
    assert planning_task == planning_task.strip()
    assert len(planning_task) <= 2_000
    assert planning_task.startswith("Build a real medium business project")
    assert "preserve decision evidence" in planning_task
    RolePlanningRequest(task=planning_task, mode=TaskMode.DISPATCH)


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


class ProjectPreflightCapabilityGateway(FakeCapabilityAvailability):
    def __init__(self) -> None:
        super().__init__({"project.preflight_architecture"})

    def is_replay_safe(self, name: str) -> bool:
        return name == "project.preflight_architecture"


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
                    "failure_codes": (
                        "plugin.timeout",
                        "plugin.schema_validation_failed",
                    ),
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
                    "failure_codes": ("mcp.server_failed",),
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


class RefreshingTenantPreparedCapabilityGateway(TenantPreparedPluginManifestCapabilityGateway):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def refresh_tenant(self, tenant_id: UUID) -> None:
        assert tenant_id == TENANT_ID
        self.calls.append("refresh")

    async def ensure_tenant_loaded(self, tenant_id: UUID) -> None:
        await super().ensure_tenant_loaded(tenant_id)
        self.calls.append("ensure")


@pytest.mark.asyncio
async def test_self_repair_runtime_plan_refreshes_capabilities_before_prepare() -> None:
    gateway = RefreshingTenantPreparedCapabilityGateway()

    await _prepare_capability_gateway_for_tenant(
        TENANT_ID,
        capability_gateway=gateway,
        routing_decision={
            "source": "self_repair",
            "self_repair_accepted": True,
            "self_repair_context": {
                "source": "self_repair",
                "failure_kind": "plugin_runtime_unavailable",
                "recovery_strategy": "repair_plugin_endpoint_or_adapter_and_retry",
                "requires_approval": False,
                "automatic_execution": True,
            },
        },
    )

    assert gateway.calls == ["refresh", "ensure"]


@pytest.mark.asyncio
async def test_unaccepted_self_repair_plan_does_not_refresh_capabilities() -> None:
    gateway = RefreshingTenantPreparedCapabilityGateway()

    await _prepare_capability_gateway_for_tenant(
        TENANT_ID,
        capability_gateway=gateway,
        routing_decision={
            "source": "self_repair",
            "self_repair_accepted": False,
            "self_repair_context": {
                "source": "self_repair",
                "failure_kind": "plugin_runtime_unavailable",
                "recovery_strategy": "repair_plugin_endpoint_or_adapter_and_retry",
                "requires_approval": False,
                "automatic_execution": True,
            },
        },
    )

    assert gateway.calls == ["ensure"]


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
        estimated_tokens: int | Mapping[str, int],
    ) -> CapacityLease:
        self.wait_timeouts.append(wait_timeout)
        if isinstance(estimated_tokens, Mapping):
            assert all(value > 0 for value in estimated_tokens.values())
        else:
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
        estimated_tokens: int | Mapping[str, int],
    ) -> CapacityLease:
        self.wait_timeouts.append(wait_timeout)
        self.events = getattr(self, "events", [])
        self.events.append(tuple(deployment.provider_model for deployment in candidates))
        if isinstance(estimated_tokens, Mapping):
            assert all(value > 0 for value in estimated_tokens.values())
        else:
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
    mode = TaskMode.DISPATCH
    instances: ClassVar[list["ProbeDispatchRuntime"]] = []

    def __init__(
        self,
        gateway: object,
        plan: object,
        *,
        capability_gateway: object | None = None,
        harness_tool_gateway: object | None = None,
        artifact_repository: object | None = None,
    ) -> None:
        del gateway, capability_gateway
        self.plan = plan
        self.harness_tool_gateway = harness_tool_gateway
        self.artifact_repository = artifact_repository
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

    def __init__(
        self,
        dispatch: object,
        discussion: object,
        direct: object,
        *,
        artifact_repository: object | None = None,
    ) -> None:
        del direct
        self.dispatch = dispatch
        self.discussion = discussion
        self.artifact_repository = artifact_repository
        self.contexts: list[TaskContext] = []
        self.instances.append(self)


class CancellableProbeRuntime:
    mode = TaskMode.DISPATCH

    def __init__(self) -> None:
        self.cancel_count = 0

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        raise AssertionError(f"not used: {context.run_id}")
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=context.run_id)

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError(f"not used: {checkpoint.id}")

    async def cancel(self) -> None:
        self.cancel_count += 1


def _main_plan_event(events: Sequence[RunEvent]) -> RunEvent:
    return next(event for event in events if event.step_id == "main_agent_plan")


def _plan_evidence_pages(
    events: Sequence[RunEvent],
    evidence_kind: str,
) -> tuple[Mapping[str, JsonValue], ...]:
    return tuple(
        event.payload
        for event in events
        if event.kind == "runtime.plan_created"
        and event.payload.get("evidence_kind") == evidence_kind
    )


def _role_step_plan_from_events(
    events: Sequence[RunEvent],
) -> tuple[tuple[Mapping[str, JsonValue], ...], tuple[Mapping[str, JsonValue], ...]]:
    roles: list[Mapping[str, JsonValue]] = []
    steps: list[Mapping[str, JsonValue]] = []
    for page in _plan_evidence_pages(events, "role_step_plan"):
        raw_items = page.get("items")
        assert isinstance(raw_items, tuple)
        items = cast(tuple[Mapping[str, JsonValue], ...], raw_items)
        if page.get("section") == "roles":
            roles.extend(items)
        elif page.get("section") == "steps":
            steps.extend(items)
    return tuple(roles), tuple(steps)


def _model_execution_plan_from_events(events: Sequence[RunEvent]) -> Mapping[str, JsonValue]:
    model_pages = _plan_evidence_pages(events, "model_execution_plan")
    assert len(model_pages) == 1
    raw_model = model_pages[0].get("details")
    assert isinstance(raw_model, Mapping)
    plan = dict(raw_model)
    orchestration_pages = _plan_evidence_pages(events, "orchestration_handoff_plan")
    assert len(orchestration_pages) == 1
    raw_orchestration = orchestration_pages[0].get("details")
    assert isinstance(raw_orchestration, Mapping)
    plan.update(raw_orchestration)
    return plan


def _discussion_trace_from_events(events: Sequence[RunEvent]) -> Mapping[str, JsonValue]:
    pages = _plan_evidence_pages(events, "dispatch_discussion_trace")
    assert len(pages) == 1
    details = pages[0].get("details")
    assert isinstance(details, Mapping)
    return details


def _capability_execution_plan_from_events(
    events: Sequence[RunEvent],
) -> Mapping[str, JsonValue]:
    pages = _plan_evidence_pages(events, "capability_execution_plan")
    assert pages
    summary = next(page for page in pages if page.get("section") == "summary")
    raw_summary = summary.get("details")
    assert isinstance(raw_summary, Mapping)
    capabilities_by_role: dict[str, list[Mapping[str, JsonValue]]] = {}
    inventory_items: list[Mapping[str, JsonValue]] = []
    inventory_schema_version: JsonValue = 1
    inventory_truncated = raw_summary.get("inventory_truncated") is True
    for page in pages:
        details = page.get("details")
        assert isinstance(details, Mapping)
        if page.get("section") == "role_capability_assignments":
            role_id = details.get("role_id")
            assert isinstance(role_id, str)
            raw_capabilities = details.get("capabilities")
            assert isinstance(raw_capabilities, tuple)
            capabilities_by_role.setdefault(role_id, []).extend(
                cast(tuple[Mapping[str, JsonValue], ...], raw_capabilities)
            )
        elif page.get("section") == "capability_inventory":
            raw_items = details.get("items")
            assert isinstance(raw_items, tuple)
            inventory_items.extend(cast(tuple[Mapping[str, JsonValue], ...], raw_items))
            inventory_schema_version = details.get("schema_version", 1)
            inventory_truncated = details.get("truncated") is True
    plan: dict[str, JsonValue] = {
        "schema_version": raw_summary.get("schema_version", 1),
        "permission_boundary": raw_summary.get(
            "permission_boundary", "runtime_capability_gateway"
        ),
        "role_capability_assignments": tuple(
            {"role_id": role_id, "capabilities": tuple(capabilities)}
            for role_id, capabilities in capabilities_by_role.items()
        ),
    }
    if raw_summary.get("inventory_item_count") or raw_summary.get("inventory_truncated") is True:
        plan["capability_inventory"] = {
            "schema_version": inventory_schema_version,
            "items": tuple(inventory_items),
            "truncated": inventory_truncated,
        }
    return plan


@pytest.mark.asyncio
async def test_planned_runtime_emits_compact_plan_index_and_trusted_events_before_child() -> None:
    run_id = uuid4()
    runtime = _PlannedRuntime(
        ProbeDispatchRuntime(None, object()),
        mode=TaskMode.DISPATCH,
        main_agent_model="main",
        roles=(
            {
                "id": "architect",
                "role": "Architect",
                "purpose": "plan",
                "logical_model": "main",
                "tools": ("read_context",),
                "has_output_schema": True,
            },
            {
                "id": "implementer",
                "role": "Implementer",
                "purpose": "execute",
                "logical_model": "main",
                "tools": ("workspace.write_text",),
                "has_output_schema": True,
            },
        ),
        steps=(
            {
                "id": "architecture_step",
                "agent": "architect",
                "depends_on": (),
                "final_synthesizer": False,
                "tools": ("read_context",),
            },
            {
                "id": "implementation_step",
                "agent": "implementer",
                "depends_on": ("architecture_step",),
                "final_synthesizer": False,
                "tools": ("workspace.write_text",),
            },
        ),
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=run_id,
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Build the project.",
            )
        )
    ]

    main_plan = next(event for event in events if event.step_id == "main_agent_plan")
    assert set(main_plan.payload) == {
        "mode",
        "main_agent_model",
        "logical_model",
        "task",
        "summary",
        "roles",
        "steps",
        "detail_references",
    }
    assert main_plan.payload["roles"] == {
        "count": 2,
        "ids": ("architect", "implementer"),
        "truncated": False,
    }
    assert main_plan.payload["steps"] == {
        "count": 2,
        "ids": ("architecture_step", "implementation_step"),
        "truncated": False,
    }
    assert "model_execution_plan" not in main_plan.payload
    assert "capability_execution_plan" not in main_plan.payload
    assert "dispatch_discussion_trace" not in main_plan.payload
    assert events[0].kind == "runtime.context_loaded"
    assert any(event.kind == "runtime.plan_created" for event in events[:-1])
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].sequence == len(events)


@pytest.mark.asyncio
async def test_planned_runtime_pages_maximum_capability_evidence_without_validation_error() -> None:
    tools = tuple(f"plugin.tool_{index}" for index in range(96))
    roles: tuple[Mapping[str, JsonValue], ...] = tuple(
        {
            "id": f"worker_{index}",
            "role": f"Worker {index}",
            "purpose": "execute",
            "logical_model": "main",
            "tools": tools,
            "has_output_schema": True,
        }
        for index in range(24)
    )
    steps = tuple(
        {
            "id": f"step_{index}",
            "agent": f"worker_{index % len(roles)}",
            "depends_on": (() if index == 0 else (f"step_{index - 1}",)),
            "final_synthesizer": False,
            "tools": tools,
        }
        for index in range(256)
    )
    capability_gateway = BadManifestCapabilityGateway(
        {
            "schema_version": 1,
            "capabilities": tuple(
                {
                    "id": tool,
                    "kind": "plugin",
                    "adapter": "plugin_registry",
                    "permission_class": "plugin.use",
                    "sandbox_profile": "remote_connector",
                    "policy_effect": "inherit",
                    "available": True,
                    "availability_reason": None,
                    "failure_codes": ("plugin.timeout",),
                    "replay_safe": False,
                    "aliases": (),
                }
                for tool in tools
            ),
        }
    )
    runtime = _PlannedRuntime(
        ProbeDispatchRuntime(None, object()),
        mode=TaskMode.DISPATCH,
        main_agent_model="main",
        roles=roles,
        steps=steps,
        capability_gateway=capability_gateway,
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Build an ultra project.",
                routing_decision={"project_scale": "ultra"},
            )
        )
    ]

    capability_pages = [
        event
        for event in events
        if event.kind == "runtime.plan_created"
        and event.payload.get("evidence_kind") == "capability_execution_plan"
    ]
    assert len(capability_pages) > 1
    assert all(event.payload["page_count"] == len(capability_pages) for event in capability_pages)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


def test_role_step_plan_summarizes_one_oversized_schema_within_event_limits() -> None:
    oversized_role: Mapping[str, JsonValue] = {
        "id": "architect",
        "role": "Architect",
        "purpose": "plan",
        "logical_model": "main",
        "output_schema": {
            f"field_{index}": {
                "type": "string",
                "description": f"Architecture field {index}",
            }
            for index in range(1_200)
        },
    }

    pages = defaults_module._role_step_plan_evidence_pages(
        roles=(oversized_role,),
        steps=(),
    )

    assert len(pages) == 1
    items = cast(tuple[Mapping[str, JsonValue], ...], pages[0]["items"])
    assert items == (
        {
            "id": "architect",
            "summary": "Oversized plan item omitted; use the configured runtime definition.",
            "truncated": True,
            "original_node_count": defaults_module._estimated_json_nodes(oversized_role),
            "top_level_keys": (
                "id",
                "logical_model",
                "output_schema",
                "purpose",
                "role",
            ),
        },
    )
    event = RunEvent(
        kind="runtime.plan_created",
        sequence=1,
        run_id=uuid4(),
        payload=pages[0],
    )
    assert RunEvent.from_payload(event.to_payload()).payload["items"] == items


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
    assert len(capacities[0].wait_timeouts) == 1
    assert 59.0 < capacities[0].wait_timeouts[0] <= 60.0
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
async def test_config_backed_direct_runtime_applies_low_cost_model_selection_policy() -> None:
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
                                "credential_ref": "secret://expensive",
                                "quota_scope_id": "openai_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "input_per_million_usd": "2.0",
                                "output_per_million_usd": "8.0",
                                "capabilities": ["text"],
                            },
                            {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://cheap",
                                "quota_scope_id": "deepseek_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "input_per_million_usd": "0.2",
                                "output_per_million_usd": "0.8",
                                "capabilities": ["text"],
                            },
                        ]
                    }
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
                request="summarize this cheaply",
                routing_decision={
                    "harness_policy": {"model_selection": "low_cost"},
                },
            )
        )
    ]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    deployment, _request, _api_key = transport.calls[0]
    assert deployment.provider_model == "deepseek/deepseek-chat"
    assert deployment.input_per_million_usd == Decimal("0.2")


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
                    timeout_seconds=0.2,
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

    assert capacities[0].events
    assert all(event == ("deepseek/deepseek-chat",) for event in capacities[0].events)
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
                    timeout_seconds=0.2,
                    routing_decision={
                        "harness_policy": {
                            "fallback_policy": "disabled",
                        }
                    },
                )
            )
        ]

    assert capacities[0].events
    assert all(event == ("deepseek/deepseek-chat",) for event in capacities[0].events)
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
            estimated_tokens: int | Mapping[str, int],
        ) -> CapacityLease:
            self.wait_timeouts.append(wait_timeout)
            self.events.append(tuple(deployment.provider_model for deployment in candidates))
            if isinstance(estimated_tokens, Mapping):
                assert all(value > 0 for value in estimated_tokens.values())
            else:
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


def test_model_execution_plan_reports_safe_blocked_contract_self_repair_recovery() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="重试被阻塞的角色交接链。",
        routing_decision={
            "source": "self_repair",
            "self_repair_context": {
                "source": "self_repair",
                "failure_kind": "step_failure",
                "repair_action": "draft_repair_proposal",
                "attempt": 1,
                "max_attempts": 1,
                "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
                "orchestration_recovery_hint": "retry_blocked_contract_chain",
                "instruction": "重规划角色交接契约链。",
                "automatic_execution": False,
                "requires_approval": True,
            },
        },
    )

    plan = defaults_module._model_execution_plan_payload(
        context,
        main_agent_model="main",
        roles=(),
        steps=(),
    )

    assert plan["self_repair_recovery"] == {
        "schema_version": 1,
        "status": "active",
        "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
        "orchestration_recovery_hint": "retry_blocked_contract_chain",
        "replan_scope": "blocked_contract_chain",
        "reuse_completed_artifacts": True,
        "retry_blocked_contracts_only": True,
        "automatic_execution": False,
    }


def test_model_execution_plan_reports_plugin_runtime_self_repair_recovery() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="插件运行时不可用后重新加载能力目录再重试。",
        routing_decision={
            "source": "self_repair",
            "self_repair_context": {
                "source": "self_repair",
                "failure_kind": "plugin_runtime_unavailable",
                "repair_action": "draft_repair_proposal",
                "attempt": 1,
                "max_attempts": 1,
                "recovery_strategy": "repair_plugin_endpoint_or_adapter_and_retry",
                "instruction": "刷新插件运行时能力目录后重试失败工具。",
                "error_code": "plugin.backend_unavailable",
                "automatic_execution": True,
                "requires_approval": False,
            },
        },
    )

    plan = defaults_module._model_execution_plan_payload(
        context,
        main_agent_model="main",
        roles=(),
        steps=(),
    )

    assert plan["self_repair_recovery"] == {
        "schema_version": 1,
        "status": "active",
        "recovery_strategy": "repair_plugin_endpoint_or_adapter_and_retry",
        "replan_scope": "plugin_runtime",
        "reuse_completed_artifacts": True,
        "refresh_runtime_capabilities": True,
        "retry_failed_capability_only": True,
        "automatic_execution": True,
        "diagnostic_error_code": "plugin.backend_unavailable",
    }


def test_model_execution_plan_drops_unknown_self_repair_recovery_metadata() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="重试失败运行。",
        routing_decision={
            "source": "self_repair",
            "self_repair_context": {
                "source": "self_repair",
                "recovery_strategy": "secret://provider-token",
                "orchestration_recovery_hint": "dump_private_context",
                "automatic_execution": True,
            },
        },
    )

    plan = defaults_module._model_execution_plan_payload(
        context,
        main_agent_model="main",
        roles=(),
        steps=(),
    )

    assert "self_repair_recovery" not in plan
    assert "secret://provider-token" not in json.dumps(plan, ensure_ascii=False)
    assert "dump_private_context" not in json.dumps(plan, ensure_ascii=False)


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
        "total_count": 1,
        "returned_count": 1,
        "pagination": {
            "page": 1,
            "page_size": 12,
            "page_count": 1,
            "has_more": False,
            "next_page": None,
            "absolute_limit": 4096,
            "absolute_fuse_reached": False,
        },
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
        "total_count": 1,
        "returned_count": 1,
        "pagination": {
            "page": 1,
            "page_size": 12,
            "page_count": 1,
            "has_more": False,
            "next_page": None,
            "absolute_limit": 4096,
            "absolute_fuse_reached": False,
        },
        "truncated": False,
    }
    assert plan["orchestration_protocol"] == {
        "schema_version": 1,
        "protocol": "role_handoff_contract_v1",
        "mode": "dispatch",
        "role_count": 2,
        "handoff_count": 1,
        "contract_count": 1,
        "returned_handoff_count": 1,
        "pagination": {
            "page": 1,
            "page_size": 12,
            "page_count": 1,
            "has_more": False,
            "next_page": None,
            "absolute_limit": 4096,
            "absolute_fuse_reached": False,
        },
        "page_reader": {
            "operation": "runtime.read_orchestration_handoff_page",
            "page_size": 12,
            "next_cursor": None,
        },
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


def test_model_capability_negotiation_prefers_config_deployment_capabilities() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "coder": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://coder",
                            "quota_scope_id": "coder",
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
    )
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
                        "traits": ("text", "structured_output", "tool_calling"),
                        "reasons": ("capability:tool_role_supported",),
                    },
                ),
            },
        ),
        config=config,
    )

    negotiation = plan["model_capability_negotiation"]
    assert isinstance(negotiation, Mapping)
    assert negotiation["items"] == (
        {
            "role_id": "builder",
            "logical_model": "coder",
            "required_capabilities": ("text", "structured_output", "tool_calling"),
            "matched_capabilities": ("text",),
            "missing_capabilities": ("structured_output", "tool_calling"),
            "status": "missing_capability",
        },
    )
    assert negotiation["satisfied_count"] == 0
    assert negotiation["missing_count"] == 1


def test_model_capability_negotiation_does_not_union_split_deployment_capabilities() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "split": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://split-text",
                            "quota_scope_id": "split-text",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text"],
                        },
                        {
                            "provider": "qwen",
                            "model": "qwen3-max",
                            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            "credential_ref": "secret://split-structured",
                            "quota_scope_id": "split-structured",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["structured_output"],
                        },
                    ]
                }
            },
            "agents": [],
        }
    )

    plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Produce a structured dispatch result.",
        ),
        main_agent_model="main",
        roles=(
            {
                "id": "planner",
                "role": "Planner",
                "purpose": "execute",
                "logical_model": "split",
                "has_output_schema": True,
                "tools": (),
            },
        ),
        model_routing_matrix=(),
        config=config,
    )

    negotiation = plan["model_capability_negotiation"]
    assert isinstance(negotiation, Mapping)
    assert negotiation["items"] == (
        {
            "role_id": "planner",
            "logical_model": "split",
            "required_capabilities": ("text", "structured_output"),
            "matched_capabilities": ("text",),
            "missing_capabilities": ("structured_output",),
            "status": "missing_capability",
        },
    )
    assert negotiation["satisfied_count"] == 0
    assert negotiation["missing_count"] == 1


def test_model_capability_negotiation_treats_messages_endpoint_tool_calling_as_missing() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "messages": {
                    "deployments": [
                        {
                            "provider": "anthropic",
                            "model": "claude-3-5-sonnet",
                            "api_base": "https://api.anthropic.com/v1/messages",
                            "credential_ref": "secret://messages",
                            "quota_scope_id": "messages",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "tool_calling"],
                        }
                    ]
                }
            },
            "agents": [],
        }
    )

    plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Read context using a tool.",
        ),
        main_agent_model="main",
        roles=(
            {
                "id": "reader",
                "role": "Reader",
                "purpose": "synthesize",
                "logical_model": "messages",
                "has_output_schema": False,
                "tools": ("read_context",),
            },
        ),
        model_routing_matrix=(),
        config=config,
    )

    negotiation = plan["model_capability_negotiation"]
    assert isinstance(negotiation, Mapping)
    assert negotiation["items"] == (
        {
            "role_id": "reader",
            "logical_model": "messages",
            "required_capabilities": ("text", "tool_calling"),
            "matched_capabilities": ("text",),
            "missing_capabilities": ("tool_calling",),
            "status": "missing_capability",
        },
    )
    assert negotiation["satisfied_count"] == 0
    assert negotiation["missing_count"] == 1


def test_model_capability_negotiation_respects_harness_deployment_constraint() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "main": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://deepseek",
                            "quota_scope_id": "deepseek",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text"],
                        },
                        {
                            "provider": "qwen",
                            "model": "qwen3-max",
                            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            "credential_ref": "secret://qwen",
                            "quota_scope_id": "qwen",
                            "max_concurrency": 2,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text", "structured_output"],
                        },
                    ]
                }
            },
            "agents": [],
        }
    )

    plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Return structured output from the selected deployment.",
        ),
        main_agent_model="main",
        roles=(
            {
                "id": "planner",
                "role": "Planner",
                "purpose": "execute",
                "logical_model": "main",
                "has_output_schema": True,
                "tools": (),
            },
        ),
        model_routing_matrix=(),
        deployment_constraint=DeploymentRoutingConstraint(
            logical_model="main",
            provider="deepseek",
            model="deepseek-chat",
        ),
        config=config,
    )

    negotiation = plan["model_capability_negotiation"]
    assert isinstance(negotiation, Mapping)
    assert negotiation["items"] == (
        {
            "role_id": "planner",
            "logical_model": "main",
            "required_capabilities": ("text", "structured_output"),
            "matched_capabilities": ("text",),
            "missing_capabilities": ("structured_output",),
            "status": "missing_capability",
        },
    )
    assert negotiation["satisfied_count"] == 0
    assert negotiation["missing_count"] == 1


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
    overflow_second_page = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Draft a launch campaign.",
            routing_decision={
                "orchestration_handoff_page": 2,
                "orchestration_handoff_page_size": 12,
            },
        ),
        main_agent_model="main",
        roles=(*source_roles, target_role),
        steps=(*source_steps, overflow_target_step),
    )
    legacy_second_page = defaults_module.read_orchestration_handoff_page(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Draft a launch campaign.",
        ),
        cursor="handoff-page-v1:2:12",
        roles=(*source_roles, target_role),
        steps=(*source_steps, overflow_target_step),
    )

    exact_handoffs = exact_plan["orchestration_handoffs"]
    assert isinstance(exact_handoffs, Mapping)
    exact_items = exact_handoffs["items"]
    assert isinstance(exact_items, tuple)
    assert len(exact_items) == 12
    assert exact_handoffs["truncated"] is False
    assert exact_handoffs["total_count"] == 12
    assert exact_handoffs["returned_count"] == 12
    assert exact_handoffs["pagination"] == {
        "page": 1,
        "page_size": 12,
        "page_count": 1,
        "has_more": False,
        "next_page": None,
        "absolute_limit": 4096,
        "absolute_fuse_reached": False,
    }
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
    assert overflow_handoffs["total_count"] == 13
    assert overflow_handoffs["returned_count"] == 12
    assert overflow_handoffs["pagination"] == {
        "page": 1,
        "page_size": 12,
        "page_count": 2,
        "has_more": True,
        "next_page": 2,
        "absolute_limit": 4096,
        "absolute_fuse_reached": False,
    }
    overflow_contracts = overflow_plan["orchestration_contracts"]
    assert isinstance(overflow_contracts, Mapping)
    overflow_contract_items = overflow_contracts["items"]
    assert isinstance(overflow_contract_items, tuple)
    assert len(overflow_contract_items) == 12
    assert overflow_contracts["truncated"] is True
    overflow_protocol = overflow_plan["orchestration_protocol"]
    assert isinstance(overflow_protocol, Mapping)
    assert overflow_protocol["handoff_count"] == 13
    assert overflow_protocol["contract_count"] == 13
    assert overflow_protocol["returned_handoff_count"] == 12
    assert overflow_protocol["truncated"] is True
    second_handoffs = overflow_second_page["orchestration_handoffs"]
    assert isinstance(second_handoffs, Mapping)
    second_items = second_handoffs["items"]
    assert isinstance(second_items, tuple)
    assert len(second_items) == 1
    assert second_handoffs["total_count"] == 13
    assert second_handoffs["returned_count"] == 1
    second_pagination = second_handoffs["pagination"]
    assert isinstance(second_pagination, Mapping)
    assert second_pagination["page"] == 2
    assert second_pagination["has_more"] is False
    legacy_handoffs = legacy_second_page["orchestration_handoffs"]
    assert isinstance(legacy_handoffs, Mapping)
    assert legacy_handoffs["items"] == second_items


def test_model_execution_plan_handoff_page_size_grows_with_scale_and_token_budget() -> None:
    source_roles: tuple[Mapping[str, JsonValue], ...] = tuple(
        {
            "id": f"worker_{index}",
            "role": "Worker",
            "purpose": "execute",
            "logical_model": "creative",
            "tools": (),
        }
        for index in range(40)
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
        for index in range(40)
    )
    target_step: Mapping[str, JsonValue] = {
        "id": "final_response_step",
        "agent": "final_synthesizer",
        "depends_on": tuple(f"worker_{index}_step" for index in range(40)),
        "final_synthesizer": True,
        "tools": (),
    }

    plan = defaults_module._model_execution_plan_payload(
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Build a real large project.",
            token_budget=262_144,
            routing_decision={"project_scale": "large"},
        ),
        main_agent_model="main",
        roles=(*source_roles, target_role),
        steps=(*source_steps, target_step),
    )

    handoffs = plan["orchestration_handoffs"]
    assert isinstance(handoffs, Mapping)
    items = handoffs["items"]
    assert isinstance(items, tuple)
    assert len(items) == 40
    assert handoffs["total_count"] == 40
    pagination = handoffs["pagination"]
    assert isinstance(pagination, Mapping)
    assert pagination["page_size"] == 64
    assert pagination["page_count"] == 1


def test_model_execution_plan_handoff_pages_are_consumable_through_runtime_reader() -> None:
    source_roles: tuple[Mapping[str, JsonValue], ...] = tuple(
        {
            "id": f"worker_{index}",
            "role": "Worker",
            "purpose": "execute",
            "logical_model": "creative",
            "tools": (),
        }
        for index in range(30)
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
        for index in range(30)
    )
    target_step: Mapping[str, JsonValue] = {
        "id": "final_response_step",
        "agent": "final_synthesizer",
        "depends_on": tuple(f"worker_{index}_step" for index in range(30)),
        "final_synthesizer": True,
        "tools": (),
    }
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="Draft a launch campaign.",
    )
    roles = (*source_roles, target_role)
    steps = (*source_steps, target_step)

    plan = defaults_module._model_execution_plan_payload(
        context,
        main_agent_model="main",
        roles=roles,
        steps=steps,
    )

    protocol = plan["orchestration_protocol"]
    assert isinstance(protocol, Mapping)
    reader = protocol["page_reader"]
    assert isinstance(reader, Mapping)
    assert reader["operation"] == "runtime.read_orchestration_handoff_page"
    assert reader["page_size"] == 12
    cursor = reader["next_cursor"]
    assert isinstance(cursor, str)

    initial_handoffs = plan["orchestration_handoffs"]
    initial_contracts = plan["orchestration_contracts"]
    assert isinstance(initial_handoffs, Mapping)
    assert isinstance(initial_contracts, Mapping)
    initial_handoff_items = initial_handoffs["items"]
    initial_contract_items = initial_contracts["items"]
    assert isinstance(initial_handoff_items, tuple)
    assert isinstance(initial_contract_items, tuple)
    handoff_ids = {
        (str(item["source_step_id"]), str(item["target_step_id"]))
        for item in initial_handoff_items
        if isinstance(item, Mapping)
    }
    contract_ids = {
        str(item["contract_id"])
        for item in initial_contract_items
        if isinstance(item, Mapping)
    }
    while cursor is not None:
        page = defaults_module.read_orchestration_handoff_page(
            context,
            cursor=cursor,
            page_size=7,
        )
        page_handoffs = page["orchestration_handoffs"]
        page_contracts = page["orchestration_contracts"]
        page_protocol = page["orchestration_protocol"]
        assert isinstance(page_handoffs, Mapping)
        assert isinstance(page_contracts, Mapping)
        assert isinstance(page_protocol, Mapping)
        assert len(cast(tuple[object, ...], page_handoffs["items"])) <= 7
        handoff_ids.update(
            (str(item["source_step_id"]), str(item["target_step_id"]))
            for item in cast(tuple[Mapping[str, JsonValue], ...], page_handoffs["items"])
        )
        contract_ids.update(
            str(item["contract_id"])
            for item in cast(tuple[Mapping[str, JsonValue], ...], page_contracts["items"])
        )
        page_reader = page_protocol["page_reader"]
        assert isinstance(page_reader, Mapping)
        next_cursor = page_reader["next_cursor"]
        assert next_cursor is None or isinstance(next_cursor, str)
        cursor = next_cursor

    assert handoff_ids == {
        (f"worker_{index}_step", "final_response_step") for index in range(30)
    }
    assert contract_ids == {
        f"worker_{index}_step-to-final_response_step" for index in range(30)
    }

    with pytest.raises(ValueError, match="does not belong to this run"):
        defaults_module.read_orchestration_handoff_page(
            context.model_copy(update={"run_id": uuid4()}),
            cursor=cast(str, reader["next_cursor"]),
            page_size=7,
        )


def test_orchestration_handoff_page_reader_preserves_absolute_fuse() -> None:
    source_roles: tuple[Mapping[str, JsonValue], ...] = tuple(
        {
            "id": f"worker_{index}",
            "role": "Worker",
            "purpose": "execute",
            "logical_model": "creative",
            "tools": (),
        }
        for index in range(4097)
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
        for index in range(4097)
    )
    target_step: Mapping[str, JsonValue] = {
        "id": "final_response_step",
        "agent": "final_synthesizer",
        "depends_on": tuple(f"worker_{index}_step" for index in range(4097)),
        "final_synthesizer": True,
        "tools": (),
    }
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="Build an ultra project.",
        token_budget=1_048_576,
        routing_decision={"project_scale": "ultra"},
    )
    plan = defaults_module._model_execution_plan_payload(
        context,
        main_agent_model="main",
        roles=(*source_roles, target_role),
        steps=(*source_steps, target_step),
    )

    handoffs = plan["orchestration_handoffs"]
    protocol = plan["orchestration_protocol"]
    assert isinstance(handoffs, Mapping)
    assert isinstance(protocol, Mapping)
    assert handoffs["total_count"] == 4097
    page_reader = protocol["page_reader"]
    assert isinstance(page_reader, Mapping)
    cursor = page_reader["next_cursor"]
    assert isinstance(cursor, str)
    returned_count = int(cast(int, handoffs["returned_count"]))
    last_handoffs = handoffs
    while cursor is not None:
        page = defaults_module.read_orchestration_handoff_page(
            context,
            cursor=cursor,
            page_size=256,
        )
        page_handoffs = page["orchestration_handoffs"]
        page_protocol = page["orchestration_protocol"]
        assert isinstance(page_handoffs, Mapping)
        assert isinstance(page_protocol, Mapping)
        last_handoffs = page_handoffs
        returned_count += int(cast(int, last_handoffs["returned_count"]))
        next_reader = page_protocol["page_reader"]
        assert isinstance(next_reader, Mapping)
        next_cursor = next_reader["next_cursor"]
        assert next_cursor is None or isinstance(next_cursor, str)
        cursor = next_cursor

    assert returned_count == 4096
    pagination = last_handoffs["pagination"]
    assert isinstance(pagination, Mapping)
    assert pagination["absolute_limit"] == 4096
    assert pagination["absolute_fuse_reached"] is True


@pytest.mark.parametrize(
    ("project_scale", "expected_max_steps"),
    (("small", 64), ("medium", 96), ("large", 160), ("ultra", 256)),
)
def test_default_dispatch_plan_max_steps_grows_with_project_scale(
    project_scale: str,
    expected_max_steps: int,
) -> None:
    roles = (
        RoleAssignment(
            id="planner",
            role="Planner",
            purpose=RolePurpose.PLAN,
            mission="Plan the requested project.",
            must_answer=("What should be done?",),
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
            request="Plan the project.",
            routing_decision={"project_scale": project_scale},
        ),
    )

    assert plan.max_steps == expected_max_steps


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
                                "capabilities": ["text", "structured_output"],
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
                                "capabilities": ["text", "structured_output"],
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

    main_plan = _main_plan_event(events)
    roles, steps = _role_step_plan_from_events(events)
    assert main_plan.kind is EventKind.STEP_STARTED
    assert main_plan.actor == "main_agent"
    assert main_plan.payload["mode"] == "dispatch"
    assert main_plan.payload["main_agent_model"] == "main"
    assert roles == (
        {
            "id": "copywriter",
            "role": "Copywriter",
            "purpose": "execute",
            "logical_model": "creative",
            "tools": (),
            "has_output_schema": True,
        },
        {
            "id": "final_synthesizer",
            "role": "Final Synthesizer",
            "purpose": "synthesize",
            "logical_model": "main",
            "tools": (),
            "has_output_schema": False,
        },
    )
    assert steps == (
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
    assert _discussion_trace_payload_passes(
        _discussion_trace_from_events(events)
    )
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].sequence == len(events)


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_injects_artifact_repository_into_each_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDispatchRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "CrewDispatchRuntime", ProbeDispatchRuntime)
    artifact_repository = InMemoryArtifactRepository()
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
                                "capabilities": ["text", "structured_output"],
                            }
                        ]
                    }
                },
                "agents": [
                    {
                        "id": "writer",
                        "role": "Writer",
                        "prompt": "Draft concise text.",
                        "model": "main",
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
        artifact_repository=artifact_repository,
    )

    for _ in range(2):
        events = [
            event
            async for event in runtime.run(
                TaskContext(
                    run_id=uuid4(),
                    tenant_id=TENANT_ID,
                    mode=TaskMode.DISPATCH,
                    request="Draft a short note.",
                    routing_decision={
                        "selected_agent_ids": ("writer",),
                        "main_agent_model": "main",
                    },
                )
            )
        ]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED

    assert len(ProbeDispatchRuntime.instances) == 2
    assert all(
        child.artifact_repository is artifact_repository
        for child in ProbeDispatchRuntime.instances
    )


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

    assert _main_plan_event(events).kind is EventKind.STEP_STARTED
    role_plan, _ = _role_step_plan_from_events(events)
    assert role_plan[0]["id"] == "scheduler"
    assert role_plan[0]["logical_model"] == "qwen_tools"
    assert role_plan[0]["tools"] == ("calendar.create_event",)


@pytest.mark.asyncio
async def test_model_capability_self_repair_retry_reassigns_tool_role_to_capable_model(
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
                        "prompt": "Schedule events through a tool-capable role.",
                        "model": "main",
                        "skills": [],
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
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Retry the scheduler role with a tool-capable model.",
                routing_decision={
                    "source": "self_repair",
                    "selected_agent_ids": ("scheduler",),
                    "main_agent_model": "main",
                    "self_repair_context": {
                        "source": "self_repair",
                        "failure_kind": "model_capability_routing_unavailable",
                        "repair_action": "draft_repair_proposal",
                        "recovery_strategy": "reassign_tool_role_to_capable_model_and_retry",
                        "role_capability_requirements": (
                            {
                                "role_id": "scheduler",
                                "required_capabilities": (
                                    "text",
                                    "structured_output",
                                    "tool_calling",
                                ),
                            },
                        ),
                    },
                },
            )
        )
    ]

    assert _main_plan_event(events).kind is EventKind.STEP_STARTED
    role_plan, _ = _role_step_plan_from_events(events)
    assert role_plan[0]["id"] == "scheduler"
    assert role_plan[0]["logical_model"] == "qwen_tools"
    model_execution_plan = _model_execution_plan_from_events(events)
    assignments = cast(
        tuple[Mapping[str, JsonValue], ...],
        model_execution_plan["role_model_assignments"],
    )
    assert {
        assignment["role_id"]: assignment["logical_model"]
        for assignment in assignments
    }["scheduler"] == "qwen_tools"
    negotiation = cast(
        Mapping[str, JsonValue],
        model_execution_plan["model_capability_negotiation"],
    )
    assert negotiation["missing_count"] == 0
    items = cast(tuple[Mapping[str, JsonValue], ...], negotiation["items"])
    scheduler_item = next(item for item in items if item["role_id"] == "scheduler")
    assert scheduler_item == {
        "role_id": "scheduler",
        "logical_model": "qwen_tools",
        "required_capabilities": (
            "text",
            "structured_output",
            "tool_calling",
        ),
        "matched_capabilities": (
            "text",
            "structured_output",
            "tool_calling",
        ),
        "missing_capabilities": (),
        "status": "satisfied",
    }


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
                                "capabilities": ["text", "structured_output"],
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
                                "capabilities": ["text", "structured_output"],
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
                                "capabilities": ["text", "structured_output"],
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

    main_plan = _main_plan_event(events)
    roles, _ = _role_step_plan_from_events(events)
    assert main_plan.payload["main_agent_model"] == "main"
    assert roles == (
        {
            "id": "copywriter",
            "role": "Copywriter",
            "purpose": "execute",
            "logical_model": "creative",
            "tools": (),
            "has_output_schema": True,
        },
        {
            "id": "final_synthesizer",
            "role": "Final Synthesizer",
            "purpose": "synthesize",
            "logical_model": "main",
            "tools": (),
            "has_output_schema": False,
        },
    )
    assert {deployment.provider_model for deployment in capacities[0].deployments} == {
        "deepseek/deepseek-chat",
        "kimi/kimi-k2-latest",
    }
    model_execution_plan_raw = _model_execution_plan_from_events(events)
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
        "total_count": 1,
        "returned_count": 1,
        "pagination": {
            "page": 1,
            "page_size": 12,
            "page_count": 1,
            "has_more": False,
            "next_page": None,
            "absolute_limit": 4096,
            "absolute_fuse_reached": False,
        },
        "truncated": False,
    }
    assert model_execution_plan["model_capability_negotiation"] == {
        "schema_version": 1,
        "items": (
            {
                "role_id": "copywriter",
                "logical_model": "creative",
                "required_capabilities": ("text", "structured_output"),
                "matched_capabilities": ("text", "structured_output"),
                "missing_capabilities": (),
                "status": "satisfied",
            },
            {
                "role_id": "final_synthesizer",
                "logical_model": "main",
                "required_capabilities": ("text",),
                "matched_capabilities": ("text",),
                "missing_capabilities": (),
                "status": "satisfied",
            },
        ),
        "role_count": 2,
        "satisfied_count": 2,
        "missing_count": 0,
        "unknown_count": 0,
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


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_routes_roles_away_from_constrained_deployment_without_required_capabilities(
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
                            },
                            {
                                "provider": "qwen",
                                "model": "qwen3-max",
                                "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                                "credential_ref": "secret://qwen",
                                "quota_scope_id": "qwen_account",
                                "max_concurrency": 2,
                                "target_utilization": 0.8,
                                "reserved_slots": 0,
                                "capabilities": ["text", "structured_output"],
                            },
                        ]
                    },
                    "structured": {
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
                                "capabilities": ["text", "structured_output"],
                            }
                        ]
                    },
                },
                "agents": [
                    {
                        "id": "planner",
                        "role": "Planner",
                        "prompt": "Return structured dispatch output.",
                        "model": "main",
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
                request="Return structured dispatch output.",
                routing_decision={
                    "selected_agent_ids": ("planner",),
                    "harness_decision": {
                        "selected_provider": "DeepSeek",
                        "selected_model": "deepseek-chat",
                        "selected_logical_model": "main",
                    },
                },
            )
        )
    ]

    assert _main_plan_event(events).kind is EventKind.STEP_STARTED
    role_plan, _ = _role_step_plan_from_events(events)
    assert role_plan[0]["id"] == "planner"
    assert role_plan[0]["logical_model"] == "structured"


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_caps_parallelism_to_constrained_deployment_capacity(
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
                                "provider": "openai",
                                "model": "gpt-5.6-sol",
                                "api_base": "https://api.openai.com/v1",
                                "credential_ref": "secret://openai",
                                "quota_scope_id": "openai_account",
                                "max_concurrency": 32,
                                "target_utilization": 0.9,
                                "reserved_slots": 0,
                                "capabilities": ["text", "structured_output"],
                            },
                            {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                                "api_base": "https://api.deepseek.com/v1",
                                "credential_ref": "secret://deepseek",
                                "quota_scope_id": "deepseek_account",
                                "max_concurrency": 4,
                                "target_utilization": 0.5,
                                "reserved_slots": 0,
                                "capabilities": ["text", "structured_output"],
                            },
                        ]
                    }
                },
                "agents": [
                    {
                        "id": "planner",
                        "role": "Planner",
                        "prompt": "Plan the work.",
                        "model": "main",
                        "skills": [],
                    },
                    {
                        "id": "reviewer",
                        "role": "Reviewer",
                        "prompt": "Review the plan.",
                        "model": "main",
                        "skills": [],
                    },
                    {
                        "id": "writer",
                        "role": "Writer",
                        "prompt": "Write the answer.",
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
                mode=TaskMode.DISPATCH,
                request="Plan, review, and write.",
                routing_decision={
                    "selected_agent_ids": ("planner", "reviewer", "writer"),
                    "harness_decision": {
                        "selected_provider": "DeepSeek",
                        "selected_model": "deepseek-chat",
                        "selected_logical_model": "main",
                    },
                },
            )
        )
    ]

    assert _main_plan_event(events).kind is EventKind.STEP_STARTED
    plan = cast(DispatchPlan, ProbeDispatchRuntime.instances[-1].plan)
    assert plan.max_parallelism == 2


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

    assert _capability_execution_plan_from_events(events) == {
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

    capability_plan = _capability_execution_plan_from_events(events)
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

    capability_plan = _capability_execution_plan_from_events(events)
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
                "failure_codes": (
                    "plugin.timeout",
                    "plugin.schema_validation_failed",
                ),
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
                "failure_codes": ("mcp.server_failed",),
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
                                "capabilities": ["text", "structured_output"],
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

    capability_plan = _capability_execution_plan_from_events(events)
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
                                "capabilities": ["text", "structured_output"],
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

    capability_plan = _capability_execution_plan_from_events(events)
    inventory = cast(Mapping[str, JsonValue], capability_plan["capability_inventory"])
    items = cast(tuple[Mapping[str, JsonValue], ...], inventory["items"])
    assert inventory["truncated"] is True
    assert len(items) == 96
    assert items[0]["id"] == "plugin.tool_0"
    assert items[0]["kind"] == "unknown"
    assert items[0]["availability_reason"] is None
    assert items[0]["aliases"] == tuple(f"alias_{index}" for index in range(16))
    assert "bad tool" not in {item["id"] for item in items}


def test_capability_inventory_payload_bounds_failure_codes() -> None:
    inventory = _capability_inventory_payload(
        TENANT_ID,
        capability_gateway=BadManifestCapabilityGateway(
            {
                "schema_version": 1,
                "capabilities": (
                    {
                        "id": "plugin.safe_tool",
                        "kind": "plugin",
                        "adapter": "plugin_registry",
                        "permission_class": "plugin.use",
                        "sandbox_profile": "remote_connector",
                        "available": True,
                        "availability_reason": None,
                        "failure_codes": (
                            "plugin.timeout",
                            "bad code",
                            "secret.token",
                            *(f"plugin.failure_{index}" for index in range(40)),
                        ),
                        "replay_safe": False,
                        "aliases": (),
                    },
                ),
            }
        ),
    )

    assert inventory is not None
    items = cast(tuple[Mapping[str, JsonValue], ...], inventory["items"])
    failure_codes = tuple(cast(tuple[str, ...], items[0]["failure_codes"]))
    assert len(failure_codes) == 32
    assert failure_codes[0] == "plugin.timeout"
    assert failure_codes[-1] == "plugin.failure_30"
    assert "bad code" not in failure_codes
    assert "secret.token" not in failure_codes


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
                                "capabilities": ["text", "structured_output"],
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

    capability_plan = _capability_execution_plan_from_events(events)
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
                        "failure_codes": (
                            "plugin.timeout",
                            "bad code",
                            "plugin.secret_token",
                        ),
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
            "failure_codes": ("plugin.timeout",),
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
async def test_config_backed_hybrid_runtime_shares_artifact_repository_across_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDispatchRuntime.instances.clear()
    ProbeDiscussionRuntime.instances.clear()
    ProbeHybridRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "CrewDispatchRuntime", ProbeDispatchRuntime)
    monkeypatch.setattr(defaults_module, "AutoGenDiscussionRuntime", ProbeDiscussionRuntime)
    monkeypatch.setattr(defaults_module, "HybridRuntime", ProbeHybridRuntime)
    artifact_repository = InMemoryArtifactRepository()
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
                                "capabilities": [
                                    "text",
                                    "tool_calling",
                                    "structured_output",
                                ],
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
        artifact_repository=artifact_repository,
    )

    _ = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.HYBRID,
                request="Resume a hybrid run with durable artifacts.",
            )
        )
    ]

    assert ProbeDispatchRuntime.instances[-1].artifact_repository is artifact_repository
    assert ProbeDiscussionRuntime.instances[-1].artifact_repository is artifact_repository
    assert ProbeHybridRuntime.instances[-1].artifact_repository is artifact_repository


@pytest.mark.asyncio
async def test_config_backed_hybrid_runtime_keeps_plan_digest_stable_across_budget_growth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDispatchRuntime.instances.clear()
    ProbeDiscussionRuntime.instances.clear()
    ProbeHybridRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "CrewDispatchRuntime", ProbeDispatchRuntime)
    monkeypatch.setattr(defaults_module, "AutoGenDiscussionRuntime", ProbeDiscussionRuntime)
    monkeypatch.setattr(defaults_module, "HybridRuntime", ProbeHybridRuntime)
    config = FakeConfigService(
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
                            "capabilities": [
                                "text",
                                "tool_calling",
                                "structured_output",
                            ],
                        }
                    ]
                }
            },
            "agents": [],
        }
    )
    routing_decision: dict[str, JsonValue] = {
        "project_scale": "large",
        "runtime_plan_token_budget": 2_500_000,
        "runtime_plan_timeout_seconds": 1_800.0,
    }
    run_id = uuid4()

    for token_budget, timeout_seconds in ((2_500_000, 1_800.0), (2_750_000, 2_100.0)):
        runtime = ConfigBackedHybridRuntime(
            config_service=config,  # type: ignore[arg-type]
            secret_service=FakeSecretService(),  # type: ignore[arg-type]
            capacity_factory=lambda tenant_id, deployments: _immediate_capacity(
                tenant_id, deployments
            ),
            transport=FakeTransport(),
        )
        _ = [
            event
            async for event in runtime.run(
                TaskContext(
                    run_id=run_id,
                    tenant_id=TENANT_ID,
                    mode=TaskMode.HYBRID,
                    request="Build a real large business project.",
                    routing_decision=routing_decision,
                    token_budget=token_budget,
                    timeout_seconds=timeout_seconds,
                )
            )
        ]

    assert len(ProbeDispatchRuntime.instances) == 2
    initial_plan = cast(DispatchPlan, ProbeDispatchRuntime.instances[0].plan)
    resumed_plan = cast(DispatchPlan, ProbeDispatchRuntime.instances[1].plan)
    assert initial_plan.digest == resumed_plan.digest


@pytest.mark.asyncio
async def test_project_preflight_agent_uses_model_with_effective_tool_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDispatchRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "CrewDispatchRuntime", ProbeDispatchRuntime)
    runtime = ConfigBackedDispatchRuntime(
        config_service=FakeConfigService(
            {
                "models": {
                    "sonnet5": {
                        "deployments": [
                            {
                                "provider": "claude-code-relay",
                                "model": "claude-sonnet-5",
                                "api_base": "https://relay.example/v1/messages",
                                "credential_ref": "secret://sonnet",
                                "quota_scope_id": "sonnet_account",
                                "max_concurrency": 8,
                                "capabilities": [
                                    "text",
                                    "tool_calling",
                                    "structured_output",
                                ],
                            }
                        ]
                    },
                    "qwen": {
                        "deployments": [
                            {
                                "provider": "qwen",
                                "model": "qwen3-max",
                                "api_base": "https://dashscope.example/v1",
                                "credential_ref": "secret://qwen",
                                "quota_scope_id": "qwen_account",
                                "max_concurrency": 8,
                                "capabilities": [
                                    "text",
                                    "tool_calling",
                                    "structured_output",
                                ],
                            }
                        ]
                    },
                },
                "agents": [
                    {
                        "id": "builder",
                        "role": "Builder",
                        "prompt": "Execute the approved plan.",
                        "model": "sonnet5",
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
        capability_gateway=ProjectPreflightCapabilityGateway(),
    )

    _ = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="Execute the approved plan.",
                routing_decision={
                    "selected_agent_ids": ("builder",),
                    "project_preflight_approved": True,
                    "project_preflight_proposal": {
                        "kind": "project_architecture_preflight",
                        "capability": "project.preflight_architecture",
                        "plan_path": "PROJECT_ARCHITECTURE_PLAN.md",
                        "graph_path": "architecture-map.html",
                        "requires_constraints_and_skills_reading": True,
                    },
                },
            )
        )
    ]

    plan = cast(DispatchPlan, ProbeDispatchRuntime.instances[-1].plan)
    agents = {agent.id: agent for agent in plan.agents}
    assert agents["builder"].logical_model == "sonnet5"
    assert agents["project_preflight_architect"].logical_model == "qwen"


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
                                "capabilities": ["text", "structured_output"],
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
                                "capabilities": ["text", "structured_output"],
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

    main_plan = _main_plan_event(events)
    assert main_plan.kind is EventKind.STEP_STARTED
    assert main_plan.actor == "main_agent"
    assert main_plan.payload["mode"] == "hybrid"
    roles, _ = _role_step_plan_from_events(events)
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
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].sequence == len(events)


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
                                "capabilities": ["text", "structured_output"],
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
                                "capabilities": ["text", "structured_output"],
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

    main_plan = _main_plan_event(events)
    roles, steps = _role_step_plan_from_events(events)
    assert main_plan.kind is EventKind.STEP_STARTED
    assert main_plan.actor == "main_agent"
    assert main_plan.payload["mode"] == "discuss"
    assert roles == (
        {
            "id": "strategist",
            "role": "Strategist",
            "purpose": "expertise",
            "logical_model": "main",
            "tools": (),
            "has_output_schema": False,
        },
        {
            "id": "reviewer",
            "role": "Reviewer",
            "purpose": "expertise",
            "logical_model": "review",
            "tools": (),
            "has_output_schema": False,
        },
    )
    assert steps == (
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
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].sequence == len(events)


@pytest.mark.asyncio
async def test_config_backed_discussion_runtime_does_not_require_structured_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDiscussionRuntime.instances.clear()
    monkeypatch.setattr(defaults_module, "AutoGenDiscussionRuntime", ProbeDiscussionRuntime)
    runtime = ConfigBackedDiscussionRuntime(
        config_service=FakeConfigService(
            {
                "models": {
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
                                "capabilities": ["text"],
                            }
                        ]
                    }
                },
                "agents": [
                    {
                        "id": "analyst",
                        "role": "Analyst",
                        "prompt": "Compare options.",
                        "model": "plain",
                        "skills": [],
                    },
                    {
                        "id": "critic",
                        "role": "Critic",
                        "prompt": "Review risks.",
                        "model": "plain",
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
                    "selected_agent_ids": ("analyst", "critic"),
                    "main_agent_model": "plain",
                },
            )
        )
    ]

    main_plan = _main_plan_event(events)
    assert main_plan.kind is EventKind.STEP_STARTED
    assert main_plan.actor == "main_agent"
    assert main_plan.payload["main_agent_model"] == "plain"
    model_execution_plan = _model_execution_plan_from_events(events)
    assert isinstance(model_execution_plan, Mapping)
    negotiation = model_execution_plan["model_capability_negotiation"]
    assert isinstance(negotiation, Mapping)
    assert negotiation["satisfied_count"] == 2
    assert negotiation["missing_count"] == 0
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


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
    roles, steps = _role_step_plan_from_events(events)
    assert tuple(role["id"] for role in roles) == ("scheduler", "reviewer")
    assert all(role["purpose"] == "expertise" for role in roles)
    scheduler = next(role for role in roles if role["id"] == "scheduler")
    assert scheduler["tools"] == ("read_context",)
    scheduler_step = next(step for step in steps if step["agent"] == "scheduler")
    assert scheduler_step["tools"] == ("read_context",)
    reviewer = next(role for role in roles if role["id"] == "reviewer")
    assert reviewer["tools"] == ("calendar.create_event",)
    reviewer_step = next(step for step in steps if step["agent"] == "reviewer")
    assert reviewer_step["tools"] == ("calendar.create_event",)


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


async def _assert_cancel_run_targets_only_matching_active_child(
    *,
    active: dict[tuple[UUID, UUID], ExecutionRuntime],
    cancel_run: Callable[[UUID], Awaitable[None]],
    cancel_owned: Callable[[UUID, UUID], Awaitable[None]],
    cancel_all: Callable[[], Awaitable[None]],
) -> None:
    selected_run_id, unrelated_run_id = uuid4(), uuid4()
    selected_token, replacement_token, unrelated_token = uuid4(), uuid4(), uuid4()
    selected = CancellableProbeRuntime()
    replacement = CancellableProbeRuntime()
    unrelated = CancellableProbeRuntime()
    active[(selected_run_id, selected_token)] = selected
    active[(selected_run_id, replacement_token)] = replacement
    active[(unrelated_run_id, unrelated_token)] = unrelated

    await cancel_owned(selected_run_id, selected_token)
    await cancel_owned(selected_run_id, uuid4())

    assert selected.cancel_count == 1
    assert replacement.cancel_count == 0
    assert unrelated.cancel_count == 0

    await cancel_run(selected_run_id)
    await cancel_run(uuid4())

    assert selected.cancel_count == 2
    assert replacement.cancel_count == 1
    assert unrelated.cancel_count == 0

    await cancel_all()

    assert selected.cancel_count == 3
    assert replacement.cancel_count == 2
    assert unrelated.cancel_count == 1


@pytest.mark.asyncio
async def test_config_backed_direct_runtime_cancel_run_cancels_only_matching_child() -> None:
    runtime = ConfigBackedDirectRuntime(
        config_service=FakeConfigService(None),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=_immediate_capacity,
        transport=FakeTransport(),
    )

    await _assert_cancel_run_targets_only_matching_active_child(
        active=runtime._active,
        cancel_run=runtime.cancel_run,
        cancel_owned=runtime.cancel_run_owned,
        cancel_all=runtime.cancel,
    )


@pytest.mark.asyncio
async def test_config_backed_dispatch_runtime_cancel_run_cancels_only_matching_child() -> None:
    runtime = ConfigBackedDispatchRuntime(
        config_service=FakeConfigService(None),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=_immediate_capacity,
        transport=FakeTransport(),
    )

    await _assert_cancel_run_targets_only_matching_active_child(
        active=runtime._active,
        cancel_run=runtime.cancel_run,
        cancel_owned=runtime.cancel_run_owned,
        cancel_all=runtime.cancel,
    )


@pytest.mark.asyncio
async def test_config_backed_discussion_runtime_cancel_run_cancels_only_matching_child() -> None:
    runtime = ConfigBackedDiscussionRuntime(
        config_service=FakeConfigService(None),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=_immediate_capacity,
        transport=FakeTransport(),
    )

    await _assert_cancel_run_targets_only_matching_active_child(
        active=runtime._active,
        cancel_run=runtime.cancel_run,
        cancel_owned=runtime.cancel_run_owned,
        cancel_all=runtime.cancel,
    )


@pytest.mark.asyncio
async def test_config_backed_hybrid_runtime_cancel_run_cancels_only_matching_child() -> None:
    runtime = ConfigBackedHybridRuntime(
        config_service=FakeConfigService(None),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=_immediate_capacity,
        transport=FakeTransport(),
    )

    await _assert_cancel_run_targets_only_matching_active_child(
        active=runtime._active,
        cancel_run=runtime.cancel_run,
        cancel_owned=runtime.cancel_run_owned,
        cancel_all=runtime.cancel,
    )


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


@pytest.mark.parametrize("multi_agent_chain", [False, True])
@pytest.mark.parametrize("fallback_policy", ["configured", "disabled"])
@pytest.mark.parametrize("pinned", [False, True])
def test_dispatch_final_synthesizer_has_eligible_recovery_models(
    multi_agent_chain: bool,
    fallback_policy: str,
    pinned: bool,
) -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                name: {
                    "deployments": [
                        {
                            "provider": "openai",
                            "model": name,
                            "api_base": "https://models.example/v1",
                            "credential_ref": f"secret://{name}",
                            "quota_scope_id": name,
                            "capabilities": capabilities,
                        }
                    ]
                }
                for name, capabilities in (
                    ("main", ["text"]),
                    ("backup", ["text"]),
                    ("image_only", ["vision"]),
                )
            },
            "agents": [],
        }
    )
    role = RoleAssignment(
        id="writer",
        role="Writer",
        purpose=RolePurpose.EXECUTE,
        mission="Write the requested answer.",
        must_answer=("What is the answer?",),
        allowed_tools=(),
        forbidden_actions=("Do not perform dangerous operations.",),
        skills=(),
        output_schema={"summary": "string"},
        model="backup",
    )
    roles = (
        tuple(replace(role, id=role_id) for role_id in ("architect", "implementer", "tester"))
        if multi_agent_chain
        else (role,)
    )
    plan = _dispatch_plan(
        roles,
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Summarize the verified answer.",
            routing_decision={
                "main_agent_model": "main",
                "flow": "multi_agent" if multi_agent_chain else "dispatch",
                "harness_policy": {"fallback_policy": fallback_policy},
            },
        ),
        config=config,
        deployment_constraint=(
            DeploymentRoutingConstraint(logical_model="main", provider="openai", model="main")
            if pinned
            else None
        ),
    )

    final = plan.agents[-1]
    assert final.logical_model == "main"
    assert final.allowed_tools == ()
    assert final.fallback_models == (
        ("backup",) if fallback_policy == "configured" and not pinned else ()
    )


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
    assert [agent.output_schema for agent in plan.agents] == [
        {"summary": "string"},
        {"summary": "string"},
        {},
    ]
    assert [step.agent for step in plan.steps] == [
        "director",
        "copywriter",
        "final_synthesizer",
    ]
    assert plan.max_parallelism == 1


def test_dispatch_role_payload_reports_schema_presence_without_field_names() -> None:
    plan = _dispatch_plan(
        (
            RoleAssignment(
                id="writer",
                role="Writer",
                purpose=RolePurpose.EXECUTE,
                mission="Write structured output.",
                must_answer=("What changed?",),
                allowed_tools=(),
                forbidden_actions=("Do not perform dangerous operations.",),
                skills=(),
                output_schema={"summary": "string", "secret_token": "string"},
                model="main",
            ),
        ),
        TaskContext(
            run_id=uuid4(),
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="Write a summary.",
        ),
    )

    payload = defaults_module._dispatch_role_payload(plan)

    writer = next(item for item in payload if item["id"] == "writer")
    final = next(item for item in payload if item["id"] == "final_synthesizer")
    assert writer["has_output_schema"] is True
    assert final["has_output_schema"] is False
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "summary" not in serialized
    assert "secret_token" not in serialized


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


def test_dispatch_plan_includes_approved_project_preflight_context_in_steps() -> None:
    role = RoleAssignment(
        id="builder",
        role="Builder",
        purpose=RolePurpose.EXECUTE,
        mission="Build the approved large project.",
        must_answer=("What was implemented?",),
        allowed_tools=(),
        forbidden_actions=("Do not perform dangerous operations.",),
        skills=(),
        output_schema={"summary": "string"},
        model="main",
    )
    routing_decision: dict[str, JsonValue] = {
        "project_preflight_approved": True,
        "project_preflight_proposal": {
            "kind": "project_architecture_preflight",
            "capability": "project.preflight_architecture",
            "plan_path": "PROJECT_ARCHITECTURE_PLAN.md",
            "graph_path": "architecture-map.html",
            "requires_constraints_and_skills_reading": True,
        },
    }
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="构建一个超大型项目。",
        artifacts=(),
        timeout_seconds=60,
        token_budget=10_000,
        routing_decision=routing_decision,
    )

    plan = _dispatch_plan((role,), context, max_parallelism=1)

    assert any("PROJECT_PREFLIGHT_CONTEXT" in step.task for step in plan.steps)
    assert any("project.preflight_architecture" in step.task for step in plan.steps)
    assert any("staged implementation" in step.task for step in plan.steps)
    assert "Use the project_preflight_step output as the implementation contract" in plan.final_step.task
    assert "stage-by-stage implementation status" in plan.final_step.task


def test_dispatch_plan_stages_approved_project_preflight_before_build_steps() -> None:
    roles = (
        RoleAssignment(
            id="builder",
            role="Builder",
            purpose=RolePurpose.EXECUTE,
            mission="Build the approved large project.",
            must_answer=("What was implemented?",),
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
            mission="Review the completed project.",
            must_answer=("Does the implementation pass review?",),
            allowed_tools=(),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
    )
    routing_decision: dict[str, JsonValue] = {
        "project_preflight_approved": True,
        "project_preflight_proposal": {
            "kind": "project_architecture_preflight",
            "capability": "project.preflight_architecture",
            "plan_path": "PROJECT_ARCHITECTURE_PLAN.md",
            "graph_path": "architecture-map.html",
            "requires_constraints_and_skills_reading": True,
        },
    }
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="构建一个超大型项目。",
        artifacts=(),
        timeout_seconds=60,
        token_budget=10_000,
        routing_decision=routing_decision,
    )

    plan = _dispatch_plan(roles, context, max_parallelism=2)

    agents = {agent.id: agent for agent in plan.agents}
    steps = {step.id: step for step in plan.steps}
    assert agents["builder"].output_schema["stage_status"] == "string[]"
    assert agents["builder"].output_schema["verification_evidence"] == "string[]"
    assert agents["builder"].output_schema["remaining_risks"] == "string[]"
    assert agents["builder"].output_schema["stage_repair_actions"] == "string[]"
    assert agents["quality_reviewer"].output_schema["stage_status"] == "string[]"
    assert agents["quality_reviewer"].output_schema["verification_evidence"] == "string[]"
    assert agents["quality_reviewer"].output_schema["remaining_risks"] == "string[]"
    assert agents["quality_reviewer"].output_schema["stage_repair_actions"] == "string[]"
    assert agents["quality_reviewer"].output_schema["acceptance_review"] == "string[]"
    assert steps["project_preflight_step"].agent == "project_preflight_architect"
    assert "PROJECT_ARCHITECTURE_PLAN.md" in steps["project_preflight_step"].task
    assert "architecture-map.html" in steps["project_preflight_step"].task
    assert steps["builder_step"].depends_on == ("project_preflight_step",)
    assert (
        "Use the project_preflight_step output as the implementation contract"
        in steps["builder_step"].task
    )
    assert "stage-by-stage implementation slices" in steps["builder_step"].task
    assert "verification evidence for each stage" in steps["builder_step"].task
    assert "diagnose failed stages before escalating" in steps["builder_step"].task
    assert "record stage_repair_actions" in steps["builder_step"].task
    assert steps["quality_reviewer_step"].depends_on == ("builder_step",)
    assert (
        "Use the project_preflight_step output as the implementation contract"
        in steps["quality_reviewer_step"].task
    )
    assert steps["final_response_step"].depends_on == (
        "project_preflight_step",
        "builder_step",
        "quality_reviewer_step",
    )
    assert "stage-by-stage implementation status" in steps["final_response_step"].task
    assert "self-repair actions" in steps["final_response_step"].task
    assert all(step.cost_budget_usd == Decimal(10) for step in steps.values())
    assert plan.total_cost_usd == Decimal(40)


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


def test_dispatch_plan_quiesces_planning_before_serial_execution_roles() -> None:
    roles = (
        RoleAssignment(
            id="architect",
            role="Architect",
            purpose=RolePurpose.PLAN,
            mission="Plan the implementation.",
            must_answer=("What should be built?",),
            allowed_tools=("read_context",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Write the project files.",
            must_answer=("What was implemented?",),
            allowed_tools=("workspace.write_text", "workspace.bundle"),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="installer",
            role="Installer",
            purpose=RolePurpose.EXECUTE,
            mission="Install the generated project.",
            must_answer=("What was installed?",),
            allowed_tools=("run_safe_command",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="tester",
            role="Tester",
            purpose=RolePurpose.VERIFY,
            mission="Verify the completed project.",
            must_answer=("What passed?",),
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
            request="Build and verify a project that requires approved workspace writes.",
        ),
        max_parallelism=4,
    )

    steps = {step.agent: step for step in plan.steps}
    assert steps["architect"].depends_on == ()
    assert steps["implementer"].depends_on == ("architect_step",)
    assert steps["installer"].depends_on == (
        "architect_step",
        "implementer_step",
    )
    assert steps["tester"].depends_on == (
        "architect_step",
        "implementer_step",
        "installer_step",
    )


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
    assert all(step.cost_budget_usd == Decimal(10) for step in plan.steps)
    assert plan.total_cost_usd == Decimal(70)


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


@pytest.mark.parametrize("suffix", [".", ".details", "_extended"])
def test_plugin_capability_token_respects_namespace_boundary(suffix: str) -> None:
    assert defaults_module._capability_token_mentioned(
        f"Use calendar.create_event{suffix}", "calendar.create_event",
    ) is (suffix == ".")


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


@pytest.mark.parametrize(
    "purpose",
    [RolePurpose.CRITIQUE, RolePurpose.RISK_REVIEW, RolePurpose.VERIFY, RolePurpose.RECORD_DECISION, RolePurpose.RELEASE],
)
@pytest.mark.parametrize("assignment", ["none", "tools", "mission"])
def test_review_plugin_tools_require_role_specific_assignment(
    purpose: RolePurpose, assignment: str,
) -> None:
    producer = RoleAssignment(
        id="producer", role="Producer", purpose=RolePurpose.EXECUTE,
        mission="Create the requested event.", must_answer=("What was created?",),
        allowed_tools=("read_context",), forbidden_actions=("Do not perform dangerous operations.",),
        skills=(), output_schema={"summary": "string"}, model="main",
    )
    reviewer = replace(
        producer, id="reviewer", role="Reviewer", purpose=purpose,
        mission=("Verify using calendar.create_event." if assignment == "mission" else "Review producer evidence."),
        allowed_tools=(("read_context", "calendar.create_event") if assignment == "tools" else ("read_context",)),
    )
    plan = _dispatch_plan(
        (producer, reviewer),
        TaskContext(
            run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISPATCH,
            request="Use calendar.create_event exactly once, then review its recorded result.",
        ),
        capability_gateway=AvailablePluginManifestCapabilityGateway(),
    )
    agents = {agent.id: agent for agent in plan.agents}
    steps = {step.agent: step for step in plan.steps}
    assert agents["producer"].allowed_tools == ("read_context", "calendar.create_event")
    expected = ("read_context",) if assignment == "none" else ("read_context", "calendar.create_event")
    assert agents["reviewer"].allowed_tools == expected
    assert steps["reviewer"].tools == expected
    assert steps["reviewer"].depends_on == ("producer_step",)


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
    capability_plan = _capability_execution_plan_from_events(events)
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


def test_project_scale_artifact_implementer_does_not_loop_on_empty_context() -> None:
    roles = (
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Build the requested project.",
            must_answer=("What code was produced?",),
            allowed_tools=("read_context", "project.generate_zip"),
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
            request=(
                "Project-scale acceptance fixture: build a small project for scale=small "
                "and flow=artifact_production."
            ),
        ),
        capability_gateway=FakeCapabilityAvailability({"read_context", "project.generate_zip"}),
    )

    implementer_step = next(step for step in plan.steps if step.agent == "implementer")
    assert implementer_step.tools == ("project.generate_zip",)
    assert "If read_context has no additional runtime context, continue with the requested files" in implementer_step.task


def test_project_scale_capability_implementer_prioritizes_project_zip_tool() -> None:
    roles = (
        RoleAssignment(
            id="architect",
            role="Architect",
            purpose=RolePurpose.PLAN,
            mission="Plan the requested project.",
            must_answer=("What should be built?",),
            allowed_tools=("read_context",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Build the requested project.",
            must_answer=("What code was produced?",),
            allowed_tools=("read_context", "project.generate_zip"),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
    )

    for request, expected_budget in (
        (
            (
                "Build a real small business project for flow=dispatch. "
                "Return strict JSON workspace_bundle.files (relative paths to full content)."
            ),
            3_000_000,
        ),
        (
            (
                "Repair this same business project; preserve every original requirement. "
                "Original request: Build a real small business project for flow=dispatch. "
                "Return strict JSON workspace_bundle.files (relative paths to full content)."
            ),
            3_500_000,
        ),
    ):
        plan = _dispatch_plan(
            roles,
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request=request,
            ),
            capability_gateway=FakeCapabilityAvailability({"read_context", "project.generate_zip"}),
        )

        architect_step = next(step for step in plan.steps if step.agent == "architect")
        implementer_step = next(step for step in plan.steps if step.agent == "implementer")
        implementer_agent = next(agent for agent in plan.agents if agent.id == "implementer")
        assert architect_step.tools == ()
        assert implementer_step.tools == ("project.generate_zip",)
        assert implementer_step.tool_argument_budget_bytes == {
            "project.generate_zip": expected_budget
        }
        assert implementer_agent.max_output_tokens == 24_576
        assert (
            "If read_context has no additional runtime context, continue with the requested files"
            in implementer_step.task
        )


@pytest.mark.parametrize("reader_available", (False, True))
def test_fresh_dispatch_plan_retains_only_available_workspace_reader(
    reader_available: bool,
) -> None:
    roles = RolePlanner().plan(
        RolePlanningRequest(
            task="Implement a TypeScript business project with tests and a workspace bundle.",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.SOFTWARE,
        )
    ).roles
    context = TaskContext(
        run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISPATCH,
        request="Build a real medium business project with sources and tests.",
        routing_decision={
            "project_scale": "medium", "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
        },
    )
    available = {"workspace.write_text", "workspace.list", "workspace.bundle"}
    if reader_available:
        available.add("workspace.read")
    plan = _dispatch_plan(
        roles, context, capability_gateway=FakeCapabilityAvailability(available),
    )

    agent = next(agent for agent in plan.agents if agent.id == "implementer")
    step = next(step for step in plan.steps if step.agent == "implementer")
    assert ("workspace.read" in step.tools) is reader_available
    assert ("workspace.read" in agent.allowed_tools) is reader_available
    assert ("workspace.read" in plan.allowed_tools) is reader_available
    assert ("workspace.read" in step.task) is reader_available
    assert {"workspace.write_text", "workspace.list", "workspace.bundle"}.issubset(step.tools)
    assert "read_context" not in step.tools and "project.generate_zip" not in step.tools
    assert step.tool_argument_budget_bytes == {"workspace.write_text": 512_000}


def test_legacy_explicit_role_does_not_receive_workspace_reader_or_guidance() -> None:
    role = RolePlanner().plan(
        RolePlanningRequest(
            task="Implement a TypeScript business project with tests and a workspace bundle.",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.SOFTWARE,
        )
    ).role("implementer")
    legacy_tools = ("workspace.write_text", "workspace.list", "workspace.bundle")
    role = replace(role, allowed_tools=legacy_tools, skills=())
    context = TaskContext(
        run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISPATCH,
        request="Build a real medium business project with sources and tests.",
        routing_decision={
            "project_scale": "medium", "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
        },
    )
    plan = _dispatch_plan(
        (role,), context,
        capability_gateway=FakeCapabilityAvailability({*legacy_tools, "workspace.read"}),
    )

    assert plan.agents[0].allowed_tools == legacy_tools
    step = next(step for step in plan.steps if step.agent == "implementer")
    assert step.tools == legacy_tools
    assert "workspace.read" not in plan.allowed_tools
    assert "workspace.read" not in step.task
    assert "Build the project incrementally" in step.task


@pytest.mark.parametrize("reader_available", (False, True))
def test_replay_safe_workspace_reader_requires_actual_gateway_availability(
    tmp_path: Path, reader_available: bool,
) -> None:
    roles = RolePlanner().plan(
        RolePlanningRequest(
            task="Implement a TypeScript business project with tests and a workspace bundle.",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.SOFTWARE,
        )
    ).roles
    context = TaskContext(
        run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISPATCH,
        request="Build a real medium business project with sources and tests.",
        routing_decision={
            "project_scale": "medium", "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
        },
    )

    class ScopedRunRepository:
        async def get(self, tenant_id: UUID, run_id: UUID) -> object:
            assert tenant_id == TENANT_ID and run_id == context.run_id
            return SimpleNamespace(
                tenant_id=tenant_id, id=run_id,
                routing_decision={
                    "project_id": "reader-test", "workspace_session_id": "reader-session",
                    "sandbox_profile": "workspace_write",
                    "requested_permissions": ("workspace.read", "workspace.write"),
                },
            )

    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills", project_workspace_dir=tmp_path / "projects",
        generated_artifact_dir=tmp_path / "artifacts", skill_sandboxes={},
        run_repository=ScopedRunRepository() if reader_available else None,
    )
    assert gateway.is_replay_safe("workspace.read") is True
    assert gateway.is_available(TENANT_ID, "workspace.read") is reader_available
    plan = _dispatch_plan(roles, context, capability_gateway=gateway)
    step = next(step for step in plan.steps if step.agent == "implementer")
    agent = next(agent for agent in plan.agents if agent.id == "implementer")
    assert ("workspace.read" in step.tools) is reader_available
    assert ("workspace.read" in agent.allowed_tools) is reader_available
    assert ("workspace.read" in plan.allowed_tools) is reader_available
    assert ("workspace.read" in step.task) is reader_available
    assert {"workspace.write_text", "workspace.list", "workspace.bundle"}.issubset(step.tools)


@pytest.mark.parametrize("reader_available", (False, True))
def test_incremental_reader_guidance_uses_returned_relative_paths_only_when_available(
    reader_available: bool,
) -> None:
    tools: tuple[str, ...] = ("workspace.write_text", "workspace.list", "workspace.bundle")
    if reader_available:
        tools = (*tools, "workspace.read")
    context = TaskContext(
        run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISPATCH,
        request="Build a TypeScript business project with tests.",
    )
    guidance = defaults_module._software_delivery_guidance(context, tools)

    assert "Build the project incrementally" in guidance
    assert ("workspace.read" in guidance) is reader_available
    if reader_available:
        assert "relative paths returned by workspace.write_text or workspace.list" in guidance
        assert "workspace/current" in guidance
        assert "Do not prepend" in guidance


@pytest.mark.parametrize("read_authorized", (False, True))
async def test_workspace_reader_requires_persisted_read_permission_after_write(
    tmp_path: Path, read_authorized: bool,
) -> None:
    run_id = uuid4()
    permissions = ["workspace.write"]
    if read_authorized:
        permissions.append("workspace.read")

    class ScopedRunRepository:
        async def get(self, tenant_id: UUID, requested_run_id: UUID) -> object:
            assert tenant_id == TENANT_ID and requested_run_id == run_id
            return SimpleNamespace(
                tenant_id=tenant_id, id=requested_run_id,
                routing_decision={
                    "project_id": "reader-test", "workspace_session_id": "reader-session",
                    "sandbox_profile": "workspace_write", "requested_permissions": permissions,
                },
            )

    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills", project_workspace_dir=tmp_path / "projects",
        run_repository=ScopedRunRepository(), skill_sandboxes={},
    )
    content = "export type OwnReaderTest = { own: boolean };\n"
    written = await gateway.execute(
        tenant_id=TENANT_ID, run_id=run_id, actor="implementer",
        name="workspace.write_text", arguments={"path": "src/types.ts", "content": content},
        idempotency_key="reader-test-write",
    )
    file = written["file"]
    assert isinstance(file, Mapping) and file["path"] == "src/types.ts"
    arguments: dict[str, JsonValue] = {
        "path": "src/types.ts", "requested_permissions": ("workspace.read",),
    }
    if not read_authorized:
        with pytest.raises(RuntimeCapabilityError, match="workspace read denied"):
            await gateway.execute(
                tenant_id=TENANT_ID, run_id=run_id, actor="implementer", name="workspace.read",
                arguments=arguments, idempotency_key="reader-test-read",
            )
        return
    result = await gateway.execute(
        tenant_id=TENANT_ID, run_id=run_id, actor="implementer", name="workspace.read",
        arguments=arguments, idempotency_key="reader-test-read",
    )
    assert result == {"path": "src/types.ts", "text": content, "truncated": False}


def test_natural_large_website_uses_workspace_bundle_budget_and_preview_contract() -> None:
    roles = (
        RoleAssignment(
            id="architect",
            role="Architect",
            purpose=RolePurpose.PLAN,
            mission="Plan the requested project.",
            must_answer=("What should be built?",),
            allowed_tools=("read_context",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Build the requested project.",
            must_answer=("What code was produced?",),
            allowed_tools=(
                "read_context",
                "run_safe_command",
                "workspace.write_text",
                "workspace.list",
                "workspace.bundle",
                "project.generate_zip",
            ),
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
            request="编写一个网盘网站",
            timeout_seconds=1200,
            routing_decision={
                "project_scale": "large",
                "project_delivery": "workspace",
                "artifact_strategy": "workspace_bundle",
                "website_preview_required": True,
            },
        ),
        capability_gateway=FakeCapabilityAvailability(
            {
                "read_context",
                "run_safe_command",
                "workspace.write_text",
                "workspace.list",
                "workspace.bundle",
                "project.generate_zip",
            }
        ),
    )

    architect_step = next(step for step in plan.steps if step.agent == "architect")
    implementer_step = next(step for step in plan.steps if step.agent == "implementer")
    final_step = next(step for step in plan.steps if step.id == "final_response_step")
    assert architect_step.tools == ()
    assert implementer_step.tools == (
        "run_safe_command",
        "workspace.write_text",
        "workspace.list",
        "workspace.bundle",
    )
    assert implementer_step.tool_argument_budget_bytes == {"workspace.write_text": 512_000}
    assert "Build the project incrementally" in implementer_step.task
    assert "workspace.bundle succeeds" in implementer_step.task
    assert "preview.html" in implementer_step.task
    assert "self-contained" in implementer_step.task
    assert "same-origin" in implementer_step.task
    assert "root-relative" in implementer_step.task
    assert "allow empty" in implementer_step.task
    assert "concise" in final_step.task


@pytest.mark.parametrize("preview_required", (False, True))
@pytest.mark.parametrize("tools", (
    ("project.generate_zip",),
    ("workspace.write_text", "workspace.list", "workspace.bundle"),
    ("workspace.write_text", "workspace.list", "workspace.bundle", "project.generate_zip"),
))
def test_software_delivery_preview_api_contract(
    preview_required: bool, tools: tuple[str, ...],
) -> None:
    context = TaskContext(
        run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISPATCH,
        request="Build a task API project.",
        routing_decision={
            "project_scale": "small", "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle", "website_preview_required": preview_required,
        },
    )
    guidance = defaults_module._software_delivery_guidance(context, tools)
    if not preview_required:
        assert "Preview API contract:" not in guidance
        return
    assert guidance.count("Preview API contract:") == 1
    for requirement in (
        "same-origin", "actual backend", "root-relative", "empty string", "allow empty",
        "localhost", "external API", "static previews offline",
    ):
        assert requirement in guidance


def test_project_scale_zip_implementer_gets_extended_step_timeout() -> None:
    roles = (
        RoleAssignment(
            id="architect",
            role="Architect",
            purpose=RolePurpose.PLAN,
            mission="Plan the requested project.",
            must_answer=("What architecture is needed?",),
            allowed_tools=(),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Build the requested project.",
            must_answer=("What code was produced?",),
            allowed_tools=("project.generate_zip",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="tester",
            role="Tester",
            purpose=RolePurpose.VERIFY,
            mission="Verify the generated project.",
            must_answer=("What verification passed?",),
            allowed_tools=(),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="security_reviewer",
            role="Security Reviewer",
            purpose=RolePurpose.RISK_REVIEW,
            mission="Review the generated project risks.",
            must_answer=("What risks remain?",),
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
            request=(
                "Build a real small business project for flow=dispatch. "
                "Return strict JSON workspace_bundle.files (relative paths to full content). "
                "Include npm run build, npm test, source, tests, README, plan and verification instructions."
            ),
            timeout_seconds=900,
            token_budget=1_000_000,
        ),
        capability_gateway=FakeCapabilityAvailability({"project.generate_zip"}),
    )

    steps = {step.agent: step for step in plan.steps}
    assert steps["architect"].timeout_seconds == 405
    assert steps["implementer"].timeout_seconds == 600
    assert steps["implementer"].timeout_seconds > steps["architect"].timeout_seconds


def test_project_scale_workspace_writer_gets_extended_step_timeout() -> None:
    roles = (
        RoleAssignment(
            id="architect",
            role="Architect",
            purpose=RolePurpose.PLAN,
            mission="Plan the requested project.",
            must_answer=("What architecture is needed?",),
            allowed_tools=(),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Build the requested project.",
            must_answer=("What code was produced?",),
            allowed_tools=("workspace.write_text",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="tester",
            role="Tester",
            purpose=RolePurpose.VERIFY,
            mission="Verify the generated project.",
            must_answer=("What verification passed?",),
            allowed_tools=(),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
        RoleAssignment(
            id="security_reviewer",
            role="Security Reviewer",
            purpose=RolePurpose.RISK_REVIEW,
            mission="Review the generated project risks.",
            must_answer=("What risks remain?",),
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
            request=(
                "Build a real large business project for flow=multi_agent. "
                "Write the complete generated project into the workspace."
            ),
            timeout_seconds=1200,
            token_budget=1_000_000,
            routing_decision={
                "project_scale": "large",
                "project_delivery": "workspace",
                "artifact_strategy": "workspace_bundle",
            },
        ),
        capability_gateway=FakeCapabilityAvailability({"workspace.write_text"}),
    )

    steps = {step.agent: step for step in plan.steps}
    assert steps["implementer"].timeout_seconds == 800
    assert steps["implementer"].timeout_seconds > steps["architect"].timeout_seconds


@pytest.mark.parametrize(
    ("project_request", "expected_budget"),
    (
        (
            (
                "Build a real medium business project for flow=dispatch. "
                "Return strict JSON workspace_bundle.files (relative paths to full content)."
            ),
            6_000_000,
        ),
    ),
)
def test_project_scale_zip_argument_budget_scales_with_project_size(
    project_request: str,
    expected_budget: int,
) -> None:
    roles = (
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Build the requested project.",
            must_answer=("What code was produced?",),
            allowed_tools=("project.generate_zip",),
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
            request=project_request,
        ),
        capability_gateway=FakeCapabilityAvailability({"project.generate_zip"}),
    )

    implementer_step = next(step for step in plan.steps if step.agent == "implementer")
    assert implementer_step.tool_argument_budget_bytes == {
        "project.generate_zip": expected_budget
    }


@pytest.mark.parametrize("scale", ("large", "ultra-large"))
def test_large_project_zip_only_plan_requires_incremental_workspace(scale: str) -> None:
    roles = (
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Build the requested project.",
            must_answer=("What code was produced?",),
            allowed_tools=("project.generate_zip",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
    )

    with pytest.raises(ValidationError, match="incremental workspace"):
        _dispatch_plan(
            roles,
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request=(
                    f"Build a real {scale} business project for flow=dispatch. "
                    "Return strict JSON workspace_bundle.files."
                ),
            ),
            capability_gateway=FakeCapabilityAvailability({"project.generate_zip"}),
        )


def test_project_scale_zip_argument_budget_uses_planner_complexity_signals() -> None:
    roles = (
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Build the requested project.",
            must_answer=("What code was produced?",),
            allowed_tools=("project.generate_zip",),
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
            request=(
                "Build a real small business project for flow=dispatch. "
                "Return strict JSON workspace_bundle.files (relative paths to full content). "
                "Include auth, admin, upload, download, database persistence, search, "
                "actual build/test evidence, generated_project_validation, and repair."
            ),
            timeout_seconds=900,
            token_budget=1_000_000,
        ),
        capability_gateway=FakeCapabilityAvailability({"project.generate_zip"}),
    )

    implementer_step = next(step for step in plan.steps if step.agent == "implementer")
    assert implementer_step.tool_argument_budget_bytes == {
        "project.generate_zip": 9_000_000
    }


def test_project_scale_zip_argument_budget_caps_complex_large_projects() -> None:
    roles = (
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Build the requested project.",
            must_answer=("What code was produced?",),
            allowed_tools=("project.generate_zip",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"summary": "string"},
            model="main",
        ),
    )

    with pytest.raises(ValidationError, match="incremental workspace"):
        _dispatch_plan(
            roles,
            TaskContext(
                run_id=uuid4(),
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request=(
                    "Build a real large business project for flow=dispatch. "
                    "Return strict JSON workspace_bundle.files (relative paths to full content). "
                    "Include auth, admin, database, upload, download, worker, queue, websocket, "
                    "payment, billing, build/test, generated_project_validation, and self-repair."
                ),
                timeout_seconds=1800,
                token_budget=2_000_000,
            ),
            capability_gateway=FakeCapabilityAvailability({"project.generate_zip"}),
        )


@pytest.mark.parametrize(
    ("routing_decision", "expected_budget"),
    (
        ({"project_zip_argument_budget_bytes": 7_500_000}, 7_500_000),
        (
            {
                "tool_argument_budget_bytes": {
                    "project.generate_zip": 8_250_000,
                }
            },
            8_250_000,
        ),
        ({"project_zip_argument_budget_bytes": 99_000_000}, 10_000_000),
        ({"project_zip_argument_budget_bytes": "9000000"}, 3_000_000),
    ),
)
def test_project_scale_zip_argument_budget_accepts_bounded_planner_override(
    routing_decision: Mapping[str, JsonValue],
    expected_budget: int,
) -> None:
    roles = (
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Build the requested project.",
            must_answer=("What code was produced?",),
            allowed_tools=("project.generate_zip",),
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
            request=(
                "Build a real small business project for flow=dispatch. "
                "Return strict JSON workspace_bundle.files (relative paths to full content)."
            ),
            routing_decision=routing_decision,
        ),
        capability_gateway=FakeCapabilityAvailability({"project.generate_zip"}),
    )

    implementer_step = next(step for step in plan.steps if step.agent == "implementer")
    assert implementer_step.tool_argument_budget_bytes == {
        "project.generate_zip": expected_budget
    }


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


def test_role_model_fallbacks_keep_unselected_candidates_internal_only() -> None:
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
    roles = (
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Build and package the project.",
            must_answer=("What changed?",),
            allowed_tools=("project.generate_zip",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={},
            model="qwen",
        ),
    )
    assigned = _assign_models_to_roles(
        roles,
        config,
        default_model="deepseek",
        task="Build a small software project and package a ZIP.",
        role_tools_by_id={"implementer": ("project.generate_zip",)},
    )

    fallbacks = _role_model_fallbacks_by_id(
        roles,
        assigned,
        config,
        default_model="deepseek",
        task="Build a small software project and package a ZIP.",
        role_tools_by_id={"implementer": ("project.generate_zip",)},
    )
    matrix, _truncated = _role_model_routing_matrix_payload(
        roles,
        assigned,
        config,
        default_model="deepseek",
        task="Build a small software project and package a ZIP.",
        role_tools_by_id={"implementer": ("project.generate_zip",)},
    )

    assert assigned[0].model == "qwen"
    assert fallbacks["implementer"] == ("deepseek",)
    candidates_value = matrix[0]["candidates"]
    assert isinstance(candidates_value, tuple)
    candidates = cast(tuple[Mapping[str, JsonValue], ...], candidates_value)
    assert [candidate["logical_model"] for candidate in candidates] == ["qwen"]


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


def test_role_model_assignment_routes_structured_output_roles_to_capable_model() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "plain": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://plain",
                            "quota_scope_id": "plain",
                            "max_concurrency": 3,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text"],
                        }
                    ]
                },
                "structured": {
                    "deployments": [
                        {
                            "provider": "qwen",
                            "model": "qwen3-max",
                            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            "credential_ref": "secret://structured",
                            "quota_scope_id": "structured",
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
        id="reviewer",
        role="Reviewer",
        purpose=RolePurpose.VERIFY,
        mission="按 schema 输出验收摘要。",
        must_answer=("验收结果是什么？",),
        allowed_tools=(),
        forbidden_actions=("不要执行危险操作。",),
        skills=(),
        output_schema={"summary": "string"},
        model="plain",
    )

    assigned = _assign_models_to_roles(
        (role,),
        config,
        default_model="plain",
        task="生成结构化验收摘要。",
    )

    assert assigned[0].model == "structured"


def test_role_model_assignment_fails_closed_when_structured_output_role_has_no_capable_model() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "plain": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://plain",
                            "quota_scope_id": "plain",
                            "max_concurrency": 3,
                            "target_utilization": 0.8,
                            "reserved_slots": 0,
                            "capabilities": ["text"],
                        }
                    ]
                }
            },
            "agents": [],
        }
    )
    role = RoleAssignment(
        id="reviewer",
        role="Reviewer",
        purpose=RolePurpose.VERIFY,
        mission="按 schema 输出验收摘要。",
        must_answer=("验收结果是什么？",),
        allowed_tools=(),
        forbidden_actions=("不要执行危险操作。",),
        skills=(),
        output_schema={"summary": "string"},
        model="plain",
    )

    with pytest.raises(
        defaults_module.HarnessModelSelectionError,
        match="model capability unavailable",
    ):
        _assign_models_to_roles(
            (role,),
            config,
            default_model="plain",
            task="生成结构化验收摘要。",
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


def test_discussion_plan_inherits_runtime_resource_budget() -> None:
    roles = (
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
            id="reviewer",
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
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISCUSS,
        request="讨论一个超大型项目。",
        timeout_seconds=1_800.0,
        token_budget=900_000,
    )

    plan = _discussion_plan(roles, "main", context)

    assert plan.wall_time_seconds == 1_800.0
    assert plan.token_budget == 900_000
    assert plan.max_turns == 4


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


def test_multi_agent_default_dispatch_plan_enforces_artifact_handoff_chain() -> None:
    roles = (
        RoleAssignment(
            id="architect",
            role="Architect",
            purpose=RolePurpose.PLAN,
            mission="Define architecture and interfaces.",
            must_answer=("What should be built?",),
            allowed_tools=("read_context",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"architecture": "string"},
            model="main",
        ),
        RoleAssignment(
            id="implementer",
            role="Implementer",
            purpose=RolePurpose.EXECUTE,
            mission="Implement the architecture.",
            must_answer=("What was implemented?",),
            allowed_tools=("workspace.write_text",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"implementation": "string"},
            model="main",
        ),
        RoleAssignment(
            id="tester",
            role="Tester",
            purpose=RolePurpose.VERIFY,
            mission="Verify the implementation independently.",
            must_answer=("What passed?",),
            allowed_tools=("run_safe_command",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"verification": "string"},
            model="main",
        ),
        RoleAssignment(
            id="security_reviewer",
            role="Security Reviewer",
            purpose=RolePurpose.RISK_REVIEW,
            mission="Review security risks.",
            must_answer=("What risks remain?",),
            allowed_tools=("read_context",),
            forbidden_actions=("Do not perform dangerous operations.",),
            skills=(),
            output_schema={"risks": "string"},
            model="main",
        ),
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request=(
            "Build a real large business project for flow=multi_agent. "
            "Use four distinct agents with mandatory artifact dependencies."
        ),
        routing_decision={"project_scale": "large"},
    )

    plan = _dispatch_plan(roles, context, max_parallelism=4)

    assert tuple(agent.id for agent in plan.agents) == (
        "architect",
        "implementer",
        "tester",
        "synthesizer",
    )
    steps = {step.agent: step for step in plan.steps}
    assert steps["architect"].depends_on == ()
    assert steps["implementer"].depends_on == ("architect_step",)
    assert steps["tester"].depends_on == ("implementer_step",)
    assert steps["synthesizer"].depends_on == (
        "architect_step",
        "implementer_step",
        "tester_step",
    )
    assert plan.layers == (
        ("architect_step",),
        ("implementer_step",),
        ("tester_step",),
        ("final_response_step",),
    )
    assert plan.max_parallelism == 1


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


@pytest.mark.asyncio
async def test_configured_runtime_registry_injects_artifact_repository_into_dispatch_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProbeDispatchRuntime.instances.clear()

    class SpyCapacityPool(ImmediateCapacity):
        def __init__(
            self,
            redis_client: object,
            *,
            deployments: Sequence[Deployment],
            fingerprint_resolver: Callable[[str], Awaitable[str]],
        ) -> None:
            del redis_client, fingerprint_resolver
            super().__init__(tuple(deployments))

    monkeypatch.setattr(defaults_module, "CapacityPool", SpyCapacityPool)
    monkeypatch.setattr(defaults_module, "CrewDispatchRuntime", ProbeDispatchRuntime)
    artifact_repository = InMemoryArtifactRepository()
    registry = configured_runtime_registry(
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
                                "capabilities": ["text", "structured_output"],
                            }
                        ]
                    }
                },
                "agents": [
                    {
                        "id": "writer",
                        "role": "Writer",
                        "prompt": "Draft concise text.",
                        "model": "main",
                        "skills": [],
                    }
                ],
            }
        ),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        redis_client=object(),
        transport=FakeTransport(),
        artifact_repository=artifact_repository,
    )

    for _ in range(2):
        events = [
            event
            async for event in registry.get(TaskMode.DISPATCH).run(
                TaskContext(
                    run_id=uuid4(),
                    tenant_id=TENANT_ID,
                    mode=TaskMode.DISPATCH,
                    request="Draft a short note.",
                    routing_decision={
                        "selected_agent_ids": ("writer",),
                        "main_agent_model": "main",
                    },
                )
            )
        ]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED

    assert len(ProbeDispatchRuntime.instances) == 2
    assert all(
        child.artifact_repository is artifact_repository
        for child in ProbeDispatchRuntime.instances
    )
