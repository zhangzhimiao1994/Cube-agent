from __future__ import annotations

import asyncio
import json
import socket
import struct
from typing import Any, cast

import pytest

from agent_hub.skills.sandbox import broker
from tests.unit.skills.sandbox.test_python_input_supply_wire import Writer
from tests.unit.skills.sandbox.test_python_project_profile import (
    api as api,  # noqa: PLC0414 - shared pytest fixture
)
from tests.unit.skills.sandbox.test_python_project_profile import (
    configured as configured,  # noqa: PLC0414
)
from tests.unit.skills.sandbox.test_python_project_profile import invocation


async def serve(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], *, uid: int = 10001) -> dict[str, Any]:
    monkeypatch.setattr(broker, '_peer_uid', lambda writer: uid)
    # The shared transport fake predates typed fixtures; it is not a real socket.
    reader, writer = asyncio.StreamReader(), cast(Any, Writer)()
    body = json.dumps(payload).encode()
    reader.feed_data(len(body).to_bytes(4) + body)
    reader.feed_eof()
    await broker._serve_client(reader, writer, broker.SkillBroker(broker.BrokerPolicy()))
    assert writer.closed
    return json.loads(writer.data[4:])  # type: ignore[no-any-return]


async def test_v6_profile_routes_only_after_peer_authorization(api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    request = api.PythonProjectProfileRequest(invocation=invocation())
    payload = await serve(monkeypatch, request.model_dump(mode='json'))
    response = api.PythonProjectProfileResponse.model_validate_json(json.dumps(payload), strict=True)
    response.require_for(request)
    assert response.ok and response.profile.status == 'configured_not_probed'
    assert configured.reads == ['root', 'supply']


async def test_v6_unauthorized_peer_never_reads_configuration(api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    request = api.PythonProjectProfileRequest(invocation=invocation())
    result = await serve(monkeypatch, request.model_dump(mode='json'), uid=7)
    assert result['ok'] is False
    assert configured.reads == []


@pytest.mark.parametrize('version', [6, '6', 6.0, True])
async def test_invalid_v6_is_redacted_and_never_downgraded(api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch, version: object) -> None:
    request = api.PythonProjectProfileRequest(invocation=invocation())
    result = await serve(monkeypatch, {**request.model_dump(mode='json'), 'version': version,
                                      'host_path': '/private/DO-NOT-ECHO'})
    assert result['ok'] is False and configured.reads == []
    assert 'DO-NOT-ECHO' not in json.dumps(result)


@pytest.mark.parametrize('peer', [(321, 10001, 10001), (321, 0, 10001), (0, 0, 0), None, b'bad'])
async def test_profile_sdk_refuses_nonroot_peer_before_task_bytes(
    api: Any, monkeypatch: pytest.MonkeyPatch, peer: Any,
) -> None:
    reader = asyncio.StreamReader()
    reader.feed_eof()
    writer = cast(Any, Writer)()
    class Socket:
        def getsockopt(self, *args: Any) -> bytes:
            return peer if type(peer) is bytes else struct.pack('3i', *peer)
    writer.get_extra_info = lambda name: None if peer is None else Socket()
    async def connect(path: str) -> Any:
        return reader, writer
    monkeypatch.setattr(asyncio, 'open_unix_connection', connect, raising=False)
    monkeypatch.setattr(socket, 'SO_PEERCRED', 17, raising=False)
    request = api.PythonProjectProfileRequest(invocation=invocation())
    with pytest.raises(OSError, match='^python_project_profile_unavailable$'):
        await api.request_python_project_profile_broker(request)
    assert not writer.data and writer.closed


async def test_profile_sdk_authenticates_root_before_sending_request(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = api.PythonProjectProfileRequest(invocation=invocation())
    payload = json.dumps(await serve(monkeypatch, request.model_dump(mode='json'))).encode()
    reader = asyncio.StreamReader()
    reader.feed_data(len(payload).to_bytes(4) + payload)
    reader.feed_eof()
    writer = cast(Any, Writer)()
    checked = []
    class Socket:
        def getsockopt(self, *args: Any) -> bytes:
            assert not writer.data
            checked.append(True)
            return struct.pack('3i', 321, 0, 0)
    writer.get_extra_info = lambda name: Socket()
    async def connect(path: str) -> Any:
        return reader, writer
    monkeypatch.setattr(asyncio, 'open_unix_connection', connect, raising=False)
    monkeypatch.setattr(socket, 'SO_PEERCRED', 17, raising=False)
    result = await api.request_python_project_profile_broker(request)
    assert result.ok and checked == [True] and writer.data and writer.closed


async def test_nonroot_profile_cannot_trigger_metadata_readonly_recovery(
    api: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.skills.sandbox.python_metadata_preparation_client import (
        PythonMetadataPreparationFailure,
        PythonMetadataPreparationSandbox,
    )
    from tests.unit.skills.test_python_metadata_preparation_protocol import request

    planning = request().planning
    reader, writer = asyncio.StreamReader(), cast(Any, Writer)()
    reader.feed_eof()
    checks, connections = [], []
    class Socket:
        def getsockopt(self, *args: Any) -> bytes:
            checks.append(True)
            return struct.pack('3i', 321, 10001, 10001)
    writer.get_extra_info = lambda name: Socket()
    async def connect(path: str) -> Any:
        connections.append(path)
        return reader, writer
    monkeypatch.setattr(asyncio, 'open_unix_connection', connect, raising=False)
    monkeypatch.setattr(api, '_systemd_supported', lambda: True)
    monkeypatch.setattr(socket, 'SO_PEERCRED', 17, raising=False)
    with pytest.raises(PythonMetadataPreparationFailure) as caught:
        await PythonMetadataPreparationSandbox().plan(planning.invocation, planning.scope, planning.draft)
    assert caught.value.phase == 'profile' and caught.value.code == 'metadata_prepare_unavailable'
    assert len(connections) == len(checks) == 1 and not writer.data and writer.closed
