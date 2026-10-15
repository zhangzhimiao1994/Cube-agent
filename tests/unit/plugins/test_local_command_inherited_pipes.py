"""Native Windows inherited-pipe regression, without descendant termination claims."""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins.runtime import _run_local_command
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

_GRANDCHILD = """
import os
import pathlib
import sys
import time

release, ready, done = (pathlib.Path(value) for value in sys.argv[1:])
deadline = time.monotonic() + 15
try:
    ready.write_text(str(os.getpid()), encoding='ascii')
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
finally:
    done.touch()
"""


_CHILD = """
import os
import pathlib
import subprocess
import sys
import time

grandchild_source, release, ready, done, trigger = sys.argv[1:]
subprocess.Popen(
    [sys.executable, '-I', '-u', '-c', grandchild_source, release, ready, done],
    stdin=subprocess.DEVNULL,
    stdout=sys.stdout,
    stderr=sys.stderr,
    close_fds=True,
    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
)
deadline = time.monotonic() + 5
while not pathlib.Path(ready).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('grandchild did not become ready')
    time.sleep(0.01)
if trigger == 'budget':
    os.write(1, b'x' * 65)
time.sleep(15)
"""


async def _wait_file(path: Path, *, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not path.exists():
            await asyncio.sleep(0.01)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows inherited pipe handles")
@pytest.mark.parametrize("trigger", ["budget", "timeout", "cancel"])
async def test_inherited_pipes_report_incomplete_before_rescue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    trigger: str,
) -> None:
    release = tmp_path / "release"
    ready = tmp_path / "ready"
    done = tmp_path / "done"
    processes: list[asyncio.subprocess.Process] = []
    create = asyncio.create_subprocess_exec

    async def spawn(
        *argv: str,
        env: dict[str, str],
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> asyncio.subprocess.Process:
        process = await create(*argv, env=env, stdin=stdin, stdout=stdout, stderr=stderr)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    before = asyncio.all_tasks()
    command = asyncio.create_task(
        _run_local_command(
            (
                sys.executable,
                "-I",
                "-u",
                "-c",
                _CHILD,
                _GRANDCHILD,
                str(release),
                str(ready),
                str(done),
                trigger,
            ),
            timeout_seconds=1 if trigger == "timeout" else 10,
            environment={},
            max_output_bytes=64,
        )
    )
    rescued = asyncio.Event()

    async def rescue() -> None:
        await asyncio.sleep(4)
        rescued.set()
        release.touch()

    rescue_task: asyncio.Task[None] | None = None
    try:
        await _wait_file(ready)
        rescue_task = asyncio.create_task(rescue())
        if trigger == "cancel":
            command.cancel()
        with pytest.raises(
            RuntimeCapabilityError,
            match="^Plugin process cleanup incomplete$",
        ) as failure:
            await command
        assert isinstance(failure.value.__cause__, ProcessCleanupIncomplete)
        assert not rescued.is_set(), "cleanup required the 4-second test rescue"
        assert not release.exists()
        assert not done.exists(), "grandchild must still be holding the pipe descriptors"
        assert len(processes) == 1
        assert processes[0].returncode is not None, "direct child was not reaped"
        assert not (asyncio.all_tasks() - before - {rescue_task})
    finally:
        release.touch()
        if rescue_task is not None:
            rescue_task.cancel()
            with suppress(asyncio.CancelledError):
                await rescue_task
        if not command.done():
            command.cancel()
        with suppress(asyncio.CancelledError, RuntimeCapabilityError):
            await command
        for process in processes:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)
        if ready.exists():
            await _wait_file(done)
