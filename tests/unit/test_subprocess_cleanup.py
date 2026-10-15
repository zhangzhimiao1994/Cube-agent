from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

import pytest

from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete, drain_and_reap_process


class _Process:
    def __init__(
        self, *, exited: bool = True, fail_wait: bool = False, hold_pipes: bool = False
    ) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = 0 if exited else None
        self.wait_calls = 0
        self.kill_calls = 0
        self.fail_wait = fail_wait
        self.hold_pipes = hold_pipes
        self.pipes_closed = asyncio.Event()
        self._transport: asyncio.SubprocessTransport | None = None
        self.exited = asyncio.Event()
        if exited:
            self.exited.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9
        self.exited.set()
        if not self.hold_pipes:
            self.stdout.feed_eof()
            self.stderr.feed_eof()

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.fail_wait:
            self.fail_wait = False
            raise OSError("controlled wait failure")
        await self.exited.wait()
        if self.hold_pipes:
            await self.pipes_closed.wait()
        return self.returncode or 0


class _Pipe(asyncio.BaseTransport):
    def __init__(self, stream: asyncio.StreamReader, on_close: Callable[[], None]) -> None:
        self.stream = stream
        self.on_close = on_close
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.stream.feed_eof()
        self.on_close()


class _Transport(asyncio.SubprocessTransport):
    def __init__(self, process: _Process) -> None:
        self.process = process
        self.pipes = {
            1: _Pipe(process.stdout, self._closed),
            2: _Pipe(process.stderr, self._closed),
        }

    def _closed(self) -> None:
        if all(pipe.closed for pipe in self.pipes.values()):
            self.process.pipes_closed.set()

    def get_pipe_transport(self, fd: int) -> asyncio.BaseTransport | None:
        return self.pipes.get(fd)


async def test_wait_failure_with_inherited_pipes_closes_owned_transports() -> None:
    process = _Process(exited=False, fail_wait=True, hold_pipes=True)
    transport = _Transport(process)
    process._transport = transport
    before = asyncio.all_tasks()
    with pytest.raises(ProcessCleanupIncomplete) as caught:
        await drain_and_reap_process(cast(asyncio.subprocess.Process, process), grace_seconds=0.1)
    assert isinstance(caught.value.__cause__, OSError)
    assert process.returncode is not None
    assert all(pipe.closed for pipe in transport.pipes.values())
    assert process.wait_calls >= 3
    assert not (asyncio.all_tasks() - before)


async def test_wait_failure_still_kills_and_reaps_owned_process() -> None:
    process = _Process(exited=False, fail_wait=True)
    before = asyncio.all_tasks()
    with pytest.raises(OSError, match="^controlled wait failure$"):
        await drain_and_reap_process(cast(asyncio.subprocess.Process, process), grace_seconds=0.1)
    assert process.kill_calls == 1, "wait failure left the direct child alive"
    assert process.returncode is not None
    assert process.wait_calls >= 2
    assert not (asyncio.all_tasks() - before)


async def test_discard_read_errors_do_not_mask_successful_reaping() -> None:
    process = _Process()
    process.stdout.set_exception(OSError("controlled discard failure"))
    process.stderr.feed_eof()
    before = asyncio.all_tasks()
    await drain_and_reap_process(cast(asyncio.subprocess.Process, process), grace_seconds=0.1)
    assert process.kill_calls == 0
    assert not (asyncio.all_tasks() - before)


async def test_unavailable_pipe_adapter_refuses_incomplete_cleanup() -> None:
    process = _Process()
    before = asyncio.all_tasks()
    with pytest.raises(ProcessCleanupIncomplete, match="pipe transports unavailable"):
        await drain_and_reap_process(cast(asyncio.subprocess.Process, process), grace_seconds=0.1)
    assert not (asyncio.all_tasks() - before)


async def test_slow_direct_child_is_killed_and_reaped() -> None:
    process = _Process(exited=False)
    before = asyncio.all_tasks()
    await drain_and_reap_process(cast(asyncio.subprocess.Process, process), grace_seconds=0.1)
    assert process.kill_calls == 1
    assert process.returncode is not None
    assert not (asyncio.all_tasks() - before)
