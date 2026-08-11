from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from json import JSONDecodeError
from typing import Protocol

import httpx

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


class McpProtocolError(TypeError):
    """The MCP peer returned a malformed protocol payload."""


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
    def __init__(self, transport: McpClient | None = None) -> None:
        super().__init__(transport or _StdioJsonRpcMcpClient(), expected=McpTransportKind.STDIO)


class SseMcpClient(_DelegatingMcpClient):
    def __init__(self, transport: McpClient | None = None) -> None:
        super().__init__(transport or _HttpJsonRpcMcpClient(), expected=McpTransportKind.SSE)


class StreamableHttpMcpClient(_DelegatingMcpClient):
    def __init__(self, transport: McpClient | None = None) -> None:
        super().__init__(transport or _HttpJsonRpcMcpClient(), expected=McpTransportKind.STREAMABLE_HTTP)


def _ensure_transport(server: McpServerDefinition, expected: McpTransportKind) -> None:
    if server.transport is not expected:
        raise ValueError(f"MCP client requires {expected.value} transport")


class _StdioJsonRpcMcpClient:
    async def discover_tools(self, server: McpServerDefinition) -> tuple[McpToolSchema, ...]:
        result = await self._request(server, "tools/list", {})
        return _tools_from_result(result)

    async def invoke(
        self,
        server: McpServerDefinition,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> McpInvocationResult:
        result = await self._request(
            server,
            "tools/call",
            {"name": tool_name, "arguments": dict(arguments)},
        )
        return _invocation_result(result)

    async def health(self, server: McpServerDefinition) -> str:
        await self._request(server, "initialize", _initialize_params(), initialize=False)
        return "healthy"

    async def _request(
        self,
        server: McpServerDefinition,
        method: str,
        params: Mapping[str, object],
        *,
        initialize: bool = True,
    ) -> Mapping[str, object]:
        if server.command is None:
            raise RuntimeError("stdio MCP server command is not configured")
        process = await asyncio.create_subprocess_exec(
            server.command,
            *server.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            if process.stdin is None or process.stdout is None:
                raise RuntimeError("stdio MCP pipes are unavailable")
            next_id = 1
            if initialize:
                await _write_framed_json(
                    process.stdin,
                    _jsonrpc(next_id, "initialize", _initialize_params()),
                )
                await _read_jsonrpc_result(process.stdout)
                next_id += 1
                await _write_framed_json(
                    process.stdin,
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    },
                )
            await _write_framed_json(process.stdin, _jsonrpc(next_id, method, params))
            return await _read_jsonrpc_result(process.stdout)
        finally:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except TimeoutError:
                process.kill()
                await process.wait()


class _HttpJsonRpcMcpClient:
    async def discover_tools(self, server: McpServerDefinition) -> tuple[McpToolSchema, ...]:
        result = await self._request(server, "tools/list", {})
        return _tools_from_result(result)

    async def invoke(
        self,
        server: McpServerDefinition,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> McpInvocationResult:
        result = await self._request(
            server,
            "tools/call",
            {"name": tool_name, "arguments": dict(arguments)},
        )
        return _invocation_result(result)

    async def health(self, server: McpServerDefinition) -> str:
        await self._request(server, "initialize", _initialize_params(), initialize=False)
        return "healthy"

    async def _request(
        self,
        server: McpServerDefinition,
        method: str,
        params: Mapping[str, object],
        *,
        initialize: bool = True,
    ) -> Mapping[str, object]:
        if server.url is None:
            raise RuntimeError("remote MCP server URL is not configured")
        headers = {"Accept": "application/json, text/event-stream"}
        async with httpx.AsyncClient(timeout=server.timeout_seconds, headers=headers) as client:
            next_id = 1
            if initialize:
                await _post_jsonrpc(client, server.url, _jsonrpc(next_id, "initialize", _initialize_params()))
                next_id += 1
                await _post_jsonrpc(
                    client,
                    server.url,
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    },
                    expect_response=False,
                )
            return await _post_jsonrpc(client, server.url, _jsonrpc(next_id, method, params))


def _initialize_params() -> dict[str, object]:
    return {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "agent-hub", "version": "0.1.0"},
    }


def _jsonrpc(request_id: int, method: str, params: Mapping[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}


async def _write_framed_json(
    writer: asyncio.StreamWriter,
    payload: Mapping[str, object],
) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    writer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded)
    await writer.drain()


async def _read_jsonrpc_result(reader: asyncio.StreamReader) -> Mapping[str, object]:
    message = await _read_framed_or_line_json(reader)
    error = message.get("error")
    if isinstance(error, Mapping):
        message_text = error.get("message")
        raise RuntimeError(str(message_text or "MCP JSON-RPC error"))  # noqa: TRY004
    result = message.get("result")
    if not isinstance(result, Mapping):
        raise McpProtocolError("MCP JSON-RPC response is missing result")
    return result


async def _read_framed_or_line_json(reader: asyncio.StreamReader) -> dict[str, object]:
    first = await reader.readline()
    if not first:
        raise RuntimeError("MCP server closed stdout")
    if first.lower().startswith(b"content-length:"):
        try:
            length = int(first.decode("ascii").split(":", 1)[1].strip())
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("MCP Content-Length is invalid") from exc
        while True:
            line = await reader.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
        raw = await reader.readexactly(length)
        return _json_object(raw)
    return _json_object(first)


async def _post_jsonrpc(
    client: httpx.AsyncClient,
    url: str,
    payload: Mapping[str, object],
    *,
    expect_response: bool = True,
) -> Mapping[str, object]:
    response = await client.post(url, json=payload)
    if not expect_response and response.status_code in {200, 202, 204} and not response.content:
        return {}
    response.raise_for_status()
    if not response.content:
        if expect_response:
            raise RuntimeError("MCP HTTP response is empty")
        return {}
    message = _jsonrpc_http_payload(response)
    if not expect_response:
        return {}
    error = message.get("error")
    if isinstance(error, Mapping):
        message_text = error.get("message")
        raise RuntimeError(str(message_text or "MCP JSON-RPC error"))  # noqa: TRY004
    result = message.get("result")
    if not isinstance(result, Mapping):
        raise McpProtocolError("MCP HTTP response is missing result")
    return result


def _jsonrpc_http_payload(response: httpx.Response) -> dict[str, object]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return _json_object(line[5:].strip().encode("utf-8"))
        raise RuntimeError("MCP SSE response did not contain data")
    return _json_object(response.content)


def _json_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError) as exc:
        raise RuntimeError("MCP response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise McpProtocolError("MCP response must be a JSON object")
    return value


def _tools_from_result(result: Mapping[str, object]) -> tuple[McpToolSchema, ...]:
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise McpProtocolError("MCP tools/list result is invalid")
    parsed: list[McpToolSchema] = []
    for raw in tools:
        if not isinstance(raw, Mapping):
            raise McpProtocolError("MCP tool entry is invalid")
        name = raw.get("name")
        if not isinstance(name, str):
            raise McpProtocolError("MCP tool name is invalid")
        description = raw.get("description")
        input_schema = raw.get("inputSchema", raw.get("input_schema", {}))
        parsed.append(
            McpToolSchema(
                name=name,
                description=description if isinstance(description, str) else "",
                input_schema=dict(input_schema) if isinstance(input_schema, Mapping) else {},
            )
        )
    return tuple(parsed)


def _invocation_result(result: Mapping[str, object]) -> McpInvocationResult:
    content: dict[str, object] = {}
    if "structuredContent" in result:
        structured = result["structuredContent"]
        if isinstance(structured, Mapping):
            content["structuredContent"] = dict(structured)
    raw_content = result.get("content")
    if isinstance(raw_content, list):
        content["content"] = cast_json_list(raw_content)
    if not content:
        content["result"] = dict(result)
    return McpInvocationResult(content=content)


def cast_json_list(value: list[object]) -> list[object]:
    return [cast_json(item) for item in value]


def cast_json(value: object) -> object:
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, list):
        return cast_json_list(value)
    if isinstance(value, Mapping):
        return {str(key): cast_json(item) for key, item in value.items()}
    return str(value)
