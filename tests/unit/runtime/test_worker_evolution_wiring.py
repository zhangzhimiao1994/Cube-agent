from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.api.routers import admin as admin_router
from agent_hub.api.routers.admin import SystemSettingsResponse
from agent_hub.config.service import ConfigService
from agent_hub.evolution_hooks import EvolutionExecutionIngestHook
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime import worker
from agent_hub.security.secrets import SecretService
from agent_hub.settings import Settings

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
OTHER_TENANT_ID = UUID("00000000-0000-4000-8000-000000000002")


def test_worker_runtime_stack_uses_configured_generated_artifact_dir(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class FakeDatabase:
        session_factory = object()

    class FakeRedis:
        @classmethod
        def from_url(cls, url: str) -> FakeRedis:
            captured["redis_url"] = url
            return cls()

    class FakeSettings:
        bootstrap_tenant_id = TENANT_ID
        skill_store_dir = tmp_path / "skills"
        attachment_store_dir = tmp_path / "attachments"
        generated_artifact_dir = tmp_path / "generated"
        project_workspace_dir = tmp_path / "workspaces"
        runtime_timeout_seconds = 10
        runtime_token_budget = 1000

        def database_url_value(self) -> str:
            return "postgresql+asyncpg://example"

        def redis_url_value(self) -> str:
            return "redis://example"

        def master_key_bytes(self) -> bytes:
            return b"0" * 32

    class FakeRuntimeStack:
        runtime_gateway = object()
        harness_tool_gateway = object()

    class Source:
        def __init__(self, capability_id: str, kind: str) -> None:
            self.capability_id = capability_id
            self.kind = kind

        def manifests_for_tenant(self, tenant_id: UUID) -> dict[str, object]:
            assert tenant_id == TENANT_ID
            return {
                "schema_version": 1,
                "capabilities": (
                    {
                        "id": self.capability_id,
                        "kind": self.kind,
                        "adapter": f"{self.kind}_runtime",
                    },
                ),
            }

    class FakeMcpService:
        def __init__(self, **kwargs: object) -> None:
            captured["mcp_service"] = kwargs

        def capability_manifest_source(self) -> object:
            return Source("search.web_search", "mcp")

    class FakePluginService:
        def __init__(self, **kwargs: object) -> None:
            captured["plugin_service"] = kwargs

        def capability_manifest_source(self) -> object:
            return Source("calendar.create_event", "plugin")

    def fake_build_runtime_capability_stack(**kwargs: object) -> FakeRuntimeStack:
        captured["runtime_stack"] = kwargs
        return FakeRuntimeStack()

    monkeypatch.setattr(worker, "build_database", lambda url: FakeDatabase())
    monkeypatch.setattr(worker, "Redis", FakeRedis)
    monkeypatch.setattr(worker, "ConfigService", lambda session_factory: object())
    monkeypatch.setattr(worker, "RunRepository", lambda session_factory: object())
    monkeypatch.setattr(worker, "SecretCipher", lambda key: object())
    monkeypatch.setattr(worker, "SecretService", lambda session_factory, cipher: object())
    monkeypatch.setattr(worker, "PersistentHermesRunAdvisor", lambda session_factory: object())
    monkeypatch.setattr(worker, "_evolution_terminal_hooks", lambda **kwargs: ())
    monkeypatch.setattr(worker, "configured_runtime_registry", lambda **kwargs: object())
    monkeypatch.setattr(worker, "RunService", lambda *args, **kwargs: object())
    monkeypatch.setattr(worker, "RuntimeMcpService", FakeMcpService)
    monkeypatch.setattr(worker, "RuntimePluginService", FakePluginService)
    monkeypatch.setattr(worker, "build_runtime_capability_stack", fake_build_runtime_capability_stack)

    resources = worker.build_worker_service(cast(Settings, FakeSettings()))

    runtime_stack = captured["runtime_stack"]
    mcp_service = captured["mcp_service"]
    plugin_service = captured["plugin_service"]
    assert isinstance(runtime_stack, dict)
    assert isinstance(mcp_service, dict)
    assert isinstance(plugin_service, dict)
    assert resources.runtime_mcp_service is not None
    assert resources.runtime_plugin_service is not None
    assert mcp_service["tenant_id"] == TENANT_ID
    assert plugin_service["tenant_id"] == TENANT_ID
    assert runtime_stack["generated_artifact_dir"] == tmp_path / "generated"
    assert runtime_stack["project_workspace_dir"] == tmp_path / "workspaces"
    assert runtime_stack["tool_registry"].manifests_for_tenant(TENANT_ID) == {
        "schema_version": 1,
        "capabilities": (
            {
                "id": "search.web_search",
                "kind": "mcp",
                "adapter": "mcp_runtime",
            },
            {
                "id": "calendar.create_event",
                "kind": "plugin",
                "adapter": "plugin_runtime",
            },
        ),
    }
    assert runtime_stack["mcp_backend"] is resources.runtime_mcp_service
    assert runtime_stack["plugin_backend"] is resources.runtime_plugin_service


def test_worker_runtime_stack_reads_tool_approval_settings_for_target_tenant(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class FakeDatabase:
        session_factory = object()

    class FakeRedis:
        @classmethod
        def from_url(cls, url: str) -> FakeRedis:
            captured["redis_url"] = url
            return cls()

    class FakeSettings:
        bootstrap_tenant_id = TENANT_ID
        skill_store_dir = tmp_path / "skills"
        attachment_store_dir = tmp_path / "attachments"
        generated_artifact_dir = tmp_path / "generated"
        project_workspace_dir = tmp_path / "workspaces"
        runtime_timeout_seconds = 10
        runtime_token_budget = 1000

        def database_url_value(self) -> str:
            return "postgresql+asyncpg://example"

        def redis_url_value(self) -> str:
            return "redis://example"

        def master_key_bytes(self) -> bytes:
            return b"0" * 32

    class FakeAdminResourceService:
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            self.tenant_id = TENANT_ID
            self.scope_calls: list[tuple[UUID, UUID]] = []

        def for_principal(
            self, tenant_id: UUID, actor_id: UUID
        ) -> FakeAdminResourceService:
            self.scope_calls.append((tenant_id, actor_id))
            scoped = FakeAdminResourceService()
            scoped.tenant_id = tenant_id
            return scoped

        async def get_settings(self) -> SystemSettingsResponse:
            return SystemSettingsResponse(
                require_approval_for_tools=self.tenant_id == OTHER_TENANT_ID,
                tool_approval_mode="auto_review"
                if self.tenant_id == OTHER_TENANT_ID
                else "ask",
            )

    class FakeRuntimeService:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def capability_manifest_source(self) -> object:
            return object()

    class FakeRuntimeStack:
        runtime_gateway = object()
        harness_tool_gateway = object()

    def fake_build_runtime_capability_stack(**kwargs: object) -> FakeRuntimeStack:
        captured["runtime_stack"] = kwargs
        return FakeRuntimeStack()

    monkeypatch.setattr(worker, "build_database", lambda url: FakeDatabase())
    monkeypatch.setattr(worker, "Redis", FakeRedis)
    monkeypatch.setattr(worker, "ConfigService", lambda session_factory: object())
    monkeypatch.setattr(worker, "RunRepository", lambda session_factory: object())
    monkeypatch.setattr(worker, "SecretCipher", lambda key: object())
    monkeypatch.setattr(worker, "SecretService", lambda session_factory, cipher: object())
    monkeypatch.setattr(worker, "PersistentHermesRunAdvisor", lambda session_factory: object())
    monkeypatch.setattr(worker, "_evolution_terminal_hooks", lambda **kwargs: ())
    monkeypatch.setattr(worker, "configured_runtime_registry", lambda **kwargs: object())
    monkeypatch.setattr(worker, "RunService", lambda *args, **kwargs: object())
    monkeypatch.setattr(worker, "RuntimeMcpService", FakeRuntimeService)
    monkeypatch.setattr(worker, "RuntimePluginService", FakeRuntimeService)
    monkeypatch.setattr(worker, "build_runtime_capability_stack", fake_build_runtime_capability_stack)
    monkeypatch.setattr(admin_router, "PersistentAdminResourceService", FakeAdminResourceService)

    worker.build_worker_service(cast(Settings, FakeSettings()))

    runtime_stack = cast(dict[str, object], captured["runtime_stack"])
    require_approval = cast(Any, runtime_stack["require_approval_for_tools"])
    approval_mode = cast(Any, runtime_stack["tool_approval_mode"])

    assert asyncio.run(require_approval(OTHER_TENANT_ID)) is True
    assert asyncio.run(approval_mode(OTHER_TENANT_ID)) == "auto_review"


def test_worker_registers_enabled_plugin_package_subprocess_adapters(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class FakeDatabase:
        session_factory = object()

    class FakeRedis:
        @classmethod
        def from_url(cls, url: str) -> FakeRedis:
            captured["redis_url"] = url
            return cls()

    class FakeSettings:
        bootstrap_tenant_id = TENANT_ID
        skill_store_dir = tmp_path / "skills"
        plugin_package_store_dir = tmp_path / "packages"
        attachment_store_dir = tmp_path / "attachments"
        generated_artifact_dir = tmp_path / "generated"
        project_workspace_dir = tmp_path / "workspaces"
        runtime_timeout_seconds = 10
        runtime_token_budget = 1000
        plugin_package_subprocess_runner_enabled = True
        plugin_package_subprocess_adapter_ids = frozenset({"calendar_python"})
        plugin_package_subprocess_isolation_backend = "bubblewrap"
        plugin_package_subprocess_bubblewrap_executable = tmp_path / "bwrap"
        plugin_package_subprocess_timeout_seconds = 1
        plugin_package_subprocess_max_stdin_bytes = 2048
        plugin_package_subprocess_max_stdout_bytes = 1024

        def database_url_value(self) -> str:
            return "postgresql+asyncpg://example"

        def redis_url_value(self) -> str:
            return "redis://example"

        def master_key_bytes(self) -> bytes:
            return b"0" * 32

    class FakeRuntimeService:
        def __init__(self, **kwargs: object) -> None:
            captured["plugin_service"] = kwargs

        def capability_manifest_source(self) -> object:
            return object()

    class FakeRuntimeStack:
        runtime_gateway = object()
        harness_tool_gateway = object()

    def fake_build_plugin_package_subprocess_adapters(**kwargs: object) -> dict[str, object]:
        captured["plugin_package_adapter_kwargs"] = kwargs
        return {"calendar_python": object()}

    monkeypatch.setattr(worker, "build_database", lambda url: FakeDatabase())
    monkeypatch.setattr(worker, "Redis", FakeRedis)
    monkeypatch.setattr(worker, "ConfigService", lambda session_factory: object())
    monkeypatch.setattr(worker, "RunRepository", lambda session_factory: object())
    monkeypatch.setattr(worker, "SecretCipher", lambda key: object())
    monkeypatch.setattr(worker, "SecretService", lambda session_factory, cipher: object())
    monkeypatch.setattr(worker, "PersistentHermesRunAdvisor", lambda session_factory: object())
    monkeypatch.setattr(worker, "_evolution_terminal_hooks", lambda **kwargs: ())
    monkeypatch.setattr(worker, "configured_runtime_registry", lambda **kwargs: object())
    monkeypatch.setattr(worker, "RunService", lambda *args, **kwargs: object())
    monkeypatch.setattr(worker, "RuntimeMcpService", lambda **kwargs: FakeRuntimeService())
    monkeypatch.setattr(worker, "RuntimePluginService", FakeRuntimeService)
    monkeypatch.setattr(
        worker,
        "build_plugin_package_subprocess_adapters",
        fake_build_plugin_package_subprocess_adapters,
    )
    monkeypatch.setattr(
        worker,
        "build_runtime_capability_stack",
        lambda **kwargs: FakeRuntimeStack(),
    )

    worker.build_worker_service(cast(Settings, FakeSettings()))

    plugin_service = cast(dict[str, object], captured["plugin_service"])
    adapters = cast(dict[str, Any], plugin_service["adapters"])
    adapter_kwargs = cast(dict[str, object], captured["plugin_package_adapter_kwargs"])

    assert tuple(adapters) == ("calendar_python",)
    assert adapter_kwargs["enabled"] is True
    assert adapter_kwargs["adapter_ids"] == ("calendar_python",)
    assert adapter_kwargs["isolation_backend"] == "bubblewrap"
    assert adapter_kwargs["bubblewrap_executable"] == tmp_path / "bwrap"
    assert adapter_kwargs["timeout_seconds"] == 1
    assert adapter_kwargs["max_stdin_bytes"] == 2048
    assert adapter_kwargs["max_stdout_bytes"] == 1024


def test_worker_runtime_invalidation_listener_uses_mcp_and_plugin_runtimes() -> None:
    class FakeBus:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] | None = None

        async def listen(self, **kwargs: object) -> None:
            self.kwargs = dict(kwargs)
            raise asyncio.CancelledError

    class Runtime:
        async def reload(self, tenant_id: UUID | None = None) -> None:
            del tenant_id

    bus = FakeBus()
    mcp_runtime = Runtime()
    plugin_runtime = Runtime()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            worker._run_runtime_config_invalidation_listener(
                cast(Any, bus),
                mcp_runtime=cast(Any, mcp_runtime),
                plugin_runtime=cast(Any, plugin_runtime),
            )
        )

    assert bus.kwargs is not None
    assert bus.kwargs["mcp_runtime"] is mcp_runtime
    assert bus.kwargs["plugin_runtime"] is plugin_runtime
    assert str(bus.kwargs["stream_consumer_group"]).startswith("worker-")
    assert str(bus.kwargs["stream_consumer_name"]).startswith("worker-")
    assert set(bus.kwargs) == {
        "mcp_runtime",
        "plugin_runtime",
        "stream_consumer_group",
        "stream_consumer_name",
    }


def test_worker_runtime_invalidation_listener_restarts_after_listen_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    class FlakyBus:
        def __init__(self) -> None:
            self.calls = 0
            self.kwargs: list[dict[str, object]] = []

        async def listen(self, **kwargs: object) -> None:
            self.calls += 1
            self.kwargs.append(dict(kwargs))
            if self.calls == 1:
                raise RuntimeError("lost connection")
            raise asyncio.CancelledError

    sleep_delays: list[float] = []

    async def immediate_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr("agent_hub.runtime.worker.asyncio.sleep", immediate_sleep)

    bus = FlakyBus()
    mcp_runtime = object()
    plugin_runtime = object()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            worker._run_runtime_config_invalidation_listener(
                cast(Any, bus),
                mcp_runtime=mcp_runtime,
                plugin_runtime=plugin_runtime,
                retry_delay_seconds=0.0,
            )
        )

    assert bus.calls == 2
    assert len(bus.kwargs) == 2
    first_kwargs, second_kwargs = bus.kwargs
    assert first_kwargs == second_kwargs
    assert first_kwargs["mcp_runtime"] is mcp_runtime
    assert first_kwargs["plugin_runtime"] is plugin_runtime
    assert str(first_kwargs["stream_consumer_group"]).startswith("worker-")
    assert str(first_kwargs["stream_consumer_name"]).startswith("worker-")
    assert sleep_delays == [0.0]


def test_worker_builds_evolution_terminal_hook(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakePersistentAdminResourceService:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        worker,
        "admin",
        SimpleNamespace(PersistentAdminResourceService=FakePersistentAdminResourceService),
        raising=False,
    )

    hooks = worker._evolution_terminal_hooks(
        config_service=cast(ConfigService, object()),
        secret_service=cast(SecretService, object()),
        tenant_id=TENANT_ID,
        run_repository=cast(RunRepository, object()),
        session_factory=cast(async_sessionmaker[AsyncSession], object()),
        skill_store_dir=tmp_path,
    )

    assert len(hooks) == 1
    assert isinstance(hooks[0], EvolutionExecutionIngestHook)
    assert captured["tenant_id"] == TENANT_ID
    assert captured["actor_id"] == TENANT_ID
    assert captured["run_repository"] is not None
    assert captured["session_factory"] is not None
    assert captured["skill_store_dir"] == tmp_path
