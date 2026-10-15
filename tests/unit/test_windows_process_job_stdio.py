from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

import pytest

from agent_hub.windows_process_job import WindowsProcessJob

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows stdio handles")


def test_native_job_inherits_only_explicit_stdio_handles() -> None:
    assert "stdio_handles" in inspect.signature(WindowsProcessJob).parameters
    if sys.platform != "win32":
        pytest.skip("Windows handle conversion")
    import msvcrt

    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    error_read, error_write = os.pipe()
    fds = (input_read, input_write, output_read, output_write, error_read, error_write)
    source = (
        "import os, sys, threading; "
        "timer=threading.Timer(5, lambda: os._exit(9)); timer.daemon=True; timer.start(); "
        "data=sys.stdin.buffer.read(5); "
        "sys.stdout.buffer.write(data+b'!'); sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(b'error'); sys.stderr.buffer.flush(); timer.cancel()"
    )
    handles = (
        msvcrt.get_osfhandle(input_read),
        msvcrt.get_osfhandle(output_write),
        msvcrt.get_osfhandle(error_write),
    )
    job: WindowsProcessJob | None = None
    try:
        for handle in handles:
            os.set_handle_inheritable(handle, True)
        executable = str(Path(str(getattr(sys, "_base_executable", sys.executable))).resolve())
        job = WindowsProcessJob((executable, "-I", "-u", "-c", source), stdio_handles=handles)
        os.write(input_write, b"hello")
        assert os.read(output_read, 6) == b"hello!"
        assert os.read(error_read, 5) == b"error"
        assert job.wait_root(timeout_seconds=3) == 0
    finally:
        if job is not None:
            job.close()
        for fd in fds:
            os.close(fd)
