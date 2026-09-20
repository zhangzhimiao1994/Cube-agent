"""Independent business checks for an already built, isolated Node project."""

from __future__ import annotations

import http.client
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

_PLATFORM = os.name
_MAX_RESPONSE_BYTES = 1024 * 1024


class _ValidationFailure(Exception):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _ValidationFailure(message)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ValidationFailure("timeout: small task API validation deadline exceeded")
    return remaining


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stop_tree(process: subprocess.Popen[bytes], taskkill: str | None) -> None:
    if sys.platform != "win32":
        # The group survives an exited npm parent; always target the entire group.
        try:
            os.killpg(process.pid, signal.SIGTERM)
            time.sleep(0.15)
        except ProcessLookupError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        if process.poll() is not None:
            raise _ValidationFailure(
                "cleanup: npm exited before Windows process-tree cleanup could be confirmed"
            )
        if taskkill is None:
            raise _ValidationFailure("cleanup: taskkill unavailable")
        result = subprocess.run(
            [taskkill, "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        _require(result.returncode == 0, "cleanup: taskkill failed to terminate npm process tree")
    process.wait(timeout=3)


class _TaskAPI:
    def __init__(self, port: int, deadline: float) -> None:
        self.port = port
        self.deadline = deadline

    def request(
        self, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> tuple[int, object]:
        # Numeric loopback HTTPConnection ignores proxy environment and redirects.
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=min(2.0, _remaining(self.deadline))
        )
        timer: threading.Timer | None = None
        try:
            connection.connect()
            sock = connection.sock

            def interrupt() -> None:
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            # Inactivity timeouts alone do not bound a slow streaming response.
            timer = threading.Timer(_remaining(self.deadline), interrupt)
            timer.daemon = True
            timer.start()
            connection.request(
                method, path,
                body=None if body is None else json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "Connection": "close"},
            )
            response = connection.getresponse()
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            _remaining(self.deadline)
            _require(len(raw) <= _MAX_RESPONSE_BYTES, f"{method} {path}: response exceeds 1 MiB")
            try:
                payload: object = json.loads(raw) if raw else None
            except (ValueError, UnicodeError) as exc:
                raise _ValidationFailure(f"{method} {path}: invalid JSON response") from exc
            return response.status, payload
        finally:
            if timer is not None:
                timer.cancel()
                timer.join(timeout=1)
            connection.close()

    def items(self) -> list[dict[str, object]]:
        status, payload = self.request("GET", "/tasks")
        _require(status == 200, f"GET /tasks: expected 200, got {status}")
        _require(isinstance(payload, dict), "GET /tasks: expected JSON object with items")
        assert isinstance(payload, dict)
        items = payload.get("items")
        _require(isinstance(items, list), "GET /tasks: expected items array")
        assert isinstance(items, list)
        _require(all(isinstance(item, dict) for item in items), "GET /tasks: invalid task item")
        return [dict(item) for item in items]

    def create(self, title: str) -> dict[str, object]:
        status, payload = self.request("POST", "/tasks", {"title": title})
        _require(status == 201, f"POST /tasks: expected 201, got {status}")
        _require(isinstance(payload, dict), "POST /tasks: expected task object")
        assert isinstance(payload, dict)
        task = dict(payload)
        task_id = task.get("id")
        _require(
            isinstance(task_id, (str, int)) and not isinstance(task_id, bool) and str(task_id) != "",
            "POST /tasks: missing or invalid id",
        )
        _require(task.get("title") == title, "POST /tasks: title was not preserved")
        _require(task.get("status") in ("todo", "doing", "done"), "POST /tasks: invalid status")
        created = task.get("created_at")
        _require(isinstance(created, str) and bool(created), "POST /tasks: missing created_at")
        return task

    def expect_visible(self, expected: dict[str, object], context: str) -> None:
        matches = [task for task in self.items() if task.get("id") == expected["id"]]
        _require(len(matches) == 1, f"{context}: expected exactly one task with id {expected['id']}")
        for field in ("id", "title", "status", "created_at"):
            _require(matches[0].get(field) == expected[field], f"{context}: {field} mismatch")

    def expect_deleted(self, task: dict[str, object], context: str) -> None:
        matches = [item for item in self.items() if item.get("id") == task["id"]]
        _require(not matches, f"{context}: deleted task is still visible in GET /tasks")


def _ready(api: _TaskAPI, process: subprocess.Popen[bytes]) -> None:
    while True:
        _remaining(api.deadline)
        _require(process.poll() is None, "startup: npm start exited before API became ready")
        try:
            api.items()
            return
        except (OSError, http.client.HTTPException):
            time.sleep(min(0.05, _remaining(api.deadline)))


def _path(task: dict[str, object]) -> str:
    return f"/tasks/{quote(str(task['id']), safe='')}"


def _check_missing(api: _TaskAPI) -> None:
    path = f"/tasks/missing-{uuid4().hex}"
    for method, url, body in (
        ("PATCH", path, {"status": "done"}),
        ("DELETE", path, None),
        ("POST", f"{path}/restore", None),
    ):
        status, payload = api.request(method, url, body)
        _require(status == 404, f"{method} missing id: expected 404, got {status}")
        error = payload.get("error") if isinstance(payload, dict) else None
        _require(isinstance(error, dict), f"{method} missing id: expected error object")
        assert isinstance(error, dict)
        for field in ("code", "message"):
            _require(
                isinstance(error.get(field), str) and bool(error[field]),
                f"{method} missing id: error.{field} must be a nonempty string",
            )


def _environment(data: str) -> dict[str, str]:
    # Do not expose host/provider credentials or the host's npm user configuration.
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
    }
    env.update({
        "DATA_DIR": data, "HOME": data, "USERPROFILE": data,
        "TMP": data, "TEMP": data, "TMPDIR": data,
        "npm_config_userconfig": str(Path(data) / "user.npmrc"),
        "npm_config_globalconfig": str(Path(data) / "global.npmrc"),
        "npm_config_cache": str(Path(data) / "npm-cache"),
        "npm_config_update_notifier": "false", "npm_config_audit": "false",
    })
    return env


def validate_small_task_api(root: Path, timeout_seconds: float) -> tuple[str, ...]:
    """Check an installed/built project's CRUD, 404s and persistence through npm start.

    The caller must supply an isolated execution environment: this executes project
    code and is not itself a security sandbox. The timeout covers startup and HTTP
    checks, with bounded process cleanup afterwards. An empty tuple verifies only
    the small-API business contract, not all project requirements.
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return ("timeout_seconds must be finite and positive",)
    if _PLATFORM not in ("posix", "nt"):
        return (f"unsupported platform for process-tree cleanup: {_PLATFORM}",)
    npm = shutil.which("npm")
    if npm is None:
        return ("npm executable unavailable; business API was not validated",)
    taskkill = shutil.which("taskkill") if _PLATFORM == "nt" else None
    if _PLATFORM == "nt" and taskkill is None:
        return ("unsupported Windows environment: taskkill unavailable for process-tree cleanup",)

    failures: list[str] = []
    process: subprocess.Popen[bytes] | None = None
    data: tempfile.TemporaryDirectory[str] | None = None
    deadline = time.monotonic() + timeout_seconds
    phase = "startup"
    try:
        root = root.resolve(strict=True)
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        scripts = package.get("scripts") if isinstance(package, dict) else None
        _require(
            isinstance(scripts, dict) and isinstance(scripts.get("start"), str)
            and bool(scripts["start"].strip()),
            "package.json must provide an npm start script",
        )
        data = tempfile.TemporaryDirectory(prefix="small-task-api-")
        env = _environment(data.name)
        first: dict[str, object] = {}
        second: dict[str, object] = {}
        for cycle in range(3):
            phase = ("CRUD", "persistence after restart", "restore persistence after restart")[cycle]
            port = _free_port()
            env["PORT"] = str(port)
            _remaining(deadline)
            process = subprocess.Popen(
                [npm, "start"], cwd=root, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=_PLATFORM == "posix",
            )
            api = _TaskAPI(port, deadline)
            _ready(api, process)
            if cycle == 0:
                first = api.create(f"Acceptance {uuid4().hex}")
                second = api.create(f"Independent {uuid4().hex}")
                _require(first["id"] != second["id"], "POST /tasks: task ids must be unique")
                api.expect_visible(first, "create first task")
                api.expect_visible(second, "create second task")
                for state in ("todo", "doing", "done"):
                    status, payload = api.request("PATCH", _path(first), {"status": state})
                    _require(status == 200, f"PATCH task: expected 200, got {status}")
                    first["status"] = state
                    _require(isinstance(payload, dict), "PATCH task: expected updated task object")
                    assert isinstance(payload, dict)
                    for field in ("id", "title", "status", "created_at"):
                        _require(payload.get(field) == first[field], f"PATCH task: {field} mismatch")
                    api.expect_visible(first, "PATCH read-back")
                    api.expect_visible(second, "PATCH must not change other task")
                _check_missing(api)
                status, _ = api.request("DELETE", _path(second))
                _require(200 <= status < 300, f"DELETE task: expected success, got {status}")
                api.expect_deleted(second, "DELETE read-back")
                api.expect_visible(first, "DELETE must not change other task")
            elif cycle == 1:
                api.expect_visible(first, "persistence after restart")
                api.expect_deleted(second, "deleted state persistence after restart")
                status, _ = api.request("POST", f"{_path(second)}/restore")
                _require(200 <= status < 300, f"restore task: expected success, got {status}")
                api.expect_visible(second, "restore read-back")
                api.expect_visible(first, "restore must not change other task")
            else:
                api.expect_visible(first, "task persistence after second restart")
                api.expect_visible(second, "restore persistence after second restart")
            _stop_tree(process, taskkill)
            process = None
        _remaining(deadline)
    except (_ValidationFailure, OSError, ValueError, http.client.HTTPException,
            subprocess.SubprocessError) as exc:
        label = "timeout" if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else phase
        failures.append(f"{label}: {exc}")
    finally:
        if process is not None:
            try:
                _stop_tree(process, taskkill)
            except (_ValidationFailure, OSError, subprocess.SubprocessError) as exc:
                failures.append(f"process-tree cleanup failed: {exc}")
        if data is not None:
            try:
                data.cleanup()
            except OSError as exc:
                failures.append(f"temporary DATA_DIR cleanup failed: {exc}")
    return tuple(failures)
