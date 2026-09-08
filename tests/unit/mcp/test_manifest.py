from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.gateway import CapabilityGateway
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.types import PolicyEffect
from agent_hub.mcp.client import InMemoryMcpClient
from agent_hub.mcp.manifest import (
    McpConfigCapabilityManifestSource,
    McpSnapshotCapabilityManifestSource,
)
from agent_hub.mcp.service import McpService
from agent_hub.mcp.types import (
    DiscoveredMcpTool,
    McpGenerationSnapshot,
    McpServerDefinition,
    McpToolSchema,
    McpTransportKind,
)
from agent_hub.security.secrets import SecretReference

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
SECRET_ID = UUID("33333333-3333-4333-8333-333333333333")
RUN_ID = UUID("44444444-4444-4444-8444-444444444444")


class NoopRunRepository:
    async def begin_capability_approval(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("tests do not exercise approval state")

    async def resolve_capability_approval(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("tests do not exercise approval state")


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


def remote_server(server_id: str = "server") -> McpServerDefinition:
    return McpServerDefinition(
        tenant_id=TENANT_ID,
        id=server_id,
        transport=McpTransportKind.STREAMABLE_HTTP,
        url=f"https://{server_id}.example.com/mcp",
        domain_allowlist=("example.com",),
    )


def allow_gateway() -> CapabilityGateway:
    return CapabilityGateway(
        CapabilityPolicy(
            (
                CapabilityRule(
                    tenant_id=TENANT_ID,
                    role=Role.OPERATOR,
                    agent_id="researcher",
                    capability="mcp",
                    operation="invoke",
                    resource_prefix="mcp/server",
                    effect=PolicyEffect.ALLOW,
                ),
            )
        ),
        ApprovalService(InMemoryApprovalStore()),
        NoopRunRepository(),
    )


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


def test_mcp_snapshot_manifest_source_projects_discovered_tools_for_tenant() -> None:
    source = McpSnapshotCapabilityManifestSource(
        snapshot_getter=lambda: McpGenerationSnapshot(
            generation_id=7,
            health={"search": "healthy", "files": "ok", "other": "healthy"},
            tools=(
                DiscoveredMcpTool(
                    server_id="search",
                    name="web_search",
                    description="Search docs",
                    input_schema={
                        "type": "object",
                        "required": ("query",),
                        "properties": {"query": {"type": "string"}},
                    },
                ),
                DiscoveredMcpTool(
                    server_id="files",
                    name="read_file",
                    description="Read a file",
                ),
                DiscoveredMcpTool(
                    server_id="other",
                    name="foreign",
                    description="Other tenant",
                ),
            ),
        ),
        servers_getter=lambda: (
            McpServerDefinition(
                tenant_id=TENANT_ID,
                id="files",
                transport=McpTransportKind.STDIO,
                command="/usr/bin/mcp-files",
                executable_allowlist=("/usr/bin/mcp-files",),
            ),
            McpServerDefinition(
                tenant_id=TENANT_ID,
                id="search",
                transport=McpTransportKind.STREAMABLE_HTTP,
                url="https://search.example.com/mcp",
                domain_allowlist=("example.com",),
                oauth_token_ref=SecretReference(TENANT_ID, SECRET_ID),
            ),
            McpServerDefinition(
                tenant_id=OTHER_TENANT_ID,
                id="other",
                transport=McpTransportKind.STREAMABLE_HTTP,
                url="https://other.example.com/mcp",
                domain_allowlist=("example.com",),
            ),
        ),
    )

    assert source.manifests_for_tenant(TENANT_ID) == {
        "schema_version": 1,
        "capabilities": (
            {
                "id": "files.read_file",
                "kind": "mcp",
                "adapter": "mcp_server",
                "permission_class": "mcp.invoke",
                "sandbox_profile": "mcp_stdio",
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
                "input_schema": {
                    "type": "object",
                    "required": ("query",),
                    "properties": {"query": {"type": "string"}},
                },
            },
        ),
    }


def test_mcp_snapshot_manifest_source_marks_unhealthy_snapshot_tools_unavailable() -> None:
    source = McpSnapshotCapabilityManifestSource(
        snapshot_getter=lambda: McpGenerationSnapshot(
            generation_id=3,
            health={"search": "timeout"},
            tools=(
                DiscoveredMcpTool(
                    server_id="search",
                    name="web_search",
                    description="Search docs",
                ),
            ),
        ),
        servers_getter=lambda: (
            McpServerDefinition(
                tenant_id=TENANT_ID,
                id="search",
                transport=McpTransportKind.STREAMABLE_HTTP,
                url="https://search.example.com/mcp",
                domain_allowlist=("example.com",),
            ),
        ),
    )

    capabilities = cast(
        tuple[dict[str, Any], ...],
        source.manifests_for_tenant(TENANT_ID)["capabilities"],
    )
    capability = capabilities[0]
    assert capability["id"] == "search.web_search"
    assert capability["available"] is False
    assert capability["availability_reason"] == "mcp_server_timeout"


def test_mcp_snapshot_manifest_source_skips_unsafe_discovered_tool_names() -> None:
    source = McpSnapshotCapabilityManifestSource(
        snapshot_getter=lambda: McpGenerationSnapshot(
            generation_id=3,
            health={"search": "healthy"},
            tools=(
                DiscoveredMcpTool(
                    server_id="search",
                    name="ReadFile",
                    description="Mixed case tool name",
                ),
                DiscoveredMcpTool(
                    server_id="search",
                    name="safe_read",
                    description="Safe tool",
                ),
            ),
        ),
        servers_getter=lambda: (
            McpServerDefinition(
                tenant_id=TENANT_ID,
                id="search",
                transport=McpTransportKind.STREAMABLE_HTTP,
                url="https://search.example.com/mcp",
                domain_allowlist=("example.com",),
            ),
        ),
    )

    capabilities = cast(
        tuple[dict[str, Any], ...],
        source.manifests_for_tenant(TENANT_ID)["capabilities"],
    )
    assert [capability["id"] for capability in capabilities] == ["search.safe_read"]


def test_mcp_snapshot_manifest_source_filters_to_projected_tool_names() -> None:
    source = McpSnapshotCapabilityManifestSource(
        snapshot_getter=lambda: McpGenerationSnapshot(
            generation_id=3,
            health={"search": "healthy"},
            tools=(
                DiscoveredMcpTool(
                    server_id="search",
                    name="safe_read",
                    description="Safe tool",
                ),
                DiscoveredMcpTool(
                    server_id="search",
                    name="dangerous_delete",
                    description="Dangerous tool",
                ),
            ),
        ),
        servers_getter=lambda: (
            McpServerDefinition(
                tenant_id=TENANT_ID,
                id="search",
                transport=McpTransportKind.STREAMABLE_HTTP,
                url="https://search.example.com/mcp",
                domain_allowlist=("example.com",),
            ),
        ),
        projected_tool_names=frozenset({"search.safe_read"}),
    )

    capabilities = cast(
        tuple[dict[str, Any], ...],
        source.manifests_for_tenant(TENANT_ID)["capabilities"],
    )
    assert [capability["id"] for capability in capabilities] == ["search.safe_read"]


async def test_mcp_service_capability_manifest_source_uses_latest_snapshot() -> None:
    service = McpService(
        servers=(remote_server(),),
        clients={
            "server": InMemoryMcpClient(
                tools=(
                    McpToolSchema(name="search", description="Search docs"),
                    McpToolSchema(name="dangerous.delete_all", description="Dangerous"),
                )
            )
        },
        tool_allowlist_by_agent={"researcher": frozenset({"server.search"})},
        gateway=allow_gateway(),
        role=Role.OPERATOR,
    )
    await service.start()
    source = service.capability_manifest_source()

    before = source.manifests_for_tenant(TENANT_ID)
    before_tools = {
        item["id"]
        for item in cast(tuple[Mapping[str, object], ...], before["capabilities"])
    }
    assert before_tools == {"server.search"}

    await service.reload(
        (remote_server("server2"),),
        clients={"server2": InMemoryMcpClient(tools=(McpToolSchema(name="summarize"),))},
    )

    after = source.manifests_for_tenant(TENANT_ID)
    after_tools = {
        item["id"]
        for item in cast(tuple[Mapping[str, object], ...], after["capabilities"])
    }
    assert after_tools == set()
