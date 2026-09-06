from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.gateway import CapabilityGateway
from agent_hub.capabilities.policy import CapabilityPolicy
from agent_hub.mcp.client import McpClient, SseMcpClient, StdioMcpClient, StreamableHttpMcpClient
from agent_hub.mcp.service import McpService
from agent_hub.mcp.types import McpServerDefinition, McpTransportKind
from agent_hub.runtime.contracts import JsonValue


class McpConfigServer(Protocol):
    id: str
    allowed_tools: Sequence[str]
    transport: str
    command: str | None
    args: Sequence[str]
    url: str | None
    executable_allowlist: Sequence[str]
    domain_allowlist: Sequence[str]
    timeout_seconds: float


class McpConfigService(Protocol):
    async def list_mcp_servers(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> Sequence[Any]: ...


McpClientFactory = Callable[[McpServerDefinition], McpClient]


class RuntimeMcpService:
    def __init__(
        self,
        *,
        tenant_id: UUID,
        admin_service: McpConfigService,
        run_repository: object,
        client_factory: McpClientFactory | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._admin_service = admin_service
        self._run_repository = run_repository
        self._client_factory = client_factory
        self._service: McpService | None = None

    async def start(self) -> None:
        await self.reload(self._tenant_id)

    async def reload(self, tenant_id: UUID | None = None) -> None:
        if tenant_id is not None and tenant_id != self._tenant_id:
            return
        try:
            configured_servers = await self._admin_service.list_mcp_servers(
                tenant_id=self._tenant_id,
            )
        except Exception:  # noqa: BLE001 - live MCP planning context is optional.
            self._service = None
            return
        definitions = _server_definitions(self._tenant_id, configured_servers)
        if not definitions:
            self._service = None
            return
        clients = {
            server.id: (self._client_factory or _default_client_for_server)(server)
            for server in definitions
        }
        service = McpService(
            servers=definitions,
            clients=clients,
            tool_allowlist_by_agent={
                "runtime_planning": _projected_tool_names(configured_servers),
            },
            gateway=CapabilityGateway(
                CapabilityPolicy(()),
                ApprovalService(InMemoryApprovalStore()),
                self._run_repository,
            ),
            role=Role.OPERATOR,
        )
        try:
            await service.start()
        except Exception:  # noqa: BLE001 - a broken MCP server must not break startup.
            self._service = None
            return
        self._service = service

    def capability_manifest_source(self) -> RuntimeMcpService:
        return self

    def manifests_for_tenant(self, tenant_id: UUID) -> Mapping[str, JsonValue]:
        if self._service is None:
            return _empty_manifest()
        return self._service.capability_manifest_source().manifests_for_tenant(tenant_id)


async def build_runtime_mcp_service(
    *,
    tenant_id: UUID,
    admin_service: McpConfigService,
    run_repository: object,
    client_factory: McpClientFactory | None = None,
) -> RuntimeMcpService:
    service = RuntimeMcpService(
        tenant_id=tenant_id,
        admin_service=admin_service,
        run_repository=run_repository,
        client_factory=client_factory,
    )
    await service.start()
    return service


def build_runtime_mcp_service_sync(
    *,
    tenant_id: UUID,
    admin_service: McpConfigService,
    run_repository: object,
    client_factory: McpClientFactory | None = None,
) -> RuntimeMcpService:
    return asyncio.run(
        build_runtime_mcp_service(
            tenant_id=tenant_id,
            admin_service=admin_service,
            run_repository=run_repository,
            client_factory=client_factory,
        )
    )


def _server_definitions(
    tenant_id: UUID,
    servers: Sequence[Any],
) -> tuple[McpServerDefinition, ...]:
    definitions: list[McpServerDefinition] = []
    for server in servers:
        try:
            definitions.append(
                McpServerDefinition(
                    tenant_id=tenant_id,
                    id=server.id,
                    transport=McpTransportKind(server.transport),
                    command=server.command,
                    args=tuple(server.args),
                    url=server.url,
                    executable_allowlist=tuple(server.executable_allowlist),
                    domain_allowlist=tuple(server.domain_allowlist),
                    timeout_seconds=server.timeout_seconds,
                )
            )
        except (AttributeError, TypeError, ValueError, ValidationError):
            continue
    return tuple(definitions)


def _projected_tool_names(servers: Sequence[Any]) -> frozenset[str]:
    names: set[str] = set()
    for server in servers:
        try:
            server_id = server.id
            allowed_tools = server.allowed_tools
        except AttributeError:
            continue
        if not isinstance(server_id, str) or isinstance(allowed_tools, str):
            continue
        names.update(
            f"{server_id}.{tool_name}"
            for tool_name in allowed_tools
            if isinstance(tool_name, str) and tool_name
        )
    return frozenset(names)


def _default_client_for_server(server: McpServerDefinition) -> McpClient:
    if server.transport is McpTransportKind.STDIO:
        return StdioMcpClient()
    if server.transport is McpTransportKind.SSE:
        return SseMcpClient()
    return StreamableHttpMcpClient()


def _empty_manifest() -> Mapping[str, JsonValue]:
    return {
        "schema_version": 1,
        "capabilities": (),
    }


__all__ = [
    "McpClientFactory",
    "McpConfigServer",
    "McpConfigService",
    "RuntimeMcpService",
    "build_runtime_mcp_service",
    "build_runtime_mcp_service_sync",
]
