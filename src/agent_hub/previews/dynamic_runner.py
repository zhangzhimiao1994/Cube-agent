"""Trusted stdlib-only private-network HTTP relay; executed with python3 -I.

Generated children never inherit protocol stdin/stdout or broker credentials.
This module is also the single wire validator shared by the client and broker.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import ctypes
import http.client
import http.server
import json
import os
import re
import runpy
import select
import shutil
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from urllib.parse import unquote, urlsplit

APP_PORT = 41739
MAX_REQUEST_BODY = 1024 * 1024
MAX_RESPONSE_BODY = 8 * 1024 * 1024
MAX_FRAME = 12 * 1024 * 1024
MAX_LOG = 1024 * 1024
REQUEST_TIMEOUT = 10.0
READY_TIMEOUT = 20.0
_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
_REQUEST_HEADERS = {"accept", "accept-language", "content-type", "if-match", "if-none-match", "range"}
_RESPONSE_HEADERS = {
    "content-type", "content-language", "cache-control", "etag", "last-modified",
    "location", "content-range", "accept-ranges",
}
_MAX_INSTALL_DIAGNOSTIC_LINE = 512
_NPM_INSTALL_REASONS = {
    b"EACCES": "permission_denied", b"EPERM": "permission_denied",
    b"ENOENT": "not_found", b"ENOSPC": "storage_full", b"EROFS": "read_only",
    b"ENOMEM": "resource_limit", b"EMFILE": "resource_limit", b"ENFILE": "resource_limit",
    b"ENOTFOUND": "registry_unavailable", b"EAI_AGAIN": "registry_unavailable",
    b"ECONNREFUSED": "registry_unavailable", b"ECONNRESET": "registry_unavailable",
    b"ETIMEDOUT": "registry_unavailable", b"ERR_SOCKET_TIMEOUT": "registry_unavailable",
    b"E404": "dependency_unavailable", b"ETARGET": "dependency_unavailable",
    b"ERESOLVE": "dependency_conflict", b"EJSONPARSE": "package_invalid",
    b"ENOLOCK": "package_invalid", b"EPACKAGEJSON": "package_invalid",
    b"CERT_HAS_EXPIRED": "certificate_error",
    b"UNABLE_TO_VERIFY_LEAF_SIGNATURE": "certificate_error",
    b"SELF_SIGNED_CERT_IN_CHAIN": "certificate_error",
    b"DEPTH_ZERO_SELF_SIGNED_CERT": "certificate_error",
}


class ProbeFailure(RuntimeError):
    def __init__(self, phase: str, reason: str) -> None:
        if phase not in {
            "bootstrap", "control_fds", "node", "npm", "storage_read", "storage_write",
            "http_bind", "http_relay", "http_cleanup", "trusted_prepare", "storage_prepare",
            "runner_protocol", "runner_exit", "storage_roundtrip", "cleanup",
        } or reason not in {
            "permission_denied", "not_found", "read_only", "timeout", "nonzero_exit",
            "bootstrap_failed", "invalid_result", "io_error", "failed",
        }:
            raise ValueError("invalid probe diagnostic")
        self.phase = phase
        self.reason = reason
        super().__init__(f"preview probe failed: {phase}/{reason}")

    def response(self) -> dict[str, object]:
        return {"ok": False, "error": "preview probe failed", "phase": self.phase, "reason": self.reason}


@contextlib.contextmanager
def probe_phase(phase: str) -> Iterator[None]:
    try:
        yield
    except ProbeFailure:
        raise
    except (OSError, ValueError, RuntimeError, EOFError, subprocess.SubprocessError) as error:
        if isinstance(error, PermissionError):
            reason = "permission_denied"
        elif isinstance(error, FileNotFoundError):
            reason = "not_found"
        elif isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
            reason = "timeout"
        elif isinstance(error, OSError):
            reason = "read_only" if error.errno == 30 else "io_error"
        else:
            reason = "failed"
        raise ProbeFailure(phase, reason) from None


class PreviewStartupFailure(RuntimeError):
    def __init__(self, phase: str, reason: str) -> None:
        if type(phase) is not str or type(reason) is not str or phase not in {
            "source_validate", "trusted_prepare", "storage_prepare", "source_copy",
            "install", "install_validate", "install_handoff",
            "build", "build_validate", "build_handoff", "start",
        } or reason not in {
            "permission_denied", "not_found", "read_only", "timeout", "nonzero_exit",
            "invalid_result", "unsafe_tree", "failed", "log_limit",
            "storage_full", "resource_limit", "registry_unavailable", "dependency_unavailable",
            "dependency_conflict", "package_invalid", "certificate_error", "dependency_rejected",
            "supervisor_exit",
        }:
            raise ValueError("invalid startup diagnostic")
        self.phase = phase
        self.reason = reason
        super().__init__(f"preview startup failed: {phase}/{reason}")

    def response(self) -> dict[str, object]:
        return {"ok": False, "error": "preview startup failed",
                "phase": self.phase, "reason": self.reason}


@contextlib.contextmanager
def preview_startup_phase(phase: str) -> Iterator[None]:
    try:
        yield
    except PreviewStartupFailure:
        raise
    except (OSError, ValueError, RuntimeError, EOFError, subprocess.SubprocessError) as error:
        if isinstance(error, PermissionError):
            reason = "permission_denied"
        elif isinstance(error, FileNotFoundError):
            reason = "not_found"
        elif isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
            reason = "timeout"
        elif isinstance(error, subprocess.CalledProcessError):
            reason = "nonzero_exit"
        elif isinstance(error, OSError):
            reason = "read_only" if error.errno == 30 else "failed"
        elif isinstance(error, ValueError) and phase in {
            "source_validate", "install_validate", "build_validate",
            "install_handoff", "build_handoff",
        }:
            reason = "unsafe_tree"
        elif isinstance(error, (ValueError, EOFError)):
            reason = "invalid_result"
        else:
            reason = "failed"
        raise PreviewStartupFailure(phase, reason) from None


class FrameStream(Protocol):
    def read(self, size: int = -1) -> bytes | None: ...
    def write(self, data: bytes) -> int | None: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


def json_object(payload: bytes) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def constant(value: str) -> object:
        raise ValueError("nonfinite JSON value")

    try:
        value: object = json.loads(payload, object_pairs_hook=pairs, parse_constant=constant)
    except (RecursionError, UnicodeError) as error:
        raise ValueError("invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("frame must be an object")  # noqa: TRY004
    return cast(dict[str, object], value)


def read_frame(stream: FrameStream) -> dict[str, object]:
    def exact(length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = stream.read(length - len(chunks))
            if not chunk:
                raise EOFError("incomplete preview frame")
            chunks.extend(chunk)
        return bytes(chunks)

    size = struct.unpack("!I", exact(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError("preview frame size exceeded")
    return json_object(exact(size))


def write_frame(stream: FrameStream, payload: dict[str, object]) -> None:
    data = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
    if not 0 < len(data) <= MAX_FRAME:
        raise ValueError("preview frame size exceeded")
    frame = struct.pack("!I", len(data)) + data
    written = 0
    while written < len(frame):
        count = stream.write(frame[written:])
        if count is None or count <= 0:
            raise EOFError("preview frame write failed")
        written += count
    stream.flush()


def bounded_body(value: object, *, limit: int = MAX_RESPONSE_BODY) -> bytes:
    if not isinstance(value, str) or len(value) > (limit + 2) // 3 * 4:
        raise ValueError("invalid preview body")
    try:
        body = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("invalid preview body encoding") from error
    if len(body) > limit:
        raise ValueError("preview body size exceeded")
    return body


def validate_target(target: str) -> None:
    if not target.startswith("/") or target.startswith("//") or len(target) > 4096:
        raise ValueError("application-relative target required")
    if any(ord(char) < 33 or ord(char) > 126 for char in target) or "\\" in target:
        raise ValueError("invalid application target")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.fragment or "#" in target:
        raise ValueError("invalid application target")
    path = parsed.path
    for _ in range(4):
        if re.search(r"%(?![0-9a-fA-F]{2})", path):
            raise ValueError("invalid target escape")
        decoded = unquote(path, errors="strict")
        if decoded == path:
            break
        if decoded.count("/") != path.count("/") or "\\" in decoded:
            raise ValueError("encoded path separator")
        path = decoded
    else:
        raise ValueError("nested target escapes")
    if any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("target traversal")
    decoded_target = unquote(target, errors="strict")
    if any(ord(char) < 32 or ord(char) == 127 for char in decoded_target):
        raise ValueError("encoded target control byte")


def filtered_headers(value: object, *, response: bool = False) -> list[list[str]]:
    if not isinstance(value, (list, tuple)) or len(value) > 64:
        raise ValueError("invalid headers")
    result: list[list[str]] = []
    size = 0
    seen: set[str] = set()
    for item in cast(list[object], value):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("invalid header pair")
        name, content = cast(tuple[object, object], item)
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("invalid header types")  # noqa: TRY004
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ValueError("invalid header name")
        if any(ord(char) < 32 or ord(char) > 126 for char in content):
            raise ValueError("invalid header value")
        size += len(name) + len(content)
        if size > 16384:
            raise ValueError("header size exceeded")
        name = name.lower()
        if name in (_RESPONSE_HEADERS if response else _REQUEST_HEADERS):
            if name in seen:
                raise ValueError("duplicate application header")
            seen.add(name)
            if name == "location":
                validate_target(content)
            result.append([name, content])
    return result


def validate_http_request(
    method: str, target: str, headers: tuple[tuple[str, str], ...], body: bytes,
) -> dict[str, object]:
    if method not in _METHODS or not isinstance(body, bytes) or len(body) > MAX_REQUEST_BODY:
        raise ValueError("invalid application method/body")
    validate_target(target)
    return {"method": method, "target": target, "headers": filtered_headers(headers),
            "body": base64.b64encode(body).decode("ascii")}


def validate_wire_http(payload: dict[str, object]) -> dict[str, object]:
    if set(payload) != {"method", "target", "headers", "body"}:
        raise ValueError("invalid HTTP request fields")
    method, target = payload["method"], payload["target"]
    if not isinstance(method, str) or not isinstance(target, str):
        raise ValueError("invalid HTTP request types")  # noqa: TRY004
    headers = filtered_headers(payload["headers"])
    return validate_http_request(method, target, tuple((a, b) for a, b in headers),
                                 bounded_body(payload["body"], limit=MAX_REQUEST_BODY))


def relay_http(payload: dict[str, object]) -> dict[str, object]:
    request = validate_wire_http(payload)
    deadline = time.monotonic() + REQUEST_TIMEOUT
    # Numeric private loopback only: no DNS, proxy environment or redirect following.
    connection = http.client.HTTPConnection("127.0.0.1", APP_PORT, timeout=REQUEST_TIMEOUT)
    try:
        connection.request(cast(str, request["method"]), cast(str, request["target"]),
                           body=bounded_body(request["body"], limit=MAX_REQUEST_BODY),
                           headers=dict(cast(list[list[str]], request["headers"])))
        response = connection.getresponse()
        headers = filtered_headers(response.getheaders(), response=True)
        body = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("application response deadline exceeded")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, MAX_RESPONSE_BODY + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BODY:
                raise ValueError("application response size exceeded")
        return {"status_code": response.status, "headers": headers,
                "body": base64.b64encode(body).decode("ascii")}
    finally:
        connection.close()


def application_environment() -> dict[str, str]:
    return {
        "PATH": "/preview/node/bin:/usr/bin:/bin", "HOME": "/preview/work/home",
        "TMPDIR": "/preview/work/tmp", "TMP": "/preview/work/tmp",
        "PORT": str(APP_PORT), "HOST": "127.0.0.1", "DATA_DIR": "/preview/work/data",
        "NODE_OPTIONS": "", "NPM_CONFIG_NODE_OPTIONS": "",
        "NPM_CONFIG_CACHE": "/preview/work/cache", "NPM_CONFIG_USERCONFIG": "/tmp/npm-user.npmrc",
        "NPM_CONFIG_GLOBALCONFIG": "/tmp/npm-global.npmrc", "NPM_CONFIG_IGNORE_SCRIPTS": "true",
        "NPM_CONFIG_AUDIT": "false", "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false", "NPM_CONFIG_FETCH_RETRIES": "0",
        "NPM_CONFIG_REGISTRY": "https://registry.npmmirror.com",
    }


def install_guard() -> str:
    sandbox = Path("/preview/trusted/harness/project_validation_sandbox.py")
    if not sandbox.exists():
        sandbox = Path(__file__).resolve().parents[1] / "harness/project_validation_sandbox.py"
    return cast(str, runpy.run_path(str(sandbox))["_INSTALL_GUARD"])


def stage_argv(stage: str) -> tuple[str, ...]:
    if stage == "install":
        return ("/preview/node/bin/node", "-e", install_guard(), "/preview/node/bin/npm",
                "install", "--ignore-scripts", "--no-audit", "--no-fund")
    if stage == "build":
        return ("/preview/node/bin/npm", "run", "build", "--if-present", "--ignore-scripts")
    if stage == "start":
        return ("/preview/node/bin/npm", "start", "--ignore-scripts")
    raise ValueError("unknown preview stage")


def _kill_child(child: subprocess.Popen[bytes]) -> None:
    if sys.platform != "linux":
        raise RuntimeError("preview runner requires Linux isolation")
    with contextlib.suppress(ProcessLookupError):
        _kill_group(child.pid)
    child.wait(timeout=3)


def _kill_group(pid: int) -> None:
    operation = getattr(os, "killpg", None)
    if not callable(operation):
        raise RuntimeError("Linux process group cleanup unavailable")  # noqa: TRY004
    cast(Callable[[int, int], None], operation)(pid, 9)


def _copy_source(source: Path, work: Path) -> None:
    shutil.copytree(source, work / "app")
    for root, _dirs, files in os.walk(work / "app"):
        Path(root).chmod(0o755)
        for name in files:
            path = Path(root) / name
            path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)


def _install_failure_reason(line: bytearray) -> str | None:
    if line.startswith(b"generated dependency source rejected: "):
        return "dependency_rejected"
    match = re.fullmatch(rb"npm (?:ERR!|error) code ([A-Z0-9_]+)\r?", line)
    return _NPM_INSTALL_REASONS.get(match[1]) if match else None


def _run_stage(stage: str) -> None:
    if sys.platform != "linux":
        raise RuntimeError("preview runner requires isolated Linux")
    work = Path("/preview/work")
    with preview_startup_phase(stage):
        for name in ("home", "tmp", "cache", "data"):
            _ensure_work_directory(work / name)
        if stage == "install":
            with preview_startup_phase("source_copy"):
                _copy_source(Path("/preview/source"), work)
        child = _spawn_application(stage)
    log_bytes = 0
    log_lock = threading.Lock()
    log_overflow = threading.Event()
    install_reason: str | None = None

    def drain(pipe: BinaryIO) -> None:
        nonlocal log_bytes, install_reason
        classify = stage == "install" and pipe is child.stderr
        line = bytearray()
        discard_line = False
        try:
            while chunk := pipe.read(4096):
                with log_lock:
                    log_bytes += len(chunk)
                    if log_bytes > MAX_LOG:
                        log_overflow.set()
                        with contextlib.suppress(ProcessLookupError):
                            _kill_group(child.pid)
                        return
                if classify and install_reason is None:
                    for byte in chunk:
                        if byte == 10:
                            if not discard_line:
                                install_reason = _install_failure_reason(line)
                            line.clear()
                            discard_line = False
                            if install_reason is not None:
                                break
                        elif not discard_line:
                            if len(line) < _MAX_INSTALL_DIAGNOSTIC_LINE:
                                line.append(byte)
                            else:
                                # Never classify a truncated line or its later chunks.
                                line.clear()
                                discard_line = True
            if classify and install_reason is None and not discard_line and line:
                install_reason = _install_failure_reason(line)
        finally:
            line.clear()
            pipe.close()

    threads: list[threading.Thread] = []
    for pipe in (child.stdout, child.stderr):
        assert pipe is not None
        thread = threading.Thread(target=drain, args=(pipe,), daemon=True)
        thread.start()
        threads.append(thread)
    try:
        with preview_startup_phase(stage):
            if stage != "start":
                exit_code = child.wait(timeout=120)
                for thread in threads:
                    thread.join(timeout=1)
                if log_overflow.is_set():
                    raise PreviewStartupFailure(stage, "log_limit")
                if exit_code != 0:
                    raise PreviewStartupFailure(stage, install_reason or "nonzero_exit")
                write_frame(sys.stdout.buffer, {"ok": True, "state": "prepared"})
                return
            ready_deadline = time.monotonic() + READY_TIMEOUT
            while True:
                if log_overflow.is_set():
                    raise PreviewStartupFailure(stage, "log_limit")
                poll_code = child.poll()
                if poll_code is not None:
                    raise PreviewStartupFailure(
                        stage, "nonzero_exit" if poll_code != 0 else "invalid_result",
                    )
                if time.monotonic() >= ready_deadline:
                    raise PreviewStartupFailure(stage, "timeout")
                try:
                    relay_http(validate_http_request("GET", "/", (), b""))
                    if child.poll() is None:
                        break
                except (OSError, http.client.HTTPException):
                    time.sleep(0.1)
            write_frame(sys.stdout.buffer, {"ok": True, "state": "ready"})
        while True:
            payload = _wait_request(child, log_overflow)
            if child.poll() is not None or log_overflow.is_set():
                raise RuntimeError("preview application exited")
            try:
                result = relay_http(payload)
                write_frame(sys.stdout.buffer, {"ok": True, "response": result})
            except (ValueError, OSError, TimeoutError, http.client.HTTPException):
                # The write might have committed; neither runner nor client retries.
                write_frame(sys.stdout.buffer, {"ok": False, "error": "application request failed"})
    finally:
        _kill_child(child)


def _wait_request(child: subprocess.Popen[bytes], log_overflow: threading.Event) -> dict[str, object]:
    while True:
        if child.poll() is not None or log_overflow.is_set():
            raise RuntimeError("preview application exited or exceeded log limit")
        if select.select([sys.stdin.buffer], [], [], 0.25)[0]:
            return read_frame(sys.stdin.buffer)


def _spawn_application(stage: str) -> subprocess.Popen[bytes]:
    if sys.platform != "linux":
        raise RuntimeError("preview application requires isolated Linux")
    _protect_control_fds()
    cwd = "/preview/app" if stage == "start" else "/preview/work/app"
    return subprocess.Popen(stage_argv(stage), cwd=cwd, env=application_environment(),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, close_fds=True, start_new_session=True)


def _protect_control_fds() -> None:
    # App and supervisor share a DynamicUser. Deny ptrace/proc-fd access even to
    # that UID; close_fds alone would not prevent reopening /proc/<parent>/fd/1.
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise OSError(ctypes.get_errno(), "cannot protect preview control descriptors")


def _ensure_work_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o777)
    except FileExistsError:
        if path.is_symlink() or not path.is_dir():
            raise ValueError("invalid private work directory") from None
    else:
        path.chmod(0o777)


def _probe() -> None:
    with probe_phase("control_fds"):
        _protect_control_fds()
    _probe_toolchain()
    work = Path("/preview/work")
    with probe_phase("storage_read"):
        if (work / ".broker-storage-probe").read_bytes() != b"preview-storage-v1":
            raise ProbeFailure("storage_read", "invalid_result")
    with probe_phase("storage_write"):
        (work / ".runner-storage-probe").write_bytes(b"preview-storage-v1")
    class ProbeHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    with probe_phase("http_bind"):
        server = http.server.HTTPServer(("127.0.0.1", APP_PORT), ProbeHandler)
    with server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with probe_phase("http_relay"):
                response = relay_http(validate_http_request("GET", "/", (), b""))
                if response["status_code"] != 204:
                    raise ProbeFailure("http_relay", "invalid_result")
                write_frame(sys.stdout.buffer, {"ok": True, "state": "probe"})
        finally:
            with probe_phase("http_cleanup"):
                server.shutdown()
                worker.join(timeout=2)


def _probe_toolchain() -> None:
    if sys.platform != "linux":
        raise RuntimeError("preview toolchain probe requires isolated Linux")
    for phase, command in (
        ("node", ("/preview/node/bin/node", "--version")),
        ("npm", ("/preview/node/bin/node", "/preview/node/bin/npm", "--version")),
    ):
        with probe_phase(phase):
            result = subprocess.run(command, cwd="/preview/work", env=application_environment(),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True, timeout=5, check=False)
            if result.returncode != 0:
                raise ProbeFailure(phase, "nonzero_exit")


def main() -> None:
    if sys.platform != "linux" or len(sys.argv) != 2:
        raise SystemExit("preview runner requires an isolated Linux unit")
    try:
        if sys.argv[1] == "probe":
            _probe()
        else:
            _run_stage(sys.argv[1])
    except (ProbeFailure, PreviewStartupFailure) as error:
        write_frame(sys.stdout.buffer, error.response())
        raise SystemExit(1) from None
    except (EOFError, ValueError, OSError, RuntimeError, TimeoutError, subprocess.SubprocessError):
        write_frame(sys.stdout.buffer, {"ok": False, "error": "preview stage failed"})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
