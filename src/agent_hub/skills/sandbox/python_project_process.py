"""Bounded output collection for caller-authorized, fixed Python phase launchers.

This helper grants no execution authority: the caller supplies trusted bwrap argv,
cwd and budgets, never model arguments. POSIX process groups are best effort;
the real bwrap PID namespace and outer cgroup own all-descendant containment.
Windows supports direct-child unit tests only, not a native sandbox.

Output is bounded, lossy UTF-8 diagnostic metadata, not complete logs or proof
of business correctness. Observed byte counts cover bytes actually read, before
decoding. Overflow discards the entire stream preview: retaining a suffix could
lose the sensitive label or multiline context needed by downstream redaction.
No project code should be run on the host through this helper.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

_READ_BYTES = 16384
_POLL_SECONDS = 0.001
PYTHON_PHASE_OUTPUT_WITHHELD = "[truncated output withheld: context lost]\n"


def python_phase_capture_limit(output_limit_bytes: int) -> int:
    """Retain complete bounded streams; overflow never kills a phase."""
    if type(output_limit_bytes) is not int or not 1 <= output_limit_bytes <= 1024**2:
        raise ValueError('python_project_output_budget_invalid')
    return max(4096, min(32768, output_limit_bytes // 8))


def python_native_report_limit(output_limit_bytes: int) -> int:
    # Three phases, two streams, JSON can encode a control byte as six ASCII bytes.
    return 65536 + 3 * 2 * 6 * python_phase_capture_limit(output_limit_bytes)


def python_root_response_limit(output_limit_bytes: int) -> int:
    # Root wraps native JSON as a SkillResult string, then a response envelope.
    return 65536 + 6 * python_native_report_limit(output_limit_bytes)


class PythonPhaseCleanupError(RuntimeError):
    """A process/pipe failure or cleanup that could not be confirmed."""


def _launcher_environment() -> dict[str, str]:
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
    if sys.platform == 'win32' and 'SYSTEMROOT' in os.environ:
        env['SYSTEMROOT'] = os.environ['SYSTEMROOT']
    return env


@dataclass(frozen=True)
class PythonPhaseProcessResult:
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_observed_bytes: int
    stderr_observed_bytes: int


class _Capture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.observed = 0
        self.head = bytearray()
        self.tail = bytearray()
        self.head_limit = (limit + 1) // 2
        self.tail_limit = limit // 2

    def feed(self, chunk: bytes) -> None:
        self.observed += len(chunk)
        if self.truncated:
            # Even complete tail lines can belong to a discarded sensitive value.
            self.head.clear()
            self.tail.clear()
            return
        take = min(len(chunk), self.head_limit - len(self.head))
        self.head.extend(chunk[:take])
        rest = chunk[take:]
        if not rest or not self.tail_limit:
            return
        if len(rest) >= self.tail_limit:
            self.tail[:] = rest[-self.tail_limit :]
        else:
            discard = max(0, len(self.tail) + len(rest) - self.tail_limit)
            del self.tail[:discard]
            self.tail.extend(rest)

    @property
    def truncated(self) -> bool:
        return self.observed > self.limit

    def text(self) -> str:
        if not self.truncated:
            return (self.head + self.tail).decode("utf-8", errors="ignore")
        return PYTHON_PHASE_OUTPUT_WITHHELD[:self.limit]


class _Reader:
    def __init__(self, pipe: IO[bytes], capture: _Capture, stopped: threading.Event) -> None:
        self.descriptor = pipe.fileno()
        self.capture = capture
        self.stopped = stopped
        self.done = threading.Event()
        self.eof = False
        self.error: BaseException | None = None

    def run(self) -> None:
        # Nonblocking reads make stopping independent of inherited write ends.
        # Each reader owns one bounded capture; there is no output event queue.
        try:
            while not self.stopped.is_set():
                try:
                    chunk = os.read(self.descriptor, _READ_BYTES)
                except BlockingIOError:
                    # sleep uses Windows' high-resolution timer; Event.wait can
                    # round a short idle wait to a full scheduler tick.
                    time.sleep(_POLL_SECONDS)
                    continue
                if not chunk:
                    self.eof = True
                    return
                self.capture.feed(chunk)
        except BaseException as exc:  # noqa: BLE001 - report every reader failure to its owner
            self.error = exc
        finally:
            self.done.set()


def _validate(
    command: Sequence[str],
    cwd: Path,
    deadline: float,
    capture_limit_bytes: int,
    pass_fds: tuple[int, ...],
) -> None:
    if (
        not isinstance(command, Sequence)
        or isinstance(command, (str, bytes))
        or not command
        or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
        or not command[0]
    ):
        raise ValueError("command must be a nonempty sequence of string arguments")
    if not isinstance(cwd, Path):
        raise TypeError("cwd must be a Path")
    if type(deadline) not in (int, float):
        raise TypeError("deadline must be a finite positive monotonic timestamp")
    try:
        valid_deadline = math.isfinite(deadline) and deadline > 0
    except OverflowError:
        valid_deadline = False
    if not valid_deadline:
        raise ValueError("deadline must be a finite positive monotonic timestamp")
    if type(capture_limit_bytes) is not int or capture_limit_bytes <= 0:
        raise ValueError("capture_limit_bytes must be a positive integer")
    if type(pass_fds) is not tuple or any(type(fd) is not int or fd < 0 for fd in pass_fds):
        raise ValueError("pass_fds must be a tuple of nonnegative file descriptors")
    if os.name == "nt" and pass_fds:
        raise ValueError("pass_fds is supported only on POSIX")


def _cleanup(
    process: subprocess.Popen[bytes],
    pipes: tuple[IO[bytes], ...],
    readers: list[_Reader],
    threads: list[threading.Thread],
    stopped: threading.Event,
    drain_until: float,
    deadline: float,
) -> None:
    failures: list[str] = []
    try:
        if sys.platform != "win32":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                failures.append("process_group_cleanup_failed")
        try:
            if process.poll() is None:
                process.kill()
        except ProcessLookupError:
            pass
        except OSError:
            failures.append("process_kill_failed")
        try:
            process.wait(timeout=max(0, drain_until - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired):
            failures.append("process_reap_failed")
        started = [
            (reader, thread)
            for reader, thread in zip(readers, threads, strict=True)
            if thread.ident is not None
        ]
        while any(not reader.done.is_set() for reader, _ in started):
            remaining = drain_until - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_POLL_SECONDS, remaining))
    finally:
        stopped.set()
        cleanup_cancelled: KeyboardInterrupt | SystemExit | None = None
        for thread in threads:
            if thread.ident is not None:
                try:
                    thread.join(timeout=max(0, deadline - time.monotonic()))
                except (KeyboardInterrupt, SystemExit) as exc:
                    cleanup_cancelled = exc
                except RuntimeError:
                    failures.append("pipe_join_failed")
        # Only unbuffered, nonblocking pipe handles are owned by this helper.
        # Caller-owned pass_fds are never closed here.
        for pipe in pipes:
            try:
                pipe.close()
            except (KeyboardInterrupt, SystemExit) as exc:
                cleanup_cancelled = exc
            except OSError:
                failures.append("pipe_close_failed")
        if any(not pipe.closed for pipe in pipes):
            failures.append("pipe_close_unconfirmed")
        if any(thread.is_alive() for thread in threads):
            failures.append("pipe_thread_cleanup_failed")
        if process.returncode is None:
            failures.append("process_reap_unconfirmed")
        for reader, thread in zip(readers, threads, strict=True):
            if reader.error is not None:
                failures.append("pipe_read_failed")
            elif thread.ident is not None and not reader.eof:
                failures.append("pipe_eof_unconfirmed")
        if failures:
            raise PythonPhaseCleanupError(", ".join(failures))
        if cleanup_cancelled is not None:
            raise cleanup_cancelled


def run_python_phase(
    command: Sequence[str],
    *,
    cwd: Path,
    deadline: float,
    capture_limit_bytes: int,
    pass_fds: tuple[int, ...] = (),
) -> PythonPhaseProcessResult:
    """Drain until natural exit plus both EOFs, or the absolute monotonic budget.

    Both stream budgets include the context-loss replacement. Capture exhaustion
    hides the entire stream preview but never kills the process. Reserve up to
    200 ms of the supplied budget for kill/reap
    and draining, including a final bounded reader join. Cleanup failures raise
    even if the launcher exited zero. Launch errors and cancellation propagate
    after owned resources are cleaned up. Python >= 3.12 is required on Windows
    for nonblocking pipes; this does not implement sandbox authorization.
    """
    _validate(command, cwd, deadline, capture_limit_bytes, pass_fds)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return PythonPhaseProcessResult(None, True, "", "", False, False, 0, 0)
    stop_at = deadline - min(0.2, remaining / 5)
    drain_until = deadline - min(0.05, remaining / 20)
    captures = [_Capture(capture_limit_bytes), _Capture(capture_limit_bytes)]
    stopped = threading.Event()
    readers: list[_Reader] = []
    threads: list[threading.Thread] = []
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=_launcher_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        shell=False,
        close_fds=True,
        start_new_session=os.name == "posix",
        pass_fds=pass_fds,
    )
    pipes = tuple(pipe for pipe in (process.stdout, process.stderr) if pipe is not None)
    timed_out = False
    try:
        for index, pipe in enumerate(pipes):
            os.set_blocking(pipe.fileno(), False)
            reader = _Reader(pipe, captures[index], stopped)
            readers.append(reader)
            threads.append(
                threading.Thread(
                    target=reader.run, name=f"python-phase-{process.pid}-{index}", daemon=True
                )
            )
        for thread in threads:
            thread.start()
        while True:
            if any(reader.error is not None for reader in readers):
                raise PythonPhaseCleanupError("pipe_read_failed")
            finished = process.poll() is not None and all(
                reader.done.is_set() for reader in readers
            )
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if finished:
                break
            time.sleep(min(_POLL_SECONDS, remaining))
    finally:
        _cleanup(process, pipes, readers, threads, stopped, drain_until, deadline)
    return PythonPhaseProcessResult(
        process.returncode,
        timed_out,
        captures[0].text(),
        captures[1].text(),
        captures[0].truncated,
        captures[1].truncated,
        captures[0].observed,
        captures[1].observed,
    )
