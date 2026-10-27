"""Cold health dispatch must not initialize unrelated privileged protocols."""
from __future__ import annotations

import asyncio
import builtins
import json
import struct
from collections.abc import Coroutine
from typing import Any, cast

import pytest

from agent_hub.skills.sandbox import broker as api

_SPECIAL = ("agent_hub.skills.sandbox.python_dependency_",
            "agent_hub.skills.sandbox.python_metadata_",
            "agent_hub.skills.sandbox.python_input_supply_",
            "agent_hub.skills.sandbox.python_project_",
            "agent_hub.skills.sandbox.toolchain_")


class Reader:
    def __init__(self, body: bytes) -> None:
        self.data = struct.pack("!I", len(body)) + body

    async def readexactly(self, size: int) -> bytes:
        value, self.data = self.data[:size], self.data[size:]
        if len(value) != size:
            raise asyncio.IncompleteReadError(value, size)
        return value


class Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, value: bytes) -> None:
        self.data.extend(value)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        assert self.closed


def run(coroutine: Coroutine[Any, Any, None]) -> None:
    # Memory streams and a native-probe double never yield to external IO.
    try:
        coroutine.send(None)
    except StopIteration:
        return
    finally:
        coroutine.close()
    pytest.fail("unexpected native IO in dispatcher unit test")


def block_special_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    original = builtins.__import__

    def guarded(name: str, *args: Any, **kwargs: Any) -> Any:
        assert not name.startswith(_SPECIAL), f"unrelated cold import: {name}"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


def serve(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object], *, uid: int = 10001,
          execution: api.SkillBroker | None = None) -> dict[str, Any]:
    broker = execution or api.SkillBroker(api.BrokerPolicy())
    if execution is None:
        async def forbidden(backend: str) -> api.BrokerResponse:
            pytest.fail("unexpected native probe in rejection test")
        monkeypatch.setattr(broker, "_probe", forbidden)
    monkeypatch.setattr(api, "_peer_uid", lambda writer: uid)
    reader, writer = Reader(json.dumps(payload).encode()), Writer()
    run(api._serve_client(cast(asyncio.StreamReader, reader), cast(asyncio.StreamWriter, writer), broker))
    assert writer.closed
    assert struct.unpack("!I", writer.data[:4])[0] == len(writer.data[4:])
    return cast(dict[str, Any], json.loads(writer.data[4:]))


def test_first_v1_probe_works_even_when_special_protocol_imports_are_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = api.SkillBroker(api.BrokerPolicy())
    called: list[str] = []

    async def native(backend: str) -> api.BrokerResponse:
        called.append(backend)
        return api.BrokerResponse(ok=True)

    monkeypatch.setattr(broker, "_probe", native)
    block_special_imports(monkeypatch)
    response = serve(monkeypatch, {"version": 1, "action": "probe", "backend": "systemd"},
                     execution=broker)
    assert response == {"ok": True, "error": None, "result": None}
    assert called == ["systemd"]


@pytest.mark.parametrize("version", [991, "1", None])
def test_unknown_versions_fail_without_loading_special_protocols(
    monkeypatch: pytest.MonkeyPatch, version: object,
) -> None:
    block_special_imports(monkeypatch)
    response = serve(monkeypatch, {"version": version, "action": "probe", "backend": "systemd"})
    assert response["ok"] is False


@pytest.mark.parametrize("version,action,error", [
    (1, "probe", "caller uid is not authorized"),
    (6, "inspect_python_project_profile", "python_project_profile_request_invalid"),
    (8, "inspect_python_dependency_closure", "python_dependency_request_invalid"),
    (9, "plan_python_dependency_metadata", "metadata_plan_request_invalid"),
    (10, "prepare_python_dependency_metadata", "metadata_prepare_request_invalid"),
])
def test_foreign_uid_cannot_trigger_privileged_metadata_imports(
    monkeypatch: pytest.MonkeyPatch, version: int, action: str, error: str,
) -> None:
    block_special_imports(monkeypatch)
    response = serve(monkeypatch, {"version": version, "action": action, "backend": "systemd"}, uid=17)
    assert response == {"ok": False, "error": error, "result": None}


@pytest.mark.parametrize("cancel", [False, True])
def test_v4_release_keeps_strict_request_and_typed_cancellation(
    monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    from agent_hub.skills.sandbox.python_project_protocol import PythonProjectBrokerResponse
    from tests.unit.skills.sandbox.test_python_project_release import release

    request = release()
    broker = api.SkillBroker(api.BrokerPolicy())
    received: list[object] = []

    async def handle(value: object, *, peer_uid: int) -> PythonProjectBrokerResponse:
        received.append(value)
        assert peer_uid == 10001
        if cancel:
            raise asyncio.CancelledError
        return PythonProjectBrokerResponse(action=request.action, execution_id=request.invocation.execution_id,
                                           ok=False, error="python_project_environment_changed")

    monkeypatch.setattr(broker, "handle_python_project", handle)
    response = serve(monkeypatch, request.model_dump(mode="json"), execution=broker)
    assert received == [request]
    assert response["version"] == 4 and response["ok"] is False
    assert response["error"] == ("python_project_execution_unavailable" if cancel
                                 else "python_project_environment_changed")
    received.clear()
    bad = request.model_dump(mode="json") | {"untrusted": "do-not-echo"}
    rejected = serve(monkeypatch, bad, execution=broker)
    assert received == [] and rejected["ok"] is False


def test_v5_cancel_retains_its_typed_response_after_branch_lazy_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.skills.sandbox.test_python_input_supply_wire import request

    broker = api.SkillBroker(api.BrokerPolicy())

    async def cancelled(value: object, *, peer_uid: int) -> Any:
        assert peer_uid == 10001
        raise asyncio.CancelledError

    monkeypatch.setattr(broker, "handle_input_supply", cancelled)
    response = serve(monkeypatch, request().model_dump(mode="json"), execution=broker)
    assert response["version"] == 5 and response["error"] == "input_supply_unavailable"


def test_v7_cancel_keeps_scope_and_typed_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.skills.sandbox import toolchain_protocol
    from tests.unit.skills.sandbox.test_toolchain_protocol import invocation

    request = toolchain_protocol.ToolchainRequest(
        action="plan_project_toolchain", invocation=invocation(toolchain_protocol))
    broker = api.SkillBroker(api.BrokerPolicy())

    async def cancelled(value: object, *, peer_uid: int) -> Any:
        assert value == request and peer_uid == 10001
        raise asyncio.CancelledError

    monkeypatch.setattr(broker, "handle_toolchain", cancelled)
    response = serve(monkeypatch, request.model_dump(mode="json"), execution=broker)
    assert response["version"] == 7 and response["error"] == "toolchain_unavailable"
    assert response["scope"] == request.invocation.scope.model_dump(mode="json")


def test_v10_still_validates_response_and_rejects_invalid_request_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.skills.sandbox.test_python_metadata_preparation_wire import request, response

    expected = request()
    broker = api.SkillBroker(api.BrokerPolicy())
    seen: list[object] = []

    async def handle(value: object, *, peer_uid: int) -> Any:
        assert peer_uid == 10001
        seen.append(value)
        return response()

    monkeypatch.setattr(broker, "handle_metadata_preparation", handle)
    result = serve(monkeypatch, expected.model_dump(mode="json"), execution=broker)
    assert result["version"] == 10 and result["ok"] is True and seen == [expected]
    seen.clear()
    bad = serve(monkeypatch, expected.model_dump(mode="json") | {"untrusted": "DO-NOT-ECHO"},
                execution=broker)
    assert bad["error"] == "metadata_prepare_request_invalid" and seen == []
    assert "DO-NOT-ECHO" not in json.dumps(bad)
