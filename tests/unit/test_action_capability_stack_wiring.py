from __future__ import annotations

import asyncio
from pathlib import Path
from types import ModuleType
from typing import Any, TypedDict, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis

from agent_hub import app as app_module
from agent_hub.api.routers.admin import InMemoryAdminResourceService
from agent_hub.capabilities.defaults import RuntimeCapabilityStack, build_runtime_capability_stack
from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.project_verification_receipts import (
    RECEIPT_KEY,
    ProjectVerificationReceiptRepository,
)
from agent_hub.runtime import worker
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.capabilities.test_verification_cold_recovery import Fixture
from tests.unit.test_app_wiring import (
    TENANT_ID,
    FakeDatabase,
    FakeRedis,
    StubAuthService,
    StubConfigService,
    StubRateLimiter,
    valid_settings,
)


class ReceiptOptions(TypedDict, total=False):
    receipt_repository: ProjectVerificationReceiptRepository


class RuntimeAdapter:
    async def reload(self, tenant_id: UUID | None = None) -> None:
        pass

    async def start(self) -> None:
        pass

    def capability_manifest_source(self) -> RuntimeAdapter:
        return self

    def manifests_for_tenant(self, tenant_id: UUID) -> dict[str, object]:
        return {"schema_version": 1, "capabilities": ()}


def _settings(tmp_path: Path) -> Any:
    return valid_settings(
        tmp_path / "attachments",
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=tmp_path / "generated",
        project_workspace_dir=tmp_path / "workspaces",
        capability_environment_root_dir=tmp_path / "environments",
        plugin_package_store_dir=tmp_path / "plugins",
        plugin_package_dependency_cache_dir=tmp_path / "dependency-cache",
    )


def _capture_builds(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> dict[str, Any]:
    captured: dict[str, Any] = {"stacks": [], "artifacts": []}

    def build_stack(**kwargs: Any) -> RuntimeCapabilityStack:
        stack = build_runtime_capability_stack(**kwargs)
        captured["stacks"].append((kwargs, stack))
        return stack

    def artifacts(factory: Any) -> PostgresArtifactRepository:
        repository = PostgresArtifactRepository(factory)
        captured["artifacts"].append(repository)
        return repository

    def registry(**kwargs: Any) -> object:
        captured["registry"] = kwargs
        return object()

    monkeypatch.setattr(module, "build_runtime_capability_stack", build_stack)
    monkeypatch.setattr(module, "PostgresArtifactRepository", artifacts)
    monkeypatch.setattr(module, "configured_runtime_registry", registry)
    return captured


def _assert_isolated_stacks(
    captured: dict[str, Any], settings: Any, factory: Any,
) -> tuple[RuntimeCapabilityStack, RuntimeCapabilityStack]:
    assert len(captured["stacks"]) == 2
    registry = captured["registry"]
    legacy_options, legacy = next(
        (options, stack) for options, stack in captured["stacks"]
        if stack.runtime_gateway is registry["capability_gateway"]
    )
    action_options, action = next(
        (options, stack) for options, stack in captured["stacks"]
        if stack.runtime_gateway is registry["action_capability_gateway"]
    )
    assert legacy is not action
    assert legacy.policy_gateway is not action.policy_gateway
    assert registry["harness_tool_gateway"] is legacy.harness_tool_gateway
    assert registry["action_harness_tool_gateway"] is action.harness_tool_gateway
    assert legacy_options.get("receipt_repository") is None
    assert legacy_options.get("artifact_repository") is None
    assert legacy.runtime_gateway._artifact_repository is None
    assert legacy.runtime_gateway._project_verification._durable is None
    assert len(captured["artifacts"]) == 1
    artifacts = captured["artifacts"][0]
    assert registry["artifact_repository"] is artifacts
    assert action_options["artifact_repository"] is artifacts
    assert artifacts._session_factory is factory
    receipts = action_options["receipt_repository"]
    assert isinstance(receipts, ProjectVerificationReceiptRepository)
    assert receipts._session_factory is factory
    durable = action.runtime_gateway._project_verification._durable
    assert durable.receipts is receipts
    assert durable.artifacts is artifacts
    for name in (
        "run_repository", "require_approval_for_tools", "tool_approval_mode",
        "tool_registry", "mcp_backend", "plugin_backend",
    ):
        assert action_options[name] is legacy_options[name]
    for stack in (legacy, action):
        assert stack.harness_tool_gateway._backend is stack.runtime_gateway
        assert stack.harness_tool_gateway._policy_gateway is stack.policy_gateway
        assert stack.harness_tool_gateway._mcp_backend is legacy_options["mcp_backend"]
        assert stack.harness_tool_gateway._plugin_backend is legacy_options["plugin_backend"]
        assert stack.runtime_gateway._project_workspace_dir == settings.project_workspace_dir
        assert stack.runtime_gateway._run_repository is legacy_options["run_repository"]
        assert stack.runtime_gateway._tenant_id == settings.bootstrap_tenant_id
        assert stack.project_cache_cleanup_hook.verifier is stack.runtime_gateway._project_verification
    return legacy, action


@pytest.mark.parametrize("durable", [False, True])
def test_capability_stack_forwards_optional_receipt_repository(
    tmp_path: Path, durable: bool,
) -> None:
    database = FakeDatabase()
    artifacts = PostgresArtifactRepository(cast(Any, database.session_factory))
    receipts = ProjectVerificationReceiptRepository(cast(Any, database.session_factory))
    options: ReceiptOptions = {"receipt_repository": receipts} if durable else {}
    stack = build_runtime_capability_stack(
        tenant_id=TENANT_ID,
        run_repository=object(),
        skill_store_dir=tmp_path / "skills",
        workspace_root=None,
        project_workspace_dir=tmp_path / "workspaces",
        artifact_repository=artifacts,
        **options,
    )
    verifier = stack.runtime_gateway._project_verification
    assert verifier is not None
    if durable:
        assert verifier._durable is not None
        assert verifier._durable.receipts is receipts
        assert verifier._durable.artifacts is artifacts
    else:
        assert verifier._durable is None


@pytest.mark.parametrize('entry', ['authorization', 'verify'])
async def test_built_legacy_stack_refuses_unknown_durable_execution_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str,
) -> None:
    fixture = Fixture(tmp_path)
    index = {'status': 'running', 'authority': {'execution_token': 'private-worker-token'}}
    fixture.db.row.routing_decision[RECEIPT_KEY] = index
    stack = build_runtime_capability_stack(
        tenant_id=fixture.db.row.tenant_id, run_repository=fixture.runs,
        skill_store_dir=tmp_path / 'skills', workspace_root=None,
        project_workspace_dir=fixture.root,
    )
    verifier = stack.runtime_gateway._project_verification
    assert verifier is not None and verifier._durable is None
    monkeypatch.setattr(verifier, 'executor', fixture.executor)

    async def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail('UNKNOWN must fail before source inspection, preparation, or native execution')

    monkeypatch.setattr(verifier, '_require_supported_source', unexpected)
    monkeypatch.setattr(verifier.executor, 'run', unexpected)
    with pytest.raises(ProjectVerificationError, match='durable verification recovery required') as caught:
        if entry == 'authorization':
            await stack.runtime_gateway.project_verification_static_authorization(CapabilityRequest(
                tenant_id=fixture.db.row.tenant_id, user_id=fixture.db.row.actor_id,
                run_id=fixture.db.row.id, agent_id='main_agent', capability='command',
                operation='run', resource='project/verify', idempotency_key='new-key',
            ))
        else:
            await verifier.verify(tenant_id=fixture.db.row.tenant_id, run_id=fixture.db.row.id,
                                  actor='main_agent', action_key='new-key')
    assert 'private-worker-token' not in str(caught.value)
    assert fixture.db.row.routing_decision[RECEIPT_KEY] == index
    assert fixture.executor.calls == []


def test_app_builds_isolated_action_stack_without_replacing_legacy_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_builds(monkeypatch, app_module)

    async def runtime_adapter(**kwargs: Any) -> RuntimeAdapter:
        return RuntimeAdapter()

    monkeypatch.setattr(app_module, "build_runtime_mcp_service", runtime_adapter)
    monkeypatch.setattr(app_module, "build_runtime_plugin_service", runtime_adapter)
    database = FakeDatabase()
    settings = _settings(tmp_path)
    application = app_module.create_app(
        settings=settings, database=database, redis_client=FakeRedis(),
        auth_service=StubAuthService(), rate_limiter=StubRateLimiter(),
        config_service=StubConfigService(), admin_resource_service=InMemoryAdminResourceService(),
        user_admin_service=object(),
    )
    assert captured["stacks"] == []
    with TestClient(application):
        legacy, action = _assert_isolated_stacks(captured, settings, database.session_factory)
        service = application.state.run_service
        assert application.state.runtime_capability_gateway is legacy.runtime_gateway
        assert service._project_input_preparer is legacy.project_input_preparer
        assert application.state.project_input_preparer is legacy.project_input_preparer
        assert service._terminal_run_hooks == (
            legacy.project_cache_cleanup_hook, action.project_cache_cleanup_hook,
        )


def _build_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[worker.WorkerResources, dict[str, Any], Any, FakeDatabase]:
    captured = _capture_builds(monkeypatch, worker)
    database = FakeDatabase()
    settings = _settings(tmp_path)
    monkeypatch.setattr(worker, "build_database", lambda url: database)
    monkeypatch.setattr(Redis, "from_url", lambda url: FakeRedis())
    monkeypatch.setattr(worker, "ConfigService", lambda factory: StubConfigService())
    monkeypatch.setattr(worker, "RuntimeMcpService", lambda **kwargs: RuntimeAdapter())
    monkeypatch.setattr(worker, "RuntimePluginService", lambda **kwargs: RuntimeAdapter())
    evolution_hook = object()
    monkeypatch.setattr(worker, "_evolution_terminal_hooks", lambda **kwargs: (evolution_hook,))
    captured["evolution"] = evolution_hook
    resources = worker.build_worker_service(settings)
    return resources, captured, settings, database


def test_worker_routes_action_stack_and_preserves_all_terminal_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources, captured, settings, database = _build_worker(tmp_path, monkeypatch)
    legacy, action = _assert_isolated_stacks(captured, settings, database.session_factory)
    assert resources.service._project_input_preparer is legacy.project_input_preparer
    assert resources.service._terminal_run_hooks == (
        captured["evolution"], legacy.project_cache_cleanup_hook, action.project_cache_cleanup_hook,
    )
    assert resources.project_cache_cleanup_hook is legacy.project_cache_cleanup_hook


def test_worker_starts_and_cancels_cleanup_retries_for_both_verifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources, captured, _, _ = _build_worker(tmp_path, monkeypatch)
    expected = {stack.runtime_gateway._project_verification for _, stack in captured["stacks"]}
    started: set[object] = set()
    cancelled: set[object] = set()

    async def retry_loop(self: PythonProjectTerminalHook, stop: asyncio.Event) -> None:
        started.add(self.verifier)
        try:
            await stop.wait()
        finally:
            cancelled.add(self.verifier)

    async def idle_listener(*args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    async def run_loop(*args: Any, **kwargs: Any) -> None:
        for _ in range(5):
            if started == expected:
                return
            await asyncio.sleep(0)
        assert started == expected

    monkeypatch.setattr(PythonProjectTerminalHook, "retry_loop", retry_loop)
    monkeypatch.setattr(worker, "get_settings", lambda: object())
    monkeypatch.setattr(worker, "build_worker_service", lambda settings: resources)
    monkeypatch.setattr(worker, "_run_runtime_config_invalidation_listener", idle_listener)
    monkeypatch.setattr(worker, "run_worker_loop", run_loop)
    asyncio.run(worker._run())
    assert len(expected) == 2
    assert started == cancelled == expected


@pytest.mark.parametrize("with_database", [False, True])
def test_injected_services_do_not_construct_or_start_runtime_stacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_database: bool,
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("injected services must not initialize runtime resources")

    async def redis_probe() -> None:
        return None

    for name in (
        "build_runtime_capability_stack", "configured_runtime_registry",
        "PostgresArtifactRepository", "build_runtime_mcp_service", "build_runtime_plugin_service",
    ):
        monkeypatch.setattr(app_module, name, unexpected)
    injected_service = object()
    injected_registry = object()
    application = app_module.create_app(
        settings=_settings(tmp_path), database=FakeDatabase() if with_database else None,
        database_factory=unexpected, redis_client=FakeRedis(),
        auth_service=StubAuthService(), rate_limiter=StubRateLimiter(),
        config_service=StubConfigService(), admin_resource_service=InMemoryAdminResourceService(),
        user_admin_service=object(), run_service=injected_service,
        runtime_registry=cast(RuntimeRegistry, injected_registry), redis_probe=redis_probe,
    )
    with TestClient(application):
        assert application.state.run_service is injected_service
        assert application.state.runtime_capability_gateway is None
        assert getattr(application.state, "action_project_cache_cleanup_hook", None) is None
