"""Explicit Windows process-family owner; not a stdio runner or security sandbox."""

from __future__ import annotations

import ctypes
import math
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Self


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority", ctypes.c_uint32),
        ("scheduling", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(f"counter_{index}", ctypes.c_uint64) for index in range(6)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("total_user_time", ctypes.c_int64),
        ("total_kernel_time", ctypes.c_int64),
        ("period_user_time", ctypes.c_int64),
        ("period_kernel_time", ctypes.c_int64),
        ("page_faults", ctypes.c_uint32),
        ("total_processes", ctypes.c_uint32),
        ("active_processes", ctypes.c_uint32),
        ("terminated_processes", ctypes.c_uint32),
    ]


def _kernel32() -> Any:
    if sys.platform != "win32":
        raise OSError("Windows Job Objects unavailable on this platform")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ((ctypes.c_void_p, wintypes.LPCWSTR), wintypes.HANDLE),
        "SetHandleInformation": ((wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD), wintypes.BOOL),
        "SetInformationJobObject": (
            (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD),
            wintypes.BOOL,
        ),
        "QueryInformationJobObject": (
            (
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ),
            wintypes.BOOL,
        ),
        "AssignProcessToJobObject": ((wintypes.HANDLE, wintypes.HANDLE), wintypes.BOOL),
        "ResumeThread": ((wintypes.HANDLE,), wintypes.DWORD),
        "TerminateJobObject": ((wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
        "TerminateProcess": ((wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
        "WaitForSingleObject": ((wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD),
        "GetExitCodeProcess": ((wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)), wintypes.BOOL),
        "CloseHandle": ((wintypes.HANDLE,), wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes = arguments
        function.restype = result
    return kernel


def _native_error() -> OSError:
    if sys.platform == "win32":
        return ctypes.WinError(ctypes.get_last_error())
    return OSError("Windows operation unavailable")


def _checked(result: object) -> None:
    if not result:
        raise _native_error()


def _milliseconds(seconds: float) -> int:
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Process timeout must be finite and nonnegative")
    return min(0xFFFFFFFE, math.ceil(seconds * 1000))


def _create_process(
    argv: tuple[str, ...], env: Mapping[str, str] | None, cwd: str | None
) -> tuple[int, int, int, int]:
    if sys.platform != "win32":
        raise OSError("Windows process creation unavailable")
    import _winapi

    # CPython accepts None for inherited environment; typeshed declares only dict.
    create: Callable[..., tuple[int, int, int, int]] = _winapi.CreateProcess
    return create(
        argv[0],
        subprocess.list2cmdline(argv),
        None,
        None,
        False,
        0x4 | subprocess.CREATE_NO_WINDOW,
        None if env is None else dict(env),
        cwd,
        subprocess.STARTUPINFO(),
    )


def _create_process_with_stdio(
    argv: tuple[str, ...],
    env: Mapping[str, str] | None,
    cwd: str | None,
    handles: tuple[int, int, int],
) -> tuple[int, int, int, int]:
    if sys.platform != "win32":
        raise OSError("Windows stdio unavailable")
    import _winapi

    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESTDHANDLES
    startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
    startup.lpAttributeList = {"handle_list": list(dict.fromkeys(handles))}
    create: Callable[..., tuple[int, int, int, int]] = _winapi.CreateProcess
    return create(
        argv[0],
        subprocess.list2cmdline(argv),
        None,
        None,
        True,
        0x4 | subprocess.CREATE_NO_WINDOW,
        None if env is None else dict(env),
        cwd,
        startup,
    )


class WindowsProcessJob:
    """Own a native Job explicitly; callers must close it, including after root exit.

    No async stdio, automatic exit watcher, filesystem restriction or thread safety.
    """

    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdio_handles: tuple[int, int, int] | None = None,
    ) -> None:
        if not argv or any("\0" in argument for argument in argv):
            raise ValueError("Process argv must be nonempty and contain no NUL")
        executable = Path(argv[0])
        if not executable.is_absolute() or executable.drive.startswith("\\\\"):
            raise ValueError("Windows Job executable must be an absolute local path")
        if stdio_handles is not None and (
            len(stdio_handles) != 3
            or any(type(handle) is not int or handle <= 0 for handle in stdio_handles)
        ):
            raise ValueError("Windows stdio requires three valid owned handle references")
        self._kernel = _kernel32()
        self._job_handle: int | None = None
        self._process_handle: int | None = None
        self._thread_handle: int | None = None
        self._root_code: int | None = None
        self._closed = False
        self._close_error: BaseException | None = None
        self.pid = 0
        try:
            handle = self._kernel.CreateJobObjectW(None, None)
            _checked(handle)
            self._job_handle = int(handle)
            _checked(self._kernel.SetHandleInformation(self._job_handle, 1, 0))
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000
            _checked(
                self._kernel.SetInformationJobObject(
                    self._job_handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                )
            )
            process, thread, pid, _thread_id = (
                _create_process(argv, env, cwd)
                if stdio_handles is None
                else _create_process_with_stdio(argv, env, cwd, stdio_handles)
            )
            self._process_handle, self._thread_handle, self.pid = process, thread, pid
            _checked(self._kernel.AssignProcessToJobObject(self._job_handle, process))
            resumed = self._kernel.ResumeThread(thread)
            if resumed == 0xFFFFFFFF:
                raise _native_error()
            if resumed != 1:
                raise OSError("Unexpected Windows primary-thread suspension count")
            self._release_handle("_thread_handle")
        except BaseException:
            self._abort_start()
            raise

    def _release_handle(self, name: str) -> None:
        handle = getattr(self, name)
        if handle is not None:
            _checked(self._kernel.CloseHandle(handle))
            setattr(self, name, None)

    def _release_all(self) -> None:
        errors: list[OSError] = []
        for name in ("_thread_handle", "_process_handle", "_job_handle"):
            try:
                self._release_handle(name)
            except OSError as error:
                errors.append(error)
        if errors:
            raise OSError("Windows Job handle cleanup incomplete") from errors[0]

    def _abort_start(self) -> None:
        errors: list[OSError] = []
        if self._job_handle is not None:
            try:
                _checked(self._kernel.TerminateJobObject(self._job_handle, 1))
            except OSError as error:
                errors.append(error)
        if self._process_handle is not None:
            exited = False
            try:
                exited = self.poll_root() is not None
            except OSError as error:
                errors.append(error)
            if not exited:
                try:
                    _checked(self._kernel.TerminateProcess(self._process_handle, 1))
                except OSError as error:
                    errors.append(error)
            try:
                self.wait_root(timeout_seconds=3)
            except OSError as error:
                errors.append(error)
        try:
            self._release_all()
        except OSError as error:
            errors.append(error)
        if errors:
            raise OSError("Windows Job startup cleanup incomplete") from errors[0]

    def poll_root(self) -> int | None:
        if self._process_handle is None:
            return self._root_code
        state = self._kernel.WaitForSingleObject(self._process_handle, 0)
        if state == 258:
            return None
        if state != 0:
            raise _native_error()
        code = ctypes.c_uint32()
        _checked(self._kernel.GetExitCodeProcess(self._process_handle, ctypes.byref(code)))
        self._root_code = int(code.value)
        return self._root_code

    def wait_root(self, timeout_seconds: float = 3) -> int:
        milliseconds = _milliseconds(timeout_seconds)
        if self._process_handle is None:
            if self._root_code is None:
                raise OSError("Windows Job root handle unavailable")
            return self._root_code
        state = self._kernel.WaitForSingleObject(self._process_handle, milliseconds)
        if state == 258:
            raise TimeoutError("Windows Job root wait timed out")
        if state != 0:
            raise _native_error()
        code = self.poll_root()
        if code is None:
            raise OSError("Windows Job root exit is unconfirmed")
        return code

    def active_processes(self) -> int:
        if self._job_handle is None:
            raise OSError("Windows Job handle unavailable")
        accounting = _Accounting()
        _checked(
            self._kernel.QueryInformationJobObject(
                self._job_handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
            )
        )
        return int(accounting.active_processes)

    @property
    def root_wait_handle(self) -> int:
        """Borrowed native wait handle; its user must never close it."""
        if self._process_handle is None:
            raise OSError("Windows Job root handle unavailable")
        return self._process_handle

    def terminate_members(self) -> None:
        """Signal every Job member without releasing a registered wait handle."""
        if self._job_handle is None:
            raise ProcessLookupError("Windows Job handle unavailable")
        _checked(self._kernel.TerminateJobObject(self._job_handle, 1))

    def close(self, timeout_seconds: float = 3) -> None:
        _milliseconds(timeout_seconds)
        if self._closed:
            if self._close_error is not None:
                raise OSError("Windows Job cleanup previously incomplete") from self._close_error
            return
        deadline = time.monotonic() + timeout_seconds
        try:
            if self._job_handle is not None:
                _checked(self._kernel.TerminateJobObject(self._job_handle, 1))
                while self.active_processes():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Windows Job members did not terminate")
                    time.sleep(min(0.01, remaining))
            self.wait_root(timeout_seconds=max(0, deadline - time.monotonic()))
        except BaseException as error:
            self._close_error = error
            raise
        finally:
            self._release_all()
            self._closed = True

    def __enter__(self) -> Self:
        if self._closed:
            raise OSError("Windows Job is already closed")
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
