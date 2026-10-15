from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.api.routers.admin import PluginResourceResponse
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from tests.api.test_admin_resources import client

_A = UUID("11111111-1111-4111-8111-111111111111")
_B = UUID("22222222-2222-4222-8222-222222222222")


class Auth:
    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        tenant = {"tenant-a": _A, "tenant-b": _B}.get(token)
        if tenant is None:
            raise InvalidCredentials("unknown test token")
        return AuthenticatedPrincipal(_A, tenant, Role.SUPER_ADMIN)


@pytest.mark.parametrize("tenant,token", [(_A, "tenant-a"), (_B, "tenant-b")])
def test_activation_uses_authenticated_tenant_scoped_preflight(tenant: UUID, token: str) -> None:
    api = client()
    state = cast(Any, api.app).state
    state.auth_service = Auth()
    calls: list[tuple[UUID, str]] = []

    class Runtime:
        async def preflight_plugin_for_tenant(
            self, tenant_id: UUID, plugin: PluginResourceResponse
        ) -> None:
            calls.append((tenant_id, plugin.id))

        async def preflight_plugin(self, plugin: PluginResourceResponse) -> None:
            raise RuntimeCapabilityError("unscoped preflight must not run")

    state.plugin_service = Runtime()
    headers = {"Authorization": "Bearer " + token}
    created = api.post(
        "/api/v1/admin/plugins",
        headers=headers,
        json={
            "id": "same-plugin",
            "name": "Scoped plugin",
            "resource_config": {"command": "approved-command"},
            "capabilities": [
                {
                    "id": "scoped.echo",
                    "adapter": "local_command",
                    "permission_class": "plugin.use",
                    "sandbox_profile": "local_process",
                }
            ],
        },
    )
    assert created.status_code == 200
    response = api.post("/api/v1/admin/plugins/same-plugin/start", headers=headers)
    assert response.status_code == 200
    assert calls == [(tenant, "same-plugin")]
