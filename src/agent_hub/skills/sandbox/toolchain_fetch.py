"""Fetch exact Root-catalog SDK bytes, without installation authority.

The caller supplies an authorized catalog entry. Revalidation is not proof of
Root provenance; native isolation, publication and approvals belong to callers.
No files, SDK extraction, SDK execution or ambient HTTP credentials are used.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import math
import re
import ssl
import sys
import time
from urllib.parse import SplitResult

from agent_hub.capabilities.tools.http_read import (
    HttpResponse,
    PinnedHttpTransport,
    Resolver,
    SystemResolver,
    Transport,
)
from agent_hub.security.network import canonical_ip
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainArtifact

_HOST = "dl.google.com"
_CA = "/etc/ssl/certs/ca-certificates.crt"
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_CODES = frozenset(
    {
        "toolchain_fetch_entry_invalid",
        "toolchain_fetch_dns",
        "toolchain_fetch_timeout",
        "toolchain_fetch_transport",
        "toolchain_fetch_tls_unavailable",
        "toolchain_fetch_http",
        "toolchain_fetch_headers",
        "toolchain_fetch_encoding",
        "toolchain_fetch_size",
        "toolchain_fetch_sha",
        "toolchain_fetch_worker_invalid",
        "toolchain_fetch_cancelled",
    }
)


class ToolchainFetchError(ValueError):
    """Finite codes only: supplied URLs, credentials and diagnostics stay private."""

    def __init__(self, code: str = "toolchain_fetch_entry_invalid") -> None:
        self.code = code if code in _CODES else "toolchain_fetch_entry_invalid"
        super().__init__(self.code)


def _need(condition: object, code: str) -> None:
    if not condition:
        raise ToolchainFetchError(code)


def _entry(artifact: ToolchainArtifact) -> ToolchainArtifact:
    try:
        _need(type(artifact) is ToolchainArtifact, "toolchain_fetch_entry_invalid")
        # Pydantic model_copy/model_construct can bypass frozen model validation.
        return ToolchainArtifact.model_validate(vars(artifact), strict=True)
    except Exception:  # noqa: BLE001 - catalog errors may include untrusted fields.
        raise ToolchainFetchError() from None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    _need(remaining > 0, "toolchain_fetch_timeout")
    return remaining


def _headers(headers: dict[str, str], size: int) -> dict[str, str]:
    _need(type(headers) is dict and len(headers) <= 100, "toolchain_fetch_headers")
    result: dict[str, str] = {}
    total = 0
    for name, value in headers.items():
        _need(type(name) is str and type(value) is str, "toolchain_fetch_headers")
        length = len(name) + len(value) + 4
        total += length
        _need(length <= 8192 and total <= 65536, "toolchain_fetch_headers")
        _need(
            _TOKEN.fullmatch(name)
            and name.lower() not in result
            and all(32 <= ord(c) <= 126 for c in value),
            "toolchain_fetch_headers",
        )
        result[name.lower()] = value
    _need(
        result.get("content-encoding", "identity").lower() == "identity", "toolchain_fetch_encoding"
    )
    length_value = result.get("content-length")
    if length_value is not None:
        _need(
            re.fullmatch(r"[0-9]{1,20}", length_value) and int(length_value) == size,
            "toolchain_fetch_size",
        )
    transfer = result.get("transfer-encoding")
    _need(
        transfer is None or (transfer.lower() == "chunked" and length_value is None),
        "toolchain_fetch_headers",
    )
    return result


def _tls_context() -> ssl.SSLContext:
    try:
        # Explicit system trust avoids SSL_CERT_*, SSLKEYLOGFILE and proxy env.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        if sys.platform == "linux":
            context.load_verify_locations(cafile=_CA)
        elif sys.platform == "win32":
            certificates = []
            for store in ("CA", "ROOT"):
                for certificate, encoding, trust in ssl.enum_certificates(store):
                    if encoding == "x509_asn" and (
                        trust is True
                        or (isinstance(trust, set) and ssl.Purpose.SERVER_AUTH.oid in trust)
                    ):
                        certificates.append(certificate)
            _need(certificates, "toolchain_fetch_tls_unavailable")
            context.load_verify_locations(cadata=b"".join(certificates))
        else:
            raise ToolchainFetchError("toolchain_fetch_tls_unavailable")
        context.set_alpn_protocols(["http/1.1"])
        return context
    except Exception:  # noqa: BLE001 - system trust errors are finite and path-free.
        raise ToolchainFetchError("toolchain_fetch_tls_unavailable") from None


class _ToolchainTransport(PinnedHttpTransport):
    def __init__(self, size: int, deadline: float) -> None:
        super().__init__(max_response_bytes=size, timeout_seconds=_remaining(deadline))
        self.deadline = deadline
        self.context = _tls_context()
        _remaining(deadline)

    async def _request_address(
        self, *, parsed: SplitResult, host: str, address: str, port: int
    ) -> HttpResponse:
        _need(
            parsed.scheme == "https" and host == _HOST and port == 443,
            "toolchain_fetch_entry_invalid",
        )
        _remaining(self.deadline)
        reader, writer = await asyncio.open_connection(
            address,
            port,
            ssl=self.context,
            server_hostname=host,
        )
        try:
            writer.write(
                (
                    f"GET {parsed.path} HTTP/1.1\r\nHost: {_HOST}\r\n"
                    "Accept: application/octet-stream\r\nAccept-Encoding: identity\r\n"
                    "User-Agent: AgentHub-SDK/1\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            return await self._read_response(reader)
        finally:
            # Cancellation must not spawn a background TLS shutdown or outlive
            # the total deadline. Abort synchronously when graceful close stalls.
            try:
                writer.close()
                task = asyncio.current_task()
                if time.monotonic() >= self.deadline or (task is not None and task.cancelling()):
                    writer.transport.abort()
                else:
                    async with asyncio.timeout(min(0.25, _remaining(self.deadline))):
                        await writer.wait_closed()
            except (OSError, TimeoutError):
                writer.transport.abort()
            except BaseException:
                writer.transport.abort()
                raise

    async def _read_response(self, reader: asyncio.StreamReader) -> HttpResponse:
        async def line() -> bytes:
            _remaining(self.deadline)
            try:
                raw = await reader.readline()
            except ValueError:
                raise ToolchainFetchError("toolchain_fetch_headers") from None
            _need(len(raw) <= 8192 and raw.endswith(b"\r\n"), "toolchain_fetch_headers")
            return raw

        status = await line()
        match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?\r\n", status)
        _need(match is not None and match[1] == b"200", "toolchain_fetch_http")
        headers: dict[str, str] = {}
        total = len(status)
        while True:
            raw = await line()
            total += len(raw)
            _need(total <= 65536, "toolchain_fetch_headers")
            if raw == b"\r\n":
                break
            key, separator, value = raw[:-2].partition(b":")
            _need(separator and len(headers) < 100, "toolchain_fetch_headers")
            name = key.decode("ascii")
            _need(_TOKEN.fullmatch(name) and name.lower() not in headers, "toolchain_fetch_headers")
            value = value.strip(b" \t")
            _need(all(32 <= c <= 126 for c in value), "toolchain_fetch_headers")
            headers[name.lower()] = value.decode("ascii")
        headers = _headers(headers, self._max_response_bytes)
        data = bytearray()
        if "transfer-encoding" in headers:
            # Every nonempty chunk consumes >= 1 catalog byte, so framing work
            # scales with the exact archive, not a wheel-specific size ceiling.
            for _ in range(self._max_response_bytes + 1):
                raw_size = await line()
                _need(re.fullmatch(rb"[0-9a-fA-F]{1,16}\r\n", raw_size), "toolchain_fetch_headers")
                size = int(raw_size, 16)
                if size == 0:
                    _need(await line() == b"\r\n", "toolchain_fetch_headers")
                    break
                _need(len(data) + size <= self._max_response_bytes, "toolchain_fetch_size")
                while size:
                    _remaining(self.deadline)
                    chunk = await reader.readexactly(min(size, 65536))
                    data.extend(chunk)
                    size -= len(chunk)
                _need(await reader.readexactly(2) == b"\r\n", "toolchain_fetch_headers")
            else:
                raise ToolchainFetchError("toolchain_fetch_headers")
            _need(not await reader.read(1), "toolchain_fetch_size")
        else:
            while True:
                _remaining(self.deadline)
                chunk = await reader.read(min(65536, self._max_response_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                _need(len(data) <= self._max_response_bytes, "toolchain_fetch_size")
        _need(len(data) == self._max_response_bytes, "toolchain_fetch_size")
        return HttpResponse(200, headers, bytes(data))


async def fetch_toolchain_artifact(
    artifact: ToolchainArtifact,
    *,
    deadline: float,
    resolver: Resolver | None = None,
    transport: Transport | None = None,
) -> bytes:
    """Return only exact bytes; deadline is absolute time.monotonic(), not a duration.

    Injection is for component verification, not native network evidence. This
    component cannot establish provenance of the caller's Root catalog entry.
    """
    try:
        _need(
            type(deadline) in {int, float} and math.isfinite(deadline),
            "toolchain_fetch_entry_invalid",
        )
        async with asyncio.timeout(_remaining(deadline)):
            selected = _entry(artifact)
            _remaining(deadline)
            try:
                answers = await (SystemResolver() if resolver is None else resolver).resolve(_HOST)
            except Exception:  # noqa: BLE001 - omit resolver diagnostics.
                raise ToolchainFetchError("toolchain_fetch_dns") from None
            _remaining(deadline)
            _need(type(answers) is list and 0 < len(answers) <= 16, "toolchain_fetch_dns")
            addresses: list[str] = []
            for answer in answers:
                canonical = canonical_ip(answer) if type(answer) is str else None
                _need(canonical is not None, "toolchain_fetch_dns")
                assert canonical is not None
                ip = ipaddress.ip_address(canonical)
                _need(
                    ip.is_global and not ip.is_multicast and not ip.is_reserved,
                    "toolchain_fetch_dns",
                )
                if canonical not in addresses:
                    addresses.append(canonical)
            client = (
                _ToolchainTransport(selected.archive_bytes, deadline)
                if transport is None
                else transport
            )
            response = await client.get(selected.url, resolved_addresses=tuple(addresses))
            _remaining(deadline)
            _need(
                type(response) is HttpResponse
                and type(response.status_code) is int
                and response.status_code == 200,
                "toolchain_fetch_http",
            )
            _headers(response.headers, selected.archive_bytes)
            _need(
                type(response.body) is bytes and len(response.body) == selected.archive_bytes,
                "toolchain_fetch_size",
            )
            digest = hashlib.sha256()
            view = memoryview(response.body)
            for offset in range(0, len(view), 1024**2):
                _remaining(deadline)
                digest.update(view[offset : offset + 1024**2])
                await asyncio.sleep(0)
            _need(digest.hexdigest() == selected.sha256, "toolchain_fetch_sha")
            _remaining(deadline)
            return response.body
    except ToolchainFetchError:
        raise
    except TimeoutError:
        raise ToolchainFetchError("toolchain_fetch_timeout") from None
    except Exception:  # noqa: BLE001 - no HTTP/TLS diagnostics escape this boundary.
        raise ToolchainFetchError("toolchain_fetch_transport") from None


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _need(key not in result, "toolchain_fetch_worker_invalid")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ToolchainFetchError("toolchain_fetch_worker_invalid")


def main(argv: list[str] | None = None) -> int:
    """Private stdin/stdout pipe only; no URL arguments or installation authority."""
    try:
        arguments = sys.argv[1:] if argv is None else argv
        _need(arguments == [], "toolchain_fetch_worker_invalid")
        raw = sys.stdin.buffer.read(65537)
        _need(0 < len(raw) <= 65536, "toolchain_fetch_worker_invalid")
        try:
            payload = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant
            )
            _need(
                type(payload) is dict and set(payload) == {"artifact", "timeout_seconds"},
                "toolchain_fetch_worker_invalid",
            )
            timeout = payload["timeout_seconds"]
            _need(
                type(timeout) in {int, float} and math.isfinite(timeout) and 0 < timeout <= 600,
                "toolchain_fetch_worker_invalid",
            )
            artifact = ToolchainArtifact.from_payload(payload["artifact"])
        except Exception:  # noqa: BLE001 - malformed controls never reach DNS.
            raise ToolchainFetchError("toolchain_fetch_worker_invalid") from None
        data = asyncio.run(fetch_toolchain_artifact(artifact, deadline=time.monotonic() + timeout))
        sys.stdout.write(base64.b64encode(data).decode("ascii"))
        sys.stdout.flush()
        return 0
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("toolchain_fetch_cancelled", file=sys.stderr, flush=True)
        return 2
    except Exception as error:  # noqa: BLE001 - never emit traceback or partial SDK data.
        code = (
            error.code
            if isinstance(error, ToolchainFetchError)
            else "toolchain_fetch_worker_invalid"
        )
        if type(code) is not str or code not in _CODES:
            code = "toolchain_fetch_worker_invalid"
        print(code, file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
