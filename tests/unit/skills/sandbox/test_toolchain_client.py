"""In-memory wire checks are not native peer credentials or installation proof."""

import asyncio
import importlib
import importlib.util
import json
import struct
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_hub.skills.sandbox import broker, toolchain_protocol
from agent_hub.skills.sandbox.broker import BrokerPolicy
from tests.unit.skills.sandbox.test_toolchain_protocol import invocation, plan


def test_explicit_toolchain_client_exists() -> None:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.toolchain_client") is not None


class Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@pytest.mark.asyncio
async def test_v7_dispatch_is_distinct_and_passes_authenticated_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = toolchain_protocol.ToolchainRequest(
        action="plan_project_toolchain", invocation=invocation(toolchain_protocol)
    )
    raw = request.model_dump_json().encode()
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", len(raw)) + raw)
    reader.feed_eof()
    writer = Writer()
    called = []

    async def handle(value: Any, *, peer_uid: int) -> Any:
        called.append((value, peer_uid))
        return toolchain_protocol.ToolchainResponse(
            action=value.action,
            execution_id=value.invocation.execution_id,
            scope=value.invocation.scope,
            ok=True,
            plan=plan(toolchain_protocol),
            missing=True,
        )

    fake = SimpleNamespace(_policy=BrokerPolicy(), handle_toolchain=handle)
    monkeypatch.setattr(broker, "_peer_uid", lambda w: 10001)
    await broker._serve_client(reader, cast(asyncio.StreamWriter, writer), cast(broker.SkillBroker, fake))
    assert called == [(request, 10001)]
    size = struct.unpack("!I", writer.data[:4])[0]
    response = toolchain_protocol.parse_toolchain_response(bytes(writer.data[4 : 4 + size]))
    response.require_for(request)
    assert writer.closed


@pytest.mark.asyncio
async def test_reserved_action_with_wrong_version_cannot_fall_back_to_old_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = toolchain_protocol.ToolchainRequest(
        action="plan_project_toolchain", invocation=invocation(toolchain_protocol)
    )
    raw = request.model_dump_json().encode().replace(b'"version":7', b'"version":5')
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", len(raw)) + raw)
    reader.feed_eof()
    writer = Writer()
    monkeypatch.setattr(broker, "_peer_uid", lambda w: 10001)
    fake = SimpleNamespace(_policy=BrokerPolicy())
    await broker._serve_client(reader, cast(asyncio.StreamWriter, writer), cast(broker.SkillBroker, fake))
    assert b"toolchain_request_invalid" in writer.data
    assert b"input_supply_request_invalid" not in writer.data


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [[], {}, ["supply_project_toolchain"]])
async def test_unhashable_action_is_a_closed_invalid_request(
    monkeypatch: pytest.MonkeyPatch,
    action: object,
) -> None:
    raw = json.dumps({"version": 1, "action": action}).encode()
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", len(raw)) + raw)
    reader.feed_eof()
    writer = Writer()
    monkeypatch.setattr(broker, "_peer_uid", lambda w: 10001)
    await broker._serve_client(reader, cast(asyncio.StreamWriter, writer),
                               cast(broker.SkillBroker, SimpleNamespace(_policy=BrokerPolicy())))
    assert writer.closed
    assert b'"ok":false' in writer.data


@pytest.mark.asyncio
async def test_normal_response_wait_includes_the_sdk_two_unit_cleanup_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.skills.sandbox import toolchain_client

    request = toolchain_protocol.ToolchainRequest(
        action="plan_project_toolchain", invocation=invocation(toolchain_protocol)
    )
    response = toolchain_protocol.ToolchainResponse(
        action=request.action,
        execution_id=request.invocation.execution_id,
        scope=request.invocation.scope,
        ok=True,
        plan=plan(toolchain_protocol),
        missing=True,
    )
    original = asyncio.timeout
    budgets = []

    def timeout(value: float) -> Any:
        budgets.append(value)
        return original(value)

    async def exchange(*args: Any, **kwargs: Any) -> Any:
        return response

    monkeypatch.setattr(toolchain_client, "_systemd_supported", lambda: True)
    monkeypatch.setattr(toolchain_client, "request_toolchain_broker", exchange)
    monkeypatch.setattr(asyncio, "timeout", timeout)
    assert await toolchain_client.ToolchainSandbox()._exchange(request) == response
    assert budgets == [
        request.invocation.timeout_seconds + toolchain_protocol.TOOLCHAIN_TERMINATION_SECONDS
    ]
