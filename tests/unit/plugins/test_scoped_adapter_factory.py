from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import pytest

from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins.runtime import (
    LocalCommandPluginAdapter,
    PluginAdapter,
    PluginInvocationContext,
    RuntimePluginService,
    build_runtime_plugin_service,
)
from agent_hub.runtime.contracts import JsonValue

_A = UUID("11111111-1111-4111-8111-111111111111")
_B = UUID("22222222-2222-4222-8222-222222222222")


def _plugin(name: str = "alpha", adapter: str = "local_command") -> PluginResourceResponse:
    return PluginResourceResponse(
        id=name,
        name=name,
        enabled=True,
        status="running",
        health="healthy",
        resource_config={"command": "approved-command"},
        capabilities=[
            PluginCapabilityRequest(
                id=name + ".echo",
                adapter=adapter,
                permission_class="test.exec",
                sandbox_profile="local_process" if adapter == "local_command" else "network",
            )
        ],
    )


class Config:
    def __init__(self, plugins: tuple[PluginResourceResponse, ...] = ()) -> None:
        self.plugins = plugins or (_plugin(),)

    async def list_plugins(
        self, *, tenant_id: UUID | None = None
    ) -> tuple[PluginResourceResponse, ...]:
        assert tenant_id is not None
        return self.plugins


class ScopedAdapter:
    def __init__(self, scope: str) -> None:
        self.scope = scope
        self.preflight_calls: list[str] = []

    def descriptor(self) -> Mapping[str, JsonValue]:
        return LocalCommandPluginAdapter().descriptor()

    async def preflight(self, *, plugin: PluginResourceResponse) -> None:
        self.preflight_calls.append(plugin.id)

    async def invoke(
        self,
        *,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        arguments: Mapping[str, JsonValue],
        context: PluginInvocationContext,
    ) -> Mapping[str, JsonValue]:
        return {
            "scope": self.scope,
            "tenant": str(context.tenant_id),
            "user": str(context.user_id),
            "actor": context.actor,
            "run": str(context.run_id),
        }


def _service(factory: Any, config: Config | None = None, **kwargs: Any) -> RuntimePluginService:
    assert "adapter_factory" in inspect.signature(RuntimePluginService).parameters, (
        "scoped adapter wiring missing"
    )
    return RuntimePluginService(
        tenant_id=_A, admin_service=config or Config(), adapter_factory=factory, **kwargs
    )


async def _invoke(
    service: RuntimePluginService, tenant: UUID, name: str = "alpha"
) -> Mapping[str, JsonValue]:
    return await service.invoke(
        tenant_id=tenant,
        user_id=_B,
        run_id=_A,
        actor="ordinary-user-agent",
        name=name + ".echo",
        arguments={},
        idempotency_key="scoped-adapter-contract",
    )


async def test_same_plugin_id_in_two_tenants_uses_distinct_execution_scope() -> None:
    def select(tenant: UUID, plugin: PluginResourceResponse) -> Mapping[str, PluginAdapter]:
        return {"local_command": ScopedAdapter(str(tenant) + "/" + plugin.id)}

    service = _service(select)
    await service.start()
    a, b = await _invoke(service, _A), await _invoke(service, _B)
    assert a["scope"] == "11111111-1111-4111-8111-111111111111/alpha"
    assert b["scope"] == "22222222-2222-4222-8222-222222222222/alpha"
    assert a["tenant"] == str(_A) and b["tenant"] == str(_B)
    assert a["user"] == b["user"] == str(_B)
    assert a["actor"] == b["actor"] == "ordinary-user-agent"
    assert a["run"] == b["run"] == str(_A)


async def test_two_plugins_in_one_tenant_do_not_share_workspace_binding() -> None:
    def select(tenant: UUID, plugin: PluginResourceResponse) -> Mapping[str, PluginAdapter]:
        return {"local_command": ScopedAdapter(plugin.id)}

    service = _service(select, Config((_plugin(), _plugin("beta"))))
    await service.start()
    assert (await _invoke(service, _A))["scope"] == "alpha"
    assert (await _invoke(service, _A, "beta"))["scope"] == "beta"


@pytest.mark.parametrize("mode", ["empty", "error", "none"])
async def test_missing_scope_never_uses_global_host_adapter(mode: str) -> None:
    def select(tenant: UUID, plugin: PluginResourceResponse) -> Any:
        if mode == "error":
            raise OSError("scope disappeared")
        return {} if mode == "empty" else None

    service = _service(select, adapters={"local_command": ScopedAdapter("HOST-FALLBACK")})
    await service.start()
    assert not service.is_available(_A, "alpha.echo")
    with pytest.raises(RuntimeCapabilityError, match="unavailable"):
        await _invoke(service, _A)
    capabilities = service.manifests_for_tenant(_A)["capabilities"]
    assert isinstance(capabilities, tuple) and isinstance(capabilities[0], Mapping)
    assert capabilities[0]["available"] is False


async def test_preflight_is_bound_to_authenticated_tenant_and_plugin() -> None:
    bindings: dict[tuple[UUID, str], ScopedAdapter] = {}

    def select(tenant: UUID, plugin: PluginResourceResponse) -> Mapping[str, PluginAdapter]:
        adapter = bindings.setdefault((tenant, plugin.id), ScopedAdapter(str(tenant)))
        return {"local_command": adapter}

    service = _service(select)
    await service.preflight_plugin_for_tenant(_B, _plugin("beta"))
    assert bindings[(_B, "beta")].preflight_calls == ["beta"]
    assert (_A, "beta") not in bindings
    with pytest.raises(RuntimeCapabilityError, match="tenant scope"):
        await service.preflight_plugin(_plugin("beta"))


async def test_revoked_scope_is_not_kept_alive_by_cached_plugin_config() -> None:
    permitted = True

    def select(tenant: UUID, plugin: PluginResourceResponse) -> Mapping[str, PluginAdapter]:
        return {"local_command": ScopedAdapter("active")} if permitted else {}

    service = _service(select)
    await service.start()
    assert (await _invoke(service, _A))["scope"] == "active"
    permitted = False
    assert not service.is_available(_A, "alpha.echo")
    with pytest.raises(RuntimeCapabilityError, match="unavailable"):
        await _invoke(service, _A)


async def test_builder_passes_scope_factory_to_runtime_service() -> None:
    assert "adapter_factory" in inspect.signature(build_runtime_plugin_service).parameters

    def select(tenant: UUID, plugin: PluginResourceResponse) -> Mapping[str, PluginAdapter]:
        return {"local_command": ScopedAdapter("bound-through-builder")}

    service = await build_runtime_plugin_service(
        tenant_id=_A, admin_service=Config(), adapter_factory=select
    )
    assert (await _invoke(service, _A))["scope"] == "bound-through-builder"
