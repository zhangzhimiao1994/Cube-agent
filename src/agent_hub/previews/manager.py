"""Owned static previews and isolated application preview lifecycles."""

from __future__ import annotations

import hmac
import http.client
import logging
import mimetypes
import os
import re
import secrets
import socket
import stat
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from typing import Literal, Protocol, Self
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID, uuid4

from agent_hub.previews.cleanup import (
    BrokerCleanupObservation,
    CleanupReceiptV1,
    PreviewCleanupRecord,
    PreviewIdentityV1,
    PreviewOwnerScope,
    ResourceObservation,
    tree_entry,
    utc_now,
)
from agent_hub.previews.cleanup_store import TERMINAL_TTL, PreviewReceiptStore
from agent_hub.previews.dynamic_runner import json_object, validate_target
from agent_hub.previews.dynamic_runtime import (
    DynamicPreviewBackend,
    DynamicPreviewCleanupError,
    DynamicPreviewResponse,
    DynamicPreviewUnavailable,
)
from agent_hub.previews.provenance import (
    STATIC_SELECTION_POLICY,
    PreviewProvenanceUnavailable,
    PreviewProvenanceV1,
    SnapshotManifestV1,
)

PreviewStatus = Literal["ready", "stopped", "expired"]
_SAFE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_LOOPBACK_HOST = "127.0.0.1"
_DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_APPLICATION_REQUEST_BYTES = 1024 * 1024
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
    application_transport: bool = False
    identity: PreviewIdentityV1 | None = None


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
    server: _LoopbackPreviewServer | None
    thread: threading.Thread | None
    snapshot: TemporaryDirectory[str]
    application: ApplicationRuntime | None = None
    request_lock: threading.Lock = field(default_factory=threading.Lock)
    display_root: Path | None = None
    snapshot_identity: tuple[int, int] = field(init=False)
    snapshot_parent_identity: tuple[int, int] = field(init=False)
    proxies: set[http.client.HTTPConnection] = field(default_factory=set)
    proxy_condition: threading.Condition = field(default_factory=threading.Condition)
    stop_reason: str | None = None
    cleanup_identity: PreviewIdentityV1 | None = None
    provenance: PreviewProvenanceV1 | None = None

    def __post_init__(self) -> None:
        path = Path(self.snapshot.name)
        info, parent = path.lstat(), path.parent.lstat()
        self.snapshot_identity = (info.st_dev, info.st_ino)
        self.snapshot_parent_identity = (parent.st_dev, parent.st_ino)
        # Automatic TemporaryDirectory finalizers bypass the identity guard.
        # All runtime-owned removals must go through _remove_snapshot instead.
        self.snapshot._finalizer.detach()  # type: ignore[attr-defined]


class ApplicationRuntime(Protocol):
    @property
    def identity(self) -> PreviewIdentityV1: ...

    def request(
        self, method: str, target: str, headers: tuple[tuple[str, str], ...], body: bytes
    ) -> DynamicPreviewResponse: ...

    def close(self) -> BrokerCleanupObservation: ...


class ApplicationBackend(Protocol):
    def start(
        self, source_root: Path, preview_id: str, lifetime_seconds: int, *, scope: PreviewOwnerScope
    ) -> ApplicationRuntime: ...


class _LoopbackPreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        self.tracker = threading.Condition()
        self.accepted: set[socket.socket] = set()
        self.workers: set[threading.Thread] = set()
        self.closing = False
        self.shutdown_worker: threading.Thread | None = None
        super().__init__(address, handler)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        try:
            super().serve_forever(poll_interval)
        except OSError:
            if not self.closing:
                raise

    def process_request(self, request: socket.socket | tuple[bytes, socket.socket], client_address: tuple[str, int]) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError("HTTP preview requires a TCP socket")
        with self.tracker:
            if self.closing:
                self.shutdown_request(request)
                return
            # Bound keepalive reads even if cross-thread shutdown races recv.
            request.settimeout(1.0)
            self.accepted.add(request)
            self.workers = {worker for worker in self.workers if worker.is_alive()}
            worker = threading.Thread(target=self.process_request_thread,
                                      args=(request, client_address), daemon=True)
            self.workers.add(worker)
            try:
                worker.start()
            except BaseException:
                self.workers.discard(worker)
                self.accepted.discard(request)
                self.shutdown_request(request)
                raise

    def process_request_thread(self, request: socket.socket | tuple[bytes, socket.socket], client_address: tuple[str, int]) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError("HTTP preview requires a TCP socket")
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.tracker:
                self.accepted.discard(request)
                self.tracker.notify_all()

    def drain(self, serving: threading.Thread | None, deadline: float) -> tuple[ResourceObservation, ...]:
        with self.tracker:
            self.closing = True
            sockets = tuple(self.accepted)
            workers = tuple(self.workers)
        for connection in sockets:
            _close_socket(connection)
        if serving is not None and serving.is_alive():
            if self.shutdown_worker is None:
                self.shutdown_worker = threading.Thread(target=self.shutdown, daemon=True)
                self.shutdown_worker.start()
            serving.join(max(0, deadline - time.monotonic()))
        if self.shutdown_worker is not None:
            self.shutdown_worker.join(max(0, deadline - time.monotonic()))
        self.server_close()
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        with self.tracker:
            # Admission is closed before collecting workers; serving exit covers
            # the accept/process_request race, and finalizers never take manager's lock.
            return (
                _manager_fact("serving_thread", "exited" if serving is None or not serving.is_alive() else "present"),
                _manager_fact("listener", "closed" if self.socket.fileno() == -1 else "present"),
                _manager_fact("accepted_threads", "drained" if not any(w.is_alive() for w in self.workers) else "present"),
                _manager_fact("accepted_sockets", "closed" if not self.accepted and all(s.fileno() == -1 for s in sockets) else "present"),
            )


def _close_socket(connection: socket.socket) -> None:
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


def _manager_fact(resource: str, result: str, reason: str = "observed") -> ResourceObservation:
    return ResourceObservation(resource, "manager", utc_now(), result,
                               "resource_present" if result == "present" else reason)


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
        dynamic_backend: ApplicationBackend | None = None,
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
        self._dynamic_backend = dynamic_backend or DynamicPreviewBackend(
            workspace_root=self._workspace_root,
        )
        self._max_response_bytes = max_response_bytes
        self._max_snapshot_bytes = max_snapshot_bytes
        self._max_snapshot_files = max_snapshot_files
        self._default_lease = default_lease
        self._max_lifetime = max_lifetime
        self._max_active_global = max_active_global
        self._max_active_per_tenant = max_active_per_tenant
        self._reaper_interval_seconds = reaper_interval.total_seconds()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._receipt_store = PreviewReceiptStore(self._workspace_root, max_active=max_active_global,
                                                  clock=self._clock)
        self._unpersisted: dict[str, PreviewCleanupRecord] = {}
        self._lock = threading.RLock()
        self._runtimes: dict[str, _PreviewRuntime] = {}
        self._finished: OrderedDict[str, PreviewState] = OrderedDict()
        self._active_by_conversation: dict[tuple[UUID, str], str] = {}
        self._start_reservations: set[tuple[UUID, str]] = set()
        self._cancelled_starts: set[tuple[UUID, str]] = set()
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
        user_id: UUID | None = None,
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
            application = _has_application(session_root)
            if root is None:
                try:
                    preview_root, entrypoint = _find_preview_root(session_root)
                except PreviewNotFound:
                    if not application:
                        raise
                    preview_root, entrypoint = session_root, "index.html"
            else:
                preview_root, entrypoint = _find_explicit_preview_root(session_root, root)
            scope = PreviewOwnerScope(str(tenant_id), str(user_id) if user_id is not None else None,
                                      project, conversation, session,
                                      preview_root.relative_to(session_root).as_posix(), entrypoint)
            if application:
                return self._start_application(
                    tenant_id,
                    conversation,
                    project,
                    session,
                    session_root,
                    preview_root,
                    entrypoint,
                    requested_lease,
                    scope,
                )
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
                file_manifest: dict[str, tuple[int, str]] = {}
                identity = PreviewIdentityV1.create(preview_id, scope, kind="static",
                    digest=_static_digest(served_root, self._max_snapshot_bytes,
                                          self._max_snapshot_files, file_manifest=file_manifest))
                if entrypoint not in file_manifest:
                    raise PreviewProvenanceUnavailable("static snapshot entrypoint missing")
                provenance = PreviewProvenanceV1(1, identity,
                    SnapshotManifestV1.from_manifest(file_manifest,
                        selection_policy=STATIC_SELECTION_POLICY,
                        max_files=self._max_snapshot_files, max_bytes=self._max_snapshot_bytes),
                    _aware_utc(self._clock()))
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
                identity=identity,
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
                provenance=provenance,
            )
            with self._lock:
                if reservation_key in self._cancelled_starts:
                    server.server_close()
                    snapshot.cleanup()
                    raise PreviewTokenRejected("preview startup was revoked")
                if self._closed:
                    server.server_close()
                    snapshot.cleanup()
                    raise RuntimeError("preview manager is closed")
                try:
                    current_id = self._active_by_conversation.get(reservation_key)
                    if current_id is not None:
                        self._stop_locked(current_id, status="stopped", now=now, reason="replaced")
                except BaseException:
                    server.server_close()
                    snapshot.cleanup()
                    raise
                self._runtimes[preview_id] = runtime
                self._active_by_conversation[reservation_key] = preview_id
                try:
                    thread.start()
                    self._receipt_store.put(PreviewCleanupRecord(identity, None, None))
                except BaseException:
                    self._stop_locked(preview_id, status="stopped", now=now)
                    raise
            return PreviewLaunch(state=state, token=token)
        finally:
            with self._lock:
                self._release_start_reservation_locked(reservation_key)

    def _start_application(
        self,
        tenant_id: UUID,
        conversation: str,
        project: str,
        session: str,
        session_root: Path,
        preview_root: Path,
        entrypoint: str,
        lease: timedelta,
        scope: PreviewOwnerScope,
    ) -> PreviewLaunch:
        now = _aware_utc(self._clock())
        key = (tenant_id, conversation)
        # Retire the previous runtime before reserving a second isolated process.
        with self._lock:
            previous = self._active_by_conversation.get(key)
            if previous is not None:
                self._stop_locked(previous, status="stopped", now=now, reason="replaced")
        staging = self._workspace_root / ".preview-staging"
        _reject_path_aliases(self._workspace_root, staging)
        staging.mkdir(exist_ok=True)
        snapshot, source_root = _snapshot_preview_root(
            session_root,
            max_bytes=self._max_snapshot_bytes,
            max_files=self._max_snapshot_files,
            staging=staging,
        )
        token = secrets.token_urlsafe(32)
        state = PreviewState(
            preview_id=str(uuid4()),
            tenant_id=tenant_id,
            conversation_id=conversation,
            project_id=project,
            session_id=session,
            token_sha256=sha256(token.encode()).hexdigest(),
            status="ready",
            internal_host=_LOOPBACK_HOST,
            internal_port=0,
            preview_root=preview_root,
            lease_expires_at=min(now + lease, now + self._max_lifetime),
            max_expires_at=now + self._max_lifetime,
            created_at=now,
            application_transport=True,
        )
        runtime = _PreviewRuntime(state, state.token_sha256, entrypoint, None, None, snapshot)
        display_root = source_root / preview_root.relative_to(session_root)
        if (display_root / entrypoint).is_file():
            runtime.display_root = display_root
        try:
            runtime.application = self._dynamic_backend.start(
                source_root,
                state.preview_id,
                max(1, int(self._max_lifetime.total_seconds())),
                scope=scope,
            )
            identity = PreviewIdentityV1.from_wire(runtime.application.identity.to_wire())
            runtime.cleanup_identity = identity
            if identity.preview_id != state.preview_id or identity.scope != scope or identity.kind != "dynamic":
                raise DynamicPreviewUnavailable("application identity mismatch")
            runtime.state = state = replace(state, identity=identity)
        except BaseException:
            # Failed publication still owns cleanup and capacity, without an API identity.
            with self._lock:
                self._runtimes[state.preview_id] = runtime
                self._active_by_conversation[key] = state.preview_id
                self._stop_locked(state.preview_id, status="stopped", now=_aware_utc(self._clock()))
            raise
        with self._lock:
            self._runtimes[state.preview_id] = runtime
            self._active_by_conversation[key] = state.preview_id
            try:
                self._receipt_store.put(PreviewCleanupRecord(identity, None, None))
            except (OSError, ValueError):
                self._stop_locked(state.preview_id, status="stopped", now=now)
                raise DynamicPreviewCleanupError("preview identity persistence failed") from None
            if key in self._cancelled_starts:
                self._stop_locked(state.preview_id, status="stopped", now=now)
                raise PreviewTokenRejected("preview startup was revoked")
            if self._closed:
                self._stop_locked(state.preview_id, status="stopped", now=now)
                raise RuntimeError("preview manager is closed")
            ready_at = _aware_utc(self._clock())
            if state.lease_expires_at <= ready_at or state.max_expires_at <= ready_at:
                self._stop_locked(state.preview_id, status="expired", now=ready_at)
                raise PreviewTokenRejected("preview expired during startup")
        return PreviewLaunch(state, token)

    def _reserve_start_locked(self, key: tuple[UUID, str]) -> None:
        if self._closed:
            raise RuntimeError("preview manager is closed")
        if key in self._start_reservations:
            raise PreviewCapacityExceeded("preview startup is already in progress for conversation")
        occupied = {
            (runtime.state.tenant_id, runtime.state.conversation_id)
            for runtime in self._runtimes.values()
        }
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
        self._cancelled_starts.discard(key)

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
            if runtime.state.lease_expires_at <= now or runtime.state.max_expires_at <= now:
                self._stop_locked(preview_id, status="expired", now=now)
                return None
            return runtime.state

    def cleanup_record(self, preview_id: str) -> PreviewCleanupRecord | None:
        with self._lock:
            if preview_id in self._unpersisted:
                return self._unpersisted[preview_id]
            try:
                return self._receipt_store.get(preview_id)
            except (OSError, ValueError, TypeError):
                # A corrupt/missing file is missing evidence, never a cached success.
                return None

    def provenance(self, preview_id: str, tenant_id: UUID, user_id: UUID) -> PreviewProvenanceV1:
        with self._lock:
            runtime = self._provenance_runtime_locked(preview_id, tenant_id, user_id)
            identity = runtime.state.identity
            captured = runtime.provenance
        if captured is None:
            try:
                provider = getattr(runtime.application, "source_provenance", None)
                if not callable(provider):
                    raise PreviewProvenanceUnavailable("backend has no source provenance")
                value: object = provider()
                if not isinstance(value, PreviewProvenanceV1):
                    raise ValueError("invalid backend provenance metadata")  # noqa: TRY004
                captured = PreviewProvenanceV1.from_wire(value.to_wire())
            except Exception as error:
                raise PreviewProvenanceUnavailable("preview source provenance unavailable") from error
        if captured.identity != identity:
            raise PreviewProvenanceUnavailable("preview source provenance identity mismatch")
        with self._lock:
            if self._provenance_runtime_locked(preview_id, tenant_id, user_id) is not runtime:
                raise PreviewNotFound("preview does not exist")
        return captured

    def _provenance_runtime_locked(self, preview_id: str, tenant_id: UUID,
                                   user_id: UUID) -> _PreviewRuntime:
        runtime = self._runtimes.get(preview_id)
        if (runtime is None or runtime.state.status != "ready" or user_id is None
                or runtime.state.tenant_id != tenant_id or runtime.state.identity is None
                or runtime.state.identity.user_id != str(user_id)
                or runtime.state.identity.tenant_id != str(tenant_id)
                or self._active_by_conversation.get((tenant_id, runtime.state.conversation_id)) != preview_id):
            raise PreviewNotFound("preview does not exist")
        now = _aware_utc(self._clock())
        if runtime.state.lease_expires_at <= now or runtime.state.max_expires_at <= now:
            self._stop_locked(preview_id, status="expired", now=now)
            raise PreviewNotFound("preview does not exist")
        return runtime

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
            key = (tenant_id, conversation)
            if key in self._start_reservations:
                self._cancelled_starts.add(key)
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
                if runtime.state.status != "ready"
                or (
                    runtime.state.lease_expires_at <= current_time
                    or runtime.state.max_expires_at <= current_time
                )
            )
            errors: list[Exception] = []
            for preview_id in expired:
                try:
                    pending_status = self._runtimes[preview_id].state.status
                    self._stop_locked(
                        preview_id,
                        status="stopped" if pending_status == "stopped" else "expired",
                        now=current_time,
                    )
                except Exception as error:
                    logger.exception("preview cleanup failed for %s", preview_id)
                    errors.append(error)
            if errors:
                raise DynamicPreviewCleanupError(
                    f"{len(errors)} preview cleanup attempts failed"
                ) from ExceptionGroup("preview cleanup failures", errors)
            return expired

    def read(self, preview_id: str, token: str, path: str = "") -> PreviewResponse:
        with self._lock:
            runtime = self._authorized_runtime(
                preview_id,
                token,
                now=_aware_utc(self._clock()),
            )
            if runtime.display_root is not None:
                relative = _validated_request_path(runtime.display_root, runtime.entrypoint, path)
                try:
                    target = _resolved_asset(runtime.display_root, relative)
                except FileNotFoundError:
                    return PreviewResponse(404, (), b"")
                if target.stat().st_size > self._max_response_bytes:
                    raise PreviewResponseTooLarge("preview display response is too large")
                with target.open("rb") as stream:
                    body = stream.read(self._max_response_bytes + 1)
                if len(body) > self._max_response_bytes:
                    raise PreviewResponseTooLarge("preview display response is too large")
                mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                return PreviewResponse(200, (("Content-Type", mime),), body)
            if runtime.application is not None:
                raise PreviewNotFound("dynamic preview has no immutable HTML entrypoint")
            relative_path = _validated_request_path(
                runtime.state.preview_root,
                runtime.entrypoint,
                path,
            )
            state = runtime.state
            connection = http.client.HTTPConnection(
                state.internal_host, state.internal_port, timeout=_PROXY_TIMEOUT_SECONDS,
            )
            with runtime.proxy_condition:
                runtime.proxies.add(connection)
        request_path = "/" if not relative_path else f"/{quote(relative_path, safe='/')}"
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
            result = PreviewResponse(status_code=response.status, headers=headers, body=body)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise PreviewNotFound("preview server is unavailable") from exc
        finally:
            connection.close()
            with runtime.proxy_condition:
                runtime.proxies.discard(connection)
                runtime.proxy_condition.notify_all()
        with self._lock:
            self._authorized_runtime(preview_id, token, now=_aware_utc(self._clock()))
        return result

    def app_request(
        self,
        preview_id: str,
        token: str,
        method: str,
        target: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
    ) -> PreviewResponse:
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            raise ValueError("invalid application method")
        validate_target(target)
        request_headers = _application_headers(headers)
        if len(body) > MAX_APPLICATION_REQUEST_BYTES:
            raise PreviewResponseTooLarge("application request is too large")
        with self._lock:
            runtime = self._authorized_runtime(preview_id, token, now=_aware_utc(self._clock()))
            if runtime.application is None:
                raise PreviewNotFound("preview has no application transport")
        # A queued write rechecks revocation before reaching the owned transport.
        with runtime.request_lock:
            with self._lock:
                self._authorized_runtime(preview_id, token, now=_aware_utc(self._clock()))
            result = runtime.application.request(method, target, request_headers, body)
            if len(result.body) > min(self._max_response_bytes, _DEFAULT_MAX_RESPONSE_BYTES):
                raise PreviewResponseTooLarge("application response is too large")
            if not 200 <= result.status_code <= 599:
                raise DynamicPreviewUnavailable("invalid application response status")
            response_headers = _application_headers(result.headers, response=True)
            with self._lock:
                self._authorized_runtime(preview_id, token, now=_aware_utc(self._clock()))
            return PreviewResponse(result.status_code, response_headers, result.body)

    def is_application(self, preview_id: str, token: str) -> bool:
        with self._lock:
            runtime = self._authorized_runtime(preview_id, token, now=_aware_utc(self._clock()))
            return runtime.state.application_transport

    def close(self) -> None:
        error: Exception | None = None
        with self._lock:
            self._closed = True
            now = _aware_utc(self._clock())
            active_ids = tuple(preview_id for preview_id, runtime in self._runtimes.items())
            for preview_id in active_ids:
                try:
                    self._stop_locked(preview_id, status="stopped", now=now, reason="shutdown")
                except (PreviewError, DynamicPreviewCleanupError, OSError, RuntimeError) as exc:
                    if error is None:
                        error = exc
            self._finished.clear()
            if not self._runtimes and not self._start_reservations:
                self._reaper_stop.set()
        if error is not None:
            raise error
        if self._reaper_thread is not threading.current_thread():
            self._reaper_thread.join(timeout=2)

    def _reaper_loop(self) -> None:
        while not self._reaper_stop.wait(self._reaper_interval_seconds):
            try:
                self.reap_expired()
                with self._lock:
                    if self._closed and not self._runtimes and not self._start_reservations:
                        self._reaper_stop.set()
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
        reason: str | None = None,
    ) -> PreviewState:
        runtime = self._runtimes[preview_id]
        if runtime.state.stopped_at is None:
            runtime.state = replace(runtime.state, status=status, stopped_at=now)
            runtime.stop_reason = reason or ("expired" if status == "expired" else "explicit")
        key = (runtime.state.tenant_id, runtime.state.conversation_id)
        identity = runtime.state.identity
        if identity is None:
            self._stop_unpublished_locked(runtime)
            if self._active_by_conversation.get(key) == preview_id:
                self._active_by_conversation.pop(key, None)
            self._runtimes.pop(preview_id, None)
            return runtime.state
        assert runtime.state.stopped_at is not None
        facts = [_manager_fact("capability", "revoked")]
        cleanup_error: Exception | None = None
        try:
            if runtime.application is not None:
                observation = BrokerCleanupObservation.from_wire(runtime.application.close().to_wire())
                if observation.identity != identity:
                    raise ValueError("broker cleanup identity mismatch")
                facts.extend(observation.observations)
            if runtime.server is not None:
                deadline = time.monotonic() + 5
                facts.extend(runtime.server.drain(runtime.thread, deadline))
                with runtime.proxy_condition:
                    while runtime.proxies:
                        for connection in tuple(runtime.proxies):
                            if connection.sock is not None:
                                _close_socket(connection.sock)
                            connection.close()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        runtime.proxy_condition.wait(min(remaining, 0.02))
                    drained = not runtime.proxies
                facts.extend((_manager_fact("proxy_operations", "drained" if drained else "present"),
                              _manager_fact("proxy_sockets", "closed" if drained else "present")))
            if any(f.result in {"unknown", "present"} for f in facts):
                raise DynamicPreviewCleanupError("preview drain remains pending")
            facts.append(_manager_fact("snapshot", _remove_snapshot(runtime)))
        except (OSError, RuntimeError, ValueError) as error:
            cleanup_error = error
            if (isinstance(error, DynamicPreviewCleanupError) and error.observation is not None
                    and error.observation.identity == identity):
                facts.extend(error.observation.observations)
        receipt = CleanupReceiptV1.create(identity, tuple(facts), requested_at=now,
                                           reason=runtime.stop_reason or "explicit")
        record = PreviewCleanupRecord(identity, receipt,
                                      now + TERMINAL_TTL if receipt.status == "confirmed" else None)
        try:
            self._receipt_store.put(record)
            self._unpersisted.pop(preview_id, None)
        except (OSError, ValueError) as error:
            self._unpersisted[preview_id] = PreviewCleanupRecord(identity, CleanupReceiptV1.create(
                identity, (), requested_at=now, reason=runtime.stop_reason or "explicit",
                reason_code="persistence_failed"), None)
            raise DynamicPreviewCleanupError("preview receipt persistence failed") from error
        if receipt.status != "confirmed":
            raise DynamicPreviewCleanupError("preview cleanup pending") from cleanup_error
        if self._active_by_conversation.get(key) == preview_id:
            self._active_by_conversation.pop(key, None)
        self._runtimes.pop(preview_id, None)
        self._finished[preview_id] = runtime.state
        self._finished.move_to_end(preview_id)
        while len(self._finished) > _MAX_FINISHED_STATES:
            self._finished.popitem(last=False)
        return runtime.state

    @staticmethod
    def _stop_unpublished_locked(runtime: _PreviewRuntime) -> None:
        try:
            if runtime.application is not None:
                observation = BrokerCleanupObservation.from_wire(runtime.application.close().to_wire())
                if (runtime.cleanup_identity is None or observation.identity != runtime.cleanup_identity
                        or observation.status != "confirmed"):
                    raise DynamicPreviewCleanupError("unpublished application cleanup unconfirmed")
            if _remove_snapshot(runtime) != "absent":
                raise DynamicPreviewCleanupError("unpublished snapshot remains present")
        except (OSError, RuntimeError, ValueError) as error:
            raise DynamicPreviewCleanupError("unpublished preview cleanup pending") from error

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


def _remove_snapshot(runtime: _PreviewRuntime) -> Literal["absent", "present"]:
    path = Path(runtime.snapshot.name)
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise InvalidPreviewPath("snapshot parent contains an alias")
    parent_info = path.parent.lstat()
    if (parent_info.st_dev, parent_info.st_ino) != runtime.snapshot_parent_identity:
        raise InvalidPreviewPath("snapshot parent identity changed")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "absent"
    if (not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or (info.st_dev, info.st_ino) != runtime.snapshot_identity
            or (sys.platform == "linux" and info.st_uid != os.getuid())):
        raise InvalidPreviewPath("snapshot ownership identity changed")
    runtime.snapshot.cleanup()
    try:
        path.lstat()
    except FileNotFoundError:
        return "absent"
    return "present"


def _static_digest(root: Path, max_bytes: int, max_files: int, *,
                   file_manifest: dict[str, tuple[int, str]] | None = None) -> str:
    digest = sha256()
    total = count = 0
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        if (getattr(info, "st_file_attributes", 0) & 0x400
                or not (directory or stat.S_ISREG(info.st_mode))):
            raise InvalidPreviewPath("unsafe snapshot identity")
        data = b""
        if not directory:
            flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
            with os.fdopen(os.open(path, flags), "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened.st_mode)
                        or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)):
                    raise InvalidPreviewPath("snapshot identity changed during capture")
                data = stream.read(max_bytes - total + 1)
                after = os.fstat(stream.fileno())
            current = path.lstat()
            if (_snapshot_file_signature(info) != _snapshot_file_signature(current)
                    or _snapshot_file_signature(opened) != _snapshot_file_signature(after)):
                raise InvalidPreviewPath("snapshot changed during capture")
            total += len(data)
            count += 1
            if total > max_bytes or count > max_files:
                raise PreviewResponseTooLarge("snapshot identity exceeds limits")
            if file_manifest is not None:
                file_manifest[path.relative_to(root).as_posix()] = (len(data), sha256(data).hexdigest())
        digest.update(tree_entry(path.relative_to(root).as_posix(), directory=directory,
                                 executable=bool(info.st_mode & 0o111), data=data))
    return digest.hexdigest()


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


def _snapshot_file_signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _snapshot_preview_root(
    root: Path,
    *,
    max_bytes: int,
    max_files: int,
    staging: Path | None = None,
) -> tuple[TemporaryDirectory[str], Path]:
    snapshot = TemporaryDirectory(prefix="agent-hub-preview-", dir=staging)
    destination_root = Path(snapshot.name) / "site"
    destination_root.mkdir()
    total_bytes = 0
    file_count = 0
    try:
        if staging is not None and sys.platform == "linux":
            _copy_application_snapshot(root, destination_root, max_bytes, max_files)
            return snapshot, destination_root
        pending = [(root, destination_root)]
        while pending:
            source_dir, destination_dir = pending.pop()
            for source in source_dir.iterdir():
                if source.is_symlink() or source.is_junction():
                    raise InvalidPreviewPath("preview snapshot contains an alias")
                if staging is not None and (
                    source.name
                    in {
                        "node_modules",
                        ".git",
                        ".preview-staging",
                        ".venv",
                        ".npmrc",
                        ".ssh",
                        ".aws",
                        ".codex",
                    }
                    or source.name == ".env"
                    or source.name.startswith(".env.")
                ):
                    continue
                destination = destination_dir / source.name
                if source.is_dir():
                    if staging is not None:
                        file_count += 1
                        if file_count > max_files:
                            raise PreviewResponseTooLarge(
                                "preview snapshot exceeds configured limits"
                            )
                    destination.mkdir()
                    pending.append((source, destination))
                    continue
                if not source.is_file():
                    raise InvalidPreviewPath("preview snapshot contains an unsupported entry")
                size = source.stat().st_size
                if staging is not None and source.stat().st_nlink != 1:
                    raise InvalidPreviewPath("preview snapshot contains a hardlink")
                file_count += 1
                total_bytes += size
                if file_count > max_files or total_bytes > max_bytes:
                    raise PreviewResponseTooLarge("preview snapshot exceeds configured limits")
                before = source.lstat()
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                with os.fdopen(os.open(source, flags), "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(opened.st_mode)
                            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
                        raise InvalidPreviewPath("preview source identity changed during preparation")
                    data = stream.read(max_bytes - (total_bytes - size) + 1)
                    after = os.fstat(stream.fileno())
                current = source.lstat()
                # Windows path and descriptor ctime semantics can differ; check
                # stability within each API and inode identity across both.
                if (_snapshot_file_signature(before) != _snapshot_file_signature(current)
                        or _snapshot_file_signature(opened) != _snapshot_file_signature(after)):
                    raise InvalidPreviewPath("preview source changed during preparation")
                total_bytes += len(data) - size
                if total_bytes > max_bytes:
                    raise PreviewResponseTooLarge("preview snapshot exceeds configured limits")
                destination.write_bytes(data)
                if staging is not None:
                    destination.chmod(0o555 if before.st_mode & 0o111 else 0o444)
        return snapshot, destination_root
    except BaseException:
        snapshot.cleanup()
        raise


def _copy_application_snapshot(
    root: Path, destination: Path, max_bytes: int, max_files: int
) -> None:
    """Anchor Linux source reads against concurrent path replacement."""
    if sys.platform != "linux":
        raise RuntimeError("descriptor-based snapshots require Linux")
    nofollow = os.O_NOFOLLOW
    directory = os.O_DIRECTORY
    fd = os.open(root.anchor, os.O_RDONLY | directory | nofollow)
    try:
        for part in root.parts[1:]:
            child = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=fd)
            os.close(fd)
            fd = child
        _copy_application_files(fd, destination, max_bytes, max_files, nofollow)
    except OSError as error:
        raise InvalidPreviewPath("preview source could not be safely snapshotted") from error
    finally:
        os.close(fd)


def _copy_application_files(
    root_fd: int,
    destination: Path,
    max_bytes: int,
    max_files: int,
    nofollow: int,
) -> None:
    if sys.platform != "linux":
        raise RuntimeError("descriptor-based snapshots require Linux")
    total = 0
    entries = 0
    excluded = {
        "node_modules",
        ".git",
        ".preview-staging",
        ".venv",
        ".npmrc",
        ".ssh",
        ".aws",
        ".codex",
    }
    for relative, directories, files, fd in os.fwalk(".", dir_fd=root_fd, follow_symlinks=False):
        target = destination / relative
        for name in tuple(directories) + tuple(files):
            if name in excluded or name == ".env" or name.startswith(".env."):
                if name in directories:
                    directories.remove(name)
                continue
            entries += 1
            if entries > max_files:
                raise PreviewResponseTooLarge("preview snapshot exceeds configured limits")
            child = os.open(name, os.O_RDONLY | nofollow | os.O_NONBLOCK, dir_fd=fd)
            try:
                info = os.fstat(child)
                path = target / name
                if stat.S_ISDIR(info.st_mode):
                    path.mkdir()
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    if info.st_size > max_bytes - total:
                        raise PreviewResponseTooLarge("preview snapshot exceeds configured limits")
                    with os.fdopen(os.dup(child), "rb") as stream:
                        data = stream.read(max_bytes - total + 1)
                    after = os.fstat(child)
                    if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        raise InvalidPreviewPath("preview source changed during preparation")
                    total += len(data)
                    if total > max_bytes:
                        raise PreviewResponseTooLarge("preview snapshot exceeds configured limits")
                    path.write_bytes(data)
                    path.chmod(0o555 if info.st_mode & 0o111 else 0o444)
                else:
                    raise InvalidPreviewPath("preview source contains a link or special file")
            finally:
                os.close(child)
        target.chmod(0o555)


def _has_application(root: Path) -> bool:
    manifest = root / "package.json"
    _reject_path_aliases(root, manifest)
    if not manifest.exists():
        return False
    if not manifest.is_file() or manifest.stat().st_size > MAX_APPLICATION_REQUEST_BYTES:
        raise InvalidPreviewPath("invalid application manifest")
    try:
        payload = json_object(manifest.read_bytes())
    except (ValueError, UnicodeError) as error:
        raise InvalidPreviewPath("invalid application manifest") from error
    if not isinstance(payload, dict):
        raise InvalidPreviewPath("invalid application manifest")
    scripts = payload.get("scripts", {})
    if not isinstance(scripts, dict):
        raise InvalidPreviewPath("invalid application scripts")
    if "start" not in scripts:
        return False
    if not isinstance(scripts["start"], str) or not scripts["start"].strip():
        raise InvalidPreviewPath("invalid application start script")
    return True


def _application_headers(
    headers: tuple[tuple[str, str], ...],
    *,
    response: bool = False,
) -> tuple[tuple[str, str], ...]:
    if len(headers) > 64:
        raise ValueError("too many application headers")
    size = 0
    for name, value in headers:
        if re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) is None:
            raise ValueError("invalid application header name")
        if any(ord(char) < 32 or ord(char) > 126 for char in value):
            raise ValueError("invalid application header value")
        size += len(name) + len(value)
        if size > 16384:
            raise ValueError("application headers too large")
    nominated = {
        value.strip().casefold()
        for name, content in headers
        if name.casefold() == "connection"
        for value in content.split(",")
    }
    allowed = (
        {
            "content-type",
            "content-language",
            "etag",
            "last-modified",
            "cache-control",
            "content-range",
            "accept-ranges",
            "location",
        }
        if response
        else {"accept", "accept-language", "content-type", "if-match", "if-none-match", "range"}
    )
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in headers:
        name = name.casefold()
        if name not in allowed or name in nominated:
            continue
        if name in seen:
            raise ValueError("duplicate application header")
        seen.add(name)
        if response and name == "location":
            validate_target(value)
        result.append((name, value))
    return tuple(result)


def _find_preview_root(session_root: Path) -> tuple[Path, str]:
    candidates = (
        (session_root / "dist", "index.html"),
        (session_root / "build", "index.html"),
        (session_root / "public", "preview.html"),
        (session_root / "public", "index.html"),
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
        part in {"", ".", ".."} or part.startswith(".") or _SAFE_SEGMENT.fullmatch(part) is None
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
