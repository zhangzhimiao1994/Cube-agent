"""In-process, loopback-only static website previews."""

from __future__ import annotations

import hmac
import http.client
import logging
import mimetypes
import re
import secrets
import shutil
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from typing import Literal, Self
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID, uuid4

PreviewStatus = Literal["ready", "stopped", "expired"]
_SAFE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_LOOPBACK_HOST = "127.0.0.1"
_DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
_DEFAULT_MAX_SNAPSHOT_FILES = 10_000
_MAX_FINISHED_STATES = 256
_DEFAULT_LEASE = timedelta(minutes=30)
_DEFAULT_MAX_LIFETIME = timedelta(hours=2)
_PROXY_TIMEOUT_SECONDS = 5.0
_CONTENT_SECURITY_POLICY = (
    "sandbox allow-scripts allow-forms; default-src 'self' data: blob:; "
    "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'none'"
)
logger = logging.getLogger(__name__)


class PreviewError(RuntimeError):
    """Base error raised by the preview manager."""


class PreviewNotFound(PreviewError):
    """The requested preview or preview entrypoint does not exist."""


class InvalidPreviewPath(PreviewError):
    """A workspace or asset path did not stay inside its authorized root."""


class PreviewTokenRejected(PreviewError):
    """A preview capability token is invalid or has been revoked."""


class PreviewResponseTooLarge(PreviewError):
    """A preview response exceeds the configured response limit."""


class PreviewCapacityExceeded(PreviewError):
    """The active preview limit has been reached."""


@dataclass(frozen=True, slots=True)
class PreviewState:
    preview_id: str
    tenant_id: UUID
    conversation_id: str
    project_id: str
    session_id: str
    token_sha256: str
    status: PreviewStatus
    internal_host: str
    internal_port: int
    preview_root: Path
    lease_expires_at: datetime
    max_expires_at: datetime
    created_at: datetime
    stopped_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PreviewLaunch:
    state: PreviewState
    token: str


@dataclass(frozen=True, slots=True)
class PreviewResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        lowered = name.casefold()
        return next((value for key, value in self.headers if key.casefold() == lowered), None)


@dataclass(slots=True)
class _PreviewRuntime:
    state: PreviewState
    token_sha256: str
    entrypoint: str
    server: ThreadingHTTPServer
    thread: threading.Thread
    snapshot: TemporaryDirectory[str]


class _LoopbackPreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class PreviewManager:
    """Own loopback static servers and their short-lived capability tokens."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_snapshot_bytes: int = _DEFAULT_MAX_SNAPSHOT_BYTES,
        max_snapshot_files: int = _DEFAULT_MAX_SNAPSHOT_FILES,
        default_lease: timedelta = _DEFAULT_LEASE,
        max_lifetime: timedelta = _DEFAULT_MAX_LIFETIME,
        max_active_global: int = 32,
        max_active_per_tenant: int = 8,
        reaper_interval: timedelta = timedelta(seconds=5),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if max_snapshot_bytes <= 0 or max_snapshot_files <= 0:
            raise ValueError("preview snapshot limits must be positive")
        if default_lease <= timedelta(0):
            raise ValueError("default_lease must be positive")
        if max_lifetime <= timedelta(0):
            raise ValueError("max_lifetime must be positive")
        if max_active_global <= 0 or max_active_per_tenant <= 0:
            raise ValueError("preview capacity limits must be positive")
        if reaper_interval <= timedelta(0):
            raise ValueError("reaper_interval must be positive")
        self._workspace_root = workspace_root.resolve()
        self._max_response_bytes = max_response_bytes
        self._max_snapshot_bytes = max_snapshot_bytes
        self._max_snapshot_files = max_snapshot_files
        self._default_lease = default_lease
        self._max_lifetime = max_lifetime
        self._max_active_global = max_active_global
        self._max_active_per_tenant = max_active_per_tenant
        self._reaper_interval_seconds = reaper_interval.total_seconds()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._runtimes: dict[str, _PreviewRuntime] = {}
        self._finished: OrderedDict[str, PreviewState] = OrderedDict()
        self._active_by_conversation: dict[tuple[UUID, str], str] = {}
        self._start_reservations: set[tuple[UUID, str]] = set()
        self._closed = False
        self._reaper_stop = threading.Event()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop,
            name="preview-expiry-reaper",
            daemon=True,
        )
        self._reaper_thread.start()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def start(
        self,
        *,
        tenant_id: UUID,
        conversation_id: str,
        project_id: str,
        session_id: str,
        root: str | None = None,
        lease: timedelta | None = None,
    ) -> PreviewLaunch:
        conversation = _safe_segment(conversation_id)
        project = _safe_segment(project_id)
        session = _safe_segment(session_id)
        requested_lease = lease or self._default_lease
        if requested_lease <= timedelta(0):
            raise ValueError("lease must be positive")
        reservation_key = (tenant_id, conversation)
        with self._lock:
            self._reserve_start_locked(reservation_key)
        try:
            session_root = self._session_root(tenant_id, project, session)
            if root is None:
                preview_root, entrypoint = _find_preview_root(session_root)
            else:
                preview_root, entrypoint = _find_explicit_preview_root(session_root, root)
            snapshot, served_root = _snapshot_preview_root(
                preview_root,
                max_bytes=self._max_snapshot_bytes,
                max_files=self._max_snapshot_files,
            )
            now = _aware_utc(self._clock())
            max_expires_at = now + self._max_lifetime
            lease_expires_at = min(now + requested_lease, max_expires_at)
            token = secrets.token_urlsafe(32)
            token_sha256 = sha256(token.encode("utf-8")).hexdigest()
            preview_id = str(uuid4())
            try:
                handler = _handler_for(
                    served_root,
                    entrypoint,
                    max_response_bytes=self._max_response_bytes,
                )
                server = _LoopbackPreviewServer((_LOOPBACK_HOST, 0), handler)
            except Exception:
                snapshot.cleanup()
                raise
            port = int(server.server_address[1])
            state = PreviewState(
                preview_id=preview_id,
                tenant_id=tenant_id,
                conversation_id=conversation,
                project_id=project,
                session_id=session,
                token_sha256=token_sha256,
                status="ready",
                internal_host=_LOOPBACK_HOST,
                internal_port=port,
                preview_root=preview_root,
                lease_expires_at=lease_expires_at,
                max_expires_at=max_expires_at,
                created_at=now,
            )
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name=f"preview-server-{preview_id}",
                daemon=True,
            )
            runtime = _PreviewRuntime(
                state=state,
                token_sha256=token_sha256,
                entrypoint=entrypoint,
                server=server,
                thread=thread,
                snapshot=snapshot,
            )
            with self._lock:
                if self._closed:
                    server.server_close()
                    snapshot.cleanup()
                    raise RuntimeError("preview manager is closed")
                current_id = self._active_by_conversation.get(reservation_key)
                if current_id is not None:
                    self._stop_locked(current_id, status="stopped", now=now)
                self._runtimes[preview_id] = runtime
                self._active_by_conversation[reservation_key] = preview_id
                try:
                    thread.start()
                except BaseException:
                    self._active_by_conversation.pop(reservation_key, None)
                    self._runtimes.pop(preview_id, None)
                    server.server_close()
                    snapshot.cleanup()
                    raise
            return PreviewLaunch(state=state, token=token)
        finally:
            with self._lock:
                self._release_start_reservation_locked(reservation_key)

    def _reserve_start_locked(self, key: tuple[UUID, str]) -> None:
        if self._closed:
            raise RuntimeError("preview manager is closed")
        if key in self._start_reservations:
            raise PreviewCapacityExceeded(
                "preview startup is already in progress for conversation"
            )
        occupied = set(self._active_by_conversation)
        occupied.update(self._start_reservations)
        if key not in occupied:
            if len(occupied) >= self._max_active_global:
                raise PreviewCapacityExceeded("global preview capacity is exhausted")
            tenant_active = sum(tenant_id == key[0] for tenant_id, _ in occupied)
            if tenant_active >= self._max_active_per_tenant:
                raise PreviewCapacityExceeded("tenant preview capacity is exhausted")
        self._start_reservations.add(key)

    def _release_start_reservation_locked(self, key: tuple[UUID, str]) -> None:
        self._start_reservations.discard(key)

    def current(self, tenant_id: UUID, conversation_id: str) -> PreviewState | None:
        conversation = _safe_segment(conversation_id)
        with self._lock:
            preview_id = self._active_by_conversation.get((tenant_id, conversation))
            if preview_id is None:
                return None
            runtime = self._runtimes.get(preview_id)
            if runtime is None or runtime.state.status != "ready":
                return None
            now = _aware_utc(self._clock())
            if (
                runtime.state.lease_expires_at <= now
                or runtime.state.max_expires_at <= now
            ):
                self._stop_locked(preview_id, status="expired", now=now)
                return None
            return runtime.state

    def renew(
        self,
        preview_id: str,
        token: str,
        *,
        extension: timedelta | None = None,
        now: datetime | None = None,
    ) -> PreviewState:
        requested_extension = extension or self._default_lease
        if requested_extension <= timedelta(0):
            raise ValueError("extension must be positive")
        current_time = _aware_utc(now or self._clock())
        with self._lock:
            runtime = self._authorized_runtime(preview_id, token, now=current_time)
            next_expiry = max(
                runtime.state.lease_expires_at,
                min(current_time + requested_extension, runtime.state.max_expires_at),
            )
            runtime.state = replace(runtime.state, lease_expires_at=next_expiry)
            return runtime.state

    def stop(self, preview_id: str) -> PreviewState:
        with self._lock:
            if preview_id not in self._runtimes:
                finished = self._finished.get(preview_id)
                if finished is None:
                    raise PreviewNotFound("preview does not exist")
                return finished
            return self._stop_locked(preview_id, status="stopped", now=_aware_utc(self._clock()))

    def stop_conversation(
        self,
        tenant_id: UUID,
        conversation_id: str,
    ) -> PreviewState | None:
        conversation = _safe_segment(conversation_id)
        with self._lock:
            preview_id = self._active_by_conversation.get((tenant_id, conversation))
            if preview_id is None:
                return None
            return self._stop_locked(
                preview_id,
                status="stopped",
                now=_aware_utc(self._clock()),
            )

    def reap_expired(self, *, now: datetime | None = None) -> tuple[str, ...]:
        current_time = _aware_utc(now or self._clock())
        with self._lock:
            expired = tuple(
                preview_id
                for preview_id, runtime in self._runtimes.items()
                if runtime.state.status == "ready"
                and (
                    runtime.state.lease_expires_at <= current_time
                    or runtime.state.max_expires_at <= current_time
                )
            )
            for preview_id in expired:
                self._stop_locked(preview_id, status="expired", now=current_time)
            return expired

    def read(self, preview_id: str, token: str, path: str = "") -> PreviewResponse:
        with self._lock:
            runtime = self._authorized_runtime(
                preview_id,
                token,
                now=_aware_utc(self._clock()),
            )
            relative_path = _validated_request_path(
                runtime.state.preview_root,
                runtime.entrypoint,
                path,
            )
            state = runtime.state
        request_path = "/" if not relative_path else f"/{quote(relative_path, safe='/')}"
        connection = http.client.HTTPConnection(
            state.internal_host,
            state.internal_port,
            timeout=_PROXY_TIMEOUT_SECONDS,
        )
        try:
            connection.request("GET", request_path, headers={"Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
                response.read()
                raise PreviewResponseTooLarge("preview response is too large")
            body = response.read(self._max_response_bytes + 1)
            if len(body) > self._max_response_bytes:
                raise PreviewResponseTooLarge("preview response is too large")
            headers = tuple(
                (name, value)
                for name, value in response.getheaders()
                if name.casefold() not in {"connection", "keep-alive", "transfer-encoding"}
            )
            return PreviewResponse(status_code=response.status, headers=headers, body=body)
        except OSError as exc:
            raise PreviewNotFound("preview server is unavailable") from exc
        finally:
            connection.close()

    def close(self) -> None:
        self._reaper_stop.set()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            now = _aware_utc(self._clock())
            active_ids = tuple(
                preview_id
                for preview_id, runtime in self._runtimes.items()
                if runtime.state.status == "ready"
            )
            for preview_id in active_ids:
                self._stop_locked(preview_id, status="stopped", now=now)
            self._active_by_conversation.clear()
            self._runtimes.clear()
            self._finished.clear()
        if self._reaper_thread is not threading.current_thread():
            self._reaper_thread.join(timeout=2)

    def _reaper_loop(self) -> None:
        while not self._reaper_stop.wait(self._reaper_interval_seconds):
            try:
                self.reap_expired()
            except Exception:
                logger.exception("preview expiry reaper failed")

    def _authorized_runtime(
        self,
        preview_id: str,
        token: str,
        *,
        now: datetime,
    ) -> _PreviewRuntime:
        runtime = self._runtimes.get(preview_id)
        provided_hash = sha256(token.encode("utf-8")).hexdigest()
        if (
            runtime is None
            or runtime.state.status != "ready"
            or not hmac.compare_digest(runtime.token_sha256, provided_hash)
        ):
            raise PreviewTokenRejected("preview token is invalid or revoked")
        if runtime.state.lease_expires_at <= now or runtime.state.max_expires_at <= now:
            self._stop_locked(preview_id, status="expired", now=now)
            raise PreviewTokenRejected("preview token is invalid or revoked")
        return runtime

    def _stop_locked(
        self,
        preview_id: str,
        *,
        status: Literal["stopped", "expired"],
        now: datetime,
    ) -> PreviewState:
        runtime = self._runtimes[preview_id]
        runtime.state = replace(runtime.state, status=status, stopped_at=now)
        key = (runtime.state.tenant_id, runtime.state.conversation_id)
        if self._active_by_conversation.get(key) == preview_id:
            self._active_by_conversation.pop(key, None)
        runtime.server.shutdown()
        runtime.server.server_close()
        if runtime.thread is not threading.current_thread():
            runtime.thread.join(timeout=2)
        runtime.snapshot.cleanup()
        self._runtimes.pop(preview_id, None)
        self._finished[preview_id] = runtime.state
        self._finished.move_to_end(preview_id)
        while len(self._finished) > _MAX_FINISHED_STATES:
            self._finished.popitem(last=False)
        return runtime.state

    def _session_root(self, tenant_id: UUID, project_id: str, session_id: str) -> Path:
        candidate = (
            self._workspace_root
            / str(tenant_id)
            / "projects"
            / project_id
            / "sessions"
            / session_id
        )
        _reject_path_aliases(self._workspace_root, candidate)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise PreviewNotFound("workspace session does not exist") from exc
        if not resolved.is_relative_to(self._workspace_root) or not resolved.is_dir():
            raise InvalidPreviewPath("workspace session escapes the configured root")
        return resolved


def _handler_for(
    root: Path,
    entrypoint: str,
    *,
    max_response_bytes: int,
) -> type[BaseHTTPRequestHandler]:
    class PreviewRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._serve(include_body=True)

        def do_HEAD(self) -> None:
            self._serve(include_body=False)

        def _serve(self, *, include_body: bool) -> None:
            try:
                relative = _validated_request_path(root, entrypoint, self.path)
                try:
                    target = _resolved_asset(root, relative)
                except FileNotFoundError:
                    if PurePosixPath(relative).suffix:
                        raise
                    target = _resolved_asset(root, entrypoint)
            except (InvalidPreviewPath, FileNotFoundError):
                self._send_empty(HTTPStatus.NOT_FOUND)
                return
            body: bytes | None = None
            size = target.stat().st_size
            if include_body and size <= max_response_bytes:
                with target.open("rb") as stream:
                    body = stream.read(max_response_bytes + 1)
                size = len(body)
            if size > max_response_bytes:
                self._send_empty(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(size))
            self._send_security_headers()
            self.end_headers()
            if body is not None:
                self.wfile.write(body)

        def _send_empty(self, status: HTTPStatus) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self._send_security_headers()
            self.end_headers()

        def _send_security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
            self.send_header("Referrer-Policy", "no-referrer")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return PreviewRequestHandler


def _snapshot_preview_root(
    root: Path,
    *,
    max_bytes: int,
    max_files: int,
) -> tuple[TemporaryDirectory[str], Path]:
    snapshot = TemporaryDirectory(prefix="agent-hub-preview-")
    destination_root = Path(snapshot.name) / "site"
    destination_root.mkdir()
    total_bytes = 0
    file_count = 0
    try:
        pending = [(root, destination_root)]
        while pending:
            source_dir, destination_dir = pending.pop()
            for source in source_dir.iterdir():
                if source.is_symlink() or source.is_junction():
                    raise InvalidPreviewPath("preview snapshot contains an alias")
                destination = destination_dir / source.name
                if source.is_dir():
                    destination.mkdir()
                    pending.append((source, destination))
                    continue
                if not source.is_file():
                    raise InvalidPreviewPath("preview snapshot contains an unsupported entry")
                size = source.stat().st_size
                file_count += 1
                total_bytes += size
                if file_count > max_files or total_bytes > max_bytes:
                    raise PreviewResponseTooLarge("preview snapshot exceeds configured limits")
                shutil.copyfile(source, destination)
        return snapshot, destination_root
    except BaseException:
        snapshot.cleanup()
        raise


def _find_preview_root(session_root: Path) -> tuple[Path, str]:
    candidates = (
        (session_root / "dist", "index.html"),
        (session_root / "build", "index.html"),
        (session_root, "preview.html"),
        (session_root, "index.html"),
    )
    for root, entrypoint in candidates:
        try:
            target = _resolved_asset(root, entrypoint)
        except (InvalidPreviewPath, FileNotFoundError):
            continue
        if target.is_file():
            return root.resolve(strict=True), entrypoint
    raise PreviewNotFound("preview entrypoint was not found")


def _find_explicit_preview_root(session_root: Path, root: str) -> tuple[Path, str]:
    if (
        not root
        or root != root.strip()
        or "\\" in root
        or any(ord(character) < 32 or ord(character) == 127 for character in root)
    ):
        raise InvalidPreviewPath("preview root must be a safe relative directory")
    parsed = urlsplit(root)
    posix = PurePosixPath(root)
    windows = PureWindowsPath(root)
    if parsed.scheme or parsed.netloc or posix.is_absolute() or windows.is_absolute():
        raise InvalidPreviewPath("preview root must be a safe relative directory")
    if not posix.parts or any(
        part in {"", ".", ".."}
        or part.startswith(".")
        or _SAFE_SEGMENT.fullmatch(part) is None
        for part in posix.parts
    ):
        raise InvalidPreviewPath("preview root must be a safe relative directory")
    candidate = session_root.joinpath(*posix.parts)
    _reject_path_aliases(session_root, candidate)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PreviewNotFound("preview root does not exist") from exc
    if not resolved.is_relative_to(session_root) or not resolved.is_dir():
        raise InvalidPreviewPath("preview root escapes the workspace session")
    for entrypoint in ("index.html", "preview.html"):
        try:
            target = _resolved_asset(resolved, entrypoint)
        except (InvalidPreviewPath, FileNotFoundError):
            continue
        if target.is_file():
            return resolved, entrypoint
    raise PreviewNotFound("preview entrypoint was not found")


def _validated_request_path(root: Path, entrypoint: str, raw_path: str) -> str:
    parsed = urlsplit(raw_path)
    if parsed.scheme or parsed.netloc:
        raise InvalidPreviewPath("preview asset path must not contain an origin")
    path = parsed.path
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if not path or path == "/":
        relative = entrypoint
    else:
        path = path.removeprefix("/")
        if path.endswith("/"):
            raise InvalidPreviewPath("preview directories cannot be listed")
        relative = path
    if (
        not relative
        or relative != relative.strip()
        or "\\" in relative
        or any(ord(character) < 32 or ord(character) == 127 for character in relative)
    ):
        raise InvalidPreviewPath("preview asset path is invalid")
    posix = PurePosixPath(relative)
    windows = PureWindowsPath(relative)
    if posix.is_absolute() or windows.is_absolute():
        raise InvalidPreviewPath("preview asset path must be relative")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in posix.parts):
        raise InvalidPreviewPath("preview asset path escapes its root")
    normalized = posix.as_posix()
    try:
        target = _resolved_asset(root, normalized)
    except FileNotFoundError:
        return normalized
    if not target.is_file():
        raise InvalidPreviewPath("preview directories cannot be listed")
    return normalized


def _resolved_asset(root: Path, relative_path: str) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
        candidate = resolved_root / relative_path
        _reject_path_aliases(resolved_root, candidate)
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise InvalidPreviewPath("preview asset path is unavailable") from exc
    if not resolved.is_relative_to(resolved_root):
        raise InvalidPreviewPath("preview asset path escapes its root")
    return resolved


def _reject_path_aliases(root: Path, candidate: Path) -> None:
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise InvalidPreviewPath("preview path escapes its root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or (current.exists() and current.is_junction()):
            raise InvalidPreviewPath("preview path contains an alias")


def _safe_segment(value: str) -> str:
    raw = value.strip().casefold()
    if raw != value or _SAFE_SEGMENT.fullmatch(raw) is None:
        raise InvalidPreviewPath("workspace identifier must be a single safe path segment")
    return raw


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("preview timestamps must be timezone-aware")
    return value.astimezone(UTC)
