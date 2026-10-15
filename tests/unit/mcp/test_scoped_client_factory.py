from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest

from agent_hub.mcp import runtime
from agent_hub.mcp.client import (
    McpClient,
    SseMcpClient,
    StdioMcpClient,
    StdioProcessFactory,
    StreamableHttpMcpClient,
)
from agent_hub.mcp.types import McpServerDefinition, McpTransportKind

_TENANT_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_StartupCall = tuple[str, tuple[str, ...], int, int, int]


class _FailingProcessFactory:
    def __init__(self, *, falsey: bool = False) -> None:
        self.falsey = falsey
        self.error = OSError("scoped launcher refused startup")
        self.calls: list[_StartupCall] = []

    def __bool__(self) -> bool:
        return not self.falsey

    async def __call__(
        self, program: str, *args: str, stdin: int, stdout: int, stderr: int
    ) -> asyncio.subprocess.Process:
        self.calls.append((program, args, stdin, stdout, stderr))
        raise self.error


class _AdminService:
    def __init__(self, servers: Sequence[McpServerDefinition]) -> None:
        self.servers = servers

    async def list_mcp_servers(
        self, *, tenant_id: UUID | None = None
    ) -> Sequence[SimpleNamespace]:
        return tuple(
            SimpleNamespace(
                id=server.id,
                transport=server.transport.value,
                command=server.command,
                args=server.args,
                url=server.url,
                executable_allowlist=server.executable_allowlist,
                domain_allowlist=server.domain_allowlist,
                timeout_seconds=server.timeout_seconds,
                allowed_tools=("ping",),
            )
            for server in self.servers
            if server.tenant_id == tenant_id
        )


@pytest.fixture(autouse=True)
def _reject_host_spawn(monkeypatch: pytest.MonkeyPatch) -> list[_StartupCall]:
    calls: list[_StartupCall] = []

    async def host_spawn(
        program: str, *args: str, stdin: int, stdout: int, stderr: int
    ) -> asyncio.subprocess.Process:
        calls.append((program, args, stdin, stdout, stderr))
        raise AssertionError("scoped MCP must never fall back to host spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", host_spawn)
    return calls


def _stdio_server(tenant_id: UUID, server_id: str) -> McpServerDefinition:
    return McpServerDefinition(
        tenant_id=tenant_id,
        id=server_id,
        transport=McpTransportKind.STDIO,
        command="scoped-mcp-peer",
        args=("argument with spaces", str(tenant_id), server_id),
        executable_allowlist=("scoped-mcp-peer",),
    )


@pytest.mark.parametrize("server_id", ["peer-a", "peer-b"])
async def test_each_tenant_server_binds_its_own_stdio_startup_factory(
    _reject_host_spawn: list[_StartupCall], server_id: str
) -> None:
    server_a = _stdio_server(_TENANT_A, "peer-a")
    server_b = _stdio_server(_TENANT_B, server_id)
    factory_a = _FailingProcessFactory()
    factory_b = _FailingProcessFactory()
    factories = {
        (_TENANT_A, "peer-a"): factory_a,
        (_TENANT_B, server_id): factory_b,
    }
    resolved: list[McpServerDefinition] = []

    def resolver(server: McpServerDefinition) -> StdioProcessFactory:
        resolved.append(server)
        return factories[(server.tenant_id, server.id)]

    client_factory = runtime.build_scoped_mcp_client_factory(resolver)
    client_a = client_factory(server_a)
    client_b = client_factory(server_b)
    assert isinstance(client_a, StdioMcpClient)
    assert isinstance(client_b, StdioMcpClient)
    assert resolved == [server_a, server_b]
    assert not factory_a.calls and not factory_b.calls

    for client, server, factory in (
        (client_b, server_b, factory_b),
        (client_a, server_a, factory_a),
    ):
        with pytest.raises(OSError) as caught:
            await client.health(server)
        assert caught.value is factory.error

    assert factory_a.calls == [
        (
            "scoped-mcp-peer",
            ("argument with spaces", str(_TENANT_A), "peer-a"),
            asyncio.subprocess.PIPE,
            asyncio.subprocess.PIPE,
            asyncio.subprocess.DEVNULL,
        )
    ]
    assert factory_b.calls == [
        (
            "scoped-mcp-peer",
            ("argument with spaces", str(_TENANT_B), server_id),
            asyncio.subprocess.PIPE,
            asyncio.subprocess.PIPE,
            asyncio.subprocess.DEVNULL,
        )
    ]
    assert not _reject_host_spawn


@pytest.mark.parametrize(
    ("transport", "client_type"),
    [
        (McpTransportKind.SSE, SseMcpClient),
        (McpTransportKind.STREAMABLE_HTTP, StreamableHttpMcpClient),
    ],
)
def test_http_transports_keep_default_clients_without_stdio_resolution(
    transport: McpTransportKind, client_type: type[McpClient]
) -> None:
    def resolver(server: McpServerDefinition) -> StdioProcessFactory:
        raise AssertionError("HTTP MCP must not resolve a stdio factory")

    server = McpServerDefinition(
        tenant_id=_TENANT_A,
        id="remote-peer",
        transport=transport,
        url="https://mcp.example.com/rpc",
        domain_allowlist=("mcp.example.com",),
    )
    client_factory = runtime.build_scoped_mcp_client_factory(resolver)

    assert isinstance(client_factory(server), client_type)


@pytest.mark.parametrize("error", [PermissionError("scope denied"), RuntimeError("scope missing")])
async def test_resolver_failure_propagates_from_runtime_without_host_fallback(
    _reject_host_spawn: list[_StartupCall], error: Exception
) -> None:
    server = _stdio_server(_TENANT_A, "peer-a")
    resolved: list[McpServerDefinition] = []

    def resolver(definition: McpServerDefinition) -> StdioProcessFactory:
        resolved.append(definition)
        raise error

    service = runtime.RuntimeMcpService(
        tenant_id=_TENANT_A,
        admin_service=_AdminService((server,)),
        run_repository=object(),
        client_factory=runtime.build_scoped_mcp_client_factory(resolver),
    )

    with pytest.raises(type(error)) as caught:
        await service.start()
    assert caught.value is error
    assert resolved == [server]
    assert not _reject_host_spawn


@pytest.mark.parametrize("invalid_factory", [None, False, object()])
async def test_invalid_resolved_factory_is_rejected_before_runtime_startup(
    _reject_host_spawn: list[_StartupCall], invalid_factory: object
) -> None:
    server = _stdio_server(_TENANT_A, "peer-a")

    def resolver(definition: McpServerDefinition) -> StdioProcessFactory:
        return cast(StdioProcessFactory, invalid_factory)

    service = runtime.RuntimeMcpService(
        tenant_id=_TENANT_A,
        admin_service=_AdminService((server,)),
        run_repository=object(),
        client_factory=runtime.build_scoped_mcp_client_factory(resolver),
    )

    with pytest.raises(TypeError, match="(?i)stdio.*factory.*callable"):
        await service.start()
    assert not _reject_host_spawn


async def test_falsey_stdio_factory_reaches_the_real_consumer_without_host_fallback(
    _reject_host_spawn: list[_StartupCall],
) -> None:
    factory = _FailingProcessFactory(falsey=True)
    client_factory = runtime.build_scoped_mcp_client_factory(lambda server: factory)
    server = _stdio_server(_TENANT_A, "peer-a")

    with pytest.raises(OSError) as caught:
        await client_factory(server).health(server)
    assert caught.value is factory.error
    assert len(factory.calls) == 1
    assert not _reject_host_spawn


async def test_runtime_preserves_explicit_falsey_client_factory_for_each_tenant(
    _reject_host_spawn: list[_StartupCall],
) -> None:
    factory = _FailingProcessFactory()
    resolved: list[McpServerDefinition] = []

    class FalseyClientFactory:
        def __bool__(self) -> bool:
            return False

        def __call__(self, server: McpServerDefinition) -> McpClient:
            resolved.append(server)
            return StdioMcpClient(process_factory=factory)

    server_a = _stdio_server(_TENANT_A, "peer-a")
    server_b = _stdio_server(_TENANT_B, "peer-b")
    service = runtime.RuntimeMcpService(
        tenant_id=_TENANT_A,
        admin_service=_AdminService((server_a, server_b)),
        run_repository=object(),
        client_factory=FalseyClientFactory(),
    )

    await service.start()
    await service.ensure_tenant_loaded(_TENANT_B)

    assert resolved == [server_a, server_b]
    assert [call[:2] for call in factory.calls] == [
        ("scoped-mcp-peer", ("argument with spaces", str(_TENANT_A), "peer-a")),
        ("scoped-mcp-peer", ("argument with spaces", str(_TENANT_B), "peer-b")),
    ]
    assert not _reject_host_spawn


async def test_failed_scope_reload_revokes_previous_tool_visibility() -> None:
    from agent_hub.mcp.client import InMemoryMcpClient
    from agent_hub.mcp.types import McpToolSchema

    permitted = True
    server = _stdio_server(_TENANT_A, "peer-a")
    peer = InMemoryMcpClient(tools=(McpToolSchema(name="ping", input_schema={"type": "object"}),))

    def factory(definition: McpServerDefinition) -> McpClient:
        if not permitted:
            raise OSError("binding revoked")
        return peer

    service = runtime.RuntimeMcpService(
        tenant_id=_TENANT_A, admin_service=_AdminService((server,)),
        run_repository=object(), client_factory=factory,
    )
    await service.start()
    assert service.is_available(_TENANT_A, "peer-a.ping")
    permitted = False
    with pytest.raises(OSError, match="revoked"):
        await service.reload(_TENANT_A)
    assert not service.is_available(_TENANT_A, "peer-a.ping")
