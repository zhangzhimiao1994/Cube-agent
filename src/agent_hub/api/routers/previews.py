"""Authenticated management and token-scoped delivery for website previews."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol, Self, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent_hub.api.dependencies import require_permission
from agent_hub.api.errors import PublicAPIError, error_responses
from agent_hub.auth.models import AuthenticatedPrincipal
from agent_hub.previews import (
    InvalidPreviewPath,
    PreviewCapacityExceeded,
    PreviewManager,
    PreviewNotFound,
    PreviewResponseTooLarge,
    PreviewState,
    PreviewTokenRejected,
)
from agent_hub.previews.cleanup import CleanupReceiptV1, PreviewCleanupRecord, PreviewIdentityV1
from agent_hub.previews.dynamic_runner import json_object
from agent_hub.previews.dynamic_runtime import (
    DynamicPreviewCleanupError,
    DynamicPreviewStartupFailed,
    DynamicPreviewUnavailable,
)

_MAX_REQUEST_BODY = 1024 * 1024
_MAX_REQUEST_WIRE = 1536 * 1024
_MAX_RESPONSE_WIRE = 12 * 1024 * 1024

router = APIRouter(
    prefix="/api/v1/web-previews",
    tags=["web-previews"],
    responses=error_responses(401, 403, 404, 405, 409, 413, 422, 429, 500, 503),
)
logger = logging.getLogger(__name__)

_PREVIEW_STORAGE_SHIM = """<script data-agent-preview-storage-shim>
(() => {
  const installStorage = (name) => {
    try {
      window[name].length;
      return;
    } catch (_error) {
      // Sandboxed previews intentionally have an opaque origin.
    }
    const maxEntries = 1024;
    const maxCharacters = 5 * 1024 * 1024;
    const values = new Map();
    let usedCharacters = 0;
    const storage = {
      get length() { return values.size; },
      clear() {
        values.clear();
        usedCharacters = 0;
      },
      getItem(key) {
        const normalized = String(key);
        return values.has(normalized) ? values.get(normalized) : null;
      },
      key(index) { return Array.from(values.keys())[Number(index)] ?? null; },
      removeItem(key) {
        const normalized = String(key);
        const previous = values.get(normalized);
        if (previous !== undefined) {
          usedCharacters -= normalized.length + previous.length;
          values.delete(normalized);
        }
      },
      setItem(key, value) {
        const normalizedKey = String(key);
        const normalizedValue = String(value);
        const previous = values.get(normalizedKey);
        const previousCharacters = previous === undefined
          ? 0
          : normalizedKey.length + previous.length;
        const nextCharacters = usedCharacters - previousCharacters
          + normalizedKey.length + normalizedValue.length;
        if (
          (previous === undefined && values.size >= maxEntries)
          || nextCharacters > maxCharacters
        ) {
          throw new DOMException("Storage quota exceeded", "QuotaExceededError");
        }
        values.set(normalizedKey, normalizedValue);
        usedCharacters = nextCharacters;
      },
    };
    Object.defineProperty(window, name, {
      configurable: false,
      enumerable: true,
      value: storage,
      writable: false,
    });
  };
  installStorage("localStorage");
  installStorage("sessionStorage");
})();
</script>"""


class WebPreviewStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    workspace_session_id: str = Field(min_length=1, max_length=64)
    root: str | None = Field(default=None, min_length=1, max_length=512)


class WebPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: Literal["ready", "stopped", "expired"]
    preview_url: str | None
    lease_expires_at: datetime | None
    application_transport: bool = False
    identity: dict[str, object]
    cleanup_url: str
    cleanup_receipt: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_cleanup(self) -> Self:
        identity = PreviewIdentityV1.from_wire(self.identity)
        if identity.preview_id != self.id or self.cleanup_url != _cleanup_url(self.id):
            raise ValueError("preview response identity mismatch")
        if self.cleanup_receipt is not None:
            receipt = CleanupReceiptV1.from_wire(self.cleanup_receipt)
            if receipt.identity != identity:
                raise ValueError("preview receipt identity mismatch")
        return self


class WebPreviewCleanupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    identity: dict[str, object]
    cleanup_receipt: dict[str, object] | None
    retention_expires_at: str | None

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        PreviewCleanupRecord.from_wire(self.model_dump())
        return self


class ApplicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    target: str = Field(min_length=1, max_length=4096)
    headers: list[list[str]] = Field(max_length=64)
    body_base64: str


class ApplicationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status_code: int
    headers: list[list[str]]
    body_base64: str


@dataclass(frozen=True, slots=True)
class _PreviewAccess:
    tenant_id: UUID
    conversation_id: str
    token: str
    user_id: UUID | None
    project_id: str
    session_id: str


class _ConversationService(Protocol):
    async def get_conversation(self, conversation_id: str) -> object: ...


class WebPreviewService:
    """Keep raw capability tokens outside the preview runtime state."""

    def __init__(self, manager: PreviewManager) -> None:
        self._manager = manager
        self._lock = threading.RLock()
        self._access_by_id: dict[str, _PreviewAccess] = {}
        self._id_by_conversation: dict[tuple[UUID, str], str] = {}
        self._reaper_stop = threading.Event()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop,
            name="preview-access-reaper",
            daemon=True,
        )
        self._reaper_thread.start()

    def start(
        self,
        *,
        tenant_id: UUID,
        conversation_id: str,
        project_id: str,
        session_id: str,
        root: str | None,
        user_id: UUID | None = None,
    ) -> WebPreviewResponse:
        launch = self._manager.start(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            project_id=project_id,
            session_id=session_id,
            root=root,
            user_id=user_id,
        )
        with self._lock:
            current = self._manager.current(tenant_id, launch.state.conversation_id)
            if current is None or current.preview_id != launch.state.preview_id:
                raise PreviewNotFound("preview was revoked before registration")
            key = (tenant_id, launch.state.conversation_id)
            previous_id = self._id_by_conversation.get(key)
            if previous_id is not None:
                self._access_by_id.pop(previous_id, None)
            self._access_by_id[launch.state.preview_id] = _PreviewAccess(
                tenant_id=tenant_id,
                conversation_id=launch.state.conversation_id,
                token=launch.token,
                user_id=user_id,
                project_id=launch.state.project_id,
                session_id=launch.state.session_id,
            )
            self._id_by_conversation[key] = launch.state.preview_id
        return _public_response(current)

    def current(
        self, tenant_id: UUID, conversation_id: str, user_id: UUID | None = None
    ) -> WebPreviewResponse | None:
        state = self._manager.current(tenant_id, conversation_id)
        if state is None:
            with self._lock:
                preview_id = self._id_by_conversation.get((tenant_id, conversation_id))
                access = self._access_by_id.get(preview_id or "")
            if preview_id is not None and access is not None:
                self._forget(preview_id, access)
            return None
        with self._lock:
            access = self._access_by_id.get(state.preview_id)
            if access is None or access.tenant_id != tenant_id:
                return None
            if user_id is not None and access.user_id != user_id:
                return None
            return _public_response(state)

    def renew(
        self, tenant_id: UUID, preview_id: str, user_id: UUID | None = None
    ) -> WebPreviewResponse:
        access = self._owned_access(tenant_id, preview_id, user_id)
        state = self._manager.renew(preview_id, access.token)
        return _public_response(state)

    def stop(
        self, tenant_id: UUID, preview_id: str, user_id: UUID | None = None
    ) -> WebPreviewResponse:
        try:
            access = self._owned_access(tenant_id, preview_id, user_id)
        except PreviewNotFound:
            record = self.cleanup(tenant_id, preview_id, user_id)
            receipt = record.cleanup_receipt
            if receipt is not None and receipt.status == "confirmed":
                return WebPreviewResponse(id=preview_id,
                    status="expired" if receipt.reason == "expired" else "stopped",
                    preview_url=None, lease_expires_at=None,
                    application_transport=record.identity.kind == "dynamic",
                    identity=record.identity.to_wire(), cleanup_url=_cleanup_url(preview_id),
                    cleanup_receipt=receipt.to_wire())
            access = None
        try:
            state = self._manager.stop(preview_id)
        except PreviewNotFound:
            raise DynamicPreviewCleanupError("historical preview cleanup remains unknown") from None
        final_record = self._manager.cleanup_record(preview_id)
        if final_record is None or final_record.cleanup_receipt is None or final_record.cleanup_receipt.status != "confirmed":
            raise DynamicPreviewCleanupError("preview cleanup evidence unavailable")
        if access is not None:
            self._forget(preview_id, access)
        return _public_response(state, final_record.cleanup_receipt)

    def cleanup(self, tenant_id: UUID, preview_id: str, user_id: UUID | None) -> PreviewCleanupRecord:
        record = self._manager.cleanup_record(preview_id)
        if (record is None or user_id is None or record.identity.user_id != str(user_id)
                or record.identity.tenant_id != str(tenant_id)):
            raise PreviewNotFound("preview does not exist")
        return record

    def app_request(
        self,
        principal: AuthenticatedPrincipal,
        preview_id: str,
        body: ApplicationRequest,
        decoded: bytes,
    ) -> ApplicationResponse:
        access = self._owned_access(principal.tenant_id, preview_id, principal.user_id)
        result = self._manager.app_request(
            preview_id,
            access.token,
            body.method,
            body.target,
            tuple((pair[0], pair[1]) for pair in body.headers),
            decoded,
        )
        response = ApplicationResponse(
            status_code=result.status_code,
            headers=[list(pair) for pair in result.headers],
            body_base64=base64.b64encode(result.body).decode("ascii"),
        )
        if len(response.model_dump_json().encode()) > _MAX_RESPONSE_WIRE:
            raise PreviewResponseTooLarge("application response envelope is too large")
        return response

    def stop_conversation(
        self,
        tenant_id: UUID,
        conversation_id: str,
    ) -> PreviewState | None:
        state = self._manager.stop_conversation(tenant_id, conversation_id)
        if state is None:
            return None
        with self._lock:
            access = self._access_by_id.get(state.preview_id)
        if access is not None:
            self._forget(state.preview_id, access)
        return state

    def read(self, preview_id: str, token: str, path: str) -> Response:
        result = self._manager.read(preview_id, token, path)
        headers = {
            name: value for name, value in result.headers if name.casefold() != "content-length"
        }
        content = result.body
        content_type = (result.header("Content-Type") or "").casefold()
        if self._manager.is_application(preview_id, token):
            headers.update(
                {
                    "Content-Security-Policy": (
                        "sandbox allow-scripts allow-forms; default-src 'self' data: blob:; "
                        "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                        "connect-src 'none'; frame-ancestors 'self'; base-uri 'self'; form-action 'none'"
                    ),
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                }
            )
        if result.status_code == 200 and (
            "text/html" in content_type or "text/css" in content_type
        ):
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                prefix = f"/api/v1/web-previews/{quote(preview_id, safe='')}/content/"
                text = re.sub(
                    r"((?:src|href|action)\s*=\s*['\"])/(?!/)",
                    rf"\1{prefix}",
                    text,
                    flags=re.IGNORECASE,
                )
                text = re.sub(
                    r"(url\(\s*['\"]?)/(?!/)",
                    rf"\1{prefix}",
                    text,
                    flags=re.IGNORECASE,
                )
                if "text/html" in content_type and not re.search(
                    r"<base\b",
                    text,
                    flags=re.IGNORECASE,
                ):
                    base = f'<base href="{prefix}">'
                    head = re.search(r"<head\b[^>]*>", text, flags=re.IGNORECASE)
                    if head is not None:
                        text = f"{text[: head.end()]}{base}{text[head.end() :]}"
                    else:
                        doctype = re.match(r"\s*<!doctype\s+html\s*>", text, flags=re.IGNORECASE)
                        offset = doctype.end() if doctype is not None else 0
                        text = f"{text[:offset]}{base}{text[offset:]}"
                if "text/html" in content_type:
                    shim = _PREVIEW_STORAGE_SHIM
                    dynamic = self._manager.is_application(preview_id, token)
                    if dynamic:
                        from agent_hub.previews.bridge import preview_fetch_shim

                        shim += preview_fetch_shim(preview_id)
                    head = re.search(r"<head\b[^>]*>", text, flags=re.IGNORECASE)
                    if head is not None and not dynamic:
                        text = f"{text[: head.end()]}{shim}{text[head.end() :]}"
                    else:
                        doctype = re.match(r"\s*<!doctype\s+html\s*>", text, flags=re.IGNORECASE)
                        offset = doctype.end() if doctype is not None else 0
                        text = f"{text[:offset]}{shim}{text[offset:]}"
                content = text.encode("utf-8")
        return Response(
            content=content,
            status_code=result.status_code,
            headers=headers,
        )

    def reap_expired_access(self) -> tuple[str, ...]:
        self._manager.reap_expired()
        with self._lock:
            accesses = tuple(self._access_by_id.items())
        expired: list[str] = []
        for preview_id, access in accesses:
            state = self._manager.current(access.tenant_id, access.conversation_id)
            if state is not None and state.preview_id == preview_id:
                continue
            self._forget(preview_id, access)
            expired.append(preview_id)
        return tuple(expired)

    def close(self) -> None:
        self._reaper_stop.set()
        self._manager.close()
        with self._lock:
            self._access_by_id.clear()
            self._id_by_conversation.clear()
        if self._reaper_thread is not threading.current_thread():
            self._reaper_thread.join(timeout=2)

    def _reaper_loop(self) -> None:
        while not self._reaper_stop.wait(5):
            try:
                self.reap_expired_access()
            except Exception:
                logger.exception("preview access reaper failed")

    def _owned_access(
        self, tenant_id: UUID, preview_id: str, user_id: UUID | None = None
    ) -> _PreviewAccess:
        with self._lock:
            access = self._access_by_id.get(preview_id)
        if (
            access is None
            or access.tenant_id != tenant_id
            or (user_id is not None and access.user_id != user_id)
        ):
            raise PreviewNotFound("preview does not exist")
        return access

    def _forget(self, preview_id: str, access: _PreviewAccess) -> None:
        with self._lock:
            self._access_by_id.pop(preview_id, None)
            key = (access.tenant_id, access.conversation_id)
            if self._id_by_conversation.get(key) == preview_id:
                self._id_by_conversation.pop(key, None)


def _preview_service(request: Request) -> WebPreviewService:
    service = getattr(request.app.state, "preview_manager", None)
    if service is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    return cast(WebPreviewService, service)


def _conversation_service(
    request: Request,
    principal: AuthenticatedPrincipal,
) -> _ConversationService:
    service = getattr(request.app.state, "admin_resource_service", None)
    if service is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    scope_for_principal = getattr(service, "for_principal", None)
    if callable(scope_for_principal):
        service = scope_for_principal(principal.tenant_id, principal.user_id)
    if not callable(getattr(service, "get_conversation", None)):
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    return cast(_ConversationService, service)


def _record_value(record: object, name: str) -> object:
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def _status_value(record: object) -> str:
    value = _record_value(record, "status")
    enum_value = getattr(value, "value", value)
    return enum_value if isinstance(enum_value, str) else ""


async def _authorize_preview_scope(
    request: Request,
    principal: AuthenticatedPrincipal,
    body: WebPreviewStartRequest,
) -> None:
    service = _conversation_service(request, principal)
    try:
        conversation = await service.get_conversation(body.conversation_id)
    except (KeyError, ValueError) as error:
        raise PublicAPIError(404, "conversation_not_found", "conversation was not found") from error
    if _record_value(conversation, "archived_at") is not None:
        raise PublicAPIError(
            409, "conversation_archived", "archived conversation cannot be previewed"
        )
    if (
        _record_value(conversation, "project_id") != body.project_id
        or _record_value(conversation, "workspace_path") != body.workspace_session_id
    ):
        raise PublicAPIError(
            409,
            "preview_scope_mismatch",
            "preview project and workspace must match the conversation",
        )
    runs = _record_value(conversation, "runs")
    if isinstance(runs, list | tuple) and any(
        _status_value(run) not in {"completed", "failed", "cancelled"} for run in runs
    ):
        raise PublicAPIError(
            409,
            "conversation_active",
            "website preview requires an idle conversation",
        )


def _cleanup_url(preview_id: str) -> str:
    return f"/api/v1/web-previews/{quote(preview_id, safe='')}/cleanup"


def _public_response(state: PreviewState, receipt: CleanupReceiptV1 | None = None) -> WebPreviewResponse:
    preview_url = None
    if state.status == "ready":
        preview_id = quote(state.preview_id, safe="")
        preview_url = f"/api/v1/web-previews/{preview_id}/content/"
    if state.identity is None:
        raise DynamicPreviewCleanupError("preview identity missing")
    return WebPreviewResponse(
        id=state.preview_id,
        status=state.status,
        preview_url=preview_url,
        lease_expires_at=state.lease_expires_at,
        application_transport=state.application_transport,
        identity=state.identity.to_wire(),
        cleanup_url=_cleanup_url(state.preview_id),
        cleanup_receipt=receipt.to_wire() if receipt else None,
    )


def _preview_error(error: Exception) -> PublicAPIError:
    if isinstance(error, DynamicPreviewStartupFailed):
        return PublicAPIError(
            503,
            "dynamic_preview_start_failed",
            "generated website preview preparation failed",
            details={"phase": error.phase, "reason": error.reason},
        )
    if isinstance(error, DynamicPreviewUnavailable):
        return PublicAPIError(
            503,
            "dynamic_preview_unavailable",
            "isolated website preview is currently unavailable",
        )
    if isinstance(error, DynamicPreviewCleanupError):
        return PublicAPIError(
            503, "preview_cleanup_pending", "preview access revoked; runtime cleanup is pending"
        )
    if isinstance(error, InvalidPreviewPath):
        return PublicAPIError(422, "invalid_preview_path", "preview path is invalid")
    if isinstance(error, PreviewResponseTooLarge):
        return PublicAPIError(413, "preview_response_too_large", "preview response is too large")
    if isinstance(error, PreviewCapacityExceeded):
        return PublicAPIError(429, "preview_capacity_exceeded", "preview capacity is exhausted")
    if isinstance(error, (PreviewNotFound, PreviewTokenRejected)):
        return PublicAPIError(404, "preview_not_found", "preview was not found")
    return PublicAPIError(409, "preview_conflict", "preview operation failed")


@router.post(
    "/start",
    response_model=WebPreviewResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def start_web_preview(
    body: WebPreviewStartRequest,
    request: Request,
    response: Response,
    service: Annotated[WebPreviewService, Depends(_preview_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
) -> WebPreviewResponse:
    await _authorize_preview_scope(request, principal, body)
    try:
        result = await asyncio.to_thread(
            service.start,
            tenant_id=principal.tenant_id,
            conversation_id=body.conversation_id,
            project_id=body.project_id,
            session_id=body.workspace_session_id,
            root=body.root,
            user_id=principal.user_id,
        )
        try:
            await _authorize_preview_scope(request, principal, body)
        except Exception:
            await asyncio.to_thread(service.stop, principal.tenant_id, result.id)
            raise
        access = service._owned_access(principal.tenant_id, result.id)
        _set_preview_cookie(response, request, result.id, access.token)
        return result
    except (
        InvalidPreviewPath,
        PreviewCapacityExceeded,
        PreviewNotFound,
        PreviewResponseTooLarge,
        PreviewTokenRejected,
        DynamicPreviewUnavailable,
        DynamicPreviewCleanupError,
        ValueError,
    ) as error:
        raise _preview_error(error) from error


@router.get(
    "/conversations/{conversation_id}",
    response_model=WebPreviewResponse,
    responses=error_responses(401, 403, 404),
)
async def current_web_preview(
    conversation_id: str,
    request: Request,
    response: Response,
    service: Annotated[WebPreviewService, Depends(_preview_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:read"))],
) -> WebPreviewResponse:
    try:
        result = await asyncio.to_thread(
            service.current, principal.tenant_id, conversation_id, principal.user_id
        )
        if result is None:
            raise PreviewNotFound("preview was not found")
        access = service._owned_access(principal.tenant_id, result.id, principal.user_id)
    except (PreviewNotFound, InvalidPreviewPath, DynamicPreviewCleanupError, ValueError) as error:
        raise _preview_error(error) from error
    await _authorize_preview_scope(
        request,
        principal,
        WebPreviewStartRequest(
            conversation_id=access.conversation_id,
            project_id=access.project_id,
            workspace_session_id=access.session_id,
        ),
    )
    # Authorization can await storage while the runtime is stopped, replaced or expires.
    try:
        confirmed = await asyncio.to_thread(
            service.current, principal.tenant_id, conversation_id, principal.user_id
        )
        if confirmed is None or confirmed.id != result.id:
            raise PreviewNotFound("preview was revoked during authorization")
        access = service._owned_access(principal.tenant_id, confirmed.id, principal.user_id)
    except (PreviewNotFound, InvalidPreviewPath, DynamicPreviewCleanupError, ValueError) as error:
        raise _preview_error(error) from error
    _set_preview_cookie(response, request, confirmed.id, access.token)
    return confirmed


@router.post(
    "/{preview_id}/renew",
    response_model=WebPreviewResponse,
    responses=error_responses(401, 403, 404, 409),
)
async def renew_web_preview(
    preview_id: str,
    service: Annotated[WebPreviewService, Depends(_preview_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
) -> WebPreviewResponse:
    try:
        return await asyncio.to_thread(
            service.renew, principal.tenant_id, preview_id, principal.user_id
        )
    except (PreviewNotFound, PreviewTokenRejected, DynamicPreviewCleanupError, ValueError) as error:
        raise _preview_error(error) from error


@router.get("/{preview_id}/cleanup", response_model=WebPreviewCleanupResponse)
async def get_web_preview_cleanup(
    preview_id: str,
    response: Response,
    service: Annotated[WebPreviewService, Depends(_preview_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:read"))],
) -> WebPreviewCleanupResponse:
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    try:
        record = await asyncio.to_thread(service.cleanup, principal.tenant_id, preview_id, principal.user_id)
    except PreviewNotFound:
        raise PublicAPIError(404, "preview_not_found", "preview was not found", headers=headers) from None
    response.headers.update(headers)
    return WebPreviewCleanupResponse.model_validate(record.to_wire())


@router.delete(
    "/{preview_id}",
    response_model=WebPreviewResponse,
    responses=error_responses(401, 403, 404),
)
async def stop_web_preview(
    preview_id: str,
    response: Response,
    service: Annotated[WebPreviewService, Depends(_preview_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
) -> WebPreviewResponse:
    try:
        result = await asyncio.to_thread(
            service.stop, principal.tenant_id, preview_id, principal.user_id
        )
        response.delete_cookie(
            _preview_cookie_name(preview_id),
            path=f"/api/v1/web-previews/{preview_id}/content",
        )
        return result
    except (PreviewNotFound, DynamicPreviewCleanupError) as error:
        raise _preview_error(error) from error


@router.post("/{preview_id}/app-request", response_model=ApplicationResponse)
async def application_request(
    preview_id: str,
    request: Request,
    service: Annotated[WebPreviewService, Depends(_preview_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
) -> ApplicationResponse:
    try:
        access = service._owned_access(principal.tenant_id, preview_id, principal.user_id)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise ValueError("application JSON envelope required")
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > _MAX_REQUEST_WIRE:
                raise PreviewResponseTooLarge("application envelope is too large")
            payload.extend(chunk)
        body = ApplicationRequest.model_validate(json_object(bytes(payload)))
        if any(len(pair) != 2 for pair in body.headers):
            raise ValueError("invalid application header pair")
        if len(body.body_base64) > ((_MAX_REQUEST_BODY + 2) // 3) * 4:
            raise PreviewResponseTooLarge("application body is too large")
        decoded = base64.b64decode(body.body_base64, validate=True)
        if len(decoded) > _MAX_REQUEST_BODY:
            raise PreviewResponseTooLarge("application body is too large")
        await _authorize_preview_scope(
            request,
            principal,
            WebPreviewStartRequest(
                conversation_id=access.conversation_id,
                project_id=access.project_id,
                workspace_session_id=access.session_id,
            ),
        )
        return await asyncio.to_thread(service.app_request, principal, preview_id, body, decoded)
    except (ValueError, ValidationError, binascii.Error) as error:
        raise PublicAPIError(
            422, "invalid_application_request", "application request is invalid"
        ) from error
    except (
        PreviewNotFound,
        PreviewTokenRejected,
        PreviewResponseTooLarge,
        DynamicPreviewUnavailable,
        DynamicPreviewCleanupError,
    ) as error:
        raise _preview_error(error) from error


async def _preview_content(
    preview_id: str,
    asset_path: str,
    request: Request,
    service: WebPreviewService,
) -> Response:
    try:
        token = request.cookies.get(_preview_cookie_name(preview_id), "")
        if await asyncio.to_thread(service._manager.is_application, preview_id, token):
            prefix = f"/api/v1/web-previews/{preview_id}/content/".encode("ascii")
            raw_path = request.scope["raw_path"]
            if raw_path.startswith(prefix):
                asset_path = raw_path[len(prefix) :].decode("ascii")
            query = request.scope["query_string"].decode("ascii")
            if query:
                asset_path += "?" + query
        return await asyncio.to_thread(service.read, preview_id, token, asset_path)
    except (
        InvalidPreviewPath,
        PreviewNotFound,
        PreviewResponseTooLarge,
        PreviewTokenRejected,
        DynamicPreviewUnavailable,
        DynamicPreviewCleanupError,
        ValueError,
    ) as error:
        raise _preview_error(error) from error


def _preview_cookie_name(preview_id: str) -> str:
    return f"agent_preview_{preview_id.replace('-', '_')}"


def _set_preview_cookie(
    response: Response, request: Request, preview_id: str, token: str,
) -> None:
    response.set_cookie(
        _preview_cookie_name(preview_id),
        token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path=f"/api/v1/web-previews/{preview_id}/content",
        max_age=7200,
    )


@router.get("/{preview_id}/content", response_model=None, include_in_schema=False)
@router.get("/{preview_id}/content/", response_model=None)
async def preview_root_content(
    preview_id: str,
    request: Request,
    service: Annotated[WebPreviewService, Depends(_preview_service)],
) -> Response:
    return await _preview_content(preview_id, "", request, service)


@router.get("/{preview_id}/content/{asset_path:path}", response_model=None)
async def preview_asset_content(
    preview_id: str,
    asset_path: str,
    request: Request,
    service: Annotated[WebPreviewService, Depends(_preview_service)],
) -> Response:
    return await _preview_content(preview_id, asset_path, request, service)


__all__ = ["WebPreviewService", "router"]
