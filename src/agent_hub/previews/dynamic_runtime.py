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

from agent_hub.previews.cleanup import (
    BrokerCleanupObservation,
    PreviewIdentityV1,
    PreviewOwnerScope,
)
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
from agent_hub.previews.provenance import PreviewProvenanceV1

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
    def __init__(self, message: str, *, observation: BrokerCleanupObservation | None = None) -> None:
        super().__init__(message)
        self.observation = (BrokerCleanupObservation.from_wire(observation.to_wire())
                            if observation is not None else None)


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
    def __init__(self, connection: socket.socket, stream: FrameStream, identity: PreviewIdentityV1,
                 *, identity_binding: str, recovery_token: str | None = None,
                 reconnect: Callable[[], tuple[socket.socket, FrameStream]] | None = None) -> None:
        self._connection = connection
        self._stream = stream
        self._identity = PreviewIdentityV1.from_wire(identity.to_wire())
        if self._identity.kind != "dynamic":
            raise ValueError("dynamic runtime identity required")
        self._handle = self._identity.runtime_handle
        self.source_sha256 = self._identity.source_sha256
        if re.fullmatch(r"[0-9a-f]{64}", identity_binding) is None:
            raise ValueError("invalid private identity binding")
        self._identity_binding = identity_binding
        self._observation: BrokerCleanupObservation | None = None
        self._lock = threading.Lock()
        self._revoked = False
        self._closed = False
        self._recovery_token = recovery_token
        self._reconnect = reconnect

    @property
    def identity(self) -> PreviewIdentityV1:
        return self._identity

    def source_provenance(self) -> PreviewProvenanceV1:
        with self._lock:
            if self._revoked:
                raise DynamicPreviewUnavailable("preview provenance unavailable after revocation")
            try:
                write_frame(self._stream, {"version": 2, "action": "source_provenance",
                                          "handle": self._handle})
                result = read_frame(self._stream)
                if set(result) != {"ok", "provenance"} or result["ok"] is not True:
                    raise ValueError("invalid provenance reply")
                provenance = PreviewProvenanceV1.from_wire(result["provenance"])
                if provenance.identity != self.identity:
                    raise ValueError("provenance identity mismatch")
                return provenance
            except (EOFError, OSError, ValueError) as error:
                # Revoke requests, retaining the original stop/recovery ownership.
                self._revoked = True
                raise DynamicPreviewUnavailable("preview source provenance unavailable") from error

    def request(self, method: str, target: str, headers: tuple[tuple[str, str], ...],
                body: bytes) -> DynamicPreviewResponse:
        request = validate_http_request(method, target, headers, body)
        with self._lock:
            if self._revoked:
                raise DynamicPreviewUnavailable("preview runtime revoked")
            try:
                write_frame(self._stream, {"version": 2, "action": "request",
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

    def _cleanup_result(self, result: dict[str, object]) -> BrokerCleanupObservation:
        if set(result) != {"ok", "state", "observation"} or type(result["ok"]) is not bool:
            raise ValueError("invalid cleanup reply")
        observation = BrokerCleanupObservation.from_wire(result["observation"])
        if observation.identity != self.identity:
            raise ValueError("cleanup identity mismatch")
        confirmed = observation.status == "confirmed"
        if result["ok"] != confirmed or result["state"] != ("stopped" if confirmed else "cleanup_pending"):
            raise ValueError("inconsistent cleanup reply")
        if not confirmed:
            raise DynamicPreviewCleanupError("preview cleanup pending", observation=observation)
        return observation

    def close(self) -> BrokerCleanupObservation:
        with self._lock:
            self._revoked = True
            if self._closed:
                assert self._observation is not None
                return self._observation
            try:
                self._connection.settimeout(70)
                write_frame(self._stream, {"version": 2, "action": "stop",
                                          "handle": self._handle})
                observation = self._cleanup_result(read_frame(self._stream))
            except (OSError, EOFError, ValueError) as error:
                if self._reconnect is None or self._recovery_token is None:
                    raise DynamicPreviewCleanupError("preview unit cleanup not confirmed") from error
                observation = self._recover_close()
            self._observation = observation
            self._closed = True
            try:
                self._stream.close()
            finally:
                self._connection.close()
            return observation

    def _recover_close(self) -> BrokerCleanupObservation:
        assert self._reconnect is not None and self._recovery_token is not None
        try:
            connection, stream = self._reconnect()
            try:
                connection.settimeout(70)
                write_frame(stream, {"version": 2, "action": "recover_stop", "handle": self._handle,
                                     "recovery_token": self._recovery_token,
                                     "identity": self.identity.to_wire(),
                                     "identity_binding": self._identity_binding})
                return self._cleanup_result(read_frame(stream))
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

    def _connect(self, *, timeout: float = 300) -> tuple[socket.socket, FrameStream]:
        if self._platform != "linux":
            raise DynamicPreviewUnavailable("dynamic preview requires configured Linux isolation")
        family = getattr(socket, "AF_UNIX", None)
        if not isinstance(family, int):
            raise DynamicPreviewUnavailable("Linux Unix socket support unavailable")
        connection = socket.socket(family, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        try:
            connection.connect(str(self._socket_path))
            return connection, connection.makefile("rwb", buffering=0)
        except OSError as error:
            connection.close()
            raise DynamicPreviewUnavailable("preview broker unavailable") from error

    def start(self, source_root: Path, preview_id: str,
              lifetime_seconds: int, *, scope: PreviewOwnerScope) -> DynamicPreviewRuntime:
        if self._platform != "linux":
            raise DynamicPreviewUnavailable("dynamic preview requires configured Linux isolation")
        staging = (self.workspace_root / ".preview-staging").resolve()
        if not source_root.is_absolute() or not source_root.resolve().is_relative_to(staging):
            raise ValueError("source_root must be a prepared preview staging snapshot")
        connection, stream = self._connect()
        try:
            write_frame(stream, {"version": 2, "action": "start", "source_root": str(source_root),
                                 "preview_id": preview_id, "lifetime_seconds": lifetime_seconds,
                                 "scope": scope.to_wire()})
            result = read_frame(stream)
            _check_result(result)
            if set(result) != {"ok", "state", "identity", "identity_binding", "recovery_token"} or result["state"] != "ready":
                raise ValueError("invalid preview start response")
            identity = PreviewIdentityV1.from_wire(result["identity"])
            if identity.preview_id != preview_id or identity.scope != scope or identity.kind != "dynamic":
                raise ValueError("invalid runtime owner identity")
            binding = result["identity_binding"]
            if not isinstance(binding, str) or re.fullmatch("[0-9a-f]{64}", binding) is None:
                raise ValueError("invalid runtime identity binding")
            recovery_token = result["recovery_token"]
            if not isinstance(recovery_token, str) or not re.fullmatch("[0-9a-f]{64}", recovery_token):
                raise ValueError("invalid stop-only recovery proof")
            connection.settimeout(15)
            return DynamicPreviewRuntime(connection, stream, identity, identity_binding=binding,
                                         recovery_token=recovery_token,
                                         reconnect=lambda: self._connect(timeout=70))
        except BaseException:
            stream.close()
            connection.close()
            raise

    def probe(self) -> None:
        connection, stream = self._connect()
        try:
            write_frame(stream, {"version": 2, "action": "probe"})
            result = read_frame(stream)
            if (result != {"ok": True, "state": "probe", "version": 2, "cleanup_schema_version": 1}
                    or type(result.get("ok")) is not bool
                    or type(result.get("version")) is not int
                    or type(result.get("cleanup_schema_version")) is not int):
                raise DynamicPreviewUnavailable("preview isolation probe failed")
        finally:
            stream.close()
            connection.close()
