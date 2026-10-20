"""Binary fixtures and fake sockets only; never download/import a wheel."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import io
import json
import stat
import struct
import subprocess
import sys
import zipfile
from contextlib import nullcontext
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any

import pytest

HOST = "files.pythonhosted.org"
FILENAME = "demo_pkg-1.0-py3-none-any.whl"
URL = "https://" + HOST + "/packages/ab/cd/" + FILENAME


@pytest.fixture
def api() -> Any:
    return importlib.import_module("agent_hub.skills.sandbox.python_input_fetch")


def wheel(extra: list[tuple[str | zipfile.ZipInfo, bytes]] | None = None,
          *, metadata: bytes = b"Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.0\n\n",
          tags: bytes = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in [
            ("demo_pkg/__init__.py", b"raise AssertionError('never execute')\n"),
            ("demo_pkg-1.0.dist-info/METADATA", metadata),
            ("demo_pkg-1.0.dist-info/WHEEL", tags),
            ("demo_pkg-1.0.dist-info/RECORD", b""),
            *(extra or []),
        ]:
            if isinstance(name, str) and "\\" in name:
                info = zipfile.ZipInfo(name)
                info.filename = info.orig_filename = name
                name = info
            archive.writestr(name, raw)
    return output.getvalue()


def entry(api: Any, raw: bytes | None = None) -> Any:
    data = wheel() if raw is None else raw
    return api.WheelDownload(FILENAME, "demo-pkg", "1.0", hashlib.sha256(data).hexdigest(), len(data), URL)


class Resolver:
    def __init__(self, answers: list[str] | None = None) -> None:
        self.answers = ["151.101.0.223"] if answers is None else answers
        self.calls: list[str] = []

    async def resolve(self, host: str) -> list[str]:
        self.calls.append(host)
        return self.answers


class Transport:
    def __init__(self, api: Any, raw: bytes, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.api, self.raw, self.status, self.headers = api, raw, status, headers or {}
        self.calls: list[Any] = []

    async def get(self, url: str, *, resolved_addresses: tuple[str, ...]) -> Any:
        self.calls.append((url, resolved_addresses))
        return self.api.HttpResponse(self.status, self.headers, self.raw)


async def test_valid_binary_wheel_returns_original_bytes(api: Any) -> None:
    raw = wheel([("demo_pkg.egg-info/PKG-INFO", b"data"),
                 ("demo_pkg/_vendor/vendor.dist-info/METADATA", b"vendor metadata")])
    resolver, transport = Resolver(), Transport(api, raw)
    assert await api.fetch_wheel(entry(api, raw), timeout_seconds=2.0,
                                 resolver=resolver, transport=transport) == raw
    assert resolver.calls == [HOST]
    assert transport.calls == [(URL, ("151.101.0.223",))]
    api.validate_wheel_bytes(entry(api, raw), raw)


@pytest.mark.parametrize("url", [
    URL.replace("https:", "http:"), URL.replace(HOST, "127.0.0.1"),
    URL.replace(HOST, HOST + ".evil"), URL.replace(HOST, "user@" + HOST),
    URL.replace(HOST, HOST + ":444"), URL + "?token=private", URL + "#fragment",
    URL.replace("/ab/", "/../"), URL.replace("/ab/", "/%61b/"),
    URL.replace("/ab/", "//"), URL.replace("/ab/", "/. /"), URL + "\n",
    URL.replace(FILENAME, "other.whl"), URL.replace("packages", "simple"),
])
async def test_invalid_url_refused_before_dns_or_transport(api: Any, url: str) -> None:
    resolver, transport = Resolver(), Transport(api, wheel())
    good = entry(api)
    object.__setattr__(good, "url", url)
    with pytest.raises(api.PythonInputFetchError):
        await api.fetch_wheel(good, timeout_seconds=1.0, resolver=resolver, transport=transport)
    assert not resolver.calls and not transport.calls


@pytest.mark.parametrize("answers", [[], ["151.101.0.223", "127.0.0.1"], ["169.254.169.254"],
    ["100.64.0.1"], ["ff02::1"], ["::ffff:127.0.0.1"], ["fe80::1%eth0"], ["not-an-ip"]])
async def test_all_dns_answers_must_be_public_and_unscoped(api: Any, answers: list[str]) -> None:
    raw = wheel()
    transport = Transport(api, raw)
    with pytest.raises(api.PythonInputFetchError, match="fetch_dns"):
        await api.fetch_wheel(entry(api, raw), timeout_seconds=1.0,
                              resolver=Resolver(answers), transport=transport)
    assert not transport.calls


@pytest.mark.parametrize("status,headers", [(301, {"location": URL}), (302, {}), (404, {}),
    (200, {"Content-Encoding": "gzip"}), (200, {"Content-Length": "999999999"})])
async def test_no_redirect_error_status_or_compressed_body(api: Any, status: int,
                                                         headers: dict[str, str]) -> None:
    raw = wheel()
    transport = Transport(api, raw, status, headers)
    with pytest.raises(api.PythonInputFetchError):
        await api.fetch_wheel(entry(api, raw), timeout_seconds=1.0,
                              resolver=Resolver(), transport=transport)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("field,value", [("size_bytes", True), ("size_bytes", 0),
    ("size_bytes", 64 * 1024**2 + 1), ("sha256", "A" * 64), ("filename", "demo_pkg-1.0-cp312-none-any.whl")])
def test_entry_types_and_first_recipe_constraints(api: Any, field: str, value: Any) -> None:
    with pytest.raises(api.PythonInputFetchError):
        replace(entry(api), **{field: value})


@pytest.mark.parametrize("data", [b"", b"not a wheel", b"different content"])
def test_exact_size_sha_and_zip_required(api: Any, data: bytes) -> None:
    with pytest.raises(api.PythonInputFetchError):
        api.validate_wheel_bytes(entry(api), data)
    if data:
        with pytest.raises(api.PythonInputFetchError):
            api.validate_wheel_bytes(entry(api, data), data)


@pytest.mark.parametrize("path", ["../escape", "/escape", "a\\escape", "C:/escape", "a/./b", "a//b",
    "demo_pkg-1.0.dist-info/../evil", "second-1.0.dist-info/METADATA", "demo_pkg/__init__.py"])
def test_unsafe_or_duplicate_zip_paths(api: Any, path: str) -> None:
    with pytest.warns(UserWarning) if path == "demo_pkg/__init__.py" else nullcontext():
        raw = wheel([(path, b"payload")])
    with pytest.raises(api.PythonInputFetchError):
        api.validate_wheel_bytes(entry(api, raw), raw)


def test_link_and_special_zip_members_rejected(api: Any) -> None:
    for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFCHR | 0o600):
        info = zipfile.ZipInfo("demo_pkg/unsafe")
        info.create_system = 3
        info.external_attr = mode << 16
        raw = wheel([(info, b"target")])
        with pytest.raises(api.PythonInputFetchError):
            api.validate_wheel_bytes(entry(api, raw), raw)


@pytest.mark.parametrize("metadata,tags", [
    (b"Name: other\nVersion: 1.0\n", b""),
    (b"Name: demo-pkg\nName: other\nVersion: 1.0\n", b""),
    (b"Name: demo-pkg\nVersion: 1.1\n", b""),
    (b"Name: demo-pkg\nVersion: 1.0\n", b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n"),
    (b"Name: demo-pkg\nVersion: 1.0\n", b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp312-cp312-linux_x86_64\n"),
])
def test_wheel_metadata_identity_and_pure_tag(api: Any, metadata: bytes, tags: bytes) -> None:
    raw = wheel(metadata=metadata, tags=tags)
    with pytest.raises(api.PythonInputFetchError):
        api.validate_wheel_bytes(entry(api, raw), raw)


async def test_cancellation_propagates_and_total_dns_deadline_is_bounded(api: Any) -> None:
    class Slow(Resolver):
        async def resolve(self, host: str) -> list[str]:
            await asyncio.sleep(30)
            return []
    task = asyncio.create_task(api.fetch_wheel(entry(api), timeout_seconds=2.0, resolver=Slow()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(api.PythonInputFetchError, match="fetch_timeout"):
        await api.fetch_wheel(entry(api), timeout_seconds=0.01, resolver=Slow())


async def test_timeout_covers_transport_and_not_only_dns(api: Any) -> None:
    class SlowTransport:
        async def get(self, url: str, *, resolved_addresses: tuple[str, ...]) -> Any:
            await asyncio.sleep(30)
    with pytest.raises(api.PythonInputFetchError, match="fetch_timeout"):
        await api.fetch_wheel(entry(api), timeout_seconds=0.01,
                              resolver=Resolver(), transport=SlowTransport())


def test_worker_cancellation_is_finite_not_a_traceback(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {**asdict(entry(api)), "timeout_seconds": 1.0}
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(api.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    monkeypatch.setattr(api.sys, "stdout", out)
    monkeypatch.setattr(api.sys, "stderr", err)
    async def cancelled(*args: Any, **kwargs: Any) -> bytes:
        raise asyncio.CancelledError
    monkeypatch.setattr(api, "fetch_wheel", cancelled)
    assert api.main(["--worker"]) == 2
    assert not out.getvalue() and err.getvalue() == "fetch_cancelled\n"


class Writer:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False
        self.aborted = False
        self.transport = SimpleNamespace(abort=self.abort)

    def abort(self) -> None:
        self.aborted = True

    def write(self, value: bytes) -> None:
        self.data += value

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


async def test_strict_transport_pins_ip_host_sni_and_ignores_proxy_env(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    reader, writer, calls = asyncio.StreamReader(), Writer(), []
    reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    reader.feed_eof()
    marker = object()
    monkeypatch.setattr(api, "_tls_context", lambda: marker)
    monkeypatch.setenv("HTTPS_PROXY", "http://private:secret@127.0.0.1")
    async def connect(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return reader, writer
    monkeypatch.setattr(asyncio, "open_connection", connect)
    transport = api._WheelTransport(2, asyncio.get_running_loop().time() + 2)
    response = await transport.get(URL, resolved_addresses=("151.101.0.223",))
    assert response.body == b"ok" and writer.closed
    assert calls[0][0] == ("151.101.0.223", 443)
    assert calls[0][1]["server_hostname"] == HOST and calls[0][1]["ssl"] is marker
    assert b"Host: files.pythonhosted.org\r\n" in writer.data
    assert b"Accept-Encoding: identity\r\n" in writer.data


async def test_cancelled_connection_does_not_wait_for_tls_shutdown(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowWriter(Writer):
        async def wait_closed(self) -> None:
            await asyncio.sleep(30)
    reader, writer = asyncio.StreamReader(), SlowWriter()
    monkeypatch.setattr(api, "_tls_context", lambda: object())
    async def connect(*args: Any, **kwargs: Any) -> Any:
        return reader, writer
    monkeypatch.setattr(asyncio, "open_connection", connect)
    transport = api._WheelTransport(2, asyncio.get_running_loop().time() + 2)
    task = asyncio.create_task(transport.get(URL, resolved_addresses=("151.101.0.223",)))
    await asyncio.sleep(0)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=0.03)
    if not done:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert done, "cancellation waited for TLS shutdown"
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed and writer.aborted


@pytest.mark.parametrize("wire", [
    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\ncontent-length: 2\r\n\r\nok",
    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\nok",
    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nextra",
])
async def test_strict_http_framing(api: Any, monkeypatch: pytest.MonkeyPatch, wire: bytes) -> None:
    monkeypatch.setattr(api, "_tls_context", lambda: object())
    reader = asyncio.StreamReader()
    reader.feed_data(wire)
    reader.feed_eof()
    transport = api._WheelTransport(2, asyncio.get_running_loop().time() + 2)
    with pytest.raises(api.PythonInputFetchError):
        await transport._read_response(reader)


def test_worker_exact_input_and_private_binary_response(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = wheel()
    payload = {**asdict(entry(api, raw)), "timeout_seconds": 1.0}
    stdout, stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr(api.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    monkeypatch.setattr(api.sys, "stdout", stdout)
    monkeypatch.setattr(api.sys, "stderr", stderr)
    async def fetch(selected: Any, **kwargs: Any) -> bytes:
        assert selected == entry(api, raw)
        return raw
    monkeypatch.setattr(api, "fetch_wheel", fetch)
    assert api.main(["--worker"]) == 0
    result = json.loads(stdout.getvalue())
    assert set(result) == {"schema_version", "body_base64"} and result["schema_version"] == 1
    assert base64.b64decode(result["body_base64"], validate=True) == raw and not stderr.getvalue()


def test_tls_context_never_selects_environment_ca_or_keylog(api: Any, monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Any) -> None:
    calls: list[Any] = []
    class Context:
        minimum_version: Any = None

        def load_verify_locations(self, **kwargs: Any) -> None:
            calls.append(kwargs)

        def set_alpn_protocols(self, protocols: Any) -> None:
            assert protocols == ["http/1.1"]
    context = Context()
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "private-ca"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "private-certs"))
    monkeypatch.setenv("SSLKEYLOGFILE", str(tmp_path / "private-keylog"))
    monkeypatch.setattr(api.sys, "platform", "linux")
    monkeypatch.setattr(api.ssl, "create_default_context", lambda *a, **k: pytest.fail("ambient TLS"))
    monkeypatch.setattr(api.ssl, "SSLContext", lambda protocol: context)
    assert api._tls_context() is context
    assert calls == [{"cafile": "/etc/ssl/certs/ca-certificates.crt"}]
    assert not list(tmp_path.iterdir())


def test_windows_tls_uses_only_server_trusted_os_certificates(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    context = SimpleNamespace(load_verify_locations=lambda **kwargs: calls.append(kwargs),
                              set_alpn_protocols=lambda protocols: None)
    monkeypatch.setattr(api.sys, "platform", "win32")
    monkeypatch.setattr(api.ssl, "SSLContext", lambda protocol: context)
    monkeypatch.setattr(api.ssl, "enum_certificates", lambda store: [
        (b"untrusted", "x509_asn", False),
        (b"wrong-purpose", "x509_asn", {api.ssl.Purpose.CLIENT_AUTH.oid}),
        (store.encode(), "x509_asn", True),
    ], raising=False)
    assert api._tls_context() is context
    assert calls == [{"cadata": b"CAROOT"}]


def test_worker_cold_module_refuses_missing_fields_without_network() -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "agent_hub.skills.sandbox.python_input_fetch", "--worker"],
        input=b"{}", capture_output=True, timeout=15, check=False,
    )
    assert result.returncode == 2 and result.stdout == b""
    assert result.stderr.strip() == b"fetch_worker_invalid"


@pytest.mark.parametrize("flag", [1, 0x20, 0x40, 0x2000])
def test_zip_unsafe_flags_are_rejected_without_reading_hooks(api: Any, flag: int) -> None:
    data = bytearray(wheel())
    offset = data.index(b"PK\x01\x02")
    data[offset + 8:offset + 10] = flag.to_bytes(2, "little")
    raw = bytes(data)
    with pytest.raises(api.PythonInputFetchError):
        api.validate_wheel_bytes(entry(api, raw), raw)


@pytest.mark.parametrize("limit", ["_MAX_MEMBERS", "_MAX_EXPANDED", "_MAX_METADATA"])
def test_zip_budgets_are_enforced(api: Any, monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    monkeypatch.setattr(api, limit, 2)
    raw = wheel()
    with pytest.raises(api.PythonInputFetchError, match="fetch_wheel_limit"):
        api.validate_wheel_bytes(entry(api, raw), raw)


def test_false_zip_entry_count_refused_before_zipfile_allocates_members(api: Any,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    data = bytearray(wheel())
    end = data.rindex(b"PK\x05\x06")
    struct.pack_into("<HH", data, end + 8, 1, 1)
    raw = bytes(data)
    monkeypatch.setattr(api.zipfile, "ZipFile", lambda *a, **k: pytest.fail("unbounded central parsing"))
    with pytest.raises(api.PythonInputFetchError):
        api.validate_wheel_bytes(entry(api, raw), raw)


@pytest.mark.parametrize("raw", [b"{}", b'{"filename":1,"filename":2}', b"NaN", b"x" * 65537],
                         ids=["missing", "duplicate", "nonfinite", "oversize"])
def test_worker_rejects_malformed_duplicate_oversize_input_without_fetch(
    api: Any, monkeypatch: pytest.MonkeyPatch, raw: bytes,
) -> None:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(api.sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    monkeypatch.setattr(api.sys, "stdout", out)
    monkeypatch.setattr(api.sys, "stderr", err)
    monkeypatch.setattr(api, "fetch_wheel", lambda *a, **k: pytest.fail("fetch before input validation"))
    assert api.main(["--worker"]) == 2
    assert not out.getvalue() and err.getvalue() == "fetch_worker_invalid\n"


def batch_payload(api: Any) -> dict[str, Any]:
    first = asdict(entry(api))
    second = {**first, "version": "1.1", "filename": FILENAME.replace("1.0", "1.1"),
              "url": URL.replace("1.0", "1.1")}
    return {"artifacts": [first, second], "timeout_seconds": 10.0}


def batch_cli(api: Any, monkeypatch: pytest.MonkeyPatch, raw: bytes) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(api.sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    monkeypatch.setattr(api.sys, "stdout", out)
    monkeypatch.setattr(api.sys, "stderr", err)
    status = api.main(["--worker-batch"])
    return status, out.getvalue(), err.getvalue()


def test_batch_preserves_order_and_passes_only_remaining_deadline(
    api: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = batch_payload(api)
    clock = [100.0]
    calls: list[tuple[str, float]] = []
    async def fetch(selected: Any, *, timeout_seconds: float) -> bytes:
        calls.append((selected.filename, timeout_seconds))
        clock[0] += 3.0
        return str(selected.version).encode()
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(api, "fetch_wheel", fetch)
    status, out, err = batch_cli(api, monkeypatch, json.dumps(payload).encode())
    assert status == 0 and not err
    assert calls == [(payload["artifacts"][0]["filename"], 10.0),
                     (payload["artifacts"][1]["filename"], 7.0)]
    assert json.loads(out) == {"schema_version": 1, "artifacts": [
        {"filename": item["filename"], "body_base64": base64.b64encode(item["version"].encode()).decode()}
        for item in payload["artifacts"]]}


@pytest.mark.parametrize("case", ["empty", "too_many", "not_list", "duplicate_filename",
    "duplicate_identity", "total_size", "invalid_later_url", "extra_item", "missing_item",
    "extra_outer", "bad_timeout", "bool_timeout"])
def test_batch_preflights_entire_input_before_any_fetch(
    api: Any, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    payload = batch_payload(api)
    items = payload["artifacts"]
    if case == "empty":
        payload["artifacts"] = []
    elif case == "too_many":
        payload["artifacts"] = items[:1] * 1001
    elif case == "not_list":
        payload["artifacts"] = {"wheel": items[0]}
    elif case == "duplicate_filename":
        items[1] = items[0].copy()
    elif case == "duplicate_identity":
        items[1] = {**items[0], "filename": FILENAME.replace("demo_pkg", "Demo_Pkg"),
                    "url": URL.replace("demo_pkg", "Demo_Pkg")}
    elif case == "total_size":
        for item in items:
            item["size_bytes"] = 32 * 1024**2 + 1
    elif case == "invalid_later_url":
        items[1]["url"] += "?secret"
    elif case == "extra_item":
        items[1]["authority"] = True
    elif case == "missing_item":
        del items[1]["sha256"]
    elif case == "extra_outer":
        payload["authority"] = True
    elif case == "bad_timeout":
        payload["timeout_seconds"] = 601
    else:
        payload["timeout_seconds"] = True
    monkeypatch.setattr(api, "fetch_wheel", lambda *a, **k: pytest.fail("fetch before batch preflight"))
    status, out, err = batch_cli(api, monkeypatch, json.dumps(payload).encode())
    assert status == 2 and not out and err.strip() in api._CODES


@pytest.mark.parametrize("raw", [b'{"artifacts":[],"artifacts":[]}',
    b'{"artifacts":[{"filename":1,"filename":2}],"timeout_seconds":1}',
    b'{"artifacts":[],"timeout_seconds":NaN}', b"x" * (512 * 1024 + 1)],
    ids=["outer_duplicate", "item_duplicate", "nonfinite", "oversize"])
def test_batch_strict_json_and_input_budget(api: Any, monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    monkeypatch.setattr(api, "fetch_wheel", lambda *a, **k: pytest.fail("fetch before JSON validation"))
    assert batch_cli(api, monkeypatch, raw) == (2, "", "fetch_worker_invalid\n")


@pytest.mark.parametrize("boundary", ["one", "thousand", "total_64mib", "input_512kib"])
def test_batch_accepts_inclusive_bounds(api: Any, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    payload = batch_payload(api)
    if boundary in {"one", "thousand"}:
        template = payload["artifacts"][0]
        payload["artifacts"] = [{**template, "version": str(index),
            "filename": FILENAME.replace("1.0", str(index)), "url": URL.replace("1.0", str(index))}
            for index in range(1 if boundary == "one" else 1000)]
    elif boundary == "total_64mib":
        for item in payload["artifacts"]:
            item["size_bytes"] = 32 * 1024**2
    raw = json.dumps(payload).encode()
    if boundary == "input_512kib":
        raw = raw.ljust(512 * 1024, b" ")
    calls = []
    async def fetch(selected: Any, **kwargs: Any) -> bytes:
        calls.append(selected.filename)
        return b"validated-by-fetch"
    monkeypatch.setattr(api, "fetch_wheel", fetch)
    status, out, err = batch_cli(api, monkeypatch, raw)
    assert status == 0 and not err
    assert calls == [item["filename"] for item in payload["artifacts"]]
    assert len(json.loads(out)["artifacts"]) == len(calls)


@pytest.mark.parametrize("arguments", [[], ["--worker", "--worker-batch"],
    ["--worker-batch", "--worker-batch"], ["--worker-batch", "extra"]])
def test_worker_flags_are_exclusive(api: Any, monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(api.sys, "stdout", out)
    monkeypatch.setattr(api.sys, "stderr", err)
    monkeypatch.setattr(api.sys, "stdin", SimpleNamespace())
    assert api.main(arguments) == 2
    assert not out.getvalue() and err.getvalue() == "fetch_worker_invalid\n"


@pytest.mark.parametrize("failure", ["error", "cancel", "deadline"])
def test_batch_later_failure_never_outputs_partial_binary(
    api: Any, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    clock, calls = [100.0], []
    async def fetch(selected: Any, **kwargs: Any) -> bytes:
        calls.append(selected.filename)
        if len(calls) == 2:
            if failure == "error":
                raise api.PythonInputFetchError("fetch_sha")
            if failure == "cancel":
                raise asyncio.CancelledError
            clock[0] += 11
        return b"private-wheel-binary"
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(api, "fetch_wheel", fetch)
    status, out, err = batch_cli(api, monkeypatch, json.dumps(batch_payload(api)).encode())
    assert len(calls) == 2 and status == 2 and not out
    assert err == {"error": "fetch_sha\n", "cancel": "fetch_cancelled\n",
                   "deadline": "fetch_timeout\n"}[failure]


def test_batch_expired_deadline_does_not_start_next_fetch(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    clock, calls = [100.0], []
    async def fetch(selected: Any, **kwargs: Any) -> bytes:
        calls.append(selected.filename)
        clock[0] += 11
        return b"private"
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(api, "fetch_wheel", fetch)
    assert batch_cli(api, monkeypatch, json.dumps(batch_payload(api)).encode()) == (2, "", "fetch_timeout\n")
    assert len(calls) == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_batch_deadline_or_cancellation_closes_actual_transport(
    api: Any, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    writer = Writer()
    async def connect(*args: Any, **kwargs: Any) -> Any:
        if cancel:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
        return asyncio.StreamReader(), writer
    monkeypatch.setattr(asyncio, "open_connection", connect)
    monkeypatch.setattr(api, "SystemResolver", Resolver)
    monkeypatch.setattr(api, "_tls_context", lambda: object())
    payload = batch_payload(api)
    payload["timeout_seconds"] = 0.02
    status, out, err = batch_cli(api, monkeypatch, json.dumps(payload).encode())
    assert status == 2 and not out
    assert err == ("fetch_cancelled\n" if cancel else "fetch_timeout\n")
    assert writer.closed and writer.aborted
