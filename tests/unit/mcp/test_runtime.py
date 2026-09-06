from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from agent_hub.mcp.client import InMemoryMcpClient
from agent_hub.mcp.runtime import build_runtime_mcp_service
from agent_hub.mcp.types import McpToolSchema, McpTransportKind

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")


class FakeAdminService:
    def __init__(self, servers: tuple[object, ...]) -> None:
        self.servers = servers
        self.tenants: list[UUID] = []

    async def list_mcp_servers(self, *, tenant_id: UUID | None = None) -> tuple[object, ...]:
        assert tenant_id is not None
        self.tenants.append(tenant_id)
        return self.servers


def server_config(
    server_id: str,
    *,
    allowed_tools: list[str],
    transport: str = "streamable_http",
    command: str | None = None,
    url: str | None = "https://search.example.com/mcp",
    executable_allowlist: list[str] | None = None,
    domain_allowlist: list[str] | None = None,
) -> object:
    return SimpleNamespace(
        id=server_id,
        name=server_id,
        health="configured",
        allowed_tools=allowed_tools,
        transport=transport,
        command=command,
        args=[],
        url=url,
        executable_allowlist=[] if executable_allowlist is None else executable_allowlist,
        domain_allowlist=["example.com"] if domain_allowlist is None else domain_allowlist,
        timeout_seconds=1,
    )


async def test_runtime_mcp_service_discovers_saved_servers_for_manifest_source() -> None:
    admin_service = FakeAdminService(
        (
            server_config(
                "search",
                allowed_tools=["web_search"],
            ),
        )
    )

    service = await build_runtime_mcp_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        run_repository=object(),
        client_factory=lambda server: InMemoryMcpClient(
            tools=(
                McpToolSchema(name="web_search"),
                McpToolSchema(name="dangerous_delete"),
            )
        ),
    )

    assert service is not None
    manifest = service.capability_manifest_source().manifests_for_tenant(TENANT_ID)
    assert manifest["capabilities"] == (
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
    )
    assert admin_service.tenants == [TENANT_ID]


async def test_runtime_mcp_service_skips_invalid_saved_servers() -> None:
    admin_service = FakeAdminService(
        (
            server_config(
                "files",
                allowed_tools=["read_file"],
                transport=McpTransportKind.STDIO.value,
                command=None,
                url=None,
                executable_allowlist=[],
            ),
        )
    )

    service = await build_runtime_mcp_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        run_repository=object(),
        client_factory=lambda server: InMemoryMcpClient(tools=(McpToolSchema(name="read_file"),)),
    )

    assert service.capability_manifest_source().manifests_for_tenant(TENANT_ID) == {
        "schema_version": 1,
        "capabilities": (),
    }


async def test_runtime_mcp_service_skips_malformed_projection_config() -> None:
    admin_service = FakeAdminService(
        (
            server_config("search", allowed_tools=["web_search"]),
            SimpleNamespace(
                id="broken",
                transport="streamable_http",
                command=None,
                args=[],
                url="https://broken.example.com/mcp",
                executable_allowlist=[],
                domain_allowlist=["broken.example.com"],
                timeout_seconds=1,
            ),
        )
    )

    service = await build_runtime_mcp_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        run_repository=object(),
        client_factory=lambda server: InMemoryMcpClient(tools=(McpToolSchema(name="web_search"),)),
    )

    manifest = service.capability_manifest_source().manifests_for_tenant(TENANT_ID)

    assert manifest["capabilities"] == (
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
    )
