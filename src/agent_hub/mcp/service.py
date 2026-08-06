from __future__ import annotations

import asyncio
from collections.abc import Mapping

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityGateway, CapabilityStatus
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.mcp.client import McpClient
from agent_hub.mcp.types import (
    DiscoveredMcpTool,
    McpAuditEvent,
    McpGenerationSnapshot,
    McpInvocationContext,
    McpInvocationResult,
    McpServerDefinition,
)


class McpToolDenied(RuntimeError):
    pass


class McpToolNotFound(LookupError):
    pass


class McpTimeout(RuntimeError):
    pass


class McpService:
    def __init__(
        self,
        *,
        servers: tuple[McpServerDefinition, ...],
        clients: Mapping[str, McpClient],
        tool_allowlist_by_agent: Mapping[str, frozenset[str]],
        gateway: CapabilityGateway,
        role: Role,
    ) -> None:
        self._clients = dict(clients)
        self._tool_allowlist_by_agent = dict(tool_allowlist_by_agent)
        self._gateway = gateway
        self._role = role
        self._lock = asyncio.Lock()
        self._generation_id = 0
        self._servers: dict[str, McpServerDefinition] = {}
        self._snapshot = McpGenerationSnapshot(generation_id=0, tools=())
        self._audit_events: list[McpAuditEvent] = []
        self._pending_servers = servers

    async def start(self) -> None:
        await self.reload(self._pending_servers, clients=self._clients)

    async def generation_id(self) -> int:
        return self._generation_id

    async def reload(
        self,
        servers: tuple[McpServerDefinition, ...],
        *,
        clients: Mapping[str, McpClient],
    ) -> None:
        async with self._lock:
            discovered: list[DiscoveredMcpTool] = []
            discovered_names: set[str] = set()
            health: dict[str, str] = {}
            server_map: dict[str, McpServerDefinition] = {}
            for server in servers:
                if not server.enabled:
                    continue
                if server.id in server_map:
                    raise ValueError("duplicate MCP server id")
                server_map[server.id] = server
            for server in server_map.values():
                client = clients[server.id]
                try:
                    health[server.id] = await asyncio.wait_for(
                        client.health(server),
                        timeout=server.timeout_seconds,
                    )
                    tools = await asyncio.wait_for(
                        client.discover_tools(server),
                        timeout=server.timeout_seconds,
                    )
                except TimeoutError:
                    health[server.id] = "timeout"
                    self._audit("mcp.discovery_timeout", server_id=server.id, status="timeout")
                    continue
                except (RuntimeError, ValueError, OSError) as exc:
                    health[server.id] = "failed"
                    self._audit(
                        "mcp.discovery_failed",
                        server_id=server.id,
                        status="failed",
                        reason=type(exc).__name__,
                    )
                    continue
                for tool in tools:
                    qualified_name = f"{server.id}.{tool.name}"
                    if qualified_name in discovered_names:
                        raise ValueError("duplicate MCP tool name")
                    discovered_names.add(qualified_name)
                    discovered.append(
                        DiscoveredMcpTool(
                            server_id=server.id,
                            name=tool.name,
                            description=tool.description,
                            input_schema=tool.input_schema,
                        )
                    )
            self._servers = server_map
            self._clients = dict(clients)
            self._generation_id += 1
            self._snapshot = McpGenerationSnapshot(
                generation_id=self._generation_id,
                tools=tuple(discovered),
                health=health,
            )
            self._audit("mcp.reloaded", status="ok")

    async def snapshot(self) -> McpGenerationSnapshot:
        return self._snapshot.model_copy(deep=True)

    async def tools_for_agent(self, agent_id: str) -> tuple[DiscoveredMcpTool, ...]:
        allowed = self._tool_allowlist_by_agent.get(agent_id, frozenset())
        return tuple(
            tool.model_copy(deep=True)
            for tool in self._snapshot.tools
            if tool.qualified_name in allowed
        )

    async def invoke_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        context: McpInvocationContext,
    ) -> McpInvocationResult:
        tool = self._tool(tool_name)
        allowed = self._tool_allowlist_by_agent.get(context.agent_id, frozenset())
        if tool.qualified_name not in allowed:
            self._audit("mcp.denied", server_id=tool.server_id, tool_name=tool.qualified_name, status="denied")
            raise McpToolDenied("MCP tool is not projected for this agent")
        server = self._servers[tool.server_id]
        if server.tenant_id != context.tenant_id:
            self._audit(
                "mcp.denied",
                server_id=tool.server_id,
                tool_name=tool.qualified_name,
                status="tenant_mismatch",
            )
            raise McpToolDenied("MCP server does not belong to tenant")
        request = CapabilityRequest(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            agent_id=context.agent_id,
            capability="mcp",
            operation="invoke",
            resource=f"mcp/{tool.server_id}/{tool.name}",
            arguments=dict(arguments),
            idempotency_key=context.idempotency_key,
            run_id=context.run_id,
        )
        decision = await self._gateway.invoke(request, role=self._role)
        if decision.status is not CapabilityStatus.ALLOWED:
            self._audit(
                "mcp.denied",
                server_id=tool.server_id,
                tool_name=tool.qualified_name,
                status=decision.status.value,
                reason=decision.reason,
            )
            raise McpToolDenied(decision.reason or "MCP capability denied")
        client = self._clients[tool.server_id]
        try:
            result = await asyncio.wait_for(
                client.invoke(server, tool.name, request.arguments),
                timeout=server.timeout_seconds,
            )
        except TimeoutError as exc:
            self._audit("mcp.timeout", server_id=tool.server_id, tool_name=tool.qualified_name, status="timeout")
            raise McpTimeout("MCP invocation timed out") from exc
        except asyncio.CancelledError:
            self._audit("mcp.cancelled", server_id=tool.server_id, tool_name=tool.qualified_name, status="cancelled")
            raise
        self._audit("mcp.invoked", server_id=tool.server_id, tool_name=tool.qualified_name, status="ok")
        return result

    async def audit_events(self) -> tuple[McpAuditEvent, ...]:
        return tuple(self._audit_events)

    def _tool(self, qualified_name: str) -> DiscoveredMcpTool:
        for tool in self._snapshot.tools:
            if tool.qualified_name == qualified_name:
                return tool
        raise McpToolNotFound(qualified_name)

    def _audit(
        self,
        kind: str,
        *,
        status: str,
        server_id: str | None = None,
        tool_name: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._audit_events.append(
            McpAuditEvent(
                kind=kind,
                server_id=server_id,
                tool_name=tool_name,
                generation_id=self._generation_id,
                status=status,
                reason=reason,
            )
        )
