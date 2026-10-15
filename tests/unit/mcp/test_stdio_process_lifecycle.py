from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.mcp.client import StdioMcpClient
from agent_hub.mcp.types import McpServerDefinition, McpTransportKind


def _serialize_reads(stream: asyncio.StreamReader, monkeypatch: pytest.MonkeyPatch) -> None:
    original = stream.read
    lock = asyncio.Lock()
    owner: asyncio.Task[object] | None = None

    async def read(n: int = -1) -> bytes:
        nonlocal owner
        caller = asyncio.current_task()
        if owner is caller:
            return await original(n)
        async with lock:
            owner = caller
            try:
                return await original(n)
            finally:
                owner = None

    monkeypatch.setattr(stream, "read", read)


@pytest.fixture
async def children(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[list[asyncio.subprocess.Process]]:
    processes: list[asyncio.subprocess.Process] = []
    original = asyncio.create_subprocess_exec

    async def spawn(program: str, *args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(program, *args, **kwargs)
        if process.stdout is not None:
            _serialize_reads(process.stdout, monkeypatch)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    try:
        yield processes
    finally:
        for process in processes:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await asyncio.wait_for(process.communicate(), timeout=5)


def _server(script: Path) -> McpServerDefinition:
    return McpServerDefinition(
        tenant_id=UUID("66666666-6666-4666-8666-666666666666"),
        id="lifecycle",
        transport=McpTransportKind.STDIO,
        command=sys.executable,
        args=("-I", "-u", str(script)),
        executable_allowlist=(sys.executable,),
    )


async def test_protocol_failure_preserves_error_and_reaps_real_child(
    tmp_path: Path, children: list[asyncio.subprocess.Process]
) -> None:
    script = tmp_path / "error_mcp.py"
    script.write_text(
        "import sys, time\n"
        'sys.stdout.write(\'{"jsonrpc":"2.0","id":1,"error":{"message":"controlled error"}}\\n\')\n'
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    before = asyncio.all_tasks()
    with pytest.raises(RuntimeError, match="^controlled error$"):
        await StdioMcpClient().health(_server(script))
    assert children[0].returncode is not None
    assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize("cancel_twice", [False, True])
async def test_cancelled_mcp_request_reaps_real_direct_child(
    tmp_path: Path, children: list[asyncio.subprocess.Process], cancel_twice: bool
) -> None:
    script = tmp_path / "waiting_mcp.py"
    marker = tmp_path / "ready"
    script.write_text(
        f"import pathlib, time; pathlib.Path({str(marker)!r}).touch(); time.sleep(30)",
        encoding="utf-8",
    )
    before = asyncio.all_tasks()
    task = asyncio.create_task(StdioMcpClient().health(_server(script)))
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
        task.cancel()
        if cancel_twice:
            async with asyncio.timeout(5):
                while not children[0].stdin or not children[0].stdin.is_closing():
                    await asyncio.sleep(0)
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None, "cancelled MCP request left its child alive"
        assert not (asyncio.all_tasks() - before), "MCP cleanup left reader/wait tasks running"
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_extra_stdout_cannot_block_mcp_reaping(
    tmp_path: Path, children: list[asyncio.subprocess.Process]
) -> None:
    script = tmp_path / "flooding_mcp.py"
    script.write_text(
        "import os, time\n"
        "os.write(1, b'Content-Length: 36\\r\\n\\r\\n'"
        ' b\'{"jsonrpc":"2.0","id":1,"result":{}}\')\n'
        "os.write(1, b'x' * 1_000_000)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    rescued = asyncio.Event()

    async def rescue() -> None:
        await asyncio.sleep(4)
        rescued.set()
        for process in children:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            if process.stdout is not None:
                await process.stdout.read()

    before = asyncio.all_tasks()
    rescuer = asyncio.create_task(rescue())
    try:
        assert await asyncio.wait_for(StdioMcpClient().health(_server(script)), 10) == "healthy"
        assert not rescued.is_set(), "stdout backpressure blocked MCP process cleanup"
        assert children[0].returncode is not None
    finally:
        rescuer.cancel()
        await asyncio.gather(rescuer, return_exceptions=True)
    assert not (asyncio.all_tasks() - before)


async def test_inherited_stdout_reports_incomplete_cleanup_without_waiting_for_descendant(
    tmp_path: Path, children: list[asyncio.subprocess.Process]
) -> None:
    release = tmp_path / "release"
    ready = tmp_path / "descendant-ready"
    done = tmp_path / "descendant-done"
    descendant = (
        "import pathlib, time; "
        f"release=pathlib.Path({str(release)!r}); pathlib.Path({str(ready)!r}).touch(); "
        "deadline=time.monotonic()+15\n"
        "while not release.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
        f"pathlib.Path({str(done)!r}).touch()\n"
    )
    script = tmp_path / "inherited_mcp.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-I', '-u', '-c', {descendant!r}], "
        "stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=subprocess.DEVNULL)\n"
        f"while not pathlib.Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
        "sys.stdout.buffer.write(b'Content-Length: 36\\r\\n\\r\\n'"
        ' b\'{"jsonrpc":"2.0","id":1,"result":{}}\')\n'
        "sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    rescued = asyncio.Event()

    async def rescue() -> None:
        await asyncio.sleep(4)
        rescued.set()
        release.touch()

    before = asyncio.all_tasks()
    rescuer = asyncio.create_task(rescue())
    try:
        with pytest.raises(RuntimeError, match="^MCP process cleanup incomplete$"):
            await StdioMcpClient().health(_server(script))
        assert not rescued.is_set(), "MCP cleanup waited for the descendant to close stdout"
        assert children[0].returncode is not None
        assert not done.exists(), "test descendant no longer holds the inherited pipe"
    finally:
        release.touch()
        rescuer.cancel()
        await asyncio.gather(rescuer, return_exceptions=True)
        async with asyncio.timeout(5):
            while not done.exists():
                await asyncio.sleep(0.01)
    assert not (asyncio.all_tasks() - before)
