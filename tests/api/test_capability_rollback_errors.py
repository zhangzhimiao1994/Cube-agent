from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_hub.api.routers.admin import (
    InMemoryAdminResourceService,
    PluginCapabilityRequest,
    PluginResourceRequest,
)
from agent_hub.app import create_app
from agent_hub.capability_installer.catalog import (
    CapabilityCatalogEntry,
    PythonLockEnvironmentRecipe,
    TrustedCapabilityCatalog,
)
from agent_hub.capability_installer.environment import EnvironmentBuildError
from agent_hub.capability_installer.service import (
    CapabilityInstallerService,
    CapabilityInstallUnavailable,
)
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


@pytest.mark.parametrize("operation", ("plan", "cancel"))
def test_missing_python_lock_returns_safe_conflict_instead_of_server_error(
    tmp_path: Path,
    operation: str,
) -> None:
    entry = CapabilityCatalogEntry(
        id="python-tool",
        name_cn="Python tool",
        summary_cn="Trusted offline tool",
        risks=("code_execution",),
        permission_summary=("Run trusted tool",),
        environment_recipe=PythonLockEnvironmentRecipe(
            version="1.0.0",
            lock_file=tmp_path / "private-root" / "missing.lock",
            wheel_cache=tmp_path / "wheels",
            smoke_module="trusted_tool",
            executable_name="tool",
        ),
        plugin=PluginResourceRequest(
            id="python-tool",
            name="Python tool",
            resource_config={"command": "managed-tool"},
            capabilities=[
                PluginCapabilityRequest(
                    id="python_tool.run",
                    adapter="local_command",
                    permission_class="plugin.use",
                    sandbox_profile="local_process",
                )
            ],
        ),
    )
    app = create_app(auth_service=StubAuthService(), rate_limiter=object())
    app.state.admin_resource_service = InMemoryAdminResourceService()
    app.state.capability_installer_service = CapabilityInstallerService(
        TrustedCapabilityCatalog((entry,))
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/v1/admin/capability-installer/" + operation,
        headers=headers(),
        json={"entry_id": entry.id, "query": "install"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "capability_install_backend_unavailable"
    assert "private-root" not in response.text and "missing.lock" not in response.text


def test_unconfirmed_python_install_does_not_capture_a_lock_at_the_http_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.capability_installer import catalog

    captures: list[Path] = []

    def forbidden_capture(path: Path) -> None:
        captures.append(path)
        raise EnvironmentBuildError("private-root must not be read")

    monkeypatch.setattr(catalog, "capture_python_lock", forbidden_capture)
    entry = CapabilityCatalogEntry(
        id="python-tool",
        name_cn="Python tool",
        summary_cn="Trusted offline tool",
        risks=("code_execution",),
        permission_summary=("Run trusted tool",),
        environment_recipe=PythonLockEnvironmentRecipe(
            version="1.0.0",
            lock_file=Path("/private-root/missing.lock"),
            wheel_cache=Path("/wheels"),
            smoke_module="trusted_tool",
            executable_name="tool",
        ),
        plugin=PluginResourceRequest(id="python-tool", name="Python tool"),
    )
    app = create_app(auth_service=StubAuthService(), rate_limiter=object())
    app.state.admin_resource_service = InMemoryAdminResourceService()
    app.state.capability_installer_service = CapabilityInstallerService(
        TrustedCapabilityCatalog((entry,))
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/v1/admin/capability-installer/install",
        headers=headers(),
        json={"entry_id": entry.id, "query": "install", "plan_id": "unapproved", "confirm": False},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "capability_install_confirmation_required"
    assert captures == []
