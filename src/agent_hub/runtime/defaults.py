"""Default and production runtime registry construction."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from agent_hub.config.schema import PlatformConfig
from agent_hub.config.service import ConfigService
from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import CapacityPool
from agent_hub.models.gateway import CapacityController, ModelGateway, ModelTransport
from agent_hub.models.litellm_client import LiteLLMClient
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
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from agent_hub.runtime.direct import DirectRuntime
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


CapacityFactory = Callable[[tuple[Deployment, ...]], CapacityController | CapacityPool]


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
        logical_model = _direct_logical_model(config)
        deployments = _deployments(config)
        gateway = ModelGateway(
            ModelRegistry(deployments),
            self._capacity_factory(deployments),
            TenantSecretResolver(self._secret_service, context.tenant_id),
            self._transport,
            fallbacks=_fallbacks(config),
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
    ) -> None:
        self._config_service = config_service
        self._secret_service = secret_service
        self._capacity_factory = capacity_factory
        self._transport = transport or LiteLLMClient()
        self._role_planner = role_planner or RolePlanner()
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
        gateway, logical_model = _gateway_for_config(
            config,
            tenant_id=context.tenant_id,
            secret_service=self._secret_service,
            capacity_factory=self._capacity_factory,
            transport=self._transport,
        )
        role_plan = self._role_planner.plan(
            RolePlanningRequest(
                task=str(context.request),
                mode=TaskMode.DISPATCH,
                profile=_task_profile(context.request),
                high_risk=_high_risk_task(context.request),
                default_model=logical_model,
            )
        )
        return CrewDispatchRuntime(gateway, _dispatch_plan(role_plan.roles, context))


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
    ) -> None:
        self._config_service = config_service
        self._secret_service = secret_service
        self._capacity_factory = capacity_factory
        self._transport = transport or LiteLLMClient()
        self._role_planner = role_planner or RolePlanner()
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
        gateway, logical_model = _gateway_for_config(
            config,
            tenant_id=context.tenant_id,
            secret_service=self._secret_service,
            capacity_factory=self._capacity_factory,
            transport=self._transport,
        )
        role_plan = self._role_planner.plan(
            RolePlanningRequest(
                task=str(context.request),
                mode=TaskMode.DISCUSS,
                profile=_task_profile(context.request),
                high_risk=_high_risk_task(context.request),
                default_model=logical_model,
            )
        )
        return AutoGenDiscussionRuntime(gateway, _discussion_plan(role_plan.roles, logical_model))


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
    ) -> None:
        self._config_service = config_service
        self._secret_service = secret_service
        self._capacity_factory = capacity_factory
        self._transport = transport or LiteLLMClient()
        self._role_planner = role_planner or RolePlanner()
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
        gateway, logical_model = _gateway_for_config(
            config,
            tenant_id=context.tenant_id,
            secret_service=self._secret_service,
            capacity_factory=self._capacity_factory,
            transport=self._transport,
        )
        profile = _task_profile(context.request)
        high_risk = _high_risk_task(context.request)
        dispatch_roles = self._role_planner.plan(
            RolePlanningRequest(
                task=str(context.request),
                mode=TaskMode.DISPATCH,
                profile=profile,
                high_risk=high_risk,
                default_model=logical_model,
            )
        ).roles
        discussion_roles = self._role_planner.plan(
            RolePlanningRequest(
                task=str(context.request),
                mode=TaskMode.DISCUSS,
                profile=profile,
                high_risk=high_risk,
                default_model=logical_model,
            )
        ).roles
        return HybridRuntime(
            CrewDispatchRuntime(gateway, _dispatch_plan(dispatch_roles, context)),
            AutoGenDiscussionRuntime(gateway, _discussion_plan(discussion_roles, logical_model)),
            DirectRuntime(gateway, logical_model=logical_model),
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


def _gateway_for_config(
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
        capacity_factory(deployments),
        TenantSecretResolver(secret_service, tenant_id),
        transport,
        fallbacks=_fallbacks(config),
    )
    return gateway, logical_model


def _dispatch_plan(
    roles: tuple[RoleAssignment, ...],
    context: TaskContext,
) -> DispatchPlan:
    selected_roles = tuple(roles[:8])
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
    agents = [
        AgentSpec(
            id=role.id,
            role=role.role,
            goal=role.mission,
            logical_model=role.model,
            allowed_tools=(),
        )
        for role in selected_roles
    ]
    if not any(agent.id == "final_synthesizer" for agent in agents):
        agents.append(
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Merge role outputs into one concise, evidence-aware final answer.",
                logical_model=selected_roles[0].model,
                allowed_tools=(),
            )
        )
    request_text = str(context.request)
    role_token_budget = min(max(context.token_budget // max(8, len(selected_roles) * 2), 512), 2048)
    final_token_budget = min(max(context.token_budget // 4, 1024), 4096)
    step_timeout = min(max(context.timeout_seconds / max(2, len(selected_roles) + 1), 30.0), 300.0)
    role_steps = tuple(
        DispatchStep(
            id=f"{role.id}_step",
            agent=role.id,
            task=(
                f"Role mission: {role.mission}\n"
                f"User task: {request_text}\n"
                "Return only the role-specific result, evidence, risks, and verification."
            ),
            depends_on=(),
            tools=(),
            token_budget=role_token_budget,
            timeout_seconds=step_timeout,
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
        timeout_seconds=step_timeout,
        cost_budget_usd=Decimal(0),
    )
    return DispatchPlan(
        agents=tuple(agents),
        steps=(*role_steps, final_step),
        allowed_tools=(),
        max_parallelism=min(4, max(1, len(role_steps))),
        total_token_budget=sum(step.token_budget for step in (*role_steps, final_step)),
        total_timeout_seconds=sum(step.timeout_seconds for step in (*role_steps, final_step)),
        total_cost_usd=Decimal(0),
    )


def _discussion_plan(
    roles: tuple[RoleAssignment, ...],
    default_model: str,
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
    participants = tuple(
        DiscussionParticipant(
            id=role.id,
            role=role.role,
            goal=role.mission,
            logical_model=role.model,
            allowed_tools=(),
            max_output_tokens=4096,
        )
        for role in selected_roles
    )
    return DiscussionPlan(
        participants=participants,
        selector_model=default_model,
        max_turns=min(12, max(4, len(participants) * 2)),
        wall_time_seconds=300.0,
        token_budget=65_536,
        cost_budget_usd=Decimal(10),
        consensus_votes=min(2, len(participants)),
    )


def _task_profile(task: object) -> TaskProfile:
    text = str(task).lower()
    if any(keyword in text for keyword in ("deploy", "部署", "install", "安装", "server")):
        return TaskProfile.DEPLOYMENT
    if any(keyword in text for keyword in ("code", "代码", "bug", "api", "github", "test")):
        return TaskProfile.SOFTWARE
    if any(keyword in text for keyword in ("research", "调研", "分析", "报告", "市场")):
        return TaskProfile.RESEARCH
    if any(keyword in text for keyword in ("incident", "故障", "日志", "告警", "监控")):
        return TaskProfile.OPERATIONS
    return TaskProfile.GENERAL


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


def _direct_logical_model(config: PlatformConfig) -> str:
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
) -> RuntimeRegistry:
    def capacity_factory(deployments: tuple[Deployment, ...]) -> CapacityPool:
        return CapacityPool(redis_client, deployments=deployments)

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
            ),
            ConfigBackedDiscussionRuntime(
                config_service=config_service,
                secret_service=secret_service,
                capacity_factory=capacity_factory,
                transport=transport,
            ),
            ConfigBackedHybridRuntime(
                config_service=config_service,
                secret_service=secret_service,
                capacity_factory=capacity_factory,
                transport=transport,
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
