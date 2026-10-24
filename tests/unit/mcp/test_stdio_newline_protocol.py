from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.mcp.client import McpProtocolError, McpResponseTooLarge, StdioMcpClient
from agent_hub.mcp.types import McpServerDefinition, McpTransportKind

_PEER = r'''
import json
import pathlib
import sys

mode, trace_name = sys.argv[1:]
trace = pathlib.Path(trace_name)

def record(event):
    with trace.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(event) + '\n')

def read():
    line = sys.stdin.buffer.readline()
    if not line:
        record({'closed': True})
        return None
    # Only this deliberately nonstandard peer accepts the old local codec.
    if mode == 'legacy' and line.startswith(b'Content-Length:'):
        length = int(line.split(b':', 1)[1])
        assert sys.stdin.buffer.readline() == b'\r\n'
        return json.loads(sys.stdin.buffer.read(length))
    assert line.endswith(b'\n'), 'request must end with newline'
    return json.loads(line.decode('utf-8'))

def write_message(payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if mode == 'legacy':
        sys.stdout.buffer.write(f'Content-Length: {len(raw)}\r\n\r\n'.encode() + raw)
    else:
        sys.stdout.buffer.write(raw + b'\n')
    sys.stdout.buffer.flush()

def send(payload):
    stage = 'initialize' if payload.get('id') == 1 else 'tool'
    variant = mode.removeprefix(stage + '-') if mode.startswith(stage + '-') else ''
    if mode == 'both-notifications' or variant in {'notifications', 'notification-flood'}:
        count = 32 if mode == 'both-notifications' else 33 if variant == 'notification-flood' else 2
        for _ in range(count):
            write_message({'jsonrpc': '2.0', 'method': 'notifications/message',
                           'params': {'level': 'info', 'data': 'peer notification'}})
    if variant == 'wrong-id':
        payload['id'] = 2 if stage == 'initialize' else 1
    elif variant == 'bool-id':
        payload['id'] = True
    elif variant == 'float-id':
        payload['id'] = float(payload['id'])
    elif variant == 'string-id':
        payload['id'] = str(payload['id'])
    elif variant == 'missing-id':
        payload.pop('id')
    elif variant == 'wrong-version':
        payload['jsonrpc'] = '1.0'
    elif variant == 'missing-version':
        payload.pop('jsonrpc')
    elif variant == 'server-request':
        payload['method'] = 'sampling/createMessage'
        payload['params'] = {}
    elif variant == 'fake-notification':
        payload.pop('id')
        payload['method'] = 'notifications/message'
    elif variant == 'invalid-notification':
        write_message({'jsonrpc': '2.0', 'method': 'notifications/message', 'params': []})
    elif variant == 'both-result-error':
        payload['error'] = {'code': -32603, 'message': 'must not masquerade as an error'}
    elif variant == 'invalid-error':
        payload.pop('result')
        payload['error'] = {'code': True, 'message': 'must not masquerade as an error'}
    elif variant == 'wrong-id-error':
        payload.pop('result')
        payload['id'] = 9
        payload['error'] = {'code': -32603, 'message': 'must not trust wrong-id error'}
    write_message(payload)

initialized = False
while True:
    message = read()
    if message is None:
        break
    record(message)
    method = message['method']
    if method == 'notifications/initialized':
        assert 'id' not in message
        initialized = True
        continue
    if method == 'initialize':
        assert message['params']['protocolVersion'] == '2025-06-18'
        if mode == 'truncated':
            sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":1,"result":{}}')
            sys.stdout.buffer.flush()
            break
        if mode == 'invalid-json':
            sys.stdout.buffer.write(b'not-json\n')
            sys.stdout.buffer.flush()
            continue
        if mode == 'oversized':
            sys.stdout.buffer.write(b'x' * 2_000_001 + b'\n')
            sys.stdout.buffer.flush()
            continue
        result = {
            'protocolVersion': '2025-06-18',
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'newline-peer', 'version': '1.0.0'},
        }
    else:
        assert initialized, 'initialized notification must precede tool requests'
        if method == 'tools/list':
            result = {'tools': [{'name': 'echo', 'inputSchema': {'type': 'object'}}]}
        elif method == 'tools/call':
            if mode in {'wait', 'notification-wait'}:
                if mode == 'notification-wait':
                    write_message({'jsonrpc': '2.0', 'method': 'notifications/message',
                                   'params': {'level': 'info', 'data': 'waiting'}})
                    record({'notification_sent': True})
                assert sys.stdin.buffer.read() == b''
                record({'closed': True})
                break
            if message['params']['name'] == 'rpc_error':
                send({'jsonrpc': '2.0', 'id': message['id'],
                      'error': {'code': -32602, 'message': 'controlled invalid arguments'}})
                continue
            result = {'content': [{'type': 'text', 'text': message['params']['arguments']['text']}]}
        else:
            raise AssertionError('unexpected method: ' + method)
    send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
'''


class _Peer:
    def __init__(self, root: Path) -> None:
        self.script = root / "standard_peer.py"
        self.script.write_text(_PEER, encoding="utf-8")
        self.trace = root / "trace.jsonl"
        self.processes: list[asyncio.subprocess.Process] = []

    def server(self, mode: str = "standard") -> McpServerDefinition:
        return McpServerDefinition(
            tenant_id=UUID("66666666-6666-4666-8666-666666666666"),
            id="newline-peer",
            transport=McpTransportKind.STDIO,
            command=sys.executable,
            args=("-I", "-u", str(self.script), mode, str(self.trace)),
            executable_allowlist=(sys.executable,),
        )

    async def spawn(
        self, program: str, *args: str, stdin: int, stdout: int, stderr: int
    ) -> asyncio.subprocess.Process:
        process = await asyncio.create_subprocess_exec(
            program, *args, stdin=stdin, stdout=stdout, stderr=stderr
        )
        self.processes.append(process)
        return process

    def events(self) -> list[dict[str, object]]:
        if not self.trace.exists():
            return []
        return [json.loads(line) for line in self.trace.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
async def peer(tmp_path: Path) -> AsyncIterator[_Peer]:
    instance = _Peer(tmp_path)
    try:
        yield instance
    finally:
        for process in instance.processes:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await asyncio.wait_for(process.communicate(), timeout=5)


async def test_standard_peer_initialize_discover_call_and_close(peer: _Peer) -> None:
    client = StdioMcpClient(process_factory=peer.spawn)
    before = asyncio.all_tasks()
    async with asyncio.timeout(10):
        assert await client.health(peer.server()) == "healthy"
        tools = await client.discover_tools(peer.server())
        # Escaped control characters must remain inside one physical UTF-8 line.
        text = "hello\n\r\t\"\\\u4e2d\u6587"
        result = await client.invoke(peer.server(), "echo", {"text": text})
    assert [tool.name for tool in tools] == ["echo"]
    assert result.content == {"content": [{"type": "text", "text": text}]}
    assert [event["method"] for event in peer.events() if "method" in event] == [
        "initialize", "initialize", "notifications/initialized", "tools/list",
        "initialize", "notifications/initialized", "tools/call",
    ]
    assert sum(event.get("closed") is True for event in peer.events()) == 3
    assert len(peer.processes) == 3
    assert all(process.returncode == 0 for process in peer.processes)
    assert not (asyncio.all_tasks() - before)


async def test_standard_peer_large_tool_result_is_not_limited_to_stream_reader_line(peer: _Peer) -> None:
    text = "x" * 100_000
    async with asyncio.timeout(10):
        result = await StdioMcpClient(process_factory=peer.spawn).invoke(
            peer.server(), "echo", {"text": text}
        )
    assert result.content == {"content": [{"type": "text", "text": text}]}
    assert peer.processes[0].returncode == 0


async def test_standard_peer_jsonrpc_error_survives_cleanup(peer: _Peer) -> None:
    before = asyncio.all_tasks()
    with pytest.raises(RuntimeError, match="^controlled invalid arguments$"):
        async with asyncio.timeout(10):
            await StdioMcpClient(process_factory=peer.spawn).invoke(peer.server(), "rpc_error", {})
    assert peer.events()[-1] == {"closed": True}
    assert peer.processes[0].returncode == 0
    assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize(
    ("mode", "error", "message"),
    [
        ("legacy", RuntimeError, "Content-Length framing is not supported"),
        ("truncated", RuntimeError, "must end with newline"),
        ("invalid-json", RuntimeError, "not valid JSON"),
        ("oversized", McpResponseTooLarge, "stdio response is too large"),
    ],
)
async def test_nonstandard_or_oversized_peer_fails_and_is_reaped(
    peer: _Peer, mode: str, error: type[Exception], message: str
) -> None:
    before = asyncio.all_tasks()
    with pytest.raises(error, match=message):
        async with asyncio.timeout(10):
            await StdioMcpClient(process_factory=peer.spawn).health(peer.server(mode))
    assert peer.processes[0].returncode is not None
    assert not (asyncio.all_tasks() - before)


async def test_cancelling_inflight_standard_call_closes_and_reaps_peer(peer: _Peer) -> None:
    before = asyncio.all_tasks()
    request = asyncio.create_task(
        StdioMcpClient(process_factory=peer.spawn).invoke(peer.server("wait"), "echo", {})
    )
    try:
        async with asyncio.timeout(10):
            while not any(event.get("method") == "tools/call" for event in peer.events()):
                if request.done():
                    request.result()
                    raise AssertionError("peer closed before tools/call")
                await asyncio.sleep(0.01)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        assert peer.events()[-1] == {"closed": True}
        assert peer.processes[0].returncode == 0
        assert not (asyncio.all_tasks() - before)
    finally:
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("stage", ["initialize", "tool"])
async def test_standard_notifications_are_skipped_before_the_matching_response(
    peer: _Peer, stage: str
) -> None:
    async with asyncio.timeout(10):
        result = await StdioMcpClient(process_factory=peer.spawn).invoke(
            peer.server(stage + "-notifications"), "echo", {"text": "matching result"}
        )
    assert result.content == {"content": [{"type": "text", "text": "matching result"}]}
    assert peer.processes[0].returncode == 0
    assert peer.events()[-1] == {"closed": True}


async def test_notification_budget_resets_for_the_initialize_and_tool_response(peer: _Peer) -> None:
    async with asyncio.timeout(10):
        result = await StdioMcpClient(process_factory=peer.spawn).invoke(
            peer.server("both-notifications"), "echo", {"text": "both responses"}
        )
    assert result.content == {"content": [{"type": "text", "text": "both responses"}]}
    assert peer.processes[0].returncode == 0


@pytest.mark.parametrize("stage", ["initialize", "tool"])
@pytest.mark.parametrize(
    "variant",
    [
        "wrong-id", "bool-id", "float-id", "string-id", "missing-id",
        "wrong-version", "missing-version", "server-request", "fake-notification",
        "invalid-notification", "both-result-error", "invalid-error", "wrong-id-error",
    ],
)
async def test_unmatched_or_invalid_envelopes_fail_closed_and_reap_the_peer(
    peer: _Peer, stage: str, variant: str
) -> None:
    before = asyncio.all_tasks()
    with pytest.raises(McpProtocolError):
        async with asyncio.timeout(10):
            await StdioMcpClient(process_factory=peer.spawn).invoke(
                peer.server(stage + "-" + variant), "echo", {"text": "untrusted result"}
            )
    methods = [event["method"] for event in peer.events() if "method" in event]
    assert methods == (["initialize"] if stage == "initialize" else [
        "initialize", "notifications/initialized", "tools/call",
    ])
    assert peer.events()[-1] == {"closed": True}
    assert peer.processes[0].returncode == 0
    assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize("stage", ["initialize", "tool"])
async def test_notification_flood_is_bounded_and_reaps_the_peer(peer: _Peer, stage: str) -> None:
    before = asyncio.all_tasks()
    with pytest.raises(McpProtocolError, match="notification limit exceeded"):
        async with asyncio.timeout(10):
            await StdioMcpClient(process_factory=peer.spawn).invoke(
                peer.server(stage + "-notification-flood"), "echo", {"text": "bounded"}
            )
    assert peer.processes[0].returncode == 0
    assert not (asyncio.all_tasks() - before)


async def test_waiting_after_a_notification_preserves_caller_timeout_and_cleanup(peer: _Peer) -> None:
    before = asyncio.all_tasks()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            StdioMcpClient(process_factory=peer.spawn).invoke(
                peer.server("notification-wait"), "echo", {}
            ),
            timeout=1,
        )
    assert {"notification_sent": True} in peer.events()
    assert peer.events()[-1] == {"closed": True}
    assert peer.processes[0].returncode == 0
    assert not (asyncio.all_tasks() - before)
