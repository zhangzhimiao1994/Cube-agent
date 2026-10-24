"""Mockwire uses real frames/parsers/tasks, not native Unix sockets or Go execution."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.skills.sandbox import go_project_protocol as protocol
from agent_hub.skills.sandbox.project import ProjectBrokerRequest
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.skills.sandbox.test_go_project_protocol import invocation, response

SOCKET = Path('/mock/go-broker.sock')


def test_go_client_exists() -> None:
    assert importlib.util.find_spec('agent_hub.skills.sandbox.go_project_client') is not None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = importlib.import_module('agent_hub.skills.sandbox.go_project_client')
    monkeypatch.setattr(module, '_systemd_supported', lambda: True)
    return module


def request(action: protocol.GoProjectAction = 'run_go_project', **changes: Any) -> protocol.GoProjectRequest:
    return protocol.GoProjectRequest(action=action, invocation=invocation(protocol, **changes))


class Writer:
    def __init__(self, *, fail: str = '') -> None:
        self.data = bytearray()
        self.closed = False
        self.waited = False
        self.fail = fail

    def write(self, data: bytes) -> None:
        self.data.extend(data)
        if self.fail == 'write':
            raise BrokenPipeError('/private/write secret')

    async def drain(self) -> None:
        if self.fail == 'drain':
            raise ConnectionResetError('/private/drain secret')

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True
        if self.fail == 'close':
            raise ConnectionResetError('/private/close secret')


def reader_for(raw: bytes, *, size: int | None = None) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data((len(raw) if size is None else size).to_bytes(4, 'big') + raw)
    reader.feed_eof()
    return reader


def connect_wire(
    monkeypatch: pytest.MonkeyPatch, raw: bytes, *, size: int | None = None, fail: str = '',
) -> Writer:
    writer = Writer(fail=fail)
    reader = reader_for(raw, size=size)

    async def connect(path: str) -> Any:
        assert path == SOCKET.as_posix()
        return reader, writer

    monkeypatch.setattr(asyncio, 'open_unix_connection', connect, raising=False)
    return writer


@pytest.mark.parametrize('action', ['run_go_project', 'inspect_go_project_environment'])
async def test_actions_keep_exact_v8_invocation_and_separate_outcomes(
    client: Any, monkeypatch: pytest.MonkeyPatch, action: protocol.GoProjectAction,
) -> None:
    call = request(action, output_limit_bytes=32 * 1024**2, source_kind='candidate')
    reply = response(protocol, call, stdout='{"passed":true,"environment":"untrusted"}')
    writer = connect_wire(monkeypatch, reply.model_dump_json().encode())
    sandbox = client.GoProjectSystemdSandbox(socket_path=SOCKET)
    if action == 'run_go_project':
        result = await sandbox.run(call.invocation)
        assert result == reply and result.environment is None
        assert result.result.stdout == '{"passed":true,"environment":"untrusted"}'
    else:
        assert await sandbox.inspect_environment(call.invocation) == call.invocation.toolchain
    assert json.loads(writer.data[4:]) == json.loads(call.model_dump_json())
    assert int.from_bytes(writer.data[:4], 'big') == len(writer.data) - 4
    assert writer.closed and writer.waited


@pytest.mark.parametrize('ok', [True, False])
@pytest.mark.parametrize('field,value', [
    ('execution_id', 'other'), ('recipe_sha256', '9' * 64),
    ('environment_input_sha256', '9' * 64), ('action', 'inspect_go_project_environment'),
    *[(field, UUID(int=99)) for field in ('tenant_id', 'user_id', 'run_id')],
    *[(field, 'other') for field in ('project_id', 'session_id')],
    ('actor_id', 'subagent'), ('actor_role', 'admin'),
    ('source_sha256', '9' * 64), ('contract_sha256', '9' * 64),
    *[(field, '9' * 64) for field in (
        'artifact_sha256', 'archive_sha256', 'tree_sha256', 'binary_sha256', 'gofmt_sha256',
    )],
    ('version', '1.27.1'), ('total_bytes', 999), ('file_count', 9),
])
async def test_wire_binds_every_identity_even_for_failed_replies(
    client: Any, monkeypatch: pytest.MonkeyPatch, ok: bool, field: str, value: object,
) -> None:
    call = request()
    payload = response(protocol, call, ok=ok).model_dump(mode='json')
    if field in type(call.invocation.scope).model_fields:
        payload['scope'][field] = str(value) if isinstance(value, UUID) else value
    elif field in type(call.invocation.toolchain).model_fields:
        payload['toolchain'][field] = value
    elif field == 'action' and ok:
        payload.pop('result')
        payload['environment'] = payload['toolchain']
        payload[field] = value
    else:
        payload[field] = value
    writer = connect_wire(monkeypatch, json.dumps(payload).encode())
    with pytest.raises(ValueError, match='^go_project_request_invalid$'):
        await client.request_go_project_broker(call, socket_path=SOCKET)
    assert writer.closed and writer.waited


@pytest.mark.parametrize('damage', [
    'json', 'legacy', 'version', 'duplicate', 'nested-duplicate', 'nan', 'encoding',
    'inspect-result', 'unmeasured-environment', 'empty', 'overlong', 'truncated',
])
async def test_invalid_or_disconnected_wire_fails_with_constant(
    client: Any, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    call = request('inspect_go_project_environment')
    raw = response(protocol, call).model_dump_json().encode()
    size = None
    if damage == 'json':
        raw = b'/private/secret not JSON'
    elif damage == 'legacy':
        raw = b'{"ok":true}'
    elif damage == 'version':
        raw = raw.replace(b'"version":8', b'"version":7', 1)
    elif damage == 'duplicate':
        raw = raw.replace(b'"version":8', b'"version":8,"version":8', 1)
    elif damage == 'nested-duplicate':
        raw = raw.replace(b'"file_count":5', b'"file_count":5,"file_count":5', 1)
    elif damage == 'nan':
        raw = raw.replace(b'"total_bytes":1000', b'"total_bytes":NaN', 1)
    elif damage == 'encoding':
        raw = b'\xff'
    elif damage in {'inspect-result', 'unmeasured-environment'}:
        payload = json.loads(raw)
        if damage == 'inspect-result':
            payload['result'] = {'exit_code': 0, 'stdout': '{}', 'stderr': '', 'timed_out': False}
            payload['environment'] = None
        else:
            payload['environment']['binary_sha256'] = '9' * 64
        raw = json.dumps(payload).encode()
    elif damage == 'empty':
        raw = b''
    elif damage == 'overlong':
        raw, size = b'', 512 * 1024 + 6 * 32 * 1024**2 + 1
    else:
        raw, size = b'{}', 100
    writer = connect_wire(monkeypatch, raw, size=size)
    with pytest.raises(ValueError, match='^go_project_request_invalid$'):
        await client.request_go_project_broker(call, socket_path=SOCKET)
    assert writer.closed and writer.waited


async def test_dynamic_wire_budget_counts_combined_utf8_and_json_escape_expansion(
    client: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = request()
    reply = response(protocol, call, stdout='\x01' * 64)
    raw = reply.model_dump_json().encode()
    limit = 512 * 1024 + 6 * 64
    writer = connect_wire(monkeypatch, raw + b' ' * (limit - len(raw)))
    assert await client.request_go_project_broker(
        call, socket_path=SOCKET, diagnostic_limit_bytes=64,
    ) == reply
    assert writer.closed and writer.waited
    writer = connect_wire(monkeypatch, b'', size=limit + 1)
    with pytest.raises(ValueError, match='^go_project_request_invalid$'):
        await client.request_go_project_broker(call, socket_path=SOCKET, diagnostic_limit_bytes=64)
    assert writer.closed and writer.waited
    payload = response(protocol, call, stdout='\u00e9' * 32).model_dump(mode='json')
    payload['result']['stderr'] = 'x'
    connect_wire(monkeypatch, json.dumps(payload).encode())
    with pytest.raises(ValueError, match='^go_project_request_invalid$'):
        await client.request_go_project_broker(call, socket_path=SOCKET, diagnostic_limit_bytes=64)


async def test_report_larger_than_legacy_one_mib_is_returned_opaquely(
    client: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = request(output_limit_bytes=32 * 1024**2)
    reply = response(protocol, call, stdout='x' * (2 * 1024**2))
    connect_wire(monkeypatch, reply.model_dump_json().encode())
    assert await client.request_go_project_broker(call, socket_path=SOCKET) == reply


@pytest.mark.parametrize('damage', [
    'legacy', 'install', 'scope', 'sdk', 'output', 'subclass', 'scope-type', 'sdk-type',
])
async def test_unvalidated_request_is_rejected_before_transport(
    client: Any, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    call: Any = request()
    inv = call.invocation
    if damage == 'legacy':
        call = ProjectBrokerRequest.model_construct(invocation=inv)
    elif damage == 'subclass':
        class OtherRequest(protocol.GoProjectRequest):
            pass
        call = OtherRequest(action=call.action, invocation=inv)
    else:
        changes = {
            'install': {'install_allowed': True},
            'scope': {'scope': inv.scope.model_copy(update={'actor_id': 'other'})},
            'sdk': {'toolchain': inv.toolchain.model_copy(update={'version': 'go1.26.1'})},
            'output': {'output_limit_bytes': True},
            'scope-type': {'scope': inv.scope.model_copy(update={'tenant_id': str(inv.tenant_id)})},
            'sdk-type': {'toolchain': inv.toolchain.model_copy(update={'total_bytes': True})},
        }[damage]
        call = call.model_copy(update={'invocation': inv.model_copy(update=changes)})

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('invalid request opened transport')

    monkeypatch.setattr(asyncio, 'open_unix_connection', forbidden, raising=False)
    with pytest.raises(ValueError, match='^go_project_request_invalid$'):
        await client.request_go_project_broker(call, socket_path=SOCKET)


@pytest.mark.parametrize('method', ['run', 'inspect_environment'])
@pytest.mark.parametrize('damage', ['mismatch', 'missing-scope', 'raw-scope', 'raw-sdk'])
async def test_public_job_rejects_forged_invocation_with_constant_before_io(
    client: Any, monkeypatch: pytest.MonkeyPatch, method: str, damage: str,
) -> None:
    inv = request().invocation
    updates: dict[str, dict[str, object]] = {
        'mismatch': {'scope': inv.scope.model_copy(update={'project_id': 'other'})},
        'missing-scope': {'scope': None},
        'raw-scope': {'scope': '/private/secret'},
        'raw-sdk': {'toolchain': '/private/secret'},
    }
    inv = inv.model_copy(update=updates[damage])

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('invalid invocation opened transport')

    monkeypatch.setattr(asyncio, 'open_unix_connection', forbidden, raising=False)
    with pytest.raises(ValueError, match='^go_project_request_invalid$'):
        await getattr(client.GoProjectSystemdSandbox(socket_path=SOCKET), method)(inv)


@pytest.mark.parametrize('limit', [True, 0, -1, '64', 32 * 1024**2 + 1])
async def test_invalid_diagnostic_budget_is_rejected_before_io(
    client: Any, monkeypatch: pytest.MonkeyPatch, limit: object,
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('invalid diagnostic budget opened transport')

    monkeypatch.setattr(asyncio, 'open_unix_connection', forbidden, raising=False)
    with pytest.raises(ValueError, match='^go_project_request_invalid$'):
        await client.request_go_project_broker(request(), socket_path=SOCKET, diagnostic_limit_bytes=limit)


@pytest.mark.parametrize('fail', ['connect', 'write', 'drain', 'read', 'close'])
async def test_os_failures_never_expose_socket_or_host_diagnostics(
    client: Any, monkeypatch: pytest.MonkeyPatch, fail: str,
) -> None:
    call = request()
    writer = connect_wire(monkeypatch, response(protocol, call).model_dump_json().encode(), fail=fail)
    if fail == 'connect':
        async def broken(path: str) -> Any:
            raise ConnectionRefusedError('/private/socket secret')
        monkeypatch.setattr(asyncio, 'open_unix_connection', broken)
    elif fail == 'read':
        async def broken_read(*args: Any) -> bytes:
            raise ConnectionResetError('/private/read secret')
        monkeypatch.setattr(client, '_read_frame', broken_read)
    with pytest.raises(OSError, match='^go_project_execution_unavailable$'):
        await client.request_go_project_broker(call, socket_path=SOCKET)
    if fail != 'connect':
        assert writer.closed and writer.waited


@pytest.mark.parametrize('action', ['run_go_project', 'inspect_go_project_environment'])
@pytest.mark.parametrize('code', ['go_project_scope_changed', 'go_project_cleanup_incomplete'])
async def test_root_failure_cannot_become_success(
    client: Any, monkeypatch: pytest.MonkeyPatch, action: protocol.GoProjectAction, code: str,
) -> None:
    call = request(action)
    reply = response(protocol, call, ok=False).model_copy(update={'error': code})
    connect_wire(monkeypatch, reply.model_dump_json().encode())
    method = 'run' if action == 'run_go_project' else 'inspect_environment'
    error = ProcessCleanupIncomplete if code.endswith('cleanup_incomplete') else OSError
    with pytest.raises(error, match=f'^{code}$'):
        await getattr(client.GoProjectSystemdSandbox(socket_path=SOCKET), method)(call.invocation)


@pytest.mark.parametrize('ack', [b'{"ok":false,"error":"unavailable"}', b'{"ok":true,"error":"x"}',
    b'{"ok":true,"result":{"exit_code":0,"stdout":"","stderr":"","timed_out":false}}',
    b'{"ok":true,"execution_id":"other"}', b'bad',
    b'{"ok":false,"ok":true}', b'{"ok":true,"ok":true}',
    b'{"ok":true,"error":"go_project_cleanup_incomplete","error":null}',
    b'{"ok":true,"result":{},"result":null}',
    b'{"ok":true,"error":null,"err\\u006fr":null}',
    b'{"ok":true,"result":NaN}', b'{"ok":1}', b'[]'])
async def test_termination_requires_empty_success_ack(
    client: Any, monkeypatch: pytest.MonkeyPatch, ack: bytes,
) -> None:
    writer = connect_wire(monkeypatch, ack)
    with pytest.raises(ProcessCleanupIncomplete, match='^go_project_cleanup_incomplete$'):
        await client.GoProjectSystemdSandbox(socket_path=SOCKET).terminate('go_1')
    assert writer.closed and writer.waited


@pytest.mark.parametrize('ack', [b'{"ok":true}', b'{"ok":true,"result":null,"error":null}'])
async def test_strict_termination_accepts_only_empty_success_outcome(
    client: Any, monkeypatch: pytest.MonkeyPatch, ack: bytes,
) -> None:
    writer = connect_wire(monkeypatch, ack)
    await client.GoProjectSystemdSandbox(socket_path=SOCKET).terminate('go_1')
    assert writer.closed and writer.waited
    assert json.loads(writer.data[4:]) == {
        'version': 2, 'action': 'terminate_project', 'execution_id': 'go_1',
    }


@pytest.mark.parametrize('action', ['run_go_project', 'inspect_go_project_environment'])
@pytest.mark.parametrize('damage', ['eof', 'json', 'reset', 'binding', 'overlong', 'write', 'drain', 'close'])
@pytest.mark.parametrize('ack_ok', [True, False])
async def test_submitted_failure_requires_confirmed_termination_before_propagating(
    client: Any, monkeypatch: pytest.MonkeyPatch, action: protocol.GoProjectAction, damage: str, ack_ok: bool,
) -> None:
    call = request(action)
    payload = response(protocol, call).model_dump(mode='json')
    if damage == 'binding':
        payload['execution_id'] = 'other'
    raw = b'private bad JSON' if damage == 'json' else json.dumps(payload).encode()
    operation_reader = reader_for(raw)
    if damage == 'eof':
        operation_reader = reader_for(b'', size=100)
    elif damage == 'overlong':
        operation_reader = reader_for(b'', size=512 * 1024 + 6 * 32 * 1024**2 + 1)
    elif damage == 'reset':
        operation_reader.set_exception(ConnectionResetError('/private/socket reset'))
    operation_writer = Writer(fail=damage)
    termination_writer = Writer()
    termination_reader = reader_for(b'{"ok":true}' if ack_ok else b'{"ok":false}')
    connections = 0

    async def connect(path: str) -> Any:
        nonlocal connections
        assert path == SOCKET.as_posix()
        connections += 1
        assert connections <= 2
        return ((operation_reader, operation_writer) if connections == 1
                else (termination_reader, termination_writer))

    monkeypatch.setattr(asyncio, 'open_unix_connection', connect, raising=False)
    method = 'run' if action == 'run_go_project' else 'inspect_environment'
    communication_error = damage in {'reset', 'write', 'drain', 'close'}
    error = (OSError if communication_error else ValueError) if ack_ok else ProcessCleanupIncomplete
    code = ('go_project_execution_unavailable' if communication_error else 'go_project_request_invalid'
            ) if ack_ok else 'go_project_cleanup_incomplete'
    with pytest.raises(error, match=f'^{code}$'):
        await getattr(client.GoProjectSystemdSandbox(socket_path=SOCKET), method)(call.invocation)
    assert connections == 2
    assert json.loads(termination_writer.data[4:]) == {
        'version': 2, 'action': 'terminate_project', 'execution_id': 'go_1',
    }
    assert operation_writer.closed and operation_writer.waited
    assert termination_writer.closed and termination_writer.waited


async def test_failure_before_submission_does_not_terminate_an_unrelated_job(
    client: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = 0

    async def connect(path: str) -> Any:
        nonlocal connections
        connections += 1
        raise ConnectionRefusedError('/private/socket')

    monkeypatch.setattr(asyncio, 'open_unix_connection', connect, raising=False)
    with pytest.raises(OSError, match='^go_project_execution_unavailable$'):
        await client.GoProjectSystemdSandbox(socket_path=SOCKET).run(request().invocation)
    assert connections == 1


@pytest.mark.parametrize('size', [0, 100, 2**32 - 1])
async def test_termination_broken_frame_is_cleanup_failure(
    client: Any, monkeypatch: pytest.MonkeyPatch, size: int,
) -> None:
    writer = connect_wire(monkeypatch, b'', size=size)
    with pytest.raises(ProcessCleanupIncomplete, match='^go_project_cleanup_incomplete$'):
        await client.GoProjectSystemdSandbox(socket_path=SOCKET).terminate('go_1')
    assert writer.closed and writer.waited


@pytest.mark.parametrize('error', [
    ConnectionResetError('/private/secret'), TimeoutError('secret'), RuntimeError('secret'),
])
async def test_termination_transport_failure_is_cleanup_failure(
    client: Any, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    async def broken(path: str) -> Any:
        raise error

    monkeypatch.setattr(asyncio, 'open_unix_connection', broken, raising=False)
    with pytest.raises(ProcessCleanupIncomplete, match='^go_project_cleanup_incomplete$'):
        await client.GoProjectSystemdSandbox(socket_path=SOCKET).terminate('go_1')


async def test_late_success_during_termination_cannot_erase_cancellation(
    client: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = request()
    sent, replied = asyncio.Event(), asyncio.Event()
    reader = asyncio.StreamReader()

    class OperationWriter(Writer):
        async def drain(self) -> None:
            sent.set()

        async def wait_closed(self) -> None:
            self.waited = True
            replied.set()

    writer = OperationWriter()
    ack_writer = Writer()
    connections = 0

    async def connect(path: str) -> Any:
        nonlocal connections
        connections += 1
        if connections == 1:
            return reader, writer
        raw = response(protocol, call).model_dump_json().encode()
        reader.feed_data(len(raw).to_bytes(4, 'big') + raw)
        reader.feed_eof()
        await replied.wait()
        return reader_for(b'{"ok":true}'), ack_writer

    monkeypatch.setattr(asyncio, 'open_unix_connection', connect, raising=False)
    task = asyncio.create_task(client.GoProjectSystemdSandbox(socket_path=SOCKET).run(call.invocation))
    try:
        await asyncio.wait_for(sent.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert writer.closed and writer.waited and ack_writer.closed and ack_writer.waited
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('action', ['run_go_project', 'inspect_go_project_environment'])
@pytest.mark.parametrize('interrupt', ['cancel', 'timeout', 'cleanup-failure'])
async def test_interruption_terminates_same_job_and_drains_both_connections(
    client: Any, monkeypatch: pytest.MonkeyPatch, action: protocol.GoProjectAction, interrupt: str,
) -> None:
    sent, closing, release_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
    call = request(action)

    class OperationWriter(Writer):
        async def drain(self) -> None:
            sent.set()

        async def wait_closed(self) -> None:
            closing.set()
            await release_close.wait()
            self.waited = True

    operation_writer, terminate_writer = OperationWriter(), Writer()
    operation_reader = asyncio.StreamReader()
    ack = b'{"ok":true}' if interrupt != 'cleanup-failure' else b'{"ok":false}'
    terminate_reader = reader_for(ack)
    connections = 0

    async def connect(path: str) -> Any:
        nonlocal connections
        assert path == SOCKET.as_posix()
        connections += 1
        assert connections <= 2
        return ((operation_reader, operation_writer) if connections == 1
                else (terminate_reader, terminate_writer))

    monkeypatch.setattr(asyncio, 'open_unix_connection', connect, raising=False)
    original_timeout = asyncio.timeout
    budgets: list[float] = []

    def timeout(seconds: float) -> Any:
        budgets.append(seconds)
        return original_timeout(0.02 if interrupt == 'timeout' and len(budgets) == 1 else seconds)

    monkeypatch.setattr(asyncio, 'timeout', timeout)
    sandbox = client.GoProjectSystemdSandbox(socket_path=SOCKET)
    method = sandbox.run if action == 'run_go_project' else sandbox.inspect_environment
    task = asyncio.create_task(method(call.invocation))
    try:
        await asyncio.wait_for(sent.wait(), 1)
        if interrupt != 'timeout':
            task.cancel()
        await asyncio.wait_for(closing.wait(), 1)
        assert terminate_writer.closed and terminate_writer.waited
        assert operation_writer.closed and not operation_writer.waited and not task.done()
        if interrupt == 'cancel':
            task.cancel()
        release_close.set()
        expected = {'cancel': asyncio.CancelledError, 'timeout': TimeoutError,
                    'cleanup-failure': ProcessCleanupIncomplete}[interrupt]
        with pytest.raises(expected):
            await task
        assert operation_writer.waited and connections == 2
        assert json.loads(operation_writer.data[4:]) == json.loads(call.model_dump_json())
        assert json.loads(terminate_writer.data[4:]) == {
            'version': 2, 'action': 'terminate_project', 'execution_id': 'go_1',
        }
        assert budgets == [call.invocation.timeout_seconds + 22.0]
    finally:
        release_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('stalled', ['operation', 'termination'])
@pytest.mark.parametrize('stubborn', [False, True])
async def test_stalled_writer_close_is_bounded_and_never_reports_cancel_success(
    client: Any, monkeypatch: pytest.MonkeyPatch, stalled: str, stubborn: bool,
) -> None:
    sent, closing, release_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closing_tasks: set[asyncio.Task[Any]] = set()
    aborted: list[bool] = []

    class Transport:
        def abort(self) -> None:
            aborted.append(True)

    class StalledWriter(Writer):
        transport = Transport()

        async def wait_closed(self) -> None:
            current = asyncio.current_task()
            assert current is not None
            closing_tasks.add(current)
            closing.set()
            try:
                await release_close.wait()
            except asyncio.CancelledError:
                if not stubborn:
                    raise
                await release_close.wait()
            self.waited = True

    class OperationWriter(Writer):
        async def drain(self) -> None:
            sent.set()

    operation_writer = StalledWriter() if stalled == 'operation' else OperationWriter()
    # A stalled operation writer still records when its request was sent.
    if stalled == 'operation':
        async def drain() -> None:
            sent.set()
        monkeypatch.setattr(operation_writer, 'drain', drain)
    terminate_writer = StalledWriter() if stalled == 'termination' else Writer()
    connections = 0

    async def connect(path: str) -> Any:
        nonlocal connections
        connections += 1
        assert connections <= 2
        return ((asyncio.StreamReader(), operation_writer) if connections == 1
                else (reader_for(b'{"ok":true}'), terminate_writer))

    monkeypatch.setattr(asyncio, 'open_unix_connection', connect, raising=False)
    real_wait = asyncio.wait
    limits: list[float] = []

    async def wait(tasks: Any, *, timeout: float, **kwargs: Any) -> Any:
        limits.append(timeout)
        return await real_wait(tasks, timeout=timeout * 0.001, **kwargs)

    monkeypatch.setattr(asyncio, 'wait', wait)
    task = asyncio.create_task(client.GoProjectSystemdSandbox(socket_path=SOCKET).run(request().invocation))
    try:
        await asyncio.wait_for(sent.wait(), 1)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel()
        done, _ = await real_wait({task}, timeout=0.2)
        assert task in done, 'cleanup waited indefinitely for writer.wait_closed'
        with pytest.raises(ProcessCleanupIncomplete, match='^go_project_cleanup_incomplete$'):
            await task
        assert 22.0 in limits
        if stalled == 'operation':
            assert 2.0 in limits and aborted
    finally:
        release_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *closing_tasks, return_exceptions=True)


@pytest.mark.parametrize('error', [OSError('private'), ValueError('private'), RuntimeError('private')])
async def test_unexpected_pending_failure_is_cleanup_incomplete(
    client: Any, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    async def terminated(execution_id: str) -> None:
        assert execution_id == 'go_1'

    async def failed() -> Any:
        raise error

    sandbox = client.GoProjectSystemdSandbox(socket_path=SOCKET)
    monkeypatch.setattr(sandbox, 'terminate', terminated)
    pending = asyncio.create_task(failed())
    await asyncio.wait({pending})
    with pytest.raises(ProcessCleanupIncomplete, match='^go_project_cleanup_incomplete$'):
        await sandbox._terminate_and_drain('go_1', pending)


async def test_direct_termination_has_hard_bound_even_if_transport_ignores_cancel(
    client: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    operations: set[asyncio.Task[Any]] = set()

    async def stuck(*args: Any, **kwargs: Any) -> Any:
        current = asyncio.current_task()
        assert current is not None
        operations.add(current)
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    real_wait = asyncio.wait
    limits: list[float] = []

    async def wait(tasks: Any, *, timeout: float, **kwargs: Any) -> Any:
        limits.append(timeout)
        return await real_wait(tasks, timeout=0.01, **kwargs)

    monkeypatch.setattr(client, '_request_go_project_termination', stuck)
    monkeypatch.setattr(asyncio, 'wait', wait)
    task = asyncio.create_task(client.GoProjectSystemdSandbox(socket_path=SOCKET).terminate('go_1'))
    try:
        await asyncio.wait_for(started.wait(), 1)
        done, _ = await real_wait({task}, timeout=0.2)
        assert task in done, 'termination relied on cooperative timeout cancellation'
        with pytest.raises(ProcessCleanupIncomplete, match='^go_project_cleanup_incomplete$'):
            await task
        assert limits == [22.0]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *operations, return_exceptions=True)


@pytest.mark.parametrize('termination_elapsed,remaining', [(16.0, 6.0), (21.0, 1.0)])
async def test_drain_uses_only_time_remaining_after_termination(
    client: Any, monkeypatch: pytest.MonkeyPatch, termination_elapsed: float, remaining: float,
) -> None:
    elapsed = 0.0
    limits: list[float] = []
    real_wait = asyncio.wait

    async def terminated(execution_id: str) -> None:
        nonlocal elapsed
        elapsed = termination_elapsed

    async def wait(tasks: Any, *, timeout: float, **kwargs: Any) -> Any:
        limits.append(timeout)
        return await real_wait(tasks, timeout=timeout, **kwargs)

    sandbox = client.GoProjectSystemdSandbox(socket_path=SOCKET)
    monkeypatch.setattr(sandbox, 'terminate', terminated)
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: SimpleNamespace(time=lambda: 100 + elapsed))
    monkeypatch.setattr(asyncio, 'wait', wait)
    pending = asyncio.create_task(asyncio.Event().wait())
    await sandbox._terminate_and_drain('go_1', pending)
    assert limits == [remaining] and pending.cancelled()


async def test_cancelled_termination_still_validates_negative_ack(
    client: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    reader = asyncio.StreamReader()
    writer = Writer()

    async def connect(path: str) -> Any:
        started.set()
        await release.wait()
        return reader, writer

    monkeypatch.setattr(asyncio, 'open_unix_connection', connect, raising=False)
    task = asyncio.create_task(client.GoProjectSystemdSandbox(socket_path=SOCKET).terminate('go_1'))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        raw = b'{"ok":false}'
        reader.feed_data(len(raw).to_bytes(4, 'big') + raw)
        reader.feed_eof()
        release.set()
        with pytest.raises(ProcessCleanupIncomplete, match='^go_project_cleanup_incomplete$'):
            await task
        assert writer.closed and writer.waited
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_missing_unix_connector_is_explicitly_unsupported(
    client: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(asyncio, 'open_unix_connection', raising=False)
    with pytest.raises(OSError, match='^go_project_unsupported$'):
        await client.request_go_project_broker(request(), socket_path=SOCKET)


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows unsupported behavior only')
async def test_native_windows_rejects_all_entry_points_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module('agent_hub.skills.sandbox.go_project_client')

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('native Windows attempted Unix transport')

    monkeypatch.setattr(asyncio, 'open_unix_connection', forbidden, raising=False)
    sandbox = module.GoProjectSystemdSandbox(socket_path=SOCKET)
    for operation in (
        module.request_go_project_broker(request(), socket_path=SOCKET),
        sandbox.run(request().invocation), sandbox.inspect_environment(request().invocation),
        sandbox.terminate('go_1'),
    ):
        with pytest.raises(OSError, match='^go_project_unsupported$'):
            await operation
