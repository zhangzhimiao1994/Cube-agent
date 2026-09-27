from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import pytest

from agent_hub.mcp.client import (
    McpHttpResponse,
    McpRemoteSecurityError,
    McpResponseTooLarge,
    PinnedMcpHttpTransport,
    _HttpJsonRpcMcpClient,
)
from agent_hub.mcp.types import McpServerDefinition, McpTransportKind

TENANT_ID = UUID("66666666-6666-4666-8666-666666666666")


def remote_server(url: str = "https://mcp.example.com/rpc") -> McpServerDefinition:
    return McpServerDefinition(
        tenant_id=TENANT_ID,
        id="remote",
        transport=McpTransportKind.STREAMABLE_HTTP,
        url=url,
        domain_allowlist=("example.com",),
    )


class FakeResolver:
    def __init__(self, answers: dict[str, list[list[str]]]) -> None:
        self._answers = {host: list(values) for host, values in answers.items()}
        self.calls: list[str] = []

    async def resolve(self, host: str) -> list[str]:
        self.calls.append(host)
        answers = self._answers[host]
        if len(answers) == 1:
            return answers[0]
        return answers.pop(0)


class FakeTransport:
    def __init__(self, responses: list[McpHttpResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, tuple[str, ...], Mapping[str, object]]] = []

    async def post_json(
        self,
        url: str,
        payload: Mapping[str, object],
        *,
        resolved_addresses: tuple[str, ...],
        timeout_seconds: float,
    ) -> McpHttpResponse:
        del timeout_seconds
        self.requests.append((url, resolved_addresses, payload))
        return self._responses.pop(0)


def json_response(payload: bytes) -> McpHttpResponse:
    return McpHttpResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=payload,
    )


async def test_remote_mcp_rejects_private_dns_answer_before_connecting() -> None:
    transport = FakeTransport([])
    client = _HttpJsonRpcMcpClient(
        resolver=FakeResolver({"mcp.example.com": [["127.0.0.1"]]}),
        transport=transport,
    )

    with pytest.raises(McpRemoteSecurityError, match="unsafe"):
        await client.health(remote_server())

    assert transport.requests == []


async def test_remote_mcp_pins_each_request_to_the_validated_dns_answer() -> None:
    resolver = FakeResolver(
        {
            "mcp.example.com": [
                ["93.184.216.34"],
                ["93.184.216.35"],
                ["93.184.216.36"],
            ]
        }
    )
    transport = FakeTransport(
        [
            json_response(b'{"jsonrpc":"2.0","id":1,"result":{}}'),
            McpHttpResponse(status_code=202, headers={}, body=b""),
            json_response(b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}'),
        ]
    )
    client = _HttpJsonRpcMcpClient(resolver=resolver, transport=transport)

    assert await client.discover_tools(remote_server()) == ()

    assert resolver.calls == ["mcp.example.com"] * 3
    assert [request[1] for request in transport.requests] == [
        ("93.184.216.34",),
        ("93.184.216.35",),
        ("93.184.216.36",),
    ]


@pytest.mark.parametrize(
    ("location", "answers"),
    [
        ("https://evil.example.net/mcp", {"mcp.example.com": [["93.184.216.34"]]}),
        (
            "/private",
            {"mcp.example.com": [["93.184.216.34"], ["169.254.169.254"]]},
        ),
    ],
)
async def test_remote_mcp_rejects_cross_origin_or_private_redirects(
    location: str,
    answers: dict[str, list[list[str]]],
) -> None:
    transport = FakeTransport(
        [McpHttpResponse(status_code=307, headers={"location": location}, body=b"")]
    )
    client = _HttpJsonRpcMcpClient(
        resolver=FakeResolver(answers),
        transport=transport,
    )

    with pytest.raises(McpRemoteSecurityError):
        await client.health(remote_server())

    assert len(transport.requests) == 1


async def test_remote_mcp_limits_response_body() -> None:
    client = _HttpJsonRpcMcpClient(
        resolver=FakeResolver({"mcp.example.com": [["93.184.216.34"]]}),
        transport=FakeTransport(
            [
                McpHttpResponse(
                    status_code=200,
                    headers={"content-type": "application/json"},
                    body=b"x" * 11,
                )
            ]
        ),
        max_response_bytes=10,
    )

    with pytest.raises(McpResponseTooLarge):
        await client.health(remote_server())


async def test_remote_mcp_parses_sse_content_type_case_insensitively() -> None:
    client = _HttpJsonRpcMcpClient(
        resolver=FakeResolver({"mcp.example.com": [["93.184.216.34"]]}),
        transport=FakeTransport(
            [
                McpHttpResponse(
                    status_code=200,
                    headers={"Content-Type": "text/event-stream; charset=utf-8"},
                    body=b'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n',
                )
            ]
        ),
    )

    assert await client.health(remote_server()) == "healthy"


class FakeStreamWriter:
    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


async def test_pinned_transport_preserves_original_host_and_https_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
    )
    reader.feed_eof()
    writer = FakeStreamWriter()
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def open_connection(*args: Any, **kwargs: Any) -> tuple[asyncio.StreamReader, Any]:
        calls.append((args, kwargs))
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", open_connection)

    response = await PinnedMcpHttpTransport(max_response_bytes=10).post_json(
        "https://mcp.example.com/rpc?q=1",
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        resolved_addresses=("93.184.216.34",),
        timeout_seconds=5,
    )

    assert response.body == b"{}"
    assert calls[0][0] == ("93.184.216.34", 443)
    assert calls[0][1]["server_hostname"] == "mcp.example.com"
    assert calls[0][1]["ssl"] is not None
    assert b"POST /rpc?q=1 HTTP/1.1\r\n" in writer.written
    assert b"Host: mcp.example.com\r\n" in writer.written
    assert b"Accept-Encoding: identity\r\n" in writer.written
    assert writer.closed is True
