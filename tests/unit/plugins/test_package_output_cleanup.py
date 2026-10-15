"""Output-error cleanup regressions; native rescue is not product success evidence."""

from __future__ import annotations

import ast
import asyncio
import ctypes
import importlib
import os
import select
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub import subprocess_cleanup
from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins.runtime import (
    PluginInvocationContext,
    PluginPackageExecutionTarget,
    PythonSubprocessPluginPackageRunner,
)
from agent_hub.runtime.contracts import JsonValue


def _invoke(
    target: PluginPackageExecutionTarget,
) -> asyncio.Task[Mapping[str, JsonValue]]:
    identity = UUID("77777777-7777-4777-8777-777777777777")
    capability = PluginCapabilityRequest(
        id="output.run",
        adapter="test_package",
        permission_class="test.execute",
        sandbox_profile="local_process",
    )
    plugin = PluginResourceResponse(
        id="output-cleanup",
        name="Output cleanup",
        status="running",
        health="healthy",
        capabilities=[capability],
    )
    runner = PythonSubprocessPluginPackageRunner(
        python_executable=str(
            Path(str(getattr(sys, "_base_executable", sys.executable))).resolve()
        ),
        timeout_seconds=30,
        max_stdout_bytes=64,
        environment={"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")},
    )
    return asyncio.create_task(
        runner.invoke(
            target=target,
            plugin=plugin,
            capability=capability,
            arguments={},
            context=PluginInvocationContext(
                tenant_id=identity,
                user_id=identity,
                run_id=identity,
                actor="tester",
                idempotency_key="package-output-cleanup",
            ),
        )
    )


async def _settled(task: asyncio.Task[Any], timeout: float = 3) -> None:
    done, _ = await asyncio.wait({task}, timeout=timeout)
    assert task in done, "output failure waited past 3 seconds toward runner timeout=30"


def _error_chain(error: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    pending = [error]
    while pending:
        current = pending.pop()
        if any(current is item for item in found):
            continue
        found.append(current)
        pending.extend(
            item for item in (current.__cause__, current.__context__) if item is not None
        )
    return found


class _Input:
    def write(self, payload: bytes) -> None:
        assert payload

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _Pipe(asyncio.Transport):
    def __init__(self, process: _Process) -> None:
        self.process = process
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.process.release()


class _Transport(asyncio.SubprocessTransport):
    def __init__(self, process: _Process) -> None:
        self.pipe = _Pipe(process)

    def get_pipe_transport(self, fd: int) -> asyncio.Transport | None:
        return self.pipe if fd == 1 else None


class _Process:
    def __init__(self, *, held_pipe: bool, missing: str | None = None) -> None:
        self.held_pipe = held_pipe
        self.entered_wait = asyncio.Event()
        self.exited = asyncio.Event()
        self.returncode: int | None = None
        self.stdin = None if missing == "stdin" else _Input()
        self.output = asyncio.StreamReader()
        self.stdout = None if missing == "stdout" else self.output
        self.stderr = None
        self._transport = _Transport(self)
        self.kills = 0
        if missing is None:
            self.output.feed_data(b"x" * 65)

    async def wait(self) -> int:
        self.entered_wait.set()
        await self.exited.wait()
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.kills += 1
        self.returncode = 1
        if not self.held_pipe:
            self.release()

    def release(self) -> None:
        self.returncode = 1
        if not self.output.at_eof():
            self.output.feed_eof()
        self.exited.set()


async def _ram_rescue(process: _Process, task: asyncio.Task[Any]) -> None:
    process.release()
    if not task.done():
        task.cancel()
    done, _ = await asyncio.wait({task}, timeout=3)
    assert task in done, "RAM rescue did not settle the owned runner task"
    await asyncio.gather(task, return_exceptions=True)


def _ram_target(tmp_path: Path) -> PluginPackageExecutionTarget:
    entrypoint = tmp_path / "never_executed.py"
    entrypoint.write_text("# RAM process fixture; never executed\n", encoding="ascii")
    return PluginPackageExecutionTarget(root=tmp_path, entrypoint=entrypoint)


@pytest.mark.parametrize("held_pipe", [False, True], ids=["ordinary-eof", "inherited-no-eof"])
async def test_stdout_overflow_uses_bounded_cleanup_before_runner_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    held_pipe: bool,
) -> None:
    process = _Process(held_pipe=held_pipe)

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    before = asyncio.all_tasks()
    task = _invoke(_ram_target(tmp_path))
    try:
        await _settled(task)
        message = "Plugin process cleanup incomplete" if held_pipe else "Plugin result is invalid"
        with pytest.raises(RuntimeCapabilityError, match="^" + message + "$") as raised:
            task.result()
        if held_pipe:
            assert isinstance(raised.value.__cause__, subprocess_cleanup.ProcessCleanupIncomplete)
            assert process._transport.pipe.closed, "incomplete output did not close its owned pipe"
            assert any(
                str(error) == "Plugin result is invalid" for error in _error_chain(raised.value)
            )
        assert process.returncode is not None and process.kills > 0
        assert not (asyncio.all_tasks() - before), "output failure left async cleanup tasks"
    finally:
        await _ram_rescue(process, task)
        assert not (asyncio.all_tasks() - before), "RAM fixture left tasks after rescue"


@pytest.mark.parametrize("missing", ["stdin", "stdout"])
async def test_missing_pipe_with_unknown_wait_is_bounded_and_reports_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    process = _Process(held_pipe=True, missing=missing)

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    before = asyncio.all_tasks()
    task = _invoke(_ram_target(tmp_path))
    try:
        await _settled(task)
        with pytest.raises(
            RuntimeCapabilityError, match="^Plugin process cleanup incomplete$"
        ) as raised:
            task.result()
        assert isinstance(raised.value.__cause__, subprocess_cleanup.ProcessCleanupIncomplete)
        assert process._transport.pipe.closed
        assert any(str(error) == "Plugin tool failed" for error in _error_chain(raised.value))
        assert not (asyncio.all_tasks() - before), "missing-pipe cleanup left async tasks"
    finally:
        await _ram_rescue(process, task)
        assert not (asyncio.all_tasks() - before), "RAM fixture left tasks after rescue"


async def test_overflow_cleanup_owns_two_cancellations_until_real_drain_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(held_pipe=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = subprocess_cleanup.drain_and_reap_process
    cleanup_calls = 0

    async def gate(child: asyncio.subprocess.Process, *, grace_seconds: float = 1) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        entered.set()
        await release.wait()
        await original(child, grace_seconds=grace_seconds)

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(subprocess_cleanup, "drain_and_reap_process", gate)
    before = asyncio.all_tasks()
    task = _invoke(_ram_target(tmp_path))
    observer = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait({task, observer}, timeout=1)
        assert observer in done, "stdout overflow bypassed owned cleanup and its drain gate"
        await observer
        assert not task.done() and not release.is_set()
        assert task.cancel(), "first cancellation missed active overflow cleanup"
        await asyncio.sleep(0)
        assert not task.done()
        assert task.cancel(), "second cancellation missed active overflow cleanup"
        await asyncio.sleep(0)
        assert not task.done(), "second cancellation escaped unfinished owned cleanup"
        release.set()
        await _settled(task)
        with pytest.raises(RuntimeCapabilityError, match="^Plugin process cleanup incomplete$"):
            task.result()
        assert cleanup_calls == 1
        assert process._transport.pipe.closed
        assert not (asyncio.all_tasks() - before), "repeated cancellation leaked cleanup tasks"
    finally:
        release.set()
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        await _ram_rescue(process, task)
        assert not (asyncio.all_tasks() - before), "gated RAM fixture left tasks after rescue"


class _Monitor:
    kernel: Any
    handle: int

    def __init__(self, pid: int) -> None:
        if sys.platform != "win32":
            raise OSError("Windows monitor unavailable")
        from ctypes import wintypes

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "OpenProcess": ((wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE),
            "WaitForSingleObject": ((wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD),
            "GetExitCodeProcess": (
                (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)),
                wintypes.BOOL,
            ),
            "TerminateProcess": ((wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
            "CloseHandle": ((wintypes.HANDLE,), wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        handle = self.kernel.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
        if not handle:
            raise OSError("Unable to open independent test-owned process monitor")
        self.handle = int(handle)

    def poll(self) -> int | None:
        state = self.kernel.WaitForSingleObject(self.handle, 0)
        if state == 258:
            return None
        if state != 0:
            raise OSError("Native monitor wait failed")
        code = ctypes.c_uint32()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise OSError("Native exit status unavailable")
        return int(code.value)

    def terminate(self) -> None:
        try:
            alive = self.poll() is None
        except OSError:
            alive = True
        if alive and not self.kernel.TerminateProcess(self.handle, 1) and self.poll() is None:
            raise OSError("Owned process rescue failed")

    async def wait_exit(self) -> int:
        async with asyncio.timeout(3):
            while (code := self.poll()) is None:
                await asyncio.sleep(0.01)
        return code

    def close(self) -> None:
        if not self.kernel.CloseHandle(self.handle):
            raise OSError("Owned monitor handle close failed")


class _PidfdMonitor:
    """Independent Linux exit readiness; pidfd does not provide an exit code here."""

    def __init__(self, pid: int) -> None:
        open_descriptor = getattr(os, "pidfd_open", None)
        if not callable(open_descriptor):
            raise OSError("Native pidfd monitor unavailable")
        self.descriptor = int(open_descriptor(pid))

    def poll(self) -> int | None:
        create_poller = getattr(select, "poll", None)
        input_mask = getattr(select, "POLLIN", None)
        if not callable(create_poller) or not isinstance(input_mask, int):
            raise OSError("Native pidfd readiness poll unavailable")
        poller = create_poller()
        poller.register(self.descriptor, input_mask)
        ready = poller.poll(0)
        if not ready:
            return None
        if any(events & input_mask for _, events in ready):
            return 0
        raise OSError("Native pidfd readiness failed")

    def terminate(self) -> None:
        if self.poll() is None:
            try:
                send_signal = getattr(signal, "pidfd_send_signal", None)
                kill_signal = getattr(signal, "SIGKILL", None)
                if not callable(send_signal) or not isinstance(kill_signal, int):
                    raise OSError("Native pidfd rescue unavailable")
                send_signal(self.descriptor, kill_signal)
            except ProcessLookupError:
                if self.poll() is None:
                    raise

    async def wait_exit(self) -> int:
        async with asyncio.timeout(3):
            while self.poll() is None:
                await asyncio.sleep(0.01)
        return 0

    def close(self) -> None:
        os.close(self.descriptor)


def _open_monitor(pid: int) -> _Monitor | _PidfdMonitor:
    return _Monitor(pid) if sys.platform == "win32" else _PidfdMonitor(pid)


@pytest.mark.skipif(sys.platform != "linux", reason="native Linux high pidfd")
def test_pidfd_monitor_supports_high_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    open_descriptor = getattr(os, "pidfd_open", None)
    assert callable(open_descriptor)
    original = int(open_descriptor(os.getpid()))
    descriptor: int | None = None
    try:
        fcntl = importlib.import_module("fcntl")
        descriptor = int(fcntl.fcntl(original, fcntl.F_DUPFD_CLOEXEC, 1100))
        monkeypatch.setattr(os, "pidfd_open", lambda _: descriptor)
        monitor = _PidfdMonitor(os.getpid())
        assert monitor.poll() is None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(original)


def test_fixture_alarm_still_exits_when_marker_write_fails() -> None:
    class AlarmExit(Exception):
        pass

    def exit_alarm(code: int) -> None:
        assert code == 124
        raise AlarmExit

    class Directory:
        def __truediv__(self, name: str) -> Directory:
            return self

        def touch(self) -> None:
            raise OSError("controlled marker write failure")

    alarm = next(
        node
        for node in ast.parse(_NATIVE_PEER).body
        if isinstance(node, ast.FunctionDef) and node.name == "alarm"
    )
    namespace: dict[str, Any] = {
        "time": SimpleNamespace(sleep=lambda _: None),
        "os": SimpleNamespace(getpid=lambda: 42, _exit=exit_alarm),
        "directory": Directory(),
    }
    exec(  # noqa: S102 - execute only the test-owned alarm function, never plugin/user source
        compile(ast.Module(body=[alarm], type_ignores=[]), "controlled-alarm", "exec"), namespace
    )
    with pytest.raises(AlarmExit):
        namespace["alarm"]()


_NATIVE_PEER = r"""import json
import os
import pathlib
import subprocess
import sys
import threading
import time

directory = pathlib.Path(__file__).parent
def alarm():
    time.sleep(15)
    try:
        (directory / ('alarm-' + str(os.getpid()))).touch()
    finally:
        os._exit(124)
threading.Thread(target=alarm, daemon=True).start()
if len(sys.argv) > 1:
    temporary = directory / 'holder.tmp'
    temporary.write_text(str(os.getpid()), encoding='ascii')
    temporary.replace(directory / 'holder.pid')
else:
    subprocess.Popen(
        [sys.executable, '-I', '-u', __file__, 'holder'],
        stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=subprocess.DEVNULL,
        close_fds=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    json.load(sys.stdin)
    (directory / 'root-ready').touch()
    while not (directory / 'go').exists():
        time.sleep(0.01)
    os.write(1, b'x' * 65)
while True:
    time.sleep(0.01)
"""


async def _read_pid(path: Path) -> int:
    async with asyncio.timeout(5):
        while True:
            try:
                return int(path.read_text(encoding="ascii"))
            except (FileNotFoundError, PermissionError):
                await asyncio.sleep(0.01)


@pytest.mark.skipif(
    sys.platform != "win32"
    and not (
        sys.platform == "linux"
        and hasattr(os, "pidfd_open")
        and hasattr(signal, "pidfd_send_signal")
    ),
    reason="requires native Windows handles or Linux pidfd",
)
async def test_native_overflow_reports_incomplete_before_inherited_stdout_rescue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = tmp_path / "native_output_peer.py"
    entrypoint.write_text(_NATIVE_PEER, encoding="ascii")
    processes: list[asyncio.subprocess.Process] = []
    monitors: list[_Monitor | _PidfdMonitor] = []
    create = asyncio.create_subprocess_exec

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await create(*argv, **kwargs)
        processes.append(process)
        monitors.append(_open_monitor(process.pid))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    before = asyncio.all_tasks()
    task = _invoke(PluginPackageExecutionTarget(root=tmp_path, entrypoint=entrypoint))
    try:
        monitors.append(_open_monitor(await _read_pid(tmp_path / "holder.pid")))
        async with asyncio.timeout(5):
            while not (tmp_path / "root-ready").exists():
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        assert len(monitors) == 2 and all(monitor.poll() is None for monitor in monitors)
        (tmp_path / "go").touch()
        await _settled(task)
        with pytest.raises(
            RuntimeCapabilityError, match="^Plugin process cleanup incomplete$"
        ) as raised:
            task.result()
        assert isinstance(raised.value.__cause__, subprocess_cleanup.ProcessCleanupIncomplete)
        assert any(str(error) == "Plugin result is invalid" for error in _error_chain(raised.value))
        root_code = monitors[0].poll()
        assert root_code is not None and root_code != 124, "root exit relied on the 15s alarm"
        assert monitors[1].poll() is None, "holder stopped before the pre-rescue assertion"
        assert not list(tmp_path.glob("alarm-*")), "exit readiness relied on fixture alarm"
        assert processes[0].returncode is not None
        assert not (asyncio.all_tasks() - before), "native overflow left owned cleanup tasks"
    finally:
        errors: list[Exception] = []
        if processes and len(monitors) < 2:
            try:
                monitors.append(_open_monitor(await _read_pid(tmp_path / "holder.pid")))
            except Exception as error:  # noqa: BLE001 - failed readiness must not skip root rescue
                errors.append(error)
        for process in processes:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except Exception as error:  # noqa: BLE001 - attempt all captured Process resources
                errors.append(error)
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception as error:  # noqa: BLE001 - kill failure must not skip pipe close
                errors.append(error)
        for monitor in reversed(monitors):
            try:
                monitor.terminate()
            except Exception as error:  # noqa: BLE001 - attempt each independently owned handle
                errors.append(error)
        for monitor in monitors:
            try:
                assert await monitor.wait_exit() != 124
            except Exception as error:  # noqa: BLE001 - attempt each owned reap before closes
                errors.append(error)
        if not task.done():
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=5)
        if task in done:
            await asyncio.gather(task, return_exceptions=True)
        else:
            errors.append(TimeoutError("Native fixture runner task did not settle"))
        for process in processes:
            try:
                if task.done():
                    await asyncio.wait_for(process.communicate(), timeout=3)
                else:
                    await asyncio.wait_for(process.wait(), timeout=3)
            except Exception as error:  # noqa: BLE001 - reap failure must not skip handle close
                errors.append(error)
        for monitor in monitors:
            try:
                monitor.close()
            except Exception as error:  # noqa: BLE001 - attempt all independent handle closes
                errors.append(error)
        assert not (asyncio.all_tasks() - before), "native fixture left async tasks after rescue"
        assert not list(tmp_path.glob("alarm-*")), "fixture rescue relied on its alarm"
        if errors:
            raise OSError("Native output fixture rescue incomplete") from errors[0]
