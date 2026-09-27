from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceRequest
from agent_hub.capability_installer.catalog import (
    CapabilityCatalogEntry,
    CliArtifactEnvironmentRecipe,
    PythonLockEnvironmentRecipe,
    TrustedCapabilityCatalog,
)
from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    CliArtifactSpec,
    EnvironmentRecord,
    PythonEnvironmentSpec,
)
from agent_hub.capability_installer.service import (
    CapabilityInstallerService,
    CapabilityInstallUnavailable,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")


class RecordingAdminService:
    def __init__(self) -> None:
        self.upserts = 0

    async def list_plugins(self, *, tenant_id: UUID) -> tuple[object, ...]:
        del tenant_id
        return ()

    async def upsert_plugin(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.upserts += 1


class RecordingEnvironmentManager:
    def __init__(self, root: Path, *, active_version: str | None = None) -> None:
        self.root = root
        self.active_version = active_version
        self.cli_specs: list[CliArtifactSpec] = []
        self.python_specs: list[PythonEnvironmentSpec] = []
        self.restores: list[tuple[str, str | None]] = []

    def active(self, capability_id: str) -> EnvironmentRecord:
        if self.active_version is None:
            raise KeyError(capability_id)
        return self._record(capability_id, self.active_version, "cli", "tool")

    def deploy_cli(self, spec: CliArtifactSpec) -> EnvironmentRecord:
        self.cli_specs.append(spec)
        self.active_version = spec.version
        return self._record(spec.capability_id, spec.version, "cli", spec.executable_name)

    def build_python(self, spec: PythonEnvironmentSpec) -> EnvironmentRecord:
        self.python_specs.append(spec)
        self.active_version = spec.version
        executable = spec.executable_name or "python"
        return self._record(spec.capability_id, spec.version, "python", executable)

    def restore_active(
        self, capability_id: str, version: str | None
    ) -> EnvironmentRecord | None:
        self.restores.append((capability_id, version))
        self.active_version = version
        if version is None:
            return None
        return self._record(capability_id, version, "cli", "tool")

    def _record(
        self, capability_id: str, version: str, kind: str, executable: str
    ) -> EnvironmentRecord:
        path = self.root / capability_id / "versions" / version
        path.mkdir(parents=True, exist_ok=True)
        target = path / executable
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"executable")
        return EnvironmentRecord(capability_id, version, kind, path, executable, "a" * 64)


class InstallingAdminService:
    def __init__(self, *, existing: PluginResourceRequest | None = None, fail_start: bool = False) -> None:
        self.plugins = () if existing is None else (existing,)
        self.current: PluginResourceRequest | None = existing
        self.fail_start = fail_start
        self.started_preflight: object | None = None
        self.state: dict[str, str] | None = None

    async def list_plugins(self, *, tenant_id: UUID) -> tuple[PluginResourceRequest, ...]:
        del tenant_id
        return self.plugins

    async def upsert_plugin(
        self, plugin: PluginResourceRequest, *, tenant_id: UUID, actor_id: UUID
    ) -> None:
        del tenant_id, actor_id
        self.current = plugin

    async def start_plugin(self, plugin_id: str, **kwargs: object) -> PluginResourceRequest:
        assert self.current is not None and self.current.id == plugin_id
        self.started_preflight = kwargs.get("activation_preflight")
        if self.fail_start:
            raise RuntimeError("activation failed")
        return self.current

    async def delete_plugin(self, plugin_id: str, **kwargs: object) -> None:
        del plugin_id, kwargs
        self.current = None

    async def upsert_capability_install_state(
        self, entry_id: str, state: dict[str, str], **kwargs: object
    ) -> None:
        del entry_id, kwargs
        self.state = dict(state)

    async def get_capability_install_state(
        self, entry_id: str, **kwargs: object
    ) -> dict[str, str] | None:
        del entry_id, kwargs
        return None if self.state is None else dict(self.state)

    async def delete_capability_install_state(self, entry_id: str, **kwargs: object) -> None:
        del entry_id, kwargs
        self.state = None


def _managed_entry(
    recipe: CliArtifactEnvironmentRecipe | PythonLockEnvironmentRecipe,
) -> CapabilityCatalogEntry:
    return CapabilityCatalogEntry(
        id="managed_tool",
        name_cn="受信工具",
        summary_cn="离线构建受信工具环境。",
        risks=("code_execution",),
        permission_summary=("执行受信工具",),
        environment_recipe=recipe,
        plugin=PluginResourceRequest(
            id="managed-tool",
            name="Managed Tool",
            resource_config={"command": "managed-by-environment"},
            capabilities=[
                PluginCapabilityRequest(
                    id="managed.run",
                    adapter="local_command",
                    permission_class="plugin.use",
                    sandbox_profile="local_process",
                )
            ],
        ),
    )


@pytest.mark.asyncio
async def test_installer_rejects_configured_http_capability_without_real_endpoint() -> None:
    installer = CapabilityInstallerService()
    plan = installer.plan("office_doc_search", query="读取 Office 文档并搜索")
    admin = RecordingAdminService()

    with pytest.raises(CapabilityInstallUnavailable, match="真实 HTTP endpoint"):
        await installer.install(
            admin,
            entry_id="office_doc_search",
            query="读取 Office 文档并搜索",
            plan_id=plan.id,
            confirm=True,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )

    assert admin.upserts == 0


@pytest.mark.asyncio
async def test_installer_rejects_local_command_capability_when_command_is_missing() -> None:
    installer = CapabilityInstallerService(
        catalog=TrustedCapabilityCatalog(
            (
                CapabilityCatalogEntry(
                    id="missing_cli",
                    name_cn="缺失命令能力",
                    summary_cn="测试缺失本地命令时不会写入插件。",
                    risks=("code_execution",),
                    permission_summary=("运行本地命令",),
                    plugin=PluginResourceRequest(
                        id="missing-cli",
                        name="Missing CLI",
                        resource_config={"command": "agent-hub-test-missing-cli"},
                        capabilities=[
                            PluginCapabilityRequest(
                                id="missing.run",
                                adapter="local_command",
                                permission_class="plugin.use",
                                sandbox_profile="local_process",
                            )
                        ],
                    ),
                ),
            )
        ),
        command_resolver=lambda _command: None,
    )
    plan = installer.plan("missing_cli", query="需要缺失命令能力")
    admin = RecordingAdminService()

    with pytest.raises(CapabilityInstallUnavailable, match="agent-hub-test-missing-cli"):
        await installer.install(
            admin,
            entry_id="missing_cli",
            query="需要缺失命令能力",
            plan_id=plan.id,
            confirm=True,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )

    assert admin.upserts == 0


@pytest.mark.asyncio
async def test_installer_rejects_local_command_capability_when_dependency_command_is_missing() -> None:
    installer = CapabilityInstallerService(
        command_resolver=lambda command: "C:/tools/strix.exe" if command == "strix" else None
    )
    plan = installer.plan("security_testing", query="需要 strix 自动化渗透能力")
    admin = RecordingAdminService()

    with pytest.raises(CapabilityInstallUnavailable, match="docker"):
        await installer.install(
            admin,
            entry_id="security_testing",
            query="需要 strix 自动化渗透能力",
            plan_id=plan.id,
            confirm=True,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )

    assert admin.upserts == 0


@pytest.mark.asyncio
async def test_installer_rejects_local_command_capability_when_required_environment_is_missing() -> None:
    installer = CapabilityInstallerService(
        command_resolver=lambda command: f"C:/tools/{command}.exe",
        environment={},
    )
    plan = installer.plan("security_testing", query="需要 strix 自动化渗透能力")
    admin = RecordingAdminService()

    with pytest.raises(CapabilityInstallUnavailable, match="环境变量"):
        await installer.install(
            admin,
            entry_id="security_testing",
            query="需要 strix 自动化渗透能力",
            plan_id=plan.id,
            confirm=True,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )

    assert admin.upserts == 0


@pytest.mark.asyncio
async def test_installer_rejects_local_command_capability_when_docker_daemon_is_unavailable() -> None:
    probes: list[str] = []

    def probe_runtime(executable: str) -> bool:
        probes.append(executable)
        return False

    installer = CapabilityInstallerService(
        command_resolver=lambda command: f"C:/tools/{command}.exe",
        environment={"STRIX_LLM": "configured"},
        runtime_probe=probe_runtime,
    )
    plan = installer.plan("security_testing", query="需要 strix 自动化渗透能力")
    admin = RecordingAdminService()

    with pytest.raises(CapabilityInstallUnavailable, match="Docker"):
        await installer.install(
            admin,
            entry_id="security_testing",
            query="需要 strix 自动化渗透能力",
            plan_id=plan.id,
            confirm=True,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )

    assert probes == ["C:/tools/docker.exe"]
    assert admin.upserts == 0


@pytest.mark.asyncio
async def test_installer_builds_cli_recipe_and_injects_versioned_executable_before_activation(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "cache" / "tool"
    artifact.parent.mkdir()
    artifact.write_bytes(b"trusted")
    recipe = CliArtifactEnvironmentRecipe(
        version="2.0.0",
        artifact_path=artifact,
        sha256="a" * 64,
        executable_name="tool",
    )
    manager = RecordingEnvironmentManager(tmp_path / "environments")
    installer = CapabilityInstallerService(
        TrustedCapabilityCatalog((_managed_entry(recipe),)),
        environment_manager=cast(CapabilityEnvironmentManager, manager),
    )
    admin = InstallingAdminService()
    preflight = cast(Any, object())
    plan = installer.plan("managed_tool", query="安装受信工具")

    installed_plan, plugin = await installer.install(
        admin,
        entry_id="managed_tool",
        query="安装受信工具",
        plan_id=plan.id,
        confirm=True,
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        activation_preflight=preflight,
    )

    assert installed_plan.status == "installed"
    assert manager.cli_specs[0].version == "2.0.0"
    command = plugin.resource_config["command"]
    assert command == str(tmp_path / "environments/managed_tool/versions/2.0.0/tool")
    assert Path(command).is_file()
    assert admin.started_preflight is preflight
    assert admin.state is not None
    assert admin.state["environment_version"] == "2.0.0"


@pytest.mark.asyncio
async def test_installer_maps_python_lock_recipe_to_versioned_environment(tmp_path: Path) -> None:
    recipe = PythonLockEnvironmentRecipe(
        version="3.1.0",
        lock_file=tmp_path / "requirements.lock",
        wheel_cache=tmp_path / "wheels",
        smoke_module="trusted_tool",
        executable_name="trusted-tool",
    )
    manager = RecordingEnvironmentManager(tmp_path / "environments")
    installer = CapabilityInstallerService(
        TrustedCapabilityCatalog((_managed_entry(recipe),)),
        environment_manager=cast(CapabilityEnvironmentManager, manager),
    )
    admin = InstallingAdminService()
    plan = installer.plan("managed_tool", query="安装 Python 工具")

    await installer.install(
        admin,
        entry_id="managed_tool",
        query="安装 Python 工具",
        plan_id=plan.id,
        confirm=True,
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    assert manager.python_specs == [
        PythonEnvironmentSpec(
            "managed_tool",
            "3.1.0",
            tmp_path / "requirements.lock",
            tmp_path / "wheels",
            "trusted_tool",
            "trusted-tool",
        )
    ]


@pytest.mark.asyncio
async def test_installer_fails_closed_when_recipe_has_no_environment_manager(tmp_path: Path) -> None:
    recipe = CliArtifactEnvironmentRecipe(
        version="1.0.0",
        artifact_path=tmp_path / "tool",
        sha256="a" * 64,
        executable_name="tool",
    )
    installer = CapabilityInstallerService(TrustedCapabilityCatalog((_managed_entry(recipe),)))
    admin = RecordingAdminService()
    plan = installer.plan("managed_tool", query="安装")

    with pytest.raises(CapabilityInstallUnavailable, match="环境管理器"):
        await installer.install(
            admin,
            entry_id="managed_tool",
            query="安装",
            plan_id=plan.id,
            confirm=True,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )

    assert admin.upserts == 0


@pytest.mark.asyncio
async def test_activation_failure_restores_previous_active_environment(tmp_path: Path) -> None:
    recipe = CliArtifactEnvironmentRecipe(
        version="2.0.0",
        artifact_path=tmp_path / "tool",
        sha256="a" * 64,
        executable_name="tool",
    )
    manager = RecordingEnvironmentManager(tmp_path / "environments", active_version="1.0.0")
    installer = CapabilityInstallerService(
        TrustedCapabilityCatalog((_managed_entry(recipe),)),
        environment_manager=cast(CapabilityEnvironmentManager, manager),
    )
    admin = InstallingAdminService(fail_start=True)
    plan = installer.plan("managed_tool", query="升级")

    with pytest.raises(RuntimeError, match="activation failed"):
        await installer.install(
            admin,
            entry_id="managed_tool",
            query="升级",
            plan_id=plan.id,
            confirm=True,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )

    assert manager.active_version == "1.0.0"
    assert manager.restores[-1] == ("managed_tool", "1.0.0")


@pytest.mark.asyncio
async def test_rollback_restores_plugin_and_environment_together(tmp_path: Path) -> None:
    recipe = CliArtifactEnvironmentRecipe(
        version="2.0.0",
        artifact_path=tmp_path / "tool",
        sha256="a" * 64,
        executable_name="tool",
    )
    manager = RecordingEnvironmentManager(tmp_path / "environments", active_version="1.0.0")
    installer = CapabilityInstallerService(
        TrustedCapabilityCatalog((_managed_entry(recipe),)),
        environment_manager=cast(CapabilityEnvironmentManager, manager),
    )
    previous = _managed_entry(recipe).plugin.model_copy(
        update={"resource_config": {"command": "C:/previous/tool.exe"}}
    )
    admin = InstallingAdminService(existing=previous)
    plan = installer.plan("managed_tool", query="升级")
    await installer.install(
        admin,
        entry_id="managed_tool",
        query="升级",
        plan_id=plan.id,
        confirm=True,
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    rolled_back = await installer.rollback(
        admin,
        entry_id="managed_tool",
        query="升级",
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    assert rolled_back.status == "rolled_back"
    assert admin.current is not None
    assert admin.current.resource_config["command"] == "C:/previous/tool.exe"
    assert manager.active_version == "1.0.0"
    assert admin.state is None
