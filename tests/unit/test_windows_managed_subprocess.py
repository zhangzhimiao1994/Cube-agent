"""Native Windows managed stdio bridge tests, without namespace or filesystem claims."""

from __future__ import annotations

import asyncio
import ctypes
import gc
import importlib
import importlib.util
import sys
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows stdio bridge")


def _backend() -> Any:
    name = "agent_hub.windows_managed_subprocess"
    assert importlib.util.find_spec(name) is not None, (
        "Windows managed subprocess is not implemented"
    )
    return importlib.import_module(name)


def _kernel() -> Any:
    if sys.platform != "win32":
        raise RuntimeError("Windows monitor unavailable")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _native_error(message: str) -> OSError:
    if sys.platform == "win32":
        return ctypes.WinError(ctypes.get_last_error())
    return OSError(message)


class _Monitor:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.kernel = _kernel()
        # Only this independently opened monitor handle is owned and closed by the test.
        handle = self.kernel.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
        if not handle:
            raise _native_error("OpenProcess failed")
        self.handle = int(handle)

    def poll(self) -> int | None:
        state = self.kernel.WaitForSingleObject(self.handle, 0)
        if state == 258:
            return None
        if state != 0:
            raise _native_error("WaitForSingleObject failed")
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise _native_error("GetExitCodeProcess failed")
        return int(code.value)

    async def wait_exit(self) -> int:
        async with asyncio.timeout(3):
            while (code := self.poll()) is None:
                await asyncio.sleep(0.01)
        return code

    def terminate_if_running(self) -> None:
        if self.poll() is None:
            result = self.kernel.TerminateProcess(self.handle, 1)
            if not result and self.poll() is None:
                raise _native_error("test rescue TerminateProcess failed")

    def close(self) -> None:
        if self.handle:
            if not self.kernel.CloseHandle(self.handle):
                raise _native_error("monitor CloseHandle failed")
            self.handle = 0


_DEADLINE = """
import os
import threading
import time

threading.Thread(target=lambda: (time.sleep(15), os._exit(124)), daemon=True).start()
"""


def _python(source: str, *args: str) -> tuple[str, ...]:
    executable = Path(str(getattr(sys, "_base_executable", sys.executable))).resolve()
    assert executable.is_absolute() and executable.is_file()
    return (str(executable), "-I", "-u", "-c", _DEADLINE + source, *args)


async def _read_pid(path: Path) -> int:
    async with asyncio.timeout(5):
        while True:
            try:
                return int(path.read_text(encoding="ascii"))
            except (FileNotFoundError, PermissionError):
                # Windows can briefly deny opening a just-published signal file.
                await asyncio.sleep(0.01)


def _assert_process(process: asyncio.subprocess.Process, *, stdin_pipe: bool = False) -> None:
    assert isinstance(process, asyncio.subprocess.Process)
    assert isinstance(getattr(process, "_transport", None), asyncio.SubprocessTransport)
    assert isinstance(process.stdout, asyncio.StreamReader)
    assert isinstance(process.stderr, asyncio.StreamReader)
    if stdin_pipe:
        assert isinstance(process.stdin, asyncio.StreamWriter)
    else:
        assert process.stdin is None


async def _read_bounded(stream: asyncio.StreamReader, budget: int) -> bytes:
    output = bytearray()
    while chunk := await stream.read(min(4096, budget + 1 - len(output))):
        assert len(output) + len(chunk) <= budget, "finite test output exceeded its budget"
        output.extend(chunk)
    return bytes(output)


async def _discard(stream: asyncio.StreamReader) -> None:
    while await stream.read(65536):
        pass


async def _cleanup(
    process: asyncio.subprocess.Process | None,
    signals: list[Path],
    monitors: list[_Monitor],
    readers: list[asyncio.Task[bytes]],
) -> None:
    for signal in signals:
        signal.touch()
    for reader in readers:
        reader.cancel()
    await asyncio.gather(*readers, return_exceptions=True)
    drains: list[asyncio.Task[None]] = []
    try:
        if process is not None:
            with suppress(ProcessLookupError):
                process.kill()
        for monitor in monitors:
            monitor.terminate_if_running()
        for monitor in monitors:
            await monitor.wait_exit()
        if process is not None:
            drains = [
                asyncio.create_task(_discard(stream))
                for stream in (process.stdout, process.stderr)
                if stream is not None
            ]
            async with asyncio.timeout(5):
                await asyncio.gather(*drains, process.wait())
    finally:
        for drain in drains:
            drain.cancel()
        await asyncio.gather(*drains, return_exceptions=True)
        for monitor in monitors:
            monitor.close()


_ECHO = """
import pathlib
import sys

ready = pathlib.Path(sys.argv[1])
ready_tmp = pathlib.Path(str(ready) + '.tmp')
ready_tmp.write_text(str(os.getpid()), encoding='ascii')
ready_tmp.replace(ready)
data = sys.stdin.buffer.read()
sys.stdout.buffer.write(b'out:' + data)
sys.stdout.buffer.flush()
sys.stderr.buffer.write(b'err:\\xe9\\x94\\x99\\xe8\\xaf\\xaf\\n')
sys.stderr.buffer.flush()
sys.exit(int(os.environ['BRIDGE_EXIT']))
"""


async def test_native_stdin_echo_separate_utf8_streams_and_exit_code(tmp_path: Path) -> None:
    backend = _backend()
    ready = tmp_path / "ready"
    process: asyncio.subprocess.Process | None = None
    monitors: list[_Monitor] = []
    readers: list[asyncio.Task[bytes]] = []
    try:
        process = await backend.create_subprocess_exec(
            *_python(_ECHO, str(ready)),
            stdin=asyncio.subprocess.PIPE,
            env={"BRIDGE_EXIT": "7"},
        )
        _assert_process(process, stdin_pipe=True)
        assert await _read_pid(ready) == process.pid
        monitors.append(_Monitor(process.pid))
        assert (
            process.stdin is not None and process.stdout is not None and process.stderr is not None
        )
        readers = [
            asyncio.create_task(_read_bounded(stream, 256))
            for stream in (process.stdout, process.stderr)
        ]
        process.stdin.write(b"request-\xe4\xb8\xad\n")
        await process.stdin.drain()
        process.stdin.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()
        async with asyncio.timeout(3):
            stdout, stderr = await asyncio.gather(*readers)
            assert await process.wait() == 7
        assert stdout == b"out:request-\xe4\xb8\xad\n"
        assert stderr == b"err:\xe9\x94\x99\xe8\xaf\xaf\n"
        assert process.returncode == 7
        assert await monitors[0].wait_exit() == 7
    finally:
        await _cleanup(process, [], monitors, readers)


_DEFAULT = """
import pathlib
import sys

ready, release = (pathlib.Path(value) for value in sys.argv[1:])
ready_tmp = pathlib.Path(str(ready) + '.tmp')
ready_tmp.write_text(str(os.getpid()), encoding='ascii')
ready_tmp.replace(ready)
while not release.exists():
    time.sleep(0.01)
"""


async def test_native_default_devnull_stdin_and_successful_wait(tmp_path: Path) -> None:
    backend = _backend()
    ready, release = tmp_path / "ready", tmp_path / "release"
    process: asyncio.subprocess.Process | None = None
    monitors: list[_Monitor] = []
    try:
        process = await backend.create_subprocess_exec(*_python(_DEFAULT, str(ready), str(release)))
        _assert_process(process)
        assert await _read_pid(ready) == process.pid
        monitors.append(_Monitor(process.pid))
        assert process.returncode is None
        release.touch()
        async with asyncio.timeout(3):
            assert await process.wait() == 0
            assert process.stdout is not None and process.stderr is not None
            assert await _read_bounded(process.stdout, 1) == b""
            assert await _read_bounded(process.stderr, 1) == b""
        assert process.returncode == 0
        assert await monitors[0].wait_exit() == 0
    finally:
        await _cleanup(process, [release], monitors, [])


_GRAND = """
import pathlib
import sys

ready, release, done = (pathlib.Path(value) for value in sys.argv[1:])
os.write(1, b'grand-out')
os.write(2, b'grand-err')
ready_tmp = pathlib.Path(str(ready) + '.tmp')
ready_tmp.write_text(str(os.getpid()), encoding='ascii')
ready_tmp.replace(ready)
while not release.exists():
    time.sleep(0.01)
done.touch()
"""


_FAMILY = """
import pathlib
import subprocess
import sys

grand_source, ready, release, done, exit_root, root_ready = sys.argv[1:]
subprocess.Popen(
    [sys.executable, '-I', '-u', '-c', grand_source, ready, release, done],
    stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
root_tmp = pathlib.Path(root_ready + '.tmp')
root_tmp.write_text(str(os.getpid()), encoding='ascii')
root_tmp.replace(root_ready)
while not pathlib.Path(exit_root).exists():
    time.sleep(0.01)
"""


@pytest.mark.parametrize("action", ["root_exit", "kill", "cancel_wait"])
async def test_native_family_cleanup_reaches_inherited_pipe_eof_without_release(
    tmp_path: Path,
    action: str,
) -> None:
    backend = _backend()
    ready, release, done = tmp_path / "grand.pid", tmp_path / "release", tmp_path / "grand.done"
    exit_root = tmp_path / "exit-root"
    root_ready = tmp_path / "root.pid"
    process: asyncio.subprocess.Process | None = None
    monitors: list[_Monitor] = []
    readers: list[asyncio.Task[bytes]] = []
    waiter: asyncio.Task[int] | None = None
    try:
        process = await backend.create_subprocess_exec(
            *_python(
                _FAMILY,
                _DEADLINE + _GRAND,
                str(ready),
                str(release),
                str(done),
                str(exit_root),
                str(root_ready),
            ),
        )
        _assert_process(process)
        assert await _read_pid(root_ready) == process.pid
        monitors.append(_Monitor(process.pid))
        monitors.append(_Monitor(await _read_pid(ready)))
        assert all(monitor.poll() is None for monitor in monitors)
        assert process.stdout is not None and process.stderr is not None
        readers = [
            asyncio.create_task(_read_bounded(stream, 256))
            for stream in (process.stdout, process.stderr)
        ]
        if action == "root_exit":
            exit_root.touch()
        elif action == "kill":
            process.kill()
        else:
            waiter = asyncio.create_task(process.wait())
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        async with asyncio.timeout(3):
            code = await process.wait()
            stdout, stderr = await asyncio.gather(*readers)
            assert await monitors[0].wait_exit() == code
            await monitors[1].wait_exit()
        assert code == 0 if action == "root_exit" else code != 0
        assert process.returncode == code
        assert stdout == b"grand-out" and stderr == b"grand-err"
        assert not release.exists(), "family cleanup must not need a test release"
        assert not done.exists(), "grandchild was terminated, not released or allowed to expire"
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await _cleanup(process, [release, exit_root], monitors, readers)


_FLOOD = """
import pathlib
import sys

ready, release, descriptor = sys.argv[1:]
ready_tmp = pathlib.Path(ready + '.tmp')
ready_tmp.write_text(str(os.getpid()), encoding='ascii')
ready_tmp.replace(ready)
remaining = 1_000_000
while remaining:
    chunk = b'x' * min(65536, remaining)
    remaining -= os.write(int(descriptor), chunk)
while not pathlib.Path(release).exists():
    time.sleep(0.01)
"""


@pytest.mark.parametrize("descriptor", [1, 2])
async def test_native_one_megabyte_flood_allows_bounded_consumer_kill_and_drain(
    tmp_path: Path,
    descriptor: int,
) -> None:
    backend = _backend()
    ready, release = tmp_path / "ready", tmp_path / "release"
    process: asyncio.subprocess.Process | None = None
    monitors: list[_Monitor] = []
    try:
        process = await backend.create_subprocess_exec(
            *_python(_FLOOD, str(ready), str(release), str(descriptor)),
        )
        _assert_process(process)
        assert await _read_pid(ready) == process.pid
        monitors.append(_Monitor(process.pid))
        stream = process.stdout if descriptor == 1 else process.stderr
        assert stream is not None
        total = 0
        async with asyncio.timeout(3):
            while total <= 64:
                chunk = await stream.read(65 - total)
                assert chunk, "controlled flood stopped before exceeding the consumer budget"
                total += len(chunk)
            assert total == 65
            process.kill()
            assert process.stdout is not None and process.stderr is not None
            await asyncio.gather(_discard(process.stdout), _discard(process.stderr), process.wait())
            await monitors[0].wait_exit()
        assert process.returncode is not None and process.returncode != 0
    finally:
        await _cleanup(process, [release], monitors, [])


async def test_selector_loop_fails_closed_without_stdlib_fallback_or_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend()
    marker = tmp_path / "must-not-run"

    async def forbidden_fallback(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        raise AssertionError("unsupported-loop bridge attempted an unmanaged fallback")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_fallback)
    argv = _python("import pathlib, sys; pathlib.Path(sys.argv[1]).touch()", str(marker))

    def run_selector() -> None:
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(backend.create_subprocess_exec(*argv))

    with pytest.raises((OSError, RuntimeError)):
        await asyncio.to_thread(run_selector)
    assert not marker.exists()


@pytest.mark.parametrize("action", ["fail_connect", "cancel_twice"])
async def test_partial_connection_startup_failure_reaps_real_root(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    backend = _backend()
    original_job = backend.WindowsProcessJob
    monitors: list[_Monitor] = []
    entered = asyncio.Event()
    blocked = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_connect = loop.connect_read_pipe
    calls = 0

    def capture_job(*args: Any, **kwargs: Any) -> Any:
        job = original_job(*args, **kwargs)
        monitors.append(_Monitor(job.pid))
        return job

    async def connect(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            if action == "fail_connect":
                raise OSError("controlled stderr connection failure")
            await blocked.wait()
        return await original_connect(*args, **kwargs)

    monkeypatch.setattr(backend, "WindowsProcessJob", capture_job)
    monkeypatch.setattr(loop, "connect_read_pipe", connect)
    before = asyncio.all_tasks()
    task = asyncio.create_task(backend.create_subprocess_exec(*_python("time.sleep(15)")))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if action == "cancel_twice":
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(OSError, match="controlled stderr connection failure"):
                await task
        assert len(monitors) == 1
        await monitors[0].wait_exit()
        assert not (asyncio.all_tasks() - before), "startup left owned async tasks"
    finally:
        blocked.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for monitor in monitors:
            try:
                monitor.terminate_if_running()
            finally:
                monitor.close()


async def test_watcher_allocation_failure_reaps_root_without_registering_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend()
    loop = asyncio.get_running_loop()
    original_job = backend.WindowsProcessJob
    original_task = loop.create_task
    original_wait = loop._proactor.wait_for_handle  # type: ignore[attr-defined]
    monitors: list[_Monitor] = []
    registrations: list[int] = []

    def capture_job(*args: Any, **kwargs: Any) -> Any:
        job = original_job(*args, **kwargs)
        monitors.append(_Monitor(job.pid))
        return job

    def fail_watcher(coroutine: Any, *args: Any, **kwargs: Any) -> Any:
        if coroutine.__qualname__ == "_JobTransport._watch_root":
            raise RuntimeError("controlled watcher allocation failure")
        return original_task(coroutine, *args, **kwargs)

    def register(handle: int, *args: Any, **kwargs: Any) -> Any:
        registrations.append(handle)
        return original_wait(handle, *args, **kwargs)

    monkeypatch.setattr(backend, "WindowsProcessJob", capture_job)
    monkeypatch.setattr(loop, "create_task", fail_watcher)
    monkeypatch.setattr(loop._proactor, "wait_for_handle", register)  # type: ignore[attr-defined]
    try:
        with pytest.raises(RuntimeError, match="controlled watcher allocation failure"):
            await backend.create_subprocess_exec(*_python("time.sleep(15)"))
        assert len(monitors) == 1
        await monitors[0].wait_exit()
        assert not registrations
    finally:
        for monitor in monitors:
            try:
                monitor.terminate_if_running()
            finally:
                monitor.close()


async def test_completed_managed_cycles_return_native_handles_to_warm_baseline() -> None:
    backend = _backend()
    kernel = _kernel()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessHandleCount.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetProcessHandleCount.restype = wintypes.BOOL

    def count() -> int:
        value = wintypes.DWORD()
        if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(value)):
            raise _native_error("GetProcessHandleCount failed")
        return int(value.value)

    async def cycle() -> None:
        process = await backend.create_subprocess_exec(
            *_python("pass"), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        async with asyncio.timeout(3):
            assert await process.wait() == 0

    await cycle()
    await asyncio.sleep(0)
    gc.collect()
    before = count()
    for _ in range(8):
        await cycle()
    await asyncio.sleep(0)
    gc.collect()
    assert count() == before, "managed cycles retained native handles"


async def test_custom_task_factory_rejected_before_any_native_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend()
    loop = asyncio.get_running_loop()

    def forbidden_job(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unsupported task factory reached native child launch")

    monkeypatch.setattr(backend, "WindowsProcessJob", forbidden_job)
    monkeypatch.setattr(loop, "get_task_factory", lambda: object())
    with pytest.raises(OSError, match="default task factory"):
        await backend.create_subprocess_exec(*_python("pass"))
