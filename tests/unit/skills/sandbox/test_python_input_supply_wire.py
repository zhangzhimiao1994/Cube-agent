import asyncio
import json
from collections.abc import Buffer
from typing import Never, Protocol
from uuid import UUID

import pytest
from pydantic import JsonValue, TypeAdapter

from agent_hub.skills.sandbox import broker as api
from agent_hub.skills.sandbox.project import OriginalProjectReference
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyRequest,
    PythonInputSupplyResponse,
)
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectProbeInvocation


class Writer(asyncio.StreamWriter):
    """In-memory writer implementing every operation used by the broker."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, value: Buffer) -> None:
        self.data.extend(memoryview(value))

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def __del__(self) -> None:
        # No underlying transport exists for this in-memory test double.
        pass


def request() -> PythonInputSupplyRequest:
    return PythonInputSupplyRequest(action="plan_python_project_inputs",
        invocation=PythonProjectProbeInvocation(execution_id="supply_wire_1",
            tenant_id=UUID(int=1), run_id=UUID(int=2), project_id="project", session_id="session",
            sandbox_profile="workspace_write", timeout_seconds=30,
            original_reference=OriginalProjectReference(schema_version=1, contract_sha256="a" * 64,
                                input_source_sha256="b" * 64, file_count=6)))


class SupplyHandler(Protocol):
    async def __call__(self, item: PythonInputSupplyRequest, *, peer_uid: int) -> PythonInputSupplyResponse: ...


async def serve(monkeypatch: pytest.MonkeyPatch, payload: object, handler: SupplyHandler) -> dict[str, JsonValue]:
    broker = api.SkillBroker(api.BrokerPolicy())
    monkeypatch.setattr(broker, "handle_input_supply", handler, raising=False)
    monkeypatch.setattr(api, "_peer_uid", lambda writer: 10001)
    reader, writer = asyncio.StreamReader(), Writer()
    body = json.dumps(payload).encode()
    reader.feed_data(len(body).to_bytes(4) + body)
    reader.feed_eof()
    await api._serve_client(reader, writer, broker)
    assert writer.closed
    return TypeAdapter(dict[str, JsonValue]).validate_json(writer.data[4:])


@pytest.mark.asyncio
async def test_v5_routes_to_supply_handler_without_changing_v4(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[PythonInputSupplyRequest, int]] = []

    async def handle(item: PythonInputSupplyRequest, *, peer_uid: int) -> PythonInputSupplyResponse:
        calls.append((item, peer_uid))
        return PythonInputSupplyResponse(action=item.action,
            execution_id=item.invocation.execution_id, ok=False, error="input_supply_configuration_missing")

    result = await serve(monkeypatch, request().model_dump(mode="json"), handle)
    assert calls == [(request(), 10001)]
    assert result["version"] == 5 and result["error"] == "input_supply_configuration_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [5, 4, "5", True, 5.0])
async def test_invalid_v5_does_not_echo_untrusted_payload_or_route(
    monkeypatch: pytest.MonkeyPatch, version: str | bool | float,
) -> None:
    async def forbidden(item: PythonInputSupplyRequest, *, peer_uid: int) -> Never:
        pytest.fail("invalid request reached privileged handler")

    result = await serve(monkeypatch, request().model_dump(mode="json") | {"version": version,
        "url": "https://secret.invalid/private-key-DO-NOT-ECHO"}, forbidden)
    assert not result["ok"] and result["error"] == "input_supply_request_invalid"
    assert "DO-NOT-ECHO" not in json.dumps(result)


@pytest.mark.asyncio
async def test_cancelled_v5_has_typed_failure_not_legacy_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def cancelled(item: PythonInputSupplyRequest, *, peer_uid: int) -> Never:
        raise asyncio.CancelledError

    result = await serve(monkeypatch, request().model_dump(mode="json"), cancelled)
    assert result["version"] == 5 and not result["ok"]
    assert result["error"] == "input_supply_unavailable"
