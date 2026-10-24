from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path
from typing import TypeVar, cast

import pytest

from agent_hub.skills.sandbox.base import SkillInvocation, run_subprocess_with_limits
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete, drain_and_reap_process

T = TypeVar("T")


def invocation(tmp_path: Path, *, output_limit: int = 1024) -> SkillInvocation:
    return SkillInvocation(
        execution_id="subprocess_limits",
        package_path=tmp_path / "skill.zip",
        package_sha256="a" * 64,
        timeout_seconds=1,
        output_limit_bytes=output_limit,
        memory_limit_bytes=128 * 1024 * 1024,
        cpu_quota_percent=50,
        writable_tmp_path=tmp_path,
    )


@pytest.fixture
async def processes() -> AsyncIterator[list[asyncio.subprocess.Process]]:
    existing_tasks = asyncio.all_tasks()
    started: list[asyncio.subprocess.Process] = []
    yield started
    # RED must not leave the real children or reader tasks behind.
    pending = asyncio.all_tasks() - existing_tasks - {asyncio.current_task()}
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for process in started:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.transport.abort()
        await drain_and_reap_process(process)


def assert_reaped(
    processes: list[asyncio.subprocess.Process], existing_tasks: set[asyncio.Task[object]]
) -> None:
    assert len(processes) == 1
    assert processes[0].returncode is not None, "local launcher was not reaped"
    assert not asyncio.all_tasks() - existing_tasks, "subprocess tasks were left running"


@pytest.mark.parametrize("grace", [0, -1, True, None, "20", float("nan"), float("inf"), 10**1000],
                         ids=["zero", "negative", "bool", "none", "string", "nan", "inf", "huge"])
async def test_invalid_backend_cleanup_budget_never_launches(
    tmp_path: Path, processes: list[asyncio.subprocess.Process], grace: object,
) -> None:
    with pytest.raises(ValueError, match="cleanup grace"):
        await run_subprocess_with_limits((sys.executable, "-c", "pass"), invocation(tmp_path),
            process_started=processes.append, termination_grace_seconds=cast(float, grace))
    assert not processes


async def test_backend_can_supply_a_distinct_cleanup_budget(
    tmp_path: Path, processes: list[asyncio.subprocess.Process], monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = asyncio.wait_for
    observed = []

    async def wait_for(awaitable: Awaitable[T], timeout: float) -> T:
        observed.append(timeout)
        return await original(awaitable, timeout)

    async def cleanup() -> None:
        return None

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    result = await run_subprocess_with_limits((sys.executable, "-c", "import time; time.sleep(30)"),
        invocation(tmp_path), stdin_payload=b"", process_started=processes.append,
        on_forced_terminate=cleanup, termination_grace_seconds=20)
    assert result.timed_out
    assert 20 in observed
    assert processes[0].returncode is not None


@pytest.mark.parametrize(
    "trigger,backend_failure",
    [("timeout", False), ("cancel", False), ("timeout", True), ("output", True), ("cancel", True)],
)
async def test_cleanup_after_stdin_disconnect_reaps_and_preserves_backend_error(
    tmp_path: Path,
    processes: list[asyncio.subprocess.Process],
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
    backend_failure: bool,
) -> None:
    existing_tasks = asyncio.all_tasks()
    started = asyncio.Event()
    backend_error = OSError("backend cleanup could not be confirmed")

    def process_started(process: asyncio.subprocess.Process) -> None:
        processes.append(process)
        assert process.stdin is not None
        transport = process.stdin.transport
        abort = transport.abort

        def abort_open_pipe() -> None:
            # Unix write-pipe abort is not idempotent after connection_lost.
            if transport.is_closing():
                raise AttributeError("'NoneType' object has no attribute '_remove_reader'")
            abort()

        if sys.platform != "linux":
            monkeypatch.setattr(transport, "abort", abort_open_pipe)
        started.set()

    async def cleanup() -> None:
        stdin = processes[0].stdin
        assert stdin is not None
        stdin.close()
        await stdin.wait_closed()
        if backend_failure:
            raise backend_error

    script = "import sys, time; "
    if trigger == "output":
        script += "sys.stdout.write('x' * 4096); sys.stdout.flush(); "
    script += "time.sleep(30)"
    task = asyncio.create_task(run_subprocess_with_limits(
        (sys.executable, "-c", script),
        invocation(tmp_path, output_limit=32),
        stdin_payload=b"",
        process_started=process_started,
        on_forced_terminate=cleanup,
    ))
    await asyncio.wait_for(started.wait(), timeout=3)
    if trigger == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught_cancel:
            await asyncio.wait_for(task, timeout=4)
        assert caught_cancel.value.__cause__ is (backend_error if backend_failure else None)
    elif backend_failure:
        with pytest.raises(OSError) as caught_backend:
            await asyncio.wait_for(task, timeout=4)
        assert caught_backend.value is backend_error
        assert caught_backend.value.__cause__ is None
    else:
        result = await asyncio.wait_for(task, timeout=4)
        assert result.timed_out is (trigger == "timeout")
    assert_reaped(processes, existing_tasks)


async def test_timeout_includes_blocked_stdin(
    tmp_path: Path, processes: list[asyncio.subprocess.Process]
) -> None:
    existing_tasks = asyncio.all_tasks()
    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    result = await asyncio.wait_for(
        run_subprocess_with_limits(
            (
                sys.executable,
                "-c",
                (
                    "import sys, time; sys.stdout.buffer.write(b'ready\\n'); "
                    "sys.stdout.flush(); time.sleep(30)"
                ),
            ),
            invocation(tmp_path),
            stdin_payload=b"x" * (8 * 1024 * 1024),
            process_started=processes.append,
            on_forced_terminate=cleanup,
        ),
        timeout=4,
    )

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.stdout == "ready\n"
    assert cleanup_calls == 1
    assert_reaped(processes, existing_tasks)


@pytest.mark.parametrize("blocked_stdin", [False, True])
async def test_cancellation_cleans_up_before_propagating(
    tmp_path: Path, processes: list[asyncio.subprocess.Process], blocked_stdin: bool
) -> None:
    existing_tasks = asyncio.all_tasks()
    started = asyncio.Event()
    cleanup_calls = 0

    def process_started(process: asyncio.subprocess.Process) -> None:
        processes.append(process)
        started.set()

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    task = asyncio.create_task(
        run_subprocess_with_limits(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            invocation(tmp_path),
            stdin_payload=b"x" * (8 * 1024 * 1024) if blocked_stdin else b"",
            process_started=process_started,
            on_forced_terminate=cleanup,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=4)

    assert cleanup_calls == 1
    assert_reaped(processes, existing_tasks)


@pytest.mark.parametrize("trigger", ["timeout", "stdout", "stderr", "both"])
async def test_cleanup_error_still_kills_and_reaps_launcher(
    tmp_path: Path, processes: list[asyncio.subprocess.Process], trigger: str
) -> None:
    existing_tasks = asyncio.all_tasks()
    cleanup_error = OSError("backend cleanup could not be confirmed")
    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise cleanup_error

    script = "import sys, time; "
    if trigger in {"stdout", "both"}:
        script += "sys.stdout.write('x' * 4096); sys.stdout.flush(); "
    if trigger in {"stderr", "both"}:
        script += "sys.stderr.write('y' * 4096); sys.stderr.flush(); "
    script += "time.sleep(30)"

    with pytest.raises(OSError) as caught:
        await asyncio.wait_for(
            run_subprocess_with_limits(
                (sys.executable, "-c", script),
                invocation(tmp_path, output_limit=32),
                process_started=processes.append,
                on_forced_terminate=cleanup,
            ),
            timeout=4,
        )

    assert caught.value is cleanup_error
    assert cleanup_calls == 1
    assert_reaped(processes, existing_tasks)


async def test_start_callback_error_cleans_up_and_preserves_both_errors(
    tmp_path: Path, processes: list[asyncio.subprocess.Process]
) -> None:
    existing_tasks = asyncio.all_tasks()
    start_error = RuntimeError("failed to register launcher")
    cleanup_error = OSError("backend cleanup could not be confirmed")

    def process_started(process: asyncio.subprocess.Process) -> None:
        processes.append(process)
        raise start_error

    async def cleanup() -> None:
        raise cleanup_error

    with pytest.raises(RuntimeError) as caught:
        await run_subprocess_with_limits(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            invocation(tmp_path),
            process_started=process_started,
            on_forced_terminate=cleanup,
        )

    assert caught.value is start_error
    assert caught.value.__cause__ is cleanup_error
    assert_reaped(processes, existing_tasks)


async def test_cancellation_preserves_cleanup_error(
    tmp_path: Path, processes: list[asyncio.subprocess.Process]
) -> None:
    existing_tasks = asyncio.all_tasks()
    started = asyncio.Event()
    cleanup_error = OSError("backend cleanup could not be confirmed")

    def process_started(process: asyncio.subprocess.Process) -> None:
        processes.append(process)
        started.set()

    async def cleanup() -> None:
        raise cleanup_error

    task = asyncio.create_task(
        run_subprocess_with_limits(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            invocation(tmp_path),
            process_started=process_started,
            on_forced_terminate=cleanup,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, timeout=4)

    assert caught.value.__cause__ is cleanup_error
    assert_reaped(processes, existing_tasks)


async def test_repeated_cancellation_waits_for_cleanup(
    tmp_path: Path, processes: list[asyncio.subprocess.Process]
) -> None:
    existing_tasks = asyncio.all_tasks()
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release_cleanup = asyncio.Event()

    def process_started(process: asyncio.subprocess.Process) -> None:
        processes.append(process)
        started.set()

    async def cleanup() -> None:
        cleaning.set()
        await release_cleanup.wait()

    task = asyncio.create_task(
        run_subprocess_with_limits(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            invocation(tmp_path),
            process_started=process_started,
            on_forced_terminate=cleanup,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=3)
    task.cancel()
    try:
        await asyncio.wait_for(cleaning.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation interrupted backend cleanup"
    finally:
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=4)

    assert_reaped(processes, existing_tasks)


@pytest.mark.parametrize("blocked_stdin", [False, True])
async def test_read_error_cleans_up_other_reader_and_launcher(
    tmp_path: Path, processes: list[asyncio.subprocess.Process], blocked_stdin: bool
) -> None:
    existing_tasks = asyncio.all_tasks()
    read_error = OSError("stdout pipe failed")

    def process_started(process: asyncio.subprocess.Process) -> None:
        processes.append(process)
        assert process.stdout is not None
        process.stdout.set_exception(read_error)

    with pytest.raises(OSError) as caught:
        await asyncio.wait_for(
            run_subprocess_with_limits(
                (sys.executable, "-c", "import time; time.sleep(30)"),
                invocation(tmp_path),
                stdin_payload=b"x" * (8 * 1024 * 1024) if blocked_stdin else b"",
                process_started=process_started,
            ),
            timeout=4,
        )

    assert caught.value is read_error
    assert_reaped(processes, existing_tasks)


async def test_timeout_includes_output_after_launcher_exits(
    tmp_path: Path, processes: list[asyncio.subprocess.Process]
) -> None:
    existing_tasks = asyncio.all_tasks()
    output_holder = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys, time; sys.stdout.buffer.write(b'held output'); "
        "sys.stdout.flush(); time.sleep(30)",
        stdout=asyncio.subprocess.PIPE,
    )
    processes.append(output_holder)

    def process_started(process: asyncio.subprocess.Process) -> None:
        processes.append(process)
        # A separate real pipe keeps output open independently of launcher exit.
        process.stdout = output_holder.stdout

    async def cleanup() -> None:
        if output_holder.returncode is None:
            output_holder.kill()
        await output_holder.wait()

    result = await asyncio.wait_for(
        run_subprocess_with_limits(
            (sys.executable, "-c", "pass"),
            invocation(tmp_path),
            process_started=process_started,
            on_forced_terminate=cleanup,
        ),
        timeout=4,
    )

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.stdout == "held output"
    assert len(processes) == 2
    assert all(process.returncode is not None for process in processes)
    assert not asyncio.all_tasks() - existing_tasks


async def test_output_is_drained_while_stdin_is_blocked(
    tmp_path: Path, processes: list[asyncio.subprocess.Process]
) -> None:
    existing_tasks = asyncio.all_tasks()
    result = await asyncio.wait_for(
        run_subprocess_with_limits(
            (
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.stdout.buffer.write(b'x' * 1048576); sys.stdout.flush(); "
                    "sys.stderr.buffer.write(b'y' * 1048576); sys.stderr.flush(); "
                    "sys.stdout.buffer.write(str(len(sys.stdin.buffer.read())).encode())"
                ),
            ),
            invocation(tmp_path, output_limit=2097152),
            stdin_payload=b"z" * (8 * 1024 * 1024),
            process_started=processes.append,
        ),
        timeout=4,
    )

    assert result.timed_out is False
    assert result.exit_code == 0
    assert result.stdout == "x" * 1048576 + "8388608"
    assert result.stderr == "y" * 1048576
    assert_reaped(processes, existing_tasks)


@pytest.mark.parametrize("trigger", ["timeout", "output", "cancel"])
async def test_hung_backend_termination_has_a_bounded_cleanup_deadline(
    tmp_path: Path, processes: list[asyncio.subprocess.Process],
    trigger: str,
) -> None:
    existing_tasks = asyncio.all_tasks()
    entered, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    def process_started(process: asyncio.subprocess.Process) -> None:
        processes.append(process)
        started.set()

    async def cleanup() -> None:
        entered.set()
        await release.wait()

    script = "import sys, time; "
    if trigger == "output":
        script += "sys.stdout.write('x' * 4096); sys.stdout.flush(); "
    script += "time.sleep(30)"
    task = asyncio.create_task(run_subprocess_with_limits(
        (sys.executable, "-c", script), invocation(tmp_path, output_limit=32),
        process_started=process_started, on_forced_terminate=cleanup,
        termination_grace_seconds=0.05,
    ))
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        if trigger == "cancel":
            task.cancel()
        await asyncio.wait_for(entered.wait(), timeout=3)
        done, _ = await asyncio.wait({task}, timeout=2)
        assert task in done, "backend termination blocked owned launcher cleanup indefinitely"
        if trigger == "cancel":
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert isinstance(caught.value.__cause__, ProcessCleanupIncomplete)
        else:
            with pytest.raises(ProcessCleanupIncomplete):
                await task
        assert_reaped(processes, existing_tasks)
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError, ProcessCleanupIncomplete):
            await task
