"""Framing tests use the real parsers with in-memory streams, never a socket."""
from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Coroutine
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.broker import BrokerPolicy, SkillBroker
from agent_hub.skills.sandbox.project import ProjectExecutionBroker

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer

from agent_hub.skills.sandbox.python_premodel_retirement import (
    PythonStoppedPremodelMaintenanceRequest,
    PythonStoppedPremodelMaintenanceResponse,
)
from agent_hub.skills.sandbox.python_project_protocol import PythonFailedMaintenanceInvocation


def request() -> PythonStoppedPremodelMaintenanceRequest:
    return PythonStoppedPremodelMaintenanceRequest(phase="inspect", invocation=PythonFailedMaintenanceInvocation(
        execution_id="python_release_expired_" + "a" * 32, tenant_id=UUID(int=1),
        run_id=UUID(int=2), project_id="billing", session_id="main",
    ))


def run_immediate[T](coroutine: Coroutine[object, object, T]) -> T:
    values: list[T] = []

    async def capture() -> None:
        values.append(await coroutine)

    pending = capture()
    try:
        pending.send(None)
    except StopIteration:
        return values[0]
    finally:
        pending.close()
        coroutine.close()
    raise AssertionError("test unexpectedly performed asynchronous external IO")


def test_new_inspect_uses_the_existing_strict_v4_root_parser() -> None:
    from agent_hub.skills.sandbox.python_project_backend import _request

    value = request().model_dump(mode="json") | {"version": 4}
    parsed = _request(json.dumps(value).encode())
    assert type(parsed) is PythonStoppedPremodelMaintenanceRequest
    assert parsed.invocation == request().invocation and parsed.phase == "inspect"


def test_client_accepts_only_revalidated_inspect_and_commit_shapes() -> None:
    from agent_hub.skills.sandbox.python_project_client import _validated_request

    assert _validated_request(request()) == request()
    with pytest.raises(ValueError):
        _validated_request(request().model_copy(update={"phase": "commit"}))


def test_new_metadata_frame_does_not_expand_legacy_request_allowance() -> None:
    from agent_hub.skills.sandbox.python_project_backend import _request
    from agent_hub.skills.sandbox.python_project_protocol import PythonFailedMaintenanceRequest

    padding = b" " * (64 * 1024)
    old = PythonFailedMaintenanceRequest(invocation=request().invocation, phase="inspect")
    with pytest.raises(ValueError):
        _request(padding + old.model_dump_json().encode())
    assert type(_request(padding + request().model_dump_json().encode())) is PythonStoppedPremodelMaintenanceRequest
    with pytest.raises(ValueError):
        _request(b" " * (1024**2 + 4096) + request().model_dump_json().encode())


def test_unauthorized_peer_receives_a_typed_failure_before_config_or_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.skills.sandbox import python_project_broker as broker

    monkeypatch.setattr(broker, "_configuration", lambda: pytest.fail("read unauthorized config"))
    reply = run_immediate(broker.handle_python_project(
        ProjectExecutionBroker(BrokerPolicy(allowed_uid=10001)), request(), peer_uid=10002,
    ))
    assert type(reply) is PythonStoppedPremodelMaintenanceResponse
    assert reply.ok is False and reply.error == "python_project_request_invalid"
    reply.require_for(request())


class Reader(asyncio.StreamReader):
    def __init__(self, body: bytes):
        self.body = struct.pack("!I", len(body)) + body

    async def readexactly(self, count: int) -> bytes:
        value, self.body = self.body[:count], self.body[count:]
        assert len(value) == count
        return value


class Writer(asyncio.StreamWriter):
    def __init__(self) -> None:
        self.body = bytearray()
        self.closed = False

    def __del__(self) -> None:
        # This in-memory StreamWriter has no transport or external resource.
        pass

    def write(self, body: ReadableBuffer) -> None:
        self.body.extend(memoryview(body))

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        assert self.closed


def test_real_broker_frame_dispatches_to_the_new_request_type(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.skills.sandbox import broker

    calls = []

    async def handle(
        value: PythonStoppedPremodelMaintenanceRequest, *, peer_uid: int,
    ) -> PythonStoppedPremodelMaintenanceResponse:
        calls.append((type(value), peer_uid))
        return PythonStoppedPremodelMaintenanceResponse(execution_id=value.invocation.execution_id,
            ok=True, retirement_phase="inspect", retirement_snapshot=None)

    monkeypatch.setattr(broker, "_peer_uid", lambda _: 10001)
    writer = Writer()
    body = json.dumps(request().model_dump(mode="json") | {"version": 4}).encode()
    instance = SkillBroker(BrokerPolicy())
    monkeypatch.setattr(instance, "handle_python_project", handle)
    run_immediate(broker._serve_client(Reader(body), writer, instance))
    assert calls == [(PythonStoppedPremodelMaintenanceRequest, 10001)]
    reply = PythonStoppedPremodelMaintenanceResponse.model_validate_json(bytes(writer.body[4:]), strict=True)
    assert reply.ok and writer.closed
    reply.require_for(request())


def test_real_client_frame_parses_the_new_reply_without_product_assumptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.skills.sandbox import python_project_client as client

    expected = PythonStoppedPremodelMaintenanceResponse(execution_id=request().invocation.execution_id,
        ok=True, retirement_phase="inspect", retirement_snapshot=None)
    writer = Writer()

    async def connect(_: object) -> tuple[Reader, Writer]:
        return Reader(expected.model_dump_json().encode()), writer

    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    result = run_immediate(client.request_python_project_broker(request()))
    assert result == expected and writer.closed
