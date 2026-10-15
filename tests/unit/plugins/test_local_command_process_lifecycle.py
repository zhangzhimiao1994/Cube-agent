"""Direct-child lifecycle checks, without descendant or namespace isolation guarantees.

Descendants retaining pipe descriptors may prevent cleanup from reaching EOF.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import cast

import pytest

from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins.runtime import _run_local_command


def _serialize_reads(stream: asyncio.StreamReader, monkeypatch: pytest.MonkeyPatch) -> None:
    read = stream.read
    lock = asyncio.Lock()
    owner: asyncio.Task[object] | None = None

    async def serialized_read(n: int = -1) -> bytes:
        nonlocal owner
        caller = asyncio.current_task()
        # StreamReader.read(-1) recursively calls self.read() in the same task.
        if owner is caller:
            return await read(n)
        async with lock:
            owner = caller
            try:
                return await read(n)
            finally:
                owner = None

    monkeypatch.setattr(stream, "read", serialized_read)


@pytest.fixture
async def children(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[list[asyncio.subprocess.Process]]:
    processes: list[asyncio.subprocess.Process] = []
    create = asyncio.create_subprocess_exec

    async def tracked_create(
        *argv: str,
        env: dict[str, str],
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> asyncio.subprocess.Process:
        process = await create(*argv, env=env, stdin=stdin, stdout=stdout, stderr=stderr)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                _serialize_reads(stream, monkeypatch)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked_create)
    try:
        yield processes
    finally:
        for process in processes:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await asyncio.wait_for(process.communicate(), timeout=5)


async def _wait_ready(marker: Path) -> None:
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(0.01)


def _python(script: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, "-I", "-u", "-c", script, *args)


async def test_cancellation_reaps_real_direct_child(
    children: list[asyncio.subprocess.Process],
    tmp_path: Path,
) -> None:
    marker = tmp_path / "ready"
    before = asyncio.all_tasks()
    task = asyncio.create_task(
        _run_local_command(
            _python(
                "import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30)",
                str(marker),
            ),
            timeout_seconds=10,
            environment={},
            max_output_bytes=64,
        )
    )
    try:
        await _wait_ready(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(children) == 1
        assert children[0].returncode is not None, "cancelled command left its direct child alive"
        assert not (asyncio.all_tasks() - before), "command left reader tasks running"
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("stdout_size,stderr_size", [(65, 0), (0, 65), (33, 32)])
async def test_output_budget_rejects_before_real_child_eof(
    children: list[asyncio.subprocess.Process],
    stdout_size: int,
    stderr_size: int,
) -> None:
    before = asyncio.all_tasks()
    with pytest.raises(RuntimeCapabilityError, match="^Plugin result is invalid$"):
        await _run_local_command(
            _python(
                "import os, time; "
                f"os.write(1, b'o' * {stdout_size}); os.write(2, b'e' * {stderr_size}); "
                "time.sleep(30)",
            ),
            timeout_seconds=1,
            environment={},
            max_output_bytes=64,
        )
    assert len(children) == 1
    assert children[0].returncode is not None
    assert not (asyncio.all_tasks() - before), "budget rejection left reader tasks running"


async def test_timeout_reaps_real_direct_child(children: list[asyncio.subprocess.Process]) -> None:
    before = asyncio.all_tasks()
    with pytest.raises(RuntimeCapabilityError, match="^Plugin tool timed out$"):
        await _run_local_command(
            _python("import time; time.sleep(30)"),
            timeout_seconds=0.1,
            environment={},
            max_output_bytes=64,
        )
    assert children[0].returncode is not None
    assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize("fd", [1, 2])
async def test_real_output_flood_does_not_block_reaping(
    children: list[asyncio.subprocess.Process],
    fd: int,
) -> None:
    before = asyncio.all_tasks()
    task = asyncio.create_task(
        _run_local_command(
            _python(f"import os, time; os.write({fd}, b'x' * 1_000_000); time.sleep(30)"),
            timeout_seconds=5,
            environment={},
            max_output_bytes=64,
        )
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=3)
        assert task in done, "output flood left killed child blocked on pipe cleanup"
        with pytest.raises(RuntimeCapabilityError, match="^Plugin result is invalid$"):
            await task
        assert children[0].returncode is not None
        assert not (asyncio.all_tasks() - before)
    finally:
        # A failing implementation may have paused pipe transports; drain them to release wait().
        if not task.done():
            task.cancel()
            for process in children:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                await asyncio.wait_for(process.communicate(), timeout=5)
        with suppress(asyncio.CancelledError, RuntimeCapabilityError):
            await task


@pytest.mark.parametrize("first_fd", [1, 2])
async def test_large_in_budget_output_drains_both_pipes_concurrently(
    children: list[asyncio.subprocess.Process],
    first_fd: int,
) -> None:
    result = await _run_local_command(
        _python(
            "import os; "
            f"os.write({first_fd}, bytes([{64 + first_fd}]) * 80_000); "
            f"os.write({3 - first_fd}, bytes([{67 - first_fd}]) * 80_000)",
        ),
        timeout_seconds=5,
        environment={},
        max_output_bytes=160_000,
    )
    assert result == (0, "A" * 80_000, "B" * 80_000)
    assert children[0].returncode == 0


@pytest.mark.parametrize("budget", [7, 8])
async def test_result_preserves_exit_code_streams_and_utf8_replacement(
    children: list[asyncio.subprocess.Process],
    budget: int,
) -> None:
    result = await _run_local_command(
        _python(
            "import os, sys; os.write(1, b'a\\xe2\\x82\\xac'); os.write(2, b'\\xffbc'); sys.exit(7)"
        ),
        timeout_seconds=5,
        environment={},
        max_output_bytes=budget,
    )
    assert result == (7, "a\u20ac", "\ufffdbc")
    assert children[0].returncode == 7


class _MeasuredReader(asyncio.StreamReader):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[int] = []
        self.received = 0

    async def read(self, n: int = -1) -> bytes:
        self.requests.append(n)
        result = await super().read(n)
        self.received += len(result)
        return result


class _ProcessFixture:
    def __init__(self) -> None:
        self.stdout = _MeasuredReader()
        self.stderr = _MeasuredReader()
        self.returncode: int | None = None
        self.received_at_kill = 0
        self.wait_entered = asyncio.Event()
        self.allow_wait = asyncio.Event()
        self.allow_wait.set()

    def kill(self) -> None:
        self.received_at_kill = self.stdout.received + self.stderr.received
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        self.wait_entered.set()
        await self.allow_wait.wait()
        return self.returncode or 0

    async def communicate(self) -> tuple[bytes, bytes]:
        readers = [asyncio.create_task(stream.read()) for stream in (self.stdout, self.stderr)]
        try:
            stdout, stderr = await asyncio.gather(*readers)
            await self.wait()
            return stdout, stderr
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)


@pytest.fixture
async def process_fixture(monkeypatch: pytest.MonkeyPatch) -> _ProcessFixture:
    process = _ProcessFixture()

    async def create(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    return process


async def test_fixture_reads_are_bounded_before_accumulating_output(
    process_fixture: _ProcessFixture,
) -> None:
    for stream in (process_fixture.stdout, process_fixture.stderr):
        stream.feed_data(b"x" * 200_000)
        stream.feed_eof()
    with pytest.raises(RuntimeCapabilityError, match="^Plugin result is invalid$"):
        await _run_local_command(
            ("fixture",), timeout_seconds=1, environment={}, max_output_bytes=64
        )
    streams = (process_fixture.stdout, process_fixture.stderr)
    assert all(n > 0 for stream in streams for n in stream.requests)
    # Two concurrent reads may each have reserved the remaining budget plus one probe byte.
    assert process_fixture.received_at_kill <= 130
    assert all(n <= 65536 for stream in streams for n in stream.requests)
    assert sum(stream.received for stream in streams) == 400_000
    assert process_fixture.returncode is not None


async def test_fixture_reader_failure_reaps_child_and_cancels_other_reader(
    process_fixture: _ProcessFixture,
) -> None:
    before = asyncio.all_tasks()
    process_fixture.stdout.set_exception(OSError("controlled read failure"))
    with pytest.raises(OSError, match="controlled read failure"):
        await _run_local_command(
            ("fixture",), timeout_seconds=1, environment={}, max_output_bytes=64
        )
    assert process_fixture.returncode is not None
    assert process_fixture.wait_entered.is_set()
    assert not (asyncio.all_tasks() - before)


async def test_fixture_repeated_cancellation_waits_for_reap(
    process_fixture: _ProcessFixture,
) -> None:
    process_fixture.allow_wait.clear()
    before = asyncio.all_tasks()
    task = asyncio.create_task(
        _run_local_command(("fixture",), timeout_seconds=10, environment={}, max_output_bytes=64)
    )
    try:
        while not process_fixture.stdout.requests:
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait_for(process_fixture.wait_entered.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "repeated cancellation interrupted child reaping"
        process_fixture.allow_wait.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process_fixture.returncode is not None
        assert not (asyncio.all_tasks() - before)
    finally:
        process_fixture.allow_wait.set()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
