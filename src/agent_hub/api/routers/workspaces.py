"""Project/session workspace file endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from agent_hub.api.dependencies import require_permission
from agent_hub.api.errors import PublicAPIError, error_responses
from agent_hub.auth.models import AuthenticatedPrincipal
from agent_hub.files.workspace import (
    ProjectWorkspaceBundle,
    ProjectWorkspaceFile,
    ProjectWorkspaceStore,
)

router = APIRouter(
    prefix="/api/v1/workspaces",
    tags=["workspaces"],
    responses=error_responses(401, 403, 404, 405, 422, 500, 503),
)


class ProjectWorkspaceStoreProtocol(Protocol):
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


def _workspace_store(request: Request) -> ProjectWorkspaceStoreProtocol:
    store = getattr(request.app.state, "project_workspace_store", None)
    if store is not None:
        return cast(ProjectWorkspaceStoreProtocol, store)
    settings = getattr(request.app.state, "settings", None)
    root = (
        settings.project_workspace_dir
        if settings is not None and hasattr(settings, "project_workspace_dir")
        else Path("/var/lib/agent-hub/workspaces")
    )
    store = ProjectWorkspaceStore(cast(Path, root))
    request.app.state.project_workspace_store = store
    return store


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
        items=[ProjectWorkspaceFileResponse.model_validate(item.to_public_dict()) for item in items],
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
