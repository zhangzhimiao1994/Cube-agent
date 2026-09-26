"""Project/session workspace file endpoints."""

from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from agent_hub.api.dependencies import require_permission
from agent_hub.api.errors import PublicAPIError, error_responses
from agent_hub.auth.models import AuthenticatedPrincipal
from agent_hub.files.workspace import (
    NativePickerCancelled,
    NativePickerUnavailable,
    ProjectWorkspaceBundle,
    ProjectWorkspaceDirectoryListing,
    ProjectWorkspaceFile,
    ProjectWorkspaceStore,
)

router = APIRouter(
    prefix="/api/v1/workspaces",
    tags=["workspaces"],
    responses=error_responses(401, 403, 404, 405, 422, 500, 503),
)


class ProjectWorkspaceStoreProtocol(Protocol):
    def list_session_directories(
        self,
        tenant_id: UUID,
        project_id: str,
    ) -> ProjectWorkspaceDirectoryListing: ...

    def select_native_session_directory(
        self,
        tenant_id: UUID,
        project_id: str,
    ) -> str: ...

    def list_files(
        self,
        tenant_id: UUID,
        project_id: str,
        session_id: str,
    ) -> tuple[ProjectWorkspaceFile, ...]: ...

    def resolve_file(
        self,
        tenant_id: UUID,
        project_id: str,
        session_id: str,
        relative_path: str,
    ) -> Path: ...

    def create_session_zip(
        self,
        tenant_id: UUID,
        project_id: str,
        session_id: str,
    ) -> ProjectWorkspaceBundle: ...

    def bundle_download_url(self, project_id: str, session_id: str) -> str: ...


class ProjectWorkspaceFileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    download_url: str


class ProjectWorkspaceFileListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ProjectWorkspaceFileResponse]
    bundle_download_url: str


class ProjectWorkspaceDirectoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    platform: str
    separator: str
    configured_root: str
    logical_root: str
    directories: list[str]
    native_picker_available: bool
    unavailable_reason: str | None


class NativeDirectorySelectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str


def _workspace_store(request: Request) -> ProjectWorkspaceStoreProtocol:
    store = getattr(request.app.state, "project_workspace_store", None)
    if store is not None:
        return cast(ProjectWorkspaceStoreProtocol, store)
    settings = getattr(request.app.state, "settings", None)
    root = (
        settings.project_workspace_dir
        if settings is not None and hasattr(settings, "project_workspace_dir")
        else Path.home() / ".agent-hub" / "workspaces"
    )
    store = ProjectWorkspaceStore(cast(Path, root))
    request.app.state.project_workspace_store = store
    return store


def _is_loopback_client(request: Request) -> bool:
    if request.client is None:
        return False
    host = request.client.host.strip()
    try:
        return ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return host.casefold() == "localhost"


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip().split("%", 1)[0]
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return normalized.casefold() == "localhost"


def _is_local_desktop_request(request: Request) -> bool:
    if not _is_loopback_client(request) or not _is_loopback_host(request.url.hostname):
        return False
    origin = request.headers.get("origin")
    return origin is None or _is_loopback_host(urlsplit(origin).hostname)


@router.get(
    "/projects/{project_id}/directories",
    response_model=ProjectWorkspaceDirectoryListResponse,
)
async def list_project_session_directories(
    request: Request,
    project_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:read"))],
    store: Annotated[ProjectWorkspaceStoreProtocol, Depends(_workspace_store)],
) -> ProjectWorkspaceDirectoryListResponse:
    try:
        listing = await run_in_threadpool(
            store.list_session_directories,
            principal.tenant_id,
            project_id,
        )
    except ValueError:
        raise PublicAPIError(404, "not_found", "not found") from None
    local_desktop = _is_local_desktop_request(request)
    return ProjectWorkspaceDirectoryListResponse(
        project_id=listing.project_id,
        platform=listing.platform,
        separator=listing.separator,
        configured_root=listing.configured_root if local_desktop else listing.logical_root,
        logical_root=listing.logical_root,
        directories=list(listing.directories),
        native_picker_available=listing.native_picker_available and local_desktop,
        unavailable_reason=(
            listing.unavailable_reason
            if local_desktop
            else "native picker requires a direct loopback desktop session"
        ),
    )


@router.post(
    "/projects/{project_id}/directories/select-native",
    response_model=NativeDirectorySelectionResponse,
    responses=error_responses(401, 403, 409, 422, 503),
)
async def select_native_project_session_directory(
    request: Request,
    project_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
    store: Annotated[ProjectWorkspaceStoreProtocol, Depends(_workspace_store)],
) -> NativeDirectorySelectionResponse:
    if not _is_local_desktop_request(request):
        raise PublicAPIError(
            403,
            "native_picker_local_only",
            "native directory picker is available only to loopback clients",
        )
    try:
        session_id = await run_in_threadpool(
            store.select_native_session_directory,
            principal.tenant_id,
            project_id,
        )
    except NativePickerCancelled:
        raise PublicAPIError(
            409,
            "native_picker_cancelled",
            "native directory selection was cancelled",
        ) from None
    except NativePickerUnavailable as exc:
        raise PublicAPIError(503, "native_picker_unavailable", str(exc)) from None
    except ValueError:
        raise PublicAPIError(
            422,
            "invalid_workspace_selection",
            "selected directory is outside the authorized sessions root",
        ) from None
    return NativeDirectorySelectionResponse(session_id=session_id)


@router.get(
    "/projects/{project_id}/sessions/{session_id}/files",
    response_model=ProjectWorkspaceFileListResponse,
)
async def list_workspace_files(
    project_id: str,
    session_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:read"))],
    store: Annotated[ProjectWorkspaceStoreProtocol, Depends(_workspace_store)],
) -> ProjectWorkspaceFileListResponse:
    try:
        items = store.list_files(principal.tenant_id, project_id, session_id)
    except ValueError:
        raise PublicAPIError(404, "not_found", "not found") from None
    return ProjectWorkspaceFileListResponse(
        items=[
            ProjectWorkspaceFileResponse.model_validate(item.to_public_dict()) for item in items
        ],
        bundle_download_url=store.bundle_download_url(project_id, session_id),
    )


@router.get(
    "/projects/{project_id}/sessions/{session_id}/files/download",
    response_class=FileResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def download_workspace_file(
    project_id: str,
    session_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:read"))],
    store: Annotated[ProjectWorkspaceStoreProtocol, Depends(_workspace_store)],
    path: str = Query(min_length=1, max_length=512),
) -> FileResponse:
    try:
        resolved = store.resolve_file(principal.tenant_id, project_id, session_id, path)
        metadata = next(
            item
            for item in store.list_files(principal.tenant_id, project_id, session_id)
            if item.path == path
        )
    except (FileNotFoundError, StopIteration, ValueError):
        raise PublicAPIError(404, "not_found", "not found") from None
    return FileResponse(resolved, media_type=metadata.mime_type, filename=metadata.filename)


@router.get(
    "/projects/{project_id}/sessions/{session_id}/bundle/download",
    response_class=FileResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def download_workspace_bundle(
    project_id: str,
    session_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:read"))],
    store: Annotated[ProjectWorkspaceStoreProtocol, Depends(_workspace_store)],
) -> FileResponse:
    try:
        bundle = store.create_session_zip(principal.tenant_id, project_id, session_id)
    except (FileNotFoundError, ValueError):
        raise PublicAPIError(404, "not_found", "not found") from None
    return FileResponse(bundle.path, media_type=bundle.mime_type, filename=bundle.filename)
