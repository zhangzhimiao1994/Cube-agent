"""Authenticated management and token-scoped delivery for website previews."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

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


@dataclass(frozen=True, slots=True)
class _PreviewAccess:
    tenant_id: UUID
    conversation_id: str
    token: str


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
    ) -> WebPreviewResponse:
        with self._lock:
            launch = self._manager.start(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                project_id=project_id,
                session_id=session_id,
                root=root,
            )
            key = (tenant_id, launch.state.conversation_id)
            previous_id = self._id_by_conversation.get(key)
            if previous_id is not None:
                self._access_by_id.pop(previous_id, None)
            self._access_by_id[launch.state.preview_id] = _PreviewAccess(
                tenant_id=tenant_id,
                conversation_id=launch.state.conversation_id,
                token=launch.token,
            )
            self._id_by_conversation[key] = launch.state.preview_id
        return _public_response(launch.state)

    def current(self, tenant_id: UUID, conversation_id: str) -> WebPreviewResponse | None:
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
            return _public_response(state)

    def renew(self, tenant_id: UUID, preview_id: str) -> WebPreviewResponse:
        access = self._owned_access(tenant_id, preview_id)
        state = self._manager.renew(preview_id, access.token)
        return _public_response(state)

    def stop(self, tenant_id: UUID, preview_id: str) -> WebPreviewResponse:
        access = self._owned_access(tenant_id, preview_id)
        state = self._manager.stop(preview_id)
        self._forget(preview_id, access)
        return _public_response(state)

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
            name: value
            for name, value in result.headers
            if name.casefold() != "content-length"
        }
        content = result.body
        content_type = headers.get("Content-Type", "").casefold()
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
                    head = re.search(r"<head\b[^>]*>", text, flags=re.IGNORECASE)
                    if head is not None:
                        text = f"{text[: head.end()]}{_PREVIEW_STORAGE_SHIM}{text[head.end() :]}"
                    else:
                        doctype = re.match(r"\s*<!doctype\s+html\s*>", text, flags=re.IGNORECASE)
                        offset = doctype.end() if doctype is not None else 0
                        text = f"{text[:offset]}{_PREVIEW_STORAGE_SHIM}{text[offset:]}"
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

    def _owned_access(self, tenant_id: UUID, preview_id: str) -> _PreviewAccess:
        with self._lock:
            access = self._access_by_id.get(preview_id)
        if access is None or access.tenant_id != tenant_id:
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
        raise PublicAPIError(409, "conversation_archived", "archived conversation cannot be previewed")
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


def _public_response(state: PreviewState) -> WebPreviewResponse:
    preview_url = None
    if state.status == "ready":
        preview_id = quote(state.preview_id, safe="")
        preview_url = f"/api/v1/web-previews/{preview_id}/content/"
    return WebPreviewResponse(
        id=state.preview_id,
        status=state.status,
        preview_url=preview_url,
        lease_expires_at=state.lease_expires_at,
    )


def _preview_error(error: Exception) -> PublicAPIError:
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
        )
        try:
            await _authorize_preview_scope(request, principal, body)
        except Exception:
            await asyncio.to_thread(service.stop, principal.tenant_id, result.id)
            raise
        access = service._owned_access(principal.tenant_id, result.id)
        response.set_cookie(
            _preview_cookie_name(result.id),
            access.token,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path=f"/api/v1/web-previews/{result.id}/content",
            max_age=7200,
        )
        return result
    except (
        InvalidPreviewPath,
        PreviewCapacityExceeded,
        PreviewNotFound,
        PreviewResponseTooLarge,
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
    service: Annotated[WebPreviewService, Depends(_preview_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:read"))],
) -> WebPreviewResponse:
    try:
        result = await asyncio.to_thread(service.current, principal.tenant_id, conversation_id)
    except (InvalidPreviewPath, ValueError) as error:
        raise _preview_error(error) from error
    if result is None:
        raise PublicAPIError(404, "preview_not_found", "preview was not found")
    return result


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
        return await asyncio.to_thread(service.renew, principal.tenant_id, preview_id)
    except (PreviewNotFound, PreviewTokenRejected, ValueError) as error:
        raise _preview_error(error) from error


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
        result = await asyncio.to_thread(service.stop, principal.tenant_id, preview_id)
        response.delete_cookie(
            _preview_cookie_name(preview_id),
            path=f"/api/v1/web-previews/{preview_id}/content",
        )
        return result
    except PreviewNotFound as error:
        raise _preview_error(error) from error


async def _preview_content(
    preview_id: str,
    asset_path: str,
    request: Request,
    service: WebPreviewService,
) -> Response:
    try:
        token = request.cookies.get(_preview_cookie_name(preview_id), "")
        return await asyncio.to_thread(service.read, preview_id, token, asset_path)
    except (
        InvalidPreviewPath,
        PreviewNotFound,
        PreviewResponseTooLarge,
        PreviewTokenRejected,
    ) as error:
        raise _preview_error(error) from error


def _preview_cookie_name(preview_id: str) -> str:
    return f"agent_preview_{preview_id.replace('-', '_')}"


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
