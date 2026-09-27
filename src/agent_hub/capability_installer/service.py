from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from agent_hub.capability_installer.catalog import (
    CapabilityCatalogEntry,
    CapabilityInstallPlan,
    CliArtifactEnvironmentRecipe,
    PythonLockEnvironmentRecipe,
    TrustedCapabilityCatalog,
    default_trusted_capability_entries,
)
from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    CliArtifactSpec,
    EnvironmentBuildError,
    EnvironmentRecord,
    PythonEnvironmentSpec,
)

if TYPE_CHECKING:
    from agent_hub.api.routers.admin import PluginActivationPreflight


class CapabilityInstallUnavailable(RuntimeError):
    pass


class CapabilityInstallerService:
    def __init__(
        self,
        catalog: TrustedCapabilityCatalog | None = None,
        *,
        command_resolver: Callable[[str], str | None] | None = None,
        environment: Mapping[str, str] | None = None,
        runtime_probe: Callable[[str], bool] | None = None,
        environment_manager: CapabilityEnvironmentManager | None = None,
    ) -> None:
        self._catalog = catalog or TrustedCapabilityCatalog(default_trusted_capability_entries())
        self._command_resolver = shutil.which if command_resolver is None else command_resolver
        self._environment = dict(os.environ if environment is None else environment)
        self._runtime_probe = _docker_runtime_available if runtime_probe is None else runtime_probe
        self._environment_manager = environment_manager

    def catalog_entries(self) -> tuple[CapabilityCatalogEntry, ...]:
        return self._catalog.list()

    def resolve(self, query: str) -> tuple[CapabilityCatalogEntry, ...]:
        return self._catalog.resolve(query)

    def plan(self, entry_id: str, *, query: str) -> CapabilityInstallPlan:
        return self._catalog.plan(entry_id, query=query)

    def cancel(self, entry_id: str, *, query: str) -> CapabilityInstallPlan:
        return self.plan(entry_id, query=query).model_copy(update={"status": "cancelled"})

    async def install(
        self,
        admin_service: Any,
        *,
        entry_id: str,
        query: str,
        plan_id: str,
        confirm: bool,
        tenant_id: UUID,
        actor_id: UUID,
        activation_preflight: PluginActivationPreflight | None = None,
    ) -> tuple[CapabilityInstallPlan, Any]:
        if confirm is not True:
            raise PermissionError("capability install requires confirmation")
        plan = self.plan(entry_id, query=query)
        if plan.id != plan_id:
            raise ValueError("capability install plan mismatch")
        entry = self._catalog.get(entry_id)
        recipe = entry.environment_recipe
        if recipe is not None and self._environment_manager is None:
            raise CapabilityInstallUnavailable("受信环境配方需要已配置环境管理器")
        previous_environment_version = self._active_environment_version(entry.id)
        existing: Any | None = None
        previous_plugin: str | None = None
        plugin_written = False
        environment_attempted = recipe is not None
        try:
            plugin_request, environment_record = self._prepare_plugin_request(entry)
            _ensure_plugin_backend_installable(
                entry,
                plugin=plugin_request,
                command_resolver=self._command_resolver,
                environment=self._environment,
                runtime_probe=self._runtime_probe,
            )
            if environment_record is None:
                plugin_request = self._inject_resolved_local_command(plugin_request)
            existing = _plugin_by_id(
                await admin_service.list_plugins(tenant_id=tenant_id),
                entry.plugin.id,
            )
            previous_plugin = (
                _plugin_request_snapshot(existing) if existing is not None else None
            )
            await admin_service.upsert_plugin(
                plugin_request,
                tenant_id=tenant_id,
                actor_id=actor_id,
            )
            plugin_written = True
            plugin = await admin_service.start_plugin(
                entry.plugin.id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                activation_preflight=activation_preflight,
            )
            install_state = {
                "entry_id": entry.id,
                "plugin_id": entry.plugin.id,
                "plan_id": plan.id,
                "query": plan.query,
                "replaced_existing": "true" if existing is not None else "false",
                **({"previous_plugin": previous_plugin} if previous_plugin is not None else {}),
            }
            if environment_record is not None:
                install_state.update(
                    {
                        "environment_managed": "true",
                        "environment_capability_id": entry.id,
                        "environment_version": environment_record.version,
                        **(
                            {"environment_previous_version": previous_environment_version}
                            if previous_environment_version is not None
                            else {}
                        ),
                    }
                )
            await admin_service.upsert_capability_install_state(
                entry.id,
                install_state,
                tenant_id=tenant_id,
                actor_id=actor_id,
            )
        except Exception as error:
            try:
                if plugin_written:
                    if existing is None:
                        await _delete_if_present(
                            admin_service,
                            entry.plugin.id,
                            tenant_id=tenant_id,
                            actor_id=actor_id,
                        )
                    else:
                        await admin_service.upsert_plugin(
                            existing,
                            tenant_id=tenant_id,
                            actor_id=actor_id,
                        )
            finally:
                if environment_attempted:
                    self._restore_environment(entry.id, previous_environment_version, error)
            raise
        return plan.model_copy(update={"status": "installed"}), plugin

    async def rollback(
        self,
        admin_service: Any,
        *,
        entry_id: str,
        query: str,
        tenant_id: UUID,
        actor_id: UUID,
    ) -> CapabilityInstallPlan:
        entry = self._catalog.get(entry_id)
        state = await admin_service.get_capability_install_state(
            entry.id,
            tenant_id=tenant_id,
        )
        if state is not None and state.get("plugin_id") == entry.plugin.id:
            environment_managed = state.get("environment_managed") == "true"
            if environment_managed and self._environment_manager is None:
                raise CapabilityInstallUnavailable(
                    "无法在缺少环境管理器时回滚受信 Capability 环境"
                )
            installed_environment_version = state.get("environment_version")
            previous_environment_version = state.get("environment_previous_version")
            if environment_managed:
                self._restore_environment(entry.id, previous_environment_version, None)
            previous_plugin = _previous_plugin_snapshot(state.get("previous_plugin"))
            try:
                if previous_plugin is not None:
                    await admin_service.upsert_plugin(
                        previous_plugin,
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                    )
                elif state.get("replaced_existing") != "true":
                    await admin_service.delete_plugin(
                        entry.plugin.id,
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                    )
            except Exception as error:
                if environment_managed and installed_environment_version is not None:
                    self._restore_environment(entry.id, installed_environment_version, error)
                raise
            await admin_service.delete_capability_install_state(
                entry.id,
                tenant_id=tenant_id,
                actor_id=actor_id,
            )
        return self.plan(entry_id, query=query).model_copy(update={"status": "rolled_back"})

    def _active_environment_version(self, capability_id: str) -> str | None:
        if self._environment_manager is None:
            return None
        try:
            return self._environment_manager.active(capability_id).version
        except KeyError:
            return None

    def _prepare_plugin_request(
        self, entry: CapabilityCatalogEntry
    ) -> tuple[Any, EnvironmentRecord | None]:
        plugin = entry.plugin.model_copy(deep=True)
        recipe = entry.environment_recipe
        record: EnvironmentRecord | None = None
        if recipe is not None:
            if self._environment_manager is None:
                raise CapabilityInstallUnavailable("受信环境配方需要已配置环境管理器")
            try:
                if isinstance(recipe, CliArtifactEnvironmentRecipe):
                    record = self._environment_manager.deploy_cli(
                        CliArtifactSpec(
                            entry.id,
                            recipe.version,
                            recipe.artifact_path,
                            recipe.sha256,
                            recipe.executable_name,
                        )
                    )
                elif isinstance(recipe, PythonLockEnvironmentRecipe):
                    record = self._environment_manager.build_python(
                        PythonEnvironmentSpec(
                            entry.id,
                            recipe.version,
                            recipe.lock_file,
                            recipe.wheel_cache,
                            recipe.smoke_module,
                            recipe.executable_name,
                        )
                    )
            except (EnvironmentBuildError, OSError, ValueError) as error:
                raise CapabilityInstallUnavailable(
                    f"{entry.name_cn} 的受信环境构建失败: {error}"
                ) from error
        resource_config = dict(plugin.resource_config)
        if record is not None:
            resource_config["command"] = str(record.path / record.executable)
        return plugin.model_copy(update={"resource_config": resource_config}), record

    def _inject_resolved_local_command(self, plugin: Any) -> Any:
        if "local_command" not in {
            capability.adapter for capability in plugin.capabilities
        }:
            return plugin
        command = plugin.resource_config.get("command")
        if not isinstance(command, str) or not command.strip():
            return plugin
        resolved = _resolved_local_command(command.strip(), self._command_resolver)
        if resolved is None:
            return plugin
        resource_config = dict(plugin.resource_config)
        resource_config["command"] = resolved
        return plugin.model_copy(update={"resource_config": resource_config})

    def _restore_environment(
        self,
        capability_id: str,
        version: str | None,
        original_error: BaseException | None,
    ) -> None:
        if self._environment_manager is None:
            raise CapabilityInstallUnavailable("缺少环境管理器，无法恢复 Capability 环境")
        try:
            self._environment_manager.restore_active(capability_id, version)
        except (EnvironmentBuildError, OSError, ValueError, KeyError) as restore_error:
            message = "Capability 环境恢复失败"
            if original_error is None:
                raise CapabilityInstallUnavailable(message) from restore_error
            raise CapabilityInstallUnavailable(message) from original_error


async def _delete_if_present(
    admin_service: Any,
    plugin_id: str,
    *,
    tenant_id: UUID,
    actor_id: UUID,
) -> None:
    try:
        await admin_service.delete_plugin(plugin_id, tenant_id=tenant_id, actor_id=actor_id)
    except KeyError:
        return


def _plugin_by_id(plugins: Sequence[Any], plugin_id: str) -> Any | None:
    for plugin in plugins:
        if plugin.id == plugin_id:
            return plugin
    return None


def _ensure_plugin_backend_installable(
    entry: CapabilityCatalogEntry,
    *,
    plugin: Any,
    command_resolver: Callable[[str], str | None],
    environment: Mapping[str, str],
    runtime_probe: Callable[[str], bool],
) -> None:
    adapters = {capability.adapter for capability in plugin.capabilities}
    if "http_json" in adapters:
        endpoint = plugin.endpoint_url
        if (
            endpoint is None
            or not endpoint.strip()
            or "plugins.example" in endpoint
            or not plugin.domain_allowlist
        ):
            raise CapabilityInstallUnavailable(
                f"{entry.name_cn} 需要先配置真实 HTTP endpoint 后才能安装"
            )
    if "local_command" in adapters:
        command = plugin.resource_config.get("command")
        if not isinstance(command, str) or not command.strip():
            raise CapabilityInstallUnavailable(
                f"{entry.name_cn} 缺少本地命令配置，无法安装"
            )
        if not _local_command_exists(command.strip(), command_resolver):
            raise CapabilityInstallUnavailable(
                f"{entry.name_cn} 需要本机已安装命令 {command.strip()} 后才能安装"
            )
        required_commands = _resource_string_list(
            plugin.resource_config.get("required_commands")
        )
        resolved_commands: dict[str, str] = {}
        for required_command in required_commands:
            resolved_command = _resolved_local_command(required_command, command_resolver)
            if resolved_command is None:
                raise CapabilityInstallUnavailable(
                    f"{entry.name_cn} 需要本机已安装命令 {required_command} 后才能安装"
                )
            resolved_commands[required_command] = resolved_command
        required_env_any = _resource_string_list(
            plugin.resource_config.get("required_env_any")
        )
        if required_env_any and not any(environment.get(key) for key in required_env_any):
            raise CapabilityInstallUnavailable(
                f"{entry.name_cn} 需要先配置至少一个所需环境变量后才能安装"
            )
        for required_command, resolved_command in resolved_commands.items():
            if Path(required_command).name.lower() in {
                "docker",
                "docker.exe",
            } and not runtime_probe(resolved_command):
                raise CapabilityInstallUnavailable(
                    f"{entry.name_cn} 需要 Docker daemon 可用后才能安装"
                )


def _local_command_exists(
    command: str,
    command_resolver: Callable[[str], str | None],
) -> bool:
    return _resolved_local_command(command, command_resolver) is not None


def _resolved_local_command(
    command: str,
    command_resolver: Callable[[str], str | None],
) -> str | None:
    if any(separator in command for separator in ("/", "\\")):
        return command if Path(command).is_file() else None
    resolved = command_resolver(command)
    if resolved is not None:
        return resolved
    for directory in ("Scripts", "bin"):
        candidate = Path(sys.prefix) / directory / command
        if candidate.is_file():
            return str(candidate)
        windows_candidate = candidate.with_suffix(".exe")
        if windows_candidate.is_file():
            return str(windows_candidate)
    return None


def _docker_runtime_available(executable: str) -> bool:
    try:
        completed = subprocess.run(
            (executable, "info", "--format", "{{.ServerVersion}}"),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _resource_string_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _plugin_request_snapshot(plugin: Any) -> str:
    payload = plugin.model_dump(
        mode="json",
        include={
            "id",
            "name",
            "description",
            "version",
            "resource_config",
            "endpoint_url",
            "domain_allowlist",
            "timeout_seconds",
            "credential_ref",
            "credential_header",
            "credential_scheme",
            "enabled",
            "capabilities",
        },
        exclude_none=True,
    )
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _previous_plugin_snapshot(value: object) -> Any | None:
    from agent_hub.api.routers.admin import PluginResourceRequest

    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(payload, Mapping):
            return PluginResourceRequest.model_validate(payload)
        return None
    if isinstance(value, Mapping):
        return PluginResourceRequest.model_validate(value)
    return None


__all__ = ["CapabilityInstallUnavailable", "CapabilityInstallerService"]
