from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from agent_hub.mcp.client import InMemoryMcpClient
from agent_hub.mcp.runtime import RuntimeMcpService, build_runtime_mcp_service
from agent_hub.mcp.types import McpInvocationResult, McpToolSchema, McpTransportKind

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


class FakeAdminService:
    def __init__(self, servers: tuple[object, ...]) -> None:
        self.servers = servers
        self.tenants: list[UUID] = []

    async def list_mcp_servers(self, *, tenant_id: UUID | None = None) -> tuple[object, ...]:
        assert tenant_id is not None
        self.tenants.append(tenant_id)
        return self.servers


class TenantMappedAdminService:
    def __init__(self, servers_by_tenant: dict[UUID, tuple[object, ...]]) -> None:
        self.servers_by_tenant = servers_by_tenant
        self.tenants: list[UUID] = []

    async def list_mcp_servers(self, *, tenant_id: UUID | None = None) -> tuple[object, ...]:
        assert tenant_id is not None
        self.tenants.append(tenant_id)
        return self.servers_by_tenant.get(tenant_id, ())


class BlockingTenantMappedAdminService(TenantMappedAdminService):
    def __init__(self, servers_by_tenant: dict[UUID, tuple[object, ...]]) -> None:
        super().__init__(servers_by_tenant)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def list_mcp_servers(self, *, tenant_id: UUID | None = None) -> tuple[object, ...]:
        assert tenant_id is not None
        self.entered.set()
        await self.release.wait()
        return await super().list_mcp_servers(tenant_id=tenant_id)


class FailingOnceTenantMappedAdminService(TenantMappedAdminService):
    def __init__(self, servers_by_tenant: dict[UUID, tuple[object, ...]]) -> None:
        super().__init__(servers_by_tenant)
        self.failures_remaining = 1

    async def list_mcp_servers(self, *, tenant_id: UUID | None = None) -> tuple[object, ...]:
        assert tenant_id is not None
        self.tenants.append(tenant_id)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("temporary mcp reload failure")
        return self.servers_by_tenant.get(tenant_id, ())


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


async def test_runtime_mcp_service_invokes_projected_tool_after_harness_policy() -> None:
    admin_service = FakeAdminService((server_config("search", allowed_tools=["web_search"]),))
    client = InMemoryMcpClient(
        tools=(McpToolSchema(name="web_search"),),
        responses={"web_search": McpInvocationResult(content={"answer": "42"})},
    )
    service = await build_runtime_mcp_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        run_repository=object(),
        client_factory=lambda _server: client,
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="runtime_planning",
        name="search.web_search",
        arguments={"query": "mofang"},
        idempotency_key="mcp_1",
    )

    assert result == {"answer": "42"}
    assert client.invocations == [("web_search", {"query": "mofang"})]


async def test_runtime_mcp_service_lazy_loads_invocation_tenant() -> None:
    admin_service = TenantMappedAdminService(
        {
            TENANT_ID: (),
            OTHER_TENANT_ID: (server_config("search", allowed_tools=["web_search"]),),
        }
    )
    clients: dict[UUID, InMemoryMcpClient] = {}

    def client_factory(server: Any) -> InMemoryMcpClient:
        tenant_id = server.tenant_id
        client = InMemoryMcpClient(
            tools=(McpToolSchema(name="web_search"),),
            responses={"web_search": McpInvocationResult(content={"answer": str(tenant_id)})},
        )
        clients[tenant_id] = client
        return client

    service = await build_runtime_mcp_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        run_repository=object(),
        client_factory=client_factory,
    )

    result = await service.invoke(
        tenant_id=OTHER_TENANT_ID,
        user_id=OTHER_TENANT_ID,
        run_id=OTHER_TENANT_ID,
        actor="runtime_planning",
        name="search.web_search",
        arguments={"query": "tenant"},
        idempotency_key="mcp_tenant",
    )

    assert result == {"answer": str(OTHER_TENANT_ID)}
    assert admin_service.tenants == [TENANT_ID, OTHER_TENANT_ID]
    assert clients[OTHER_TENANT_ID].invocations == [("web_search", {"query": "tenant"})]


async def test_runtime_mcp_reload_without_tenant_refreshes_loaded_tenants() -> None:
    admin_service = TenantMappedAdminService(
        {
            TENANT_ID: (),
            OTHER_TENANT_ID: (server_config("search", allowed_tools=["web_search"]),),
        }
    )
    service = await build_runtime_mcp_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        run_repository=object(),
        client_factory=lambda _server: InMemoryMcpClient(
            tools=(McpToolSchema(name="web_search"),)
        ),
    )
    await service.ensure_tenant_loaded(OTHER_TENANT_ID)

    admin_service.servers_by_tenant[OTHER_TENANT_ID] = ()
    await service.reload()

    assert admin_service.tenants == [
        TENANT_ID,
        OTHER_TENANT_ID,
        TENANT_ID,
        OTHER_TENANT_ID,
    ]
    assert service.manifests_for_tenant(OTHER_TENANT_ID) == {
        "schema_version": 1,
        "capabilities": (),
    }


async def test_runtime_mcp_service_coalesces_concurrent_reload_for_same_tenant() -> None:
    admin_service = BlockingTenantMappedAdminService(
        {
            OTHER_TENANT_ID: (server_config("search", allowed_tools=["web_search"]),),
        }
    )
    service = RuntimeMcpService(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        run_repository=object(),
        client_factory=lambda _server: InMemoryMcpClient(
            tools=(McpToolSchema(name="web_search"),)
        ),
    )

    reloads = [
        asyncio.create_task(service.reload(OTHER_TENANT_ID)),
        asyncio.create_task(service.reload(OTHER_TENANT_ID)),
    ]
    await admin_service.entered.wait()
    admin_service.release.set()
    await asyncio.gather(*reloads)

    assert admin_service.tenants == [OTHER_TENANT_ID]


async def test_runtime_mcp_reload_waiter_cancellation_does_not_cancel_shared_reload() -> None:
    admin_service = BlockingTenantMappedAdminService(
        {
            OTHER_TENANT_ID: (server_config("search", allowed_tools=["web_search"]),),
        }
    )
    service = RuntimeMcpService(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        run_repository=object(),
        client_factory=lambda _server: InMemoryMcpClient(
            tools=(McpToolSchema(name="web_search"),)
        ),
    )

    leader = asyncio.create_task(service.reload(OTHER_TENANT_ID))
    await admin_service.entered.wait()
    follower = asyncio.create_task(service.reload(OTHER_TENANT_ID))
    await asyncio.sleep(0)

    follower.cancel()
    try:
        await follower
    except asyncio.CancelledError:
        pass
    admin_service.release.set()
    await leader

    assert admin_service.tenants == [OTHER_TENANT_ID]
    assert service.is_available(OTHER_TENANT_ID, "search.web_search") is True


async def test_runtime_mcp_reload_cleans_inflight_task_after_only_waiter_cancelled() -> None:
    admin_service = BlockingTenantMappedAdminService(
        {
            OTHER_TENANT_ID: (server_config("search", allowed_tools=["web_search"]),),
        }
    )
    service = RuntimeMcpService(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        run_repository=object(),
        client_factory=lambda _server: InMemoryMcpClient(
            tools=(McpToolSchema(name="web_search"),)
        ),
    )

    waiter = asyncio.create_task(service.reload(OTHER_TENANT_ID))
    await admin_service.entered.wait()
    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass
    admin_service.release.set()
    for _ in range(10):
        if service._reload_tasks_by_tenant == {}:
            break
        await asyncio.sleep(0)

    assert service._reload_tasks_by_tenant == {}
    assert service.is_available(OTHER_TENANT_ID, "search.web_search") is True


async def test_runtime_mcp_reload_failure_does_not_block_later_retry() -> None:
    admin_service = FailingOnceTenantMappedAdminService(
        {
            OTHER_TENANT_ID: (server_config("search", allowed_tools=["web_search"]),),
        }
    )
    service = RuntimeMcpService(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        run_repository=object(),
        client_factory=lambda _server: InMemoryMcpClient(
            tools=(McpToolSchema(name="web_search"),)
        ),
    )

    await service.reload(OTHER_TENANT_ID)
    await service.reload(OTHER_TENANT_ID)

    assert admin_service.tenants == [OTHER_TENANT_ID, OTHER_TENANT_ID]
    assert service.is_available(OTHER_TENANT_ID, "search.web_search") is True
