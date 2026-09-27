from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from agent_hub.capabilities.tools.http_read import (
    HttpReader,
    HttpReadError,
    HttpResponse,
    PinnedHttpTransport,
    ResponseTooLarge,
    UnsafeTarget,
)


class FakeResolver:
    def __init__(self, answers: dict[str, list[list[str]]]) -> None:
        self._answers = {host: list(values) for host, values in answers.items()}
        self.calls: list[str] = []

    async def resolve(self, host: str) -> list[str]:
        self.calls.append(host)
        answers = self._answers.get(host)
        if not answers:
            return ["93.184.216.34"]
        if len(answers) == 1:
            return answers[0]
        return answers.pop(0)


class FakeTransport:
    def __init__(self, responses: dict[str, HttpResponse]) -> None:
        self._responses = responses
        self.requested_urls: list[str] = []
        self.resolved_addresses: list[tuple[str, ...]] = []

    async def get(self, url: str, *, resolved_addresses: tuple[str, ...]) -> HttpResponse:
        self.requested_urls.append(url)
        self.resolved_addresses.append(resolved_addresses)
        return self._responses[url]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest",
        "http://10.0.0.12/metadata",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "https://user:password@example.com/private",
        "http://100.64.0.1/x",
        "http://100.127.255.254/x",
        "http://2130706433/private",
        "http://0x7f000001/private",
        "http://0177.0.0.1/private",
        "http://127.1/private",
    ],
)
async def test_http_reader_rejects_private_targets_and_unsafe_schemes(url: str) -> None:
    reader = HttpReader(
        resolver=FakeResolver({}),
        transport=FakeTransport({}),
    )

    with pytest.raises(UnsafeTarget):
        await reader.fetch(url)


async def test_http_reader_resolves_dns_and_rejects_private_answers() -> None:
    transport = FakeTransport({})
    reader = HttpReader(
        resolver=FakeResolver({"metadata.test": [["169.254.169.254"]]}),
        transport=transport,
    )

    with pytest.raises(UnsafeTarget):
        await reader.fetch("https://metadata.test/latest")

    assert transport.requested_urls == []


@pytest.mark.parametrize("address", ["100.64.0.1", "100.127.255.254"])
async def test_http_reader_rejects_dns_answers_in_shared_cgnat_space(address: str) -> None:
    transport = FakeTransport({})
    reader = HttpReader(
        resolver=FakeResolver({"shared.test": [[address]]}),
        transport=transport,
    )

    with pytest.raises(UnsafeTarget):
        await reader.fetch("https://shared.test/resource")

    assert transport.requested_urls == []


async def test_http_reader_rechecks_dns_after_redirect_to_catch_rebinding() -> None:
    resolver = FakeResolver(
        {
            "safe.test": [
                ["93.184.216.34"],
                ["127.0.0.1"],
            ],
        }
    )
    transport = FakeTransport(
        {
            "https://safe.test/start": HttpResponse(
                status_code=302,
                headers={"location": "/after-redirect"},
                body=b"",
            ),
        }
    )
    reader = HttpReader(resolver=resolver, transport=transport)

    with pytest.raises(UnsafeTarget):
        await reader.fetch("https://safe.test/start")

    assert transport.requested_urls == ["https://safe.test/start"]
    assert resolver.calls == ["safe.test", "safe.test"]


async def test_http_reader_limits_redirects() -> None:
    responses = {
        f"https://example.test/{index}": HttpResponse(
            status_code=302,
            headers={"location": f"/{index + 1}"},
            body=b"",
        )
        for index in range(5)
    }
    reader = HttpReader(
        resolver=FakeResolver({}),
        transport=FakeTransport(responses),
        max_redirects=3,
    )

    with pytest.raises(HttpReadError, match="redirect"):
        await reader.fetch("https://example.test/0")


async def test_http_reader_rejects_oversized_response() -> None:
    reader = HttpReader(
        resolver=FakeResolver({}),
        transport=FakeTransport(
            {
                "https://example.test/large": HttpResponse(
                    status_code=200,
                    headers={},
                    body=b"x" * 11,
                )
            }
        ),
        max_response_bytes=10,
    )

    with pytest.raises(ResponseTooLarge):
        await reader.fetch("https://example.test/large")


async def test_http_reader_returns_bounded_text_and_status() -> None:
    transport = FakeTransport(
        {
            "https://example.test/readme": HttpResponse(
                status_code=200,
                headers={"content-type": "text/plain"},
                body=b"hello",
            )
        }
    )
    reader = HttpReader(
        resolver=FakeResolver({}),
        transport=transport,
        max_response_bytes=10,
    )

    result = await reader.fetch("https://example.test/readme")

    assert result.url == "https://example.test/readme"
    assert result.status_code == 200
    assert result.body == "hello"
    assert result.truncated is False
    assert transport.resolved_addresses == [("93.184.216.34",)]


async def test_http_reader_errors_do_not_leak_url_credentials() -> None:
    reader = HttpReader(
        resolver=FakeResolver({}),
        transport=FakeTransport({}),
    )

    with pytest.raises(UnsafeTarget) as exc_info:
        await reader.fetch("https://alice:secret-token@example.test/private")

    assert "secret-token" not in str(exc_info.value)
    assert "alice" not in str(exc_info.value)


@dataclass
class CountingBodyTransport:
    body_size: int

    async def get(self, url: str, *, resolved_addresses: tuple[str, ...]) -> HttpResponse:
        del url, resolved_addresses
        return HttpResponse(
            status_code=200,
            headers={"content-length": str(self.body_size)},
            body=b"",
        )


async def test_http_reader_rejects_oversized_content_length_before_body() -> None:
    reader = HttpReader(
        resolver=FakeResolver({}),
        transport=CountingBodyTransport(body_size=50),
        max_response_bytes=10,
    )

    with pytest.raises(ResponseTooLarge):
        await reader.fetch("https://example.test/large")


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


async def test_pinned_transport_connects_to_validated_ip_and_preserves_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    reader.feed_eof()
    writer = FakeStreamWriter()
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def open_connection(*args: Any, **kwargs: Any) -> tuple[asyncio.StreamReader, Any]:
        calls.append((args, kwargs))
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    transport = PinnedHttpTransport(max_response_bytes=10)

    response = await transport.get(
        "http://public.example:8080/read?q=1",
        resolved_addresses=("93.184.216.34",),
    )

    assert response.body == b"ok"
    assert calls == [
        (
            ("93.184.216.34", 8080),
            {"ssl": None, "server_hostname": None},
        )
    ]
    assert b"GET /read?q=1 HTTP/1.1\r\n" in writer.written
    assert b"Host: public.example:8080\r\n" in writer.written
    assert writer.closed is True


async def test_pinned_transport_stream_limit_applies_without_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"HTTP/1.1 200 OK\r\n\r\n123456")
    reader.feed_eof()
    writer = FakeStreamWriter()

    async def open_connection(*args: Any, **kwargs: Any) -> tuple[asyncio.StreamReader, Any]:
        del args, kwargs
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", open_connection)

    with pytest.raises(ResponseTooLarge):
        await PinnedHttpTransport(max_response_bytes=5).get(
            "http://public.example/read",
            resolved_addresses=("93.184.216.34",),
        )
