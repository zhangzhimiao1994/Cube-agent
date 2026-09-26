from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from agent_hub.capability_installer.catalog import (
    CapabilityCatalogEntry,
    CapabilityInstallPlan,
    TrustedCapabilityCatalog,
    default_trusted_capability_entries,
)


class CapabilityInstallUnavailable(RuntimeError):
    pass


class CapabilityInstallerService:
    def __init__(
        self,
        catalog: TrustedCapabilityCatalog | None = None,
        *,
        command_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self._catalog = catalog or TrustedCapabilityCatalog(default_trusted_capability_entries())
        self._command_resolver = shutil.which if command_resolver is None else command_resolver

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
    ) -> tuple[CapabilityInstallPlan, Any]:
        if confirm is not True:
            raise PermissionError("capability install requires confirmation")
        plan = self.plan(entry_id, query=query)
        if plan.id != plan_id:
            raise ValueError("capability install plan mismatch")
        entry = self._catalog.get(entry_id)
        _ensure_plugin_backend_installable(entry, command_resolver=self._command_resolver)
        existing = _plugin_by_id(
            await admin_service.list_plugins(tenant_id=tenant_id),
            entry.plugin.id,
        )
        previous_plugin = _plugin_request_snapshot(existing) if existing is not None else None
        plugin_request = entry.plugin.model_copy(deep=True)
        try:
            await admin_service.upsert_plugin(
                plugin_request,
                tenant_id=tenant_id,
                actor_id=actor_id,
            )
            plugin = await admin_service.start_plugin(
                entry.plugin.id,
                tenant_id=tenant_id,
                actor_id=actor_id,
            )
            await admin_service.upsert_capability_install_state(
                entry.id,
                {
                    "entry_id": entry.id,
                    "plugin_id": entry.plugin.id,
                    "plan_id": plan.id,
                    "query": plan.query,
                    "replaced_existing": "true" if existing is not None else "false",
                    **({"previous_plugin": previous_plugin} if previous_plugin is not None else {}),
                },
                tenant_id=tenant_id,
                actor_id=actor_id,
            )
        except Exception:
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
            previous_plugin = _previous_plugin_snapshot(state.get("previous_plugin"))
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
            await admin_service.delete_capability_install_state(
                entry.id,
                tenant_id=tenant_id,
                actor_id=actor_id,
            )
        return self.plan(entry_id, query=query).model_copy(update={"status": "rolled_back"})


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
    command_resolver: Callable[[str], str | None],
) -> None:
    plugin = entry.plugin
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
        for required_command in _resource_string_list(
            plugin.resource_config.get("required_commands")
        ):
            if not _local_command_exists(required_command, command_resolver):
                raise CapabilityInstallUnavailable(
                    f"{entry.name_cn} 需要本机已安装命令 {required_command} 后才能安装"
                )


def _local_command_exists(
    command: str,
    command_resolver: Callable[[str], str | None],
) -> bool:
    if any(separator in command for separator in ("/", "\\")):
        return Path(command).is_file()
    if command_resolver(command) is not None:
        return True
    for directory in ("Scripts", "bin"):
        candidate = Path(sys.prefix) / directory / command
        if candidate.is_file() or candidate.with_suffix(".exe").is_file():
            return True
    return False


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
