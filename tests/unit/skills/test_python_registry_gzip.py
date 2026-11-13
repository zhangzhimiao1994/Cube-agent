"""Real metadata HTTP framing/decompression over in-memory streams, never network."""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import random
import ssl
import time

import pytest

from agent_hub.skills.sandbox import python_input_fetch, python_registry_read
from agent_hub.skills.sandbox.python_registry_protocol import (
    ResolutionBudget,
    ResolutionError,
    ResolutionLimits,
)
from tests.unit.skills.test_python_registry_read import DNS
from tests.unit.skills.test_python_registry_read import offline_network as no_live_network

offline_network = no_live_network
DOCUMENT = b'{"meta":{"api-version":"1.1"},"name":"demo","versions":["1.0"],"files":[]}'


class Writer:
    def __init__(self) -> None:
        self.request = bytearray()
        self.closed = False
        self.transport = self

    def write(self, raw: bytes) -> None:
        self.request.extend(raw)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def abort(self) -> None:
        self.closed = True


type TLSContext = ssl.SSLContext
type Connection = tuple[asyncio.StreamReader, Writer, list[tuple[str, int, TLSContext, str]], TLSContext]


def frame(raw: bytes, encoding: str, framing: str = "length") -> bytes:
    headers = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.pypi.simple.v1+json\r\n"
        + f"Content-Encoding: {encoding}\r\n".encode()
    )
    if framing == "length":
        return headers + f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw
    if framing == "chunked":
        pieces = [raw[i : i + 7] for i in range(0, len(raw), 7)]
        return headers + b"Transfer-Encoding: chunked\r\n\r\n" + b"".join(
            f"{len(piece):x}\r\n".encode() + piece + b"\r\n" for piece in pieces
        ) + b"0\r\n\r\n"
    assert framing == "eof"
    return headers + b"\r\n" + raw


@pytest.fixture
async def connection(monkeypatch: pytest.MonkeyPatch, offline_network: None) -> Connection:
    stream = asyncio.StreamReader()
    writer = Writer()
    calls: list[tuple[str, int, TLSContext, str]] = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    monkeypatch.setattr(python_input_fetch, "_tls_context", lambda: context)

    async def connect(address: str, port: int, *, ssl: TLSContext, server_hostname: str) -> tuple[asyncio.StreamReader, Writer]:
        calls.append((address, port, ssl, server_hostname))
        return stream, writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    return stream, writer, calls, context


@pytest.mark.parametrize("encoding", ["gzip", "identity"])
@pytest.mark.parametrize("framing", ["length", "chunked", "eof"])
async def test_metadata_returns_decoded_json_hash_with_normalized_headers(
    connection: Connection, encoding: str, framing: str
) -> None:
    stream, writer, calls, context = connection
    raw = gzip.compress(DOCUMENT, mtime=0) if encoding == "gzip" else DOCUMENT
    stream.feed_data(frame(raw, encoding, framing))
    stream.feed_eof()
    deadline = time.monotonic() + 5
    transport = python_registry_read._RegistryTransport(1024, deadline)
    response = await transport.get(
        "https://pypi.org/simple/demo/", resolved_addresses=("151.101.64.223",)
    )
    assert response.body == DOCUMENT
    assert response.headers == {
        "content-type": "application/vnd.pypi.simple.v1+json",
        "content-length": str(len(DOCUMENT)),
    }
    assert b"Accept-Encoding: gzip\r\n" in writer.request
    assert b"Host: pypi.org\r\n" in writer.request
    assert calls == [("151.101.64.223", 443, context, "pypi.org")]
    assert writer.closed


async def test_gzip_worker_snapshot_preserves_decoded_bytes_for_root_json_and_sha(connection: Connection) -> None:
    from agent_hub.skills.sandbox.python_registry_fetch import (
        MetadataReadRequest,
        read_metadata_snapshot,
    )

    stream, _, _, _ = connection
    stream.feed_data(frame(gzip.compress(DOCUMENT, mtime=0), "gzip"))
    stream.feed_eof()
    result = await read_metadata_snapshot(
        MetadataReadRequest(name="demo", maximum=1024, deadline=time.monotonic() + 5),
        resolver=DNS(),
    )
    assert result == {
        "schema_version": 1,
        "url": "https://pypi.org/simple/demo/",
        "body_base64": base64.b64encode(DOCUMENT).decode("ascii"),
    }


@pytest.mark.parametrize("large_wire", [False, True])
async def test_decoded_limit_and_multiblock_gzip_preserve_original_provenance_and_usage(
    connection: Connection, large_wire: bool
) -> None:
    if large_wire:
        document = json.dumps({"value": random.Random(0).randbytes(40_000).hex()}).encode()
        maximum = 100_000
    else:
        document = b'{"value":"' + b"x" * 1012 + b'"}'
        maximum = 1024
        assert len(document) == maximum
    raw = gzip.compress(document, mtime=0)
    assert (len(raw) > 16384) is large_wire
    stream, _, _, _ = connection
    stream.feed_data(frame(raw, "gzip"))
    stream.feed_eof()
    budget = ResolutionBudget(ResolutionLimits(max_response_bytes=maximum), time.monotonic() + 5)
    value, proof = await python_registry_read.RegistryReader(resolver=DNS()).read("demo", budget=budget)
    assert value == json.loads(document)
    assert proof.url == "https://pypi.org/simple/demo/"
    assert proof.sha256 == hashlib.sha256(document).hexdigest()
    assert dict(budget.usage) == {"requests": 1, "metadata_bytes": len(document)}


@pytest.mark.parametrize("fault", ["truncated", "crc", "trailing", "concatenated"])
async def test_invalid_gzip_is_rejected_before_metadata_provenance(connection: Connection, fault: str) -> None:
    raw = gzip.compress(DOCUMENT, mtime=0)
    raw = {
        "truncated": raw[:-8],
        "crc": raw[:-8] + bytes([raw[-8] ^ 1]) + raw[-7:],
        "trailing": raw + b"\x00",
        "concatenated": raw + gzip.compress(b"{}", mtime=0),
    }[fault]
    stream, _, _, _ = connection
    stream.feed_data(frame(raw, "gzip"))
    stream.feed_eof()
    with pytest.raises(ResolutionError, match="registry_gzip_invalid"):
        await python_registry_read.RegistryReader(resolver=DNS()).read(
            "demo", budget=ResolutionBudget(ResolutionLimits(), time.monotonic() + 5)
        )


@pytest.mark.parametrize("framing", ["length", "chunked", "eof"])
async def test_compressed_bomb_never_escapes_decoded_limit(connection: Connection, framing: str) -> None:
    stream, _, _, _ = connection
    stream.feed_data(frame(gzip.compress(b"x" * 100_000, mtime=0), "gzip", framing))
    stream.feed_eof()
    with pytest.raises(ResolutionError, match="registry_response_limit"):
        await python_registry_read.RegistryReader(resolver=DNS()).read(
            "demo",
            budget=ResolutionBudget(ResolutionLimits(max_response_bytes=1024), time.monotonic() + 5),
        )


@pytest.mark.parametrize("framing", ["length", "chunked", "eof"])
async def test_wire_limit_is_enforced_independently_of_decoded_limit(connection: Connection, framing: str) -> None:
    stream, _, _, _ = connection
    # Incompressible wire is larger than the 1024-byte decoded payload.
    raw = gzip.compress(random.Random(0).randbytes(1024), mtime=0)
    assert len(raw) > 1024
    stream.feed_data(frame(raw, "gzip", framing))
    stream.feed_eof()
    with pytest.raises(ValueError):
        await python_registry_read.RegistryReader(resolver=DNS()).read(
            "demo",
            budget=ResolutionBudget(ResolutionLimits(max_response_bytes=1024), time.monotonic() + 5),
        )


@pytest.mark.parametrize("encoding", ["br", "deflate", "gzip, identity"])
async def test_unnegotiated_metadata_encoding_is_rejected(connection: Connection, encoding: str) -> None:
    stream, _, _, _ = connection
    stream.feed_data(frame(DOCUMENT, encoding))
    stream.feed_eof()
    with pytest.raises(ResolutionError, match="registry_encoding"):
        await python_registry_read.RegistryReader(resolver=DNS()).read(
            "demo", budget=ResolutionBudget(ResolutionLimits(), time.monotonic() + 5)
        )


@pytest.mark.parametrize("cancel", [False, True])
async def test_original_deadline_or_cancellation_closes_partial_body_without_retry(
    connection: Connection, cancel: bool
) -> None:
    stream, writer, calls, _ = connection
    raw = gzip.compress(DOCUMENT, mtime=0)
    stream.feed_data(frame(raw, "gzip")[:-10])
    budget = ResolutionBudget(ResolutionLimits(), time.monotonic() + (5 if cancel else 0.03))
    task = asyncio.create_task(python_registry_read.RegistryReader(resolver=DNS()).read("demo", budget=budget))
    if cancel:
        await asyncio.sleep(0)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ResolutionError):
        await task
    assert writer.closed and len(calls) == 1
    assert dict(budget.usage) == {"requests": 1}


async def test_wheel_transport_still_rejects_gzip_and_keeps_identity_request(connection: Connection) -> None:
    stream, writer, _, _ = connection
    stream.feed_data(frame(gzip.compress(DOCUMENT, mtime=0), "gzip"))
    stream.feed_eof()
    transport = python_input_fetch._WheelTransport(1024, time.monotonic() + 5)
    with pytest.raises(python_input_fetch.PythonInputFetchError, match="fetch_encoding"):
        await transport.get(
            "https://files.pythonhosted.org/packages/ab/cd/demo.whl",
            resolved_addresses=("151.101.64.223",),
        )
    assert b"Accept-Encoding: identity\r\n" in writer.request
    assert writer.closed
