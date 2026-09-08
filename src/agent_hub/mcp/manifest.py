from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Protocol, cast
from uuid import UUID

from agent_hub.mcp.types import McpGenerationSnapshot, McpServerDefinition
from agent_hub.runtime.contracts import JsonValue

_SAFE_MCP_CAPABILITY_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


class McpManifestServer(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def health(self) -> str: ...

    @property
    def allowed_tools(self) -> Sequence[str]: ...

    @property
    def transport(self) -> str: ...


class McpConfigCapabilityManifestSource:
    def __init__(self, servers: Sequence[McpManifestServer]) -> None:
        self._servers = tuple(servers)

    def manifests(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "capabilities": tuple(self._manifest_items()),
        }

    def _manifest_items(self) -> tuple[dict[str, JsonValue], ...]:
        items: list[dict[str, JsonValue]] = []
        for server in sorted(self._servers, key=lambda item: item.id):
            if not _safe_segment(server.id):
                continue
            for tool_name in sorted(dict.fromkeys(server.allowed_tools)):
                if not _safe_segment(tool_name):
                    continue
                capability_id = f"{server.id}.{tool_name}"
                if len(capability_id) > 128:
                    continue
                available = server.health in {"healthy", "ok"}
                items.append(
                    {
                        "id": capability_id,
                        "kind": "mcp",
                        "adapter": "mcp_server",
                        "permission_class": "mcp.invoke",
                        "sandbox_profile": _sandbox_profile(server.transport),
                        "available": available,
                        "availability_reason": None
                        if available
                        else _availability_reason(server.health),
                        "replay_safe": False,
                        "aliases": (),
                    }
                )
        return tuple(items)


class McpSnapshotCapabilityManifestSource:
    def __init__(
        self,
        *,
        snapshot_getter: Callable[[], McpGenerationSnapshot],
        servers_getter: Callable[[], Sequence[McpServerDefinition]],
        projected_tool_names: frozenset[str] | None = None,
    ) -> None:
        self._snapshot_getter = snapshot_getter
        self._servers_getter = servers_getter
        self._projected_tool_names = projected_tool_names

    def manifests_for_tenant(self, tenant_id: UUID) -> dict[str, JsonValue]:
        snapshot = self._snapshot_getter()
        server_by_id = {
            server.id: server
            for server in self._servers_getter()
            if server.tenant_id == tenant_id and _safe_segment(server.id)
        }
        items: list[dict[str, JsonValue]] = []
        for tool in sorted(snapshot.tools, key=lambda item: item.qualified_name):
            capability_id = tool.qualified_name
            if (
                self._projected_tool_names is not None
                and capability_id not in self._projected_tool_names
            ):
                continue
            server = server_by_id.get(tool.server_id)
            if server is None or not _safe_segment(tool.name):
                continue
            if len(capability_id) > 128:
                continue
            health = snapshot.health.get(tool.server_id, "unknown")
            available = health in {"healthy", "ok"}
            item: dict[str, JsonValue] = {
                "id": capability_id,
                "kind": "mcp",
                "adapter": "mcp_server",
                "permission_class": "mcp.invoke",
                "sandbox_profile": _sandbox_profile(str(server.transport)),
                "available": available,
                "availability_reason": None if available else _availability_reason(health),
                "replay_safe": False,
                "aliases": (),
            }
            if tool.input_schema:
                item["input_schema"] = cast(JsonValue, tool.input_schema)
            items.append(item)
        return {
            "schema_version": 1,
            "capabilities": tuple(items),
        }


def _safe_segment(value: object) -> bool:
    return isinstance(value, str) and _SAFE_MCP_CAPABILITY_SEGMENT.fullmatch(value) is not None


def _sandbox_profile(transport: str) -> str:
    if transport == "stdio":
        return "mcp_stdio"
    return "mcp_remote"


def _availability_reason(health: str) -> str:
    if health == "configured":
        return "mcp_server_not_discovered"
    if _safe_segment(health):
        return f"mcp_server_{health}"
    return "mcp_server_unavailable"


__all__ = ["McpConfigCapabilityManifestSource", "McpSnapshotCapabilityManifestSource"]
