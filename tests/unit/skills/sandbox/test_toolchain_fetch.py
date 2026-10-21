"""Component fixtures only: no network, SDK execution, extraction or native claims."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import importlib.util
import io
import json
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_hub.capabilities.tools.http_read import HttpResponse
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainArtifact

MODULE = "agent_hub.skills.sandbox.toolchain_fetch"
HOST = "dl.google.com"
URL = "https://dl.google.com/go/go1.27.3.linux-amd64.tar.gz"
DATA = b"opaque SDK bytes: do not unpack or execute"


def test_module_exposes_fetch() -> None:
    assert importlib.util.find_spec(MODULE) is not None, "independent SDK fetch is missing"


@pytest.fixture
def api() -> Any:
    return importlib.import_module(MODULE)


def entry(data: bytes = DATA) -> ToolchainArtifact:
    return ToolchainArtifact(
        stack="go",
        version="1.27.3",
        platform="linux_amd64",
        url=URL,
        sha256=hashlib.sha256(data).hexdigest(),
        archive_bytes=len(data),
        unpacked_bytes_limit=100_000,
        archive_entries_limit=1000,
        directory_entries_limit=10_000,
        tar_bytes_limit=102_400,
    )


class Resolver:
    def __init__(self, answers: list[str] | None = None) -> None:
        self.answers = ["142.250.190.14"] if answers is None else answers
        self.calls: list[str] = []

    async def resolve(self, host: str) -> list[str]:
        self.calls.append(host)
        return self.answers


class Transport:
    def __init__(
        self, body: bytes = DATA, *, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        self.response = HttpResponse(status, headers or {}, body)
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def get(self, url: str, *, resolved_addresses: tuple[str, ...]) -> HttpResponse:
        self.calls.append((url, resolved_addresses))
        return self.response


async def fetch(
    api: Any,
    *,
    artifact: Any = None,
    resolver: Any = None,
    transport: Any = None,
    seconds: float = 2,
) -> bytes:
    return cast(
        bytes,
        await api.fetch_toolchain_artifact(
            entry() if artifact is None else artifact,
            deadline=time.monotonic() + seconds,
            resolver=Resolver() if resolver is None else resolver,
            transport=Transport() if transport is None else transport,
        ),
    )


async def test_exact_catalog_entry_and_public_addresses(api: Any) -> None:
    resolver = Resolver(["142.250.190.14", "2607:f8b0:4009:81a::200e", "142.250.190.14"])
    transport = Transport(headers={"Content-Length": str(len(DATA))})
    assert await fetch(api, resolver=resolver, transport=transport) == DATA
    assert resolver.calls == [HOST]
    assert transport.calls == [(URL, ("142.250.190.14", "2607:f8b0:4009:81a::200e"))]


@pytest.mark.parametrize(
    "url",
    [
        URL.replace("https:", "http:"),
        URL + "?secret=hidden",
        URL + "#fragment",
        URL.replace(HOST, HOST + ":443"),
        URL.replace(HOST, "user@" + HOST),
        URL.replace(HOST, "127.0.0.1"),
        URL.replace(HOST, HOST + ".evil"),
        URL.replace("/go/", "/%67o/"),
        URL.replace("/go/", "/go/../go/"),
        URL + "\r\n",
    ],
)
async def test_revalidates_constructed_entry_before_dns(api: Any, url: str) -> None:
    artifact = entry().model_copy(update={"url": url})
    resolver, transport = Resolver(), Transport()
    with pytest.raises(api.ToolchainFetchError, match="toolchain_fetch_entry_invalid"):
        await fetch(api, artifact=artifact, resolver=resolver, transport=transport)
    assert resolver.calls == [] and transport.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("archive_bytes", True),
        ("archive_bytes", 0),
        ("archive_bytes", 1.5),
        ("sha256", "A" * 64),
        ("version", "1.27"),
        ("platform", "windows_amd64"),
        ("stack", "python"),
        ("tar_bytes_limit", 1),
        ("directory_entries_limit", False),
        ("archive_entries_limit", -1),
    ],
)
async def test_revalidates_all_catalog_data(api: Any, field: str, value: object) -> None:
    artifact = entry().model_copy(update={field: value})
    resolver = Resolver()
    with pytest.raises(api.ToolchainFetchError, match="toolchain_fetch_entry_invalid"):
        await fetch(api, artifact=artifact, resolver=resolver)
    assert not resolver.calls


async def test_rejects_duck_types_and_subclasses(api: Any) -> None:
    class Subclass(ToolchainArtifact):
        pass

    for artifact in (entry().to_payload(), Subclass(**entry().model_dump()), object()):
        with pytest.raises(api.ToolchainFetchError, match="toolchain_fetch_entry_invalid"):
            await fetch(api, artifact=artifact)


@pytest.mark.parametrize(
    "answers",
    [
        [],
        ["142.250.190.14", "127.0.0.1"],
        ["10.0.0.1"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["224.0.0.1"],
        ["240.0.0.1"],
        ["::"],
        ["::1"],
        ["ff0e::1"],
        ["fe80::1%eth0"],
        ["::ffff:127.0.0.1"],
        ["192.0.2.1"],
        ["not-an-ip"],
        ["142.250.190.14"] * 17,
    ],
)
async def test_all_dns_candidates_must_be_public_unicast(api: Any, answers: list[str]) -> None:
    transport = Transport()
    with pytest.raises(api.ToolchainFetchError, match="toolchain_fetch_dns"):
        await fetch(api, resolver=Resolver(answers), transport=transport)
    assert not transport.calls


@pytest.mark.parametrize(
    "deadline", [True, None, "123", float("nan"), float("inf"), float("-inf"), 0, -1]
)
async def test_bad_deadline_fails_before_dns(api: Any, deadline: Any) -> None:
    resolver = Resolver()
    with pytest.raises(api.ToolchainFetchError):
        await api.fetch_toolchain_artifact(entry(), deadline=deadline, resolver=resolver)
    assert not resolver.calls


@pytest.mark.parametrize("status", [100, 206, 301, 302, 307, 308, 404, 500, True])
async def test_only_http_200_without_redirect(api: Any, status: int) -> None:
    transport = Transport(status=status, headers={"Location": URL})
    with pytest.raises(api.ToolchainFetchError, match="toolchain_fetch_http"):
        await fetch(api, transport=transport)
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": "0"},
        {"Content-Length": str(len(DATA) + 1)},
        {"Content-Length": "+40"},
        {"Content-Length": "1e2"},
        {"Content-Length": "9" * 21},
        {"Content-Length": str(len(DATA)), "content-length": str(len(DATA))},
        {"Transfer-Encoding": "gzip"},
        {"Transfer-Encoding": "chunked", "Content-Length": "40"},
        {"Content-Encoding": "gzip"},
        {"Content-Encoding": "br"},
        {"Bad Name": "value"},
        {"X-Test": "secret\r\n"},
        {"X-Test": "\x00"},
        {"X-Test": "a" * 8192},
        {f"X-{i}": "v" for i in range(101)},
        {f"X-{i}": "v" * 1000 for i in range(70)},
    ],
)
async def test_injected_transport_obeys_same_header_limits(
    api: Any, headers: dict[str, str]
) -> None:
    with pytest.raises(api.ToolchainFetchError):
        await fetch(api, transport=Transport(headers=headers))


@pytest.mark.parametrize("body", [b"", DATA[:-1], DATA + b"x", b"x" * len(DATA)])
async def test_exact_catalog_length_and_hash(api: Any, body: bytes) -> None:
    with pytest.raises(api.ToolchainFetchError):
        await fetch(api, transport=Transport(body))


async def test_sdk_larger_than_wheel_limit_is_allowed(api: Any) -> None:
    data = b"a" * (64 * 1024**2 + 1)
    assert await fetch(api, artifact=entry(data), transport=Transport(data), seconds=10) == data


@pytest.mark.parametrize("phase", ["dns", "transport"])
async def test_total_timeout_and_cancellation(api: Any, phase: str) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def slow(*args: Any, **kwargs: Any) -> Any:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    resolver, transport = Resolver(), Transport()
    setattr(resolver if phase == "dns" else transport, "resolve" if phase == "dns" else "get", slow)
    with pytest.raises(api.ToolchainFetchError, match="toolchain_fetch_timeout"):
        await fetch(api, resolver=resolver, transport=transport, seconds=0.02)
    assert stopped.is_set()
    stopped.clear()
    task = asyncio.create_task(fetch(api, resolver=resolver, transport=transport))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


class Writer:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False
        self.aborted = False
        self.transport = SimpleNamespace(abort=self.abort)

    def abort(self) -> None:
        self.aborted = True

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def reader(wire: bytes) -> asyncio.StreamReader:
    result = asyncio.StreamReader()
    result.feed_data(wire)
    result.feed_eof()
    return result


async def socket_fetch(
    api: Any,
    monkeypatch: pytest.MonkeyPatch,
    wire: bytes,
    *,
    writer: Writer | None = None,
    seconds: float = 2,
) -> bytes:
    socket_writer = Writer() if writer is None else writer

    async def connect(*args: Any, **kwargs: Any) -> Any:
        assert args == ("142.250.190.14", 443)
        assert kwargs["server_hostname"] == HOST
        return reader(wire), socket_writer

    monkeypatch.setattr(api, "_tls_context", object)
    monkeypatch.setattr(asyncio, "open_connection", connect)
    return cast(
        bytes,
        await api.fetch_toolchain_artifact(
            entry(), deadline=time.monotonic() + seconds, resolver=Resolver()
        ),
    )


@pytest.mark.parametrize("framing", ["length", "eof", "chunked"])
async def test_pinned_transport_real_parser_and_identity_request(
    api: Any, monkeypatch: pytest.MonkeyPatch, framing: str
) -> None:
    for name in ("HTTPS_PROXY", "ALL_PROXY", "SSLKEYLOGFILE", "NETRC"):
        monkeypatch.setenv(name, "private:secret@127.0.0.1")
    body, header = DATA, b""
    if framing == "length":
        header = f"Content-Length: {len(DATA)}\r\n".encode()
    elif framing == "chunked":
        header = b"Transfer-Encoding: chunked\r\n"
        body = f"{len(DATA):x}\r\n".encode() + DATA + b"\r\n0\r\n\r\n"
    writer = Writer()
    assert (
        await socket_fetch(
            api, monkeypatch, b"HTTP/1.1 200 OK\r\n" + header + b"\r\n" + body, writer=writer
        )
        == DATA
    )
    assert writer.closed
    assert writer.data.startswith(b"GET /go/go1.27.3.linux-amd64.tar.gz HTTP/1.1\r\n")
    assert b"Host: dl.google.com\r\n" in writer.data
    assert b"Accept-Encoding: identity\r\n" in writer.data
    assert b"Connection: close\r\n" in writer.data
    assert b"secret" not in writer.data and b"Authorization" not in writer.data


@pytest.mark.parametrize(
    "wire",
    [
        b"HTTP/1.1 302 Found\r\nLocation: https://other.invalid/\r\n\r\n",
        b"HTTP/1.1 200 OK\n\n",
        b"HTTP/2 200 OK\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\ncontent-length: 40\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n Fold: value\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nX: \x00\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n" + DATA,
        b"HTTP/1.1 200 OK\r\n\r\n" + DATA + b"x",
        b"HTTP/1.1 200 OK\r\n\r\n" + DATA[:-1],
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nX: trailer\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;foo=bar\r\nx\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nffffffffffffffff\r\n",
        b"HTTP/1.1 200 OK\r\nX: " + b"a" * 8192 + b"\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n" + b"".join(f"X-{i}: v\r\n".encode() for i in range(101)) + b"\r\n",
    ],
)
async def test_strict_wire_rejection_closes_writer(
    api: Any, monkeypatch: pytest.MonkeyPatch, wire: bytes
) -> None:
    writer = Writer()
    with pytest.raises(api.ToolchainFetchError):
        await socket_fetch(api, monkeypatch, wire, writer=writer)
    assert writer.closed


async def test_declared_length_cannot_hide_trailing_data(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = f"HTTP/1.1 200 OK\r\nContent-Length: {len(DATA)}\r\n\r\n".encode() + DATA + b"hidden"
    with pytest.raises(api.ToolchainFetchError, match="toolchain_fetch_size"):
        await socket_fetch(api, monkeypatch, wire)


@pytest.mark.parametrize("phase", ["read", "close"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_socket_deadline_and_cancellation_abort_without_leaked_tasks(
    api: Any, monkeypatch: pytest.MonkeyPatch, phase: str, cancel: bool
) -> None:
    entered = asyncio.Event()

    class SlowWriter(Writer):
        async def wait_closed(self) -> None:
            entered.set()
            await asyncio.Event().wait()

    writer = SlowWriter()

    async def connect(*args: Any, **kwargs: Any) -> Any:
        if phase == "read":
            entered.set()
            return asyncio.StreamReader(), writer
        return reader(b"HTTP/1.1 200 OK\r\n\r\n" + DATA), writer

    monkeypatch.setattr(api, "_tls_context", object)
    monkeypatch.setattr(asyncio, "open_connection", connect)
    baseline = asyncio.all_tasks()
    task = asyncio.create_task(
        api.fetch_toolchain_artifact(
            entry(), deadline=time.monotonic() + (2 if cancel else 0.03), resolver=Resolver()
        )
    )
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else api.ToolchainFetchError):
        await asyncio.wait_for(task, timeout=0.5)
    assert writer.closed and writer.aborted
    assert asyncio.all_tasks() == baseline


def test_linux_ca_explicit_and_no_environment_defaults(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    context = SimpleNamespace(
        load_verify_locations=lambda **kw: calls.append(kw),
        set_alpn_protocols=lambda p: calls.append(p),
    )
    monkeypatch.setattr(api.sys, "platform", "linux")
    monkeypatch.setattr(api.ssl, "SSLContext", lambda protocol: context)
    monkeypatch.setattr(api.ssl, "create_default_context", lambda: pytest.fail("ambient TLS"))
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE"):
        monkeypatch.setenv(name, "forbidden-env")
    assert api._tls_context() is context
    assert calls == [{"cafile": "/etc/ssl/certs/ca-certificates.crt"}, ["http/1.1"]]
    assert context.minimum_version == api.ssl.TLSVersion.TLSv1_2


def test_windows_ca_uses_only_server_trusted_system_certificates(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    context = SimpleNamespace(
        load_verify_locations=lambda **kw: calls.append(kw), set_alpn_protocols=lambda p: None
    )
    monkeypatch.setattr(api.sys, "platform", "win32")
    monkeypatch.setattr(api.ssl, "SSLContext", lambda protocol: context)
    monkeypatch.setattr(
        api.ssl,
        "enum_certificates",
        lambda store: [
            (b"untrusted", "x509_asn", False),
            (store.encode(), "x509_asn", True),
            (b"client-only", "x509_asn", {api.ssl.Purpose.CLIENT_AUTH.oid}),
            (b"wrong-encoding", "pkcs_7_asn", True),
        ],
        raising=False,
    )
    api._tls_context()
    assert calls == [{"cadata": b"CAROOT"}]


def cli(
    api: Any, monkeypatch: pytest.MonkeyPatch, raw: bytes, argv: list[str] | None = None
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(api.sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    monkeypatch.setattr(api.sys, "stdout", out)
    monkeypatch.setattr(api.sys, "stderr", err)
    status = api.main([] if argv is None else argv)
    return status, out.getvalue(), err.getvalue()


def payload(timeout: object = 1) -> bytes:
    return json.dumps({"artifact": entry().to_payload(), "timeout_seconds": timeout}).encode()


def test_cli_base64_only_and_absolute_deadline(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    before = time.monotonic()

    async def fetched(artifact: ToolchainArtifact, *, deadline: float) -> bytes:
        assert artifact == entry() and before < deadline <= time.monotonic() + 1
        return DATA

    monkeypatch.setattr(api, "fetch_toolchain_artifact", fetched)
    status, out, err = cli(api, monkeypatch, payload())
    assert status == 0 and not err
    assert base64.b64decode(out, validate=True) == DATA


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b"null",
        b"NaN",
        b"\xff",
        b'{"artifact":{},"artifact":{},"timeout_seconds":1}',
        b'{"artifact":{"stack":"go","stack":"go"},"timeout_seconds":1}',
        payload().replace(b'"timeout_seconds": 1', b'"timeout_seconds": NaN'),
        payload().replace(b'"timeout_seconds": 1', b'"timeout_seconds": Infinity'),
        payload().replace(b'"timeout_seconds": 1', b'"timeout_seconds": 1e999'),
        payload().replace(b'"timeout_seconds": 1', b'"timeout_seconds": 1, "extra": true'),
        payload().replace(b'"stack": "go"', b'"stack": "go", "extra": true'),
        b"x" * 65537,
        b"[" * 1500,
    ],
    ids=[
        "missing",
        "array",
        "null",
        "nan",
        "encoding",
        "duplicate-outer",
        "duplicate-inner",
        "nan-timeout",
        "infinity",
        "overflow",
        "extra-outer",
        "extra-inner",
        "oversize",
        "deep",
    ],
)
def test_cli_strict_input_before_fetch(
    api: Any, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    monkeypatch.setattr(api, "fetch_toolchain_artifact", lambda *a, **kw: pytest.fail("fetch"))
    assert cli(api, monkeypatch, raw) == (2, "", "toolchain_fetch_worker_invalid\n")


@pytest.mark.parametrize("timeout", [True, False, 0, -1, 600.01, "1", None])
def test_cli_timeout_rejection(api: Any, monkeypatch: pytest.MonkeyPatch, timeout: object) -> None:
    assert cli(api, monkeypatch, payload(timeout)) == (2, "", "toolchain_fetch_worker_invalid\n")


def test_cli_inclusive_input_and_timeout_bounds(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fetched(*args: Any, **kwargs: Any) -> bytes:
        return DATA

    monkeypatch.setattr(api, "fetch_toolchain_artifact", fetched)
    assert cli(api, monkeypatch, payload(600).ljust(65536, b" "))[0] == 0


@pytest.mark.parametrize(
    "error",
    [
        ValueError("secret token"),
        OSError("private URL"),
        asyncio.CancelledError(),
        KeyboardInterrupt(),
    ],
)
def test_cli_errors_finite_and_no_partial_stdout(
    api: Any, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    async def failed(*args: Any, **kwargs: Any) -> bytes:
        raise error

    monkeypatch.setattr(api, "fetch_toolchain_artifact", failed)
    status, out, err = cli(api, monkeypatch, payload())
    assert status == 2 and not out
    assert err in {"toolchain_fetch_worker_invalid\n", "toolchain_fetch_cancelled\n"}


def test_cli_rejects_arguments_before_read(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli(api, monkeypatch, payload(), [URL]) == (2, "", "toolchain_fetch_worker_invalid\n")


async def test_entry_extra_data_is_not_silently_dropped(api: Any) -> None:
    resolver = Resolver()
    with pytest.raises(api.ToolchainFetchError, match="toolchain_fetch_entry_invalid"):
        await fetch(
            api, artifact=entry().model_copy(update={"authority": "install"}), resolver=resolver
        )
    assert not resolver.calls


async def test_deadline_is_shared_across_dns_and_transfer(api: Any) -> None:
    class DelayedResolver(Resolver):
        async def resolve(self, host: str) -> list[str]:
            await asyncio.sleep(0.02)
            return await super().resolve(host)

    class DelayedTransport(Transport):
        async def get(self, url: str, *, resolved_addresses: tuple[str, ...]) -> HttpResponse:
            await asyncio.sleep(0.02)
            return await super().get(url, resolved_addresses=resolved_addresses)

    with pytest.raises(api.ToolchainFetchError, match="toolchain_fetch_timeout"):
        await fetch(api, resolver=DelayedResolver(), transport=DelayedTransport(), seconds=0.03)


async def test_deadline_rechecked_after_non_yielding_transport(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [10.0]

    class LateTransport(Transport):
        async def get(self, url: str, *, resolved_addresses: tuple[str, ...]) -> HttpResponse:
            clock[0] = 12.0
            return self.response

    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(api.ToolchainFetchError, match="toolchain_fetch_timeout"):
        await api.fetch_toolchain_artifact(
            entry(), deadline=11.0, resolver=Resolver(), transport=LateTransport()
        )


@pytest.mark.parametrize("phase", ["dns", "transport"])
async def test_diagnostics_never_expose_target_or_credentials(api: Any, phase: str) -> None:
    async def failed(*args: Any, **kwargs: Any) -> Any:
        raise OSError("private credential in target URL")

    resolver, transport = Resolver(), Transport()
    setattr(
        resolver if phase == "dns" else transport, "resolve" if phase == "dns" else "get", failed
    )
    with pytest.raises(api.ToolchainFetchError) as error:
        await fetch(api, resolver=resolver, transport=transport)
    assert str(error.value) == (
        "toolchain_fetch_dns" if phase == "dns" else "toolchain_fetch_transport"
    )


@pytest.mark.parametrize("phase", ["connect", "drain"])
async def test_cancel_during_connect_or_drain_has_no_background_cleanup(
    api: Any, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    entered = asyncio.Event()

    class DrainWriter(Writer):
        async def drain(self) -> None:
            entered.set()
            await asyncio.Event().wait()

    writer = DrainWriter()

    async def connect(*args: Any, **kwargs: Any) -> Any:
        if phase == "connect":
            entered.set()
            await asyncio.Event().wait()
        return asyncio.StreamReader(), writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    monkeypatch.setattr(api, "_tls_context", object)
    baseline = asyncio.all_tasks()
    task = asyncio.create_task(
        api.fetch_toolchain_artifact(entry(), deadline=time.monotonic() + 2, resolver=Resolver())
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert asyncio.all_tasks() == baseline
    if phase == "drain":
        assert writer.closed and writer.aborted


async def test_retry_stays_pinned_and_does_not_resolve_again(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    writer = Writer()

    async def connect(address: str, port: int, **kwargs: Any) -> Any:
        calls.append(address)
        assert port == 443 and kwargs["server_hostname"] == HOST
        if len(calls) == 1:
            raise OSError("first address unavailable")
        return reader(b"HTTP/1.1 200 OK\r\n\r\n" + DATA), writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    monkeypatch.setattr(api, "_tls_context", object)
    resolver = Resolver(["142.250.190.14", "142.250.190.15"])
    assert (
        await api.fetch_toolchain_artifact(
            entry(), deadline=time.monotonic() + 2, resolver=resolver
        )
        == DATA
    )
    assert calls == resolver.answers and resolver.calls == [HOST] and writer.closed


def test_cli_code_is_revalidated_before_stderr(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    error = api.ToolchainFetchError("toolchain_fetch_sha")
    error.code = "private URL and credential"

    async def failed(*args: Any, **kwargs: Any) -> bytes:
        raise error

    monkeypatch.setattr(api, "fetch_toolchain_artifact", failed)
    assert cli(api, monkeypatch, payload()) == (2, "", "toolchain_fetch_worker_invalid\n")


def test_cold_cli_rejects_invalid_control_without_network() -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-m", MODULE],
        input=b"{}",
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2 and not result.stdout
    assert result.stderr.splitlines() == [b"toolchain_fetch_worker_invalid"]


@pytest.mark.parametrize("platform", ["unsupported", "win32", "linux"])
def test_unavailable_system_trust_fails_closed(
    api: Any, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(api.sys, "platform", platform)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise OSError("private CA location")

    monkeypatch.setattr(api.ssl, "SSLContext", unavailable)
    with pytest.raises(api.ToolchainFetchError, match="toolchain_fetch_tls_unavailable"):
        api._tls_context()
