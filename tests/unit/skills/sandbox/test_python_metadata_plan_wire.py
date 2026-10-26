"""Real dispatch/SDK with framed memory streams, not native peer/cache proof."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import socket
import struct
from typing import Any

import pytest

from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    PythonMetadataPlanningResponse,
)
from tests.unit.skills.sandbox.test_python_project_profile_wire import serve
from tests.unit.skills.test_python_metadata_plan_protocol import request, response

CLIENT = "agent_hub.skills.sandbox.python_metadata_plan_client"


def client() -> Any:
    assert importlib.util.find_spec(CLIENT) is not None, "authenticated metadata planning client missing"
    return importlib.import_module(CLIENT)


def test_client_exists() -> None:
    assert callable(client().request_python_metadata_plan_broker)


async def test_v9_dispatch_validates_authenticated_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    planner = importlib.import_module("agent_hub.skills.sandbox.python_metadata_planning")
    seen = []

    async def plan(value: Any, *, policy: Any) -> Any:
        assert policy.allowed_uid == 10001
        seen.append(value)
        return response()

    monkeypatch.setattr(planner, "plan_python_dependency_metadata", plan)
    expected = request()
    result = await serve(monkeypatch, expected.model_dump(mode="json"))
    parsed = PythonMetadataPlanningResponse.model_validate_json(json.dumps(result), strict=True)
    parsed.require_for(expected)
    assert parsed.ok and seen == [expected]


@pytest.mark.parametrize("uid", [0, 17])
async def test_v9_rejects_foreign_peer_before_reading_workspace(
    monkeypatch: pytest.MonkeyPatch, uid: int,
) -> None:
    planner = importlib.import_module("agent_hub.skills.sandbox.python_metadata_planning")

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("foreign peer reached Root metadata planner")

    monkeypatch.setattr(planner, "plan_python_dependency_metadata", forbidden)
    result = await serve(monkeypatch, request().model_dump(mode="json"), uid=uid)
    assert result == {"ok": False, "result": None, "error": "metadata_plan_request_invalid"}


@pytest.mark.parametrize("changes", [
    {"version": "9"}, {"version": 9.0}, {"version": True}, {"version": 8},
    {"action": "supply_python_project_inputs"}, {"purpose": "install"},
    {"url": "/private/DO-NOT-ECHO"}, {"original_reference": {}},
])
async def test_v9_invalid_envelopes_never_downgrade(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any],
) -> None:
    planner = importlib.import_module("agent_hub.skills.sandbox.python_metadata_planning")

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid envelope reached Root metadata planner")

    monkeypatch.setattr(planner, "plan_python_dependency_metadata", forbidden)
    result = await serve(monkeypatch, request().model_dump(mode="json") | changes)
    assert result["ok"] is False
    assert "DO-NOT-ECHO" not in json.dumps(result)


async def test_v9_readonly_cancellation_does_not_become_project_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = importlib.import_module("agent_hub.skills.sandbox.python_metadata_planning")

    async def cancelled(*args: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(planner, "plan_python_dependency_metadata", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await serve(monkeypatch, request().model_dump(mode="json"))


class Writer:
    def __init__(self, raw: bytes, *, uid: int = 0, gid: int = 0) -> None:
        self.raw = raw
        self.uid, self.gid = uid, gid
        self.writes = bytearray()
        self.closed = False

    def write(self, raw: bytes) -> None:
        self.writes.extend(raw)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        assert self.closed

    def get_extra_info(self, name: str) -> Any:
        return self if name == "socket" else None

    def getsockopt(self, level: int, option: int, length: int) -> bytes:
        assert level == socket.SOL_SOCKET and length == 12
        return struct.pack("3i", 123, self.uid, self.gid)


async def setup_connection(monkeypatch: pytest.MonkeyPatch, raw: bytes, **kwargs: Any) -> tuple[Any, Writer]:
    api = client()
    writer = Writer(raw, **kwargs)
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", len(raw)) + raw)
    reader.feed_eof()

    async def connect(path: str) -> tuple[Any, Any]:
        return reader, writer

    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(api.socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(api.asyncio, "open_unix_connection", connect, raising=False)
    return api, writer


async def test_client_uses_root_peer_and_scope_bound_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    api, writer = await setup_connection(monkeypatch, response().model_dump_json().encode())
    result = await api.request_python_metadata_plan_broker(request())
    result.require_for(request())
    assert result.ok and writer.closed
    frame = json.loads(writer.writes[4:])
    assert frame["version"] == 9 and frame["purpose"] == "python_dependency_metadata"
    assert "original_reference" not in frame and "install_allowed" not in frame


@pytest.mark.parametrize("uid,gid", [(10001, 10001), (0, 10001), (17, 0)])
async def test_client_checks_root_peer_before_sending_task_data(
    monkeypatch: pytest.MonkeyPatch, uid: int, gid: int,
) -> None:
    api, writer = await setup_connection(monkeypatch, response().model_dump_json().encode(), uid=uid, gid=gid)
    with pytest.raises((OSError, ValueError)):
        await api.request_python_metadata_plan_broker(request())
    assert not writer.writes and writer.closed


@pytest.mark.parametrize("change", ["foreign", "duplicate", "extra", "truncated", "nonfinite"])
async def test_client_rejects_unbound_or_malformed_response(
    monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    value = response().model_dump(mode="json")
    if change == "foreign": value["scope"]["run_id"] = "00000000-0000-0000-0000-000000000099"
    if change == "extra": value["url"] = "/private/DO-NOT-ECHO"
    raw = json.dumps(value).encode()
    if change == "duplicate": raw = raw.replace(b'"version": 9', b'"version": 9, "version": 9')
    if change == "truncated": raw = raw[:12]
    if change == "nonfinite": raw = raw.replace(b'"ok": true', b'"ok": NaN')
    api, writer = await setup_connection(monkeypatch, raw)
    with pytest.raises(ValueError, match="metadata_plan_response_invalid"):
        await api.request_python_metadata_plan_broker(request())
    assert writer.closed


@pytest.mark.parametrize("level", ["request", "invocation", "scope", "draft"])
async def test_invalid_copied_request_is_rejected_before_connect(
    monkeypatch: pytest.MonkeyPatch, level: str,
) -> None:
    api = client()

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid request opened a socket")

    monkeypatch.setattr(api.asyncio, "open_unix_connection", forbidden, raising=False)
    req = request()
    if level == "request":
        req = req.model_copy(update={"original_reference": {}})
    else:
        req = req.model_copy(update={level: getattr(req, level).model_copy(update={"recipe": {}})})
    with pytest.raises(ValueError, match="metadata_plan_request_invalid"):
        await api.request_python_metadata_plan_broker(req)


async def test_typed_root_negative_is_not_a_download_or_install_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    req = request()
    from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileScope

    negative = PythonMetadataPlanningResponse(scope=PythonProjectProfileScope.for_invocation(req.invocation),
        ok=False, error="metadata_plan_cache_untrusted")
    api, writer = await setup_connection(monkeypatch, negative.model_dump_json().encode())
    result = await api.request_python_metadata_plan_broker(req)
    assert not result.ok and result.error == "metadata_plan_cache_untrusted" and result.plan is None
    assert writer.closed


async def test_transport_cancellation_closes_socket_without_termination_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    api = client()
    writer = Writer(b"")
    reader = asyncio.StreamReader()

    async def connect(path: str) -> tuple[Any, Any]:
        return reader, writer

    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(api.socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(api.asyncio, "open_unix_connection", connect, raising=False)
    task = asyncio.create_task(api.request_python_metadata_plan_broker(request()))
    for _ in range(20):
        await asyncio.sleep(0)
        if writer.writes:
            break
    assert writer.writes
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed
    assert b"terminate_project" not in writer.writes
