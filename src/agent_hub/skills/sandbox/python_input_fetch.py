"""Bounded wheel bytes from a Root catalog, never model URLs or install authority.

No filesystem publication, package import, hooks, credentials or proxy discovery.
The worker's base64 output belongs only on the Root helper's private pipe.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import math
import re
import ssl
import stat
import struct
import sys
import time
import zipfile
from dataclasses import dataclass, fields
from email.parser import BytesParser
from email.policy import compat32
from urllib.parse import SplitResult, urlsplit

from agent_hub.capabilities.python_project_wheels import PythonWheelBinding
from agent_hub.capabilities.tools.http_read import (
    HttpResponse,
    PinnedHttpTransport,
    Resolver,
    SystemResolver,
    Transport,
)
from agent_hub.security.network import canonical_ip

_HOST = "files.pythonhosted.org"
_MAX_BYTES = 64 * 1024**2
_MAX_EXPANDED = 256 * 1024**2
_MAX_MEMBERS = 4096
_MAX_METADATA = 256 * 1024
_CA = "/etc/ssl/certs/ca-certificates.crt"
_CODES = frozenset({"fetch_entry_invalid", "fetch_url_invalid", "fetch_dns", "fetch_timeout",
    "fetch_transport", "fetch_tls_unavailable", "fetch_http", "fetch_headers", "fetch_encoding",
    "fetch_size", "fetch_sha", "fetch_wheel_invalid", "fetch_wheel_limit", "fetch_worker_invalid",
    "fetch_cancelled"})


class PythonInputFetchError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code if code in _CODES else "fetch_entry_invalid"
        super().__init__(self.code)


def _need(value: object, code: str) -> None:
    if not value:
        raise PythonInputFetchError(code)


@dataclass(frozen=True, slots=True)
class WheelDownload:
    filename: str
    name: str
    version: str
    sha256: str
    size_bytes: int
    url: str

    def __post_init__(self) -> None:
        try:
            PythonWheelBinding(self.filename, self.name, self.version, self.sha256)
            _need(type(self.size_bytes) is int and 0 < self.size_bytes <= _MAX_BYTES,
                  "fetch_entry_invalid")
            _need(type(self.url) is str and len(self.url) <= 2048
                  and all(33 <= ord(c) <= 126 for c in self.url)
                  and not any(c in self.url for c in "%\\?#"), "fetch_url_invalid")
            parsed = urlsplit(self.url)
            parts = parsed.path.split("/")
            _need(parsed.scheme == "https" and parsed.netloc in {_HOST, _HOST + ":443"}
                  and self.url == "https://" + parsed.netloc + parsed.path
                  and len(parts) >= 4 and parts[:2] == ["", "packages"]
                  and parts[-1] == self.filename
                  and all(re.fullmatch(r"[A-Za-z0-9_.+!-]+", p) and p not in {".", ".."}
                          for p in parts[2:]), "fetch_url_invalid")
        except PythonInputFetchError:
            raise
        except (ValueError, TypeError, AttributeError):
            raise PythonInputFetchError("fetch_entry_invalid") from None


def _entry(entry: WheelDownload) -> WheelDownload:
    _need(type(entry) is WheelDownload, "fetch_entry_invalid")
    return WheelDownload(**{field.name: getattr(entry, field.name) for field in fields(WheelDownload)})


def _headers(headers: dict[str, str], maximum: int) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.items():
        _need(type(name) is str and type(value) is str and name.lower() not in result,
              "fetch_headers")
        result[name.lower()] = value
    _need(result.get("content-encoding", "identity").lower() == "identity", "fetch_encoding")
    length = result.get("content-length")
    if length is not None:
        _need(re.fullmatch(r"[0-9]{1,20}", length) and int(length) <= maximum, "fetch_size")
    transfer = result.get("transfer-encoding")
    _need(transfer is None or (transfer.lower() == "chunked" and length is None), "fetch_headers")
    return result


def _tls_context() -> ssl.SSLContext:
    # Unlike create_default_context/load_default_certs, no environment CA/keylog selection.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if sys.platform == "linux":
        context.load_verify_locations(cafile=_CA)
    elif sys.platform == "win32":
        certificates = []
        for store in ("CA", "ROOT"):
            for certificate, encoding, trust in ssl.enum_certificates(store):
                if encoding == "x509_asn" and (trust is True or (
                        isinstance(trust, set) and ssl.Purpose.SERVER_AUTH.oid in trust)):
                    certificates.append(certificate)
        _need(certificates, "fetch_tls_unavailable")
        context.load_verify_locations(cadata=b"".join(certificates))
    else:
        raise PythonInputFetchError("fetch_tls_unavailable")
    context.set_alpn_protocols(["http/1.1"])
    return context


class _WheelTransport(PinnedHttpTransport):
    def __init__(self, maximum: int, deadline: float) -> None:
        super().__init__(max_response_bytes=maximum,
                         timeout_seconds=max(0.001, deadline - time.monotonic()))
        self.deadline = deadline
        self.context = _tls_context()

    async def _request_address(self, *, parsed: SplitResult, host: str,
                               address: str, port: int) -> HttpResponse:
        _need(host == _HOST and port == 443 and parsed.scheme == "https", "fetch_url_invalid")
        reader, writer = await asyncio.open_connection(
            address, port, ssl=self.context, server_hostname=host,
        )
        try:
            writer.write((f"GET {parsed.path} HTTP/1.1\r\nHost: {_HOST}\r\n"
                "Accept: application/octet-stream\r\nAccept-Encoding: identity\r\n"
                "User-Agent: AgentHub-Wheel/1\r\nConnection: close\r\n\r\n").encode("ascii"))
            await writer.drain()
            return await self._read_response(reader)
        finally:
            writer.close()
            task = asyncio.current_task()
            if time.monotonic() >= self.deadline or (task is not None and task.cancelling()):
                writer.transport.abort()
            else:
                try:
                    async with asyncio.timeout_at(min(self.deadline, time.monotonic() + 0.25)):
                        await writer.wait_closed()
                except (OSError, TimeoutError):
                    writer.transport.abort()

    async def _read_response(self, reader: asyncio.StreamReader) -> HttpResponse:
        async def line() -> bytes:
            value = await reader.readline()
            _need(len(value) <= 8192 and value.endswith(b"\r\n"), "fetch_headers")
            return value
        status = await line()
        match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?\r\n", status)
        _need(match is not None and match[1] == b"200", "fetch_http")
        headers: dict[str, str] = {}
        total = len(status)
        while True:
            raw = await line()
            total += len(raw)
            _need(total <= 65536 and len(headers) <= 100, "fetch_headers")
            if raw == b"\r\n":
                break
            key, separator, value = raw[:-2].partition(b":")
            _need(separator and re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                  and all(32 <= c <= 126 for c in value.strip(b" \t")), "fetch_headers")
            name = key.decode("ascii").lower()
            _need(name not in headers, "fetch_headers")
            headers[name] = value.strip(b" \t").decode("ascii")
        headers = _headers(headers, self._max_response_bytes)
        if "transfer-encoding" in headers:
            data = bytearray()
            for _ in range(131072):
                size_line = await line()
                _need(re.fullmatch(rb"[0-9a-fA-F]{1,16}\r\n", size_line), "fetch_headers")
                size = int(size_line, 16)
                if size == 0:
                    _need(await line() == b"\r\n", "fetch_headers")
                    break
                _need(len(data) + size <= self._max_response_bytes, "fetch_size")
                data.extend(await reader.readexactly(size))
                _need(await reader.readexactly(2) == b"\r\n", "fetch_headers")
            else:
                raise PythonInputFetchError("fetch_headers")
            body = bytes(data)
        elif "content-length" in headers:
            body = await reader.readexactly(int(headers["content-length"]))
        else:
            return HttpResponse(200, headers, await self._read_to_eof(reader))
        _need(not await reader.read(1), "fetch_headers")
        return HttpResponse(200, headers, body)


def _central_directory(data: bytes) -> None:
    # Bound actual central records before ZipFile creates objects. Count fields
    # alone are attacker-controlled. ZIP64/multidisk are outside this small recipe.
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    _need(end >= 0 and end + 22 <= len(data), "fetch_wheel_invalid")
    _, disk, central_disk, count_disk, count, size, offset, comment = struct.unpack_from("<4s4H2IH", data, end)
    _need(disk == central_disk == 0 and count_disk == count and 0 < count <= _MAX_MEMBERS,
          "fetch_wheel_limit")
    _need(end + 22 + comment == len(data) and offset + size == end, "fetch_wheel_invalid")
    position, actual = offset, 0
    while position < end:
        _need(position + 46 <= end and data[position:position + 4] == b"PK\x01\x02", "fetch_wheel_invalid")
        name, extra, note = struct.unpack_from("<3H", data, position + 28)
        position += 46 + name + extra + note
        actual += 1
        _need(position <= end and actual <= _MAX_MEMBERS, "fetch_wheel_limit")
    _need(actual == count, "fetch_wheel_invalid")


def _validate(entry: WheelDownload, data: bytes, deadline: float) -> None:
    entry = _entry(entry)
    _need(type(data) is bytes and len(data) == entry.size_bytes, "fetch_size")
    _need(hashlib.sha256(data).hexdigest() == entry.sha256, "fetch_sha")
    _central_directory(data)
    primary = "-".join(entry.filename.split("-")[:2]) + ".dist-info"
    required = {primary + "/METADATA", primary + "/WHEEL"}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        _need(0 < len(members) <= _MAX_MEMBERS, "fetch_wheel_limit")
        seen: dict[str, bool] = {}
        expanded = 0
        for member in members:
            name = member.filename
            path = name.rstrip("/")
            parts = path.split("/")
            mode = member.external_attr >> 16
            _need(name == member.orig_filename and 0 < len(name.encode("utf-8")) <= 1024
                  and len(parts) <= 32 and not any(ord(c) < 32 or ord(c) == 127 for c in name)
                  and "\\" not in name and ":" not in name
                  and all(p and p not in {".", ".."} for p in parts)
                  and path not in seen and not name.endswith("//"), "fetch_wheel_invalid")
            _need(stat.S_IFMT(mode) in ({0, stat.S_IFDIR} if member.is_dir() else {0, stat.S_IFREG})
                  and not mode & 0o7000 and member.flag_bits & ~0x80E == 0
                  and member.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                  and (member.compress_type != zipfile.ZIP_STORED or member.flag_bits & 6 == 0),
                  "fetch_wheel_invalid")
            _need(not parts[0].endswith(".dist-info") or parts[0] == primary, "fetch_wheel_invalid")
            seen[path] = member.is_dir()
            expanded += member.file_size
            _need(0 <= member.file_size and expanded <= _MAX_EXPANDED
                  and (name not in required or member.file_size <= _MAX_METADATA), "fetch_wheel_limit")
        _need(required <= seen.keys() and not any(seen[p] for p in required), "fetch_wheel_invalid")
        for path in seen:
            _need(all(seen.get("/".join(path.split("/")[:n]), True)
                      for n in range(1, len(path.split("/")))), "fetch_wheel_invalid")
        metadata: dict[str, bytes] = {}
        total = 0
        for member in members:
            body = bytearray()
            with archive.open(member) as stream:
                while True:
                    _need(time.monotonic() < deadline, "fetch_timeout")
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    _need(total <= _MAX_EXPANDED, "fetch_wheel_limit")
                    if member.filename in required:
                        _need(len(body) + len(chunk) <= _MAX_METADATA, "fetch_wheel_limit")
                        body.extend(chunk)
            if member.filename in required:
                metadata[member.filename] = bytes(body)
        package = BytesParser(policy=compat32).parsebytes(metadata[primary + "/METADATA"])
        wheel = BytesParser(policy=compat32).parsebytes(metadata[primary + "/WHEEL"])
        _need(not package.defects and not wheel.defects, "fetch_wheel_invalid")
        for document, key, expected in ((package, "Name", entry.name), (package, "Version", entry.version),
                (wheel, "Wheel-Version", "1.0"), (wheel, "Root-Is-Purelib", "true"),
                (wheel, "Tag", "py3-none-any")):
            values = document.get_all(key, [])
            _need(len(values) == 1 and type(values[0]) is str, "fetch_wheel_invalid")
            actual = re.sub(r"[-_.]+", "-", values[0]).lower() if key == "Name" else values[0]
            _need(actual == expected, "fetch_wheel_invalid")


def validate_wheel_bytes(entry: WheelDownload, data: bytes) -> None:
    """Independently hash and inspect actual bytes; no extraction/import/authority."""
    try:
        _validate(entry, data, math.inf)
    except PythonInputFetchError:
        raise
    except Exception:  # noqa: BLE001 - malformed archive exceptions must remain path-free.
        raise PythonInputFetchError("fetch_wheel_invalid") from None


async def fetch_wheel(entry: WheelDownload, *, timeout_seconds: float,
                      resolver: Resolver | None = None, transport: Transport | None = None) -> bytes:
    try:
        entry = _entry(entry)
        _need(type(timeout_seconds) in {int, float} and math.isfinite(timeout_seconds)
              and 0 < timeout_seconds <= 600, "fetch_entry_invalid")
        deadline = time.monotonic() + timeout_seconds
        async with asyncio.timeout(timeout_seconds):
            answers = await (resolver or SystemResolver()).resolve(_HOST)
            _need(type(answers) is list and 0 < len(answers) <= 16, "fetch_dns")
            addresses = []
            for answer in answers:
                canonical = canonical_ip(answer) if type(answer) is str else None
                _need(canonical is not None, "fetch_dns")
                assert canonical is not None
                ip = ipaddress.ip_address(canonical)
                _need(ip.is_global and not ip.is_multicast, "fetch_dns")
                if canonical not in addresses:
                    addresses.append(canonical)
            _need(time.monotonic() < deadline, "fetch_timeout")
            response = await (transport or _WheelTransport(entry.size_bytes, deadline)).get(
                entry.url, resolved_addresses=tuple(addresses))
            _need(response.status_code == 200, "fetch_http")
            headers = _headers(response.headers, entry.size_bytes)
            if "content-length" in headers:
                _need(int(headers["content-length"]) == len(response.body), "fetch_size")
            _validate(entry, response.body, deadline)
            _need(time.monotonic() < deadline, "fetch_timeout")
            return response.body
    except PythonInputFetchError:
        raise
    except TimeoutError:
        raise PythonInputFetchError("fetch_timeout") from None
    except Exception:  # noqa: BLE001 - no network/TLS URLs or diagnostics cross this boundary.
        raise PythonInputFetchError("fetch_transport") from None


async def _fetch_batch(entries: list[WheelDownload], timeout_seconds: float) -> list[dict[str, str]]:
    deadline = time.monotonic() + timeout_seconds
    artifacts = []
    try:
        async with asyncio.timeout(timeout_seconds):
            for entry in entries:
                remaining = deadline - time.monotonic()
                _need(remaining > 0, "fetch_timeout")
                data = await fetch_wheel(entry, timeout_seconds=remaining)
                artifacts.append({"filename": entry.filename,
                                  "body_base64": base64.b64encode(data).decode("ascii")})
                _need(time.monotonic() < deadline, "fetch_timeout")
        return artifacts
    except TimeoutError:
        raise PythonInputFetchError("fetch_timeout") from None


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = sys.argv[1:] if argv is None else argv
        _need(arguments in (["--worker"], ["--worker-batch"]), "fetch_worker_invalid")
        batch = arguments == ["--worker-batch"]
        maximum = 512 * 1024 if batch else 65536
        raw = sys.stdin.buffer.read(maximum + 1)
        _need(0 < len(raw) <= maximum, "fetch_worker_invalid")
        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                _need(key not in result, "fetch_worker_invalid")
                result[key] = value
            return result
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                             parse_constant=lambda _: _need(False, "fetch_worker_invalid"))
        wheel_fields = {field.name for field in fields(WheelDownload)}
        _need(type(payload) is dict and set(payload) == (
            {"artifacts", "timeout_seconds"} if batch else wheel_fields | {"timeout_seconds"}),
            "fetch_worker_invalid")
        timeout = payload.pop("timeout_seconds")
        if batch:
            _need(type(timeout) in {int, float} and math.isfinite(timeout) and 0 < timeout <= 600,
                  "fetch_worker_invalid")
            items = payload["artifacts"]
            _need(type(items) is list and 1 <= len(items) <= 1000, "fetch_worker_invalid")
            entries = []
            for item in items:
                _need(type(item) is dict and set(item) == wheel_fields, "fetch_worker_invalid")
                entries.append(WheelDownload(**item))
            # Complete preflight before any DNS, including invalid later entries.
            _need(len({entry.filename for entry in entries}) == len(entries)
                  and len({(entry.name, entry.version) for entry in entries}) == len(entries)
                  and sum(entry.size_bytes for entry in entries) <= _MAX_BYTES,
                  "fetch_worker_invalid")
            response: dict[str, object] = {"schema_version": 1,
                "artifacts": asyncio.run(_fetch_batch(entries, timeout))}
        else:
            data = asyncio.run(fetch_wheel(WheelDownload(**payload), timeout_seconds=timeout))
            response = {"schema_version": 1, "body_base64": base64.b64encode(data).decode("ascii")}
        print(json.dumps(response, separators=(",", ":")), flush=True)
        return 0
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("fetch_cancelled", file=sys.stderr, flush=True)
        return 2
    except Exception as error:  # noqa: BLE001 - worker stderr is finite codes only.
        code = error.code if isinstance(error, PythonInputFetchError) else "fetch_worker_invalid"
        print(code, file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
