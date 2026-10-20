from __future__ import annotations

import ast
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
import tracemalloc
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def phase(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = Path(__file__).parents[4] / "src/agent_hub/skills/sandbox" / "python_project_process.py"
    assert path.is_file(), "Python phase process helper is missing"
    spec = importlib.util.spec_from_file_location("python_phase_process_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_reader_leaks() -> Iterator[None]:
    before = set(threading.enumerate())
    yield
    assert not [
        t for t in threading.enumerate() if t not in before and t.name.startswith("python-phase-")
    ]


def run(phase: ModuleType, cwd: Path, program: str, *, cap: int = 8192, seconds: float = 5) -> Any:
    return phase.run_python_phase(
        [sys.executable, "-I", "-B", "-c", program],
        cwd=cwd,
        deadline=time.monotonic() + seconds,
        capture_limit_bytes=cap,
    )


@pytest.mark.parametrize('budget', [1, 8192, 262144, 1024**2])
def test_capture_and_protocol_budgets_account_for_nested_json_without_execution(phase: ModuleType, budget: int) -> None:
    cap = phase.python_phase_capture_limit(budget)
    assert 4096 <= cap <= 32768
    body = json.dumps({'checks': [{'stdout': '\0' * cap, 'stderr': '\0' * cap} for _ in range(3)]})
    report_limit = phase.python_native_report_limit(budget)
    assert len(body.encode()) < report_limit <= 2 * 1024**2
    envelope = json.dumps({'result': {'stdout': body}})
    assert len(envelope.encode()) < phase.python_root_response_limit(budget) <= 22_000_000


@pytest.mark.parametrize('budget', [True, 0, -1, 1024**2 + 1, '8192'])
def test_output_policy_rejects_coerced_or_unbounded_budgets(phase: ModuleType, budget: object) -> None:
    with pytest.raises(ValueError):
        phase.python_phase_capture_limit(budget)


def test_launcher_does_not_inherit_service_credentials(
    phase: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('AGENT_HUB_FAKE_PROVIDER_TOKEN', 'test-sentinel-not-a-real-secret')
    result = run(phase, tmp_path,
        'import os;print(os.environ.get("AGENT_HUB_FAKE_PROVIDER_TOKEN", "absent"))')
    assert result.exit_code == 0 and result.stdout.splitlines() == ['absent']


@pytest.mark.parametrize("code", [0, 7])
def test_large_concurrent_streams_hide_context_lost_output_but_keep_exit_and_counts(
    phase: ModuleType,
    tmp_path: Path,
    code: int,
) -> None:
    result = run(
        phase,
        tmp_path,
        f"""
import os, threading
def emit(fd, head, tail):
    os.write(fd, head)
    for _ in range(512):
        os.write(fd, b'x' * 4096)
    os.write(fd, tail)
t = threading.Thread(target=emit, args=(1, b'OUT HEAD\\n', b'OUT FINAL\\n'))
t.start()
emit(2, b'ERR HEAD\\n', b'TrueError: final failure\\n')
t.join()
raise SystemExit({code})
""",
    )
    assert result.exit_code == code and not result.timed_out
    assert result.stdout == phase.PYTHON_PHASE_OUTPUT_WITHHELD
    assert result.stderr == phase.PYTHON_PHASE_OUTPUT_WITHHELD
    assert "truncated" in result.stdout and "truncated" in result.stderr
    assert result.stdout_truncated and result.stderr_truncated
    assert len(result.stdout.encode()) <= 8192
    assert len(result.stderr.encode()) <= 8192
    assert result.stdout_observed_bytes == 2097171
    assert result.stderr_observed_bytes == 2097186


@pytest.mark.parametrize("cap", [1, 2, 3, 8, 17, 31, 64, 8192])
def test_utf8_capture_budget_includes_marker(
    phase: ModuleType,
    tmp_path: Path,
    cap: int,
) -> None:
    result = run(
        phase,
        tmp_path,
        """
import os
data = ('\u754c\U0001f600' * 10000).encode('utf-8')
os.write(1, data)
os.write(2, data)
""",
        cap=cap,
    )
    assert result.exit_code == 0 and not result.timed_out
    for stream in ("stdout", "stderr"):
        text = getattr(result, stream)
        assert len(text.encode("utf-8")) <= cap
        assert "\ufffd" not in text
        assert getattr(result, stream + "_truncated")
        assert getattr(result, stream + "_observed_bytes") == 70000
        if cap >= 31:
            assert "truncated" in text


@pytest.mark.parametrize("fd", [1, 2])
def test_pipe_output_never_exposes_context_lost_multiline_value(
    phase: ModuleType, tmp_path: Path, fd: int,
) -> None:
    result = run(phase, tmp_path, f"""
import os
os.write({fd}, b'progress ok\\n' * 4000 + b'API_')
os.write({fd}, b'KEY=|\\n' + b'  synthetic-secret-sentinel\\n' * 6000)
os.write({fd}, b'ActualFailure: mismatch\\n')
raise SystemExit(7)
"""
    )
    assert result.exit_code == 7 and not result.timed_out
    stream = "stdout" if fd == 1 else "stderr"
    assert getattr(result, stream) == phase.PYTHON_PHASE_OUTPUT_WITHHELD
    assert getattr(result, stream + "_truncated") is True
    assert getattr(result, stream + "_observed_bytes") > 8192


@pytest.mark.parametrize(
    ("program", "out", "err"),
    [
        ("pass", "", ""),
        ("import os; os.write(1, b'hello'); os.write(2, b'error')", "hello", "error"),
        ("import os; os.write(1, b'a' * 32)", "a" * 32, ""),
    ],
)
def test_natural_exit_preserves_small_and_exact_budget_output(
    phase: ModuleType,
    tmp_path: Path,
    program: str,
    out: str,
    err: str,
) -> None:
    result = run(phase, tmp_path, program, cap=32)
    assert result.exit_code == 0 and not result.timed_out
    assert (result.stdout, result.stderr) == (out, err)
    assert not result.stdout_truncated and not result.stderr_truncated
    assert (result.stdout_observed_bytes, result.stderr_observed_bytes) == (len(out), len(err))
    with pytest.raises(FrozenInstanceError):
        result.exit_code = 9


def test_invalid_utf8_still_has_a_byte_budget(phase: ModuleType, tmp_path: Path) -> None:
    result = run(phase, tmp_path, "import os; os.write(1, b'\\xff' * 4096)", cap=32)
    assert result.exit_code == 0 and not result.timed_out
    assert result.stdout_observed_bytes == 4096 and result.stdout_truncated
    assert len(result.stdout.encode()) <= 32


def test_timeout_after_output_overflow_keeps_draining(
    phase: ModuleType,
    tmp_path: Path,
) -> None:
    started = time.monotonic()
    result = run(
        phase,
        tmp_path,
        """
import os, time
os.write(1, b'x' * 100000)
os.write(2, b'y' * 100000)
os.write(2, b'LAST ERROR')
time.sleep(60)
""",
        seconds=1.5,
    )
    assert result.timed_out and result.exit_code != 0
    assert result.stdout_observed_bytes == 100000
    assert result.stderr_observed_bytes == 100010
    assert result.stderr == phase.PYTHON_PHASE_OUTPUT_WITHHELD
    assert time.monotonic() - started < 2.5


def test_closed_pipes_do_not_hide_a_live_process(phase: ModuleType, tmp_path: Path) -> None:
    result = run(
        phase, tmp_path, "import os, time; os.close(1); os.close(2); time.sleep(60)", seconds=1
    )
    assert result.timed_out and result.exit_code != 0


def test_expired_deadline_never_launches(phase: ModuleType, tmp_path: Path) -> None:
    result = phase.run_python_phase(
        [str(tmp_path / "must-not-exist")],
        cwd=tmp_path,
        deadline=time.monotonic() - 1,
        capture_limit_bytes=1,
    )
    assert result.exit_code is None and result.timed_out
    assert (result.stdout, result.stderr) == ("", "")
    assert (result.stdout_observed_bytes, result.stderr_observed_bytes) == (0, 0)


def test_capture_memory_does_not_grow_with_total_output(
    phase: ModuleType,
    tmp_path: Path,
) -> None:
    tracemalloc.start()
    try:
        result = run(
            phase,
            tmp_path,
            """
import os
for _ in range(4096):
    os.write(1, b'x' * 16384)
""",
            seconds=15,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.exit_code == 0 and not result.timed_out
    assert result.stdout_observed_bytes == 67108864
    assert peak < 4 * 1024 * 1024


@pytest.mark.parametrize(
    ("argument", "bad"),
    [
        ("command", []),
        ("command", "python"),
        ("command", [1]),
        ("command", [""]),
        ("command", ["a\x00b"]),
        ("cwd", "."),
        ("deadline", float("inf")),
        ("deadline", float("nan")),
        ("deadline", True),
        ("deadline", "tomorrow"),
        ("deadline", 0),
        ("capture_limit_bytes", 0),
        ("capture_limit_bytes", -1),
        ("capture_limit_bytes", True),
        ("capture_limit_bytes", 1.5),
        ("capture_limit_bytes", float("inf")),
        ("pass_fds", [1]),
        ("pass_fds", (-1,)),
        ("pass_fds", (True,)),
    ],
)
def test_invalid_inputs_are_rejected_before_launch(
    phase: ModuleType,
    tmp_path: Path,
    argument: str,
    bad: object,
) -> None:
    args: dict[str, Any] = {
        "command": [str(tmp_path / "missing")],
        "cwd": tmp_path,
        "deadline": time.monotonic() + 2,
        "capture_limit_bytes": 32,
    }
    args[argument] = bad
    with pytest.raises((TypeError, ValueError)):
        phase.run_python_phase(**args)


def test_launch_error_propagates(phase: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(OSError):
        phase.run_python_phase(
            [str(tmp_path / "missing")],
            cwd=tmp_path,
            deadline=time.monotonic() + 2,
            capture_limit_bytes=32,
        )


def track_processes(
    phase: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> list[subprocess.Popen[bytes]]:
    original = phase.subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process: subprocess.Popen[bytes] = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(phase.subprocess, "Popen", launch)
    return processes


def assert_closed(process: subprocess.Popen[bytes]) -> None:
    assert process.poll() is not None
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed


def test_cancellation_reaps_and_closes_pipes(
    phase: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = track_processes(phase, monkeypatch)
    original = time.sleep
    interrupted = False

    def sleep(seconds: float) -> None:
        nonlocal interrupted
        if threading.current_thread() is threading.main_thread() and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        original(seconds)

    monkeypatch.setattr(phase.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        run(phase, tmp_path, "import time; time.sleep(60)")
    assert len(processes) == 1
    assert_closed(processes[0])


def test_cancellation_during_reader_join_still_closes_pipes(
    phase: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = track_processes(phase, monkeypatch)
    original = threading.Thread.join
    interrupted = False

    def join(thread: threading.Thread, timeout: float | None = None) -> None:
        nonlocal interrupted
        original(thread, timeout=timeout)
        if thread.name.startswith("python-phase-") and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(phase.threading.Thread, "join", join)
    with pytest.raises(KeyboardInterrupt):
        run(phase, tmp_path, "pass")
    assert interrupted
    assert_closed(processes[0])


def test_cancellation_during_pipe_close_still_closes_other_pipe(
    phase: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = track_processes(phase, monkeypatch)
    original_launch = phase.subprocess.Popen

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process: subprocess.Popen[bytes] = original_launch(*args, **kwargs)
        assert process.stdout is not None
        real_close = process.stdout.close

        def close() -> None:
            real_close()
            raise KeyboardInterrupt

        monkeypatch.setattr(process.stdout, "close", close)
        return process

    monkeypatch.setattr(phase.subprocess, "Popen", launch)
    with pytest.raises(KeyboardInterrupt):
        run(phase, tmp_path, "pass")
    assert_closed(processes[0])


def test_reader_error_is_not_success_and_resources_close(
    phase: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = track_processes(phase, monkeypatch)
    original = os.read

    def read(descriptor: int, count: int) -> bytes:
        if threading.current_thread().name.startswith("python-phase-"):
            raise OSError("injected pipe failure")
        return original(descriptor, count)

    monkeypatch.setattr(phase.os, "read", read)
    with pytest.raises(phase.PythonPhaseCleanupError):
        run(phase, tmp_path, "import time; time.sleep(60)")
    assert_closed(processes[0])


def test_partial_reader_start_failure_reaps_and_closes_pipes(
    phase: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = track_processes(phase, monkeypatch)
    original = threading.Thread.start
    started = 0

    def start(thread: threading.Thread) -> None:
        nonlocal started
        if thread.name.startswith("python-phase-"):
            started += 1
            if started == 2:
                raise RuntimeError("injected thread start failure")
        original(thread)

    monkeypatch.setattr(phase.threading.Thread, "start", start)
    with pytest.raises(RuntimeError, match="injected thread start failure"):
        run(phase, tmp_path, "import time; time.sleep(60)")
    assert_closed(processes[0])


def test_exit_zero_without_observed_eof_raises_cleanup_error(
    phase: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = track_processes(phase, monkeypatch)
    original = os.read

    def read(descriptor: int, count: int) -> bytes:
        if threading.current_thread().name.startswith("python-phase-"):
            raise BlockingIOError
        return original(descriptor, count)

    monkeypatch.setattr(phase.os, "read", read)
    with pytest.raises(phase.PythonPhaseCleanupError, match="pipe_eof_unconfirmed"):
        run(phase, tmp_path, "pass", seconds=0.5)
    assert processes[0].returncode == 0
    assert_closed(processes[0])


def test_unconfirmed_reap_is_cleanup_error_even_after_pipes_close(
    phase: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = track_processes(phase, monkeypatch)
    original_launch = phase.subprocess.Popen

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process: subprocess.Popen[bytes] = original_launch(*args, **kwargs)
        real_wait = process.wait

        def wait(timeout: float | None = None) -> int:
            assert timeout is not None
            real_wait(timeout=timeout)
            raise subprocess.TimeoutExpired("fixed-test-command", timeout)

        monkeypatch.setattr(process, "wait", wait)
        return process

    monkeypatch.setattr(phase.subprocess, "Popen", launch)
    with pytest.raises(phase.PythonPhaseCleanupError, match="process_reap_failed"):
        run(phase, tmp_path, "pass")
    assert_closed(processes[0])


def test_nonblocking_setup_failure_closes_both_pipes(
    phase: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = track_processes(phase, monkeypatch)
    original = os.set_blocking
    calls = 0

    def set_blocking(fd: int, blocking: bool) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected nonblocking setup failure")
        original(fd, blocking)

    monkeypatch.setattr(phase.os, "set_blocking", set_blocking)
    with pytest.raises(OSError, match="injected nonblocking setup failure"):
        run(phase, tmp_path, "import time; time.sleep(60)")
    assert_closed(processes[0])


def test_unresponsive_reader_raises_with_bounded_join(
    phase: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = track_processes(phase, monkeypatch)
    original = os.read
    release = threading.Event()

    def read(fd: int, count: int) -> bytes:
        name = threading.current_thread().name
        if name.startswith("python-phase-") and name.endswith("-0"):
            release.wait(timeout=3)
            return b""
        return original(fd, count)

    monkeypatch.setattr(phase.os, "read", read)
    started = time.monotonic()
    try:
        with pytest.raises(phase.PythonPhaseCleanupError, match="pipe_thread_cleanup_failed"):
            run(phase, tmp_path, "pass", seconds=0.5)
        assert time.monotonic() - started < 1.5
        assert_closed(processes[0])
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name.startswith("python-phase-"):
                thread.join(timeout=1)


def test_explicit_capture_budget_can_exceed_eight_kib(
    phase: ModuleType,
    tmp_path: Path,
) -> None:
    result = run(phase, tmp_path, "import os; os.write(1, b'x' * 65536)", cap=65536)
    assert result.exit_code == 0 and not result.timed_out
    assert result.stdout == "x" * 65536
    assert not result.stdout_truncated and result.stdout_observed_bytes == 65536


def test_late_exit_observation_cannot_pass_after_deadline(
    phase: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = track_processes(phase, monkeypatch)
    original_launch = phase.subprocess.Popen
    delayed = False

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process: subprocess.Popen[bytes] = original_launch(*args, **kwargs)
        real_poll = process.poll

        def poll() -> int | None:
            nonlocal delayed
            code = real_poll()
            if code is not None and not delayed:
                delayed = True
                time.sleep(0.6)
            return code

        monkeypatch.setattr(process, "poll", poll)
        return process

    monkeypatch.setattr(phase.subprocess, "Popen", launch)
    result = run(phase, tmp_path, "pass", seconds=0.5)
    assert delayed
    assert result.timed_out
    assert_closed(processes[0])


def test_helper_imports_only_stdlib(phase: ModuleType) -> None:
    assert phase.__file__ is not None
    tree = ast.parse(Path(phase.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(item.name.split(".")[0] in sys.stdlib_module_names for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            assert node.module.split(".")[0] in sys.stdlib_module_names


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process group cleanup")
@pytest.mark.parametrize("parent_exits", [True, False])
def test_timeout_kills_same_group_child_even_after_parent_zero(
    phase: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_exits: bool,
) -> None:
    if sys.platform != "linux":
        return
    processes = track_processes(phase, monkeypatch)
    try:
        result = run(
            phase,
            tmp_path,
            f"""
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'import time; time.sleep(60)'])
print(child.pid, flush=True)
if {parent_exits}:
    os._exit(0)
time.sleep(60)
""",
            seconds=1,
        )
        assert result.timed_out
        assert result.exit_code == (0 if parent_exits else -signal.SIGKILL)
        assert_closed(processes[0])
        pid = int(result.stdout.strip())
        # An orphan may await init's reap, but must no longer be running.
        status = Path(f"/proc/{pid}/stat")
        stop_at = time.monotonic() + 0.5
        while True:
            try:
                state = status.read_text().split(") ", 1)[1]
            except FileNotFoundError:
                break
            if state.startswith("Z "):
                break
            assert time.monotonic() < stop_at, "same-group child is still running"
            time.sleep(0.01)
    finally:
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX inherited descriptor")
def test_pass_fds_remains_owned_by_caller(phase: ModuleType, tmp_path: Path) -> None:
    with (tmp_path / "trusted-input").open("w+b") as stream:
        stream.write(b"trusted")
        stream.seek(0)
        result = phase.run_python_phase(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                f"import os; os.write(1, os.read({stream.fileno()}, 7))",
            ],
            cwd=tmp_path,
            deadline=time.monotonic() + 3,
            capture_limit_bytes=32,
            pass_fds=(stream.fileno(),),
        )
        assert result.exit_code == 0 and result.stdout == "trusted"
        assert not stream.closed
