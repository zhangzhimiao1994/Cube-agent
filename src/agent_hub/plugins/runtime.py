from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast
from uuid import UUID

from agent_hub.api.routers.admin import PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.capabilities.tools.registry import PluginConfigCapabilityManifestSource
from agent_hub.runtime.contracts import JsonValue


class PluginConfigService(Protocol):
    async def list_plugins(self) -> Sequence[Any]: ...


class RuntimePluginService:
    def __init__(
        self,
        *,
        tenant_id: UUID,
        admin_service: PluginConfigService,
    ) -> None:
        self._tenant_id = tenant_id
        self._admin_service = admin_service
        self._plugins: tuple[PluginResourceResponse, ...] = ()

    async def start(self) -> None:
        await self.reload(self._tenant_id)

    async def reload(self, tenant_id: UUID | None = None) -> None:
        if tenant_id is not None and tenant_id != self._tenant_id:
            return
        try:
            self._plugins = tuple(
                PluginResourceResponse.model_validate(plugin)
                for plugin in await self._admin_service.list_plugins()
            )
        except Exception:  # noqa: BLE001 - plugin runtime context must fail closed.
            self._plugins = ()

    def capability_manifest_source(self) -> RuntimePluginService:
        return self

    def manifests_for_tenant(self, tenant_id: UUID) -> Mapping[str, JsonValue]:
        if tenant_id != self._tenant_id:
            return _empty_manifest()
        return PluginConfigCapabilityManifestSource(cast(Any, self._plugins)).manifests()

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        manifest = self.manifests_for_tenant(tenant_id)
        capabilities = manifest.get("capabilities")
        if not isinstance(capabilities, tuple):
            return False
        return any(
            isinstance(capability, Mapping)
            and capability.get("id") == name
            and capability.get("available") is True
            for capability in capabilities
        )

    async def invoke(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        del tenant_id, user_id, run_id, actor, name, arguments, idempotency_key
        raise RuntimeCapabilityError("Plugin backend unavailable")


async def build_runtime_plugin_service(
    *,
    tenant_id: UUID,
    admin_service: PluginConfigService,
) -> RuntimePluginService:
    service = RuntimePluginService(
        tenant_id=tenant_id,
        admin_service=admin_service,
    )
    await service.start()
    return service


def _empty_manifest() -> Mapping[str, JsonValue]:
    return {
        "schema_version": 1,
        "capabilities": (),
    }


__all__ = [
    "PluginConfigService",
    "RuntimePluginService",
    "build_runtime_plugin_service",
]
