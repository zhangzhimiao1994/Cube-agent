from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import SplitResult, urljoin, urlsplit


class HttpReadError(RuntimeError):
    """Stable HTTP reader error without target secrets."""


class UnsafeTarget(HttpReadError):
    """Target URL or resolved address is unsafe."""


class ResponseTooLarge(HttpReadError):
    """HTTP response exceeds the configured byte limit."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class HttpReadResult:
    url: str
    status_code: int
    body: str
    truncated: bool


class Resolver(Protocol):
    async def resolve(self, host: str) -> list[str]: ...


class Transport(Protocol):
    async def get(self, url: str, *, resolved_addresses: tuple[str, ...]) -> HttpResponse: ...


class SystemResolver:
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
            raise HttpReadError("target resolution failed") from error
        addresses: list[str] = []
        for record in records:
            address = str(record[4][0])
            if address not in addresses:
                addresses.append(address)
        return addresses


class PinnedHttpTransport:
    """Small HTTP/1.1 transport that connects only to pre-validated IP addresses."""

    def __init__(self, *, max_response_bytes: int = 1_000_000, timeout_seconds: float = 15) -> None:
        self._max_response_bytes = max_response_bytes
        self._timeout_seconds = timeout_seconds

    async def get(self, url: str, *, resolved_addresses: tuple[str, ...]) -> HttpResponse:
        parsed = urlsplit(url)
        host = parsed.hostname
        if host is None or not resolved_addresses:
            raise HttpReadError("HTTP request failed")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as error:
            raise HttpReadError("HTTP request failed") from error
        last_error: Exception | None = None
        for address in resolved_addresses:
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    return await self._request_address(
                        parsed=parsed,
                        host=host,
                        address=address,
                        port=port,
                    )
            except (OSError, TimeoutError, asyncio.IncompleteReadError) as error:
                last_error = error
        raise HttpReadError("HTTP request failed") from last_error

    async def _request_address(
        self,
        *,
        parsed: SplitResult,
        host: str,
        address: str,
        port: int,
    ) -> HttpResponse:
        scheme = parsed.scheme
        ssl_context = ssl.create_default_context() if scheme == "https" else None
        reader, writer = await asyncio.open_connection(
            address,
            port,
            ssl=ssl_context,
            server_hostname=host if ssl_context is not None else None,
        )
        try:
            path = parsed.path or "/"
            query = parsed.query
            if query:
                path = f"{path}?{query}"
            default_port = 443 if scheme == "https" else 80
            host_value = _host_header(host, port, default_port)
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host_value}\r\n"
                "User-Agent: AgentHub/1.0\r\n"
                "Accept: text/*, application/json, application/xml;q=0.9, */*;q=0.5\r\n"
                "Accept-Encoding: identity\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(request.encode("ascii"))
            await writer.drain()
            return await self._read_response(reader)
        finally:
            writer.close()
            await writer.wait_closed()

    async def _read_response(self, reader: asyncio.StreamReader) -> HttpResponse:
        status_line = await reader.readline()
        if len(status_line) > 8_192:
            raise HttpReadError("HTTP response is invalid")
        parts = status_line.decode("iso-8859-1", errors="replace").strip().split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
            raise HttpReadError("HTTP response is invalid")
        headers: dict[str, str] = {}
        header_bytes = len(status_line)
        while True:
            line = await reader.readline()
            header_bytes += len(line)
            if header_bytes > 65_536:
                raise HttpReadError("HTTP response headers are too large")
            if line in {b"\r\n", b"\n"}:
                break
            if not line or b":" not in line:
                raise HttpReadError("HTTP response is invalid")
            name, value = line.decode("iso-8859-1").split(":", 1)
            headers[name.strip()] = value.strip()
        transfer_encoding = (_header(headers, "transfer-encoding") or "").casefold()
        if "chunked" in transfer_encoding:
            body = await self._read_chunked(reader)
        else:
            content_length = _header(headers, "content-length")
            if content_length is not None:
                try:
                    length = int(content_length)
                except ValueError:
                    raise HttpReadError("content length is invalid") from None
                if length < 0 or length > self._max_response_bytes:
                    raise ResponseTooLarge("response is too large")
                body = await reader.readexactly(length)
            else:
                body = await self._read_to_eof(reader)
        return HttpResponse(status_code=int(parts[1]), headers=headers, body=body)

    async def _read_to_eof(self, reader: asyncio.StreamReader) -> bytes:
        body = bytearray()
        while True:
            chunk = await reader.read(min(65_536, self._max_response_bytes + 1 - len(body)))
            if not chunk:
                return bytes(body)
            body.extend(chunk)
            if len(body) > self._max_response_bytes:
                raise ResponseTooLarge("response is too large")

    async def _read_chunked(self, reader: asyncio.StreamReader) -> bytes:
        body = bytearray()
        while True:
            size_line = await reader.readline()
            try:
                size = int(size_line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise HttpReadError("HTTP chunk is invalid") from None
            if size == 0:
                while (trailer := await reader.readline()) not in {b"\r\n", b"\n", b""}:
                    if len(trailer) > 8_192:
                        raise HttpReadError("HTTP response is invalid")
                return bytes(body)
            if size < 0 or len(body) + size > self._max_response_bytes:
                raise ResponseTooLarge("response is too large")
            body.extend(await reader.readexactly(size))
            if await reader.readexactly(2) != b"\r\n":
                raise HttpReadError("HTTP chunk is invalid")


class HttpReader:
    def __init__(
        self,
        *,
        resolver: Resolver,
        transport: Transport,
        max_redirects: int = 3,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        self._resolver = resolver
        self._transport = transport
        self._max_redirects = max_redirects
        self._max_response_bytes = max_response_bytes

    @classmethod
    def production(
        cls,
        *,
        max_redirects: int = 3,
        max_response_bytes: int = 1_000_000,
    ) -> HttpReader:
        return cls(
            resolver=SystemResolver(),
            transport=PinnedHttpTransport(max_response_bytes=max_response_bytes),
            max_redirects=max_redirects,
            max_response_bytes=max_response_bytes,
        )

    async def fetch(self, url: str) -> HttpReadResult:
        current, addresses = await self._validate_url(url)
        redirects = 0
        while True:
            response = await self._transport.get(current, resolved_addresses=addresses)
            self._check_content_length(response.headers)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = _header(response.headers, "location")
                if location is None:
                    raise HttpReadError("redirect response is invalid")
                redirects += 1
                if redirects > self._max_redirects:
                    raise HttpReadError("redirect limit exceeded")
                current, addresses = await self._validate_url(urljoin(current, location))
                continue
            if len(response.body) > self._max_response_bytes:
                raise ResponseTooLarge("response is too large")
            return HttpReadResult(
                url=current,
                status_code=response.status_code,
                body=response.body.decode("utf-8", errors="replace"),
                truncated=False,
            )

    async def _validate_url(self, url: str) -> tuple[str, tuple[str, ...]]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            raise UnsafeTarget("target is unsafe")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeTarget("target is unsafe")
        host = parsed.hostname
        if host is None:
            raise UnsafeTarget("target is unsafe")
        addresses = await self._validate_host(host)
        return parsed.geturl(), addresses

    async def _validate_host(self, host: str) -> tuple[str, ...]:
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            if _looks_like_noncanonical_ip_literal(host):
                raise UnsafeTarget("target is unsafe") from None
            addresses = await self._resolver.resolve(host)
        if not addresses:
            raise UnsafeTarget("target is unsafe")
        validated: list[str] = []
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                raise UnsafeTarget("target is unsafe") from None
            if _unsafe_ip(ip):
                raise UnsafeTarget("target is unsafe")
            validated.append(str(ip))
        return tuple(validated)

    def _check_content_length(self, headers: dict[str, str]) -> None:
        value = _header(headers, "content-length")
        if value is None:
            return
        try:
            length = int(value)
        except ValueError:
            raise HttpReadError("content length is invalid") from None
        if length > self._max_response_bytes:
            raise ResponseTooLarge("response is too large")


def _unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_multicast or not ip.is_global


def _header(headers: dict[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _host_header(host: str, port: int, default_port: int) -> str:
    rendered = f"[{host}]" if ":" in host else host
    return rendered if port == default_port else f"{rendered}:{port}"


_NONCANONICAL_IPV4 = re.compile(r"^(?:0x[0-9a-fA-F]+|\d+)(?:\.(?:0x[0-9a-fA-F]+|\d+))*$")


def _looks_like_noncanonical_ip_literal(host: str) -> bool:
    return _NONCANONICAL_IPV4.fullmatch(host) is not None
