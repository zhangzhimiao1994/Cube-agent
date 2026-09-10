from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from typing import Protocol, cast

from agent_hub.runtime.contracts import JsonValue

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolManifest:
    id: str
    kind: str
    adapter: str
    permission_class: str
    sandbox_profile: str
    replay_safe: bool
    aliases: tuple[str, ...] = ()

    def to_public_dict(self) -> Mapping[str, JsonValue]:
        return {
            "id": self.id,
            "kind": self.kind,
            "adapter": self.adapter,
            "permission_class": self.permission_class,
            "sandbox_profile": self.sandbox_profile,
            "replay_safe": self.replay_safe,
            "aliases": self.aliases,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, object] = {}
        self._manifests: dict[str, ToolManifest] = {}

    def register(
        self,
        name: str,
        tool: object,
        *,
        kind: str = "plugin",
        adapter: str = "tool_registry",
        permission_class: str = "tool.use",
        sandbox_profile: str = "unspecified",
        replay_safe: bool = False,
        aliases: tuple[str, ...] = (),
    ) -> None:
        if not name or name in self._tools:
            raise ValueError("tool name is invalid")
        manifest = ToolManifest(
            id=name,
            kind=_nonblank(kind, "kind"),
            adapter=_nonblank(adapter, "adapter"),
            permission_class=_nonblank(permission_class, "permission_class"),
            sandbox_profile=_nonblank(sandbox_profile, "sandbox_profile"),
            replay_safe=replay_safe is True,
            aliases=_aliases(aliases),
        )
        self._tools[name] = tool
        self._manifests[name] = manifest

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def schemas(self) -> tuple[dict[str, str], ...]:
        return tuple({"name": name} for name in self.names())

    def manifests(self) -> Mapping[str, JsonValue]:
        return {
            "schema_version": 1,
            "capabilities": tuple(
                self._manifests[name].to_public_dict() for name in self.names()
            ),
        }


class CompositeCapabilityManifestSource:
    def __init__(self, sources: Sequence[object]) -> None:
        self._sources = tuple(sources)

    async def ensure_tenant_loaded(self, tenant_id: object) -> None:
        for source in self._sources:
            ensure_tenant_loaded = getattr(source, "ensure_tenant_loaded", None)
            if not callable(ensure_tenant_loaded):
                continue
            try:
                result = ensure_tenant_loaded(tenant_id)
                if isawaitable(result):
                    await result
            except Exception as error:  # noqa: BLE001 - optional inventory preparation must fail closed.
                _LOGGER.warning(
                    "capability_manifest_source_prepare_failed source=%s tenant_id=%s error_type=%s",
                    type(source).__name__,
                    tenant_id,
                    type(error).__name__,
                )
                continue

    def manifests_for_tenant(self, tenant_id: object) -> Mapping[str, JsonValue]:
        capabilities: list[Mapping[str, JsonValue]] = []
        for source in self._sources:
            capabilities.extend(_source_manifest_items(source, tenant_id))
        return {
            "schema_version": 1,
            "capabilities": tuple(capabilities),
        }


class PluginCapabilityConfig(Protocol):
    id: str
    adapter: str
    permission_class: str
    sandbox_profile: str
    policy_effect: str
    replay_safe: bool
    aliases: Sequence[str]


class PluginConfig(Protocol):
    id: str
    enabled: bool
    status: str
    health: str
    capabilities: Sequence[PluginCapabilityConfig]
    package_metadata: object | None


class PluginConfigCapabilityManifestSource:
    def __init__(self, plugins: Sequence[PluginConfig]) -> None:
        self._plugins = tuple(plugins)

    def manifests(self) -> Mapping[str, JsonValue]:
        capabilities: list[Mapping[str, JsonValue]] = []
        for plugin in self._plugins:
            available = _plugin_config_available(plugin)
            reason = None if available else _plugin_availability_reason(plugin)
            for capability in plugin.capabilities:
                item: dict[str, JsonValue] = {
                    "id": capability.id,
                    "kind": "plugin",
                    "adapter": capability.adapter,
                    "permission_class": capability.permission_class,
                    "sandbox_profile": capability.sandbox_profile,
                    "policy_effect": _policy_effect(getattr(capability, "policy_effect", None)),
                    "available": available,
                    "availability_reason": reason,
                    "replay_safe": capability.replay_safe is True,
                    "aliases": tuple(capability.aliases),
                }
                input_schema = getattr(capability, "input_schema", None)
                if isinstance(input_schema, Mapping):
                    item["input_schema"] = cast(JsonValue, input_schema)
                output_schema = getattr(capability, "output_schema", None)
                if isinstance(output_schema, Mapping):
                    item["output_schema"] = cast(JsonValue, output_schema)
                capabilities.append(
                    item
                )
        return {
            "schema_version": 1,
            "capabilities": tuple(capabilities),
        }


def create_builtin_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        "calculator.evaluate",
        object(),
        kind="builtin",
        adapter="runtime_builtin",
        permission_class="calculator.evaluate",
        sandbox_profile="in_process",
        replay_safe=True,
    )
    registry.register(
        "document.generate_docx",
        object(),
        kind="builtin",
        adapter="runtime_builtin",
        permission_class="file.create",
        sandbox_profile="generated_artifact_store",
        replay_safe=True,
    )
    registry.register(
        "http.read",
        object(),
        kind="builtin",
        adapter="runtime_builtin",
        permission_class="network.read",
        sandbox_profile="http_read",
        replay_safe=False,
    )
    registry.register(
        "presentation.generate_pptx",
        object(),
        kind="builtin",
        adapter="runtime_builtin",
        permission_class="file.create",
        sandbox_profile="generated_artifact_store",
        replay_safe=True,
    )
    registry.register(
        "project.generate_zip",
        object(),
        kind="builtin",
        adapter="runtime_builtin",
        permission_class="file.create",
        sandbox_profile="generated_artifact_store",
        replay_safe=True,
    )
    registry.register(
        "workspace.read",
        object(),
        kind="builtin",
        adapter="runtime_builtin",
        permission_class="file.read",
        sandbox_profile="workspace_read",
        replay_safe=True,
        aliases=("workspace_read",),
    )
    return registry


def _policy_effect(value: object) -> str:
    if value in {"inherit", "allow", "require_approval", "deny"}:
        return value
    return "inherit"


def _plugin_availability_reason(plugin: PluginConfig) -> str:
    if not plugin.enabled:
        return "plugin_disabled"
    if _plugin_package_blocks_activation(plugin):
        return _plugin_package_activation_reason(plugin)
    if plugin.status == "running" and plugin.health != "healthy":
        return "plugin_unhealthy"
    if plugin.status in {"stopped", "disabled", "failed"}:
        return f"plugin_{plugin.status}"
    return "plugin_unavailable"


def _plugin_config_available(plugin: PluginConfig) -> bool:
    return (
        plugin.enabled
        and plugin.status == "running"
        and plugin.health == "healthy"
        and not _plugin_package_blocks_activation(plugin)
    )


def _plugin_package_blocks_activation(plugin: PluginConfig) -> bool:
    package = getattr(plugin, "package_metadata", None)
    return (
        getattr(package, "kind", None) == "adapter_package"
        and getattr(package, "activation_state", None) != "eligible"
    )


def _plugin_package_activation_reason(plugin: PluginConfig) -> str:
    package = getattr(plugin, "package_metadata", None)
    reason = getattr(package, "activation_reason", None)
    if type(reason) is str and reason.strip():
        return reason
    return "plugin_package_not_eligible"


def _source_manifest_items(
    source: object,
    tenant_id: object,
) -> tuple[Mapping[str, JsonValue], ...]:
    try:
        tenant_manifest = getattr(source, "manifests_for_tenant", None)
        if callable(tenant_manifest):
            manifest = tenant_manifest(tenant_id)
        else:
            plain_manifest = getattr(source, "manifests", None)
            if not callable(plain_manifest):
                return ()
            manifest = plain_manifest()
    except Exception:  # noqa: BLE001 - optional sources must fail closed.
        return ()
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
        return ()
    raw_items = manifest.get("capabilities")
    if not isinstance(raw_items, tuple | list):
        return ()
    return tuple(
        cast(Mapping[str, JsonValue], item)
        for item in raw_items
        if isinstance(item, Mapping)
    )


def _nonblank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} is invalid")
    return value


def _aliases(value: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError("aliases are invalid")
    aliases = tuple(_nonblank(alias, "alias") for alias in value)
    if len(set(aliases)) != len(aliases):
        raise ValueError("aliases are invalid")
    return aliases
