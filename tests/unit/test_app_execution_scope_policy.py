from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_hub import app as app_module
from agent_hub.api.routers.admin import InMemoryAdminResourceService, PluginResourceResponse
from agent_hub.app import create_app
from agent_hub.settings import Settings
from tests.unit.test_app_wiring import (
    FakeDatabase,
    FakeRedis,
    StubAuthService,
    StubConfigService,
    StubRateLimiter,
    valid_settings,
)


def _application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, Any]]:
    assert "execution_scope_policy_file" in Settings.model_fields
    captured: dict[str, Any] = {}

    class Runtime:
        async def reload(self, tenant_id: Any = None) -> None:
            pass

        def capability_manifest_source(self) -> object:
            return self

        def manifests_for_tenant(self, tenant_id: Any) -> dict[str, object]:
            return {"schema_version": 1, "capabilities": ()}

    async def build_plugin(**kwargs: Any) -> Runtime:
        captured["plugin"] = kwargs
        return Runtime()

    async def build_mcp(**kwargs: Any) -> Runtime:
        captured["mcp"] = kwargs
        return Runtime()

    monkeypatch.setattr(app_module, "build_runtime_plugin_service", build_plugin)
    monkeypatch.setattr(app_module, "build_runtime_mcp_service", build_mcp)
    monkeypatch.setattr(app_module, "configured_runtime_registry", lambda **kwargs: object())
    monkeypatch.setattr(
        app_module,
        "build_runtime_capability_stack",
        lambda **kwargs: SimpleNamespace(runtime_gateway=object(), harness_tool_gateway=object()),
    )
    app = create_app(
        settings=valid_settings(
            tmp_path / "attachments",
            project_workspace_dir=tmp_path / "workspaces",
            capability_environment_root_dir=tmp_path / "environments",
            skill_store_dir=tmp_path / "skills",
            execution_scope_policy_file=tmp_path / "scope.json",
        ),
        database=FakeDatabase(),
        redis_client=FakeRedis(),
        auth_service=StubAuthService(),
        rate_limiter=StubRateLimiter(),
        config_service=StubConfigService(),
        admin_resource_service=InMemoryAdminResourceService(),
        user_admin_service=object(),
    )
    return app, captured


def test_operator_policy_file_is_loaded_and_wired_into_real_app_lifespan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "execution_scope_policy_file" in Settings.model_fields
    selected_plugin: Any = lambda tenant, plugin: {}
    selected_mcp = lambda server: None
    loaded: list[tuple[Path, tuple[Path, ...], object, Path | None]] = []

    def load(
        path: Path,
        protected_paths: tuple[Path, ...],
        *,
        environment_manager: object = None,
        authorized_workspace_root: Path | None = None,
    ) -> Any:
        loaded.append((path, protected_paths, environment_manager, authorized_workspace_root))
        return SimpleNamespace(plugin_adapters=selected_plugin, mcp_client_factory=selected_mcp)

    monkeypatch.setattr(app_module, "load_execution_scope_policy", load)
    app, captured = _application(tmp_path, monkeypatch)
    with TestClient(app):
        assert app.state.execution_scope_policy_status == "loaded"
    assert loaded[0][0] == tmp_path / "scope.json"
    assert app.state.settings.plugin_package_store_dir in loaded[0][1]
    assert loaded[0][2] is app.state.capability_installer_service._environment_manager
    assert loaded[0][3] == tmp_path / "workspaces"
    assert captured["plugin"]["adapter_factory"] is selected_plugin
    assert captured["mcp"]["client_factory"] is selected_mcp


def test_missing_configured_policy_is_fail_closed_without_breaking_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, captured = _application(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert app.state.execution_scope_policy_status == "unavailable"
    selector = captured["plugin"]["adapter_factory"]
    assert (
        callable(selector)
        and selector(
            app.state.settings.bootstrap_tenant_id,
            PluginResourceResponse(
                id="unknown", name="Unknown", status="stopped", health="stopped"
            ),
        )
        == {}
    )


def test_policy_file_setting_rejects_relative_paths() -> None:
    assert "execution_scope_policy_file" in Settings.model_fields
    with pytest.raises(ValueError, match="absolute"):
        valid_settings(execution_scope_policy_file=Path("relative-scope.json"))
