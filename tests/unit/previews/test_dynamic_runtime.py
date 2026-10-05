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

from tests.unit.previews.test_cleanup import broker_observation, identity


def test_v2_rejects_legacy_stopped_as_receipt() -> None:
    mod = runtime()
    client, server = socket.socketpair()

    def serve() -> None:
        with server, server.makefile("rwb", buffering=0) as stream:
            mod.read_frame(stream)
            mod.write_frame(stream, {"ok": True, "state": "stopped"})

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    app = mod.DynamicPreviewRuntime(client, client.makefile("rwb", buffering=0), identity("dynamic"),
                                    identity_binding="d" * 64)
    try:
        with pytest.raises(mod.DynamicPreviewCleanupError):
            app.close()
        assert not app._closed
    finally:
        client.close()
        worker.join(timeout=2)


@pytest.mark.parametrize("reply", [
    {"ok": True, "state": "probe"},
    {"ok": True, "state": "probe", "version": 2, "cleanup_schema_version": True},
    {"ok": True, "state": "probe", "version": 2.0, "cleanup_schema_version": 1},
    {"ok": 1, "state": "probe", "version": 2, "cleanup_schema_version": 1},
])
def test_probe_requires_explicit_v2_receipt_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: dict[str, object],
) -> None:
    mod = runtime()
    client, server = socket.socketpair()
    backend = mod.DynamicPreviewBackend(workspace_root=tmp_path, platform="linux")
    monkeypatch.setattr(backend, "_connect", lambda: (client, client.makefile("rwb", buffering=0)))

    def serve() -> None:
        with server, server.makefile("rwb", buffering=0) as stream:
            mod.read_frame(stream)
            mod.write_frame(stream, reply)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        with pytest.raises(mod.DynamicPreviewUnavailable):
            backend.probe()
    finally:
        client.close()
        worker.join(timeout=2)


@pytest.mark.parametrize("field,value", [
    ("preview_id", "90000000-0000-0000-0000-000000000001"),
    ("tenant_id", "90000000-0000-0000-0000-000000000001"),
    ("user_id", None), ("project_id", "other"), ("conversation_id", "other"),
    ("workspace_session_id", "other"), ("runtime_handle", "c" * 32),
    ("source_sha256", "c" * 64), ("display_root", "other"), ("display_entrypoint", "other.html"),
])
def test_cleanup_compares_every_frozen_identity_field(field: str, value: object) -> None:
    from dataclasses import replace
    mod = runtime()
    client, server = socket.socketpair()
    stream = client.makefile("rwb", buffering=0)
    app = mod.DynamicPreviewRuntime(client, stream, identity("dynamic"), identity_binding="d" * 64)
    changed = replace(identity("dynamic"), **{field: value})
    try:
        with pytest.raises(ValueError, match="identity"):
            app._cleanup_result({"ok": True, "state": "stopped", "observation": broker_observation(changed).to_wire()})
        assert not app._closed
    finally:
        stream.close()
        client.close()
        server.close()


def test_close_caches_exact_success_and_returns_validated_partial() -> None:
    from datetime import UTC, datetime

    from tests.unit.previews.test_cleanup import contract
    mod = runtime()
    client, server = socket.socketpair()
    partial = contract().BrokerCleanupObservation.create(identity("dynamic"), (), requested_at=datetime.now(UTC))
    final = broker_observation()
    seen: list[dict[str, object]] = []

    def serve() -> None:
        with server, server.makefile("rwb", buffering=0) as stream:
            seen.append(mod.read_frame(stream))
            mod.write_frame(stream, {"ok": False, "state": "cleanup_pending", "observation": partial.to_wire()})
            seen.append(mod.read_frame(stream))
            mod.write_frame(stream, {"ok": True, "state": "stopped", "observation": final.to_wire()})

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    app = mod.DynamicPreviewRuntime(client, client.makefile("rwb", buffering=0), identity("dynamic"), identity_binding="d" * 64)
    try:
        with pytest.raises(mod.DynamicPreviewCleanupError) as error:
            app.close()
        assert error.value.observation == partial
        assert app.close() == final
        assert app.close() == final
        assert len(seen) == 2
    finally:
        client.close()
        worker.join(timeout=2)


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
    ("install", "storage_conflict"), ("install", "runtime_incompatible"),
    ("install", "integrity_error"), ("install", "registry_denied"),
    ("install", "npm_exit_incomplete"), ("install", "npm_internal_error"),
    ("install", "signal_abort"), ("install", "signal_kill"),
    ("install", "signal_segv"), ("install", "signal_term"),
    ("install", "file_size_limit"), ("install", "signal_exit"),
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
        backend.start(tmp_path, "fixture", 30, scope=identity("dynamic").scope)


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
            mod.write_frame(stream, {"ok": True, "state": "stopped", "observation": broker_observation().to_wire()})

    worker = threading.Thread(target=serve)
    worker.start()
    app = mod.DynamicPreviewRuntime(client, client.makefile("rwb", buffering=0), identity("dynamic"), identity_binding="d" * 64)
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
            mod.write_frame(stream, {"ok": True, "state": "stopped", "observation": broker_observation().to_wire()})

    worker = threading.Thread(target=serve)
    worker.start()
    app = mod.DynamicPreviewRuntime(client, client.makefile("rwb", buffering=0), identity("dynamic"), identity_binding="d" * 64)
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
            mod.write_frame(stream, {"ok": True, "state": "stopped", "observation": broker_observation().to_wire()})

    worker = threading.Thread(target=serve)
    worker.start()
    try:
        app = mod.DynamicPreviewRuntime(
            client, client.makefile("rwb", buffering=0), identity("dynamic"), identity_binding="d" * 64,
            recovery_token="c" * 64, reconnect=lambda: (fresh, fresh.makefile("rwb", buffering=0)),
        )
        app.close()
        app.close()
    finally:
        client.close()
        fresh.close()
        server.close()
        worker.join(timeout=2)
    assert seen == [{"version": 2, "action": "recover_stop", "handle": "a" * 32,
                     "recovery_token": "c" * 64, "identity": identity("dynamic").to_wire(),
                     "identity_binding": "d" * 64}]


def test_failed_recovery_never_reports_closed() -> None:
    mod = runtime()
    client, server = socket.socketpair()
    server.close()

    def unavailable() -> object:
        raise mod.DynamicPreviewUnavailable("fixture unavailable")

    app = mod.DynamicPreviewRuntime(
        client, client.makefile("rwb", buffering=0), identity("dynamic"), identity_binding="d" * 64,
        recovery_token="c" * 64, reconnect=unavailable,
    )
    try:
        for _ in range(2):
            with pytest.raises(mod.DynamicPreviewCleanupError):
                app.close()
            assert not app._closed
    finally:
        client.close()
