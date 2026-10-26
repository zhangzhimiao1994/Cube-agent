"""Real broker dispatch with framed in-memory IO; not native execution evidence."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent_hub.skills.sandbox import python_dependency_inspection, python_dependency_protocol
from tests.unit.skills.sandbox.test_python_project_profile_wire import serve
from tests.unit.skills.test_python_dependency_protocol import observation, request


async def test_v8_dispatches_validated_request_after_peer_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[Any] = []

    async def inspect(value: Any, *, policy: Any) -> Any:
        received.append(value)
        assert policy.allowed_uid == 10001
        return python_dependency_protocol.PythonDependencyClosureResponse(
            scope=python_dependency_protocol.PythonProjectProfileScope.for_invocation(value.invocation), ok=True,
            observation=python_dependency_protocol.PythonDependencyClosureObservation(**observation()),
        )

    monkeypatch.setattr(python_dependency_inspection, 'inspect_python_dependency_closure', inspect)
    expected = request()
    result = await serve(monkeypatch, expected.model_dump(mode='json'))
    response = python_dependency_protocol.PythonDependencyClosureResponse.model_validate_json(
        json.dumps(result), strict=True)
    response.require_for(expected)
    assert response.ok and len(received) == 1
    assert type(received[0]) is python_dependency_protocol.PythonDependencyClosureRequest


@pytest.mark.parametrize('uid', [0, 7])
async def test_v8_unauthorized_peer_never_reaches_root_worker(monkeypatch: pytest.MonkeyPatch, uid: int) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('unauthorized Root inspection')

    monkeypatch.setattr(python_dependency_inspection, 'inspect_python_dependency_closure', forbidden)
    result = await serve(monkeypatch, request().model_dump(mode='json'), uid=uid)
    assert result == {'ok': False, 'result': None, 'error': 'python_dependency_request_invalid'}


@pytest.mark.parametrize('version', [8, '8', 8.0, True, 6, 7])
async def test_v8_invalid_cannot_downgrade_or_echo_private_fields(
    monkeypatch: pytest.MonkeyPatch, version: object,
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('invalid Root inspection')

    monkeypatch.setattr(python_dependency_inspection, 'inspect_python_dependency_closure', forbidden)
    value = request().model_dump(mode='json') | {'version': version, 'host_path': '/private/DO-NOT-ECHO'}
    result = await serve(monkeypatch, value)
    assert result['ok'] is False and 'DO-NOT-ECHO' not in json.dumps(result)


async def test_v8_readonly_cancellation_is_not_project_termination(monkeypatch: pytest.MonkeyPatch) -> None:
    async def cancelled(*args: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(python_dependency_inspection, 'inspect_python_dependency_closure', cancelled)
    with pytest.raises(asyncio.CancelledError):
        await serve(monkeypatch, request().model_dump(mode='json'))


async def test_v8_worker_failure_is_finite_not_raw_host_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failed(*args: Any, **kwargs: Any) -> Any:
        raise OSError('/private/DO-NOT-ECHO')

    monkeypatch.setattr(python_dependency_inspection, 'inspect_python_dependency_closure', failed)
    result = await serve(monkeypatch, request().model_dump(mode='json'))
    assert result['ok'] is False and result['error'] == 'python_dependency_request_invalid'
    assert 'DO-NOT-ECHO' not in json.dumps(result)
