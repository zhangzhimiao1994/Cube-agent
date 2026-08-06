from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Protocol

from agent_hub.mcp.types import (
    McpInvocationResult,
    McpServerDefinition,
    McpToolSchema,
    McpTransportKind,
)


class McpClient(Protocol):
    async def discover_tools(self, server: McpServerDefinition) -> tuple[McpToolSchema, ...]: ...

    async def invoke(
        self,
        server: McpServerDefinition,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> McpInvocationResult: ...

    async def health(self, server: McpServerDefinition) -> str: ...


class InMemoryMcpClient:
    def __init__(
        self,
        *,
        tools: tuple[McpToolSchema, ...],
        responses: Mapping[str, McpInvocationResult] | None = None,
        delay_seconds: float = 0,
    ) -> None:
        self._tools = tools
        self._responses = dict(responses or {})
        self._delay_seconds = delay_seconds
        self.invocations: list[tuple[str, dict[str, object]]] = []
        self.cancelled = False

    async def discover_tools(self, server: McpServerDefinition) -> tuple[McpToolSchema, ...]:
        del server
        return self._tools

    async def invoke(
        self,
        server: McpServerDefinition,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> McpInvocationResult:
        del server
        self.invocations.append((tool_name, dict(arguments)))
        try:
            if self._delay_seconds:
                await asyncio.sleep(self._delay_seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self._responses.get(tool_name, McpInvocationResult(content={"ok": True}))

    async def health(self, server: McpServerDefinition) -> str:
        del server
        return "healthy"


class _DelegatingMcpClient:
    def __init__(self, transport: McpClient, *, expected: McpTransportKind) -> None:
        self._transport = transport
        self._expected = expected

    async def discover_tools(self, server: McpServerDefinition) -> tuple[McpToolSchema, ...]:
        _ensure_transport(server, self._expected)
        return await self._transport.discover_tools(server)

    async def invoke(
        self,
        server: McpServerDefinition,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> McpInvocationResult:
        _ensure_transport(server, self._expected)
        return await self._transport.invoke(server, tool_name, arguments)

    async def health(self, server: McpServerDefinition) -> str:
        _ensure_transport(server, self._expected)
        return await self._transport.health(server)


class StdioMcpClient(_DelegatingMcpClient):
    def __init__(self, transport: McpClient) -> None:
        super().__init__(transport, expected=McpTransportKind.STDIO)


class SseMcpClient(_DelegatingMcpClient):
    def __init__(self, transport: McpClient) -> None:
        super().__init__(transport, expected=McpTransportKind.SSE)


class StreamableHttpMcpClient(_DelegatingMcpClient):
    def __init__(self, transport: McpClient) -> None:
        super().__init__(transport, expected=McpTransportKind.STREAMABLE_HTTP)


def _ensure_transport(server: McpServerDefinition, expected: McpTransportKind) -> None:
    if server.transport is not expected:
        raise ValueError(f"MCP client requires {expected.value} transport")
