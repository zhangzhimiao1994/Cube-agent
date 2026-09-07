from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

import pytest

from agent_hub.api.routers.admin import (
    PluginCapabilityRequest,
    PluginResourceRequest,
    PluginResourceResponse,
)
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins.runtime import build_runtime_plugin_service

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")


class FakeAdminService:
    def __init__(self, plugins: tuple[PluginResourceResponse, ...]) -> None:
        self.plugins = plugins
        self.calls = 0

    async def list_plugins(self) -> tuple[PluginResourceResponse, ...]:
        self.calls += 1
        return self.plugins


def plugin(
    plugin_id: str,
    *,
    enabled: bool = True,
    status: str = "running",
    health: str = "healthy",
    capability_id: str = "calendar.create_event",
) -> PluginResourceResponse:
    return PluginResourceResponse(
        **PluginResourceRequest(
            id=plugin_id,
            name=plugin_id,
            enabled=enabled,
            capabilities=[
                PluginCapabilityRequest(
                    id=capability_id,
                    permission_class="calendar.write",
                    sandbox_profile="remote_connector",
                    aliases=["calendar_create"],
                )
            ],
        ).model_dump(),
        status=status,
        health=health,
        last_error_type=None,
    )


async def test_runtime_plugin_service_exposes_running_plugins_as_manifest() -> None:
    admin_service = FakeAdminService(
        (
            plugin("calendar"),
            plugin("stopped", status="stopped", health="stopped", capability_id="stopped.run"),
        )
    )

    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
    )

    assert service.is_available(TENANT_ID, "calendar.create_event") is True
    assert service.is_available(TENANT_ID, "stopped.run") is False
    manifest = service.capability_manifest_source().manifests_for_tenant(TENANT_ID)
    capability_items = cast(tuple[Mapping[str, object], ...], manifest["capabilities"])
    capabilities = {
        str(item["id"]): item
        for item in capability_items
    }
    assert capabilities["calendar.create_event"]["available"] is True
    assert capabilities["stopped.run"]["available"] is False
    assert admin_service.calls == 1


async def test_runtime_plugin_service_invoke_fails_closed_until_backend_is_installed() -> None:
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService((plugin("calendar"),)),
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin backend unavailable"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="plugin_1",
        )


async def test_runtime_plugin_service_fails_closed_when_admin_listing_fails() -> None:
    class FailingAdminService:
        async def list_plugins(self) -> tuple[PluginResourceResponse, ...]:
            raise RuntimeError("raw plugin db failure")

    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FailingAdminService(),
    )

    assert service.manifests_for_tenant(TENANT_ID) == {"schema_version": 1, "capabilities": ()}
    assert service.is_available(TENANT_ID, "calendar.create_event") is False
