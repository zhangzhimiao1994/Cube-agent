from __future__ import annotations

from fastapi.testclient import TestClient

from agent_hub.api.routers.admin import InMemoryAdminResourceService
from agent_hub.app import create_app
from agent_hub.capability_installer.service import CapabilityInstallUnavailable
from tests.api.test_admin_resources import StubAuthService, headers


def test_unavailable_managed_rollback_is_a_safe_conflict_not_a_server_error() -> None:
    class UnavailableInstaller:
        async def rollback(self, *args: object, **kwargs: object) -> None:
            raise CapabilityInstallUnavailable("private-root/fixture must not leave the server")

    app = create_app(auth_service=StubAuthService(), rate_limiter=object())
    app.state.admin_resource_service = InMemoryAdminResourceService()
    app.state.capability_installer_service = UnavailableInstaller()

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/v1/admin/capability-installer/rollback",
        headers=headers(),
        json={"entry_id": "fixture-tool", "query": "fixture rollback"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "capability_install_backend_unavailable"
    assert "private-root" not in response.text
