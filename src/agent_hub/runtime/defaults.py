"""Default and production runtime registry construction."""

from __future__ import annotations

import keyword
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.config.schema import AgentDefinition, LogicalModelDefinition, PlatformConfig
from agent_hub.config.service import ConfigService
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.capacity import (
    CapacityPool,
    safe_operational_limit,
)
from agent_hub.models.gateway import CapacityController, ModelGateway, ModelTransport
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.profiles import infer_model_traits
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.routing_matrix import (
    RoleModelRoutingRequest,
    rank_role_models,
)
from agent_hub.models.routing_matrix import (
    task_characteristics as routing_task_characteristics,
)
from agent_hub.models.routing_policy import (
    DeploymentRoutingConstraint,
    DeploymentRoutingConstraintError,
    FallbackExecutionPolicy,
    FallbackExecutionPolicyError,
    constrain_deployments_for_routing,
    deployment_routing_constraint_from_decision,
    fallback_execution_policy_from_decision,
)
from agent_hub.models.types import Deployment, ModelCapability
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
)
from agent_hub.runtime.contracts import (
    EventKind,
    ExecutionRuntime,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.hermes_context import hermes_memory_context_text
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.runtime.role_planner import (
    RoleAssignment,
    RolePlanner,
    RolePlanningRequest,
    RolePurpose,
    TaskProfile,
)
from agent_hub.security.secrets import SecretService


class SecretResolver(Protocol):
    async def resolve(self, secret_ref: str) -> str: ...


class RuntimeCapabilityGatewayProtocol(Protocol):
    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]: ...

    def is_replay_safe(self, name: str) -> bool: ...


class HarnessToolInvoker(Protocol):
    async def invoke(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult: ...


CapacityFactory = Callable[
    [UUID, tuple[Deployment, ...]],
    Awaitable[CapacityController | CapacityPool],
]
_DISPATCH_OUTPUT_SCHEMA: Mapping[str, str] = {
    "status": "done | blocked | needs_user",
    "summary": "string",
    "evidence": "string[]",
    "risks": "string[]",
    "artifacts": "string[]",
    "verification": "string[]",
}
_MAX_CAPABILITY_INVENTORY_ITEMS = 96
_MAX_CAPABILITY_INVENTORY_ALIASES = 16
_MAX_CAPABILITY_INVENTORY_SCAN_ITEMS = 512
_MAX_ORCHESTRATION_HANDOFFS = 12
_ORCHESTRATION_CONTRACT_READY_STATUS = "done"
_ORCHESTRATION_CONTRACT_BLOCKING_STATUSES = ("blocked", "needs_user")
_ORCHESTRATION_CONTRACT_RECOVERY_HINT = "retry_blocked_contract_chain"
_ORCHESTRATION_PROTOCOL_ID = "role_handoff_contract_v1"
_DISPATCH_OUTPUT_SCHEMA_ID = "dispatch_output_v1"
_SAFE_CAPABILITY_INVENTORY_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SAFE_MODEL_SELECTION_TEXT = re.compile(r"^[A-Za-z0-9_.:/@ -]{1,128}$")
_SENSITIVE_CAPABILITY_INVENTORY_TEXT = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "password",
        "secret",
        "token",
    }
)
_SENSITIVE_ORCHESTRATION_HANDOFF_TEXT = _SENSITIVE_CAPABILITY_INVENTORY_TEXT | frozenset(
    {
        "api_base",
        "apibase",
        "capacity",
        "credential",
        "lease",
        "quota",
    }
)
_SOFTWARE_TASK_KEYWORDS = (
    "code",
    "代码",
    "源码",
    "项目源码",
    "python",
    "javascript",
    "typescript",
    "node",
    "react",
    "vue",
    "main.py",
    ".py",
    ".js",
    ".ts",
    "zip",
    "压缩包",
    "可下载",
    "download",
    "网页",
    "web",
    "前端",
    "后端",
    "api",
    "github",
    "test",
    "测试",
)
_DISCUSSION_OUTPUT_SCHEMA: Mapping[str, str] = {
    "position": "approve | reject | needs_user",
    "recommended_option": "string | null",
    "confidence": "0.0-1.0",
    "claims": "string[]",
    "evidence": "string[]",
    "objections": "string[]",
    "risks": "string[]",
    "questions_for_user": "string[]",
    "verification_needed": "string[]",
}
_MAX_MODEL_ROUTING_MATRIX_ROLES = 24
_MAX_MODEL_ROUTING_MATRIX_REASONS = 16
_MAX_MODEL_ROUTING_MATRIX_TRAITS = 16
_MODEL_CAPABILITY_TRAIT_ALIASES: Mapping[str, ModelCapability] = {
    "general": ModelCapability.TEXT,
    "text": ModelCapability.TEXT,
    "tool": ModelCapability.TOOL_CALLING,
    "tool_calling": ModelCapability.TOOL_CALLING,
    "structured": ModelCapability.STRUCTURED_OUTPUT,
    "structured_output": ModelCapability.STRUCTURED_OUTPUT,
    "vision": ModelCapability.VISION,
    "image": ModelCapability.VISION,
    "audio": ModelCapability.AUDIO,
    "speech": ModelCapability.AUDIO,
    "voice": ModelCapability.AUDIO,
    "image_generation": ModelCapability.IMAGE_GENERATION,
    "video_generation": ModelCapability.VIDEO_GENERATION,
    "audio_generation": ModelCapability.AUDIO_GENERATION,
}


class UnavailableRuntime:
    """Fail queued runs deterministically instead of leaving them stuck forever."""

    def __init__(self, mode: TaskMode, *, reason: str = "runtime_not_configured") -> None:
        if mode is TaskMode.AUTO:
            raise ValueError("default runtime mode must be executable")
        self.mode: TaskMode = mode
        self._reason = reason

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.RUNTIME_FAILED,
            sequence=1,
            run_id=context.run_id,
            reason=self._reason,
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise RuntimeError("runtime checkpoint unavailable")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        return None


class TenantSecretResolver:
    """Resolve model credentials inside the tenant boundary carried by a run."""

    def __init__(self, secret_service: SecretService, tenant_id: UUID) -> None:
        self._secret_service = secret_service
        self._tenant_id = tenant_id

    async def resolve(self, secret_ref: str) -> str:
        return await self._secret_service.resolve(self._tenant_id, secret_ref)


class HarnessModelSelectionError(RuntimeError):
    """Stable failure when a recorded harness model selection no longer exists."""


class _PlannedRuntime:
    """Add the main-Agent planning decision before a configured child runtime starts."""

    def __init__(
        self,
        child: ExecutionRuntime,
        *,
        mode: TaskMode,
        main_agent_model: str,
        roles: tuple[Mapping[str, JsonValue], ...],
        steps: tuple[Mapping[str, JsonValue], ...],
        model_routing_matrix: tuple[Mapping[str, JsonValue], ...] = (),
        model_routing_matrix_truncated: bool = False,
        deployment_constraints: Mapping[str, JsonValue] | None = None,
        deployment_constraint: DeploymentRoutingConstraint | None = None,
        fallback_policy: FallbackExecutionPolicy = "configured",
        capability_gateway: RuntimeCapabilityGatewayProtocol | None = None,
    ) -> None:
        self.mode = mode
        self._child = child
        self._main_agent_model = main_agent_model
        self._roles = roles
        self._steps = steps
        self._model_routing_matrix = model_routing_matrix
        self._model_routing_matrix_truncated = model_routing_matrix_truncated
        self._deployment_constraints = deployment_constraints
        self._deployment_constraint = deployment_constraint
        self._fallback_policy = fallback_policy
        self._capability_gateway = capability_gateway

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        sequence_offset = 1
        if context.checkpoint is None:
            yield RunEvent(
                kind=EventKind.STEP_STARTED,
                sequence=1,
                run_id=context.run_id,
                actor="main_agent",
                step_id="main_agent_plan",
                payload={
                    "mode": self.mode.value,
                    "main_agent_model": self._main_agent_model,
                    "logical_model": self._main_agent_model,
                    "task": "选择运行模式、角色和模型。",
                    "summary": "Main Agent selected the runtime mode, roles, and models.",
                    "roles": self._roles,
                    "steps": self._steps,
                    "model_execution_plan": _model_execution_plan_payload(
                        context,
                        main_agent_model=self._main_agent_model,
                        roles=self._roles,
                        steps=self._steps,
                        model_routing_matrix=self._model_routing_matrix,
                        model_routing_matrix_truncated=self._model_routing_matrix_truncated,
                        deployment_constraints=self._deployment_constraints,
                        deployment_constraint=self._deployment_constraint,
                        fallback_policy=self._fallback_policy,
                    ),
                    "capability_execution_plan": _capability_execution_plan_payload(
                        self._roles,
                        tenant_id=context.tenant_id,
                        capability_gateway=self._capability_gateway,
                    ),
                },
            )
        async for event in self._child.run(context):
            yield _renumber_event(event, sequence_offset, run_id=context.run_id)

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        return await self._child.save_checkpoint()

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        await self._child.restore_checkpoint(checkpoint)

    async def cancel(self) -> None:
        await self._child.cancel()


def _renumber_event(event: RunEvent, offset: int, *, run_id: UUID) -> RunEvent:
    return event.model_copy(update={"sequence": event.sequence + offset, "run_id": run_id})


class ConfigBackedDirectRuntime:
    """Build a fresh DirectRuntime from the published model config for each run."""

    mode = TaskMode.DIRECT

    def __init__(
        self,
        *,
        config_service: ConfigService,
        secret_service: SecretService,
        capacity_factory: CapacityFactory,
        transport: ModelTransport | None = None,
    ) -> None:
        self._config_service = config_service
        self._secret_service = secret_service
        self._capacity_factory = capacity_factory
        self._transport = transport or LiteLLMClient()
        self._pending_checkpoints: dict[UUID, RuntimeCheckpoint] = {}
        self._active: dict[UUID, ExecutionRuntime] = {}

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        runtime = await self._runtime_for(context)
        checkpoint = self._pending_checkpoints.pop(context.run_id, None)
        if checkpoint is not None:
            await runtime.restore_checkpoint(checkpoint)
        self._active[context.run_id] = runtime
        try:
            async for event in runtime.run(context):
                yield event
        finally:
            self._active.pop(context.run_id, None)

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise RuntimeError("runtime checkpoint unavailable outside an active run")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        self._pending_checkpoints[checkpoint.run_id] = checkpoint

    async def cancel(self) -> None:
        active = tuple(self._active.values())
        for runtime in active:
            await runtime.cancel()

    async def _runtime_for(self, context: TaskContext) -> ExecutionRuntime:
        current = await self._config_service.get_current(context.tenant_id)
        if current is None:
            return UnavailableRuntime(TaskMode.DIRECT)
        config = PlatformConfig.model_validate(current.document)
        if not config.models:
            return UnavailableRuntime(TaskMode.DIRECT)
        try:
            gateway, logical_model, _fallback_policy = await _gateway_for_config(
                config,
                tenant_id=context.tenant_id,
                secret_service=self._secret_service,
                capacity_factory=self._capacity_factory,
                transport=self._transport,
                routing_decision=context.routing_decision,
            )
        except HarnessModelSelectionError:
            return UnavailableRuntime(TaskMode.DIRECT, reason="harness_model_unavailable")
        return DirectRuntime(gateway, logical_model=logical_model)


class ConfigBackedDispatchRuntime:
    """Build a fresh Crew-style dispatch runtime from published model config."""

    mode = TaskMode.DISPATCH

    def __init__(
        self,
        *,
        config_service: ConfigService,
        secret_service: SecretService,
        capacity_factory: CapacityFactory,
        transport: ModelTransport | None = None,
        role_planner: RolePlanner | None = None,
        capability_gateway: RuntimeCapabilityGatewayProtocol | None = None,
        harness_tool_gateway: HarnessToolInvoker | None = None,
    ) -> None:
        self._config_service = config_service
        self._secret_service = secret_service
        self._capacity_factory = capacity_factory
        self._transport = transport or LiteLLMClient()
        self._role_planner = role_planner or RolePlanner()
        self._capability_gateway = capability_gateway
        self._harness_tool_gateway = harness_tool_gateway
        self._pending_checkpoints: dict[UUID, RuntimeCheckpoint] = {}
        self._active: dict[UUID, ExecutionRuntime] = {}

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        runtime = await self._runtime_for(context)
        checkpoint = self._pending_checkpoints.pop(context.run_id, None)
        if checkpoint is not None:
            await runtime.restore_checkpoint(checkpoint)
        self._active[context.run_id] = runtime
        try:
            async for event in runtime.run(context):
                yield event
        finally:
            self._active.pop(context.run_id, None)

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise RuntimeError("runtime checkpoint unavailable outside an active run")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        self._pending_checkpoints[checkpoint.run_id] = checkpoint

    async def cancel(self) -> None:
        for runtime in tuple(self._active.values()):
            await runtime.cancel()

    async def _runtime_for(self, context: TaskContext) -> ExecutionRuntime:
        config = await _current_platform_config(self._config_service, context.tenant_id)
        if config is None:
            return UnavailableRuntime(TaskMode.DISPATCH)
        try:
            gateway, logical_model, fallback_policy = await _gateway_for_config(
                config,
                tenant_id=context.tenant_id,
                secret_service=self._secret_service,
                capacity_factory=self._capacity_factory,
                transport=self._transport,
                routing_decision=context.routing_decision,
            )
        except HarnessModelSelectionError:
            return UnavailableRuntime(TaskMode.DISPATCH, reason="harness_model_unavailable")
        await _prepare_capability_gateway_for_tenant(
            context.tenant_id,
            capability_gateway=self._capability_gateway,
        )
        selected_roles = _selected_config_role_assignments(
            context,
            config,
            purpose=RolePurpose.EXECUTE,
            output_schema=_DISPATCH_OUTPUT_SCHEMA,
        )
        if selected_roles:
            planned_roles = selected_roles
        else:
            planned_roles = self._role_planner.plan(
                RolePlanningRequest(
                    task=str(context.request),
                    mode=TaskMode.DISPATCH,
                    profile=_task_profile(context.request),
                    profiles=_task_profiles(context.request),
                    high_risk=_high_risk_task(context.request),
                    requested_skills=_requested_skills(context),
                    default_model=logical_model,
                )
            ).roles
        role_sources = (*planned_roles, *_temporary_role_assignments(context, logical_model))
        roles = _assign_models_to_roles(
            role_sources,
            config,
            default_model=logical_model,
            task=context.request,
        )
        model_routing_matrix, model_routing_matrix_truncated = _role_model_routing_matrix_payload(
            role_sources,
            roles,
            config,
            default_model=logical_model,
            task=context.request,
        )
        plan = _dispatch_plan(
            roles,
            context,
            max_parallelism=_dispatch_parallelism(config, logical_model, roles),
            capability_gateway=self._capability_gateway,
        )
        role_payload = _dispatch_role_payload(plan)
        deployment_constraint = _deployment_routing_constraint(config, context.routing_decision)
        return _PlannedRuntime(
            CrewDispatchRuntime(
                gateway,
                plan,
                capability_gateway=self._capability_gateway,
                harness_tool_gateway=self._harness_tool_gateway,
            ),
            mode=TaskMode.DISPATCH,
            main_agent_model=logical_model,
            roles=role_payload,
            steps=_dispatch_step_payload(plan),
            model_routing_matrix=model_routing_matrix,
            model_routing_matrix_truncated=model_routing_matrix_truncated,
            deployment_constraints=_deployment_constraints_payload(
                config,
                main_agent_model=logical_model,
                roles=role_payload,
                constraint=deployment_constraint,
                fallback_policy=fallback_policy,
            ),
            deployment_constraint=deployment_constraint,
            fallback_policy=fallback_policy,
            capability_gateway=self._capability_gateway,
        )


class ConfigBackedDiscussionRuntime:
    """Build a fresh AutoGen-style discussion runtime from published model config."""

    mode = TaskMode.DISCUSS

    def __init__(
        self,
        *,
        config_service: ConfigService,
        secret_service: SecretService,
        capacity_factory: CapacityFactory,
        transport: ModelTransport | None = None,
        role_planner: RolePlanner | None = None,
        capability_gateway: RuntimeCapabilityGatewayProtocol | None = None,
        harness_tool_gateway: HarnessToolInvoker | None = None,
    ) -> None:
        self._config_service = config_service
        self._secret_service = secret_service
        self._capacity_factory = capacity_factory
        self._transport = transport or LiteLLMClient()
        self._role_planner = role_planner or RolePlanner()
        self._capability_gateway = capability_gateway
        self._harness_tool_gateway = harness_tool_gateway
        self._pending_checkpoints: dict[UUID, RuntimeCheckpoint] = {}
        self._active: dict[UUID, ExecutionRuntime] = {}

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        runtime = await self._runtime_for(context)
        checkpoint = self._pending_checkpoints.pop(context.run_id, None)
        if checkpoint is not None:
            await runtime.restore_checkpoint(checkpoint)
        self._active[context.run_id] = runtime
        try:
            async for event in runtime.run(context):
                yield event
        finally:
            self._active.pop(context.run_id, None)

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise RuntimeError("runtime checkpoint unavailable outside an active run")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        self._pending_checkpoints[checkpoint.run_id] = checkpoint

    async def cancel(self) -> None:
        for runtime in tuple(self._active.values()):
            await runtime.cancel()

    async def _runtime_for(self, context: TaskContext) -> ExecutionRuntime:
        config = await _current_platform_config(self._config_service, context.tenant_id)
        if config is None:
            return UnavailableRuntime(TaskMode.DISCUSS)
        try:
            gateway, logical_model, fallback_policy = await _gateway_for_config(
                config,
                tenant_id=context.tenant_id,
                secret_service=self._secret_service,
                capacity_factory=self._capacity_factory,
                transport=self._transport,
                routing_decision=context.routing_decision,
            )
        except HarnessModelSelectionError:
            return UnavailableRuntime(TaskMode.DISCUSS, reason="harness_model_unavailable")
        await _prepare_capability_gateway_for_tenant(
            context.tenant_id,
            capability_gateway=self._capability_gateway,
        )
        selected_roles = _selected_config_role_assignments(
            context,
            config,
            purpose=RolePurpose.EXPERTISE,
            output_schema=_DISCUSSION_OUTPUT_SCHEMA,
        )
        if len(selected_roles) >= 2:
            planned_roles = selected_roles
        else:
            planned_roles = self._role_planner.plan(
                RolePlanningRequest(
                    task=str(context.request),
                    mode=TaskMode.DISCUSS,
                    profile=_task_profile(context.request),
                    profiles=_task_profiles(context.request),
                    high_risk=_high_risk_task(context.request),
                    requested_skills=_requested_skills(context),
                    default_model=logical_model,
                )
            ).roles
        roles = _assign_models_to_roles(
            planned_roles,
            config,
            default_model=logical_model,
            task=context.request,
        )
        model_routing_matrix, model_routing_matrix_truncated = _role_model_routing_matrix_payload(
            planned_roles,
            roles,
            config,
            default_model=logical_model,
            task=context.request,
        )
        plan = _discussion_plan(
            roles,
            logical_model,
            context,
            capability_gateway=self._capability_gateway,
        )
        role_payload = _discussion_role_payload(plan)
        deployment_constraint = _deployment_routing_constraint(config, context.routing_decision)
        return _PlannedRuntime(
            AutoGenDiscussionRuntime(
                gateway,
                plan,
                capability_gateway=self._capability_gateway,
                harness_tool_gateway=self._harness_tool_gateway,
            ),
            mode=TaskMode.DISCUSS,
            main_agent_model=logical_model,
            roles=role_payload,
            steps=_discussion_step_payload(plan),
            model_routing_matrix=model_routing_matrix,
            model_routing_matrix_truncated=model_routing_matrix_truncated,
            deployment_constraints=_deployment_constraints_payload(
                config,
                main_agent_model=logical_model,
                roles=role_payload,
                constraint=deployment_constraint,
                fallback_policy=fallback_policy,
            ),
            deployment_constraint=deployment_constraint,
            fallback_policy=fallback_policy,
            capability_gateway=self._capability_gateway,
        )


class ConfigBackedHybridRuntime:
    """Build a fresh hybrid runtime from dispatch, discussion, and synthesis stages."""

    mode = TaskMode.HYBRID

    def __init__(
        self,
        *,
        config_service: ConfigService,
        secret_service: SecretService,
        capacity_factory: CapacityFactory,
        transport: ModelTransport | None = None,
        role_planner: RolePlanner | None = None,
        capability_gateway: RuntimeCapabilityGatewayProtocol | None = None,
        harness_tool_gateway: HarnessToolInvoker | None = None,
    ) -> None:
        self._config_service = config_service
        self._secret_service = secret_service
        self._capacity_factory = capacity_factory
        self._transport = transport or LiteLLMClient()
        self._role_planner = role_planner or RolePlanner()
        self._capability_gateway = capability_gateway
        self._harness_tool_gateway = harness_tool_gateway
        self._pending_checkpoints: dict[UUID, RuntimeCheckpoint] = {}
        self._active: dict[UUID, ExecutionRuntime] = {}

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        runtime = await self._runtime_for(context)
        checkpoint = self._pending_checkpoints.pop(context.run_id, None)
        if checkpoint is not None:
            await runtime.restore_checkpoint(checkpoint)
        self._active[context.run_id] = runtime
        try:
            async for event in runtime.run(context):
                yield event
        finally:
            self._active.pop(context.run_id, None)

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise RuntimeError("runtime checkpoint unavailable outside an active run")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        self._pending_checkpoints[checkpoint.run_id] = checkpoint

    async def cancel(self) -> None:
        for runtime in tuple(self._active.values()):
            await runtime.cancel()

    async def _runtime_for(self, context: TaskContext) -> ExecutionRuntime:
        config = await _current_platform_config(self._config_service, context.tenant_id)
        if config is None:
            return UnavailableRuntime(TaskMode.HYBRID)
        try:
            gateway, logical_model, fallback_policy = await _gateway_for_config(
                config,
                tenant_id=context.tenant_id,
                secret_service=self._secret_service,
                capacity_factory=self._capacity_factory,
                transport=self._transport,
                routing_decision=context.routing_decision,
            )
        except HarnessModelSelectionError:
            return UnavailableRuntime(TaskMode.HYBRID, reason="harness_model_unavailable")
        await _prepare_capability_gateway_for_tenant(
            context.tenant_id,
            capability_gateway=self._capability_gateway,
        )
        profile = _task_profile(context.request)
        profiles = _task_profiles(context.request)
        high_risk = _high_risk_task(context.request)
        selected_dispatch_roles = _selected_config_role_assignments(
            context,
            config,
            purpose=RolePurpose.EXECUTE,
            output_schema=_DISPATCH_OUTPUT_SCHEMA,
        )
        selected_discussion_roles = _selected_config_role_assignments(
            context,
            config,
            purpose=RolePurpose.EXPERTISE,
            output_schema=_DISCUSSION_OUTPUT_SCHEMA,
        )
        if selected_dispatch_roles:
            dispatch_roles = selected_dispatch_roles
        else:
            dispatch_roles = self._role_planner.plan(
                RolePlanningRequest(
                    task=str(context.request),
                    mode=TaskMode.DISPATCH,
                    profile=profile,
                    profiles=profiles,
                    high_risk=high_risk,
                    requested_skills=_requested_skills(context),
                    default_model=logical_model,
                )
            ).roles
        if len(selected_discussion_roles) >= 2:
            discussion_roles = selected_discussion_roles
        elif len(selected_dispatch_roles) >= 2:
            discussion_roles = tuple(
                replace(
                    role,
                    purpose=RolePurpose.EXPERTISE,
                    must_answer=("What is this agent's position and evidence?",),
                    output_schema=_DISCUSSION_OUTPUT_SCHEMA,
                )
                for role in selected_dispatch_roles
            )
        else:
            discussion_roles = self._role_planner.plan(
                RolePlanningRequest(
                    task=str(context.request),
                    mode=TaskMode.DISCUSS,
                    profile=profile,
                    profiles=profiles,
                    high_risk=high_risk,
                    requested_skills=_requested_skills(context),
                    default_model=logical_model,
                )
            ).roles
        dispatch_role_sources = (
            *dispatch_roles,
            *_temporary_role_assignments(context, logical_model),
        )
        discussion_role_sources = discussion_roles
        dispatch_roles = _assign_models_to_roles(
            dispatch_role_sources,
            config,
            default_model=logical_model,
            task=context.request,
        )
        discussion_roles = _assign_models_to_roles(
            discussion_role_sources,
            config,
            default_model=logical_model,
            task=context.request,
        )
        dispatch_matrix, dispatch_matrix_truncated = _role_model_routing_matrix_payload(
            dispatch_role_sources,
            dispatch_roles,
            config,
            default_model=logical_model,
            task=context.request,
        )
        discussion_matrix, discussion_matrix_truncated = _role_model_routing_matrix_payload(
            discussion_role_sources,
            discussion_roles,
            config,
            default_model=logical_model,
            task=context.request,
        )
        model_routing_matrix = (*dispatch_matrix, *discussion_matrix)
        model_routing_matrix_truncated = dispatch_matrix_truncated or discussion_matrix_truncated
        dispatch_plan = _dispatch_plan(
            dispatch_roles,
            context,
            max_parallelism=_dispatch_parallelism(config, logical_model, dispatch_roles),
            capability_gateway=self._capability_gateway,
        )
        discussion_plan = _discussion_plan(
            discussion_roles,
            logical_model,
            context,
            capability_gateway=self._capability_gateway,
        )
        role_payload = _hybrid_role_payload(dispatch_plan, discussion_plan)
        deployment_constraint = _deployment_routing_constraint(config, context.routing_decision)
        return _PlannedRuntime(
            HybridRuntime(
                CrewDispatchRuntime(
                    gateway,
                    dispatch_plan,
                    capability_gateway=self._capability_gateway,
                    harness_tool_gateway=self._harness_tool_gateway,
                ),
                AutoGenDiscussionRuntime(
                    gateway,
                    discussion_plan,
                    capability_gateway=self._capability_gateway,
                    harness_tool_gateway=self._harness_tool_gateway,
                ),
                DirectRuntime(gateway, logical_model=logical_model),
            ),
            mode=TaskMode.HYBRID,
            main_agent_model=logical_model,
            roles=role_payload,
            steps=(
                *_dispatch_step_payload(dispatch_plan),
                *_discussion_step_payload(discussion_plan),
                {
                    "id": "final_synthesis",
                    "agent": "main_agent",
                    "depends_on": ("discussion",),
                    "final_synthesizer": True,
                    "tools": (),
                },
            ),
            model_routing_matrix=model_routing_matrix,
            model_routing_matrix_truncated=model_routing_matrix_truncated,
            deployment_constraints=_deployment_constraints_payload(
                config,
                main_agent_model=logical_model,
                roles=role_payload,
                constraint=deployment_constraint,
                fallback_policy=fallback_policy,
            ),
            deployment_constraint=deployment_constraint,
            fallback_policy=fallback_policy,
            capability_gateway=self._capability_gateway,
        )


async def _current_platform_config(
    config_service: ConfigService,
    tenant_id: UUID,
) -> PlatformConfig | None:
    current = await config_service.get_current(tenant_id)
    if current is None:
        return None
    config = PlatformConfig.model_validate(current.document)
    if not config.models:
        return None
    return config


async def _gateway_for_config(
    config: PlatformConfig,
    *,
    tenant_id: UUID,
    secret_service: SecretService,
    capacity_factory: CapacityFactory,
    transport: ModelTransport,
    routing_decision: object | None = None,
) -> tuple[ModelGateway, str, FallbackExecutionPolicy]:
    logical_model = _direct_logical_model(config, routing_decision)
    deployments = _deployments(config)
    try:
        fallback_policy = fallback_execution_policy_from_decision(routing_decision)
    except FallbackExecutionPolicyError as error:
        raise HarnessModelSelectionError(str(error)) from error
    deployment_constraint = _deployment_routing_constraint(config, routing_decision)
    deployments = _constrained_deployments_for_harness_decision(
        deployments,
        deployment_constraint,
    )
    gateway = ModelGateway(
        ModelRegistry(deployments),
        await capacity_factory(tenant_id, deployments),
        TenantSecretResolver(secret_service, tenant_id),
        transport,
        fallbacks=(
            {}
            if deployment_constraint is not None or fallback_policy == "disabled"
            else _fallbacks(config)
        ),
        capacity_wait_timeout=60,
    )
    return gateway, logical_model, fallback_policy


def _constrained_deployments_for_harness_decision(
    deployments: tuple[Deployment, ...],
    constraint: DeploymentRoutingConstraint | None,
) -> tuple[Deployment, ...]:
    try:
        return constrain_deployments_for_routing(deployments, constraint)
    except DeploymentRoutingConstraintError as error:
        raise HarnessModelSelectionError(str(error)) from error


def _deployment_routing_constraint(
    config: PlatformConfig,
    routing_decision: object | None,
) -> DeploymentRoutingConstraint | None:
    try:
        return deployment_routing_constraint_from_decision(config, routing_decision)
    except DeploymentRoutingConstraintError as error:
        raise HarnessModelSelectionError(str(error)) from error


def _software_delivery_guidance(context: TaskContext, tools: tuple[str, ...]) -> str:
    if TaskProfile.SOFTWARE not in _task_profiles(context.request):
        return ""
    lines = [
        "Software delivery requirements:",
        "List every file path included in the ZIP and state how each file satisfies the user request.",
        "Do not claim the project works without verification evidence.",
    ]
    if "run_safe_command" in tools:
        lines.append("Run an available safe command smoke test before final packaging.")
    if "project.generate_zip" in tools:
        lines.append(
            "Use project.generate_zip only after verification; set presentation to final_attachment for the user-downloadable ZIP."
        )
    return "\n" + "\n".join(lines) + "\n"


def _software_final_guidance(context: TaskContext) -> str:
    if TaskProfile.SOFTWARE not in _task_profiles(context.request):
        return ""
    return (
        "Do not claim the project works without verification evidence. "
        "Summarize the verified file list, smoke-test result, and any remaining risk. "
    )


def _dispatch_plan(
    roles: tuple[RoleAssignment, ...],
    context: TaskContext,
    *,
    max_parallelism: int = 1,
    capability_gateway: RuntimeCapabilityGatewayProtocol | None = None,
) -> DispatchPlan:
    selected_roles = tuple(roles)
    if not selected_roles:
        selected_roles = (
            RoleAssignment(
                id="planner",
                role="Planner",
                purpose=RolePurpose.PLAN,
                mission="Plan and execute the task safely.",
                must_answer=("What was done?",),
                allowed_tools=(),
                forbidden_actions=("Do not perform dangerous operations.",),
                skills=(),
                output_schema={"summary": "string"},
                model="main",
            ),
        )
    plan_allowed_tools = _plan_allowed_tools(
        selected_roles,
        context,
        capability_gateway=capability_gateway,
    )
    role_tools_by_id = {
        role.id: _role_allowed_tools(
            role,
            context,
            capability_gateway=capability_gateway,
        )
        for role in selected_roles
    }
    agents = [
        AgentSpec(
            id=role.id,
            role=role.role,
            goal=role.mission,
            logical_model=role.model,
            allowed_tools=role_tools_by_id[role.id],
        )
        for role in selected_roles
    ]
    if not any(agent.id == "final_synthesizer" for agent in agents):
        agents.append(
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Merge role outputs into one concise, evidence-aware final answer.",
                logical_model=_dispatch_final_synthesizer_model(context, selected_roles[0].model),
                allowed_tools=(),
            )
    )
    request_text = str(context.request)
    hermes_context = hermes_memory_context_text(context.routing_decision)
    memory_guidance = (
        f"\nHermes+ confirmed memory guidance:\n{hermes_context}\n"
        if hermes_context
        else ""
    )
    step_token_budget = min(context.token_budget, 1_000_000)
    role_token_budget = step_token_budget
    final_token_budget = step_token_budget
    producer_step_timeout = _producer_step_timeout(context, selected_roles)
    post_product_step_timeout = _post_product_step_timeout(context, selected_roles)
    final_step_timeout = _final_step_timeout(
        context,
        selected_roles,
        post_product_step_timeout=post_product_step_timeout,
    )
    producer_step_ids = tuple(
        f"{role.id}_step" for role in selected_roles if not _is_post_product_role(role)
    )
    role_steps = tuple(
        DispatchStep(
            id=f"{role.id}_step",
            agent=role.id,
            task=(
                f"Role mission: {role.mission}\n"
                f"User task: {request_text}\n"
                f"{memory_guidance}"
                f"{_software_delivery_guidance(context, role_tools_by_id[role.id])}"
                "Return only the role-specific result, evidence, risks, and verification."
            ),
            depends_on=producer_step_ids if _is_post_product_role(role) else (),
            tools=role_tools_by_id[role.id],
            token_budget=role_token_budget,
            timeout_seconds=(
                post_product_step_timeout if _is_post_product_role(role) else producer_step_timeout
            ),
            cost_budget_usd=Decimal(0),
        )
        for role in selected_roles
    )
    final_dependencies = tuple(step.id for step in role_steps)
    final_step = DispatchStep(
        id="final_response_step",
        agent="final_synthesizer",
        task=(
            f"Synthesize all role outputs into the final answer for this task: {request_text}. "
            f"{memory_guidance}"
            f"{_software_final_guidance(context)}"
            "Resolve conflicts explicitly and state any user decision required."
        ),
        depends_on=final_dependencies,
        tools=(),
        final_synthesizer=True,
        token_budget=final_token_budget,
        timeout_seconds=final_step_timeout,
        cost_budget_usd=Decimal(0),
    )
    return DispatchPlan(
        agents=tuple(agents),
        steps=(*role_steps, final_step),
        allowed_tools=plan_allowed_tools,
        max_parallelism=max(1, min(max_parallelism, len(role_steps) or 1)),
        total_token_budget=context.token_budget,
        total_timeout_seconds=sum(step.timeout_seconds for step in (*role_steps, final_step)),
        total_cost_usd=Decimal(0),
    )


def _is_post_product_role(role: RoleAssignment) -> bool:
    return role.purpose in {
        RolePurpose.CRITIQUE,
        RolePurpose.RISK_REVIEW,
        RolePurpose.RECORD_DECISION,
        RolePurpose.VERIFY,
        RolePurpose.RELEASE,
    }


def _producer_step_timeout(
    context: TaskContext,
    selected_roles: tuple[RoleAssignment, ...],
) -> float:
    return min(
        max(context.timeout_seconds / max(2, len(selected_roles)), 120.0),
        300.0,
    )


def _post_product_step_timeout(
    context: TaskContext,
    selected_roles: tuple[RoleAssignment, ...],
) -> float:
    producer_timeout = _producer_step_timeout(context, selected_roles)
    request_size_bonus = min(len(str(context.request).encode("utf-8")) / 2048 * 30.0, 120.0)
    role_count_bonus = max(0, len(selected_roles) - 2) * 30.0
    return min(
        max(
            producer_timeout * 1.5,
            context.timeout_seconds * 0.45,
            240.0,
        )
        + request_size_bonus
        + role_count_bonus,
        600.0,
    )


def _final_step_timeout(
    context: TaskContext,
    selected_roles: tuple[RoleAssignment, ...],
    *,
    post_product_step_timeout: float,
) -> float:
    request_size_bonus = min(len(str(context.request).encode("utf-8")) / 2048 * 30.0, 120.0)
    return min(
        max(
            context.timeout_seconds * 0.45,
            post_product_step_timeout * 0.75,
            240.0,
        )
        + request_size_bonus
        + max(0, len(selected_roles) - 3) * 20.0,
        600.0,
    )


def _dispatch_role_payload(plan: DispatchPlan) -> tuple[Mapping[str, JsonValue], ...]:
    step_purposes = {
        step.agent: ("synthesize" if step.final_synthesizer else "execute")
        for step in plan.steps
    }
    return tuple(
        {
            "id": agent.id,
            "role": agent.role,
            "purpose": step_purposes.get(agent.id, "execute"),
            "logical_model": agent.logical_model,
            "tools": agent.allowed_tools,
        }
        for agent in plan.agents
    )


def _dispatch_step_payload(plan: DispatchPlan) -> tuple[Mapping[str, JsonValue], ...]:
    return tuple(
        {
            "id": step.id,
            "agent": step.agent,
            "depends_on": step.depends_on,
            "final_synthesizer": step.final_synthesizer,
            "tools": step.tools,
        }
        for step in plan.steps
    )


def _discussion_role_payload(plan: DiscussionPlan) -> tuple[Mapping[str, JsonValue], ...]:
    return tuple(
        {
            "id": participant.id,
            "role": participant.role,
            "purpose": "expertise",
            "logical_model": participant.logical_model,
            "tools": participant.allowed_tools,
        }
        for participant in plan.participants
    )


def _discussion_step_payload(plan: DiscussionPlan) -> tuple[Mapping[str, JsonValue], ...]:
    return tuple(
        {
            "id": "discussion",
            "agent": participant.id,
            "depends_on": (),
            "final_synthesizer": False,
            "tools": participant.allowed_tools,
        }
        for participant in plan.participants
    )


def _hybrid_role_payload(
    dispatch_plan: DispatchPlan,
    discussion_plan: DiscussionPlan,
) -> tuple[Mapping[str, JsonValue], ...]:
    return (
        *_dispatch_role_payload(dispatch_plan),
        *_discussion_role_payload(discussion_plan),
    )


def _role_allowed_tools(
    role: RoleAssignment,
    context: TaskContext | None,
    *,
    capability_gateway: RuntimeCapabilityGatewayProtocol | None,
) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys((*role.allowed_tools, *role.skills)))
    if context is None or capability_gateway is None:
        return ()
    requested = tuple(
        dict.fromkeys(
            (
                *requested,
                *_available_inventory_tools_for_role(
                    role,
                    context,
                    requested=requested,
                    capability_gateway=capability_gateway,
                ),
            )
        )
    )
    if not requested:
        return ()
    is_available = getattr(capability_gateway, "is_available", None)
    filtered: list[str] = []
    for name in requested:
        if _is_replay_safe_capability(name, capability_gateway=capability_gateway):
            filtered.append(name)
            continue
        if callable(is_available) and is_available(context.tenant_id, name):
            filtered.append(name)
    return tuple(dict.fromkeys(filtered))


def _available_inventory_tools_for_role(
    role: RoleAssignment,
    context: TaskContext,
    *,
    requested: tuple[str, ...],
    capability_gateway: RuntimeCapabilityGatewayProtocol,
) -> tuple[str, ...]:
    inventory = _capability_inventory_payload(
        context.tenant_id,
        capability_gateway=capability_gateway,
    )
    if inventory is None:
        return ()
    raw_items = inventory.get("items")
    if not isinstance(raw_items, tuple | list):
        return ()
    requested_tokens = {item.casefold() for item in requested}
    match_text = _role_capability_match_text(role, context)
    tools: list[str] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        if item.get("kind") not in {"mcp", "plugin"} or item.get("available") is not True:
            continue
        tool_id = item.get("id")
        if not isinstance(tool_id, str) or not _is_safe_inventory_token(tool_id, max_length=128):
            continue
        aliases = item.get("aliases")
        candidates = (tool_id, *_tool_names(aliases))
        if any(
            candidate.casefold() in requested_tokens
            or _capability_token_mentioned(match_text, candidate)
            for candidate in candidates
        ):
            tools.append(tool_id)
    return tuple(dict.fromkeys(tools))


def _role_capability_match_text(role: RoleAssignment, context: TaskContext) -> str:
    return "\n".join(
        (
            str(context.request),
            role.id,
            role.role,
            role.mission,
            " ".join(role.allowed_tools),
            " ".join(role.skills),
        )
    ).casefold()


def _capability_token_mentioned(text: str, token: str) -> bool:
    if not _is_safe_inventory_token(token, max_length=128):
        return False
    pattern = rf"(?<![a-z0-9_.-]){re.escape(token.casefold())}(?![a-z0-9_.-])"
    return re.search(pattern, text) is not None


def _plan_allowed_tools(
    roles: tuple[RoleAssignment, ...],
    context: TaskContext,
    *,
    capability_gateway: RuntimeCapabilityGatewayProtocol | None,
) -> tuple[str, ...]:
    tools: list[str] = []
    for role in roles:
        tools.extend(
            _role_allowed_tools(
                role,
                context,
                capability_gateway=capability_gateway,
            )
        )
    return tuple(dict.fromkeys(tools))


async def _prepare_capability_gateway_for_tenant(
    tenant_id: UUID,
    *,
    capability_gateway: RuntimeCapabilityGatewayProtocol | None,
) -> None:
    if capability_gateway is None:
        return
    ensure_tenant_loaded = getattr(capability_gateway, "ensure_tenant_loaded", None)
    if not callable(ensure_tenant_loaded):
        return
    try:
        await ensure_tenant_loaded(tenant_id)
    except Exception:  # noqa: BLE001 - capability inventory remains optional planning context.
        return


def _selected_config_role_assignments(
    context: TaskContext,
    config: PlatformConfig,
    *,
    purpose: RolePurpose,
    output_schema: Mapping[str, str],
) -> tuple[RoleAssignment, ...]:
    raw_ids = context.routing_decision.get("selected_agent_ids")
    if not isinstance(raw_ids, (list, tuple)):
        return ()
    requested_ids = tuple(item for item in raw_ids if isinstance(item, str) and item)
    if not requested_ids:
        return ()
    agents_by_id = {agent.id: agent for agent in config.agents}
    assignments: list[RoleAssignment] = []
    for agent_id in requested_ids:
        agent = agents_by_id.get(agent_id)
        if agent is None:
            continue
        role_purpose = _selected_config_agent_purpose(agent, default=purpose)
        assignments.append(
            RoleAssignment(
                id=agent.id,
                role=agent.role,
                purpose=role_purpose,
                mission=agent.prompt,
                must_answer=("What did this agent contribute and what evidence supports it?",),
                allowed_tools=(),
                forbidden_actions=("Do not perform dangerous operations without approval.",),
                skills=tuple(agent.skills),
                output_schema=output_schema,
                model=agent.model,
            )
        )
    return tuple(assignments)


def _selected_config_agent_purpose(
    agent: AgentDefinition,
    *,
    default: RolePurpose,
) -> RolePurpose:
    if default is not RolePurpose.EXECUTE:
        return default
    text = f"{agent.id} {agent.role} {agent.prompt}".casefold()
    if any(keyword in text for keyword in ("合规", "法律", "隐私", "版权", "compliance")):
        return RolePurpose.RISK_REVIEW
    if any(
        keyword in text
        for keyword in (
            "review",
            "reviewer",
            "审查",
            "审核",
            "复核",
            "评审",
            "质量",
            "验收",
            "检查",
            "校验",
            "risk",
            "风险",
        )
    ):
        return RolePurpose.VERIFY
    if any(keyword in text for keyword in ("裁决", "决策", "decision", "record")):
        return RolePurpose.RECORD_DECISION
    return default

def _temporary_role_assignments(
    context: TaskContext,
    logical_model: str,
) -> tuple[RoleAssignment, ...]:
    if context.routing_decision.get("temporary_agent_approved") is not True:
        return ()
    raw_agents = context.routing_decision.get("temporary_agents")
    if not isinstance(raw_agents, (list, tuple)):
        return ()
    assignments: list[RoleAssignment] = []
    for raw in raw_agents[:4]:
        if not isinstance(raw, dict):
            continue
        identifier = raw.get("id")
        role = raw.get("role") or raw.get("name")
        mission = raw.get("prompt") or raw.get("reason")
        selected_model = raw.get("model")
        if not isinstance(identifier, str) or not isinstance(role, str) or not isinstance(mission, str):
            continue
        logical_model_for_agent = selected_model if isinstance(selected_model, str) and selected_model else identifier
        skills = raw.get("suggested_skills")
        skill_tuple = tuple(item for item in skills if isinstance(item, str)) if isinstance(skills, (list, tuple)) else ()
        try:
            assignments.append(
                RoleAssignment(
                    id=identifier,
                    role=role,
                    purpose=RolePurpose.EXECUTE,
                    mission=mission,
                    must_answer=("What did this temporary agent contribute?",),
                    allowed_tools=(),
                    forbidden_actions=("Do not perform dangerous operations without approval.",),
                    skills=skill_tuple,
                    output_schema={
                        "status": "done | blocked | needs_user",
                        "summary": "string",
                        "evidence": "string[]",
                    },
                    model=logical_model_for_agent,
                )
            )
        except ValueError:
            continue
    return tuple(assignments)


def _assign_models_to_roles(
    roles: tuple[RoleAssignment, ...],
    config: PlatformConfig,
    *,
    default_model: str,
    task: object,
) -> tuple[RoleAssignment, ...]:
    assigned_counts: dict[str, int] = {}
    capacities = {
        logical_model: _logical_model_capacity(config, logical_model)
        for logical_model in config.models
    }
    assigned: list[RoleAssignment] = []
    for role in roles:
        ranked = _rank_logical_models_for_role(
            role,
            config,
            default_model=default_model,
            task=task,
        )
        selected = default_model
        if ranked:
            selected = max(
                ranked,
                key=lambda item: _capacity_adjusted_model_score(
                    item,
                    assigned_counts=assigned_counts,
                    capacities=capacities,
                ),
            )[2]
        assigned_counts[selected] = assigned_counts.get(selected, 0) + 1
        assigned.append(replace(role, model=selected))
    return tuple(assigned)


def _capacity_adjusted_model_score(
    ranked_item: tuple[int, int, str],
    *,
    assigned_counts: Mapping[str, int],
    capacities: Mapping[str, int],
) -> tuple[int, int, int, str]:
    score, length_tiebreaker, logical_model = ranked_item
    used = assigned_counts.get(logical_model, 0)
    capacity = max(1, capacities.get(logical_model, 1))
    unused_bonus = 10 if used == 0 else 0
    adjusted = score + unused_bonus - min(used, capacity) * 6
    if used >= capacity:
        adjusted -= (used - capacity + 1) * 24
    return adjusted, -used, length_tiebreaker, logical_model


def _logical_model_capacity(config: PlatformConfig, logical_model: str) -> int:
    definition = config.models.get(logical_model)
    if definition is None:
        return 1
    slots = sum(
        safe_operational_limit(
            deployment.max_concurrency,
            deployment.target_utilization,
            deployment.reserved_slots,
        )
        for deployment in definition.deployments
    )
    return max(1, slots)


def _string_or_default(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _fallback_policy_label(
    constraint: DeploymentRoutingConstraint | None,
    fallback_policy: FallbackExecutionPolicy,
) -> Literal["configured", "disabled_by_harness_policy", "disabled_for_harness_selection"]:
    if constraint is not None:
        return "disabled_for_harness_selection"
    if fallback_policy == "disabled":
        return "disabled_by_harness_policy"
    return "configured"


def _scheduler_selection_payload(
    context: TaskContext,
    *,
    main_agent_model: str,
) -> Mapping[str, JsonValue]:
    explicit_main = context.routing_decision.get("main_agent_model")
    harness_decision = context.routing_decision.get("harness_decision")
    source = (
        "harness_decision"
        if isinstance(harness_decision, Mapping)
        else "main_agent_model"
        if isinstance(explicit_main, str) and explicit_main
        else "runtime_default"
    )
    selected_logical_model: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    requires_approval = False
    if isinstance(harness_decision, Mapping):
        selected_logical_model = _optional_selection_id(
            harness_decision.get("selected_logical_model")
        )
        selected_provider = _optional_selection_id(harness_decision.get("selected_provider"))
        selected_model = _optional_model_selection_text(harness_decision.get("selected_model"))
        requires_approval = harness_decision.get("requires_approval") is True
    elif isinstance(explicit_main, str) and explicit_main:
        selected_logical_model = _optional_selection_id(explicit_main)
    return {
        "schema_version": 1,
        "source": source,
        "selected_logical_model": selected_logical_model,
        "selected_provider": selected_provider,
        "selected_model": selected_model,
        "applies_to_main_agent": selected_logical_model == main_agent_model,
        "requires_approval": requires_approval,
    }


def _gateway_execution_policy_payload(
    *,
    deployment_constraint: DeploymentRoutingConstraint | None,
    fallback_policy: FallbackExecutionPolicy,
) -> Mapping[str, JsonValue]:
    return {
        "schema_version": 1,
        "capacity_boundary": "model_gateway",
        "deployment_constraint_applied": deployment_constraint is not None,
        "constrained_logical_model": (
            deployment_constraint.logical_model
            if deployment_constraint is not None
            else None
        ),
        "fallback_policy": _fallback_policy_label(deployment_constraint, fallback_policy),
        "fallback_mappings_enabled": (
            deployment_constraint is None and fallback_policy == "configured"
        ),
    }


def _optional_selection_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.casefold()
    if not _is_safe_inventory_token(normalized, max_length=128):
        return None
    return normalized


def _optional_model_selection_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (
        not text
        or len(text) > 128
        or _SAFE_MODEL_SELECTION_TEXT.fullmatch(text) is None
    ):
        return None
    normalized = text.casefold()
    if any(part in normalized for part in _SENSITIVE_CAPABILITY_INVENTORY_TEXT):
        return None
    return text


def _deployment_constraints_payload(
    config: PlatformConfig,
    *,
    main_agent_model: str,
    roles: tuple[Mapping[str, JsonValue], ...],
    constraint: DeploymentRoutingConstraint | None,
    fallback_policy: FallbackExecutionPolicy = "configured",
) -> Mapping[str, JsonValue]:
    logical_models = _logical_models_for_deployment_constraints(
        main_agent_model=main_agent_model,
        roles=roles,
    )
    items: list[Mapping[str, JsonValue]] = []
    for logical_model in logical_models:
        definition = config.models.get(logical_model)
        if definition is None:
            continue
        harness_constrained = (
            constraint is not None and constraint.logical_model == logical_model
        )
        total_deployments = len(definition.deployments)
        eligible_deployments = sum(
            1
            for deployment in definition.deployments
            if not harness_constrained
            or (
                constraint is not None
                and _deployment_definition_matches_constraint(
                    logical_model,
                    deployment,
                    constraint,
                )
            )
        )
        items.append(
            {
                "logical_model": logical_model,
                "total_deployments": total_deployments,
                "eligible_deployments": eligible_deployments,
                "harness_constrained": harness_constrained,
                "selected_provider": (
                    constraint.provider if harness_constrained and constraint else None
                ),
                "selected_model": (
                    constraint.model if harness_constrained and constraint else None
                ),
                "fallback_policy": _fallback_policy_label(constraint, fallback_policy),
            }
        )
    return {
        "schema_version": 1,
        "items": tuple(items),
    }


def _logical_models_for_deployment_constraints(
    *,
    main_agent_model: str,
    roles: tuple[Mapping[str, JsonValue], ...],
) -> tuple[str, ...]:
    logical_models: list[str] = []
    for role in roles:
        logical_model = role.get("logical_model")
        if isinstance(logical_model, str) and logical_model:
            logical_models.append(logical_model)
    logical_models.append(main_agent_model)
    return tuple(dict.fromkeys(logical_models))


def _deployment_definition_matches_constraint(
    logical_model: str,
    deployment: object,
    constraint: DeploymentRoutingConstraint,
) -> bool:
    provider = getattr(deployment, "provider", None)
    model = getattr(deployment, "model", None)
    if not isinstance(provider, str) or not isinstance(model, str):
        return False
    return constraint.matches_provider_model(
        logical_model=logical_model,
        provider=provider,
        model=model,
    )


def _model_execution_plan_payload(
    context: TaskContext,
    *,
    main_agent_model: str,
    roles: tuple[Mapping[str, JsonValue], ...],
    steps: tuple[Mapping[str, JsonValue], ...] = (),
    model_routing_matrix: tuple[Mapping[str, JsonValue], ...] = (),
    model_routing_matrix_truncated: bool = False,
    deployment_constraints: Mapping[str, JsonValue] | None = None,
    deployment_constraint: DeploymentRoutingConstraint | None = None,
    fallback_policy: FallbackExecutionPolicy = "configured",
) -> Mapping[str, JsonValue]:
    explicit_main = context.routing_decision.get("main_agent_model")
    main_agent_constraint = (
        deployment_constraint
        if deployment_constraint is not None
        and deployment_constraint.logical_model == main_agent_model
        else None
    )
    selection_source = (
        "main_agent_model"
        if isinstance(explicit_main, str) and explicit_main
        else "harness_decision"
        if main_agent_constraint is not None
        else "runtime_default"
    )
    selected_provider: str | None = None
    selected_model: str | None = None
    if main_agent_constraint is not None:
        selected_provider = main_agent_constraint.provider
        selected_model = main_agent_constraint.model
    payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "main_agent": {
            "logical_model": main_agent_model,
            "selection_source": selection_source,
            "harness_constrained": main_agent_constraint is not None,
            "selected_provider": selected_provider,
            "selected_model": selected_model,
            "fallback_policy": _fallback_policy_label(deployment_constraint, fallback_policy),
        },
        "scheduler_selection": _scheduler_selection_payload(
            context,
            main_agent_model=main_agent_model,
        ),
        "gateway_execution_policy": _gateway_execution_policy_payload(
            deployment_constraint=deployment_constraint,
            fallback_policy=fallback_policy,
        ),
        "role_model_assignments": tuple(
            {
                "role_id": str(role["id"]),
                "purpose": str(role["purpose"]),
                "logical_model": str(role["logical_model"]),
            }
            for role in roles
            if "id" in role and "purpose" in role and "logical_model" in role
        ),
        "orchestration_handoffs": _orchestration_handoffs_payload(
            roles=roles,
            steps=steps,
        ),
        "orchestration_contracts": _orchestration_contracts_payload(
            roles=roles,
            steps=steps,
        ),
        "orchestration_protocol": _orchestration_protocol_payload(
            context,
            roles=roles,
            steps=steps,
        ),
        "model_capability_negotiation": _model_capability_negotiation_payload(
            roles=roles,
            model_routing_matrix=model_routing_matrix,
            model_routing_matrix_truncated=model_routing_matrix_truncated,
        ),
        "role_model_routing_matrix": model_routing_matrix,
        "role_model_routing_matrix_truncated": model_routing_matrix_truncated,
    }
    if deployment_constraints is not None:
        payload["deployment_constraints"] = deployment_constraints
    return payload


def _orchestration_handoffs_payload(
    *,
    roles: tuple[Mapping[str, JsonValue], ...],
    steps: tuple[Mapping[str, JsonValue], ...],
) -> Mapping[str, JsonValue]:
    items, truncated = _orchestration_handoff_items(roles=roles, steps=steps)
    return {
        "schema_version": 1,
        "items": tuple(items),
        "truncated": truncated,
    }


def _orchestration_contracts_payload(
    *,
    roles: tuple[Mapping[str, JsonValue], ...],
    steps: tuple[Mapping[str, JsonValue], ...],
) -> Mapping[str, JsonValue]:
    handoffs, truncated = _orchestration_handoff_items(roles=roles, steps=steps)
    items: list[Mapping[str, JsonValue]] = [
        {
            "contract_id": f"{handoff['source_step_id']}-to-{handoff['target_step_id']}",
            "source_step_id": handoff["source_step_id"],
            "target_step_id": handoff["target_step_id"],
            "source_role_id": handoff["source_role_id"],
            "target_role_id": handoff["target_role_id"],
            "handoff_kind": handoff["handoff_kind"],
            "status": "planned",
            "required_output_fields": tuple(_DISPATCH_OUTPUT_SCHEMA),
            "ready_status": _ORCHESTRATION_CONTRACT_READY_STATUS,
            "blocking_statuses": _ORCHESTRATION_CONTRACT_BLOCKING_STATUSES,
            "recovery_hint": _ORCHESTRATION_CONTRACT_RECOVERY_HINT,
        }
        for handoff in handoffs
    ]
    return {
        "schema_version": 1,
        "items": tuple(items),
        "truncated": truncated,
    }


def _orchestration_protocol_payload(
    context: TaskContext,
    *,
    roles: tuple[Mapping[str, JsonValue], ...],
    steps: tuple[Mapping[str, JsonValue], ...],
) -> Mapping[str, JsonValue]:
    handoffs, truncated = _orchestration_handoff_items(roles=roles, steps=steps)
    role_ids = {
        role_id
        for handoff in handoffs
        for role_id in (
            handoff["source_role_id"],
            handoff["target_role_id"],
        )
    }
    recovery_hints = (
        (_ORCHESTRATION_CONTRACT_RECOVERY_HINT,)
        if handoffs
        else ()
    )
    return {
        "schema_version": 1,
        "protocol": _ORCHESTRATION_PROTOCOL_ID,
        "mode": context.mode.value,
        "role_count": len(role_ids),
        "handoff_count": len(handoffs),
        "contract_count": len(handoffs),
        "structured_output_schema": _DISPATCH_OUTPUT_SCHEMA_ID,
        "required_output_fields": tuple(_DISPATCH_OUTPUT_SCHEMA),
        "ready_status": _ORCHESTRATION_CONTRACT_READY_STATUS,
        "blocking_statuses": _ORCHESTRATION_CONTRACT_BLOCKING_STATUSES,
        "recovery_hints": recovery_hints,
        "truncated": truncated,
    }


def _model_capability_negotiation_payload(
    *,
    roles: tuple[Mapping[str, JsonValue], ...],
    model_routing_matrix: tuple[Mapping[str, JsonValue], ...],
    model_routing_matrix_truncated: bool,
) -> Mapping[str, JsonValue]:
    selected_capabilities = _selected_model_capabilities_by_role(model_routing_matrix)
    items: list[Mapping[str, JsonValue]] = []
    truncated = model_routing_matrix_truncated
    for role in roles:
        role_id = _optional_orchestration_handoff_token(role.get("id"), max_length=128)
        logical_model = _optional_orchestration_handoff_token(
            role.get("logical_model"),
            max_length=128,
        )
        if role_id is None or logical_model is None:
            continue
        if len(items) >= _MAX_MODEL_ROUTING_MATRIX_ROLES:
            truncated = True
            break
        required = _required_model_capabilities_for_role(role)
        selected = selected_capabilities.get(role_id)
        matched = tuple(capability for capability in required if selected and capability in selected)
        missing = (
            tuple(capability for capability in required if capability not in selected)
            if selected is not None
            else ()
        )
        status = (
            "unknown"
            if selected is None
            else "satisfied"
            if not missing
            else "missing_capability"
        )
        items.append(
            {
                "role_id": role_id,
                "logical_model": logical_model,
                "required_capabilities": tuple(capability.value for capability in required),
                "matched_capabilities": tuple(capability.value for capability in matched),
                "missing_capabilities": tuple(capability.value for capability in missing),
                "status": status,
            }
        )
    return {
        "schema_version": 1,
        "items": tuple(items),
        "role_count": len(items),
        "satisfied_count": sum(1 for item in items if item["status"] == "satisfied"),
        "missing_count": sum(1 for item in items if item["status"] == "missing_capability"),
        "unknown_count": sum(1 for item in items if item["status"] == "unknown"),
        "truncated": truncated,
    }


def _required_model_capabilities_for_role(
    role: Mapping[str, JsonValue],
) -> tuple[ModelCapability, ...]:
    required = [ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT]
    tools = role.get("tools")
    if isinstance(tools, tuple | list) and tools:
        required.append(ModelCapability.TOOL_CALLING)
    return tuple(dict.fromkeys(required))


def _selected_model_capabilities_by_role(
    model_routing_matrix: tuple[Mapping[str, JsonValue], ...],
) -> Mapping[str, frozenset[ModelCapability]]:
    capabilities_by_role: dict[str, frozenset[ModelCapability]] = {}
    for entry in model_routing_matrix:
        role_id = _optional_orchestration_handoff_token(entry.get("role_id"), max_length=128)
        if role_id is None:
            continue
        selected_candidate = _selected_model_routing_candidate(entry)
        if selected_candidate is None:
            continue
        capabilities_by_role[role_id] = _safe_model_capabilities_from_candidate(
            selected_candidate,
        )
    return capabilities_by_role


def _selected_model_routing_candidate(
    entry: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue] | None:
    candidates = entry.get("candidates")
    if not isinstance(candidates, tuple | list):
        return None
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        logical_model = _optional_orchestration_handoff_token(
            candidate.get("logical_model"),
            max_length=128,
        )
        if logical_model is None:
            continue
        if candidate.get("selected") is True:
            return candidate
    return None


def _safe_model_capabilities_from_candidate(
    candidate: Mapping[str, JsonValue],
) -> frozenset[ModelCapability]:
    capabilities: set[ModelCapability] = set()
    for token in _candidate_capability_tokens(candidate.get("traits")):
        capability = _MODEL_CAPABILITY_TRAIT_ALIASES.get(token)
        if capability is not None:
            capabilities.add(capability)
    for reason in _candidate_capability_tokens(candidate.get("reasons")):
        if reason == "capability:tool_role_supported":
            capabilities.add(ModelCapability.TOOL_CALLING)
    return frozenset(capabilities)


def _candidate_capability_tokens(value: JsonValue | object) -> tuple[str, ...]:
    if not isinstance(value, tuple | list):
        return ()
    tokens: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        token = item.strip().casefold()
        if _optional_orchestration_handoff_token(token, max_length=128) is None:
            continue
        tokens.append(token)
    return tuple(tokens)


def _orchestration_handoff_items(
    *,
    roles: tuple[Mapping[str, JsonValue], ...],
    steps: tuple[Mapping[str, JsonValue], ...],
) -> tuple[list[Mapping[str, JsonValue]], bool]:
    safe_roles = {
        role_id: role
        for role in roles
        if (role_id := _optional_orchestration_handoff_token(role.get("id"), max_length=128))
        is not None
        and _optional_orchestration_handoff_token(role.get("purpose"), max_length=64)
        is not None
        and _optional_orchestration_handoff_token(role.get("logical_model"), max_length=128)
        is not None
    }
    safe_steps = {
        step_id: step
        for step in steps
        if (step_id := _optional_orchestration_handoff_token(step.get("id"), max_length=128))
        is not None
        and _optional_orchestration_handoff_token(step.get("agent"), max_length=128)
        in safe_roles
    }
    items: list[Mapping[str, JsonValue]] = []
    truncated = False
    for target_step_id, target_step in safe_steps.items():
        target_role_id = _optional_orchestration_handoff_token(
            target_step.get("agent"),
            max_length=128,
        )
        if target_role_id is None:
            continue
        target_role = safe_roles[target_role_id]
        depends_on = target_step.get("depends_on")
        if not isinstance(depends_on, tuple | list):
            continue
        for raw_source_step_id in depends_on:
            source_step_id = _optional_orchestration_handoff_token(
                raw_source_step_id,
                max_length=128,
            )
            if source_step_id is None or source_step_id not in safe_steps:
                continue
            source_step = safe_steps[source_step_id]
            source_role_id = _optional_orchestration_handoff_token(
                source_step.get("agent"),
                max_length=128,
            )
            if source_role_id is None or source_role_id not in safe_roles:
                continue
            source_role = safe_roles[source_role_id]
            source_purpose = _optional_orchestration_handoff_token(
                source_role.get("purpose"),
                max_length=64,
            )
            target_purpose = _optional_orchestration_handoff_token(
                target_role.get("purpose"),
                max_length=64,
            )
            source_logical_model = _optional_orchestration_handoff_token(
                source_role.get("logical_model"),
                max_length=128,
            )
            target_logical_model = _optional_orchestration_handoff_token(
                target_role.get("logical_model"),
                max_length=128,
            )
            if (
                source_purpose is None
                or target_purpose is None
                or source_logical_model is None
                or target_logical_model is None
            ):
                continue
            if len(items) >= _MAX_ORCHESTRATION_HANDOFFS:
                truncated = True
                return items, truncated
            items.append(
                {
                    "source_step_id": source_step_id,
                    "target_step_id": target_step_id,
                    "source_role_id": source_role_id,
                    "target_role_id": target_role_id,
                    "source_purpose": source_purpose,
                    "target_purpose": target_purpose,
                    "source_logical_model": source_logical_model,
                    "target_logical_model": target_logical_model,
                    "handoff_kind": "step_dependency",
                }
            )
    return items, truncated


def _optional_orchestration_handoff_token(value: object, *, max_length: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    normalized = value.casefold()
    if any(part in normalized for part in _SENSITIVE_ORCHESTRATION_HANDOFF_TEXT):
        return None
    if _SAFE_CAPABILITY_INVENTORY_ID.fullmatch(value) is None:
        return None
    return value


def _role_model_routing_matrix_payload(
    source_roles: tuple[RoleAssignment, ...],
    assigned_roles: tuple[RoleAssignment, ...],
    config: PlatformConfig,
    *,
    default_model: str,
    task: object,
) -> tuple[tuple[Mapping[str, JsonValue], ...], bool]:
    payload: list[Mapping[str, JsonValue]] = []
    truncated = len(source_roles) > _MAX_MODEL_ROUTING_MATRIX_ROLES
    capacities = {
        logical_model: _logical_model_capacity(config, logical_model)
        for logical_model in config.models
    }
    assigned_counts: dict[str, int] = {}
    for index, role in enumerate(source_roles[:_MAX_MODEL_ROUTING_MATRIX_ROLES]):
        ranked = rank_role_models(
            RoleModelRoutingRequest(
                task=task,
                role_id=role.id,
                role=role.role,
                purpose=role.purpose.value,
                mission=role.mission,
                skills=role.skills,
                must_answer=role.must_answer,
                allowed_tools=role.allowed_tools,
                preferred_model=role.model,
                default_model=default_model,
            ),
            config,
        )
        assigned_role = assigned_roles[index] if index < len(assigned_roles) else None
        selected = assigned_role.model if assigned_role is not None else default_model
        selected_candidate = next(
            (candidate for candidate in ranked if candidate.logical_model == selected),
            None,
        )
        adjusted_score: int | None = None
        if selected_candidate is not None:
            adjusted_score = _capacity_adjusted_model_score(
                (
                    selected_candidate.score,
                    -len(selected_candidate.logical_model),
                    selected_candidate.logical_model,
                ),
                assigned_counts=assigned_counts,
                capacities=capacities,
            )[0]
        candidate_count = len(ranked)
        ranked_top = ranked[0].logical_model if ranked else ""
        reasons = (
            selected_candidate.reasons
            if selected_candidate is not None
            else ("fallback:default_model",)
        )
        if selected_candidate is not None and ranked_top and ranked_top != selected:
            reasons = (*reasons, "capacity_adjustment:selected_after_balance")
        traits = selected_candidate.traits if selected_candidate is not None else frozenset()
        selected_payload: Mapping[str, JsonValue] = {
            "logical_model": selected,
            "score": selected_candidate.score if selected_candidate is not None else 0,
            "adjusted_score": adjusted_score if adjusted_score is not None else 0,
            "eligible": selected_candidate.eligible if selected_candidate is not None else False,
            "selected": True,
            "traits": tuple(sorted(traits))[:_MAX_MODEL_ROUTING_MATRIX_TRAITS],
            "reasons": reasons[:_MAX_MODEL_ROUTING_MATRIX_REASONS],
        }
        payload.append(
            {
                "role_id": role.id,
                "purpose": role.purpose.value,
                "selected_logical_model": selected,
                "candidate_count": candidate_count,
                "truncated_candidates": candidate_count > 1,
                "candidates": (selected_payload,),
            }
        )
        assigned_counts[selected] = assigned_counts.get(selected, 0) + 1
    return tuple(payload), truncated


def _capability_execution_plan_payload(
    roles: tuple[Mapping[str, JsonValue], ...],
    *,
    tenant_id: UUID | None = None,
    capability_gateway: RuntimeCapabilityGatewayProtocol | None,
) -> Mapping[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "permission_boundary": "runtime_capability_gateway",
        "role_capability_assignments": tuple(
            {
                "role_id": str(role["id"]),
                "capabilities": tuple(
                    _capability_plan_item(tool, capability_gateway=capability_gateway)
                    for tool in _tool_names(role.get("tools"))
                ),
            }
            for role in roles
            if "id" in role
        ),
    }
    capability_inventory = _capability_inventory_payload(
        tenant_id,
        capability_gateway=capability_gateway,
    )
    if capability_inventory is not None:
        payload["capability_inventory"] = capability_inventory
    return payload


def _tool_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple | list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _capability_plan_item(
    name: str,
    *,
    capability_gateway: RuntimeCapabilityGatewayProtocol | None,
) -> Mapping[str, JsonValue]:
    replay_safe = _is_replay_safe_capability(name, capability_gateway=capability_gateway)
    return {
        "name": name,
        "replay_safe": replay_safe,
        "approval_policy": "not_required" if replay_safe else "runtime_policy",
    }


def _capability_inventory_payload(
    tenant_id: UUID | None,
    *,
    capability_gateway: RuntimeCapabilityGatewayProtocol | None,
) -> Mapping[str, JsonValue] | None:
    if tenant_id is None or capability_gateway is None:
        return None
    manifest_provider = getattr(capability_gateway, "capability_manifest", None)
    if not callable(manifest_provider):
        return None
    try:
        manifest = manifest_provider(tenant_id)
    except Exception:  # noqa: BLE001 - capability inventory is optional planning context.
        return None
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
        return None
    raw_items = manifest.get("capabilities")
    if not isinstance(raw_items, tuple | list):
        return None
    items: list[Mapping[str, JsonValue]] = []
    truncated = False
    for index, raw_item in enumerate(raw_items):
        if (
            index >= _MAX_CAPABILITY_INVENTORY_SCAN_ITEMS
            or len(items) >= _MAX_CAPABILITY_INVENTORY_ITEMS
        ):
            truncated = True
            break
        item = _capability_inventory_item(raw_item)
        if item is not None:
            items.append(item)
    return {
        "schema_version": 1,
        "items": tuple(items),
        "truncated": truncated,
    }


def _capability_inventory_item(raw_item: object) -> Mapping[str, JsonValue] | None:
    if not isinstance(raw_item, Mapping):
        return None
    item_id = raw_item.get("id")
    if (
        not isinstance(item_id, str)
        or _SAFE_CAPABILITY_INVENTORY_ID.fullmatch(item_id) is None
    ):
        return None
    available = raw_item.get("available")
    availability_reason = raw_item.get("availability_reason")
    aliases = tuple(
        alias
        for alias in _tool_names(raw_item.get("aliases"))
        if _is_safe_inventory_token(alias, max_length=128)
    )[:_MAX_CAPABILITY_INVENTORY_ALIASES]
    return {
        "id": item_id,
        "kind": _inventory_token(raw_item.get("kind"), "unknown", max_length=64),
        "adapter": _inventory_token(raw_item.get("adapter"), "unknown", max_length=128),
        "permission_class": _inventory_token(
            raw_item.get("permission_class"),
            "unknown",
            max_length=128,
        ),
        "sandbox_profile": _inventory_token(
            raw_item.get("sandbox_profile"),
            "unknown",
            max_length=128,
        ),
        "policy_effect": _inventory_token(
            raw_item.get("policy_effect"),
            "inherit",
            max_length=32,
        ),
        "available": available if isinstance(available, bool) else False,
        "availability_reason": _optional_inventory_token(
            availability_reason,
            max_length=128,
        ),
        "replay_safe": raw_item.get("replay_safe") is True,
        "aliases": aliases,
    }


def _inventory_token(value: object, default: str, *, max_length: int) -> str:
    if not _is_safe_inventory_token(value, max_length=max_length):
        return default
    return cast(str, value)


def _optional_inventory_token(value: object, *, max_length: int) -> str | None:
    if not _is_safe_inventory_token(value, max_length=max_length):
        return None
    return cast(str, value)


def _is_safe_inventory_token(value: object, *, max_length: int) -> bool:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return False
    normalized = value.casefold()
    if any(part in normalized for part in _SENSITIVE_CAPABILITY_INVENTORY_TEXT):
        return False
    return _SAFE_CAPABILITY_INVENTORY_ID.fullmatch(value) is not None


def _is_replay_safe_capability(
    name: str,
    *,
    capability_gateway: RuntimeCapabilityGatewayProtocol | None,
) -> bool:
    if capability_gateway is None:
        return False
    try:
        return capability_gateway.is_replay_safe(name) is True
    except (LookupError, RuntimeError, TypeError, ValueError):
        return False


def _dispatch_final_synthesizer_model(context: TaskContext, fallback: str) -> str:
    return _string_or_default(
        context.routing_decision.get("main_agent_model"),
        _string_or_default(_harness_selected_logical_model(context.routing_decision), fallback),
    )


def _harness_selected_logical_model(routing_decision: object | None) -> str | None:
    if not isinstance(routing_decision, Mapping):
        return None
    harness_decision = routing_decision.get("harness_decision")
    if not isinstance(harness_decision, Mapping):
        return None
    selected = harness_decision.get("selected_logical_model")
    return selected if isinstance(selected, str) and selected else None


def _select_logical_model_for_role(
    role: RoleAssignment,
    config: PlatformConfig,
    *,
    default_model: str,
    task: object,
) -> str:
    ranked = _rank_logical_models_for_role(
        role,
        config,
        default_model=default_model,
        task=task,
    )
    return ranked[0][2] if ranked else default_model


def _rank_logical_models_for_role(
    role: RoleAssignment,
    config: PlatformConfig,
    *,
    default_model: str,
    task: object,
) -> list[tuple[int, int, str]]:
    ranked = rank_role_models(
        RoleModelRoutingRequest(
            task=task,
            role_id=role.id,
            role=role.role,
            purpose=role.purpose.value,
            mission=role.mission,
            skills=role.skills,
            must_answer=role.must_answer,
            allowed_tools=role.allowed_tools,
            preferred_model=role.model,
            default_model=default_model,
        ),
        config,
    )
    return [
        (candidate.score, -len(candidate.logical_model), candidate.logical_model)
        for candidate in ranked
    ]


def _logical_model_supports_tool_roles(definition: LogicalModelDefinition) -> bool:
    return any(
        "tool_calling" in {str(capability).lower() for capability in deployment.capabilities}
        and not _is_messages_endpoint_api_base(deployment.api_base)
        for deployment in definition.deployments
    )


def _is_messages_endpoint_api_base(api_base: str | None) -> bool:
    if api_base is None:
        return False
    return urlsplit(api_base).path.rstrip("/").endswith("/messages")

def _model_characteristics(
    logical_model: str,
    definition: LogicalModelDefinition,
) -> frozenset[str]:
    return infer_model_traits(
        logical_model=logical_model,
        deployments=(
            (deployment.provider, deployment.model, deployment.capabilities)
            for deployment in definition.deployments
        ),
    )


def _task_characteristics(text: str) -> frozenset[str]:
    return routing_task_characteristics(text)


def _task_characteristic_score(text: str, characteristics: frozenset[str]) -> int:
    task_characteristics = _task_characteristics(text)
    score = 0
    if "audio" in task_characteristics:
        score += 36 if "audio" in characteristics else -18
    if "vision" in task_characteristics:
        score += 36 if "vision" in characteristics else -18
    if "code" in task_characteristics:
        if "code" in characteristics:
            score += 18
        if "tool_calling" in characteristics or "tool" in characteristics:
            score += 8
    if "review" in task_characteristics:
        if "review" in characteristics:
            score += 30
        if "reasoning" in characteristics:
            score += 8
        if "structured" in characteristics or "structured_output" in characteristics:
            score += 6
    if "analysis" in task_characteristics:
        if "analysis" in characteristics:
            score += 18
        if "reasoning" in characteristics:
            score += 8
        if "synthesis" in characteristics:
            score += 4
        if "structured" in characteristics or "structured_output" in characteristics:
            score += 8
    if "creative" in task_characteristics and (
        "creative" in characteristics or "writing" in characteristics
    ):
        score += 18
    if "chinese" in task_characteristics and "chinese" in characteristics:
        score += 5
    if "general" in task_characteristics and ("general" in characteristics or "text" in characteristics):
        score += 4
    return score

def _requested_skills(context: TaskContext) -> tuple[str, ...]:
    value = context.routing_decision.get("requested_skills")
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, tuple):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()


def _discussion_plan(
    roles: tuple[RoleAssignment, ...],
    default_model: str,
    context: TaskContext | None = None,
    *,
    capability_gateway: RuntimeCapabilityGatewayProtocol | None = None,
) -> DiscussionPlan:
    selected_roles = tuple(roles[:6])
    if len(selected_roles) < 2:
        selected_roles = (
            RoleAssignment(
                id="analyst",
                role="Analyst",
                purpose=RolePurpose.EXPERTISE,
                mission="Analyze the task and propose a solution.",
                must_answer=("What is the best answer?",),
                allowed_tools=(),
                forbidden_actions=("Do not perform external operations.",),
                skills=(),
                output_schema={"position": "string"},
                model=default_model,
            ),
            RoleAssignment(
                id="critic",
                role="Critic",
                purpose=RolePurpose.CRITIQUE,
                mission="Challenge assumptions and identify risks.",
                must_answer=("What could be wrong?",),
                allowed_tools=(),
                forbidden_actions=("Do not perform external operations.",),
                skills=(),
                output_schema={"risks": "string[]"},
                model=default_model,
            ),
        )
    participant_ids = _autogen_participant_ids(selected_roles)
    participants = tuple(
        DiscussionParticipant(
            id=participant_id,
            role=role.role,
            goal=role.mission,
            logical_model=role.model,
            allowed_tools=_role_allowed_tools(
                role,
                context,
                capability_gateway=capability_gateway,
            ),
            max_output_tokens=1536,
        )
        for role, participant_id in zip(selected_roles, participant_ids, strict=True)
    )
    return DiscussionPlan(
        participants=participants,
        selector_model=default_model,
        selector_max_output_tokens=512,
        max_turns=min(12, max(4, len(participants) * 2)),
        wall_time_seconds=300.0,
        token_budget=65_536,
        cost_budget_usd=Decimal(10),
        consensus_votes=min(2, len(participants)),
    )


def _autogen_participant_ids(roles: tuple[RoleAssignment, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    identifiers: list[str] = []
    for index, role in enumerate(roles, start=1):
        candidate = role.id.replace("-", "_").replace(".", "_")
        if not candidate.isidentifier() or keyword.iskeyword(candidate):
            candidate = f"agent_{index}"
        base = candidate
        suffix = 2
        while candidate in seen:
            candidate = f"{base}_{suffix}"
            suffix += 1
        seen.add(candidate)
        identifiers.append(candidate)
    return tuple(identifiers)


def _task_profile(task: object) -> TaskProfile:
    text = str(task).lower()
    if any(keyword in text for keyword in ("deploy", "部署", "install", "安装", "server")):
        return TaskProfile.DEPLOYMENT
    if any(keyword in text for keyword in _SOFTWARE_TASK_KEYWORDS):
        return TaskProfile.SOFTWARE
    if any(keyword in text for keyword in ("research", "调研", "分析", "报告", "市场")):
        return TaskProfile.RESEARCH
    if any(keyword in text for keyword in ("incident", "故障", "日志", "告警", "监控")):
        return TaskProfile.OPERATIONS
    return TaskProfile.GENERAL


def _task_profiles(task: object) -> tuple[TaskProfile, ...]:
    text = str(task).lower()
    profiles: list[TaskProfile] = []
    if any(keyword in text for keyword in ("deploy", "部署", "install", "安装", "server")):
        profiles.append(TaskProfile.DEPLOYMENT)
    if any(keyword in text for keyword in _SOFTWARE_TASK_KEYWORDS):
        profiles.append(TaskProfile.SOFTWARE)
    if any(
        keyword in text
        for keyword in ("research", "调研", "分析", "报告", "市场", "竞品", "机会")
    ):
        profiles.append(TaskProfile.RESEARCH)
    if any(keyword in text for keyword in ("incident", "故障", "日志", "告警", "监控")):
        profiles.append(TaskProfile.OPERATIONS)
    if not profiles or TaskProfile.GENERAL not in profiles:
        profiles.append(TaskProfile.GENERAL)
    return tuple(profiles)


def _dispatch_parallelism(
    config: PlatformConfig,
    logical_model: str,
    roles: tuple[RoleAssignment, ...] | None = None,
) -> int:
    logical_models = (
        {role.model for role in roles if role.model in config.models}
        if roles is not None
        else set()
    )
    if not logical_models:
        logical_models = {logical_model}
    slots = sum(_logical_model_capacity(config, item) for item in logical_models)
    return max(1, min(slots, 16))


def _high_risk_task(task: object) -> bool:
    text = str(task).lower()
    return any(
        keyword in text
        for keyword in (
            "delete",
            "删除",
            "drop",
            "生产",
            "payment",
            "付款",
            "credential",
            "密钥",
            "sudo",
        )
    )


def _direct_logical_model(
    config: PlatformConfig,
    routing_decision: object | None = None,
) -> str:
    if isinstance(routing_decision, Mapping):
        harness_decision = routing_decision.get("harness_decision")
        if isinstance(harness_decision, Mapping):
            selected = harness_decision.get("selected_logical_model")
            if isinstance(selected, str) and selected in config.models:
                return selected
        requested = routing_decision.get("direct_model")
        if isinstance(requested, str) and requested:
            return requested
    if "main" in config.models:
        return "main"
    if "direct" in config.models:
        return "direct"
    return min(config.models)


def _deployments(config: PlatformConfig) -> tuple[Deployment, ...]:
    deployments: list[Deployment] = []
    for logical_model, definition in sorted(config.models.items()):
        for index, deployment in enumerate(definition.deployments, start=1):
            deployments.append(
                deployment.to_deployment(
                    deployment_id=f"{logical_model}_{index}",
                    logical_model=logical_model,
                )
            )
    return tuple(deployments)


def _fallbacks(config: PlatformConfig) -> dict[str, str]:
    return {
        logical_model: definition.fallback_model
        for logical_model, definition in config.models.items()
        if definition.fallback_model is not None
    }


def default_runtime_registry() -> RuntimeRegistry:
    return RuntimeRegistry(
        UnavailableRuntime(mode)
        for mode in (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID)
    )


def configured_runtime_registry(
    *,
    config_service: ConfigService,
    secret_service: SecretService,
    redis_client: object,
    transport: ModelTransport | None = None,
    capability_gateway: RuntimeCapabilityGatewayProtocol | None = None,
    harness_tool_gateway: HarnessToolInvoker | None = None,
) -> RuntimeRegistry:
    async def capacity_factory(
        tenant_id: UUID,
        deployments: tuple[Deployment, ...],
    ) -> CapacityPool:
        async def resolve_fingerprint(secret_ref: str) -> str:
            return await secret_service.fingerprint(tenant_id, secret_ref)

        return CapacityPool(
            redis_client,
            deployments=deployments,
            fingerprint_resolver=resolve_fingerprint,
        )

    return RuntimeRegistry(
        (
            ConfigBackedDirectRuntime(
                config_service=config_service,
                secret_service=secret_service,
                capacity_factory=capacity_factory,
                transport=transport,
            ),
            ConfigBackedDispatchRuntime(
                config_service=config_service,
                secret_service=secret_service,
                capacity_factory=capacity_factory,
                transport=transport,
                capability_gateway=capability_gateway,
                harness_tool_gateway=harness_tool_gateway,
            ),
            ConfigBackedDiscussionRuntime(
                config_service=config_service,
                secret_service=secret_service,
                capacity_factory=capacity_factory,
                transport=transport,
                capability_gateway=capability_gateway,
                harness_tool_gateway=harness_tool_gateway,
            ),
            ConfigBackedHybridRuntime(
                config_service=config_service,
                secret_service=secret_service,
                capacity_factory=capacity_factory,
                transport=transport,
                capability_gateway=capability_gateway,
                harness_tool_gateway=harness_tool_gateway,
            ),
        )
    )


__all__ = [
    "ConfigBackedDirectRuntime",
    "ConfigBackedDiscussionRuntime",
    "ConfigBackedDispatchRuntime",
    "ConfigBackedHybridRuntime",
    "TenantSecretResolver",
    "UnavailableRuntime",
    "configured_runtime_registry",
    "default_runtime_registry",
]
