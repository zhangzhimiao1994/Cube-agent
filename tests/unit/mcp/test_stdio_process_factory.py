from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.mcp.client import InMemoryMcpClient, StdioMcpClient
from agent_hub.mcp.types import McpServerDefinition, McpTransportKind


class _FailingFactory:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def __bool__(self) -> bool:
        return False

    async def __call__(
        self, program: str, *args: str, stdin: int, stdout: int, stderr: int
    ) -> asyncio.subprocess.Process:
        self.calls += 1
        raise self.error


class _SpyFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], int, int, int]] = []
        self.processes: list[asyncio.subprocess.Process] = []

    async def __call__(
        self, program: str, *args: str, stdin: int, stdout: int, stderr: int
    ) -> asyncio.subprocess.Process:
        self.calls.append((program, args, stdin, stdout, stderr))
        process = await asyncio.create_subprocess_exec(
            program, *args, stdin=stdin, stdout=stdout, stderr=stderr
        )
        self.processes.append(process)
        return process


def _server(*args: str) -> McpServerDefinition:
    return McpServerDefinition(
        tenant_id=UUID("66666666-6666-4666-8666-666666666666"),
        id="factory-peer",
        transport=McpTransportKind.STDIO,
        command=sys.executable,
        args=args,
        executable_allowlist=(sys.executable,),
    )


def test_constructor_accepts_process_factory_without_starting_it() -> None:
    factory = _FailingFactory(RuntimeError("must not start during construction"))

    assert isinstance(StdioMcpClient(process_factory=factory), StdioMcpClient)
    assert factory.calls == 0


def test_transport_and_process_factory_are_rejected_before_starting() -> None:
    factory = _FailingFactory(RuntimeError("must not start during construction"))
    transport = InMemoryMcpClient(tools=())

    with pytest.raises(ValueError, match="transport.*process_factory"):
        StdioMcpClient(transport, process_factory=factory)
    assert factory.calls == 0


async def test_injected_factory_runs_real_peer_with_exact_argv_and_pipes(tmp_path: Path) -> None:
    script = tmp_path / "factory peer.py"
    script.write_text(
        r"""import json
import sys

assert sys.argv[1:] == ['argument with spaces', '--factory-check']
header = sys.stdin.buffer.readline()
length = int(header.decode('ascii').split(':', 1)[1].strip())
assert sys.stdin.buffer.readline() == b'\r\n'
request = json.loads(sys.stdin.buffer.read(length))
assert request['method'] == 'initialize'
reply = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {}}).encode()
sys.stdout.buffer.write(f'Content-Length: {len(reply)}\r\n\r\n'.encode() + reply)
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
""",
        encoding="utf-8",
    )
    args = ("-I", "-u", str(script), "argument with spaces", "--factory-check")
    factory = _SpyFactory()
    before = asyncio.all_tasks()
    try:
        client = StdioMcpClient(process_factory=factory)
        assert await asyncio.wait_for(client.health(_server(*args)), timeout=10) == "healthy"
        assert factory.calls == [
            (
                sys.executable,
                args,
                asyncio.subprocess.PIPE,
                asyncio.subprocess.PIPE,
                asyncio.subprocess.DEVNULL,
            )
        ]
        assert len(factory.processes) == 1
        assert factory.processes[0].returncode == 0
        assert not (asyncio.all_tasks() - before)
    finally:
        for process in factory.processes:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await asyncio.wait_for(process.communicate(), timeout=5)


@pytest.mark.parametrize("error", [OSError("factory unavailable"), RuntimeError("factory refused")])
async def test_factory_failure_propagates_once_without_default_spawn(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    default_calls = 0

    async def default_spawn(
        program: str, *args: str, stdin: int, stdout: int, stderr: int
    ) -> asyncio.subprocess.Process:
        nonlocal default_calls
        default_calls += 1
        raise AssertionError("injected factory must not fall back to default spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", default_spawn)
    factory = _FailingFactory(error)
    client = StdioMcpClient(process_factory=factory)
    before = asyncio.all_tasks()

    with pytest.raises(type(error)) as caught:
        await client.health(_server("-I", "-c", "pass"))
    assert caught.value is error
    assert factory.calls == 1
    assert default_calls == 0
    assert not (asyncio.all_tasks() - before)


async def test_none_factory_uses_current_asyncio_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    client = StdioMcpClient(process_factory=None)
    calls: list[tuple[str, tuple[str, ...], int, int, int]] = []
    error = OSError("controlled default spawn failure")

    async def default_spawn(
        program: str, *args: str, stdin: int, stdout: int, stderr: int
    ) -> asyncio.subprocess.Process:
        calls.append((program, args, stdin, stdout, stderr))
        raise error

    monkeypatch.setattr(asyncio, "create_subprocess_exec", default_spawn)
    with pytest.raises(OSError) as caught:
        await client.health(_server("-I", "-c", "pass"))
    assert caught.value is error
    assert calls == [
        (
            sys.executable,
            ("-I", "-c", "pass"),
            asyncio.subprocess.PIPE,
            asyncio.subprocess.PIPE,
            asyncio.subprocess.DEVNULL,
        )
    ]


async def test_transport_only_remains_supported() -> None:
    client = StdioMcpClient(InMemoryMcpClient(tools=()))
    assert await client.health(_server("-I", "-c", "pass")) == "healthy"
