from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

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
    tenant_environment_id,
)
from agent_hub.capability_installer.install_gate import (
    SdkInstallGateUnavailable,
    _complete_owned_operation,
    sdk_install_gate,
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
        entry = self._catalog.get(entry_id)

        async def operation() -> tuple[CapabilityInstallPlan, Any]:
            return await self._install_owned(
                admin_service,
                entry_id=entry_id,
                query=query,
                plan_id=plan_id,
                confirm=confirm,
                tenant_id=tenant_id,
                actor_id=actor_id,
                activation_preflight=activation_preflight,
            )

        if entry.environment_recipe is None:
            return await operation()
        return await self._with_sdk_gate(tenant_id, entry.plugin.id, operation)

    async def _with_sdk_gate[T](
        self,
        tenant_id: UUID,
        plugin_id: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        if self._environment_manager is None:
            return await operation()
        try:
            async with sdk_install_gate(self._environment_manager, tenant_id, plugin_id):
                return await operation()
        except SdkInstallGateUnavailable as error:
            raise CapabilityInstallUnavailable("受信 SDK 安装操作锁不可用，请稍后重试") from error

    async def _install_owned(
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
        if recipe is None:
            await self._check_backend(entry, entry.plugin)
        saved_state = await admin_service.get_capability_install_state(
            entry.id,
            tenant_id=tenant_id,
        )
        if saved_state is not None and saved_state.get("plan_id") == plan.id:
            plugin = await self._replay_installed_plan(
                admin_service,
                entry,
                plan,
                saved_state,
                tenant_id=tenant_id,
            )
            return plan.model_copy(update={"status": "installed"}), plugin
        environment_capability_id = tenant_environment_id(tenant_id, entry.id)
        previous_environment_version = await self._read_active_environment_version(
            environment_capability_id
        )
        existing: Any | None = None
        previous_plugin: str | None = None
        previous_install_state = None if saved_state is None else dict(saved_state)
        reference_owner = f"capability-install:{uuid4().hex}"
        plugin_attempted = False
        state_attempted = False
        environment_attempted = recipe is not None
        try:
            if recipe is not None and previous_environment_version is not None:
                await self._pin_environment(
                    environment_capability_id,
                    previous_environment_version,
                    reference_owner,
                )
            if recipe is None:
                plugin_request, environment_record = self._prepare_plugin_request(entry)
            else:
                prepared, preparation_cancelled = await _complete_owned_operation(
                    asyncio.to_thread(
                        self._prepare_plugin_request, entry, environment_capability_id
                    ),
                )
                plugin_request, environment_record = prepared
                if environment_record is not None:
                    await self._pin_environment(
                        environment_capability_id,
                        environment_record.version,
                        reference_owner,
                    )
                if preparation_cancelled:
                    raise asyncio.CancelledError
            if recipe is not None:
                await self._check_backend(entry, plugin_request)
            if environment_record is None:
                plugin_request = self._inject_resolved_local_command(plugin_request)
            existing = _plugin_by_id(
                await admin_service.list_plugins(tenant_id=tenant_id),
                entry.plugin.id,
            )
            previous_plugin = _plugin_request_snapshot(existing) if existing is not None else None
            plugin_attempted = True
            _, write_cancelled = await _complete_owned_operation(
                admin_service.upsert_plugin(
                    plugin_request,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                ),
            )
            if write_cancelled:
                raise asyncio.CancelledError
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
                "confirmed_plan": _confirmed_plan_snapshot(plan),
                "installed_plugin": _plugin_request_snapshot(plugin),
                "query": plan.query,
                "replaced_existing": "true" if existing is not None else "false",
                **({"previous_plugin": previous_plugin} if previous_plugin is not None else {}),
            }
            if environment_record is not None:
                install_state.update(
                    {
                        "environment_managed": "true",
                        "environment_capability_id": environment_record.capability_id,
                        "environment_version": environment_record.version,
                        "environment_reference_owner": reference_owner,
                        "environment_pinned_versions": json.dumps(
                            sorted(
                                {
                                    environment_record.version,
                                    *(
                                        (previous_environment_version,)
                                        if previous_environment_version is not None
                                        else ()
                                    ),
                                }
                            )
                        ),
                        **(
                            {"environment_previous_version": previous_environment_version}
                            if previous_environment_version is not None
                            else {}
                        ),
                    }
                )
            state_attempted = True
            _, state_cancelled = await _complete_owned_operation(
                admin_service.upsert_capability_install_state(
                    entry.id,
                    install_state,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                ),
            )
            if state_cancelled:
                raise asyncio.CancelledError
        except BaseException as error:
            failure = error

            async def rollback_owned_install() -> None:
                try:
                    if plugin_attempted:
                        if existing is None:
                            await _delete_if_present(
                                admin_service,
                                entry.plugin.id,
                                tenant_id=tenant_id,
                                actor_id=actor_id,
                            )
                        else:
                            await _restore_plugin_snapshot(
                                admin_service,
                                existing,
                                tenant_id=tenant_id,
                                actor_id=actor_id,
                            )
                finally:
                    try:
                        if state_attempted:
                            if previous_install_state is None:
                                await admin_service.delete_capability_install_state(
                                    entry.id,
                                    tenant_id=tenant_id,
                                    actor_id=actor_id,
                                )
                            else:
                                await admin_service.upsert_capability_install_state(
                                    entry.id,
                                    previous_install_state,
                                    tenant_id=tenant_id,
                                    actor_id=actor_id,
                                )
                    finally:
                        if environment_attempted:
                            await asyncio.to_thread(
                                self._restore_environment,
                                environment_capability_id,
                                previous_environment_version,
                                failure,
                            )

            _, cancelled = await _complete_owned_operation(rollback_owned_install())
            if cancelled:
                raise asyncio.CancelledError from error
            raise
        return plan.model_copy(update={"status": "installed"}), plugin

    async def _pin_environment(self, capability_id: str, version: str, owner: str) -> None:
        manager = self._environment_manager
        if manager is None:
            raise CapabilityInstallUnavailable("缺少环境管理器，无法保留 SDK 版本引用")
        try:
            _, cancelled = await _complete_owned_operation(
                asyncio.to_thread(manager.add_reference, capability_id, version, owner)
            )
        except (EnvironmentBuildError, OSError, ValueError) as error:
            raise CapabilityInstallUnavailable(
                "SDK 版本引用未确认，保留已有引用以供核验"
            ) from error
        if cancelled:
            raise asyncio.CancelledError

    async def _check_backend(self, entry: CapabilityCatalogEntry, plugin: Any) -> None:
        _, cancelled = await _complete_owned_operation(
            asyncio.to_thread(
                _ensure_plugin_backend_installable,
                entry,
                plugin=plugin,
                command_resolver=self._command_resolver,
                environment=self._environment,
                runtime_probe=self._runtime_probe,
            )
        )
        if cancelled:
            raise asyncio.CancelledError

    async def _read_active_environment_version(self, capability_id: str) -> str | None:
        version, cancelled = await _complete_owned_operation(
            asyncio.to_thread(self._active_environment_version, capability_id)
        )
        if cancelled:
            raise asyncio.CancelledError
        return version

    async def _replay_installed_plan(
        self,
        admin_service: Any,
        entry: CapabilityCatalogEntry,
        plan: CapabilityInstallPlan,
        state: Mapping[str, str],
        *,
        tenant_id: UUID,
    ) -> Any:
        plugin = _plugin_by_id(
            await admin_service.list_plugins(tenant_id=tenant_id), entry.plugin.id
        )
        if (
            state.get("entry_id") != entry.id
            or state.get("plugin_id") != entry.plugin.id
            or state.get("confirmed_plan") != _confirmed_plan_snapshot(plan)
            or plugin is None
            or state.get("installed_plugin") != _plugin_request_snapshot(plugin)
            or getattr(plugin, "status", "running") != "running"
        ):
            raise CapabilityInstallUnavailable("已确认安装的记录或插件已变化，拒绝重复覆盖回滚基线")
        if entry.environment_recipe is not None:
            capability_id = tenant_environment_id(tenant_id, entry.id)
            try:
                version = await self._read_active_environment_version(capability_id)
            except (EnvironmentBuildError, OSError, ValueError) as error:
                raise CapabilityInstallUnavailable("已确认安装的环境不可验证") from error
            if (
                state.get("environment_managed") != "true"
                or state.get("environment_capability_id") != capability_id
                or version is None
                or state.get("environment_version") != version
            ):
                raise CapabilityInstallUnavailable("已确认安装的环境已变化，拒绝重复覆盖回滚基线")
        return plugin

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

        async def operation() -> CapabilityInstallPlan:
            return await self._rollback_drained(
                admin_service,
                entry_id=entry_id,
                query=query,
                tenant_id=tenant_id,
                actor_id=actor_id,
            )

        return await self._with_sdk_gate(tenant_id, entry.plugin.id, operation)

    async def _rollback_drained(
        self,
        admin_service: Any,
        *,
        entry_id: str,
        query: str,
        tenant_id: UUID,
        actor_id: UUID,
    ) -> CapabilityInstallPlan:
        result, cancelled = await _complete_owned_operation(
            self._rollback_owned(
                admin_service,
                entry_id=entry_id,
                query=query,
                tenant_id=tenant_id,
                actor_id=actor_id,
            )
        )
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _rollback_owned(
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
                raise CapabilityInstallUnavailable("无法在缺少环境管理器时回滚受信 Capability 环境")
            previous_environment_version = state.get("environment_previous_version")
            environment_capability_id = tenant_environment_id(tenant_id, entry.id)
            if (
                environment_managed
                and state.get("environment_capability_id") != environment_capability_id
            ):
                raise CapabilityInstallUnavailable("旧受信环境缺少租户绑定，需要显式迁移后回滚")
            previous_plugin = _previous_plugin_snapshot(state.get("previous_plugin"))
            installed_plugin = _plugin_by_id(
                await admin_service.list_plugins(tenant_id=tenant_id),
                entry.plugin.id,
            )
            installed_environment_version = (
                await self._read_active_environment_version(environment_capability_id)
                if environment_managed
                else None
            )
            plugin_attempted = state_delete_attempted = environment_attempted = False
            try:
                if environment_managed:
                    environment_attempted = True
                    await asyncio.to_thread(
                        self._restore_environment,
                        environment_capability_id,
                        previous_environment_version,
                        None,
                    )
                if previous_plugin is not None:
                    plugin_attempted = True
                    await admin_service.upsert_plugin(
                        previous_plugin,
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                    )
                elif state.get("replaced_existing") != "true":
                    plugin_attempted = True
                    await admin_service.delete_plugin(
                        entry.plugin.id,
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                    )
                state_delete_attempted = True
                await admin_service.delete_capability_install_state(
                    entry.id,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                )
            except BaseException as error:
                # A failed await may already have written; restore every attempted effect.
                try:
                    if plugin_attempted:
                        if installed_plugin is None:
                            await _delete_if_present(
                                admin_service,
                                entry.plugin.id,
                                tenant_id=tenant_id,
                                actor_id=actor_id,
                            )
                        else:
                            await _restore_plugin_snapshot(
                                admin_service,
                                installed_plugin,
                                tenant_id=tenant_id,
                                actor_id=actor_id,
                            )
                finally:
                    try:
                        if state_delete_attempted:
                            await admin_service.upsert_capability_install_state(
                                entry.id,
                                dict(state),
                                tenant_id=tenant_id,
                                actor_id=actor_id,
                            )
                    finally:
                        if environment_attempted:
                            await asyncio.to_thread(
                                self._restore_environment,
                                environment_capability_id,
                                installed_environment_version,
                                error,
                            )
                raise
        return self.plan(entry_id, query=query).model_copy(update={"status": "rolled_back"})

    def _active_environment_version(self, capability_id: str) -> str | None:
        if self._environment_manager is None:
            return None
        try:
            return self._environment_manager.active(capability_id).version
        except KeyError:
            return None

    def _prepare_plugin_request(
        self,
        entry: CapabilityCatalogEntry,
        environment_capability_id: str | None = None,
    ) -> tuple[Any, EnvironmentRecord | None]:
        plugin = entry.plugin.model_copy(deep=True)
        recipe = entry.environment_recipe
        record: EnvironmentRecord | None = None
        capability_id = entry.id if environment_capability_id is None else environment_capability_id
        if recipe is not None:
            if self._environment_manager is None:
                raise CapabilityInstallUnavailable("受信环境配方需要已配置环境管理器")
            try:
                if isinstance(recipe, CliArtifactEnvironmentRecipe):
                    record = self._environment_manager.deploy_cli(
                        CliArtifactSpec(
                            capability_id,
                            recipe.version,
                            recipe.artifact_path,
                            recipe.sha256,
                            recipe.executable_name,
                        )
                    )
                elif isinstance(recipe, PythonLockEnvironmentRecipe):
                    record = self._environment_manager.build_python(
                        PythonEnvironmentSpec(
                            capability_id,
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
        if "local_command" not in {capability.adapter for capability in plugin.capabilities}:
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


async def _restore_plugin_snapshot(
    admin_service: Any,
    plugin: Any,
    *,
    tenant_id: UUID,
    actor_id: UUID,
) -> None:
    """Restore configuration and metadata under admin policy, without starting the plugin."""
    from agent_hub.api.routers.admin import PluginResourceRequest, PluginResourceResponse

    if isinstance(plugin, PluginResourceResponse):
        if plugin.enabled and plugin.status != "running":
            # Upsert inherits a running candidate's lifecycle; never activate a stopped baseline.
            try:
                await admin_service.stop_plugin(plugin.id, tenant_id=tenant_id, actor_id=actor_id)
            except KeyError:
                pass
        # Response-only fields are separate upsert arguments, not request payload fields.
        request = PluginResourceRequest.model_validate(
            plugin.model_dump(include=set(PluginResourceRequest.model_fields))
        )
        await admin_service.upsert_plugin(
            request,
            tenant_id=tenant_id,
            actor_id=actor_id,
            source_filename=plugin.source_filename,
            content_sha256=plugin.content_sha256,
            package_metadata=plugin.package_metadata,
        )
    else:
        await admin_service.upsert_plugin(plugin, tenant_id=tenant_id, actor_id=actor_id)


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
            raise CapabilityInstallUnavailable(f"{entry.name_cn} 缺少本地命令配置，无法安装")
        if not _local_command_exists(command.strip(), command_resolver):
            raise CapabilityInstallUnavailable(
                f"{entry.name_cn} 需要本机已安装命令 {command.strip()} 后才能安装"
            )
        required_commands = _resource_string_list(plugin.resource_config.get("required_commands"))
        resolved_commands: dict[str, str] = {}
        for required_command in required_commands:
            resolved_command = _resolved_local_command(required_command, command_resolver)
            if resolved_command is None:
                raise CapabilityInstallUnavailable(
                    f"{entry.name_cn} 需要本机已安装命令 {required_command} 后才能安装"
                )
            resolved_commands[required_command] = resolved_command
        required_env_any = _resource_string_list(plugin.resource_config.get("required_env_any"))
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


def _confirmed_plan_snapshot(plan: CapabilityInstallPlan) -> str:
    return json.dumps(
        plan.model_dump(mode="json", exclude={"status"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


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
