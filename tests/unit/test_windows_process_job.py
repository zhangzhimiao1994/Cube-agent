"""Native Windows Job lifecycle proof, without filesystem or namespace isolation claims.

All payloads are controlled local Python programs, with hidden descendants and
15-second exit limits. Fault injection keeps the real suspended native process.
"""

from __future__ import annotations

import ctypes
import importlib
import importlib.util
import json
import sys
import time
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows Job Object tests")


def _backend() -> Any:
    name = "agent_hub.windows_process_job"
    assert importlib.util.find_spec(name) is not None, "Windows job backend is not implemented"
    return importlib.import_module(name)


def _kernel() -> Any:
    if sys.platform != "win32":
        raise RuntimeError("Windows native monitor unavailable")
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
    kernel.GetHandleInformation.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetHandleInformation.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    return kernel


def _native_error(message: str) -> OSError:
    if sys.platform == "win32":
        return ctypes.WinError(ctypes.get_last_error())
    return OSError(message)


class _ProcessMonitor:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.kernel = _kernel()
        # This independently opened handle is owned by the test, never borrowed from the backend.
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

    def wait_exit(self, timeout_seconds: float = 3) -> int:
        state = self.kernel.WaitForSingleObject(self.handle, int(timeout_seconds * 1000))
        assert state == 0, f"owned native process {self.pid} did not exit: wait state {state}"
        code = self.poll()
        assert code is not None
        return code

    def terminate_if_running(self) -> None:
        if self.poll() is None:
            terminated = self.kernel.TerminateProcess(self.handle, 1)
            if not terminated and self.poll() is None:
                raise _native_error("test rescue TerminateProcess failed")
            self.wait_exit()

    def close(self) -> None:
        if self.handle:
            if not self.kernel.CloseHandle(self.handle):
                raise _native_error("test monitor CloseHandle failed")
            self.handle = 0


def _python(source: str, *args: str) -> tuple[str, ...]:
    # Avoid a venv forwarding executable so the native root PID is the payload interpreter PID.
    executable = Path(str(getattr(sys, "_base_executable", sys.executable))).resolve()
    assert executable.is_absolute() and executable.is_file()
    return (str(executable), "-I", "-u", "-c", source, *args)


def _wait_file(path: Path, timeout_seconds: float = 5) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"controlled payload did not create {path.name}"
        time.sleep(0.01)


def _cleanup_owned(job: Any, release: Path, monitors: list[_ProcessMonitor]) -> None:
    release.touch()
    try:
        if job is not None:
            job.close(timeout_seconds=3)
    finally:
        for monitor in monitors:
            try:
                monitor.terminate_if_running()
            finally:
                monitor.close()


_WAITING_ROOT = """
import ctypes
import json
import os
import pathlib
import sys
import time

ready, release, exit_code = sys.argv[1:]
ready_tmp = pathlib.Path(ready + '.tmp')
ready_tmp.write_text(json.dumps({
    'pid': os.getpid(), 'cwd': os.getcwd(),
    'env': os.environ.get('JOB_TEST_SENTINEL'),
    'console': ctypes.windll.kernel32.GetConsoleWindow(),
}), encoding='utf-8')
ready_tmp.replace(ready)
deadline = time.monotonic() + 15
while not pathlib.Path(release).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
sys.exit(int(exit_code))
"""


@pytest.mark.parametrize("exit_code", [0, 7])
def test_native_root_poll_timeout_exit_and_idempotent_close(tmp_path: Path, exit_code: int) -> None:
    backend = _backend()
    ready, release = tmp_path / "ready", tmp_path / "release"
    monitors: list[_ProcessMonitor] = []
    job: Any = None
    try:
        job = backend.WindowsProcessJob(
            _python(_WAITING_ROOT, str(ready), str(release), str(exit_code)),
            env={"JOB_TEST_SENTINEL": "local-only"},
            cwd=str(tmp_path),
        )
        with job as entered:
            assert entered is job
            _wait_file(ready)
            payload = json.loads(ready.read_text(encoding="utf-8"))
            assert payload == {
                "pid": job.pid,
                "cwd": str(tmp_path),
                "env": "local-only",
                "console": 0,
            }
            monitor = _ProcessMonitor(job.pid)
            monitors.append(monitor)
            assert monitor.poll() is None
            assert job.poll_root() is None
            assert job.active_processes() >= 1
            with pytest.raises(TimeoutError):
                job.wait_root(timeout_seconds=0.02)
            assert monitor.poll() is None, "root wait timeout must not kill the job"
            release.touch()
            assert job.wait_root(timeout_seconds=3) == exit_code
            assert job.poll_root() == exit_code
            assert monitor.wait_exit() == exit_code
        job.close(timeout_seconds=3)
        job.close(timeout_seconds=3)
    finally:
        _cleanup_owned(job, release, monitors)


_GRANDCHILD = """
import os
import pathlib
import sys
import time

ready, release, done = (pathlib.Path(value) for value in sys.argv[1:])
ready_tmp = pathlib.Path(str(ready) + '.tmp')
ready_tmp.write_text(str(os.getpid()), encoding='ascii')
ready_tmp.replace(ready)
deadline = time.monotonic() + 15
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
done.touch()
"""


_CHILD = """
import os
import pathlib
import subprocess
import sys
import time

grand_source, child_pid, child_done, grand_ready, release, grand_done = sys.argv[1:]
subprocess.Popen(
    [sys.executable, '-I', '-u', '-c', grand_source, grand_ready, release, grand_done],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
pid_tmp = pathlib.Path(child_pid + '.tmp')
pid_tmp.write_text(str(os.getpid()), encoding='ascii')
pid_tmp.replace(child_pid)
deadline = time.monotonic() + 15
while not pathlib.Path(release).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
pathlib.Path(child_done).touch()
"""


_EXITING_ROOT = """
import pathlib
import subprocess
import sys
import time

child_source, grand_source, child_pid, child_done, grand_ready, release, grand_done = sys.argv[1:]
subprocess.Popen(
    [sys.executable, '-I', '-u', '-c', child_source, grand_source,
     child_pid, child_done, grand_ready, release, grand_done],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
deadline = time.monotonic() + 5
while not pathlib.Path(child_pid).exists() or not pathlib.Path(grand_ready).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('descendants did not become ready')
    time.sleep(0.01)
"""


def test_native_root_exit_keeps_job_until_close_terminates_child_and_grandchild(
    tmp_path: Path,
) -> None:
    backend = _backend()
    release = tmp_path / "release"
    child_pid, child_done = tmp_path / "child.pid", tmp_path / "child.done"
    grand_ready, grand_done = tmp_path / "grand.pid", tmp_path / "grand.done"
    monitors: list[_ProcessMonitor] = []
    job: Any = None
    try:
        job = backend.WindowsProcessJob(
            _python(
                _EXITING_ROOT,
                _CHILD,
                _GRANDCHILD,
                str(child_pid),
                str(child_done),
                str(grand_ready),
                str(release),
                str(grand_done),
            ),
        )
        _wait_file(child_pid)
        _wait_file(grand_ready)
        for marker in (child_pid, grand_ready):
            monitors.append(_ProcessMonitor(int(marker.read_text(encoding="ascii"))))
        assert job.wait_root(timeout_seconds=3) == 0
        assert job.poll_root() == 0
        assert job.active_processes() >= 2
        assert all(monitor.poll() is None for monitor in monitors)
        assert not child_done.exists() and not grand_done.exists()
        job.close(timeout_seconds=3)
        for monitor in monitors:
            monitor.wait_exit()
        assert not release.exists(), (
            "descendants exited because the job closed, not the release signal"
        )
        assert not child_done.exists() and not grand_done.exists()
        job.close(timeout_seconds=3)
    finally:
        _cleanup_owned(job, release, monitors)


def test_native_context_exit_on_exception_terminates_root(tmp_path: Path) -> None:
    backend = _backend()
    ready, release = tmp_path / "ready", tmp_path / "release"
    monitors: list[_ProcessMonitor] = []
    job: Any = None
    try:
        job = backend.WindowsProcessJob(_python(_WAITING_ROOT, str(ready), str(release), "0"))
        with pytest.raises(ValueError, match="controlled context failure"), job:
            _wait_file(ready)
            monitors.append(_ProcessMonitor(job.pid))
            assert monitors[0].poll() is None
            raise ValueError("controlled context failure")
        monitors[0].wait_exit()
        assert not release.exists()
        job.close(timeout_seconds=3)
    finally:
        _cleanup_owned(job, release, monitors)


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "read_ops",
            "write_ops",
            "other_ops",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _AssignFailure:
    def __init__(self, marker: Path) -> None:
        self.marker = marker
        self.calls: list[tuple[int, int]] = []
        self.limit_flags: list[int] = []
        self.handle_flags: list[int] = []
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, job_handle: int, process_handle: int) -> int:
        kernel = _kernel()
        flags = wintypes.DWORD()
        if not kernel.GetHandleInformation(job_handle, ctypes.byref(flags)):
            raise _native_error("GetHandleInformation failed")
        limits = _ExtendedLimits()
        if not kernel.QueryInformationJobObject(
            job_handle,
            9,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
            None,
        ):
            raise _native_error("QueryInformationJobObject failed")
        self.calls.append((job_handle, process_handle))
        self.handle_flags.append(int(flags.value))
        self.limit_flags.append(int(limits.basic.flags))
        # Let an incorrectly unsuspended interpreter run long enough to expose its marker.
        time.sleep(0.25)
        if sys.platform == "win32":
            ctypes.set_last_error(5)
        return 0


class _KernelFaultProxy:
    def __init__(self, kernel: Any, assign: _AssignFailure) -> None:
        self.kernel = kernel
        self.AssignProcessToJobObject = assign

    def __getattr__(self, name: str) -> Any:
        return getattr(self.kernel, name)


@pytest.mark.parametrize("poll_failure", [False, True])
def test_assign_failure_never_runs_real_suspended_payload_and_reaps_owned_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    poll_failure: bool,
) -> None:
    backend = _backend()
    marker, release = tmp_path / "must-not-run", tmp_path / "release"
    monitors: list[_ProcessMonitor] = []
    borrowed_process_handles: list[int] = []
    create = backend._create_process
    assign = _AssignFailure(marker)
    proxy = _KernelFaultProxy(backend._kernel32(), assign)
    monkeypatch.setattr(backend, "_kernel32", lambda: proxy)
    if poll_failure:

        def unavailable_poll(self: Any) -> int | None:
            raise OSError("controlled root poll failure")

        monkeypatch.setattr(backend.WindowsProcessJob, "poll_root", unavailable_poll)

    def capture_create(
        argv: tuple[str, ...],
        env: Any,
        cwd: str | None,
    ) -> tuple[int, int, int, int]:
        result: tuple[int, int, int, int] = create(argv, env, cwd)
        process_handle, _thread_handle, pid, _thread_id = result
        borrowed_process_handles.append(process_handle)
        monitor = _ProcessMonitor(pid)
        monitors.append(monitor)
        assert monitor.poll() is None
        assert not marker.exists()
        return result

    monkeypatch.setattr(backend, "_create_process", capture_create)
    job: Any = None
    try:
        with pytest.raises((OSError, RuntimeError)):
            job = backend.WindowsProcessJob(
                _python(
                    "import pathlib, time, sys; pathlib.Path(sys.argv[1]).touch(); time.sleep(15)",
                    str(marker),
                ),
            )
        assert len(monitors) == 1, "failure must occur after creating a real native suspended child"
        assert len(assign.calls) == 1, "failure injection must reach native assignment"
        assert assign.calls[0][1] == borrowed_process_handles[0]
        assert assign.limit_flags[0] & 0x2000, "native Job is missing KILL_ON_JOB_CLOSE"
        assert assign.handle_flags[0] & 1 == 0, "Job handle must not be inheritable"
        monitors[0].wait_exit()
        assert not marker.exists(), "failed assignment executed the suspended user payload"
    finally:
        _cleanup_owned(job, release, monitors)


def test_query_failure_never_reports_successful_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend()
    ready, release = tmp_path / "ready", tmp_path / "release"
    monitors: list[_ProcessMonitor] = []
    job: Any = None
    try:
        job = backend.WindowsProcessJob(_python(_WAITING_ROOT, str(ready), str(release), "0"))
        _wait_file(ready)
        monitors.append(_ProcessMonitor(job.pid))

        class FaultProxy:
            def __getattr__(self, name: str) -> Any:
                return getattr(kernel, name)

            def QueryInformationJobObject(self, *args: object) -> int:
                if sys.platform == "win32":
                    ctypes.set_last_error(5)
                return 0

        kernel = job._kernel
        monkeypatch.setattr(job, "_kernel", FaultProxy())
        with pytest.raises(OSError):
            job.close(timeout_seconds=3)
        monitors[0].wait_exit()
        with pytest.raises(OSError, match="cleanup previously incomplete"):
            job.close(timeout_seconds=3)
    finally:
        release.touch()
        if job is not None:
            with suppress(OSError):
                job.close(timeout_seconds=3)
        for monitor in monitors:
            try:
                monitor.terminate_if_running()
            finally:
                monitor.close()


def test_owned_job_handles_return_to_baseline() -> None:
    backend = _backend()
    kernel = _kernel()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessHandleCount.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count() -> int:
        count = wintypes.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count))
        return int(count.value)

    before = handle_count()
    for _ in range(8):
        with backend.WindowsProcessJob(_python("pass")) as job:
            assert job.wait_root(timeout_seconds=3) == 0
    assert handle_count() == before, "native Job/process/thread handles were not released"
