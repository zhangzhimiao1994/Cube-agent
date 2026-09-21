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
from collections.abc import Mapping, Sequence
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


class _TenantCRMAPI:
    def __init__(self, port: int, deadline: float) -> None:
        self.port = port
        self.deadline = deadline

    def request(
        self, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> tuple[int, object]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=min(2.0, _remaining(self.deadline))
        )
        try:
            connection.connect()
            connection.request(
                method,
                path,
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
            connection.close()

    def tenant_path(self, tenant: str, resource: str, suffix: str = "") -> str:
        return f"/tenants/{quote(tenant, safe='')}/{resource}{suffix}"

    def create(
        self,
        tenant: str,
        resource: str,
        body: Mapping[str, object],
    ) -> dict[str, object]:
        status, payload = self.request("POST", self.tenant_path(tenant, resource), body)
        _require(status == 201, f"POST {resource}: expected 201, got {status}")
        _require(isinstance(payload, dict), f"POST {resource}: expected object")
        assert isinstance(payload, dict)
        item = dict(payload)
        _require(
            isinstance(item.get("id"), (str, int)) and not isinstance(item.get("id"), bool),
            f"POST {resource}: missing id",
        )
        return item

    def list_items(self, tenant: str, resource: str, query: str = "") -> list[dict[str, object]]:
        status, payload = self.request("GET", self.tenant_path(tenant, resource, query))
        _require(status == 200, f"GET {resource}: expected 200, got {status}")
        _require(isinstance(payload, dict), f"GET {resource}: expected object")
        assert isinstance(payload, dict)
        items = payload.get("items")
        _require(isinstance(items, list), f"GET {resource}: expected items array")
        assert isinstance(items, list)
        _require(all(isinstance(item, dict) for item in items), f"GET {resource}: invalid item")
        return [dict(item) for item in items]

    def expect_error_404(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        context: str,
    ) -> None:
        status, payload = self.request(method, path, body)
        _require(status == 404, f"{context}: expected 404, got {status}")
        error = payload.get("error") if isinstance(payload, dict) else None
        _require(isinstance(error, dict), f"{context}: expected error object")
        assert isinstance(error, dict)
        for field in ("code", "message"):
            _require(
                isinstance(error.get(field), str) and bool(error[field]),
                f"{context}: error.{field} must be nonempty",
            )


class _OrderOpsAPI:
    def __init__(self, port: int, deadline: float) -> None:
        self.port = port
        self.deadline = deadline

    def request(
        self, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> tuple[int, object]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=min(2.0, _remaining(self.deadline))
        )
        try:
            connection.connect()
            connection.request(
                method,
                path,
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
            connection.close()

    def create(self, path: str, body: Mapping[str, object]) -> dict[str, object]:
        status, payload = self.request("POST", path, body)
        _require(status == 201, f"POST {path}: expected 201, got {status}")
        _require(isinstance(payload, dict), f"POST {path}: expected object")
        assert isinstance(payload, dict)
        item = dict(payload)
        _require(
            isinstance(item.get("id"), (str, int)) and not isinstance(item.get("id"), bool),
            f"POST {path}: missing id",
        )
        return item

    def get_object(self, path: str) -> dict[str, object]:
        status, payload = self.request("GET", path)
        _require(status == 200, f"GET {path}: expected 200, got {status}")
        _require(isinstance(payload, dict), f"GET {path}: expected object")
        assert isinstance(payload, dict)
        return dict(payload)

    def expect_conflict(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        context: str,
    ) -> None:
        status, payload = self.request(method, path, body)
        _require(status == 409, f"{context}: expected 409, got {status}")
        error = payload.get("error") if isinstance(payload, dict) else None
        _require(isinstance(error, dict), f"{context}: expected error object")
        assert isinstance(error, dict)
        for field in ("code", "message"):
            _require(
                isinstance(error.get(field), str) and bool(error[field]),
                f"{context}: error.{field} must be nonempty",
            )


class _PortfolioAPI:
    def __init__(self, port: int, deadline: float) -> None:
        self.port = port
        self.deadline = deadline

    def request(
        self, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> tuple[int, object]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=min(2.0, _remaining(self.deadline))
        )
        try:
            connection.connect()
            connection.request(
                method,
                path,
                body=None if body is None else json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "Connection": "close"},
            )
            response = connection.getresponse()
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            _remaining(self.deadline)
            _require(len(raw) <= _MAX_RESPONSE_BYTES, f"{method} {path}: response exceeds 1 MiB")
            content_type = response.getheader("Content-Type", "")
            if "text/csv" in content_type or path.startswith("/analytics/portfolio.csv"):
                try:
                    payload: object = raw.decode("utf-8")
                except UnicodeError as exc:
                    raise _ValidationFailure(f"{method} {path}: invalid CSV encoding") from exc
            else:
                try:
                    payload = json.loads(raw) if raw else None
                except (ValueError, UnicodeError) as exc:
                    raise _ValidationFailure(f"{method} {path}: invalid JSON response") from exc
            return response.status, payload
        finally:
            connection.close()

    def create(self, path: str, body: Mapping[str, object]) -> dict[str, object]:
        status, payload = self.request("POST", path, body)
        _require(status == 201, f"POST {path}: expected 201, got {status}")
        _require(isinstance(payload, dict), f"POST {path}: expected object")
        assert isinstance(payload, dict)
        item = dict(payload)
        _require(
            isinstance(item.get("id"), (str, int)) and not isinstance(item.get("id"), bool),
            f"POST {path}: missing id",
        )
        return item

    def get_object(self, path: str) -> dict[str, object]:
        status, payload = self.request("GET", path)
        _require(status == 200, f"GET {path}: expected 200, got {status}")
        _require(isinstance(payload, dict), f"GET {path}: expected object")
        assert isinstance(payload, dict)
        return dict(payload)

    def expect_blocked(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        context: str,
    ) -> None:
        status, payload = self.request(method, path, body)
        _require(status in {403, 409}, f"{context}: expected 403 or 409, got {status}")
        error = payload.get("error") if isinstance(payload, dict) else None
        _require(isinstance(error, dict), f"{context}: expected error object")
        assert isinstance(error, dict)
        for field in ("code", "message"):
            _require(
                isinstance(error.get(field), str) and bool(error[field]),
                f"{context}: error.{field} must be nonempty",
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


def validate_medium_crm_api(root: Path, timeout_seconds: float) -> tuple[str, ...]:
    """Check the medium CRM-lite contract with tenant isolation and persistence."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return ("timeout_seconds must be finite and positive",)
    if _PLATFORM not in ("posix", "nt"):
        return (f"unsupported platform for process-tree cleanup: {_PLATFORM}",)
    npm = shutil.which("npm")
    if npm is None:
        return ("npm executable unavailable; medium CRM API was not validated",)
    taskkill = shutil.which("taskkill") if _PLATFORM == "nt" else None
    if _PLATFORM == "nt" and taskkill is None:
        return ("unsupported Windows environment: taskkill unavailable for process-tree cleanup",)

    failures: list[str] = []
    process: subprocess.Popen[bytes] | None = None
    data: tempfile.TemporaryDirectory[str] | None = None
    deadline = time.monotonic() + timeout_seconds
    phase = "startup"
    tenant_a = f"tenant-a-{uuid4().hex[:8]}"
    tenant_b = f"tenant-b-{uuid4().hex[:8]}"
    account_a: dict[str, object] = {}
    account_b: dict[str, object] = {}
    contact_a: dict[str, object] = {}
    opportunity_a: dict[str, object] = {}
    reminder_a: dict[str, object] = {}
    try:
        root = root.resolve(strict=True)
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        scripts = package.get("scripts") if isinstance(package, dict) else None
        _require(
            isinstance(scripts, dict)
            and isinstance(scripts.get("start"), str)
            and bool(scripts["start"].strip()),
            "package.json must provide an npm start script",
        )
        data = tempfile.TemporaryDirectory(prefix="medium-crm-api-")
        env = _environment(data.name)
        for cycle in range(2):
            phase = "CRM workflow" if cycle == 0 else "CRM persistence after restart"
            port = _free_port()
            env["PORT"] = str(port)
            _remaining(deadline)
            process = subprocess.Popen(
                [npm, "start"],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=_PLATFORM == "posix",
            )
            api = _TenantCRMAPI(port, deadline)
            _ready_crm(api, process, tenant_a)
            if cycle == 0:
                account_a = api.create(tenant_a, "accounts", {"name": "Acme Alpha"})
                account_b = api.create(tenant_b, "accounts", {"name": "Acme Beta"})
                _require(account_a["id"] != account_b["id"], "accounts: ids must be unique")
                _require(
                    _contains_id(api.list_items(tenant_a, "accounts", "?search=Acme"), account_a),
                    "tenant A search must include tenant A account",
                )
                _require(
                    not _contains_id(api.list_items(tenant_a, "accounts", "?search=Beta"), account_b),
                    "tenant A search must not expose tenant B account",
                )
                contact_a = api.create(
                    tenant_a,
                    "contacts",
                    {"account_id": account_a["id"], "name": "Ava Buyer", "email": "ava@example.test"},
                )
                _require(
                    _contains_id(
                        api.list_items(tenant_a, "contacts", f"?account_id={quote(str(account_a['id']), safe='')}"),
                        contact_a,
                    ),
                    "contacts: account filter must include tenant-owned contact",
                )
                api.expect_error_404(
                    "POST",
                    api.tenant_path(tenant_b, "contacts"),
                    {"account_id": account_a["id"], "name": "Cross Tenant"},
                    context="cross-tenant contact account reference",
                )
                opportunity_a = api.create(
                    tenant_a,
                    "opportunities",
                    {"account_id": account_a["id"], "name": "Renewal", "amount": 4200, "stage": "open"},
                )
                status, payload = api.request(
                    "PATCH",
                    api.tenant_path(
                        tenant_a,
                        "opportunities",
                        f"/{quote(str(opportunity_a['id']), safe='')}",
                    ),
                    {"stage": "won"},
                )
                _require(status == 200, f"PATCH opportunity: expected 200, got {status}")
                _require(isinstance(payload, dict), "PATCH opportunity: expected object")
                assert isinstance(payload, dict)
                opportunity_a = dict(payload)
                _require(opportunity_a.get("stage") == "won", "PATCH opportunity: stage mismatch")
                reminder_a = api.create(
                    tenant_a,
                    "reminders",
                    {"contact_id": contact_a["id"], "due_at": "2030-01-01", "note": "Follow up"},
                )
                _require(
                    _contains_id(api.list_items(tenant_a, "reminders"), reminder_a),
                    "reminders: created reminder must be visible",
                )
                api.expect_error_404(
                    "PATCH",
                    api.tenant_path(
                        tenant_b,
                        "opportunities",
                        f"/{quote(str(opportunity_a['id']), safe='')}",
                    ),
                    {"stage": "lost"},
                    context="cross-tenant opportunity mutation",
                )
            else:
                _require(
                    _contains_id(api.list_items(tenant_a, "accounts", "?search=Acme"), account_a),
                    "accounts: tenant A account must persist",
                )
                _require(
                    _contains_id(api.list_items(tenant_a, "contacts"), contact_a),
                    "contacts: tenant A contact must persist",
                )
                _require(
                    _contains_id(api.list_items(tenant_a, "opportunities"), opportunity_a),
                    "opportunities: tenant A opportunity must persist",
                )
                _require(
                    _contains_id(api.list_items(tenant_a, "reminders"), reminder_a),
                    "reminders: tenant A reminder must persist",
                )
            _stop_tree(process, taskkill)
            process = None
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


def validate_large_order_ops_api(root: Path, timeout_seconds: float) -> tuple[str, ...]:
    """Check the large order-operations contract with failure paths and persistence."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return ("timeout_seconds must be finite and positive",)
    if _PLATFORM not in ("posix", "nt"):
        return (f"unsupported platform for process-tree cleanup: {_PLATFORM}",)
    npm = shutil.which("npm")
    if npm is None:
        return ("npm executable unavailable; large order API was not validated",)
    taskkill = shutil.which("taskkill") if _PLATFORM == "nt" else None
    if _PLATFORM == "nt" and taskkill is None:
        return ("unsupported Windows environment: taskkill unavailable for process-tree cleanup",)

    failures: list[str] = []
    process: subprocess.Popen[bytes] | None = None
    data: tempfile.TemporaryDirectory[str] | None = None
    deadline = time.monotonic() + timeout_seconds
    phase = "startup"
    sku = f"SKU-{uuid4().hex[:8]}"
    order: dict[str, object] = {}
    fulfillment: dict[str, object] = {}
    try:
        root = root.resolve(strict=True)
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        scripts = package.get("scripts") if isinstance(package, dict) else None
        _require(
            isinstance(scripts, dict)
            and isinstance(scripts.get("start"), str)
            and bool(scripts["start"].strip()),
            "package.json must provide an npm start script",
        )
        data = tempfile.TemporaryDirectory(prefix="large-order-api-")
        env = _environment(data.name)
        for cycle in range(2):
            phase = "order operations workflow" if cycle == 0 else "order persistence after restart"
            port = _free_port()
            env["PORT"] = str(port)
            _remaining(deadline)
            process = subprocess.Popen(
                [npm, "start"],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=_PLATFORM == "posix",
            )
            api = _OrderOpsAPI(port, deadline)
            _ready_order_ops(api, process)
            if cycle == 0:
                item = api.create(
                    "/catalog/items",
                    {"sku": sku, "name": "Acceptance Widget", "price": 1200},
                )
                _require(str(item.get("sku")) == sku, "catalog: sku was not preserved")
                stock = api.create("/inventory/stock", {"sku": sku, "quantity": 2})
                _require(str(stock.get("sku")) == sku, "inventory stock: sku mismatch")
                reservation = api.create(
                    "/inventory/reservations",
                    {"sku": sku, "quantity": 1, "reason": "acceptance"},
                )
                _require(str(reservation.get("sku")) == sku, "reservation: sku mismatch")
                api.expect_conflict(
                    "POST",
                    "/inventory/reservations",
                    {"sku": sku, "quantity": 99, "reason": "conflict"},
                    context="stock conflict reservation",
                )
                request_id = f"order-{uuid4().hex[:8]}"
                order = api.create(
                    "/orders",
                    {
                        "customer_id": "customer-acceptance",
                        "client_request_id": request_id,
                        "lines": [{"sku": sku, "quantity": 1}],
                    },
                )
                _require(order.get("status") in {"created", "reserved", "pending"}, "order: invalid status")
                api.expect_conflict(
                    "POST",
                    "/orders",
                    {
                        "customer_id": "customer-acceptance",
                        "client_request_id": request_id,
                        "lines": [{"sku": sku, "quantity": 1}],
                    },
                    context="duplicate order submission",
                )
                status, payload = api.request(
                    "POST",
                    f"/orders/{quote(str(order['id']), safe='')}/payment",
                    {"state": "authorized", "amount": 1200},
                )
                _require(status == 200, f"payment: expected 200, got {status}")
                _require(isinstance(payload, dict), "payment: expected object")
                assert isinstance(payload, dict)
                order = dict(payload)
                _require(
                    order.get("payment_state") == "authorized",
                    "payment: payment_state must be authorized",
                )
                fulfillment = api.create(
                    "/fulfillment/jobs",
                    {"order_id": order["id"], "warehouse": "main"},
                )
                status, payload = api.request(
                    "PATCH",
                    f"/fulfillment/jobs/{quote(str(fulfillment['id']), safe='')}",
                    {"status": "cancelled"},
                )
                _require(status == 200, f"cancel fulfillment: expected 200, got {status}")
                _require(isinstance(payload, dict), "cancel fulfillment: expected object")
                assert isinstance(payload, dict)
                fulfillment = dict(payload)
                _require(
                    fulfillment.get("status") == "cancelled",
                    "fulfillment: status must be cancelled",
                )
                api.expect_conflict(
                    "PATCH",
                    f"/fulfillment/jobs/{quote(str(fulfillment['id']), safe='')}",
                    {"status": "completed"},
                    context="cancelled fulfillment completion",
                )
            persisted_order = api.get_object(f"/orders/{quote(str(order['id']), safe='')}")
            _require(
                persisted_order.get("payment_state") == "authorized",
                "orders: authorized payment must persist",
            )
            audit = api.get_object(f"/audit?entity_id={quote(str(order['id']), safe='')}")
            _require(_payload_has_items(audit), "audit: expected nonempty items")
            report = api.get_object("/admin/reports/summary")
            for key in ("orders", "inventory", "fulfillment"):
                _require(key in report, f"admin report: missing {key}")
            _stop_tree(process, taskkill)
            process = None
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


def validate_ultra_portfolio_api(root: Path, timeout_seconds: float) -> tuple[str, ...]:
    """Check the ultra portfolio-OS contract with RBAC, analytics and persistence."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return ("timeout_seconds must be finite and positive",)
    if _PLATFORM not in ("posix", "nt"):
        return (f"unsupported platform for process-tree cleanup: {_PLATFORM}",)
    npm = shutil.which("npm")
    if npm is None:
        return ("npm executable unavailable; ultra portfolio API was not validated",)
    taskkill = shutil.which("taskkill") if _PLATFORM == "nt" else None
    if _PLATFORM == "nt" and taskkill is None:
        return ("unsupported Windows environment: taskkill unavailable for process-tree cleanup",)

    failures: list[str] = []
    process: subprocess.Popen[bytes] | None = None
    data: tempfile.TemporaryDirectory[str] | None = None
    deadline = time.monotonic() + timeout_seconds
    phase = "startup"
    program: dict[str, object] = {}
    project: dict[str, object] = {}
    dependency: dict[str, object] = {}
    approval: dict[str, object] = {}
    try:
        root = root.resolve(strict=True)
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        scripts = package.get("scripts") if isinstance(package, dict) else None
        _require(
            isinstance(scripts, dict)
            and isinstance(scripts.get("start"), str)
            and bool(scripts["start"].strip()),
            "package.json must provide an npm start script",
        )
        data = tempfile.TemporaryDirectory(prefix="ultra-portfolio-api-")
        env = _environment(data.name)
        for cycle in range(2):
            phase = "portfolio workflow" if cycle == 0 else "portfolio persistence after restart"
            port = _free_port()
            env["PORT"] = str(port)
            _remaining(deadline)
            process = subprocess.Popen(
                [npm, "start"],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=_PLATFORM == "posix",
            )
            api = _PortfolioAPI(port, deadline)
            _ready_portfolio(api, process)
            if cycle == 0:
                program = api.create("/programs", {"name": "Transformation Portfolio"})
                project = api.create(
                    "/projects",
                    {
                        "program_id": program["id"],
                        "name": "Customer Migration",
                        "owner": "pm@example.test",
                    },
                )
                sibling = api.create(
                    "/projects",
                    {
                        "program_id": program["id"],
                        "name": "Billing Modernization",
                        "owner": "pm2@example.test",
                    },
                )
                api.create(
                    f"/projects/{quote(str(project['id']), safe='')}/milestones",
                    {"name": "Pilot", "due_at": "2030-03-01"},
                )
                api.create(
                    f"/projects/{quote(str(project['id']), safe='')}/budgets",
                    {"category": "engineering", "amount": 125000},
                )
                api.create(
                    f"/projects/{quote(str(project['id']), safe='')}/staffing",
                    {"person": "Ava", "role": "lead", "allocation": 0.5},
                )
                api.create(
                    f"/projects/{quote(str(project['id']), safe='')}/risks",
                    {"title": "Data readiness", "severity": "high"},
                )
                dependency = api.create(
                    "/dependencies",
                    {"from_project_id": project["id"], "to_project_id": sibling["id"]},
                )
                api.expect_blocked(
                    "POST",
                    "/dependencies",
                    {"from_project_id": project["id"], "to_project_id": "missing-project"},
                    context="invalid dependency",
                )
                approval = api.create(
                    "/approvals",
                    {
                        "project_id": project["id"],
                        "requested_by": "pm@example.test",
                        "action": "launch",
                    },
                )
                api.expect_blocked(
                    "PATCH",
                    f"/approvals/{quote(str(approval['id']), safe='')}",
                    {"decision": "approved", "role": "viewer"},
                    context="viewer approval",
                )
                status, payload = api.request(
                    "PATCH",
                    f"/approvals/{quote(str(approval['id']), safe='')}",
                    {"decision": "approved", "role": "portfolio_admin"},
                )
                _require(status == 200, f"admin approval: expected 200, got {status}")
                _require(isinstance(payload, dict), "admin approval: expected object")
                assert isinstance(payload, dict)
                approval = dict(payload)
                _require(approval.get("decision") == "approved", "admin approval: decision mismatch")
                status, payload = api.request(
                    "POST",
                    "/access/check",
                    {"project_id": project["id"], "role": "viewer", "action": "approve"},
                )
                _require(status == 200, f"access check: expected 200, got {status}")
                _require(isinstance(payload, dict), "access check: expected object")
                assert isinstance(payload, dict)
                _require(payload.get("allowed") is False, "access check: viewer approve must deny")
            persisted_project = api.get_object(f"/projects/{quote(str(project['id']), safe='')}")
            _require(
                persisted_project.get("program_id") == program["id"],
                "projects: program link must persist",
            )
            persisted_dependency = api.get_object(
                f"/dependencies/{quote(str(dependency['id']), safe='')}"
            )
            _require(
                persisted_dependency.get("from_project_id") == project["id"],
                "dependencies: from project must persist",
            )
            status, csv_payload = api.request(
                "GET",
                f"/analytics/portfolio.csv?program_id={quote(str(program['id']), safe='')}",
            )
            _require(status == 200, f"portfolio CSV: expected 200, got {status}")
            _require(
                isinstance(csv_payload, str) and "Customer Migration" in csv_payload,
                "portfolio CSV: expected project row",
            )
            read_model = api.get_object(
                f"/portfolio/read-model?program_id={quote(str(program['id']), safe='')}&limit=100"
            )
            _require(_payload_has_items(read_model), "portfolio read model: expected nonempty items")
            _stop_tree(process, taskkill)
            process = None
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


def _ready_crm(api: _TenantCRMAPI, process: subprocess.Popen[bytes], tenant: str) -> None:
    path = api.tenant_path(tenant, "accounts")
    while True:
        _remaining(api.deadline)
        _require(process.poll() is None, "startup: npm start exited before CRM API became ready")
        try:
            api.request("GET", path)
            return
        except (OSError, http.client.HTTPException):
            time.sleep(min(0.05, _remaining(api.deadline)))


def _contains_id(items: Sequence[Mapping[str, object]], expected: Mapping[str, object]) -> bool:
    expected_id = expected.get("id")
    return any(item.get("id") == expected_id for item in items)


def _ready_order_ops(api: _OrderOpsAPI, process: subprocess.Popen[bytes]) -> None:
    while True:
        _remaining(api.deadline)
        _require(
            process.poll() is None,
            "startup: npm start exited before order operations API became ready",
        )
        try:
            api.request("GET", "/catalog/items")
            return
        except (OSError, http.client.HTTPException):
            time.sleep(min(0.05, _remaining(api.deadline)))


def _payload_has_items(payload: Mapping[str, object]) -> bool:
    items = payload.get("items")
    return isinstance(items, list) and bool(items)


def _ready_portfolio(api: _PortfolioAPI, process: subprocess.Popen[bytes]) -> None:
    while True:
        _remaining(api.deadline)
        _require(process.poll() is None, "startup: npm start exited before portfolio API became ready")
        try:
            api.request("GET", "/programs")
            return
        except (OSError, http.client.HTTPException):
            time.sleep(min(0.05, _remaining(api.deadline)))
