"""Controlled local Python children simulate command info; no Docker or network.

The lifecycle guarantee covers only the direct child, without namespace isolation.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest

from agent_hub.plugins.runtime import _probe_local_runtime


class _NativeProbe:
    def __init__(self, root: Path) -> None:
        self.marker = root / "ready"
        self.script = (
            "import pathlib, sys, time; "
            "assert sys.argv[1:] == ['info', '--format', '{{.ServerVersion}}']; "
            f"pathlib.Path({str(self.marker)!r}).touch(); time.sleep(30)"
        )
        self.processes: list[asyncio.subprocess.Process] = []
        self.gate_reap = False
        self.fail_wait = False
        self.reap_entered = asyncio.Event()
        self.allow_reap = asyncio.Event()

    async def ready(self) -> None:
        async with asyncio.timeout(5):
            while not self.marker.exists():
                await asyncio.sleep(0.01)


@pytest.fixture
async def native_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[_NativeProbe]:
    probe = _NativeProbe(tmp_path)
    create = asyncio.create_subprocess_exec

    async def spawn(
        *argv: str,
        env: dict[str, str],
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> asyncio.subprocess.Process:
        assert argv == ("controlled-probe", "info", "--format", "{{.ServerVersion}}")
        assert stdin == stdout == stderr == asyncio.subprocess.DEVNULL
        process = await create(
            sys.executable,
            "-I",
            "-u",
            "-c",
            probe.script,
            *argv[1:],
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        probe.processes.append(process)
        wait = process.wait

        async def controlled_wait() -> int:
            if probe.fail_wait:
                probe.fail_wait = False
                raise OSError("controlled wait failure")
            result = await wait()
            if probe.gate_reap:
                probe.reap_entered.set()
                await probe.allow_reap.wait()
            return result

        monkeypatch.setattr(process, "wait", controlled_wait)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    try:
        yield probe
    finally:
        probe.allow_reap.set()
        for process in probe.processes:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)


async def _finish(task: asyncio.Task[bool], probe: _NativeProbe) -> None:
    probe.allow_reap.set()
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_cancellation_reaps_real_probe_child(native_probe: _NativeProbe) -> None:
    before = asyncio.all_tasks()
    task = asyncio.create_task(_probe_local_runtime("controlled-probe", 10, {}))
    try:
        await native_probe.ready()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert native_probe.processes[0].returncode is not None, "cancelled probe left child alive"
        assert not (asyncio.all_tasks() - before)
    finally:
        await _finish(task, native_probe)


async def test_repeated_cancellation_finishes_real_probe_reap(native_probe: _NativeProbe) -> None:
    native_probe.gate_reap = True
    before = asyncio.all_tasks()
    task = asyncio.create_task(_probe_local_runtime("controlled-probe", 10, {}))
    try:
        await native_probe.ready()
        task.cancel()
        await asyncio.wait_for(native_probe.reap_entered.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "repeated cancellation interrupted reaping"
        native_probe.allow_reap.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert native_probe.processes[0].returncode is not None
        assert not (asyncio.all_tasks() - before)
    finally:
        await _finish(task, native_probe)


@pytest.mark.parametrize("timeout", [0.0, 0.1])
async def test_timeout_returns_false_and_reaps_real_probe_child(
    native_probe: _NativeProbe,
    timeout: float,
) -> None:
    before = asyncio.all_tasks()
    assert await _probe_local_runtime("controlled-probe", timeout, {}) is False
    assert native_probe.processes[0].returncode is not None
    assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize("exit_code,expected", [(0, True), (7, False)])
async def test_real_probe_preserves_exit_code_and_environment_behavior(
    native_probe: _NativeProbe,
    exit_code: int,
    expected: bool,
) -> None:
    native_probe.script = (
        "import os, sys; "
        "assert sys.argv[1:] == ['info', '--format', '{{.ServerVersion}}']; "
        "assert os.environ['PROBE_ENV'] == 'passed'; "
        "os.write(1, b'discarded stdout'); os.write(2, b'discarded stderr'); "
        f"sys.exit({exit_code})"
    )
    assert await _probe_local_runtime("controlled-probe", 5, {"PROBE_ENV": "passed"}) is expected
    assert native_probe.processes[0].returncode == exit_code


async def test_injected_wait_failure_reaps_real_probe_child(native_probe: _NativeProbe) -> None:
    native_probe.fail_wait = True
    before = asyncio.all_tasks()
    with pytest.raises(OSError, match="controlled wait failure"):
        await _probe_local_runtime("controlled-probe", 5, {})
    assert native_probe.processes[0].returncode is not None
    assert not (asyncio.all_tasks() - before)


async def test_spawn_oserror_still_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unavailable(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        raise OSError("controlled unavailable executable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unavailable)
    assert await _probe_local_runtime("missing-probe", 5, {}) is False
