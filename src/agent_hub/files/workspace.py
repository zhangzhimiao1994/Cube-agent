"""Tenant/project/session scoped workspace files."""

from __future__ import annotations

import re
import zipfile
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote
from uuid import UUID

WORKSPACE_ZIP_MIME_TYPE = "application/zip"
_SAFE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class ProjectWorkspaceFile:
    path: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    download_url: str

    def to_public_dict(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProjectWorkspaceBundle:
    path: Path
    filename: str
    mime_type: str = WORKSPACE_ZIP_MIME_TYPE


class ProjectWorkspaceStore:
    """Safe file store for generated project/session workspaces."""

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = 50 * 1024 * 1024,
        max_bundle_files: int = 512,
        max_bundle_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        self._root = root.resolve()
        self._max_file_bytes = max_file_bytes
        self._max_bundle_files = max_bundle_files
        self._max_bundle_bytes = max_bundle_bytes

    def write_bytes(
        self,
        tenant_id: UUID,
        project_id: str,
        session_id: str,
        relative_path: str,
        data: bytes,
        mime_type: str,
    ) -> ProjectWorkspaceFile:
        if len(data) > self._max_file_bytes:
            raise ValueError("workspace file is too large")
        safe_path = _safe_workspace_path(relative_path)
        destination = self._resolve_candidate(
            tenant_id,
            project_id,
            session_id,
            safe_path,
            must_exist=False,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return self._metadata_for(tenant_id, project_id, session_id, safe_path, destination, mime_type)

    def list_files(
        self,
        tenant_id: UUID,
        project_id: str,
        session_id: str,
    ) -> tuple[ProjectWorkspaceFile, ...]:
        session_root = self.session_root(tenant_id, project_id, session_id)
        if not session_root.exists():
            return ()
        files: list[ProjectWorkspaceFile] = []
        for path in sorted(session_root.rglob("*"), key=lambda item: item.as_posix()):
            if not path.is_file():
                continue
            relative = path.relative_to(session_root).as_posix()
            if relative.startswith(".bundles/"):
                continue
            safe_path = _safe_workspace_path(relative)
            resolved = self._resolve_candidate(
                tenant_id,
                project_id,
                session_id,
                safe_path,
                must_exist=True,
            )
            files.append(
                self._metadata_for(
                    tenant_id,
                    project_id,
                    session_id,
                    safe_path,
                    resolved,
                    _guess_mime_type(safe_path),
                )
            )
        return tuple(files)

    def resolve_file(
        self,
        tenant_id: UUID,
        project_id: str,
        session_id: str,
        relative_path: str,
    ) -> Path:
        safe_path = _safe_workspace_path(relative_path)
        resolved = self._resolve_candidate(
            tenant_id,
            project_id,
            session_id,
            safe_path,
            must_exist=True,
        )
        if not resolved.is_file():
            raise FileNotFoundError(safe_path)
        return resolved

    def create_session_zip(
        self,
        tenant_id: UUID,
        project_id: str,
        session_id: str,
    ) -> ProjectWorkspaceBundle:
        files = self.list_files(tenant_id, project_id, session_id)
        if not files:
            raise FileNotFoundError("workspace has no files")
        if len(files) > self._max_bundle_files:
            raise ValueError("too many workspace files to bundle")
        total_bytes = sum(item.size_bytes for item in files)
        if total_bytes > self._max_bundle_bytes:
            raise ValueError("workspace bundle is too large")
        project = _safe_workspace_segment(project_id)
        session = _safe_workspace_segment(session_id)
        bundle_dir = self.session_root(tenant_id, project_id, session_id) / ".bundles"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = (bundle_dir / f"{project}-{session}-workspace.zip").resolve()
        if not bundle_path.is_relative_to(self.session_root(tenant_id, project_id, session_id)):
            raise ValueError("workspace path escapes session root")
        with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in files:
                archive.write(
                    self.resolve_file(tenant_id, project_id, session_id, item.path),
                    arcname=item.path,
                )
        return ProjectWorkspaceBundle(
            path=bundle_path,
            filename=f"{project}-{session}-workspace.zip",
        )

    def bundle_download_url(self, project_id: str, session_id: str) -> str:
        project = _safe_workspace_segment(project_id)
        session = _safe_workspace_segment(session_id)
        return f"/api/v1/workspaces/projects/{project}/sessions/{session}/bundle/download"

    def session_root(self, tenant_id: UUID, project_id: str, session_id: str) -> Path:
        project = _safe_workspace_segment(project_id)
        session = _safe_workspace_segment(session_id)
        root = (self._root / str(tenant_id) / "projects" / project / "sessions" / session).resolve()
        if not root.is_relative_to(self._root):
            raise ValueError("workspace path escapes store root")
        return root

    def _resolve_candidate(
        self,
        tenant_id: UUID,
        project_id: str,
        session_id: str,
        relative_path: str,
        *,
        must_exist: bool,
    ) -> Path:
        session_root = self.session_root(tenant_id, project_id, session_id)
        candidate = session_root / relative_path
        resolved = candidate.resolve(strict=must_exist)
        if not resolved.is_relative_to(session_root):
            raise ValueError("workspace path escapes session root")
        return resolved

    def _metadata_for(
        self,
        tenant_id: UUID,
        project_id: str,
        session_id: str,
        relative_path: str,
        path: Path,
        mime_type: str,
    ) -> ProjectWorkspaceFile:
        if path.stat().st_size > self._max_file_bytes:
            raise ValueError("workspace file is too large")
        data = path.read_bytes()
        project = _safe_workspace_segment(project_id)
        session = _safe_workspace_segment(session_id)
        encoded_path = quote(relative_path, safe="")
        return ProjectWorkspaceFile(
            path=relative_path,
            filename=PurePosixPath(relative_path).name,
            mime_type=mime_type,
            size_bytes=len(data),
            sha256=sha256(data).hexdigest(),
            download_url=(
                f"/api/v1/workspaces/projects/{project}/sessions/{session}/files/download"
                f"?path={encoded_path}"
            ),
        )


def _safe_workspace_segment(value: str) -> str:
    raw = (value or "default").strip()
    if not raw:
        raw = "default"
    if "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError("workspace segment must be a single safe path segment")
    normalized = re.sub(r"[^a-z0-9_-]+", "-", raw.casefold())
    normalized = re.sub(r"[-_]{2,}", "-", normalized).strip("-_")
    if not normalized:
        normalized = "default"
    if _SAFE_SEGMENT.fullmatch(normalized) is None:
        raise ValueError("workspace segment must be a safe path segment")
    return normalized


def _safe_workspace_path(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("workspace path must be unpadded and non-blank")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("workspace path must not contain control characters")
    if "\\" in value:
        raise ValueError("workspace path must use POSIX separators")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute():
        raise ValueError("workspace path must be relative")
    parts = posix.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("workspace path must not escape the session")
    if any(part.startswith(".") for part in parts):
        raise ValueError("workspace path must not contain hidden files")
    return posix.as_posix()


def _guess_mime_type(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith(".md"):
        return "text/markdown"
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith(".csv"):
        return "text/csv"
    if lowered.endswith(".html"):
        return "text/html"
    if lowered.endswith(".css"):
        return "text/css"
    if lowered.endswith(".js"):
        return "text/javascript"
    if lowered.endswith(".ts"):
        return "text/typescript"
    if lowered.endswith(".py"):
        return "text/x-python"
    if lowered.endswith(".zip"):
        return WORKSPACE_ZIP_MIME_TYPE
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    return "text/plain"
