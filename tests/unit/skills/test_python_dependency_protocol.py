"""v8 read-only closure wire boundaries, without Root fixtures or real sockets."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.project import ContractProjectInvocation, ProjectInvocation

MODULE = "agent_hub.skills.sandbox.python_dependency_protocol"
SOURCE = "a" * 64
CATALOG = "b" * 64
ENV = {
    "implementation_name": "cpython", "implementation_version": "3.12.4",
    "os_name": "posix", "platform_machine": "x86_64", "platform_release": "6.8",
    "platform_system": "Linux", "platform_version": "#1",
    "platform_python_implementation": "CPython", "python_full_version": "3.12.4",
    "python_version": "3.12", "sys_platform": "linux",
}
ERRORS = (
    "python_dependency_unavailable", "python_dependency_metadata_unavailable",
    "python_dependency_metadata_untrusted", "python_dependency_catalog_changed",
    "python_dependency_draft_changed", "python_dependency_declaration_invalid",
)


def api() -> Any:
    assert importlib.util.find_spec(MODULE) is not None, "v8 dependency protocol missing"
    return importlib.import_module(MODULE)


def invocation(**changes: Any) -> ProjectInvocation:
    fields: dict[str, Any] = {
        "execution_id": "dependency_1", "tenant_id": UUID(int=1), "run_id": UUID(int=2),
        "project_id": "example", "session_id": "one", "sandbox_profile": "restricted",
        "timeout_seconds": 120,
    }
    return ProjectInvocation(**(fields | changes))


def request(*, timeout_seconds: int = 120) -> Any:
    return api().PythonDependencyClosureRequest(invocation=invocation(timeout_seconds=timeout_seconds),
        expected_source_sha256=SOURCE, expected_catalog_sha256=CATALOG)


def observation() -> dict[str, Any]:
    raw = json.dumps(ENV, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return {
        "source_sha256": SOURCE, "manifest_sha256": "c" * 64, "lock_sha256": "d" * 64,
        "catalog_sha256": CATALOG, "metadata_index_sha256": "e" * 64,
        "target_environment_sha256": hashlib.sha256(raw).hexdigest(),
        "target_environment": dict(ENV), "closure": {"status": "complete", "issues": []},
    }


def payload() -> dict[str, Any]:
    return {
        "version": 8, "action": "inspect_python_dependency_closure",
        "scope": {"tenant_id": str(UUID(int=1)), "run_id": str(UUID(int=2)),
            "project_id": "example", "session_id": "one", "execution_id": "dependency_1"},
        "ok": True, "error": None, "observation": observation(),
    }


def encode(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()


def test_v8_dependency_protocol_is_available() -> None:
    api()


def test_exact_restricted_request_round_trip() -> None:
    module = api()
    value = module._parse_request(request().model_dump_json().encode())
    assert value.version == 8 and value.action == "inspect_python_dependency_closure"
    assert type(value.invocation) is ProjectInvocation
    assert value.invocation.sandbox_profile == "restricted"
    assert value.invocation.install_allowed is False
    assert value.expected_source_sha256 == SOURCE and value.expected_catalog_sha256 == CATALOG


@pytest.mark.parametrize("version", [1, 6, 7, 9, True, False, 8.0, "8"])
def test_legacy_and_noninteger_versions_are_rejected(version: object) -> None:
    module = api()
    body = request().model_dump(mode="json") | {"version": version}
    with pytest.raises(ValueError, match="^python_dependency_request_invalid$"):
        module._parse_request(encode(body))


@pytest.mark.parametrize("change", [
    {"install_allowed": True}, {"install_allowed": 0}, {"timeout_seconds": 3601},
    {"timeout_seconds": True}, {"sandbox_profile": "workspace_write"},
    {"sandbox_profile": "read_only"}, {"url": "https://private/secret"},
    {"path": "/private/secret"}, {"original_reference": {}},
])
def test_invocation_authority_and_extra_fields_are_rejected(change: dict[str, object]) -> None:
    module = api()
    body = request().model_dump(mode="json")
    body["invocation"].update(change)
    with pytest.raises(ValueError, match="^python_dependency_request_invalid$"):
        module._parse_request(encode(body))


@pytest.mark.parametrize("change", [
    {"action": "run_project"}, {"url": "https://private/secret"}, {"path": "/secret"},
    {"expected_source_sha256": "../secret"}, {"expected_catalog_sha256": "B" * 64},
])
def test_request_shape_is_closed(change: dict[str, object]) -> None:
    module = api()
    with pytest.raises(ValueError, match="^python_dependency_request_invalid$"):
        module._parse_request(encode(request().model_dump(mode="json") | change))


@pytest.mark.parametrize("raw", [
    b"", b"[]", b"null", b"{", b'{"version":8,"version":8}',
    b'{"invocation":{"timeout_seconds":1,"timeout_seconds":1}}',
    b'{"v":NaN}', b'{"v":Infinity}', b'{"v":-Infinity}', b" " * 16385,
])
def test_request_json_rejects_duplicates_nonfinite_and_oversize(raw: bytes) -> None:
    with pytest.raises(ValueError, match="^python_dependency_request_invalid$"):
        api()._parse_request(raw)


def test_scope_and_all_observation_digests_round_trip() -> None:
    module = api()
    result = module._parse_response(encode(payload()), request())
    assert type(result.scope) is module.PythonProjectProfileScope
    result.require_for(request())
    assert result.observation.model_dump() == observation()


@pytest.mark.parametrize("field", ["tenant_id", "run_id", "project_id", "session_id", "execution_id"])
def test_foreign_scope_is_rejected(field: str) -> None:
    module = api()
    body = payload()
    body["scope"][field] = str(UUID(int=99)) if field.endswith("_id") and field in {
        "tenant_id", "run_id"} else "foreign"
    value = module.PythonDependencyClosureResponse.model_validate_json(encode(body), strict=True)
    with pytest.raises(ValueError, match="^python_dependency_response_invalid$"):
        value.require_for(request())


@pytest.mark.parametrize("field", ["source_sha256", "catalog_sha256"])
def test_stale_binding_is_rejected(field: str) -> None:
    module = api()
    body = payload()
    body["observation"][field] = "f" * 64
    with pytest.raises(ValueError, match="^python_dependency_response_invalid$"):
        module._parse_response(encode(body), request())


@pytest.mark.parametrize("key", tuple(ENV))
def test_all_eleven_marker_keys_are_required(key: str) -> None:
    body = observation()
    del body["target_environment"][key]
    with pytest.raises(ValueError):
        api().PythonDependencyClosureObservation.model_validate(body, strict=True)


@pytest.mark.parametrize("change", [{"extra": "x"}, {"python_version": False}])
def test_marker_environment_rejects_extra_keys_and_nonstrings(change: dict[str, object]) -> None:
    body = observation()
    body["target_environment"].update(change)
    with pytest.raises(ValueError):
        api().PythonDependencyClosureObservation.model_validate(body, strict=True)


def test_target_environment_digest_must_match_canonical_json() -> None:
    body = observation()
    body["target_environment"]["platform_release"] = "another-release"
    with pytest.raises(ValueError):
        api().PythonDependencyClosureObservation.model_validate(body, strict=True)


@pytest.mark.parametrize("field", [
    "source_sha256", "manifest_sha256", "lock_sha256", "catalog_sha256",
    "metadata_index_sha256", "target_environment_sha256",
])
@pytest.mark.parametrize("value", [False, "f" * 63, "F" * 64, "/private/secret"])
def test_all_six_observation_hashes_are_strict(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        api().PythonDependencyClosureObservation.model_validate(
            observation() | {field: value}, strict=True)


@pytest.mark.parametrize("status,code", [
    ("complete", None), ("incomplete", "missing_dependency"),
    ("metadata_unavailable", "metadata_missing"), ("unsupported", "unsupported_dependency"),
])
def test_all_domain_closure_states_remain_observations(status: str, code: str | None) -> None:
    body = observation()
    domain = importlib.import_module("agent_hub.capabilities.python_dependency_closure")
    issues = [] if code is None else [domain.PythonDependencyClosureIssue(
        code=code, source="demo", dependency="helper" if code == "missing_dependency" else "",
    ).to_payload()]
    body["closure"] = {"status": status, "issues": issues}
    result = api().PythonDependencyClosureObservation.model_validate(body, strict=True)
    assert result.closure == body["closure"]


@pytest.mark.parametrize("change", [{"path": "/private/secret"}, {"url": "https://private/secret"}])
def test_observation_has_no_path_or_url_fields(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        api().PythonDependencyClosureObservation.model_validate(
            observation() | change, strict=True)


def test_observation_accepts_actual_metadata_index_and_closure_api() -> None:
    domain = importlib.import_module("agent_hub.capabilities.python_dependency_closure")
    metadata = importlib.import_module("agent_hub.skills.sandbox.python_dependency_metadata")
    wheels = importlib.import_module("agent_hub.capabilities.python_project_wheels")
    recipe = importlib.import_module("agent_hub.capabilities.python_project_recipe")
    binding = wheels.PythonWheelBinding("demo-1.0-py3-none-any.whl", "demo", "1.0", "f" * 64)
    index = metadata.PythonDependencyMetadataIndex(CATALOG, (
        domain.PythonWheelMetadata(binding, None, ()),
    ))
    decoded = metadata.PythonDependencyMetadataIndex.from_bytes(index.to_bytes())
    closure = domain.check_python_dependency_closure(
        (recipe.PythonLockedRequirement("demo", "1.0", ("f" * 64,)),), decoded.records, ENV)
    body = observation() | {"metadata_index_sha256": decoded.sha256, "closure": closure.to_payload()}
    result = api().PythonDependencyClosureObservation.model_validate(body, strict=True)
    assert result.metadata_index_sha256 == hashlib.sha256(index.to_bytes()).hexdigest()
    assert result.closure == {"status": "complete", "issues": []}


@pytest.mark.parametrize("closure", [
    {"status": "installed", "issues": []}, {"status": "complete", "issues": [], "extra": True},
    {"status": "incomplete", "issues": []}, {"status": "complete", "issues": [False]},
    {"status": "unsupported", "issues": [{"code": "https://private/secret"}]},
])
def test_closure_payload_is_revalidated_by_domain_dto(closure: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        api().PythonDependencyClosureObservation.model_validate(
            observation() | {"closure": closure}, strict=True)


@pytest.mark.parametrize("code", ERRORS)
def test_each_finite_error_is_a_scope_bound_exclusive_outcome(code: str) -> None:
    module = api()
    body = payload() | {"ok": False, "error": code, "observation": None}
    value = module._parse_response(encode(body), request())
    assert value.error == code and value.observation is None


@pytest.mark.parametrize("change", [
    {"ok": 1}, {"ok": False}, {"error": "python_dependency_unavailable"},
    {"observation": None}, {"ok": False, "error": "/private/secret", "observation": None},
    {"version": 7}, {"version": 8.0}, {"version": True}, {"path": "/private/secret"},
])
def test_response_outcome_and_envelope_are_strict(change: dict[str, object]) -> None:
    module = api()
    with pytest.raises(ValueError, match="^python_dependency_response_invalid$"):
        module._parse_response(encode(payload() | change), request())


class Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False
        self.waited = False
        self.close_started = asyncio.Event()
        self.close_gate: asyncio.Event | None = None

    def write(self, value: bytes) -> None:
        self.data.extend(value)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.waited = True


class Wire:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reader = asyncio.StreamReader()
        self.writer = Writer()
        self.calls: list[str] = []
        self.connected = asyncio.Event()
        monkeypatch.setattr(asyncio, "open_unix_connection", self.connect, raising=False)
        monkeypatch.setattr(api(), "_systemd_supported", lambda: True)

    async def connect(self, path: str) -> tuple[asyncio.StreamReader, Writer]:
        self.calls.append(path)
        self.connected.set()
        return self.reader, self.writer

    def feed(self, raw: bytes) -> None:
        self.reader.feed_data(len(raw).to_bytes(4, "big") + raw)
        self.reader.feed_eof()


async def inspect(*, timeout_seconds: int = 120) -> Any:
    return await api().PythonDependencyClosureSandbox(socket_path=Path("/closure.sock")).inspect(
        invocation(timeout_seconds=timeout_seconds),
        expected_source_sha256=SOURCE, expected_catalog_sha256=CATALOG)


async def test_client_exchanges_actual_rootbroker_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = Wire(monkeypatch)
    wire.feed(encode(payload()))
    result = await inspect()
    assert result.model_dump() == observation()
    assert wire.calls == ["/closure.sock"]
    written = wire.writer.data
    assert int.from_bytes(written[:4], "big") == len(written) - 4 <= 16384
    assert json.loads(written[4:]) == request().model_dump(mode="json")
    assert wire.writer.closed and wire.writer.waited


@pytest.mark.parametrize("seconds", [16, 3600])
async def test_large_remaining_window_uses_invocation_timeout_plus_one(
    monkeypatch: pytest.MonkeyPatch, seconds: int,
) -> None:
    wire = Wire(monkeypatch)
    wire.feed(encode(payload()))
    real_timeout = asyncio.timeout
    delays: list[float | None] = []
    def recorded_timeout(delay: float | None) -> asyncio.Timeout:
        delays.append(delay)
        return real_timeout(delay)
    monkeypatch.setattr(asyncio, "timeout", recorded_timeout)
    result = await inspect(timeout_seconds=seconds)
    assert result.closure["status"] == "complete"
    assert delays == [seconds + 1, seconds + 1, 2]
    assert json.loads(wire.writer.data[4:])["invocation"]["timeout_seconds"] == seconds
    assert wire.writer.closed and wire.writer.waited


@pytest.mark.parametrize("seconds", [0, 3601])
def test_existing_dto_rejects_timeouts_outside_its_range(seconds: int) -> None:
    with pytest.raises(ValueError):
        invocation(timeout_seconds=seconds)
    module = api()
    body = request().model_dump(mode="json")
    body["invocation"]["timeout_seconds"] = seconds
    with pytest.raises(ValueError, match="^python_dependency_request_invalid$"):
        module._parse_request(encode(body))


async def test_reused_client_preserves_smaller_remaining_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = api()
    client = module.PythonDependencyClosureSandbox()
    real_timeout = asyncio.timeout
    delays: list[float | None] = []
    def recorded_timeout(delay: float | None) -> asyncio.Timeout:
        delays.append(delay)
        return real_timeout(delay)
    monkeypatch.setattr(asyncio, "timeout", recorded_timeout)
    for seconds in (240, 3):
        wire = Wire(monkeypatch)
        wire.feed(encode(payload()))
        await client.inspect(invocation(timeout_seconds=seconds),
            expected_source_sha256=SOURCE, expected_catalog_sha256=CATALOG)
        assert json.loads(wire.writer.data[4:])["invocation"]["timeout_seconds"] == seconds
        assert wire.writer.closed and wire.writer.waited
    assert delays == [241, 241, 2, 4, 4, 2]


async def test_large_window_does_not_extend_outer_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = Wire(monkeypatch)
    budget = asyncio.timeout(0.01)
    original_deadline = budget.when()
    with pytest.raises(TimeoutError):
        async with budget:
            await inspect(timeout_seconds=3600)
    assert budget.expired() and budget.when() == original_deadline
    assert len(wire.calls) == 1 and wire.writer.closed and wire.writer.waited


def test_request_byte_limit_is_inclusive() -> None:
    module = api()
    raw = request().model_dump_json().encode()
    bounded = raw + b" " * (16384 - len(raw))
    assert module._parse_request(bounded).version == 8
    with pytest.raises(ValueError, match="^python_dependency_request_invalid$"):
        module._parse_request(bounded + b" ")


async def test_response_byte_limit_is_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = Wire(monkeypatch)
    raw = encode(payload())
    wire.feed(raw + b" " * (32768 - len(raw)))
    assert (await inspect()).closure["status"] == "complete"
    assert wire.writer.closed and wire.writer.waited


@pytest.mark.parametrize("code", ERRORS)
async def test_client_raises_precise_error_without_returning_observation(
    monkeypatch: pytest.MonkeyPatch, code: str,
) -> None:
    wire = Wire(monkeypatch)
    wire.feed(encode(payload() | {"ok": False, "error": code, "observation": None}))
    with pytest.raises(ValueError, match=f"^{code}$"):
        await inspect()
    assert wire.writer.closed and wire.writer.waited


@pytest.mark.parametrize("kind", [
    "foreign_scope", "stale_source", "stale_catalog", "legacy", "duplicate", "nested_duplicate",
    "nan", "infinity", "exponent_overflow", "oversize", "zero", "truncated", "malformed",
])
async def test_client_rejects_bad_response_bytes_and_closes(
    monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    wire = Wire(monkeypatch)
    body = payload()
    if kind == "foreign_scope":
        body["scope"]["execution_id"] = "foreign"
    elif kind.startswith("stale_"):
        body["observation"][kind.removeprefix("stale_") + "_sha256"] = "f" * 64
    elif kind == "legacy":
        body["version"] = 7
    raw = encode(body)
    if kind == "duplicate":
        raw = raw.replace(b'"version":8', b'"version":8,"version":8', 1)
    elif kind == "nested_duplicate":
        raw = raw.replace(b'"status":"complete"', b'"status":"complete","status":"complete"')
    elif kind in {"nan", "infinity", "exponent_overflow"}:
        token = {"nan": b"NaN", "infinity": b"Infinity", "exponent_overflow": b"1e999"}[kind]
        raw = raw.replace(b'"issues":[]', b'"issues":[' + token + b']')
    elif kind == "malformed":
        raw = b"{"
    if kind in {"oversize", "zero", "truncated"}:
        size = {"oversize": 32769, "zero": 0, "truncated": len(raw) + 1}[kind]
        wire.reader.feed_data(size.to_bytes(4, "big") + (raw if kind == "truncated" else b""))
        wire.reader.feed_eof()
    else:
        wire.feed(raw)
    with pytest.raises(ValueError, match="^python_dependency_response_invalid$"):
        await inspect()
    assert wire.writer.closed and wire.writer.waited


@pytest.mark.parametrize("kind", ["copy", "construct", "subclass", "contract", "request_subclass"])
async def test_invalid_exact_dtos_fail_before_connection(
    monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    module = api()
    wire = Wire(monkeypatch)
    value = request()
    if kind == "copy":
        value = value.model_copy(update={"invocation": invocation().model_copy(
            update={"install_allowed": True})})
    elif kind == "construct":
        value = module.PythonDependencyClosureRequest.model_construct(
            invocation=invocation(), expected_source_sha256="secret", expected_catalog_sha256=CATALOG)
    elif kind in {"subclass", "contract"}:
        class Derived(ProjectInvocation):
            pass
        cls = ContractProjectInvocation if kind == "contract" else Derived
        value = value.model_copy(update={"invocation": cls.model_construct(
            **invocation().model_dump())})
    else:
        DerivedRequest: Any = type("DerivedRequest", (module.PythonDependencyClosureRequest,), {})
        value = DerivedRequest.model_construct(**value.model_dump())
    with pytest.raises(ValueError, match="^python_dependency_request_invalid$"):
        await module.request_python_dependency_closure_broker(value)
    assert wire.calls == []


@pytest.mark.parametrize("kind", ["contract", "copy", "construct"])
def test_direct_request_constructor_revalidates_invocation(kind: str) -> None:
    module = api()
    value = invocation()
    if kind == "contract":
        value = ContractProjectInvocation.model_construct(**value.model_dump())
    elif kind == "copy":
        value = value.model_copy(update={"timeout_seconds": True})
    else:
        value = ProjectInvocation.model_construct(**(value.model_dump() | {"install_allowed": 0}))
    with pytest.raises(ValueError):
        module.PythonDependencyClosureRequest(invocation=value,
            expected_source_sha256=SOURCE, expected_catalog_sha256=CATALOG)


@pytest.mark.parametrize("profile", ["read_only", "workspace_write"])
@pytest.mark.parametrize("kind", ["copy", "construct"])
async def test_forged_existing_dto_profile_is_rejected_before_connection(
    monkeypatch: pytest.MonkeyPatch, profile: str, kind: str,
) -> None:
    module = api()
    wire = Wire(monkeypatch)
    value = invocation()
    if kind == "copy":
        value = value.model_copy(update={"sandbox_profile": profile})
    else:
        value = ProjectInvocation.model_construct(**(value.model_dump() | {"sandbox_profile": profile}))
    with pytest.raises(ValueError, match="^python_dependency_request_invalid$"):
        await module.PythonDependencyClosureSandbox().inspect(value,
            expected_source_sha256=SOURCE, expected_catalog_sha256=CATALOG)
    assert not wire.calls


async def test_non_native_client_has_no_root_or_process_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = api()
    wire = Wire(monkeypatch)
    project = importlib.import_module("agent_hub.skills.sandbox.project")
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("read-only inspection crossed a filesystem/process/termination boundary")
    monkeypatch.setattr(module, "_systemd_supported", lambda: False)
    monkeypatch.setattr(project, "request_project_broker", forbidden)
    monkeypatch.setattr(project, "snapshot_project_source", forbidden)
    monkeypatch.setattr(project, "run_subprocess_with_limits", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    with pytest.raises(OSError, match="^python_dependency_unavailable$"):
        await inspect()
    assert not wire.calls


async def test_substituted_transport_is_reparsed(monkeypatch: pytest.MonkeyPatch) -> None:
    module = api()
    wire = Wire(monkeypatch)
    response = module.PythonDependencyClosureResponse.model_validate_json(encode(payload()), strict=True)
    response.observation.closure["status"] = "installed"
    async def exchange(*args: Any, **kwargs: Any) -> Any:
        return response
    monkeypatch.setattr(module, "request_python_dependency_closure_broker", exchange)
    with pytest.raises(ValueError, match="^python_dependency_response_invalid$"):
        await inspect()
    assert not wire.calls


async def test_timeout_closes_once_without_terminate(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = Wire(monkeypatch)
    project = importlib.import_module("agent_hub.skills.sandbox.project")
    async def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("read-only timeout attempted a terminate action")
    monkeypatch.setattr(project, "request_project_broker", forbidden)
    with pytest.raises(TimeoutError):
        await inspect(timeout_seconds=1)
    assert len(wire.calls) == 1 and wire.writer.closed and wire.writer.waited


async def test_repeated_cancel_drains_same_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = Wire(monkeypatch)
    gate = wire.writer.close_gate = asyncio.Event()
    task = asyncio.create_task(inspect())
    try:
        await asyncio.wait_for(wire.connected.wait(), 1)
        task.cancel()
        await asyncio.wait_for(wire.writer.close_started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and wire.writer.closed
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert wire.writer.waited and len(wire.calls) == 1
    finally:
        gate.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_close_handshake_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    module = api()
    wire = Wire(monkeypatch)
    wire.writer.close_gate = asyncio.Event()
    monkeypatch.setattr(module, "_CLOSE_SECONDS", 0.01)
    wire.feed(encode(payload()))
    result = await asyncio.wait_for(inspect(), 1)
    assert result.closure["status"] == "complete"
    assert wire.writer.closed and not wire.writer.waited


async def test_direct_transport_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    module = api()
    wire = Wire(monkeypatch)
    with pytest.raises(TimeoutError):
        await module.request_python_dependency_closure_broker(request(timeout_seconds=1))
    assert wire.writer.closed and wire.writer.waited


async def test_connection_failure_is_finite(monkeypatch: pytest.MonkeyPatch) -> None:
    module = api()
    async def fail(path: str) -> Any:
        raise OSError("/private/secret token=secret")
    monkeypatch.setattr(module, "_systemd_supported", lambda: True)
    monkeypatch.setattr(asyncio, "open_unix_connection", fail, raising=False)
    with pytest.raises(OSError, match="^python_dependency_unavailable$"):
        await module.request_python_dependency_closure_broker(request())


async def test_native_socket_timeout_does_not_echo_private_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = api()
    async def fail(path: str) -> Any:
        raise TimeoutError("/private/secret token=secret")
    monkeypatch.setattr(module, "_systemd_supported", lambda: True)
    monkeypatch.setattr(asyncio, "open_unix_connection", fail, raising=False)
    with pytest.raises(TimeoutError) as error:
        await module.request_python_dependency_closure_broker(request())
    assert "secret" not in str(error.value)


async def test_direct_transport_rejects_nonnative_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    module = api()
    wire = Wire(monkeypatch)
    monkeypatch.setattr(module, "_systemd_supported", lambda: False)
    with pytest.raises(OSError, match="^python_dependency_unavailable$"):
        await module.request_python_dependency_closure_broker(request())
    assert not wire.calls
