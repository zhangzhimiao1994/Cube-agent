"""Owned synchronous Unix preview client. No generated host execution fallback."""

from __future__ import annotations

import re
import socket
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from agent_hub.previews.dynamic_runner import (
    MAX_REQUEST_BODY,
    MAX_RESPONSE_BODY,
    FrameStream,
    PreviewStartupFailure,
    bounded_body,
    filtered_headers,
    read_frame,
    validate_http_request,
    write_frame,
)

BROKER_SOCKET_PATH = Path("/run/agent-hub/preview-broker.sock")
__all__ = [
    "MAX_REQUEST_BODY", "MAX_RESPONSE_BODY", "DynamicPreviewBackend", "DynamicPreviewCleanupError",
    "DynamicPreviewResponse", "DynamicPreviewRuntime", "DynamicPreviewStartupFailed",
    "DynamicPreviewUnavailable",
]


class DynamicPreviewUnavailable(RuntimeError):
    pass


class DynamicPreviewStartupFailed(DynamicPreviewUnavailable):
    def __init__(self, phase: str, reason: str) -> None:
        diagnostic = PreviewStartupFailure(phase, reason)
        self.phase = diagnostic.phase
        self.reason = diagnostic.reason
        super().__init__(str(diagnostic))


class DynamicPreviewCleanupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DynamicPreviewResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


def decode_response(payload: dict[str, object]) -> DynamicPreviewResponse:
    if set(payload) != {"status_code", "headers", "body"}:
        raise ValueError("invalid response fields")
    status = payload["status_code"]
    if type(status) is not int or not 100 <= status <= 599:
        raise ValueError("invalid application status")
    return DynamicPreviewResponse(status, tuple(
        (name, value) for name, value in filtered_headers(payload["headers"], response=True)
    ), bounded_body(payload["body"]))


def _check_result(payload: dict[str, object]) -> None:
    if type(payload.get("ok")) is not bool or payload["ok"] is not True:
        if (
            set(payload) == {"ok", "error", "phase", "reason"}
            and payload["ok"] is False
            and payload["error"] == "preview startup failed"
            and isinstance(payload["phase"], str)
            and isinstance(payload["reason"], str)
        ):
            try:
                diagnostic = DynamicPreviewStartupFailed(payload["phase"], payload["reason"])
            except ValueError:
                pass
            else:
                raise diagnostic
        raise DynamicPreviewUnavailable("preview broker operation failed")


class DynamicPreviewRuntime:
    def __init__(self, connection: socket.socket, stream: FrameStream, handle: str,
                 source_sha256: str, *, recovery_token: str | None = None,
                 reconnect: Callable[[], tuple[socket.socket, FrameStream]] | None = None) -> None:
        self._connection = connection
        self._stream = stream
        self._handle = handle
        self.source_sha256 = source_sha256
        self._lock = threading.Lock()
        self._revoked = False
        self._closed = False
        self._recovery_token = recovery_token
        self._reconnect = reconnect

    def request(self, method: str, target: str, headers: tuple[tuple[str, str], ...],
                body: bytes) -> DynamicPreviewResponse:
        request = validate_http_request(method, target, headers, body)
        with self._lock:
            if self._revoked:
                raise DynamicPreviewUnavailable("preview runtime revoked")
            try:
                write_frame(self._stream, {"version": 1, "action": "request",
                                          "handle": self._handle, "request": request})
                result = read_frame(self._stream)
                _check_result(result)
                if set(result) != {"ok", "response"}:
                    raise ValueError("invalid application response fields")
                response = result.get("response")
                if not isinstance(response, dict):
                    raise ValueError("missing application response")  # noqa: TRY004
                return decode_response(cast(dict[str, object], response))
            except (EOFError, OSError, ValueError):
                self._revoked = True
                raise DynamicPreviewUnavailable("preview transport failed; request not replayed")

    def close(self) -> None:
        with self._lock:
            self._revoked = True
            if self._closed:
                return
            try:
                write_frame(self._stream, {"version": 1, "action": "stop",
                                          "handle": self._handle})
                result = read_frame(self._stream)
                if result != {"ok": True, "state": "stopped"}:
                    raise DynamicPreviewCleanupError("preview unit cleanup not confirmed")
            except (OSError, EOFError, ValueError) as error:
                if self._reconnect is None or self._recovery_token is None:
                    raise DynamicPreviewCleanupError("preview unit cleanup not confirmed") from error
                self._recover_close()
            except DynamicPreviewCleanupError:
                if self._reconnect is None or self._recovery_token is None:
                    raise
                self._recover_close()
            self._closed = True
            try:
                self._stream.close()
            finally:
                self._connection.close()

    def _recover_close(self) -> None:
        assert self._reconnect is not None and self._recovery_token is not None
        try:
            connection, stream = self._reconnect()
            try:
                connection.settimeout(60)
                write_frame(stream, {"version": 1, "action": "recover_stop", "handle": self._handle,
                                     "recovery_token": self._recovery_token})
                if read_frame(stream) != {"ok": True, "state": "stopped"}:
                    raise DynamicPreviewCleanupError("owned recovery cleanup not confirmed")
            finally:
                stream.close()
                connection.close()
        except (OSError, EOFError, ValueError, DynamicPreviewUnavailable) as error:
            raise DynamicPreviewCleanupError("owned recovery cleanup not confirmed") from error


class DynamicPreviewBackend:
    def __init__(self, *, workspace_root: Path, socket_path: Path = BROKER_SOCKET_PATH,
                 platform: str = sys.platform) -> None:
        self.workspace_root = workspace_root
        self._socket_path = socket_path
        self._platform = platform

    def _connect(self) -> tuple[socket.socket, FrameStream]:
        if self._platform != "linux":
            raise DynamicPreviewUnavailable("dynamic preview requires configured Linux isolation")
        family = getattr(socket, "AF_UNIX", None)
        if not isinstance(family, int):
            raise DynamicPreviewUnavailable("Linux Unix socket support unavailable")
        connection = socket.socket(family, socket.SOCK_STREAM)
        connection.settimeout(300)
        try:
            connection.connect(str(self._socket_path))
            return connection, connection.makefile("rwb", buffering=0)
        except OSError as error:
            connection.close()
            raise DynamicPreviewUnavailable("preview broker unavailable") from error

    def start(self, source_root: Path, preview_id: str,
              lifetime_seconds: int) -> DynamicPreviewRuntime:
        if self._platform != "linux":
            raise DynamicPreviewUnavailable("dynamic preview requires configured Linux isolation")
        staging = (self.workspace_root / ".preview-staging").resolve()
        if not source_root.is_absolute() or not source_root.resolve().is_relative_to(staging):
            raise ValueError("source_root must be a prepared preview staging snapshot")
        connection, stream = self._connect()
        try:
            write_frame(stream, {"version": 1, "action": "start", "source_root": str(source_root),
                                 "preview_id": preview_id, "lifetime_seconds": lifetime_seconds})
            result = read_frame(stream)
            _check_result(result)
            if set(result) != {"ok", "state", "handle", "source_sha256", "recovery_token"} or result["state"] != "ready":
                raise ValueError("invalid preview start response")
            handle, digest = result["handle"], result["source_sha256"]
            if not isinstance(handle, str) or not re.fullmatch("[0-9a-f]{32}", handle):
                raise ValueError("invalid runtime ownership handle")
            if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
                raise ValueError("invalid runtime source identity")
            recovery_token = result["recovery_token"]
            if not isinstance(recovery_token, str) or not re.fullmatch("[0-9a-f]{64}", recovery_token):
                raise ValueError("invalid stop-only recovery proof")
            connection.settimeout(15)
            return DynamicPreviewRuntime(connection, stream, handle, digest,
                                         recovery_token=recovery_token, reconnect=self._connect)
        except BaseException:
            stream.close()
            connection.close()
            raise

    def probe(self) -> None:
        connection, stream = self._connect()
        try:
            write_frame(stream, {"version": 1, "action": "probe"})
            result = read_frame(stream)
            if result != {"ok": True, "state": "probe"}:
                raise DynamicPreviewUnavailable("preview isolation probe failed")
        finally:
            stream.close()
            connection.close()
