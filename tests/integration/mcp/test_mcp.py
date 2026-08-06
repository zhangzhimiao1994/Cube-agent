from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.gateway import CapabilityGateway
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.types import PolicyEffect
from agent_hub.mcp.client import (
    InMemoryMcpClient,
    SseMcpClient,
    StdioMcpClient,
    StreamableHttpMcpClient,
)
from agent_hub.mcp.service import McpService, McpTimeout, McpToolDenied
from agent_hub.mcp.types import (
    McpInvocationContext,
    McpInvocationResult,
    McpServerDefinition,
    McpToolSchema,
    McpTransportKind,
)
from agent_hub.security.secrets import SecretReference

TENANT_ID = UUID("66666666-6666-4666-8666-666666666666")
USER_ID = UUID("77777777-7777-4777-8777-777777777777")
RUN_ID = UUID("88888888-8888-4888-8888-888888888888")
OTHER_TENANT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OAUTH_SECRET_ID = UUID("99999999-9999-4999-8999-999999999999")
HEADER_SECRET_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class NoopRunRepository:
    async def begin_capability_approval(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("tests do not exercise approval state")

    async def resolve_capability_approval(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("tests do not exercise approval state")


def context(agent_id: str = "researcher") -> McpInvocationContext:
    return McpInvocationContext(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        run_id=RUN_ID,
        agent_id=agent_id,
        idempotency_key=f"mcp:{agent_id}",
    )


def gateway(*, allow: bool, tenant_id: UUID = TENANT_ID) -> CapabilityGateway:
    rules: tuple[CapabilityRule, ...]
    if allow:
        rules = (
            CapabilityRule(
                tenant_id=tenant_id,
                role=Role.OPERATOR,
                agent_id="researcher",
                capability="mcp",
                operation="invoke",
                resource_prefix="mcp/server",
                effect=PolicyEffect.ALLOW,
            ),
        )
    else:
        rules = ()
    return CapabilityGateway(
        CapabilityPolicy(rules),
        ApprovalService(InMemoryApprovalStore()),
        NoopRunRepository(),
    )


def remote_server(server_id: str = "server", *, timeout_seconds: float = 10.0) -> McpServerDefinition:
    return McpServerDefinition(
        tenant_id=TENANT_ID,
        id=server_id,
        transport=McpTransportKind.STREAMABLE_HTTP,
        url=f"https://{server_id}.example.com/mcp",
        domain_allowlist=("example.com",),
        oauth_token_ref=SecretReference(TENANT_ID, OAUTH_SECRET_ID),
        header_secret_refs={"X-API-Key": SecretReference(TENANT_ID, HEADER_SECRET_ID)},
        timeout_seconds=timeout_seconds,
    )


async def mcp_service(
    *,
    allow_gateway: bool = True,
    client: InMemoryMcpClient | None = None,
) -> McpService:
    client = client or InMemoryMcpClient(
        tools=(
            McpToolSchema(name="search", description="Search docs", input_schema={"type": "object"}),
            McpToolSchema(name="dangerous.delete_all", description="Dangerous"),
        )
    )
    service = McpService(
        servers=(remote_server(),),
        clients={"server": client},
        tool_allowlist_by_agent={"researcher": frozenset({"server.search"})},
        gateway=gateway(allow=allow_gateway),
        role=Role.OPERATOR,
    )
    await service.start()
    return service


async def test_unapproved_mcp_tool_is_not_projected() -> None:
    service = await mcp_service()

    tools = await service.tools_for_agent("researcher")

    assert {tool.qualified_name for tool in tools} == {"server.search"}
    assert "server.dangerous.delete_all" not in {tool.qualified_name for tool in tools}


async def test_config_generation_reloads_remote_server() -> None:
    service = await mcp_service()
    before = await service.generation_id()
    new_client = InMemoryMcpClient(tools=(McpToolSchema(name="summarize"),))

    await service.reload((remote_server("server2"),), clients={"server2": new_client})

    assert await service.generation_id() != before
    snapshot = await service.snapshot()
    assert {tool.qualified_name for tool in snapshot.tools} == {"server2.summarize"}


def test_stdio_transport_requires_executable_allowlist() -> None:
    with pytest.raises(ValidationError, match="allowlist"):
        McpServerDefinition(
            tenant_id=TENANT_ID,
            id="local",
            transport=McpTransportKind.STDIO,
            command="/usr/bin/python",
            executable_allowlist=("/usr/bin/node",),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://server.example.com/mcp",
        "https://user:pass@server.example.com/mcp",
        "https://localhost/mcp",
        "https://10.0.0.1/mcp",
        "https://evil.com/mcp",
    ],
)
def test_remote_transport_requires_https_safe_allowlisted_domain(url: str) -> None:
    with pytest.raises(ValidationError):
        McpServerDefinition(
            tenant_id=TENANT_ID,
            id="remote",
            transport=McpTransportKind.SSE,
            url=url,
            domain_allowlist=("example.com",),
        )


def test_secret_refs_are_references_not_values() -> None:
    server = remote_server()
    assert server.oauth_token_ref == SecretReference(TENANT_ID, OAUTH_SECRET_ID)
    assert server.header_secret_refs["X-API-Key"] == SecretReference(TENANT_ID, HEADER_SECRET_ID)
    parsed = McpServerDefinition.model_validate(
        {
            "tenant_id": TENANT_ID,
            "id": "parsed",
            "transport": McpTransportKind.SSE,
            "url": "https://server.example.com/mcp",
            "domain_allowlist": ("example.com",),
            "oauth_token_ref": f"secret://{OAUTH_SECRET_ID}",
        },
        strict=True,
    )
    assert parsed.oauth_token_ref == SecretReference(TENANT_ID, OAUTH_SECRET_ID)
    with pytest.raises(ValidationError):
        McpServerDefinition.model_validate(
            {
                "tenant_id": TENANT_ID,
                "id": "remote",
                "transport": McpTransportKind.SSE,
                "url": "https://server.example.com/mcp",
                "domain_allowlist": ("example.com",),
                "oauth_token_ref": "sk-real-secret-value",
            },
            strict=True,
        )


async def test_transport_specific_client_adapters_validate_transport() -> None:
    transport = InMemoryMcpClient(
        tools=(McpToolSchema(name="ping"),),
        responses={"ping": McpInvocationResult(content={"pong": True})},
    )
    stdio = McpServerDefinition(
        tenant_id=TENANT_ID,
        id="local",
        transport=McpTransportKind.STDIO,
        command="/usr/bin/mcp-server",
        executable_allowlist=("/usr/bin/mcp-server",),
    )
    sse = McpServerDefinition(
        tenant_id=TENANT_ID,
        id="sse",
        transport=McpTransportKind.SSE,
        url="https://sse.example.com/mcp",
        domain_allowlist=("example.com",),
    )
    http = remote_server()

    assert await StdioMcpClient(transport).health(stdio) == "healthy"
    assert await SseMcpClient(transport).discover_tools(sse) == (McpToolSchema(name="ping"),)
    assert await StreamableHttpMcpClient(transport).invoke(http, "ping", {}) == McpInvocationResult(
        content={"pong": True}
    )
    with pytest.raises(ValueError):
        await StdioMcpClient(transport).health(http)


async def test_invocation_is_routed_through_capability_gateway() -> None:
    client = InMemoryMcpClient(tools=(McpToolSchema(name="search"),))
    service = await mcp_service(allow_gateway=True, client=client)

    result = await service.invoke_tool("server.search", {"query": "agent"}, context=context())

    assert result.content == {"ok": True}
    assert client.invocations == [("search", {"query": "agent"})]
    assert (await service.audit_events())[-1].kind == "mcp.invoked"


async def test_gateway_denial_prevents_client_invocation() -> None:
    client = InMemoryMcpClient(tools=(McpToolSchema(name="search"),))
    service = await mcp_service(allow_gateway=False, client=client)

    with pytest.raises(McpToolDenied):
        await service.invoke_tool("server.search", {"query": "agent"}, context=context())

    assert client.invocations == []
    assert (await service.audit_events())[-1].status == "deny"


async def test_cross_tenant_context_cannot_invoke_server_even_if_gateway_allows() -> None:
    client = InMemoryMcpClient(tools=(McpToolSchema(name="search"),))
    service = McpService(
        servers=(remote_server(),),
        clients={"server": client},
        tool_allowlist_by_agent={"researcher": frozenset({"server.search"})},
        gateway=gateway(allow=True, tenant_id=OTHER_TENANT_ID),
        role=Role.OPERATOR,
    )
    await service.start()

    with pytest.raises(McpToolDenied, match="tenant"):
        await service.invoke_tool(
            "server.search",
            {"query": "agent"},
            context=context().model_copy(update={"tenant_id": OTHER_TENANT_ID}),
        )

    assert client.invocations == []
    assert (await service.audit_events())[-1].status == "tenant_mismatch"


async def test_client_receives_sanitized_arguments_used_for_capability_request() -> None:
    class FlippingMapping(Mapping[str, object]):
        def __init__(self) -> None:
            self.calls = 0

        def __getitem__(self, key: str) -> object:
            if key != "query":
                raise KeyError(key)
            self.calls += 1
            if self.calls == 1:
                return "safe"
            return object()

        def __iter__(self) -> Iterator[str]:
            return iter(("query",))

        def __len__(self) -> int:
            return 1

    client = InMemoryMcpClient(tools=(McpToolSchema(name="search"),))
    service = await mcp_service(client=client)

    await service.invoke_tool("server.search", FlippingMapping(), context=context())

    assert client.invocations == [("search", {"query": "safe"})]


async def test_reload_rejects_duplicate_server_and_tool_names() -> None:
    client = InMemoryMcpClient(tools=(McpToolSchema(name="search"),))
    service = await mcp_service()

    with pytest.raises(ValueError, match="duplicate MCP server"):
        await service.reload((remote_server(), remote_server()), clients={"server": client})

    duplicate_tool_client = InMemoryMcpClient(
        tools=(McpToolSchema(name="search"), McpToolSchema(name="search"))
    )
    with pytest.raises(ValueError, match="duplicate MCP tool"):
        await service.reload((remote_server(),), clients={"server": duplicate_tool_client})


async def test_timeout_and_cancellation_are_audited() -> None:
    slow = InMemoryMcpClient(tools=(McpToolSchema(name="search"),), delay_seconds=10)
    timeout_service = McpService(
        servers=(remote_server(timeout_seconds=0.01),),
        clients={"server": slow},
        tool_allowlist_by_agent={"researcher": frozenset({"server.search"})},
        gateway=gateway(allow=True),
        role=Role.OPERATOR,
    )
    await timeout_service.start()
    with pytest.raises(McpTimeout):
        await timeout_service.invoke_tool("server.search", {"query": "agent"}, context=context())
    assert (await timeout_service.audit_events())[-1].kind == "mcp.timeout"

    cancellable = InMemoryMcpClient(tools=(McpToolSchema(name="search"),), delay_seconds=10)
    cancel_service = await mcp_service(client=cancellable)
    task = asyncio.create_task(
        cancel_service.invoke_tool("server.search", {"query": "agent"}, context=context())
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancellable.cancelled is True
    assert (await cancel_service.audit_events())[-1].kind == "mcp.cancelled"
