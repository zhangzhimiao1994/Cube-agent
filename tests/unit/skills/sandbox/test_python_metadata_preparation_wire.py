"""v10 SDK and real dispatcher over framed memory streams, without native IO proof."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import socket
import struct
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, cast

import pytest

from agent_hub.skills.sandbox import broker
from agent_hub.skills.sandbox import python_project_profile as profile_api
from agent_hub.skills.sandbox.project import ProjectExecutionBroker
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    MetadataPlanningError,
    PythonMetadataPlan,
    PythonMetadataPlanningRequest,
    PythonMetadataPlanningResponse,
)
from agent_hub.skills.sandbox.python_metadata_preparation_protocol import (
    PythonMetadataPreparationRequest,
    PythonMetadataPreparationResponse,
)
from tests.unit.skills.test_python_metadata_preparation_protocol import (
    ERRORS,
    replace,
)
from tests.unit.skills.test_python_metadata_preparation_protocol import (
    request as protocol_request,
)
from tests.unit.skills.test_python_metadata_preparation_protocol import (
    response as protocol_response,
)

CLIENT = "agent_hub.skills.sandbox.python_metadata_preparation_client"
ROOT = "agent_hub.skills.sandbox.python_metadata_preparation_broker"
PRIVATE = "/private/DO-NOT-ECHO"


def client() -> ModuleType:
    assert importlib.util.find_spec(CLIENT) is not None, "v10 metadata preparation client missing"
    return importlib.import_module(CLIENT)


def request(inspect: bool = False) -> PythonMetadataPreparationRequest:
    return cast(PythonMetadataPreparationRequest, protocol_request(inspect))


def response(inspect: bool = False, **changes: Any) -> PythonMetadataPreparationResponse:
    return cast(PythonMetadataPreparationResponse, protocol_response(inspect, **changes))


class Peer:
    def __init__(self, *, pid: int = 123, uid: int = 0, gid: int = 0) -> None:
        self.credentials = struct.pack("3i", pid, uid, gid)

    def getsockopt(self, level: int, option: int, length: int) -> bytes:
        assert level == socket.SOL_SOCKET and length == 12
        return self.credentials


class Writer:
    def __init__(self, peer: Peer | None = None) -> None:
        self.peer = peer
        self.writes = bytearray()
        self.closed = False
        self.sent = asyncio.Event()

    def get_extra_info(self, name: str) -> Peer | None:
        assert name == "socket"
        return self.peer

    def write(self, raw: bytes) -> None:
        self.writes.extend(raw)

    async def drain(self) -> None:
        self.sent.set()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        assert self.closed


async def connection(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, *, peer: Peer | None = None,
    framed: bool = False,
) -> tuple[ModuleType, Writer]:
    api = client()
    reader, writer = asyncio.StreamReader(), Writer(peer if peer is not None else Peer())
    reader.feed_data(payload if framed else struct.pack("!I", len(payload)) + payload)
    reader.feed_eof()

    async def connect(path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        assert path == Path("metadata.sock").as_posix()
        return reader, cast(asyncio.StreamWriter, writer)

    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    return api, writer


async def serve(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | bytes, *, uid: int = 10001,
) -> dict[str, Any]:
    monkeypatch.setattr(broker, "_peer_uid", lambda writer: uid)
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(struct.pack("!I", len(raw)) + raw)
    reader.feed_eof()
    await broker._serve_client(reader, cast(asyncio.StreamWriter, writer),
                               broker.SkillBroker(broker.BrokerPolicy()))
    assert writer.closed
    assert struct.unpack("!I", writer.writes[:4])[0] == len(writer.writes[4:])
    return cast(dict[str, Any], json.loads(writer.writes[4:]))


def test_client_exists() -> None:
    assert callable(client().request_python_metadata_preparation_broker)
    assert callable(client().parse_metadata_preparation_request)


@pytest.mark.parametrize("inspect", [False, True])
async def test_v10_dispatches_to_the_new_root_handler(
    monkeypatch: pytest.MonkeyPatch, inspect: bool,
) -> None:
    root = importlib.import_module(ROOT)
    seen: list[PythonMetadataPreparationRequest] = []

    async def handle(
        execution: ProjectExecutionBroker, value: PythonMetadataPreparationRequest, *, peer_uid: int,
    ) -> PythonMetadataPreparationResponse:
        assert execution.policy.allowed_uid == peer_uid == 10001
        assert type(value) is PythonMetadataPreparationRequest
        assert value.invocation == value.planning.invocation
        seen.append(value)
        return response(inspect)

    monkeypatch.setattr(root, "handle_metadata_preparation", handle)
    expected = request(inspect)
    payload = await serve(monkeypatch, expected.model_dump(mode="json"))
    checked = PythonMetadataPreparationResponse.model_validate_json(json.dumps(payload), strict=True)
    checked.require_for(expected)
    assert checked.ok and seen == [expected]


@pytest.mark.parametrize("uid", [0, 17])
async def test_foreign_peer_is_rejected_before_root_dispatch(
    monkeypatch: pytest.MonkeyPatch, uid: int,
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("foreign peer reached metadata Root handler")

    monkeypatch.setattr(importlib.import_module(ROOT), "handle_metadata_preparation", forbidden)
    payload = await serve(monkeypatch, request().model_dump(mode="json"), uid=uid)
    assert payload == {"ok": False, "result": None, "error": "metadata_prepare_request_invalid"}


@pytest.mark.parametrize("changes", [
    {"version": "10"}, {"version": 10.0}, {"version": True}, {"version": 9},
    {"version": 1}, {"action": "plan_python_dependency_metadata"},
    {"action": "supply_python_project_inputs"}, {"action": "run"}, {"action": ["prepare"]},
    {"url": PRIVATE}, {"original_reference": {}}, {"recipe": {}}, {"install_allowed": True},
])
async def test_invalid_v10_never_downgrades_or_echoes_private_values(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any],
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid v10 request reached a handler")

    monkeypatch.setattr(importlib.import_module(ROOT), "handle_metadata_preparation", forbidden)
    monkeypatch.setattr(broker.SkillBroker, "handle", forbidden)
    payload = await serve(monkeypatch, request().model_dump(mode="json") | changes)
    assert payload == {"ok": False, "result": None, "error": "metadata_prepare_request_invalid"}


@pytest.mark.parametrize("defect", ["duplicate", "nested_duplicate", "nonfinite", "oversize"])
async def test_broker_uses_strict_bounded_v10_json(
    monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid v10 JSON reached Root handler")

    monkeypatch.setattr(importlib.import_module(ROOT), "handle_metadata_preparation", forbidden)
    raw = request().model_dump_json().encode()
    if defect == "duplicate":
        raw = raw.replace(b'"version":10', b'"version":10,"version":10', 1)
    elif defect == "nested_duplicate":
        raw = raw.replace(b'"install_allowed":false', b'"install_allowed":false,"install_allowed":false')
    elif defect == "nonfinite":
        raw = raw.replace(b'"timeout_seconds":120', b'"timeout_seconds":NaN')
    else:
        raw = raw[:-1] + b',"host_path":"' + b"x" * (64 * 1024) + b'"}'
    payload = await serve(monkeypatch, raw)
    assert payload == {"ok": False, "result": None, "error": "metadata_prepare_request_invalid"}


@pytest.mark.parametrize("error", [OSError(PRIVATE), ValueError(PRIVATE), TypeError(PRIVATE),
                                  RuntimeError(PRIVATE)])
async def test_root_exceptions_are_isolated_to_finite_v10_error(
    monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    async def failed(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(importlib.import_module(ROOT), "handle_metadata_preparation", failed)
    payload = await serve(monkeypatch, request().model_dump(mode="json"))
    assert payload == {"ok": False, "result": None, "error": "metadata_prepare_request_invalid"}


@pytest.mark.parametrize("path,changes", [
    ("", {"url": PRIVATE}), ("scope", {"extra": True}),
    ("receipt", {"recipe": {}}), ("receipt.plan", {"catalog_count": True}),
    ("receipt.plan.scope", {"install_allowed": True}),
    ("receipt.plan.draft", {"extra": True}),
])
async def test_dispatch_revalidates_response_tree_before_serializing(
    monkeypatch: pytest.MonkeyPatch, path: str, changes: dict[str, Any],
) -> None:
    async def forged(*args: Any, **kwargs: Any) -> Any:
        return replace(response(), path, changes)

    monkeypatch.setattr(importlib.import_module(ROOT), "handle_metadata_preparation", forged)
    payload = await serve(monkeypatch, request().model_dump(mode="json"))
    assert payload == {"ok": False, "result": None, "error": "metadata_prepare_request_invalid"}


@pytest.mark.parametrize("inspect", [False, True])
async def test_sdk_authenticates_root_and_roundtrips_bound_response(
    monkeypatch: pytest.MonkeyPatch, inspect: bool,
) -> None:
    api, writer = await connection(monkeypatch, response(inspect).model_dump_json().encode())
    result = await api.request_python_metadata_preparation_broker(request(inspect),
                                                                 socket_path=Path("metadata.sock"))
    assert type(result) is PythonMetadataPreparationResponse and result.ok
    result.require_for(request(inspect))
    assert writer.closed
    sent = PythonMetadataPreparationRequest.model_validate_json(bytes(writer.writes[4:]), strict=True)
    assert sent == request(inspect)
    assert struct.unpack("!I", writer.writes[:4])[0] == len(writer.writes[4:])
    assert set(json.loads(writer.writes[4:])) == {
        "version", "action", "planning", "expected_plan_sha256", "preparation_execution_id",
    }


@pytest.mark.parametrize("pid,uid,gid", [(0, 0, 0), (-1, 0, 0), (123, 10001, 10001),
                                      (123, 0, 17), (123, 17, 0)])
async def test_root_credentials_are_checked_before_sending_data(
    monkeypatch: pytest.MonkeyPatch, pid: int, uid: int, gid: int,
) -> None:
    api, writer = await connection(monkeypatch, response().model_dump_json().encode(),
                                   peer=Peer(pid=pid, uid=uid, gid=gid))
    with pytest.raises(OSError, match="^metadata_prepare_unavailable$"):
        await api.request_python_metadata_preparation_broker(request(), socket_path=Path("metadata.sock"))
    assert not writer.writes and writer.closed


@pytest.mark.parametrize("defect", ["socket", "option", "credentials"])
async def test_missing_or_malformed_root_peer_credentials_fail_closed(
    monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    api, writer = await connection(monkeypatch, b"{}")
    if defect == "socket":
        writer.peer = None
    elif defect == "option":
        monkeypatch.setattr(socket, "SO_PEERCRED", True)
    else:
        assert writer.peer is not None
        writer.peer.credentials = b"short"
    with pytest.raises(OSError, match="^metadata_prepare_unavailable$"):
        await api.request_python_metadata_preparation_broker(request(), socket_path=Path("metadata.sock"))
    assert not writer.writes and writer.closed


@pytest.mark.parametrize("forge", ["copy", "construct"])
@pytest.mark.parametrize("path,changes", [
    ("", {"version": True}), ("", {"url": PRIVATE}),
    ("planning", {"version": True}), ("planning", {"recipe": {}}),
    ("planning.invocation", {"install_allowed": True}),
    ("planning.invocation", {"timeout_seconds": True}),
    ("planning.invocation", {"url": PRIVATE}),
    ("planning.scope", {"extra": True}), ("planning.draft", {"extra": True}),
])
async def test_forged_model_tree_is_revalidated_before_serialization_or_connection(
    monkeypatch: pytest.MonkeyPatch, forge: str, path: str, changes: dict[str, Any],
) -> None:
    api = client()

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid request opened a socket")

    def serialization_forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("unvalidated model tree reached serialization")

    candidate = replace(request(), path, changes, forge)
    monkeypatch.setattr(PythonMetadataPreparationRequest, "model_dump_json", serialization_forbidden)
    monkeypatch.setattr(asyncio, "open_unix_connection", forbidden, raising=False)
    with pytest.raises(ValueError, match="^metadata_prepare_request_invalid$"):
        await api.request_python_metadata_preparation_broker(candidate)


@pytest.mark.parametrize("defect", [
    "foreign_scope", "wrong_hash", "wrong_draft", "wrong_action", "bool_version", "duplicate",
    "nested_duplicate", "extra", "nonfinite", "overflow", "truncated", "generic_error",
])
async def test_sdk_rejects_unbound_or_malformed_response(
    monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    value = response().model_dump(mode="json")
    if defect == "foreign_scope":
        value["scope"]["run_id"] = "00000000-0000-0000-0000-000000000099"
    elif defect == "wrong_hash":
        value["receipt"]["plan"]["fetch_timeout_seconds"] = 31
    elif defect == "wrong_draft":
        value["receipt"]["plan"]["draft"]["lock_sha256"] = "f" * 64
    elif defect == "wrong_action":
        value = response(True).model_dump(mode="json")
    elif defect == "bool_version":
        value["version"] = True
    elif defect == "extra":
        value["receipt"]["url"] = PRIVATE
    elif defect == "generic_error":
        value = {"ok": False, "result": None, "error": "metadata_prepare_request_invalid"}
    raw = json.dumps(value, separators=(",", ":")).encode()
    if defect == "duplicate":
        raw = raw.replace(b'"version":10', b'"version":10,"version":10', 1)
    elif defect == "nested_duplicate":
        raw = raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1', 1)
    elif defect == "nonfinite":
        raw = raw.replace(b'"ok":true', b'"ok":NaN')
    elif defect == "overflow":
        raw = raw.replace(b'"ok":true', b'"ok":1e9999')
    elif defect == "truncated":
        raw = raw[:12]
    api, writer = await connection(monkeypatch, raw)
    with pytest.raises(ValueError, match="^metadata_prepare_response_invalid$"):
        await api.request_python_metadata_preparation_broker(request(), socket_path=Path("metadata.sock"))
    assert writer.closed


@pytest.mark.parametrize("framed", [b"", b"\x00\x00", struct.pack("!I", 0),
                                  struct.pack("!I", 2**31), struct.pack("!I", 100) + b"{}"])
async def test_sdk_rejects_invalid_frame_lengths_and_truncation(
    monkeypatch: pytest.MonkeyPatch, framed: bytes,
) -> None:
    api, writer = await connection(monkeypatch, framed, framed=True)
    with pytest.raises(ValueError, match="^metadata_prepare_response_invalid$"):
        await api.request_python_metadata_preparation_broker(request(), socket_path=Path("metadata.sock"))
    assert writer.closed


@pytest.mark.parametrize("inspect", [False, True])
@pytest.mark.parametrize("code", ERRORS)
async def test_typed_root_negative_is_preserved(
    monkeypatch: pytest.MonkeyPatch, inspect: bool, code: str,
) -> None:
    negative = response(inspect, ok=False, receipt=None, status=None, error=code)
    api, writer = await connection(monkeypatch, negative.model_dump_json().encode())
    result = await api.request_python_metadata_preparation_broker(request(inspect),
                                                                 socket_path=Path("metadata.sock"))
    assert result == negative and writer.closed


@pytest.mark.parametrize("error", [OSError(PRIVATE), NotImplementedError(PRIVATE), TimeoutError(PRIVATE)])
async def test_connection_errors_are_finite(
    monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    api = client()

    async def fail(path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise error

    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(asyncio, "open_unix_connection", fail, raising=False)
    expected = "metadata_prepare_timeout" if isinstance(error, TimeoutError) else "metadata_prepare_unavailable"
    with pytest.raises(type(error) if isinstance(error, TimeoutError) else OSError,
                       match="^" + expected + "$"):
        await api.request_python_metadata_preparation_broker(request())


async def test_unsupported_transport_does_not_open_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    api = client()

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("unsupported transport opened a socket")

    monkeypatch.setattr(api, "_systemd_supported", lambda: False)
    monkeypatch.setattr(asyncio, "open_unix_connection", forbidden, raising=False)
    with pytest.raises(OSError, match="^metadata_prepare_unavailable$"):
        await api.request_python_metadata_preparation_broker(request())


async def test_sdk_cancellation_closes_socket_without_legacy_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = client()
    reader, writer = asyncio.StreamReader(), Writer(Peer())

    async def connect(path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return reader, cast(asyncio.StreamWriter, writer)

    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    task = asyncio.create_task(api.request_python_metadata_preparation_broker(request()))
    await asyncio.wait_for(writer.sent.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed
    assert b"terminate_project" not in writer.writes


async def test_dispatch_cancellation_does_not_fall_into_legacy_project_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cancelled(*args: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(importlib.import_module(ROOT), "handle_metadata_preparation", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await serve(monkeypatch, request().model_dump(mode="json"))


def discovery(invocation: Any, *, root_digest: str | None = "c" * 64) -> Any:
    return profile_api.PythonProjectProfileResponse(
        scope=profile_api.PythonProjectProfileScope.for_invocation(invocation), ok=True,
        profile=profile_api.PythonProjectProfile(
            interpreter=profile_api.PythonProjectProfileInterpreter(
                implementation="cpython", version="3.12.1", abi="cp312", platform="linux_x86_64",
                binary_sha256="a" * 64, base_runtime_sha256="b" * 64),
            catalog=(profile_api.PythonProjectProfileWheel(name="example-pkg",
                filename="example_pkg-1.0-py3-none-any.whl", version="1.0",
                sha256="e" * 64, size_bytes=200),),
            catalog_sha256="d" * 64, root_config_sha256=root_digest,
        ),
    )


def approved_plan() -> PythonMetadataPlan:
    value = response().receipt
    assert value is not None
    return value.plan


async def test_sandbox_plans_from_discovered_root_and_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    api = client()
    value = request().planning
    seen: list[str] = []

    async def inspect(self: Any, invocation: Any) -> Any:
        assert self.socket_path == Path("metadata.sock")
        assert invocation.execution_id == value.invocation.execution_id
        seen.append("profile")
        return discovery(invocation)

    async def planning(item: PythonMetadataPlanningRequest, *, socket_path: Path | None) -> Any:
        assert seen == ["profile"] and socket_path == Path("metadata.sock")
        assert item.scope == value.scope and item.draft == value.draft
        assert item.expected_root_config_sha256 == "c" * 64
        assert item.expected_catalog_sha256 == "d" * 64
        seen.append("plan")
        return PythonMetadataPlanningResponse(
            scope=profile_api.PythonProjectProfileScope.for_invocation(item.invocation),
            ok=True, plan=approved_plan(),
        )

    monkeypatch.setattr(profile_api.PythonProjectProfileSandbox, "inspect", inspect)
    monkeypatch.setattr(api, "request_python_metadata_plan_broker", planning, raising=False)
    result = await api.PythonMetadataPreparationSandbox(socket_path=Path("metadata.sock")).plan(
        value.invocation, value.scope, value.draft,
    )
    assert type(result) is PythonMetadataPlan and result == approved_plan()
    assert seen == ["profile", "plan"]


@pytest.mark.parametrize("defect", ["old_service", "negative", "foreign", "extra",
                                   "scope_extra", "interpreter_extra", "wheel_extra"])
async def test_sandbox_profile_must_be_strict_bound_and_have_root_digest(
    monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    api = client()
    value = request().planning

    async def inspect(self: Any, invocation: Any) -> Any:
        result = discovery(invocation, root_digest=None if defect == "old_service" else "c" * 64)
        if defect == "negative":
            return result.model_copy(update={"ok": False, "profile": None,
                "error": "python_project_profile_unavailable"})
        if defect == "foreign":
            return replace(result, "scope", {"project_id": "foreign"})
        if defect == "extra":
            return replace(result, "", {"url": PRIVATE})
        if defect == "scope_extra":
            return replace(result, "scope", {"extra": True})
        if defect == "interpreter_extra":
            return replace(result, "profile.interpreter", {"extra": True})
        if defect == "wheel_extra":
            wheel = result.profile.catalog[0].model_copy(update={"extra": True})
            return replace(result, "profile", {"catalog": (wheel,)})
        return result

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("untrusted profile reached v9 planning")

    monkeypatch.setattr(profile_api.PythonProjectProfileSandbox, "inspect", inspect)
    monkeypatch.setattr(api, "request_python_metadata_plan_broker", forbidden, raising=False)
    with pytest.raises(api.PythonMetadataPreparationFailure,
                       match=(r"^python_metadata\.profile\.python_project_profile_unavailable$"
                              if defect == "negative" else
                              r"^python_metadata\.profile\.metadata_prepare_(unavailable|response_invalid)$")):
        await api.PythonMetadataPreparationSandbox().plan(value.invocation, value.scope, value.draft)


@pytest.mark.parametrize("path,changes", [
    ("", {"extra": True}), ("scope", {"extra": True}),
    ("plan", {"extra": True}), ("plan.scope", {"extra": True}),
    ("plan.draft", {"extra": True}), ("plan", {"root_config_sha256": "f" * 64}),
])
async def test_sandbox_revalidates_v9_planner_response_tree(
    monkeypatch: pytest.MonkeyPatch, path: str, changes: dict[str, Any],
) -> None:
    api = client()
    value = request().planning

    async def inspect(self: Any, invocation: Any) -> Any:
        return discovery(invocation)

    async def planning(item: PythonMetadataPlanningRequest, **kwargs: Any) -> Any:
        result = PythonMetadataPlanningResponse(
            scope=profile_api.PythonProjectProfileScope.for_invocation(item.invocation),
            ok=True, plan=approved_plan())
        return replace(result, path, changes)

    monkeypatch.setattr(profile_api.PythonProjectProfileSandbox, "inspect", inspect)
    monkeypatch.setattr(api, "request_python_metadata_plan_broker", planning, raising=False)
    with pytest.raises(api.PythonMetadataPreparationFailure,
                       match=r"^python_metadata\.plan\.metadata_prepare_response_invalid$"):
        await api.PythonMetadataPreparationSandbox().plan(value.invocation, value.scope, value.draft)


async def test_profile_and_planner_share_one_absolute_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    api = client()
    value = request().planning
    invocation = value.invocation.model_copy(update={"timeout_seconds": 1})
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def inspect(self: Any, invocation: Any) -> Any:
        await asyncio.sleep(0.6)
        return discovery(invocation)

    async def planning(item: PythonMetadataPlanningRequest, **kwargs: Any) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(profile_api.PythonProjectProfileSandbox, "inspect", inspect)
    monkeypatch.setattr(api, "request_python_metadata_plan_broker", planning, raising=False)
    with pytest.raises(api.PythonMetadataPreparationFailure,
                       match=r"^python_metadata\.plan\.metadata_prepare_timeout$"):
        await asyncio.wait_for(api.PythonMetadataPreparationSandbox().plan(
            invocation, value.scope, value.draft), timeout=2)
    assert entered.is_set() and cancelled.is_set()


async def test_planner_receives_remaining_invocation_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    api = client()
    value = request().planning
    invocation = value.invocation.model_copy(update={"timeout_seconds": 3})

    async def inspect(self: Any, invocation: Any) -> Any:
        await asyncio.sleep(1.05)
        return discovery(invocation)

    async def planning(item: PythonMetadataPlanningRequest, **kwargs: Any) -> Any:
        assert 1 <= item.invocation.timeout_seconds <= 2
        return PythonMetadataPlanningResponse(
            scope=profile_api.PythonProjectProfileScope.for_invocation(item.invocation),
            ok=True, plan=approved_plan())

    monkeypatch.setattr(profile_api.PythonProjectProfileSandbox, "inspect", inspect)
    monkeypatch.setattr(api, "request_python_metadata_plan_broker", planning, raising=False)
    result = await api.PythonMetadataPreparationSandbox().plan(invocation, value.scope, value.draft)
    assert result == approved_plan()


@pytest.mark.parametrize("inspect", [False, True])
async def test_sandbox_prepares_or_inspects_the_exact_plan(
    monkeypatch: pytest.MonkeyPatch, inspect: bool,
) -> None:
    api = client()
    expected = request(inspect)
    seen: list[PythonMetadataPreparationRequest] = []

    async def exchange(item: PythonMetadataPreparationRequest, *, socket_path: Path | None) -> Any:
        assert socket_path == Path("metadata.sock")
        assert item == expected
        seen.append(item)
        return response(inspect)

    monkeypatch.setattr(api, "request_python_metadata_preparation_broker", exchange)
    sandbox = api.PythonMetadataPreparationSandbox(socket_path=Path("metadata.sock"))
    if inspect:
        result = await sandbox.inspect(expected.invocation, approved_plan(),
                                       request(True).preparation_execution_id)
        assert result == response(True).status
    else:
        result = await sandbox.prepare(expected.invocation, approved_plan())
        assert result == response().receipt
    assert seen == [expected]


@pytest.mark.parametrize("inspect", [False, True])
@pytest.mark.parametrize("path", ["", "scope", "receipt", "receipt.plan", "receipt.plan.draft"])
async def test_sandbox_revalidates_even_substituted_v10_transport(
    monkeypatch: pytest.MonkeyPatch, inspect: bool, path: str,
) -> None:
    api = client()
    expected = request(inspect)
    if inspect and path.startswith("receipt"):
        path = "status." + path

    async def exchange(*args: Any, **kwargs: Any) -> Any:
        return replace(response(inspect), path, {"extra": True})

    monkeypatch.setattr(api, "request_python_metadata_preparation_broker", exchange)
    phase = "inspect" if inspect else "prepare"
    with pytest.raises(api.PythonMetadataPreparationFailure,
                       match=rf"^python_metadata\.{phase}\.metadata_prepare_response_invalid$"):
        sandbox = api.PythonMetadataPreparationSandbox()
        if inspect:
            await sandbox.inspect(expected.invocation, approved_plan(), expected.preparation_execution_id)
        else:
            await sandbox.prepare(expected.invocation, approved_plan())


@pytest.mark.parametrize("inspect", [False, True])
async def test_sandbox_preserves_typed_root_failure_as_finite_error(
    monkeypatch: pytest.MonkeyPatch, inspect: bool,
) -> None:
    api = client()
    expected = request(inspect)

    async def exchange(*args: Any, **kwargs: Any) -> Any:
        return response(inspect, ok=False, receipt=None, status=None, error="metadata_prepare_plan_changed")

    monkeypatch.setattr(api, "request_python_metadata_preparation_broker", exchange)
    phase = "inspect" if inspect else "prepare"
    with pytest.raises(api.PythonMetadataPreparationFailure,
                       match=rf"^python_metadata\.{phase}\.metadata_prepare_plan_changed$"):
        sandbox = api.PythonMetadataPreparationSandbox()
        if inspect:
            await sandbox.inspect(expected.invocation, approved_plan(), expected.preparation_execution_id)
        else:
            await sandbox.prepare(expected.invocation, approved_plan())


@pytest.mark.parametrize("path", ["invocation", "scope", "draft"])
async def test_sandbox_rejects_forged_planning_inputs_before_discovery(
    monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    api = client()
    value = replace(request().planning, path, {"extra": True})

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("forged planning inputs reached profile discovery")

    monkeypatch.setattr(profile_api.PythonProjectProfileSandbox, "inspect", forbidden)
    with pytest.raises(api.PythonMetadataPreparationFailure,
                       match=r"^python_metadata\.plan\.metadata_prepare_request_invalid$"):
        await api.PythonMetadataPreparationSandbox().plan(value.invocation, value.scope, value.draft)


def failure_type() -> Any:
    failure = getattr(client(), "PythonMetadataPreparationFailure", None)
    assert failure is not None, "high-level metadata SDK must preserve typed phase/code"
    return failure


@pytest.mark.parametrize("code,attempts", [
    ("python_project_profile_unavailable", 2),
    ("python_project_profile_unsupported", 1),
    ("python_project_runtime_missing", 1),
    ("python_project_runtime_untrusted", 1),
    ("input_supply_configuration_missing", 1),
    ("input_supply_configuration_untrusted", 1),
])
async def test_bound_v6_profile_negative_preserves_cause_without_extra_authority(
    monkeypatch: pytest.MonkeyPatch, code: str, attempts: int,
) -> None:
    from agent_hub.capabilities.project_verification import ProjectVerificationError
    from agent_hub.harness.tool_gateway import _project_authorization_failure_reason

    api = client()
    failure = failure_type()
    value = request().planning
    negative = profile_api.PythonProjectProfileResponse.model_validate_json(json.dumps({
        "version": 6, "action": "inspect_python_project_profile", "ok": False,
        "scope": profile_api.PythonProjectProfileScope.for_invocation(
            value.invocation).model_dump(mode="json"),
        "profile": None, "error": code,
    }), strict=True)
    raw = negative.model_dump_json().encode()
    writers: list[Writer] = []

    async def connect(path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        assert path == Path("metadata.sock").as_posix()
        reader, writer = asyncio.StreamReader(), Writer(Peer())
        reader.feed_data(struct.pack("!I", len(raw)) + raw)
        reader.feed_eof()
        writers.append(writer)
        return reader, cast(asyncio.StreamWriter, writer)

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("profile negative reached planning or preparation")

    monkeypatch.setattr(profile_api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    monkeypatch.setattr(api, "request_python_metadata_plan_broker", forbidden)
    monkeypatch.setattr(api, "request_python_metadata_preparation_broker", forbidden)
    with pytest.raises(failure) as caught:
        await api.PythonMetadataPreparationSandbox(socket_path=Path("metadata.sock")).plan(
            value.invocation, value.scope, value.draft)
    assert type(caught.value) is failure
    assert (caught.value.phase, caught.value.code) == ("profile", code)
    assert caught.value.public_code == f"python_metadata.profile.{code}"
    assert caught.value.public_code in api.PUBLIC_FAILURE_CODES
    assert _project_authorization_failure_reason(
        ProjectVerificationError(caught.value.public_code)) == caught.value.public_code
    assert _project_authorization_failure_reason(
        ProjectVerificationError(caught.value.public_code + ".forged")) == "capability authorization unavailable"
    assert len(writers) == attempts and all(writer.closed for writer in writers)
    for writer in writers:
        sent = profile_api.PythonProjectProfileRequest.model_validate_json(
            bytes(writer.writes[4:]), strict=True)
        assert sent.action == "inspect_python_project_profile"
        assert sent.invocation.install_allowed is False
        assert sent.invocation.sandbox_profile == "restricted"


def test_failure_constructor_is_finite_and_has_keyword_interface() -> None:
    from agent_hub.skills.sandbox.python_metadata_preparation_client import (
        PythonMetadataPreparationFailure,
    )

    failure = failure_type()
    value = failure(phase="profile", code="metadata_prepare_unavailable")
    assert type(value) is failure and isinstance(value, RuntimeError)
    assert isinstance(value, PythonMetadataPreparationFailure)
    assert (value.phase, value.code) == ("profile", "metadata_prepare_unavailable")
    assert value.public_code == str(value) == "python_metadata.profile.metadata_prepare_unavailable"
    codes = client().PUBLIC_FAILURE_CODES
    assert type(codes) is frozenset and value.public_code in codes
    assert "python_metadata.plan.metadata_plan_timeout" in codes
    assert "python_metadata.prepare.metadata_prepare_unknown" in codes
    assert "python_metadata.inspect.metadata_prepare_unknown" in codes
    for phase, code in [(True, "metadata_prepare_unavailable"), ("root", "metadata_prepare_unavailable"),
                        ("plan", PRIVATE), ("plan", True), ("plan", "metadata_plan_timeout\n"),
                        ("profile", "python_project_runtime_" + PRIVATE),
                        ("profile", "python_project_runtime_missing\n"),
                        ("prepare", "python_project_runtime_missing")]:
        with pytest.raises((ValueError, TypeError)) as caught:
            failure(phase=phase, code=code)
        assert PRIVATE not in str(caught.value)
    class Text(str):
        pass
    for phase, code in [(Text("plan"), "metadata_plan_timeout"), ("plan", Text("metadata_plan_timeout"))]:
        with pytest.raises((ValueError, TypeError)):
            failure(phase=phase, code=code)


@pytest.mark.parametrize("phase,code", [
    ("profile", "metadata_prepare_unavailable"),
    ("plan", "metadata_plan_unavailable"), ("plan", "metadata_plan_timeout"),
])
async def test_trusted_negative_reconnects_once_then_returns_plan(
    monkeypatch: pytest.MonkeyPatch, phase: str,
    code: Literal["metadata_prepare_unavailable", "metadata_plan_unavailable", "metadata_plan_timeout"],
) -> None:
    api = client()
    value = request().planning
    seen: list[str] = []

    async def inspect(self: Any, invocation: Any) -> Any:
        seen.append("profile")
        result = discovery(invocation)
        if phase == "profile" and seen == ["profile"]:
            return result.model_copy(update={"ok": False, "profile": None,
                                             "error": "python_project_profile_unavailable"})
        return result

    async def planning(item: PythonMetadataPlanningRequest, **kwargs: Any) -> Any:
        seen.append("plan")
        negative = phase == "plan" and seen == ["profile", "plan"]
        plan_error: MetadataPlanningError | None = None
        if negative:
            assert code != "metadata_prepare_unavailable"
            plan_error = code
        return PythonMetadataPlanningResponse(
            scope=profile_api.PythonProjectProfileScope.for_invocation(item.invocation),
            ok=not negative, error=plan_error,
            plan=None if negative else approved_plan())

    monkeypatch.setattr(profile_api.PythonProjectProfileSandbox, "inspect", inspect)
    monkeypatch.setattr(api, "request_python_metadata_plan_broker", planning)
    assert await api.PythonMetadataPreparationSandbox().plan(
        value.invocation, value.scope, value.draft) == approved_plan()
    assert seen == (["profile", "profile", "plan"] if phase == "profile"
                    else ["profile", "plan", "profile", "plan"])


@pytest.mark.parametrize("phase", ["profile", "plan"])
@pytest.mark.parametrize("error,code", [
    (OSError("metadata_plan_unavailable"), "metadata_prepare_unavailable"),
    (OSError("python_project_profile_unavailable"), "metadata_prepare_unavailable"),
    (OSError("python_project_runtime_missing"), "metadata_prepare_unavailable"),
    (RuntimeError("input_supply_configuration_untrusted"), "metadata_prepare_unavailable"),
    (TimeoutError("metadata_plan_timeout"), "metadata_prepare_timeout"),
    (ValueError("metadata_plan_unavailable"), "metadata_prepare_response_invalid"),
])
async def test_plain_errors_never_authorize_readonly_retry(
    monkeypatch: pytest.MonkeyPatch, phase: str, error: Exception, code: str,
) -> None:
    api = client()
    failure = failure_type()
    value = request().planning
    seen: list[str] = []

    async def inspect(self: Any, invocation: Any) -> Any:
        seen.append("profile")
        if phase == "profile":
            raise error
        return discovery(invocation)

    async def planning(*args: Any, **kwargs: Any) -> Any:
        seen.append("plan")
        raise error

    monkeypatch.setattr(profile_api.PythonProjectProfileSandbox, "inspect", inspect)
    monkeypatch.setattr(api, "request_python_metadata_plan_broker", planning)
    with pytest.raises(failure) as caught:
        await api.PythonMetadataPreparationSandbox().plan(value.invocation, value.scope, value.draft)
    assert type(caught.value) is failure
    assert (caught.value.phase, caught.value.code) == (phase, code)
    assert seen == (["profile"] if phase == "profile" else ["profile", "plan"])


@pytest.mark.parametrize("code", ["metadata_plan_unavailable", "metadata_plan_timeout",
                                  "metadata_plan_cache_untrusted"])
async def test_repeated_or_nonretryable_root_plan_failure_is_typed(
    monkeypatch: pytest.MonkeyPatch,
    code: Literal["metadata_plan_unavailable", "metadata_plan_timeout", "metadata_plan_cache_untrusted"],
) -> None:
    api = client()
    failure = failure_type()
    value = request().planning
    seen: list[str] = []

    async def inspect(self: Any, invocation: Any) -> Any:
        seen.append("profile")
        return discovery(invocation)

    async def planning(item: PythonMetadataPlanningRequest, **kwargs: Any) -> Any:
        seen.append("plan")
        return PythonMetadataPlanningResponse(
            scope=profile_api.PythonProjectProfileScope.for_invocation(item.invocation),
            ok=False, error=code)

    monkeypatch.setattr(profile_api.PythonProjectProfileSandbox, "inspect", inspect)
    monkeypatch.setattr(api, "request_python_metadata_plan_broker", planning)
    with pytest.raises(failure) as caught:
        await api.PythonMetadataPreparationSandbox().plan(value.invocation, value.scope, value.draft)
    assert (caught.value.phase, caught.value.code) == ("plan", code)
    assert seen == ["profile", "plan"] * (1 if code == "metadata_plan_cache_untrusted" else 2)


@pytest.mark.parametrize("inspect", [False, True])
@pytest.mark.parametrize("code", ERRORS)
async def test_high_level_negative_preserves_phase_and_code_without_reexecution(
    monkeypatch: pytest.MonkeyPatch, inspect: bool, code: str,
) -> None:
    api = client()
    failure = failure_type()
    expected = request(inspect)
    calls = 0

    async def exchange(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return response(inspect, ok=False, receipt=None, status=None, error=code)

    monkeypatch.setattr(api, "request_python_metadata_preparation_broker", exchange)
    with pytest.raises(failure) as caught:
        sandbox = api.PythonMetadataPreparationSandbox()
        if inspect:
            await sandbox.inspect(expected.invocation, approved_plan(), expected.preparation_execution_id)
        else:
            await sandbox.prepare(expected.invocation, approved_plan())
    assert type(caught.value) is failure
    assert (caught.value.phase, caught.value.code) == ("inspect" if inspect else "prepare", code)
    assert calls == 1


async def test_readonly_retry_does_not_reset_absolute_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    api = client()
    failure = failure_type()
    value = request().planning
    invocation = value.invocation.model_copy(update={"timeout_seconds": 1})
    calls = 0

    async def inspect(self: Any, invocation: Any) -> Any:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.6)
        return discovery(invocation).model_copy(update={"ok": False, "profile": None,
            "error": "python_project_profile_unavailable"})

    monkeypatch.setattr(profile_api.PythonProjectProfileSandbox, "inspect", inspect)
    started = asyncio.get_running_loop().time()
    with pytest.raises(failure) as caught:
        await api.PythonMetadataPreparationSandbox().plan(invocation, value.scope, value.draft)
    assert caught.value.code == "metadata_prepare_timeout"
    assert calls == 2 and asyncio.get_running_loop().time() - started < 1.2


@pytest.mark.parametrize("phase", ["profile", "plan", "prepare", "inspect"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_sdk_cancels_or_sanitizes_unexpected_exception_without_retry(
    monkeypatch: pytest.MonkeyPatch, phase: str, cancel: bool,
) -> None:
    api = client()
    failure = failure_type()
    value = request().planning
    calls = 0

    async def fail(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if cancel:
            raise asyncio.CancelledError
        raise RuntimeError(PRIVATE)

    async def inspect(self: Any, invocation: Any) -> Any:
        return discovery(invocation)

    monkeypatch.setattr(profile_api.PythonProjectProfileSandbox, "inspect",
                        fail if phase == "profile" else inspect)
    monkeypatch.setattr(api, "request_python_metadata_plan_broker", fail)
    monkeypatch.setattr(api, "request_python_metadata_preparation_broker", fail)
    expected = asyncio.CancelledError if cancel else failure
    with pytest.raises(expected) as caught:
        sandbox = api.PythonMetadataPreparationSandbox()
        if phase in {"profile", "plan"}:
            await sandbox.plan(value.invocation, value.scope, value.draft)
        elif phase == "prepare":
            await sandbox.prepare(request().invocation, approved_plan())
        else:
            await sandbox.inspect(request(True).invocation, approved_plan(),
                                  request(True).preparation_execution_id)
    assert calls == 1
    if not cancel:
        assert (caught.value.phase, caught.value.code) == (phase, "metadata_prepare_unavailable")
        assert PRIVATE not in str(caught.value)


@pytest.mark.parametrize("phase,code", [
    ("profile", "python_project_profile_unavailable"),
    ("profile", "python_project_runtime_missing"),
    ("profile", "input_supply_configuration_untrusted"),
    ("plan", "metadata_plan_unavailable"),
])
@pytest.mark.parametrize("defect", ["extra", "scope", "code"])
async def test_negative_response_must_validate_before_retry(
    monkeypatch: pytest.MonkeyPatch, phase: str, code: str, defect: str,
) -> None:
    api = client()
    failure = failure_type()
    value = request().planning
    calls = 0

    def corrupt(result: Any) -> Any:
        if defect == "code":
            return replace(result, "", {"error": "python_project_runtime_" + PRIVATE})
        return replace(result, "" if defect == "extra" else "scope",
                       {"extra": True} if defect == "extra" else {"project_id": "foreign"})

    async def inspect(self: Any, invocation: Any) -> Any:
        nonlocal calls
        if phase == "profile":
            calls += 1
            negative = discovery(invocation).model_copy(update={"ok": False, "profile": None,
                "error": code})
            return corrupt(negative)
        return discovery(invocation)

    async def planning(item: PythonMetadataPlanningRequest, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return corrupt(PythonMetadataPlanningResponse(
            scope=profile_api.PythonProjectProfileScope.for_invocation(item.invocation),
            ok=False, error="metadata_plan_unavailable"))

    monkeypatch.setattr(profile_api.PythonProjectProfileSandbox, "inspect", inspect)
    monkeypatch.setattr(api, "request_python_metadata_plan_broker", planning)
    with pytest.raises(failure) as caught:
        await api.PythonMetadataPreparationSandbox().plan(value.invocation, value.scope, value.draft)
    assert (caught.value.phase, caught.value.code) == (phase, "metadata_prepare_response_invalid")
    assert calls == 1


@pytest.mark.parametrize("state", ["unknown", "absent"])
async def test_readonly_inspect_returns_unknown_or_absent_without_preparing(
    monkeypatch: pytest.MonkeyPatch, state: Literal["unknown", "absent"],
) -> None:
    from agent_hub.skills.sandbox.python_metadata_preparation_protocol import (
        PythonMetadataPreparationStatus,
    )

    api = client()
    expected = request(True)
    assert expected.preparation_execution_id is not None
    status = PythonMetadataPreparationStatus(plan_sha256=expected.expected_plan_sha256,
        preparation_execution_id=expected.preparation_execution_id, state=state)
    calls = 0

    async def exchange(item: PythonMetadataPreparationRequest, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        assert item.action == "inspect_python_dependency_metadata_preparation"
        return response(True, status=status, receipt=None)

    monkeypatch.setattr(api, "request_python_metadata_preparation_broker", exchange)
    assert await api.PythonMetadataPreparationSandbox().inspect(
        expected.invocation, approved_plan(), expected.preparation_execution_id) == status
    assert calls == 1
