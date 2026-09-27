from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Protocol
from urllib.parse import SplitResult, urljoin, urlsplit

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


class McpRemoteSecurityError(RuntimeError):
    """The remote MCP target failed a network-boundary safety check."""


class McpResponseTooLarge(RuntimeError):
    """The remote MCP response exceeded its configured byte limit."""


@dataclass(frozen=True, slots=True)
class McpHttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


class McpResolver(Protocol):
    async def resolve(self, host: str) -> list[str]: ...


class McpHttpTransport(Protocol):
    async def post_json(
        self,
        url: str,
        payload: Mapping[str, object],
        *,
        resolved_addresses: tuple[str, ...],
        timeout_seconds: float,
    ) -> McpHttpResponse: ...


class SystemMcpResolver:
    async def resolve(self, host: str) -> list[str]:
        loop = asyncio.get_running_loop()
        try:
            records = await loop.getaddrinfo(
                host,
                None,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        except OSError as error:
            raise RuntimeError("remote MCP resolution failed") from error
        addresses: list[str] = []
        for record in records:
            address = str(record[4][0])
            if address not in addresses:
                addresses.append(address)
        return addresses


class PinnedMcpHttpTransport:
    """HTTPS/1.1 transport that never performs DNS resolution or proxy lookup."""

    def __init__(self, *, max_response_bytes: int = 1_000_000) -> None:
        self._max_response_bytes = max_response_bytes

    async def post_json(
        self,
        url: str,
        payload: Mapping[str, object],
        *,
        resolved_addresses: tuple[str, ...],
        timeout_seconds: float,
    ) -> McpHttpResponse:
        parsed = urlsplit(url)
        host = parsed.hostname
        if host is None or not resolved_addresses:
            raise RuntimeError("remote MCP request failed")
        try:
            port = parsed.port or 443
        except ValueError as error:
            raise RuntimeError("remote MCP request failed") from error
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        last_error: Exception | None = None
        for address in resolved_addresses:
            try:
                async with asyncio.timeout(timeout_seconds):
                    return await self._request_address(
                        parsed=parsed,
                        host=host,
                        address=address,
                        port=port,
                        body=encoded,
                    )
            except (OSError, TimeoutError, asyncio.IncompleteReadError) as error:
                last_error = error
        raise RuntimeError("remote MCP request failed") from last_error

    async def _request_address(
        self,
        *,
        parsed: SplitResult,
        host: str,
        address: str,
        port: int,
        body: bytes,
    ) -> McpHttpResponse:
        ssl_context = ssl.create_default_context()
        reader, writer = await asyncio.open_connection(
            address,
            port,
            ssl=ssl_context,
            server_hostname=host,
        )
        try:
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            host_value = _host_header(host, port)
            headers = (
                f"POST {path} HTTP/1.1\r\n"
                f"Host: {host_value}\r\n"
                "User-Agent: AgentHub/1.0\r\n"
                "Accept: application/json, text/event-stream\r\n"
                "Accept-Encoding: identity\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            writer.write(headers + body)
            await writer.drain()
            return await self._read_response(reader)
        finally:
            writer.close()
            await writer.wait_closed()

    async def _read_response(self, reader: asyncio.StreamReader) -> McpHttpResponse:
        status_line = await reader.readline()
        if len(status_line) > 8_192:
            raise RuntimeError("remote MCP response is invalid")
        parts = status_line.decode("iso-8859-1", errors="replace").strip().split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
            raise RuntimeError("remote MCP response is invalid")
        headers: dict[str, str] = {}
        header_bytes = len(status_line)
        while True:
            line = await reader.readline()
            header_bytes += len(line)
            if header_bytes > 65_536:
                raise RuntimeError("remote MCP response headers are too large")
            if line in {b"\r\n", b"\n"}:
                break
            if not line or b":" not in line:
                raise RuntimeError("remote MCP response is invalid")
            name, value = line.decode("iso-8859-1").split(":", 1)
            headers[name.strip()] = value.strip()
        transfer_encoding = (_header(headers, "transfer-encoding") or "").casefold()
        if "chunked" in transfer_encoding:
            body = await self._read_chunked(reader)
        else:
            content_length = _header(headers, "content-length")
            if content_length is None:
                body = await self._read_to_eof(reader)
            else:
                try:
                    length = int(content_length)
                except ValueError:
                    raise RuntimeError("remote MCP content length is invalid") from None
                if length < 0 or length > self._max_response_bytes:
                    raise McpResponseTooLarge("remote MCP response is too large")
                body = await reader.readexactly(length)
        return McpHttpResponse(status_code=int(parts[1]), headers=headers, body=body)

    async def _read_to_eof(self, reader: asyncio.StreamReader) -> bytes:
        body = bytearray()
        while True:
            remaining = self._max_response_bytes + 1 - len(body)
            chunk = await reader.read(min(65_536, remaining))
            if not chunk:
                return bytes(body)
            body.extend(chunk)
            if len(body) > self._max_response_bytes:
                raise McpResponseTooLarge("remote MCP response is too large")

    async def _read_chunked(self, reader: asyncio.StreamReader) -> bytes:
        body = bytearray()
        while True:
            size_line = await reader.readline()
            try:
                size = int(size_line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise RuntimeError("remote MCP chunk is invalid") from None
            if size == 0:
                while (trailer := await reader.readline()) not in {b"\r\n", b"\n", b""}:
                    if len(trailer) > 8_192:
                        raise RuntimeError("remote MCP response is invalid")
                return bytes(body)
            if size < 0 or len(body) + size > self._max_response_bytes:
                raise McpResponseTooLarge("remote MCP response is too large")
            body.extend(await reader.readexactly(size))
            if await reader.readexactly(2) != b"\r\n":
                raise RuntimeError("remote MCP chunk is invalid")


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
    def __init__(
        self,
        *,
        resolver: McpResolver | None = None,
        transport: McpHttpTransport | None = None,
        max_redirects: int = 3,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        self._resolver = resolver or SystemMcpResolver()
        self._transport = transport or PinnedMcpHttpTransport(
            max_response_bytes=max_response_bytes
        )
        self._max_redirects = max_redirects
        self._max_response_bytes = max_response_bytes

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
        next_id = 1
        if initialize:
            await self._post_jsonrpc(
                server,
                _jsonrpc(next_id, "initialize", _initialize_params()),
            )
            next_id += 1
            await self._post_jsonrpc(
                server,
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                },
                expect_response=False,
            )
        return await self._post_jsonrpc(server, _jsonrpc(next_id, method, params))

    async def _post_jsonrpc(
        self,
        server: McpServerDefinition,
        payload: Mapping[str, object],
        *,
        expect_response: bool = True,
    ) -> Mapping[str, object]:
        if server.url is None:
            raise RuntimeError("remote MCP server URL is not configured")
        original_origin = _origin(server.url)
        current_url = server.url
        redirects = 0
        while True:
            addresses = await self._resolve_public_addresses(current_url)
            response = await self._transport.post_json(
                current_url,
                payload,
                resolved_addresses=addresses,
                timeout_seconds=server.timeout_seconds,
            )
            if len(response.body) > self._max_response_bytes:
                raise McpResponseTooLarge("remote MCP response is too large")
            if response.status_code in {301, 302, 303, 307, 308}:
                location = _header(response.headers, "location")
                if location is None:
                    raise RuntimeError("remote MCP redirect is invalid")
                redirects += 1
                if redirects > self._max_redirects:
                    raise RuntimeError("remote MCP redirect limit exceeded")
                redirected = urljoin(current_url, location)
                if _origin(redirected) != original_origin:
                    raise McpRemoteSecurityError("remote MCP redirect is unsafe")
                current_url = redirected
                continue
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError(f"remote MCP returned HTTP {response.status_code}")
            if not expect_response and response.status_code in {200, 202, 204} and not response.body:
                return {}
            if not response.body:
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

    async def _resolve_public_addresses(self, url: str) -> tuple[str, ...]:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
            raise McpRemoteSecurityError("remote MCP target is unsafe")
        host = parsed.hostname
        if host is None:
            raise McpRemoteSecurityError("remote MCP target is unsafe")
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            addresses = await self._resolver.resolve(host)
        if not addresses:
            raise McpRemoteSecurityError("remote MCP target is unsafe")
        validated: list[str] = []
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                raise McpRemoteSecurityError("remote MCP target is unsafe") from None
            if not ip.is_global or ip.is_multicast:
                raise McpRemoteSecurityError("remote MCP target is unsafe")
            normalized = str(ip)
            if normalized not in validated:
                validated.append(normalized)
        return tuple(validated)


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


def _jsonrpc_http_payload(response: McpHttpResponse) -> dict[str, object]:
    content_type = _header(response.headers, "content-type") or ""
    if "text/event-stream" in content_type:
        for line in response.body.decode("utf-8", errors="replace").splitlines():
            if line.startswith("data:"):
                return _json_object(line[5:].strip().encode("utf-8"))
        raise RuntimeError("MCP SSE response did not contain data")
    return _json_object(response.body)


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    host = parsed.hostname
    if parsed.scheme != "https" or host is None:
        raise McpRemoteSecurityError("remote MCP target is unsafe")
    try:
        port = parsed.port or 443
    except ValueError as error:
        raise McpRemoteSecurityError("remote MCP target is unsafe") from error
    return parsed.scheme, host.rstrip(".").casefold(), port


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.casefold()
    for key, value in headers.items():
        if key.casefold() == lowered:
            return value
    return None


def _host_header(host: str, port: int) -> str:
    rendered = f"[{host}]" if ":" in host else host
    return rendered if port == 443 else f"{rendered}:{port}"


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
