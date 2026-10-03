"""Offline protocol fixtures only; these are NOT the real 20-project acceptance."""

from __future__ import annotations

import importlib
import io
import json
import socket
import struct
import threading
from collections.abc import Buffer
from pathlib import Path
from typing import Any

import pytest


def runtime() -> Any:
    assert importlib.util.find_spec("agent_hub.previews.dynamic_runtime"), "Task1 runtime missing"
    return importlib.import_module("agent_hub.previews.dynamic_runtime")


@pytest.mark.parametrize("method,target", [
    ("CONNECT", "/"), ("TRACE", "/"), ("get", "/"), ("GET", "https://example.invalid/"),
    ("GET", "//localhost/"), ("GET", "/%2e%2e/secret"), ("GET", "/a%2fb"),
    ("GET", "/a\\b"), ("GET", "/a\r\nX: y"), ("GET", "/a#fragment"),
])
def test_rejects_unsafe_application_target(method: str, target: str) -> None:
    mod = runtime()
    with pytest.raises(ValueError):
        mod.validate_http_request(method, target, (), b"")


def test_business_request_bytes_and_query_survive_credentials_filter() -> None:
    mod = runtime()
    request = mod.validate_http_request("PATCH", "/tasks?q=a%20b", (
        ("Authorization", "sentinel"), ("Cookie", "sentinel"), ("Host", "evil"),
        ("Origin", "null"), ("Content-Type", "application/json"),
    ), b'{"status":"done"}')
    assert request["target"] == "/tasks?q=a%20b"
    assert request["headers"] == [["content-type", "application/json"]]
    assert request["body"] == "eyJzdGF0dXMiOiJkb25lIn0="


def test_shared_transport_limits_and_accept_language() -> None:
    mod = runtime()
    assert mod.MAX_REQUEST_BODY == 1024 * 1024
    assert mod.MAX_RESPONSE_BODY == 8 * 1024 * 1024
    request = mod.validate_http_request("POST", "/tasks", (("Accept-Language", "zh-CN"),), b"x")
    assert request["headers"] == [["accept-language", "zh-CN"]]
    with pytest.raises(ValueError):
        mod.validate_http_request("POST", "/tasks", (), b"x" * (1024 * 1024 + 1))
    import base64
    body = b"x" * (3 * 1024 * 1024)
    assert mod.decode_response({"status_code": 200, "headers": [],
                                "body": base64.b64encode(body).decode()}).body == body


def test_frames_are_length_bounded_and_strict() -> None:
    mod = runtime()
    stream = io.BytesIO()
    mod.write_frame(stream, {"action": "probe", "version": 1})
    stream.seek(0)
    assert mod.read_frame(stream) == {"action": "probe", "version": 1}
    for payload in [b"", b"[]", b'{"x":1,"x":2}', b'{"x":NaN}']:
        with pytest.raises((ValueError, EOFError)):
            mod.read_frame(io.BytesIO(struct.pack("!I", len(payload)) + payload))
    with pytest.raises(ValueError):
        mod.read_frame(io.BytesIO(struct.pack("!I", 0xFFFFFFFF)))


@pytest.mark.parametrize("phase,reason", [
    ("install", "nonzero_exit"), ("install_validate", "unsafe_tree"),
    ("install_handoff", "permission_denied"), ("build", "timeout"),
    ("start", "not_found"),
    ("install", "storage_full"), ("install", "resource_limit"),
    ("install", "registry_unavailable"), ("install", "dependency_unavailable"),
    ("install", "dependency_conflict"), ("install", "package_invalid"),
    ("install", "certificate_error"), ("install", "dependency_rejected"),
    ("install", "supervisor_exit"),
])
def test_startup_failure_preserves_only_validated_diagnostic(phase: str, reason: str) -> None:
    mod = runtime()
    with pytest.raises(mod.DynamicPreviewUnavailable) as caught:
        mod._check_result({"ok": False, "error": "preview startup failed",
                           "phase": phase, "reason": reason})
    assert getattr(caught.value, "phase", None) == phase
    assert getattr(caught.value, "reason", None) == reason
    assert str(caught.value) == f"preview startup failed: {phase}/{reason}"


@pytest.mark.parametrize("patch", [
    {"phase": "/private/source-token"}, {"reason": "Bearer private-token"},
    {"error": "private-token"}, {"extra": "private-token"},
    {"phase": ["install"]}, {"reason": None},
])
def test_invalid_startup_diagnostics_are_not_exposed(patch: dict[str, object]) -> None:
    mod = runtime()
    payload = {"ok": False, "error": "preview startup failed",
               "phase": "install", "reason": "nonzero_exit", **patch}
    with pytest.raises(mod.DynamicPreviewUnavailable) as caught:
        mod._check_result(payload)
    assert str(caught.value) == "preview broker operation failed"
    assert getattr(caught.value, "phase", None) is None


def test_frame_writes_handle_short_socket_sends() -> None:
    mod = runtime()

    class ShortWriter(io.BytesIO):
        def write(self, value: Buffer, /) -> int:
            return super().write(bytes(value)[:3])

    stream = ShortWriter()
    mod.write_frame(stream, {"fixture": "partial socket writes"})
    stream.seek(0)
    assert mod.read_frame(stream) == {"fixture": "partial socket writes"}


def test_windows_never_connects_or_executes_generated_code(tmp_path: Path) -> None:
    mod = runtime()
    backend = mod.DynamicPreviewBackend(workspace_root=tmp_path, platform="win32")
    with pytest.raises(mod.DynamicPreviewUnavailable, match="Linux"):
        backend.start(tmp_path, "fixture", 30)


def test_response_rejects_malformed_wire_types_and_bounds() -> None:
    mod = runtime()
    for value in [True, 99, 600, "200"]:
        with pytest.raises(ValueError):
            mod.decode_response({"status_code": value, "headers": [], "body": ""})
    response = mod.decode_response({"status_code": 204, "headers": [
        ["set-cookie", "secret"], ["content-type", "application/json"],
    ], "body": ""})
    assert response.status_code == 204
    assert response.headers == (("content-type", "application/json"),)
    assert response.body == b""
    with pytest.raises(ValueError):
        mod.decode_response(json.loads('{"status_code":200,"headers":[],"body":"!"}'))


def test_stop_failure_revokes_requests_and_retains_owned_retry() -> None:
    mod = runtime()
    client, server = socket.socketpair()
    seen: list[dict[str, object]] = []

    def serve() -> None:
        with server, server.makefile("rwb", buffering=0) as stream:
            seen.append(mod.read_frame(stream))
            mod.write_frame(stream, {"ok": False, "error": "cleanup fixture"})
            seen.append(mod.read_frame(stream))
            mod.write_frame(stream, {"ok": True, "state": "stopped"})

    worker = threading.Thread(target=serve)
    worker.start()
    app = mod.DynamicPreviewRuntime(client, client.makefile("rwb", buffering=0), "a" * 32, "b" * 64)
    try:
        with pytest.raises(mod.DynamicPreviewCleanupError):
            app.close()
        with pytest.raises(mod.DynamicPreviewUnavailable, match="revoked"):
            app.request("POST", "/tasks", (), b"fixture")
        app.close()
        app.close()
    finally:
        client.close()
        worker.join(timeout=2)
    assert [item["action"] for item in seen] == ["stop", "stop"]
    assert seen[0]["handle"] == seen[1]["handle"]


def test_runtime_rejects_extra_wire_fields_and_never_replays_write() -> None:
    mod = runtime()
    client, server = socket.socketpair()
    seen: list[dict[str, object]] = []

    def serve() -> None:
        with server, server.makefile("rwb", buffering=0) as stream:
            seen.append(mod.read_frame(stream))
            mod.write_frame(stream, {"ok": True, "extra": "invalid", "response": {
                "status_code": 200, "headers": [], "body": "",
            }})
            seen.append(mod.read_frame(stream))
            mod.write_frame(stream, {"ok": True, "state": "stopped"})

    worker = threading.Thread(target=serve)
    worker.start()
    app = mod.DynamicPreviewRuntime(client, client.makefile("rwb", buffering=0), "a" * 32, "b" * 64)
    try:
        with pytest.raises(mod.DynamicPreviewUnavailable):
            app.request("POST", "/tasks", (), b"fixture")
    finally:
        app.close()
        worker.join(timeout=2)
    assert [item["action"] for item in seen] == ["request", "stop"]


def test_close_recovers_confirmed_stop_after_transport_disconnect() -> None:
    mod = runtime()
    client, old_server = socket.socketpair()
    fresh, server = socket.socketpair()
    old_server.close()
    seen: list[dict[str, object]] = []

    def serve() -> None:
        with server, server.makefile("rwb", buffering=0) as stream:
            seen.append(mod.read_frame(stream))
            mod.write_frame(stream, {"ok": True, "state": "stopped"})

    worker = threading.Thread(target=serve)
    worker.start()
    try:
        app = mod.DynamicPreviewRuntime(
            client, client.makefile("rwb", buffering=0), "a" * 32, "b" * 64,
            recovery_token="c" * 64, reconnect=lambda: (fresh, fresh.makefile("rwb", buffering=0)),
        )
        app.close()
        app.close()
    finally:
        client.close()
        fresh.close()
        server.close()
        worker.join(timeout=2)
    assert seen == [{"version": 1, "action": "recover_stop", "handle": "a" * 32,
                     "recovery_token": "c" * 64}]


def test_failed_recovery_never_reports_closed() -> None:
    mod = runtime()
    client, server = socket.socketpair()
    server.close()

    def unavailable() -> object:
        raise mod.DynamicPreviewUnavailable("fixture unavailable")

    app = mod.DynamicPreviewRuntime(
        client, client.makefile("rwb", buffering=0), "a" * 32, "b" * 64,
        recovery_token="c" * 64, reconnect=unavailable,
    )
    try:
        for _ in range(2):
            with pytest.raises(mod.DynamicPreviewCleanupError):
                app.close()
            assert not app._closed
    finally:
        client.close()
