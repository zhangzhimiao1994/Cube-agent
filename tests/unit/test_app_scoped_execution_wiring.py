from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from agent_hub import app as app_module
from agent_hub.api.routers.admin import InMemoryAdminResourceService
from agent_hub.app import create_app
from tests.unit.test_app_wiring import (
    FakeDatabase,
    FakeRedis,
    StubAuthService,
    StubConfigService,
    StubRateLimiter,
    valid_settings,
)


def test_app_lifespan_passes_both_trusted_scope_selectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "plugin_adapter_factory" in inspect.signature(create_app).parameters
    assert "mcp_client_factory" in inspect.signature(create_app).parameters
    captured: dict[str, Any] = {}

    class RuntimeService:
        async def reload(self, tenant_id: UUID | None = None) -> None:
            pass

        def capability_manifest_source(self) -> object:
            return self

        def manifests_for_tenant(self, tenant_id: UUID) -> dict[str, object]:
            return {"schema_version": 1, "capabilities": ()}

    async def build_plugin(**kwargs: Any) -> RuntimeService:
        captured["plugin"] = kwargs
        return RuntimeService()

    async def build_mcp(**kwargs: Any) -> RuntimeService:
        captured["mcp"] = kwargs
        return RuntimeService()

    def plugin_factory(*args: Any) -> Any:
        raise AssertionError("builder spy must not execute selector")

    def mcp_factory(*args: Any) -> Any:
        raise AssertionError("builder spy must not execute selector")

    monkeypatch.setattr(app_module, "build_runtime_plugin_service", build_plugin)
    monkeypatch.setattr(app_module, "build_runtime_mcp_service", build_mcp)
    monkeypatch.setattr(app_module, "configured_runtime_registry", lambda **kwargs: object())
    monkeypatch.setattr(
        app_module,
        "build_runtime_capability_stack",
        lambda **kwargs: SimpleNamespace(runtime_gateway=object(), harness_tool_gateway=object()),
    )
    application = create_app(
        settings=valid_settings(
            tmp_path / "attachments",
            project_workspace_dir=tmp_path / "workspaces",
            capability_environment_root_dir=tmp_path / "environments",
            skill_store_dir=tmp_path / "skills",
        ),
        database=FakeDatabase(),
        redis_client=FakeRedis(),
        auth_service=StubAuthService(),
        rate_limiter=StubRateLimiter(),
        config_service=StubConfigService(),
        admin_resource_service=InMemoryAdminResourceService(),
        user_admin_service=object(),
        plugin_adapter_factory=plugin_factory,
        mcp_client_factory=mcp_factory,
    )
    with TestClient(application):
        pass
    assert captured["plugin"]["adapter_factory"] is plugin_factory
    assert captured["mcp"]["client_factory"] is mcp_factory
    assert "adapters" in captured["plugin"]
