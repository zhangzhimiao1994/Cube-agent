from __future__ import annotations

import asyncio
import json
from typing import Any

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
    reader, writer = asyncio.StreamReader(), Writer()
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
