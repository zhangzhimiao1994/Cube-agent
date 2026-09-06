from __future__ import annotations

from typing import Any, cast

from agent_hub.mcp.manifest import McpConfigCapabilityManifestSource


class Server:
    def __init__(
        self,
        server_id: str,
        *,
        health: str = "healthy",
        allowed_tools: list[str] | None = None,
        transport: str = "streamable_http",
    ) -> None:
        self.id = server_id
        self.health = health
        self.allowed_tools = [] if allowed_tools is None else allowed_tools
        self.transport = transport


def test_mcp_config_manifest_source_projects_allowed_tools() -> None:
    source = McpConfigCapabilityManifestSource(
        (
            Server("search", allowed_tools=["web_search", "web_search", "read_page"]),
            Server("files", health="failed", transport="stdio", allowed_tools=["read_file"]),
        )
    )

    assert source.manifests() == {
        "schema_version": 1,
        "capabilities": (
            {
                "id": "files.read_file",
                "kind": "mcp",
                "adapter": "mcp_server",
                "permission_class": "mcp.invoke",
                "sandbox_profile": "mcp_stdio",
                "available": False,
                "availability_reason": "mcp_server_failed",
                "replay_safe": False,
                "aliases": (),
            },
            {
                "id": "search.read_page",
                "kind": "mcp",
                "adapter": "mcp_server",
                "permission_class": "mcp.invoke",
                "sandbox_profile": "mcp_remote",
                "available": True,
                "availability_reason": None,
                "replay_safe": False,
                "aliases": (),
            },
            {
                "id": "search.web_search",
                "kind": "mcp",
                "adapter": "mcp_server",
                "permission_class": "mcp.invoke",
                "sandbox_profile": "mcp_remote",
                "available": True,
                "availability_reason": None,
                "replay_safe": False,
                "aliases": (),
            },
        ),
    }


def test_mcp_config_manifest_source_skips_unsafe_or_empty_entries() -> None:
    source = McpConfigCapabilityManifestSource(
        (
            Server("bad server", allowed_tools=["read_file"]),
            Server("safe", allowed_tools=["", "bad tool", "read_file"]),
            Server("long", allowed_tools=["x" * 130]),
        )
    )

    assert source.manifests() == {
        "schema_version": 1,
        "capabilities": (
            {
                "id": "safe.read_file",
                "kind": "mcp",
                "adapter": "mcp_server",
                "permission_class": "mcp.invoke",
                "sandbox_profile": "mcp_remote",
                "available": True,
                "availability_reason": None,
                "replay_safe": False,
                "aliases": (),
            },
        ),
    }


def test_mcp_config_manifest_source_treats_configured_as_not_discovered() -> None:
    source = McpConfigCapabilityManifestSource(
        (
            Server("files", health="configured", allowed_tools=["read_file"]),
        )
    )

    capabilities = cast(tuple[dict[str, Any], ...], source.manifests()["capabilities"])
    capability = capabilities[0]
    assert capability["id"] == "files.read_file"
    assert capability["available"] is False
    assert capability["availability_reason"] == "mcp_server_not_discovered"
