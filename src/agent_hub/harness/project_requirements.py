"""Independent business checks for an already built, isolated Node project."""

from __future__ import annotations

import csv
import ctypes
import http.client
import json
import math
import os
import runpy
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from io import StringIO
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from uuid import uuid4

_PLATFORM = os.name
_MAX_RESPONSE_BYTES = 1024 * 1024
_TH32CS_SNAPPROCESS = 0x00000002


def _dispatch_validation(
    root: Path, timeout_seconds: float, scale: str,
    fixture_validator: Callable[[Path, float], tuple[str, ...]],
) -> tuple[str, ...]:
    if _PLATFORM == "posix":
        from agent_hub.harness.project_validation_sandbox import validate_requirements

        return validate_requirements(root, scale, timeout_seconds)
    # Windows compatibility is for trusted local fixtures, never Linux production.
    return fixture_validator(root, timeout_seconds)


def validate_small_task_api(root: Path, timeout_seconds: float) -> tuple[str, ...]:
    return _dispatch_validation(root, timeout_seconds, "small", _validate_small_task_api)


def validate_medium_crm_api(root: Path, timeout_seconds: float) -> tuple[str, ...]:
    return _dispatch_validation(root, timeout_seconds, "medium", _validate_medium_crm_api)


def validate_large_order_ops_api(root: Path, timeout_seconds: float) -> tuple[str, ...]:
    return _dispatch_validation(root, timeout_seconds, "large", _validate_large_order_ops_api)


def validate_large_order_modules(root: Path, timeout_seconds: float) -> dict[str, object]:
    if _PLATFORM == "posix":
        from agent_hub.harness.project_validation_sandbox import validate_scale_modules

        return validate_scale_modules(root, "large", timeout_seconds)
    # Trusted Windows fixtures can observe behavior but cannot prove isolation.
    return _validate_large_order_modules(root, timeout_seconds)


def _validate_large_order_modules(root: Path, timeout_seconds: float) -> dict[str, object]:
    evaluator = runpy.run_path(str(Path(__file__).with_name("project_validation_modules.py")))
    return cast(dict[str, object], evaluator["validate_large_modules"](root, timeout_seconds))


def validate_ultra_portfolio_api(root: Path, timeout_seconds: float) -> tuple[str, ...]:
    return _dispatch_validation(root, timeout_seconds, "ultra", _validate_ultra_portfolio_api)


def validate_ultra_portfolio_load(root: Path, timeout_seconds: float) -> dict[str, object]:
    if _PLATFORM == "posix":
        from agent_hub.harness.project_validation_sandbox import validate_scale_load

        return validate_scale_load(root, "ultra", timeout_seconds)
    return _validate_ultra_portfolio_load(root, timeout_seconds)


def validate_ultra_portfolio_storage(root: Path, timeout_seconds: float) -> dict[str, object]:
    if _PLATFORM == "posix":
        from agent_hub.harness.project_validation_sandbox import validate_scale_storage

        return validate_scale_storage(root, "ultra", timeout_seconds)
    return _validate_ultra_portfolio_storage(root, timeout_seconds)


class _ValidationFailure(Exception):
    pass


class _WindowsProcessEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_char * 260),
    ]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _ValidationFailure(message)


def _payload_summary(payload: object, *, limit: int = 500) -> str:
    try:
        text = (
            payload
            if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
    except (TypeError, ValueError):
        text = repr(payload)
    summary = " ".join(text.split())
    if len(summary) > limit:
        return f"{summary[:limit - 3]}..."
    return summary


def _status_message(
    context: str,
    *,
    expected: str,
    actual: int,
    payload: object,
) -> str:
    if payload is None:
        return f"{context}: expected {expected}, got {actual}"
    return f"{context}: expected {expected}, got {actual}; body={_payload_summary(payload)}"


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ValidationFailure("timeout: small task API validation deadline exceeded")
    return remaining


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _windows_kernel32() -> Any:
    if sys.platform != "win32":
        raise _ValidationFailure("Windows cleanup is unavailable on this platform")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateToolhelp32Snapshot": ((wintypes.DWORD, wintypes.DWORD), wintypes.HANDLE),
        "Process32First": (
            (wintypes.HANDLE, ctypes.POINTER(_WindowsProcessEntry32)), wintypes.BOOL,
        ),
        "Process32Next": (
            (wintypes.HANDLE, ctypes.POINTER(_WindowsProcessEntry32)), wintypes.BOOL,
        ),
        "OpenProcess": ((wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE),
        "GetProcessTimes": (
            (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4), wintypes.BOOL,
        ),
        "TerminateProcess": ((wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
        "WaitForSingleObject": ((wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD),
        "CloseHandle": ((wintypes.HANDLE,), wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes = arguments
        function.restype = result
    return kernel


def _windows_process_parents(kernel: Any) -> dict[int, int]:
    if sys.platform != "win32":
        raise _ValidationFailure("Windows cleanup is unavailable on this platform")
    snapshot = kernel.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    parents: dict[int, int] = {}
    entry = _WindowsProcessEntry32()
    entry.dwSize = ctypes.sizeof(_WindowsProcessEntry32)
    try:
        has_entry = kernel.Process32First(snapshot, ctypes.byref(entry))
        while has_entry:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            has_entry = kernel.Process32Next(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(error)
    finally:
        kernel.CloseHandle(snapshot)
    return parents


def _windows_creation_time(kernel: Any, handle: int) -> int:
    if sys.platform != "win32":
        raise _ValidationFailure("Windows cleanup is unavailable on this platform")
    from ctypes import wintypes

    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime)


def _kill_windows_process_tree(process: subprocess.Popen[bytes]) -> tuple[str, ...]:
    if sys.platform != "win32":
        raise _ValidationFailure("Windows cleanup is unavailable on this platform")
    kernel = _windows_kernel32()
    deadline = time.monotonic() + 3
    errors: list[str] = []
    root_pid = process.pid
    root_handle = int(cast(Any, process)._handle)
    with ExitStack() as stack:
        root_created: int | None = None
        try:
            root_created = _windows_creation_time(kernel, root_handle)
        except OSError as exc:
            errors.append(f"pid {root_pid}: identity query failed: {exc}")
        identities: dict[int, tuple[int, int | None]] = {root_pid: (root_handle, root_created)}
        seen = {root_pid}
        try:
            parents = _windows_process_parents(kernel)
        except OSError as exc:
            errors.append(f"process snapshot query failed: {exc}")
            parents = {}
        while True:
            children: dict[int, list[int]] = {}
            for pid, parent in parents.items():
                children.setdefault(parent, []).append(pid)
            candidates = identities.copy()
            ordered = list(identities)
            for parent in ordered:
                for pid in children.get(parent, ()):
                    if pid in seen:
                        continue
                    if time.monotonic() >= deadline:
                        errors.append("identity discovery exceeded 3s deadline")
                        break
                    seen.add(pid)
                    parent_created = candidates[parent][1]
                    if parent_created is None:
                        errors.append(f"pid {pid}: parent identity query unavailable")
                        continue
                    # Hold identity handles across every snapshot and termination.
                    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
                    if not handle:
                        error = ctypes.get_last_error()
                        errors.append(f"pid {pid}: subtree identity unavailable: {ctypes.WinError(error)}")
                        continue
                    handle = cast(int, handle)
                    stack.callback(kernel.CloseHandle, handle)
                    try:
                        created = _windows_creation_time(kernel, handle)
                    except OSError as exc:
                        errors.append(f"pid {pid}: identity query failed: {exc}")
                        continue
                    if created < parent_created:
                        continue
                    candidates[pid] = (handle, created)
                    ordered.append(pid)
            if time.monotonic() >= deadline:
                errors.append("identity confirmation exceeded 3s deadline")
                current_parents = {}
            else:
                try:
                    current_parents = _windows_process_parents(kernel)
                except OSError as exc:
                    errors.append(f"process snapshot query failed: {exc}")
                    current_parents = {}
            accepted = set(identities)
            for pid in ordered:
                if pid in identities:
                    continue
                parent = parents[pid]
                same_parent = current_parents.get(pid) == parent
                exited = (
                    pid not in current_parents
                    and kernel.WaitForSingleObject(candidates[pid][0], 0) == 0
                )
                if parent in accepted and (same_parent or exited):
                    accepted.add(pid)
                    identities[pid] = candidates[pid]
            waiting: list[tuple[int, int]] = []
            for pid in reversed(identities):
                handle = identities[pid][0]
                state = kernel.WaitForSingleObject(handle, 0)
                if state == 0:
                    continue
                if state != 258:
                    errors.append(f"pid {pid}: process state query failed")
                    continue
                terminate_handle = handle if pid == root_pid else kernel.OpenProcess(1, False, pid)
                if terminate_handle and pid != root_pid:
                    stack.callback(kernel.CloseHandle, terminate_handle)
                if not terminate_handle or not kernel.TerminateProcess(terminate_handle, 1):
                    error = ctypes.get_last_error()
                    if error == 5 and kernel.WaitForSingleObject(handle, 0) == 0:
                        continue
                    errors.append(f"pid {pid}: {ctypes.WinError(error)}")
                    continue
                waiting.append((pid, handle))
            for pid, handle in waiting:
                milliseconds = max(0, int((deadline - time.monotonic()) * 1000))
                if kernel.WaitForSingleObject(handle, milliseconds) != 0:
                    errors.append(f"pid {pid}: cleanup wait failed or exceeded 3s deadline")
            # Check after termination too: a confirmed parent may have spawned
            # another child between the identity snapshot and TerminateProcess.
            if time.monotonic() >= deadline:
                errors.append("descendant cleanup exceeded 3s deadline")
                break
            try:
                parents = _windows_process_parents(kernel)
            except OSError as exc:
                errors.append(f"process snapshot query failed: {exc}")
                break
            new_children = any(
                pid not in seen and parent in identities for pid, parent in parents.items()
            )
            known_live = any(kernel.WaitForSingleObject(handle, 0) != 0 for handle, _ in identities.values())
            if not new_children:
                if known_live and not errors:
                    errors.append("known process remains alive after cleanup")
                break
            if time.monotonic() >= deadline:
                errors.append("descendant cleanup exceeded 3s deadline")
                break
    return tuple(errors)


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
        kill_errors = _kill_windows_process_tree(process)
        if kill_errors:
            raise _ValidationFailure(f"cleanup: process tree kill failed; {'; '.join(kill_errors)}")
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
        _require(
            status == 201,
            _status_message(f"POST {resource}", expected="201", actual=status, payload=payload),
        )
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
        _require(
            status == 200,
            _status_message(f"GET {resource}", expected="200", actual=status, payload=payload),
        )
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
        _require(
            status == 404,
            _status_message(context, expected="404", actual=status, payload=payload),
        )
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

            # Bound headers and body together, including continuously streaming bytes.
            timer = threading.Timer(_remaining(self.deadline), interrupt)
            timer.daemon = True
            timer.start()
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
            if timer is not None:
                timer.cancel()
                timer.join(timeout=1)
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


def _environment(data: str, runtime: str | None = None) -> dict[str, str]:
    # Do not expose host/provider credentials or the host's npm user configuration.
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
    }
    runtime = data if runtime is None else runtime
    env.update({
        "DATA_DIR": data, "HOME": runtime, "USERPROFILE": runtime,
        "TMP": runtime, "TEMP": runtime, "TMPDIR": runtime,
        "npm_config_userconfig": str(Path(runtime) / "user.npmrc"),
        "npm_config_globalconfig": str(Path(runtime) / "global.npmrc"),
        "npm_config_cache": str(Path(runtime) / "npm-cache"),
        "npm_config_update_notifier": "false", "npm_config_audit": "false",
    })
    return env


def _validate_small_task_api(root: Path, timeout_seconds: float) -> tuple[str, ...]:
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


def _validate_medium_crm_api(root: Path, timeout_seconds: float) -> tuple[str, ...]:
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
                _require(
                    status == 200,
                    _status_message(
                        "PATCH opportunity",
                        expected="200",
                        actual=status,
                        payload=payload,
                    ),
                )
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


def _order_report(api: _OrderOpsAPI) -> dict[str, int]:
    report = api.get_object("/admin/reports/summary")
    metrics: dict[str, int] = {}
    for section, fields in (
        ("orders", ("total", "authorized_count")),
        ("inventory", ("reserved_units",)),
        ("fulfillment", ("total", "cancelled_count")),
    ):
        value = report.get(section)
        _require(isinstance(value, dict), f"admin report: expected {section} object")
        assert isinstance(value, dict)
        for field in fields:
            counter = value.get(field)
            _require(
                type(counter) is int and counter >= 0,
                f"admin report: {section}.{field} must be a nonnegative integer",
            )
            assert isinstance(counter, int)
            metrics[f"{section}.{field}"] = counter
    return metrics


def _order_audit(
    api: _OrderOpsAPI, order_id: object,
    required_actions: tuple[str, ...] = ("order.created", "payment.authorized"),
) -> None:
    audit = api.get_object(f"/audit?entity_id={quote(str(order_id), safe='')}")
    items = audit.get("items")
    _require(isinstance(items, list) and bool(items), "audit: expected nonempty items")
    assert isinstance(items, list)
    actions: set[str] = set()
    for item in items:
        _require(isinstance(item, dict), "audit: expected object items")
        assert isinstance(item, dict)
        _require(item.get("entity_id") == order_id, "audit: wrong entity_id")
        action = item.get("action")
        _require(isinstance(action, str) and bool(action), "audit: missing action")
        assert isinstance(action, str)
        actions.add(action)
    _require(
        set(required_actions) <= actions,
        "audit: missing required order action",
    )


def _validate_large_order_ops_api(root: Path, timeout_seconds: float) -> tuple[str, ...]:
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
    inventory_sku = f"RESERVE-{uuid4().hex[:8]}"
    remaining_sku = f"REMAINING-{uuid4().hex[:8]}"
    order: dict[str, object] = {}
    other_order: dict[str, object] = {}
    fulfillment: dict[str, object] = {}
    baseline: dict[str, int] = {}
    final_report: dict[str, int] = {}
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
                baseline = _order_report(api)
                item = api.create(
                    "/catalog/items",
                    {"sku": inventory_sku, "name": "Reservation Widget", "price": 1200},
                )
                _require(item.get("sku") == inventory_sku, "catalog: sku was not preserved")
                stock = api.create("/inventory/stock", {"sku": inventory_sku, "quantity": 2})
                _require(stock.get("sku") == inventory_sku, "inventory stock: sku mismatch")
                reservation = api.create(
                    "/inventory/reservations",
                    {"sku": inventory_sku, "quantity": 1, "reason": "acceptance"},
                )
                _require(reservation.get("sku") == inventory_sku, "reservation: sku mismatch")
                api.expect_conflict(
                    "POST",
                    "/inventory/reservations",
                    {"sku": inventory_sku, "quantity": 99, "reason": "conflict"},
                    context="stock conflict reservation",
                )
                second_reservation = api.create(
                    "/inventory/reservations",
                    {"sku": inventory_sku, "quantity": 1, "reason": "remaining stock"},
                )
                _require(
                    second_reservation.get("sku") == inventory_sku,
                    "remaining reservation: sku mismatch",
                )
                api.expect_conflict(
                    "POST", "/inventory/reservations",
                    {"sku": inventory_sku, "quantity": 1, "reason": "exhausted"},
                    context="exhausted inventory reservation",
                )
                reserved_report = _order_report(api)
                expected_reserved_report = dict(baseline)
                expected_reserved_report["inventory.reserved_units"] += 2
                _require(
                    reserved_report == expected_reserved_report,
                    "admin report: explicit reservations must add exactly two reserved units",
                )
                # Replenishing exactly one unit exposes failed requests that drove
                # an exhausted balance negative. Keep another SKU nonempty at restart.
                api.create("/inventory/stock", {"sku": inventory_sku, "quantity": 1})
                api.create(
                    "/inventory/reservations",
                    {"sku": inventory_sku, "quantity": 1, "reason": "replenished"},
                )
                api.create(
                    "/catalog/items", {"sku": remaining_sku, "name": "Persist Widget", "price": 1200},
                )
                api.create("/inventory/stock", {"sku": remaining_sku, "quantity": 2})
                api.create(
                    "/inventory/reservations",
                    {"sku": remaining_sku, "quantity": 1, "reason": "keep one unit"},
                )
                expected_reserved_report["inventory.reserved_units"] += 2
                _require(
                    _order_report(api) == expected_reserved_report,
                    "admin report: replenishment and partial inventory must reflect reservations",
                )
                # Orders may reserve stock implicitly, so use a separate SKU.
                api.create(
                    "/catalog/items", {"sku": sku, "name": "Order Widget", "price": 1200},
                )
                api.create("/inventory/stock", {"sku": sku, "quantity": 2})
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
                other_order = api.create(
                    "/orders", {
                        "customer_id": "another-customer",
                        "client_request_id": f"other-{uuid4().hex[:8]}",
                        "lines": [{"sku": sku, "quantity": 1}],
                    },
                )
                _require(other_order["id"] != order["id"], "orders: duplicate id")
            persisted_order = api.get_object(f"/orders/{quote(str(order['id']), safe='')}")
            _require(
                persisted_order.get("payment_state") == "authorized",
                "orders: authorized payment must persist",
            )
            _order_audit(api, order["id"])
            _order_audit(api, other_order["id"], ("order.created",))
            report = _order_report(api)
            if cycle == 0:
                for metric, increment in (
                    ("orders.total", 2), ("orders.authorized_count", 1),
                    ("fulfillment.total", 1), ("fulfillment.cancelled_count", 1),
                ):
                    _require(
                        report[metric] == baseline[metric] + increment,
                        f"admin report: {metric} must reflect the actual business write",
                    )
                _require(
                    report["inventory.reserved_units"] >= baseline["inventory.reserved_units"] + 4,
                    "admin report: completed order must not discard explicit reservations",
                )
                final_report = report
            else:
                _require(report == final_report, "admin report: metrics must persist after restart")
                api.expect_conflict(
                    "POST", "/inventory/reservations",
                    {"sku": inventory_sku, "quantity": 1, "reason": "after restart"},
                    context="exhausted inventory must persist after restart",
                )
                api.create("/inventory/stock", {"sku": inventory_sku, "quantity": 1})
                for reserved_sku in (inventory_sku, remaining_sku):
                    api.create(
                        "/inventory/reservations",
                        {"sku": reserved_sku, "quantity": 1, "reason": "persisted balance"},
                    )
                api.expect_conflict(
                    "POST", "/inventory/reservations",
                    {"sku": remaining_sku, "quantity": 1, "reason": "persisted balance exhausted"},
                    context="remaining inventory must persist exactly after restart",
                )
                expected_after_restart = dict(final_report)
                expected_after_restart["inventory.reserved_units"] += 2
                _require(
                    _order_report(api) == expected_after_restart,
                    "admin report: restarted inventory reservations must add exactly two units",
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


def _business_record_fields(
    actual: Mapping[str, object], expected: Mapping[str, object], context: str,
) -> None:
    for field, value in expected.items():
        received = actual.get(field)
        numeric = type(value) in (int, float)
        _require(
            received == value and (not numeric or type(received) in (int, float)),
            f"{context}: {field} must preserve the submitted value",
        )


def _business_items(payload: Mapping[str, object], context: str) -> list[dict[str, object]]:
    items = payload.get("items")
    _require(isinstance(items, list), f"{context}: expected items array")
    assert isinstance(items, list)
    _require(all(isinstance(item, dict) for item in items), f"{context}: expected object items")
    return cast(list[dict[str, object]], items)


def _portfolio_analytics(
    api: _PortfolioAPI, projects: Sequence[Mapping[str, object]],
    expected_metrics: Mapping[str, Mapping[str, object]],
) -> None:
    for program_id in dict.fromkeys(str(project["program_id"]) for project in projects):
        selected = [project for project in projects if str(project["program_id"]) == program_id]
        status, payload = api.request(
            "GET", f"/analytics/portfolio.csv?program_id={quote(program_id, safe='')}",
        )
        _require(status == 200, f"portfolio CSV: expected 200, got {status}")
        _require(isinstance(payload, str), "portfolio CSV: expected text")
        assert isinstance(payload, str)
        try:
            reader = csv.DictReader(StringIO(payload), strict=True)
            columns = reader.fieldnames or []
            _require(
                {"project_id", "program_id", "name"} <= set(columns)
                and len(columns) == len(set(columns)),
                "portfolio CSV: missing or duplicate columns",
            )
            rows = list(reader)
        except csv.Error as exc:
            raise _ValidationFailure("portfolio CSV: malformed quoting") from exc
        _require(
            all(None not in row and all(value is not None for value in row.values()) for row in rows),
            "portfolio CSV: malformed row columns",
        )
        actual_rows = [
            (row["project_id"], row["program_id"], row["name"]) for row in rows
        ]
        expected_rows = [
            (str(project["id"]), str(project["program_id"]), str(project["name"]))
            for project in selected
        ]
        _require(
            sorted(actual_rows) == sorted(expected_rows),
            "portfolio CSV: expected exact filtered project rows without duplicates",
        )
        read_model = api.get_object(
            f"/portfolio/read-model?program_id={quote(program_id, safe='')}&limit=100",
        )
        items = _business_items(read_model, "portfolio read model")
        _require(len(items) == len(selected), "portfolio read model: filtered row count mismatch")
        for project in selected:
            matches = [item for item in items if item.get("project_id") == project["id"]]
            _require(len(matches) == 1, "portfolio read model: missing or duplicate project")
            expected = {
                "project_id": project["id"], "program_id": project["program_id"],
                "name": project["name"], **expected_metrics[str(project["id"])],
            }
            _business_record_fields(matches[0], expected, "portfolio read model")


def _validate_ultra_portfolio_api(root: Path, timeout_seconds: float) -> tuple[str, ...]:
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
    projects: list[dict[str, object]] = []
    records: dict[str, list[dict[str, object]]] = {}
    expected_metrics: dict[str, dict[str, object]] = {}
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
                other_program = api.create("/programs", {"name": "Independent Portfolio"})
                _require(program["id"] != other_program["id"], "programs: duplicate id")
                marker = uuid4().hex[:8]
                for name, program_id in (
                    (f'Customer Migration, "{marker}"', program["id"]),
                    (f'Billing Modernization, "{marker}"', program["id"]),
                    (f'Independent Project, "{marker}"', other_program["id"]),
                ):
                    body: dict[str, object] = {
                        "program_id": program_id, "name": name, "owner": "pm@example.test",
                    }
                    created = api.create("/projects", body)
                    _business_record_fields(created, body, "projects")
                    projects.append({"id": created["id"], **body})
                _require(
                    len({str(item["id"]) for item in projects}) == len(projects),
                    "projects: duplicate id",
                )
                project, sibling, _ = projects
                budget_amount = 125001 + int(marker, 16) % 10000
                module_requests: dict[str, tuple[dict[str, object], ...]] = {
                    "milestones": (
                        {"name": f"Pilot {marker}", "due_at": "2030-03-01"},
                        {"name": f"Launch {marker}", "due_at": "2030-04-01"},
                    ),
                    "budgets": (
                        {"category": "engineering", "amount": budget_amount},
                        {"category": "operations", "amount": 7500},
                    ),
                    "staffing": (
                        {"person": f"Ava {marker}", "role": "lead", "allocation": 0.5},
                        {"person": f"Sam {marker}", "role": "reviewer", "allocation": 0.25},
                    ),
                    "risks": (
                        {"title": f"Data readiness {marker}", "severity": "high"},
                        {"title": f"Schedule {marker}", "severity": "low"},
                    ),
                }
                for module, bodies in module_requests.items():
                    records[module] = []
                    for module_body in bodies:
                        created = api.create(
                            f"/projects/{quote(str(project['id']), safe='')}/{module}", module_body,
                        )
                        expected_record = {
                            "id": created["id"], "project_id": project["id"], **module_body,
                        }
                        _business_record_fields(created, expected_record, module)
                        _require(
                            all(item["id"] != created["id"] for item in records[module]),
                            f"{module}: duplicate id",
                        )
                        records[module].append(expected_record)
                for item in projects:
                    populated = item["id"] == project["id"]
                    expected_metrics[str(item["id"])] = {
                        "budget_total": budget_amount + 7500 if populated else 0,
                        "staffing_allocation": 0.75 if populated else 0,
                        "risk_count": 2 if populated else 0,
                        "milestone_count": 2 if populated else 0,
                    }
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
            for item in projects:
                prefix = f"/projects/{quote(str(item['id']), safe='')}"
                _business_record_fields(api.get_object(prefix), item, "projects persistence")
                for module, expected_records in records.items():
                    context = f"{module} persistence"
                    items = _business_items(api.get_object(f"{prefix}/{module}"), context)
                    populated = item["id"] == project["id"]
                    _require(
                        len(items) == (len(expected_records) if populated else 0),
                        f"{context}: expected stored records filtered by project",
                    )
                    if populated:
                        for expected_record in expected_records:
                            matches = [row for row in items if row.get("id") == expected_record["id"]]
                            _require(len(matches) == 1, f"{context}: missing or duplicate id")
                            _business_record_fields(matches[0], expected_record, context)
            persisted_dependency = api.get_object(
                f"/dependencies/{quote(str(dependency['id']), safe='')}"
            )
            _require(
                persisted_dependency.get("from_project_id") == project["id"],
                "dependencies: from project must persist",
            )
            _portfolio_analytics(api, projects, expected_metrics)
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


_STORAGE_GUARD = r"""
import json, os, sys
from pathlib import Path
blocked, network, selected, npm, token = json.loads(sys.argv[1])
if Path('/proc/self/ns/net').stat().st_ino != network:
    raise SystemExit(71)
for name in blocked:
    if os.path.lexists(name):
        raise SystemExit(72)
for name in selected:
    if not Path(name).is_dir() or os.path.realpath(name) != name:
        raise SystemExit(73)
if os.getcwd() != selected[0] or os.environ['DATA_DIR'] != selected[1]:
    raise SystemExit(74)
os.write(1, token.encode('ascii'))
null = os.open(os.devnull, os.O_WRONLY)
os.dup2(null, 1)
os.close(null)
os.execv(npm, [npm, 'start'])
"""


_STORAGE_CLEANUP = r"""
import json, shutil, sys
from pathlib import Path
name, parent, identity = json.loads(sys.argv[1])
root = Path(name)
info = root.lstat()
if (root.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400
        or root.resolve(strict=True) != root or root.parent != Path(parent)
        or [info.st_dev, info.st_ino] != identity):
    raise SystemExit(75)
shutil.rmtree(root)
"""


class _UltraStorageContext:
    """Own one fixed A/B/A/C challenge; the legacy load retains its HTTP oracle."""

    def __init__(self, root: Path, deadline: float) -> None:
        self.source = root.resolve(strict=True)
        self.deadline = deadline
        self.checks: dict[str, Any] = {}
        for name, keys in (
            ("data_dir_isolation", (
                "starts", "stops", "empty_program_checks", "marker_writes", "marker_readbacks",
                "original_program_checks", "marker_absence_checks",
            )),
            ("same_version_relocation", (
                "starts", "stops", "target_projects", "foreign_projects", "traversals",
                "read_model_requests", "original_program_checks", "data_files", "data_bytes",
            )),
        ):
            self.checks[name] = {
                "status": "unknown", "reasons": ["storage check not completed"],
                "cleanup_ok": True, "measurements": dict.fromkeys(keys, 0),
            }
        self.checks["same_version_relocation"]["measurements"].update(
            old_paths_unavailable=False, source_data_sha256=None, copied_data_sha256=None,
            frozen_code_sha256=None, relocated_code_sha256=None,
        )
        self.active = "data_dir_isolation"
        self.temporary: tempfile.TemporaryDirectory[str] | None = None
        self.processes: dict[int, tuple[subprocess.Popen[bytes], str, int]] = {}
        self.runtimes: list[Path] = []
        self.programs: list[dict[str, object]] = []
        self.target_rows: list[dict[str, object]] = []
        self.foreign_rows: list[dict[str, object]] = []
        self.project_records: dict[str, dict[str, object]] = {}
        self.markers: list[dict[str, object]] = []

    def prepare(self) -> None:
        if _PLATFORM not in {"nt", "posix"}:
            raise OSError("storage validation requires Linux isolation or trusted Windows fixtures")
        if _PLATFORM == "posix" and (
            sys.platform != "linux" or self.source != Path("/workspace")
            or not Path(__file__).resolve().is_relative_to("/opt/validator/src")
        ):
            raise OSError("storage validation requires the outer private-network validator")
        self.helpers = runpy.run_path(str(Path(__file__).with_name("project_validation_storage.py")))
        self.temporary = tempfile.TemporaryDirectory(prefix="ultra-storage-owned-")
        # Cleanup is explicit and identity checked, including after exceptional exits.
        cast(Any, self.temporary)._finalizer.detach()
        self.owned = Path(self.temporary.name).resolve(strict=True)
        self.owned_identity = (self.owned.stat().st_dev, self.owned.stat().st_ino)
        self.code_a, self.code_c = self.owned / "code-a", self.owned / "code-c"
        self.data_a, self.data_b, self.data_c = (self.owned / f"data-{s}" for s in "abc")
        first = self.copy(self.source, self.code_a, data=False)
        second = self.copy(self.source, self.code_c, data=False)
        _require(first["sha256"] == second["sha256"], "frozen code changed between copies")
        self.checks["same_version_relocation"]["measurements"].update(
            frozen_code_sha256=first["sha256"], relocated_code_sha256=second["sha256"],
        )
        self.data_a.mkdir()
        self.data_b.mkdir()

    def copy(self, source: Path, target: Path, *, data: bool) -> dict[str, Any]:
        _remaining(self.deadline)
        try:
            return cast(dict[str, Any], self.helpers["copy_validation_tree"](
                source, target, deadline=self.deadline, reject_hardlinks=data,
            ))
        except (ValueError, RuntimeError) as exc:
            label = "temporary runtime cleanup failed" if "cleanup" in str(exc) else "unsafe storage copy"
            raise _ValidationFailure(f"{label}: {exc}") from exc

    def fail(self, exc: Exception, *, cleanup: bool = False) -> None:
        check = self.checks[self.active]
        failed = cleanup or (isinstance(exc, _ValidationFailure) and not str(exc).startswith("timeout:"))
        if check["status"] != "failed":
            check["status"] = "failed" if failed else "unknown"
        if check["reasons"] == ["storage check not completed"]:
            check["reasons"] = []
        check["reasons"].append(str(exc))
        if cleanup:
            check["cleanup_ok"] = False

    def start(
        self, code: Path, data: Path, check_name: str,
    ) -> tuple[subprocess.Popen[bytes], _PortfolioAPI]:
        self.active = check_name
        _remaining(self.deadline)
        runtime = self.owned / f"runtime-{len(self.runtimes)}"
        runtime.mkdir()
        self.runtimes.append(runtime)
        npm = shutil.which("npm")
        if npm is None:
            raise OSError("npm executable unavailable; storage validation not started")
        env = _environment(str(data), str(runtime))
        port = _free_port()
        env["PORT"] = str(port)
        command = [npm, "start"]
        token: bytes | None = None
        if _PLATFORM == "posix":
            blocked = [str(self.source), "/workspace", "/opt/validator/src"]
            blocked.extend(str(p) for p in (
                self.code_a, self.code_c, self.data_a, self.data_b, self.data_c, *self.runtimes,
            ) if p not in {code, data, runtime})
            command = [
                "/usr/bin/bwrap", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
                "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
                "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
                "--ro-bind", "/lib", "/lib", "--proc", "/proc", "--dev", "/dev",
                "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/var",
            ]
            for system in (Path("/lib64"), Path("/opt/validator/node")):
                if system.exists():
                    command.extend(("--ro-bind", str(system), str(system)))
            for current in (code, data, runtime):
                command.extend(("--bind", str(current), str(current)))
            command.extend(("--chdir", str(code), "--clearenv"))
            for key, value in env.items():
                command.extend(("--setenv", key, value))
            token = uuid4().hex.encode("ascii")
            guard = json.dumps([
                blocked, Path("/proc/self/ns/net").stat().st_ino,
                [str(code), str(data), str(runtime)], npm, token.decode("ascii"),
            ])
            command.extend(("--", "/usr/bin/python3", "-I", "-B", "-c", _STORAGE_GUARD, guard))
        process = subprocess.Popen(
            command, cwd=code, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if token is not None else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=_PLATFORM == "posix",
        )
        self.processes[process.pid] = (process, check_name, port)
        measurements = self.checks[check_name]["measurements"]
        measurements["starts"] += 1
        try:
            if token is not None:
                assert process.stdout is not None
                try:
                    readable, _, _ = select.select([process.stdout], [], [], _remaining(self.deadline))
                    if not readable or os.read(process.stdout.fileno(), len(token) + 1) != token:
                        raise OSError("nested namespace guard unavailable before npm launch")
                finally:
                    process.stdout.close()
                if check_name == "same_version_relocation":
                    measurements["old_paths_unavailable"] = True
            api = _PortfolioAPI(port, self.deadline)
            _ready_portfolio(api, process)
            _remaining(self.deadline)
            return process, api
        except Exception:
            self.stop(process)
            raise

    def stop(self, process: subprocess.Popen[bytes]) -> None:
        entry = self.processes.get(process.pid)
        if entry is None:
            return
        _, check_name, port = entry
        try:
            _stop_tree(process, shutil.which("taskkill") if _PLATFORM == "nt" else None)
            close_deadline = min(self.deadline, time.monotonic() + 2)
            while True:
                remaining = close_deadline - time.monotonic()
                with socket.socket() as connection:
                    connection.settimeout(max(0.001, min(0.1, remaining)))
                    if connection.connect_ex(("127.0.0.1", port)) != 0:
                        break
                remaining = close_deadline - time.monotonic()
                _require(remaining > 0, "storage process-tree cleanup left a live listener")
                time.sleep(min(0.05, remaining))
        except Exception as exc:
            self.active = check_name
            self.fail(_ValidationFailure(f"process-tree cleanup failed: {exc}"), cleanup=True)
            raise
        self.checks[check_name]["measurements"]["stops"] += 1
        del self.processes[process.pid]

    def check_b(self) -> None:
        process, api = self.start(self.code_a, self.data_b, "data_dir_isolation")
        measured = self.checks["data_dir_isolation"]["measurements"]
        try:
            items = _business_items(api.get_object("/programs"), "B programs")
            _require(not items, "B DATA_DIR must have empty programs")
            measured["empty_program_checks"] += 1
            program_body: dict[str, object] = {"name": f"B program {uuid4().hex}"}
            program = api.create("/programs", program_body)
            _business_record_fields(program, program_body, "B program write")
            _require(isinstance(program["id"], (str, int))
                     and not isinstance(program["id"], bool) and str(program["id"]) != "",
                     "B program invalid id")
            measured["marker_writes"] += 1
            project_body: dict[str, object] = {
                "program_id": program["id"], "name": f"B project {uuid4().hex}",
                "owner": f"b-{uuid4().hex}@example.test",
            }
            project = api.create("/projects", project_body)
            _business_record_fields(project, project_body, "B project write")
            _require(isinstance(project["id"], (str, int))
                     and not isinstance(project["id"], bool) and str(project["id"]) != "",
                     "B project invalid id")
            measured["marker_writes"] += 1
            self.markers = [
                {"id": program["id"], **program_body}, {"id": project["id"], **project_body},
            ]
            programs = _business_items(api.get_object("/programs"), "B marker programs")
            _require(len(programs) == 1, "B must contain only its marker program")
            _business_record_fields(programs[0], self.markers[0], "B program readback")
            measured["marker_readbacks"] += 1
            actual = api.get_object(f"/projects/{quote(str(project['id']), safe='')}")
            _business_record_fields(actual, self.markers[1], "B project readback")
            measured["marker_readbacks"] += 1
        except Exception as exc:
            self.fail(exc)
            raise
        finally:
            self.stop(process)

    def verify_programs(self, api: _PortfolioAPI, check_name: str) -> None:
        items = _business_items(api.get_object("/programs"), "original programs")
        _require(len(items) == 2, "original DATA_DIR must preserve exactly two programs")
        for expected in self.programs:
            matches = [p for p in items if p.get("id") == expected["id"]]
            _require(len(matches) == 1, "original program missing or duplicated")
            _business_record_fields(matches[0], expected, "original program")
            self.checks[check_name]["measurements"]["original_program_checks"] += 1

    def check_a(self, api: _PortfolioAPI) -> None:
        try:
            self.verify_programs(api, "data_dir_isolation")
            measured = self.checks["data_dir_isolation"]["measurements"]
            _require(all(p["name"] != self.markers[0]["name"] for p in self.programs),
                     "B program leaked into A")
            measured["marker_absence_checks"] += 1
            marker = self.markers[1]
            status, actual = api.request("GET", f"/projects/{quote(str(marker['id']), safe='')}")
            expected = self.project_records.get(str(marker["id"]))
            if expected is None:
                _require(status == 404, "B project leaked into A")
            else:
                _require(status == 200 and isinstance(actual, dict), "original A project missing")
                _business_record_fields(cast(dict[str, object], actual), expected, "A project")
            measured["marker_absence_checks"] += 1
        except Exception as exc:
            self.fail(exc)
            raise

    def relocate(self) -> None:
        self.active = "same_version_relocation"
        measured = self.checks[self.active]["measurements"]
        _require(not self.processes, "relocation requires every writer stopped")
        # Reuse the copy helper's canonical snapshot, without creating a third code copy.
        code_tree = self.helpers["_Tree"](self.code_c, self.code_c.lstat(), self.deadline)
        code_summary = self.helpers["_summary"](
            self.helpers["_snapshot"](code_tree, True), self.deadline,
        )
        measured["relocated_code_sha256"] = code_summary["sha256"]
        _require(code_summary["sha256"] == measured["frozen_code_sha256"],
                 "relocated code changed after freeze")
        copied = self.copy(self.data_a, self.data_c, data=True)
        measured.update(data_files=copied["files"], data_bytes=copied["bytes"],
                        source_data_sha256=copied["sha256"], copied_data_sha256=copied["sha256"])
        _require(copied["files"] > 0 and copied["bytes"] > 0, "DATA_DIR has no persisted data")
        process, api = self.start(self.code_c, self.data_c, self.active)
        try:
            self.verify_programs(api, "same_version_relocation")
            for rows, program, counter in (
                (self.target_rows, self.programs[0], "target_projects"),
                (self.foreign_rows, self.programs[1], "foreign_projects"),
            ):
                ordered = sorted(rows, key=lambda p: str(p["project_id"]).encode(
                    "utf-16-be", errors="surrogatepass",
                ))
                for offset in range(0, len(ordered) + 100, 100):
                    _remaining(self.deadline)
                    path = (f"/portfolio/read-model?program_id={quote(str(program['id']), safe='')}"
                            f"&offset={offset}&limit=100")
                    measured["read_model_requests"] += 1
                    items = _business_items(api.get_object(path), "relocated portfolio page")
                    wanted = ordered[offset:offset + 100]
                    _require(len(items) == len(wanted), "relocated portfolio page row count")
                    for actual, expected in zip(items, wanted, strict=True):
                        _business_record_fields(actual, expected, "relocated portfolio row")
                        measured[counter] += 1
                measured["traversals"] += 1
        finally:
            self.stop(process)
        check = self.checks["same_version_relocation"]
        if measured["old_paths_unavailable"]:
            check.update(status="passed", reasons=[])
        else:
            check.update(status="unknown", reasons=[
                "trusted Windows fixture verified semantics only; old paths are not isolated",
            ])

    def cleanup_owned(self) -> None:
        subprocess.run(
            [sys.executable, "-I", "-B", "-c", _STORAGE_CLEANUP, json.dumps([
                str(self.owned), str(Path(tempfile.gettempdir()).resolve(strict=True)),
                list(self.owned_identity),
            ])],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3, check=True,
        )
        _require(not self.owned.exists(), "owned storage cleanup incomplete")

    def cleanup(self) -> None:
        for process, _, _ in list(self.processes.values()):
            try:
                self.stop(process)
            except (OSError, ValueError, _ValidationFailure, subprocess.SubprocessError) as exc:
                self.fail(_ValidationFailure(f"process-tree cleanup failed: {exc}"), cleanup=True)
        if self.temporary is not None:
            try:
                info = self.owned.lstat()
                _require(
                    not self.owned.is_symlink() and self.owned.resolve(strict=True) == self.owned
                    and (info.st_dev, info.st_ino) == self.owned_identity
                    and self.owned.parent == Path(tempfile.gettempdir()).resolve(strict=True),
                    "owned storage cleanup boundary changed",
                )
                self.cleanup_owned()
            except (OSError, ValueError, _ValidationFailure, subprocess.SubprocessError) as exc:
                for name in self.checks:
                    self.active = name
                    self.fail(_ValidationFailure(f"temporary runtime cleanup failed: {exc}"), cleanup=True)


def _validate_ultra_portfolio_storage(root: Path, timeout_seconds: float) -> dict[str, object]:
    protocol = runpy.run_path(str(Path(__file__).with_name("project_validation_result.py")))
    result = cast(dict[str, Any], protocol["scale_validation_unknown"](
        "storage validation not completed", profile="ultra-load-storage-v1",
    ))
    context: _UltraStorageContext | None = None
    errors: list[str] = []
    deadline = time.monotonic()
    try:
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        deadline += timeout_seconds
        context = _UltraStorageContext(root, deadline)
        result["checks"].update(context.checks)
        context.prepare()
        load = _validate_ultra_portfolio_load(context.code_a, timeout_seconds, _storage=context)
        result["checks"]["load"] = load
        if load["status"] == "passed":
            context.checks["data_dir_isolation"].update(status="passed", reasons=[])
            context.relocate()
    except (OSError, ValueError, RuntimeError, _ValidationFailure,
            subprocess.SubprocessError, http.client.HTTPException) as exc:
        if context is not None:
            context.fail(exc)
        else:
            errors.append(str(exc))
    finally:
        if context is not None:
            context.cleanup()
        checks = cast(dict[str, Any], result["checks"])
        cleanup_ok = all(check["cleanup_ok"] for check in checks.values())
        result["cleanup_ok"] = cleanup_ok
        states = [check["status"] for check in checks.values()]
        result["status"] = (
            "failed" if "failed" in states else "unknown" if "unknown" in states else "passed"
        )
        result["reasons"] = [
            f"{name}: {reason}" for name, check in checks.items() for reason in check["reasons"]
        ] + errors
        if time.monotonic() >= deadline and result["status"] != "failed":
            result["status"] = "unknown"
            result["reasons"].append("timeout: storage validation deadline exceeded")
    return result


def _validate_ultra_portfolio_load(
    root: Path, timeout_seconds: float, *, _storage: _UltraStorageContext | None = None,
) -> dict[str, object]:
    """Run the fixed ultra workload inside a native sandbox or trusted Windows fixture.

    Expected rows come only from acknowledged seed writes. Active clients count
    traversal loops (including their barrier), never claimed HTTP concurrency.
    """
    started = time.monotonic()
    measurements: dict[str, int | float] = dict.fromkeys((
        "target_projects", "foreign_projects", "module_records", "concurrent_clients",
        "peak_active_clients", "initial_traversals", "restart_traversals", "updated_traversals",
        "boundary_pages", "read_model_requests", "write_requests", "request_errors",
    ), 0)
    measurements["elapsed_seconds"] = 0.0
    reasons: list[str] = []
    result: dict[str, object] = {
        "schema_version": 1, "profile": "ultra-load-v1", "scale": "ultra",
        "status": "passed", "reasons": reasons, "cleanup_ok": True,
        "measurements": measurements,
    }
    process: subprocess.Popen[bytes] | None = None
    data: tempfile.TemporaryDirectory[str] | None = None
    runtime: tempfile.TemporaryDirectory[str] | None = None
    taskkill: str | None = None
    deadline = started
    phase = "environment"
    lock = threading.Lock()

    def increment(key: str) -> None:
        with lock:
            measurements[key] += 1

    def record_failure(exc: Exception, context: str) -> None:
        unknown = (
            not isinstance(exc, _ValidationFailure)
            or str(exc).startswith("timeout:")
            or "npm start exited" in str(exc)
        )
        # A response interrupted at the deadline may raise HTTPException/OSError.
        label = "timeout" if time.monotonic() >= deadline and unknown else context
        with lock:
            reasons.append(f"{label}: {exc}")
            if not unknown or result["status"] != "failed":
                result["status"] = "unknown" if unknown else "failed"

    def valid_id(value: object, context: str) -> str:
        _require(
            isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) != "",
            f"{context}: invalid id",
        )
        return str(value)

    def create(api: _PortfolioAPI, path: str, body: dict[str, object]) -> dict[str, object]:
        _remaining(deadline)
        increment("write_requests")
        try:
            item = api.create(path, body)
            valid_id(item.get("id"), f"POST {path}")
            _business_record_fields(item, body, f"POST {path}")
            return item
        except (_ValidationFailure, OSError, ValueError, http.client.HTTPException):
            increment("request_errors")
            raise

    expected_rows: list[dict[str, object]] = []
    program_id: object = None

    def page(api: _PortfolioAPI, offset: int, limit: int, *, defaults: bool = False) -> None:
        _remaining(deadline)
        path = f"/portfolio/read-model?program_id={quote(str(program_id), safe='')}"
        if not defaults or offset != 0:
            path += f"&offset={offset}"
        if not defaults or limit != 100:
            path += f"&limit={limit}"
        increment("read_model_requests")
        try:
            items = _business_items(api.get_object(path), "portfolio load page")
            expected = expected_rows[offset:offset + limit]
            _require(len(items) == len(expected), f"portfolio load page offset {offset}: row count")
            for actual, wanted in zip(items, expected, strict=True):
                _business_record_fields(actual, wanted, f"portfolio load page offset {offset}")
        except (_ValidationFailure, OSError, ValueError, http.client.HTTPException):
            increment("request_errors")
            raise

    def traverse(api: _PortfolioAPI, limit: int, counter: str, *, defaults: bool = False) -> None:
        # Include an empty page even when the preceding page is short.
        for offset in range(0, len(expected_rows) + limit, limit):
            page(api, offset, limit, defaults=defaults)
        increment(counter)

    errors = (_ValidationFailure, OSError, ValueError, http.client.HTTPException,
              subprocess.SubprocessError, threading.BrokenBarrierError)
    try:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        deadline = started + timeout_seconds if _storage is None else _storage.deadline
        if _PLATFORM not in ("posix", "nt"):
            raise OSError(f"unsupported platform for process-tree cleanup: {_PLATFORM}")
        npm = shutil.which("npm")
        if npm is None:
            raise OSError("npm executable unavailable; ultra load was not validated")
        taskkill = shutil.which("taskkill") if _PLATFORM == "nt" else None
        if _PLATFORM == "nt" and taskkill is None:
            raise OSError("taskkill unavailable for Windows process-tree cleanup")
        root = root.resolve(strict=True)
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        scripts = package.get("scripts") if isinstance(package, dict) else None
        _require(
            isinstance(scripts, dict) and isinstance(scripts.get("start"), str)
            and bool(scripts["start"].strip()),
            "package.json must provide an npm start script",
        )
        _remaining(deadline)
        if _storage is None:
            data = tempfile.TemporaryDirectory(prefix="ultra-load-data-")
            runtime = tempfile.TemporaryDirectory(prefix="ultra-load-runtime-")
            env = _environment(data.name, runtime.name)

        def start() -> _PortfolioAPI:
            nonlocal process
            if _storage is not None:
                process, api = _storage.start(_storage.code_a, _storage.data_a, "data_dir_isolation")
                return api
            _remaining(deadline)
            port = _free_port()
            env["PORT"] = str(port)
            process = subprocess.Popen(
                [npm, "start"], cwd=root, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=_PLATFORM == "posix",
            )
            api = _PortfolioAPI(port, deadline)
            _ready_portfolio(api, process)
            _remaining(deadline)
            return api

        phase = "startup"
        api = start()
        phase = "seed"
        programs = [create(api, "/programs", {"name": f"Load portfolio {uuid4().hex}"})
                    for _ in range(2)]
        if _storage is not None:
            _storage.programs = [{"id": p["id"], "name": p["name"]} for p in programs]
        program_id = programs[0]["id"]
        _require(str(program_id) != str(programs[1]["id"]), "programs: duplicate id")
        project_records: dict[str, dict[str, object]] = {}
        module_ids: dict[str, set[str]] = {name: set() for name in (
            "budgets", "staffing", "risks", "milestones",
        )}
        seeded: list[dict[str, object]] = []

        def seed_project(selected_program: object, counter: str) -> dict[str, object]:
            body = {
                "program_id": selected_program, "name": f'Load project, "{uuid4().hex}"',
                "owner": f"owner-{uuid4().hex[:12]}@example.test",
            }
            created = create(api, "/projects", body)
            key = valid_id(created["id"], "projects")
            _require(key not in project_records, "projects: duplicate id")
            project_records[key] = {"id": created["id"], **body}
            increment(counter)
            # Never take expected fields or aggregate values from a read response.
            return {
                "project_id": created["id"], "program_id": selected_program, "name": body["name"],
                "budget_total": 0, "staffing_allocation": 0, "risk_count": 0, "milestone_count": 0,
            }

        def seed_module(project: dict[str, object], module: str, body: dict[str, object]) -> None:
            path = f"/projects/{quote(str(project['project_id']), safe='')}/{module}"
            item = create(api, path, body)
            _business_record_fields(item, {"project_id": project["project_id"]}, module)
            key = valid_id(item["id"], module)
            _require(key not in module_ids[module], f"{module}: duplicate id")
            module_ids[module].add(key)

        for index in range(1000):
            project = seed_project(program_id, "target_projects")
            seeded.append(project)
            if index % 59 == 0 and measurements["foreign_projects"] < 17:
                foreign = seed_project(programs[1]["id"], "foreign_projects")
                if _storage is not None:
                    _storage.foreign_rows.append(foreign)
            if index % 97 == 0:
                amount = 1001 + index * 13
                allocation = (index % 3 + 1) / 4
                bodies: dict[str, dict[str, object]] = {
                    "budgets": {"category": f"budget-{index}", "amount": amount},
                    "staffing": {"person": f"person-{index}", "role": "lead",
                                 "allocation": allocation},
                    "risks": {"title": f"risk-{index}", "severity": "high" if index % 2 else "low"},
                    "milestones": {"name": f"milestone-{index}", "due_at": "2030-04-01"},
                }
                for module, body in bodies.items():
                    seed_module(project, module, body)
                    increment("module_records")
                project.update(budget_total=amount, staffing_allocation=allocation,
                               risk_count=1, milestone_count=1)
        # JavaScript String ordering compares UTF-16 code units, including non-ASCII IDs.
        expected_rows = sorted(seeded, key=lambda p: str(p["project_id"]).encode(
            "utf-16-be", errors="surrogatepass",
        ))
        if _storage is not None:
            _storage.target_rows = expected_rows
            _storage.project_records = project_records
        phase = "initial traversals"
        barrier = threading.Barrier(4)
        active = 0

        def client(limit: int, defaults: bool) -> None:
            nonlocal active
            with lock:
                active += 1
                measurements["concurrent_clients"] += 1
                measurements["peak_active_clients"] = max(measurements["peak_active_clients"], active)
            try:
                barrier.wait(timeout=_remaining(deadline))
                traverse(_PortfolioAPI(api.port, deadline), limit, "initial_traversals",
                         defaults=defaults)
            except errors as exc:
                barrier.abort()
                record_failure(exc, "initial traversals")
            finally:
                with lock:
                    active -= 1

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="ultra-load") as executor:
            futures = [executor.submit(client, limit, index == 0)
                       for index, limit in enumerate((100, 100, 37, 37))]
            for future in futures:
                future.result()
        if reasons:
            return result
        phase = "boundary pages"
        for offset in (0, 999, 1000):
            page(api, offset, 1)
            increment("boundary_pages")
        phase = "restart"
        _remaining(deadline)
        assert process is not None
        try:
            if _storage is None:
                _stop_tree(process, taskkill)
            else:
                _storage.stop(process)
        except errors as exc:
            result["cleanup_ok"] = False
            raise _ValidationFailure(f"process-tree cleanup failed: {exc}") from exc
        process = None
        if _storage is not None:
            _storage.check_b()
        api = start()
        if _storage is not None:
            _storage.check_a(api)
        traverse(api, 100, "restart_traversals")
        phase = "updated traversal"
        updated = seeded[0]
        seed_module(updated, "budgets", {"category": f"update-{uuid4().hex}", "amount": 7919})
        updated["budget_total"] = cast(int, updated["budget_total"]) + 7919
        traverse(api, 100, "updated_traversals")
        _remaining(deadline)
    except errors as exc:
        record_failure(exc, phase)
    finally:
        if process is not None:
            try:
                if _storage is None:
                    _stop_tree(process, taskkill)
                else:
                    _storage.stop(process)
            except errors as exc:
                result["cleanup_ok"] = False
                result["status"] = "failed"
                reasons.append(f"process-tree cleanup failed: {exc}")
        for label, directory in (("DATA_DIR", data), ("runtime", runtime)):
            if directory is not None:
                try:
                    directory.cleanup()
                except OSError as exc:
                    result["cleanup_ok"] = False
                    result["status"] = "failed"
                    reasons.append(f"temporary {label} cleanup failed: {exc}")
        if result["status"] == "passed" and time.monotonic() >= deadline:
            result["status"] = "unknown"
            reasons.append("timeout: ultra load validation deadline exceeded")
        measurements["elapsed_seconds"] = float(time.monotonic() - started)
    return result


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
