"""Default and production runtime registry construction."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
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
from agent_hub.runtime.contracts import (
    EventKind,
    ExecutionRuntime,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.registry import RuntimeRegistry
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
            UnavailableRuntime(TaskMode.DISPATCH),
            UnavailableRuntime(TaskMode.DISCUSS),
            UnavailableRuntime(TaskMode.HYBRID),
        )
    )


__all__ = [
    "ConfigBackedDirectRuntime",
    "TenantSecretResolver",
    "UnavailableRuntime",
    "configured_runtime_registry",
    "default_runtime_registry",
]
