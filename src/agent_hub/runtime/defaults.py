"""Default and production runtime registry construction."""

from __future__ import annotations

import keyword
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from agent_hub.config.schema import AgentDefinition, LogicalModelDefinition, PlatformConfig
from agent_hub.config.service import ConfigService
from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import (
    CapacityPool,
    safe_operational_limit,
)
from agent_hub.models.gateway import CapacityController, ModelGateway, ModelTransport
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.profiles import infer_model_traits
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import Deployment
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
from agent_hub.runtime.failure_reason import runtime_failure_diagnostic_from_reason
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


class UnavailableRuntime:
    """Fail queued runs deterministically instead of leaving them stuck forever."""

    def __init__(self, mode: TaskMode) -> None:
        if mode is TaskMode.AUTO:
            raise ValueError("default runtime mode must be executable")
        self.mode: TaskMode = mode

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.RUNTIME_FAILED,
            sequence=1,
            run_id=context.run_id,
            reason="runtime_not_configured",
            payload=runtime_failure_diagnostic_from_reason("runtime_not_configured"),
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
    ) -> None:
        self.mode = mode
        self._child = child
        self._main_agent_model = main_agent_model
        self._roles = roles
        self._steps = steps

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
        logical_model = _direct_logical_model(config, context.routing_decision)
        deployments = _deployments(config)
        gateway = ModelGateway(
            ModelRegistry(deployments),
            await self._capacity_factory(context.tenant_id, deployments),
            TenantSecretResolver(self._secret_service, context.tenant_id),
            self._transport,
            fallbacks=_fallbacks(config),
            capacity_wait_timeout=60,
        )
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
    ) -> None:
        self._config_service = config_service
        self._secret_service = secret_service
        self._capacity_factory = capacity_factory
        self._transport = transport or LiteLLMClient()
        self._role_planner = role_planner or RolePlanner()
        self._capability_gateway = capability_gateway
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
        gateway, logical_model = await _gateway_for_config(
            config,
            tenant_id=context.tenant_id,
            secret_service=self._secret_service,
            capacity_factory=self._capacity_factory,
            transport=self._transport,
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
        roles = _assign_models_to_roles(
            (*planned_roles, *_temporary_role_assignments(context, logical_model)),
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
        return _PlannedRuntime(
            CrewDispatchRuntime(
                gateway,
                plan,
                capability_gateway=self._capability_gateway,
            ),
            mode=TaskMode.DISPATCH,
            main_agent_model=logical_model,
            roles=_dispatch_role_payload(plan),
            steps=_dispatch_step_payload(plan),
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
    ) -> None:
        self._config_service = config_service
        self._secret_service = secret_service
        self._capacity_factory = capacity_factory
        self._transport = transport or LiteLLMClient()
        self._role_planner = role_planner or RolePlanner()
        self._capability_gateway = capability_gateway
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
        gateway, logical_model = await _gateway_for_config(
            config,
            tenant_id=context.tenant_id,
            secret_service=self._secret_service,
            capacity_factory=self._capacity_factory,
            transport=self._transport,
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
        plan = _discussion_plan(
            roles,
            logical_model,
            context,
            capability_gateway=self._capability_gateway,
        )
        return _PlannedRuntime(
            AutoGenDiscussionRuntime(
                gateway,
                plan,
                capability_gateway=self._capability_gateway,
            ),
            mode=TaskMode.DISCUSS,
            main_agent_model=logical_model,
            roles=_discussion_role_payload(plan),
            steps=_discussion_step_payload(plan),
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
    ) -> None:
        self._config_service = config_service
        self._secret_service = secret_service
        self._capacity_factory = capacity_factory
        self._transport = transport or LiteLLMClient()
        self._role_planner = role_planner or RolePlanner()
        self._capability_gateway = capability_gateway
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
        gateway, logical_model = await _gateway_for_config(
            config,
            tenant_id=context.tenant_id,
            secret_service=self._secret_service,
            capacity_factory=self._capacity_factory,
            transport=self._transport,
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
        dispatch_roles = _assign_models_to_roles(
            (*dispatch_roles, *_temporary_role_assignments(context, logical_model)),
            config,
            default_model=logical_model,
            task=context.request,
        )
        discussion_roles = _assign_models_to_roles(
            discussion_roles,
            config,
            default_model=logical_model,
            task=context.request,
        )
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
        return _PlannedRuntime(
            HybridRuntime(
                CrewDispatchRuntime(
                    gateway,
                    dispatch_plan,
                    capability_gateway=self._capability_gateway,
                ),
                AutoGenDiscussionRuntime(
                    gateway,
                    discussion_plan,
                    capability_gateway=self._capability_gateway,
                ),
                DirectRuntime(gateway, logical_model=logical_model),
            ),
            mode=TaskMode.HYBRID,
            main_agent_model=logical_model,
            roles=_hybrid_role_payload(dispatch_plan, discussion_plan),
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
) -> tuple[ModelGateway, str]:
    logical_model = _direct_logical_model(config)
    deployments = _deployments(config)
    gateway = ModelGateway(
        ModelRegistry(deployments),
        await capacity_factory(tenant_id, deployments),
        TenantSecretResolver(secret_service, tenant_id),
        transport,
        fallbacks=_fallbacks(config),
        capacity_wait_timeout=60,
    )
    return gateway, logical_model


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
    agents = [
        AgentSpec(
            id=role.id,
            role=role.role,
            goal=role.mission,
            logical_model=role.model,
            allowed_tools=_role_allowed_tools(
                role,
                context,
                capability_gateway=capability_gateway,
            ),
        )
        for role in selected_roles
    ]
    if not any(agent.id == "final_synthesizer" for agent in agents):
        agents.append(
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Merge role outputs into one concise, evidence-aware final answer.",
                logical_model=_string_or_default(
                    context.routing_decision.get("main_agent_model"),
                    selected_roles[0].model,
                ),
                allowed_tools=(),
            )
        )
    request_text = str(context.request)
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
                "Return only the role-specific result, evidence, risks, and verification."
            ),
            depends_on=producer_step_ids if _is_post_product_role(role) else (),
            tools=_role_allowed_tools(
                role,
                context,
                capability_gateway=capability_gateway,
            ),
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
        step.agent: ("synthesize" if step.final_synthesizer else "execute") for step in plan.steps
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
    if not requested or context is None or capability_gateway is None:
        return ()
    is_available = getattr(capability_gateway, "is_available", None)
    filtered: list[str] = []
    for name in requested:
        if capability_gateway.is_replay_safe(name):
            filtered.append(name)
            continue
        if callable(is_available) and is_available(context.tenant_id, name):
            filtered.append(name)
    return tuple(dict.fromkeys(filtered))


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
        if (
            not isinstance(identifier, str)
            or not isinstance(role, str)
            or not isinstance(mission, str)
        ):
            continue
        logical_model_for_agent = (
            selected_model if isinstance(selected_model, str) and selected_model else identifier
        )
        skills = raw.get("suggested_skills")
        skill_tuple = (
            tuple(item for item in skills if isinstance(item, str))
            if isinstance(skills, (list, tuple))
            else ()
        )
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
    if not config.models:
        return []
    text = " ".join(
        (
            str(task),
            role.id,
            role.role,
            role.mission,
            " ".join(role.skills),
            " ".join(role.must_answer),
        )
    ).lower()
    preferred = role.model if role.model in config.models and role.model != default_model else ""
    scored: list[tuple[int, int, str]] = []
    for logical_model, definition in config.models.items():
        haystack = " ".join(
            (
                logical_model,
                " ".join(
                    " ".join(
                        (
                            deployment.provider,
                            deployment.model,
                            " ".join(sorted(deployment.capabilities)),
                        )
                    )
                    for deployment in definition.deployments
                ),
            )
        ).lower()
        score = 0
        if logical_model == preferred:
            score += 12
        if logical_model == default_model:
            score += 1
        score += min(
            8, sum(deployment.max_concurrency for deployment in definition.deployments) // 2
        )
        if role.allowed_tools and not _logical_model_supports_tool_roles(definition):
            score -= 1000
        characteristics = _model_characteristics(logical_model, definition)
        score += _task_characteristic_score(text, characteristics)
        if any(keyword in text for keyword in _SOFTWARE_TASK_KEYWORDS):
            if any(keyword in haystack for keyword in ("coder", "code", "qwen", "program")):
                score += 30
            if "tool_calling" in haystack:
                score += 4
        if any(
            keyword in text
            for keyword in (
                "文案",
                "脚本",
                "短剧",
                "视频",
                "导演",
                "剪辑",
                "prompt",
                "提示词",
                "creative",
                "story",
            )
        ):
            if any(
                keyword in haystack
                for keyword in ("creative", "kimi", "qwen", "deepseek", "chat", "text")
            ):
                score += 24
            if any(keyword in haystack for keyword in ("creative", "kimi", "story")):
                score += 10
            if any(keyword in haystack for keyword in ("coder", "code")):
                score -= 4
        if any(
            keyword in text
            for keyword in (
                "分析",
                "调研",
                "研究",
                "经济",
                "金融",
                "市场",
                "竞品",
                "风险",
                "review",
                "audit",
            )
        ):
            if any(
                keyword in haystack
                for keyword in (
                    "analyst",
                    "analysis",
                    "reason",
                    "max",
                    "sonnet",
                    "claude",
                    "deepseek",
                    "qwen",
                    "glm",
                )
            ):
                score += 22
            if "structured_output" in haystack:
                score += 6
        if (
            any(keyword in text for keyword in ("图片", "识图", "视觉", "image", "vision"))
            and "vision" in haystack
        ):
            score += 28
        if any(
            keyword in text for keyword in ("合规", "法律", "隐私", "版权", "资质", "compliance")
        ) and any(
            keyword in haystack for keyword in ("analyst", "review", "sonnet", "claude", "max")
        ):
            score += 18
        scored.append((score, -len(logical_model), logical_model))
    scored.sort(reverse=True)
    return scored


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
    characteristics: set[str] = set()
    if any(
        keyword in text
        for keyword in ("语音", "录音", "音频", "听写", "转写", "speech", "audio", "voice")
    ):
        characteristics.add("audio")
    if any(
        keyword in text for keyword in ("图片", "识图", "视觉", "图像", "截图", "image", "vision")
    ):
        characteristics.add("vision")
    if any(keyword in text for keyword in _SOFTWARE_TASK_KEYWORDS):
        characteristics.add("code")
    if any(
        keyword in text for keyword in ("质量", "审查", "复核", "验收", "评审", "review", "audit")
    ):
        characteristics.add("review")
    if any(
        keyword in text
        for keyword in ("分析", "调研", "研究", "经济", "金融", "市场", "竞品", "风险")
    ):
        characteristics.add("analysis")
    if any(
        keyword in text
        for keyword in (
            "文案",
            "脚本",
            "短剧",
            "视频",
            "导演",
            "剪辑",
            "prompt",
            "提示词",
            "creative",
            "story",
        )
    ):
        characteristics.add("creative")
    if any("\u4e00" <= char <= "\u9fff" for char in text):
        characteristics.add("chinese")
    if not characteristics:
        characteristics.add("general")
    return frozenset(characteristics)


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
    if "general" in task_characteristics and (
        "general" in characteristics or "text" in characteristics
    ):
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
        keyword in text for keyword in ("research", "调研", "分析", "报告", "市场", "竞品", "机会")
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
            ),
            ConfigBackedDiscussionRuntime(
                config_service=config_service,
                secret_service=secret_service,
                capacity_factory=capacity_factory,
                transport=transport,
                capability_gateway=capability_gateway,
            ),
            ConfigBackedHybridRuntime(
                config_service=config_service,
                secret_service=secret_service,
                capacity_factory=capacity_factory,
                transport=transport,
                capability_gateway=capability_gateway,
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
