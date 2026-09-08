from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import ValidationError

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.gateway import CapabilityGateway
from agent_hub.capabilities.policy import CapabilityPolicy
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.mcp.client import McpClient, SseMcpClient, StdioMcpClient, StreamableHttpMcpClient
from agent_hub.mcp.service import McpService, McpTimeout, McpToolDenied, McpToolNotFound
from agent_hub.mcp.types import McpInvocationContext, McpServerDefinition, McpTransportKind
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
        self._services_by_tenant: dict[UUID, McpService | None] = {}

    async def start(self) -> None:
        await self.reload(self._tenant_id)

    async def reload(self, tenant_id: UUID | None = None) -> None:
        if tenant_id is None:
            for target_tenant_id in self._loaded_tenant_ids():
                await self.reload(target_tenant_id)
            return
        target_tenant_id = self._tenant_id if tenant_id is None else tenant_id
        try:
            configured_servers = await self._admin_service.list_mcp_servers(
                tenant_id=target_tenant_id,
            )
        except Exception:  # noqa: BLE001 - live MCP planning context is optional.
            self._services_by_tenant[target_tenant_id] = None
            return
        definitions = _server_definitions(target_tenant_id, configured_servers)
        if not definitions:
            self._services_by_tenant[target_tenant_id] = None
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
            self._services_by_tenant[target_tenant_id] = None
            return
        self._services_by_tenant[target_tenant_id] = service

    async def ensure_tenant_loaded(self, tenant_id: UUID) -> None:
        if tenant_id not in self._services_by_tenant:
            await self.reload(tenant_id)

    def _loaded_tenant_ids(self) -> tuple[UUID, ...]:
        return tuple(dict.fromkeys((self._tenant_id, *self._services_by_tenant)))

    def capability_manifest_source(self) -> RuntimeMcpService:
        return self

    def manifests_for_tenant(self, tenant_id: UUID) -> Mapping[str, JsonValue]:
        service = self._services_by_tenant.get(tenant_id)
        if service is None:
            return _empty_manifest()
        return service.capability_manifest_source().manifests_for_tenant(tenant_id)

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        manifest = self.manifests_for_tenant(tenant_id)
        capabilities = manifest.get("capabilities")
        if not isinstance(capabilities, tuple):
            return False
        return any(
            isinstance(capability, Mapping)
            and capability.get("id") == name
            and capability.get("available") is True
            for capability in capabilities
        )

    async def invoke(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        await self.ensure_tenant_loaded(tenant_id)
        service = self._services_by_tenant.get(tenant_id)
        if service is None:
            raise RuntimeCapabilityError("MCP tool unavailable")
        try:
            result = await service.invoke_projected_tool(
                name,
                arguments,
                context=McpInvocationContext(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    run_id=run_id,
                    agent_id=actor,
                    idempotency_key=idempotency_key,
                ),
            )
        except (McpToolDenied, McpToolNotFound) as error:
            raise RuntimeCapabilityError("MCP tool unavailable") from error
        except McpTimeout as error:
            raise RuntimeCapabilityError("MCP tool timed out") from error
        return cast(Mapping[str, JsonValue], result.content)


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
]
