"""Offline protocol inputs; no PyPI requests or downloaded distribution bytes."""

import asyncio
import hashlib
import io
import json
import socket
import time
import zipfile
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import replace
from ssl import SSLContext
from typing import Never, Unpack
from uuid import UUID

import pytest

from agent_hub.capabilities.tools.http_read import HttpResponse
from agent_hub.skills.sandbox.python_registry_protocol import ResolutionBudget, ResolutionScope
from agent_hub.skills.sandbox.python_registry_read import RegistryReader
from tests.unit.skills.python_registry_typed_fixture import (
    LimitOverrides,
    ProtocolAPI,
    ReaderAPI,
    WheelEntry,
    object_array,
    object_map,
)


@pytest.fixture(autouse=True)
def offline_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forbid live IO, retaining only Python's synchronous local socketpair construction."""
    pairing = ContextVar("g1_test_socketpair", default=False)
    original_pair = socket.socketpair
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("real network disabled in G1 tests")

    def connect(connection: socket.socket, address: tuple[object, ...] | str | bytes) -> None:
        if pairing.get() and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(connection, address)
        forbidden()

    def connect_ex(connection: socket.socket, address: tuple[object, ...] | str | bytes) -> int:
        if pairing.get() and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect_ex(connection, address)
        return forbidden()

    def socketpair(*args: int, **kwargs: int) -> tuple[socket.socket, socket.socket]:
        token = pairing.set(True)
        try:
            return original_pair(*args, **kwargs)
        finally:
            pairing.reset(token)

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    monkeypatch.setattr(asyncio, "open_connection", forbidden)


def api() -> tuple[ProtocolAPI, ReaderAPI]:
    from agent_hub.skills.sandbox import python_registry_protocol, python_registry_read

    protocol: ProtocolAPI = python_registry_protocol
    reader_api: ReaderAPI = python_registry_read
    return protocol, reader_api


def scope() -> ResolutionScope:
    p, _ = api()
    return p.ResolutionScope(
        UUID(int=1),
        UUID(int=2),
        UUID(int=3),
        "implementer",
        "project",
        "session",
        "1" * 64,
        "2" * 64,
        "3" * 64,
    )


def wheel(
    name: str, version: str = "1.0", deps: Iterable[str] = ()
) -> tuple[WheelEntry, bytes]:
    filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
    prefix = filename.removesuffix("-py3-none-any.whl") + ".dist-info/"
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(
            prefix + "METADATA",
            (
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nRequires-Python: >=3.12\n"
                + "".join(f"Requires-Dist: {dep}\n" for dep in deps)
                + "\n"
            ),
        )
        archive.writestr(
            prefix + "WHEEL", ("Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n")
        )
        archive.writestr(name.replace("-", "_") + "/__init__.py", "VALUE = 1\n")
    body = data.getvalue()
    return {
        "filename": filename,
        "url": "https://files.pythonhosted.org/packages/ab/cd/" + filename,
        "digests": {"sha256": hashlib.sha256(body).hexdigest()},
        "size": len(body),
        "packagetype": "bdist_wheel",
        "requires_python": ">=3.12",
        "yanked": False,
    }, body


def environment() -> dict[str, str]:
    return {
        "implementation_name": "cpython",
        "implementation_version": "3.12.14",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_release": "test",
        "platform_system": "Linux",
        "platform_version": "test",
        "platform_python_implementation": "CPython",
        "python_full_version": "3.12.14",
        "python_version": "3.12",
        "sys_platform": "linux",
    }


class DNS:
    def __init__(self, addresses: list[str] | None = None) -> None:
        self.addresses = ["93.184.216.34"] if addresses is None else addresses
        self.hosts: list[str] = []

    async def resolve(self, host: str) -> list[str]:
        self.hosts.append(host)
        return self.addresses


class Wire:
    def __init__(self) -> None:
        self.documents: dict[str, object] = {}
        self.requests: list[tuple[str, tuple[str, ...]]] = []

    async def get(self, url: str, *, resolved_addresses: tuple[str, ...]) -> HttpResponse:
        self.requests.append((url, resolved_addresses))
        value = self.documents[url]
        if isinstance(value, HttpResponse):
            return value
        kind = "application/vnd.pypi.simple.v1+json" if "/simple/" in url else "application/json"
        return HttpResponse(200, {"Content-Type": kind}, json.dumps(value).encode())

    def release(
        self, name: str, version: str = "1.0", deps: Iterable[str] = ()
    ) -> tuple[WheelEntry, bytes]:
        entry, body = wheel(name, version, deps)
        self.documents[f"https://pypi.org/pypi/{name}/{version}/json"] = {
            "info": {
                "name": name,
                "version": version,
                "requires_python": ">=3.12",
                "requires_dist": ["json-only-fake==99"],
                "yanked": False,
            },
            "urls": [entry],
            "last_serial": 123,
        }
        index = self.documents.setdefault(
            f"https://pypi.org/simple/{name}/",
            {"meta": {"api-version": "1.1"}, "name": name, "versions": [], "files": []},
        )
        object_array(object_map(index)["versions"]).append(version)
        return entry, body


def reader(wire: Wire, dns: DNS | None = None) -> RegistryReader:
    _, r = api()
    return r.RegistryReader(resolver=dns or DNS(), transport=wire)


def budget(**limits: Unpack[LimitOverrides]) -> ResolutionBudget:
    p, _ = api()
    return p.ResolutionBudget(p.ResolutionLimits(**limits), time.monotonic() + 30)


async def test_real_json_candidate_carries_official_hash_and_snapshot() -> None:
    p, _ = api()
    wire = Wire()
    row, _ = wire.release("demo-lib")
    found = await reader(wire).candidates(
        p.DependencyNeed("Demo_Lib>=1", "a" * 64),
        environment=environment(),
        budget=budget(),
    )
    assert len(found) == 1
    assert found[0].download.sha256 == row["digests"]["sha256"]
    assert found[0].download.filename == "demo_lib-1.0-py3-none-any.whl"
    assert found[0].provenance.url == "https://pypi.org/pypi/demo-lib/1.0/json"
    assert len(found[0].provenance.sha256) == 64
    assert wire.requests[0][0] == "https://pypi.org/simple/demo-lib/"


@pytest.mark.parametrize(
    "requirement",
    [
        "demo @ https://evil.invalid/x.whl",
        "../demo",
        "demo\nOther: x",
        "demo; os_name == 'x'",
    ],
)
def test_need_cannot_supply_urls_paths_headers_or_root_markers(requirement: str) -> None:
    p, _ = api()
    with pytest.raises(ValueError):
        p.DependencyNeed(requirement, "a" * 64)


@pytest.mark.parametrize(
    "addresses",
    [
        [],
        ["127.0.0.1"],
        ["::1"],
        ["169.254.169.254"],
        ["93.184.216.34", "10.0.0.1"],
    ],
)
async def test_dns_refuses_nonpublic_or_mixed_answers_before_http(addresses: list[str]) -> None:
    p, _ = api()
    wire = Wire()
    with pytest.raises(ValueError):
        await reader(wire, DNS(addresses)).candidates(
            p.DependencyNeed("demo", "a" * 64),
            environment=environment(),
            budget=budget(),
        )
    assert wire.requests == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "https://evil.invalid/demo-1.0-py3-none-any.whl"),
        ("size", True),
        ("digests", {"sha256": "bad"}),
        ("filename", "other-1.0-py3-none-any.whl"),
        ("packagetype", "sdist"),
        ("yanked", True),
        ("requires_python", ">=9"),
    ],
)
async def test_ineligible_release_never_yields_a_descriptor(field: str, value: object) -> None:
    p, _ = api()
    wire = Wire()
    wire.release("demo")
    document = object_map(wire.documents["https://pypi.org/pypi/demo/1.0/json"])
    object_map(object_array(document["urls"])[0])[field] = value
    with pytest.raises(ValueError):
        await reader(wire).candidates(
            p.DependencyNeed("demo", "a" * 64),
            environment=environment(),
            budget=budget(),
        )


@pytest.mark.parametrize(
    "response",
    [
        HttpResponse(302, {"Location": "https://evil.invalid"}, b"{}"),
        HttpResponse(
            200,
            {"Content-Type": "application/vnd.pypi.simple.v1+json"},
            b'{"name":"demo","name":"other"}',
        ),
        HttpResponse(200, {"Content-Type": "application/vnd.pypi.simple.v1+json"}, b'{"name":NaN}'),
        HttpResponse(200, {"Content-Type": "text/html"}, b"{}"),
        HttpResponse(200, {"Content-Type": "application/json", "Content-Encoding": "gzip"}, b"{}"),
    ],
)
async def test_untrusted_http_and_json_do_not_escape_fixed_protocol(response: HttpResponse) -> None:
    p, _ = api()
    wire = Wire()
    wire.documents["https://pypi.org/simple/demo/"] = response
    with pytest.raises(ValueError):
        await reader(wire).candidates(
            p.DependencyNeed("demo", "a" * 64),
            environment=environment(),
            budget=budget(),
        )
    assert len(wire.requests) == 1


async def test_more_than_four_candidates_are_soft_not_a_human_or_hard_gate() -> None:
    p, _ = api()
    wire = Wire()
    for i in range(1, 7):
        wire.release("demo", f"{i}.0")
        if i > 1:
            document = object_map(wire.documents[f"https://pypi.org/pypi/demo/{i}.0/json"])
            object_map(object_array(document["urls"])[0])["yanked"] = True
    found = await reader(wire).candidates(
        p.DependencyNeed("demo", "a" * 64),
        environment=environment(),
        budget=budget(),
    )
    assert found[0].download.version == "1.0"
    assert len(wire.requests) == 7


async def test_deadline_and_byte_limits_are_not_renewed() -> None:
    p, _ = api()
    wire = Wire()
    wire.release("demo")
    expired = p.ResolutionBudget(p.ResolutionLimits(), time.monotonic() - 1)
    with pytest.raises(ValueError, match="deadline"):
        await reader(wire).candidates(
            p.DependencyNeed("demo", "a" * 64),
            environment=environment(),
            budget=expired,
        )
    assert wire.requests == []
    tiny = budget(max_response_bytes=32)
    with pytest.raises(ValueError):
        await reader(wire).candidates(
            p.DependencyNeed("demo", "a" * 64),
            environment=environment(),
            budget=tiny,
        )


async def test_candidate_provenance_must_name_its_exact_release() -> None:
    p, _ = api()
    wire = Wire()
    wire.release("demo")
    candidate = (
        await reader(wire).candidates(
            p.DependencyNeed("demo", "a" * 64),
            environment=environment(),
            budget=budget(),
        )
    )[0]
    with pytest.raises(ValueError):
        replace(
            candidate,
            provenance=p.RegistryProvenance(
                "https://pypi.org/pypi/other/2.0/json",
                "1" * 64,
            ),
        )


def test_soft_milestones_follow_project_demand_without_changing_hard_ceiling() -> None:
    allowance = budget()
    before = allowance.limits
    assert allowance.soft_limit("packages", demand=32) >= 32
    assert allowance.soft_limit("candidates", demand=9) >= 9
    assert allowance.limits == before


def test_actual_work_publishes_adaptive_soft_milestones() -> None:
    allowance = budget()
    deadline = allowance.deadline
    for _ in range(17):
        allowance.consume("packages")
    assert dict(allowance.milestones)["packages"] >= 17
    assert dict(allowance.usage)["packages"] == 17
    assert allowance.deadline == deadline


async def test_index_api_version_is_not_coerced_from_a_number() -> None:
    p, _ = api()
    wire = Wire()
    wire.release("demo")
    document = object_map(wire.documents["https://pypi.org/simple/demo/"])
    object_map(document["meta"])["api-version"] = 1.1
    with pytest.raises(ValueError, match="api_version"):
        await reader(wire).candidates(
            p.DependencyNeed("demo", "a" * 64),
            environment=environment(),
            budget=budget(),
        )


async def test_request_budget_is_shared_between_index_and_release() -> None:
    p, _ = api()
    wire = Wire()
    wire.release("demo")
    with pytest.raises(ValueError, match="requests_limit"):
        await reader(wire).candidates(
            p.DependencyNeed("demo", "a" * 64),
            environment=environment(),
            budget=budget(max_requests=1),
        )
    assert len(wire.requests) == 1


async def test_all_unmocked_network_entries_are_hard_blocked(request: pytest.FixtureRequest) -> None:
    request.getfixturevalue("offline_network")
    left, right = socket.socketpair()
    try:
        left.sendall(b"local-event-loop")
        assert right.recv(16) == b"local-event-loop"
    finally:
        left.close()
        right.close()
    with pytest.raises(AssertionError, match="real network disabled"):
        socket.getaddrinfo("pypi.org", 443)
    with pytest.raises(AssertionError, match="real network disabled"):
        socket.create_connection(("93.184.216.34", 443))
    with socket.socket() as connection:
        with pytest.raises(AssertionError, match="real network disabled"):
            connection.connect(("93.184.216.34", 443))
        with pytest.raises(AssertionError, match="real network disabled"):
            connection.connect_ex(("93.184.216.34", 443))
    with (
        socket.socket(type=socket.SOCK_DGRAM) as connection,
        pytest.raises(AssertionError, match="real network disabled"),
    ):
        connection.sendto(b"no packet", ("93.184.216.34", 53))
    with pytest.raises(AssertionError, match="real network disabled"):
        await asyncio.open_connection("93.184.216.34", 443)


async def test_slow_metadata_cannot_outlive_original_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p, _ = api()
    wire = Wire()
    wire.release("demo")
    original = wire.get

    async def slow(url: str, *, resolved_addresses: tuple[str, ...]) -> HttpResponse:
        await asyncio.sleep(1)
        return await original(url, resolved_addresses=resolved_addresses)

    monkeypatch.setattr(wire, "get", slow)
    allowance = p.ResolutionBudget(p.ResolutionLimits(), time.monotonic() + 0.03)
    started = time.monotonic()
    with pytest.raises(ValueError):
        await reader(wire).candidates(
            p.DependencyNeed("demo", "a" * 64),
            environment=environment(),
            budget=allowance,
        )
    assert time.monotonic() - started < 0.8
    assert wire.requests == []


def test_scope_and_limits_are_validated_and_cannot_gain_authority() -> None:
    p, _ = api()
    with pytest.raises(ValueError):
        replace(scope(), source_sha256="fake")
    with pytest.raises(ValueError):
        p.ResolutionLimits(max_wheel_bytes=64 * 1024**2 + 1)
    with pytest.raises(ValueError):
        p.DependencyNeed("demo", "fake")


async def test_production_transport_uses_pinned_tls_and_strict_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, r = api()
    calls: list[bytes] = []
    stream = asyncio.StreamReader()
    stream.feed_data(
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}"
    )
    stream.feed_eof()

    class Writer:
        def write(self, data: bytes) -> None:
            calls.append(data)

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def connect(
        address: str, port: int, *, ssl: SSLContext, server_hostname: str
    ) -> tuple[asyncio.StreamReader, Writer]:
        assert address == "93.184.216.34" and port == 443
        assert server_hostname == "pypi.org" and ssl is not None
        return stream, Writer()

    monkeypatch.setattr(asyncio, "open_connection", connect)
    result = await r.RegistryReader(resolver=DNS()).read("demo", "1.0", budget=budget())
    assert result[0] == {}
    assert b"Host: pypi.org\r\n" in calls[0]
    assert calls[0].startswith(b"GET /pypi/demo/1.0/json HTTP/1.1\r\n")
