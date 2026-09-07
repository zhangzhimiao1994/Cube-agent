from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from agent_hub.runtime.contracts import JsonValue


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


class PluginCapabilityConfig(Protocol):
    id: str
    adapter: str
    permission_class: str
    sandbox_profile: str
    replay_safe: bool
    aliases: Sequence[str]


class PluginConfig(Protocol):
    id: str
    enabled: bool
    status: str
    health: str
    capabilities: Sequence[PluginCapabilityConfig]


class PluginConfigCapabilityManifestSource:
    def __init__(self, plugins: Sequence[PluginConfig]) -> None:
        self._plugins = tuple(plugins)

    def manifests(self) -> Mapping[str, JsonValue]:
        capabilities: list[Mapping[str, JsonValue]] = []
        for plugin in self._plugins:
            available = (
                plugin.enabled
                and plugin.status == "running"
                and plugin.health == "healthy"
            )
            reason = None if available else _plugin_availability_reason(plugin)
            for capability in plugin.capabilities:
                capabilities.append(
                    {
                        "id": capability.id,
                        "kind": "plugin",
                        "adapter": capability.adapter,
                        "permission_class": capability.permission_class,
                        "sandbox_profile": capability.sandbox_profile,
                        "available": available,
                        "availability_reason": reason,
                        "replay_safe": capability.replay_safe is True,
                        "aliases": tuple(capability.aliases),
                    }
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


def _plugin_availability_reason(plugin: PluginConfig) -> str:
    if not plugin.enabled:
        return "plugin_disabled"
    if plugin.status == "running" and plugin.health != "healthy":
        return "plugin_unhealthy"
    if plugin.status in {"stopped", "disabled", "failed"}:
        return f"plugin_{plugin.status}"
    return "plugin_unavailable"


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
