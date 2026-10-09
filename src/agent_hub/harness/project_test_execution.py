"""Bounded, isolated evidence that a supported project's test bodies actually run."""

from __future__ import annotations

import json
import math
import os
import shlex
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from queue import Empty, Queue
from uuid import uuid4

from agent_hub.harness.project_validation_sandbox import generated_command

_COPY_MAX_ENTRIES = 100_000
_COPY_MAX_BYTES = 1_073_741_824
_REPORT_MAX_BYTES = 4096
_REPORTER_PATH = "/opt/validator/project-test-reporter.cjs"
_PROCESS_ENV = {"PATH": "/usr/bin:/bin"}


class _Reason(StrEnum):
    UNSUPPORTED = (
        "requirements: test execution unsupported runner; use named node:test cases with node --test"
    )
    NO_TESTS = "requirements: test execution no completed named tests"
    BASELINE = "requirements: test execution baseline failed"
    CANARY = "requirements: test execution canary body was not verified"
    COPY = "requirements: test execution unsafe or unavailable disposable copy"
    REPORT = "requirements: test execution invalid runner report"
    DEADLINE = "requirements: test execution deadline exceeded"
    UNAVAILABLE = "requirements: test execution unavailable"
    CLEANUP = "requirements: test execution temporary runtime cleanup failed"


class _ProofFailure(Exception):
    def __init__(self, reason: _Reason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ProofFailure(_Reason.DEADLINE)
    return remaining


def _copy_project(root: Path, destination: Path, deadline: float) -> None:
    source_root = root.resolve(strict=True)
    if root.is_symlink() or not source_root.is_dir():
        raise _ProofFailure(_Reason.COPY)
    entries = 0
    copied_bytes = 0

    def copy_directory(source: Path, target: Path, ancestors: frozenset[Path]) -> None:
        nonlocal entries, copied_bytes
        _remaining(deadline)
        resolved = source.resolve(strict=True)
        if not resolved.is_relative_to(source_root) or resolved in ancestors:
            raise _ProofFailure(_Reason.COPY)
        target.mkdir()
        with os.scandir(resolved) as children:
            for child in children:
                _remaining(deadline)
                entries += 1
                if entries > _COPY_MAX_ENTRIES:
                    raise _ProofFailure(_Reason.COPY)
                original = Path(child.path).resolve(strict=True)
                if not original.is_relative_to(source_root):
                    raise _ProofFailure(_Reason.COPY)
                metadata = original.stat()
                output = target / child.name
                if stat.S_ISDIR(metadata.st_mode):
                    copy_directory(original, output, ancestors | {resolved})
                elif stat.S_ISREG(metadata.st_mode):
                    with original.open("rb") as incoming, output.open("xb") as outgoing:
                        opened = os.fstat(incoming.fileno())
                        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                            raise _ProofFailure(_Reason.COPY)
                        cloned = os.fstat(outgoing.fileno())
                        if (opened.st_dev, opened.st_ino) == (cloned.st_dev, cloned.st_ino):
                            raise _ProofFailure(_Reason.COPY)
                        file_bytes = 0
                        while chunk := incoming.read(65_536):
                            _remaining(deadline)
                            file_bytes += len(chunk)
                            copied_bytes += len(chunk)
                            if copied_bytes > _COPY_MAX_BYTES:
                                raise _ProofFailure(_Reason.COPY)
                            outgoing.write(chunk)
                        if file_bytes != metadata.st_size:
                            raise _ProofFailure(_Reason.COPY)
                    output.chmod(stat.S_IMODE(metadata.st_mode))
                else:
                    raise _ProofFailure(_Reason.COPY)

    copy_directory(source_root, destination, frozenset())


def _node_command(root: Path) -> tuple[str, ...]:
    with (root / "package.json").open("rb") as manifest:
        raw = manifest.read(65_537)
    if len(raw) > 65_536:
        raise _ProofFailure(_Reason.UNSUPPORTED)
    payload = json.loads(raw)
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    script = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(script, str):
        raise _ProofFailure(_Reason.UNSUPPORTED)
    # Build already ran in outer validation; only replay tests in the disposable copy.
    script = script.removeprefix("npm run build && ")
    command = shlex.split(script)
    if command[:2] != ["node", "--test"]:
        raise _ProofFailure(_Reason.UNSUPPORTED)
    paths: list[str] = []
    for pattern in command[2:]:
        if (
            pattern.startswith("-") or Path(pattern).is_absolute()
            or ".." in Path(pattern).parts
            or any(character in pattern for character in "\\:$`;|&<>")
        ):
            raise _ProofFailure(_Reason.UNSUPPORTED)
        matches = sorted(root.glob(pattern))
        if not matches or len(matches) + len(paths) > 2000:
            raise _ProofFailure(_Reason.UNSUPPORTED)
        for path in matches:
            if not path.is_file() or path.suffix not in {".js", ".cjs", ".mjs"}:
                raise _ProofFailure(_Reason.UNSUPPORTED)
            paths.append(path.relative_to(root).as_posix())
    return ("node", "--test", *dict.fromkeys(paths))


def _reporter_source(name: str, body: str, filename: str) -> str:
    return (
        "const expected = " + json.dumps({"name": name, "body": body, "file": filename}) + ";\n"
        "module.exports = async function* (events) {\n"
        "let passed = 0, failed = 0, canary = false;\n"
        "for await (const event of events) {\n"
        "const d = event.data;\n"
        "if (!d || (d.details?.type !== undefined && d.details.type !== 'test')"
        " || !d.file || !d.line || !d.column"
        " || d.skip || d.todo) continue;\n"
        "const file = d.file.replaceAll('\\\\', '/');\n"
        "const testName = d.name.replaceAll('\\\\', '/');\n"
        "if (file === testName || file.endsWith('/' + testName)) continue;\n"
        "if (event.type === 'test:pass') passed++;\n"
        "if (event.type === 'test:fail') { failed++;\n"
        "const error = d.details?.error;\n"
        "if (d.name === expected.name && file.endsWith('/' + expected.file)"
        " && error?.failureType === 'testCodeFailure' && error.cause?.code === 'ERR_ASSERTION'"
        " && error.cause.message === expected.body) canary = true;\n"
        "}\n"
        "if (passed + failed > 50000) throw Error('bounded test report exceeded');\n"
        "}\n"
        "yield JSON.stringify({schema:1, passed, failed, canary}) + '\\n';\n"
        "};\n"
    )


def _test_command(
    root: Path, command: Sequence[str], reporter: Path, config: Mapping[str, str]
) -> list[str]:
    inner = ("node", "--test", f"--test-reporter=file://{_REPORTER_PATH}", *command[2:])
    argv = generated_command(inner, cwd=root, config=config)
    boundary = argv.index("--")
    mounts = ["--ro-bind", str(reporter), _REPORTER_PATH]
    canary_name = reporter.name.removesuffix(".reporter.cjs") + ".test.cjs"
    canary = root / "test" / canary_name
    if canary.is_file():
        mounts.extend(("--ro-bind", str(canary), f"/workspace/test/{canary_name}"))
    # Neither generated source nor its subprocesses may replace the reporter or canary.
    return [
        *argv[:boundary], *mounts, *argv[boundary:],
    ]


def _run_report(
    root: Path, command: Sequence[str], reporter: Path,
    config: Mapping[str, str], deadline: float,
) -> tuple[int, dict[str, object]]:
    argv = _test_command(root, command, reporter, config)
    process = subprocess.Popen(
        argv, cwd=root, env=_PROCESS_ENV, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
    )
    stream = process.stdout
    if stream is None:
        process.kill()
        process.wait(timeout=1)
        raise _ProofFailure(_Reason.UNAVAILABLE)
    captured: Queue[bytes | None] = Queue(maxsize=1)

    def collect() -> None:
        bounded = bytearray()
        try:
            while len(bounded) <= _REPORT_MAX_BYTES:
                chunk = stream.read(_REPORT_MAX_BYTES + 1 - len(bounded))
                if not chunk:
                    break
                bounded.extend(chunk)
            captured.put(bytes(bounded))
        except OSError:
            captured.put(None)

    reader = threading.Thread(target=collect, daemon=True)
    reader_started = False
    try:
        reader.start()
        reader_started = True
        try:
            raw = captured.get(timeout=_remaining(deadline))
        except Empty:
            raise _ProofFailure(_Reason.DEADLINE) from None
        if raw is None:
            raise _ProofFailure(_Reason.REPORT)
        if len(raw) > _REPORT_MAX_BYTES:
            raise _ProofFailure(_Reason.REPORT)
        try:
            code = process.wait(timeout=_remaining(deadline))
        except subprocess.TimeoutExpired:
            raise _ProofFailure(_Reason.DEADLINE) from None
        _remaining(deadline)
    finally:
        # Killing bwrap terminates its PID namespace; cleanup never waits indefinitely.
        cleanup_failed = False
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            cleanup_failed = True
        if reader_started:
            reader.join(timeout=1)
        if reader.is_alive():
            cleanup_failed = True
        else:
            stream.close()
        if cleanup_failed:
            raise _ProofFailure(_Reason.CLEANUP)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise _ProofFailure(_Reason.REPORT) from None
    if (
        not isinstance(payload, dict) or set(payload) != {"schema", "passed", "failed", "canary"}
        or type(payload["schema"]) is not int or payload["schema"] != 1
        or type(payload["canary"]) is not bool
        or any(type(payload[key]) is not int or not 0 <= payload[key] <= 50_000
               for key in ("passed", "failed"))
    ):
        raise _ProofFailure(_Reason.REPORT)
    return code, payload


def verify_project_test_execution(
    root: Path, *, timeout_seconds: float, absolute_deadline: float,
    config: Mapping[str, str],
) -> tuple[str, ...]:
    """Return only fixed failure reasons; no model, install, original-root write or raw output."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or math.isnan(absolute_deadline):
        return (_Reason.DEADLINE.value,)
    deadline = min(absolute_deadline, time.monotonic() + timeout_seconds)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    reason: _Reason | None = None
    try:
        _remaining(deadline)
        temporary = tempfile.TemporaryDirectory(prefix="agent-hub-test-proof-")
        parent = Path(temporary.name)
        project = parent / "project"
        _copy_project(root, project, deadline)
        command = _node_command(project)
        token = uuid4().hex
        name, body = f"agent-hub-canary-{token}", f"agent-hub-body-{token}"
        canary_name = f"agent-hub-{token}.test.cjs"
        reporter = parent / f"agent-hub-{token}.reporter.cjs"
        reporter.write_text(_reporter_source(name, body, canary_name), encoding="utf-8")
        code, baseline = _run_report(project, command, reporter, config, deadline)
        if code != 0 or baseline["failed"] != 0:
            raise _ProofFailure(_Reason.BASELINE)
        if baseline["passed"] == 0 or baseline["canary"] is not False:
            raise _ProofFailure(_Reason.NO_TESTS)
        test_directory = project / "test"
        test_directory.mkdir(exist_ok=True)
        canary = test_directory / canary_name
        with canary.open("x", encoding="utf-8") as output:
            output.write(
                "require('node:test').test(" + json.dumps(name) + ", () => {\n"
                "require('node:assert/strict').fail(" + json.dumps(body) + ");\n});\n"
            )
        probe_command = command if len(command) == 2 else (*command, f"test/{canary_name}")
        code, probe = _run_report(project, probe_command, reporter, config, deadline)
        if code == 0 or probe["canary"] is not True or probe["failed"] == 0:
            raise _ProofFailure(_Reason.CANARY)
    except _ProofFailure as failure:
        reason = failure.reason
    except (OSError, RuntimeError, ValueError, TypeError):
        reason = _Reason.UNAVAILABLE
    finally:
        if temporary is not None:
            try:
                temporary.cleanup()
            except OSError:
                reason = _Reason.CLEANUP
    return () if reason is None else (reason.value,)
