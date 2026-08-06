from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlsplit


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


_NONCANONICAL_IPV4 = re.compile(r"^(?:0x[0-9a-fA-F]+|\d+)(?:\.(?:0x[0-9a-fA-F]+|\d+))*$")


def _looks_like_noncanonical_ip_literal(host: str) -> bool:
    return _NONCANONICAL_IPV4.fullmatch(host) is not None
