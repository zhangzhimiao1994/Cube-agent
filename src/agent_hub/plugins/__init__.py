"""Runtime plugin lifecycle and execution adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_hub.plugins.runtime import RuntimePluginService

__all__ = ["RuntimePluginService", "build_runtime_plugin_service"]


def __getattr__(name: str) -> Any:
    if name == "RuntimePluginService":
        from agent_hub.plugins.runtime import RuntimePluginService

        return RuntimePluginService
    if name == "build_runtime_plugin_service":
        from agent_hub.plugins.runtime import build_runtime_plugin_service

        return build_runtime_plugin_service
    raise AttributeError(name)
