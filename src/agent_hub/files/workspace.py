"""Tenant/project/session scoped workspace files."""

from __future__ import annotations

import os
import platform as platform_module
import re
import shutil
import subprocess
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
from itertools import islice
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Literal
from urllib.parse import quote
from uuid import UUID

WORKSPACE_ZIP_MIME_TYPE = "application/zip"
_SAFE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
WorkspacePlatform = Literal["windows", "linux", "other"]
ExecutableFinder = Callable[[str], str | None]
_MAX_DIRECTORY_SCAN = 2_048
_MAX_DIRECTORY_RESULTS = 256


@dataclass(frozen=True, slots=True)
class WorkspaceRootDescription:
    platform: WorkspacePlatform
    separator: str
    configured_root: str


@dataclass(frozen=True, slots=True)
class NativePickerCapability:
    available: bool
    unavailable_reason: str | None
    executable: str | None
    picker: Literal["windows-folder-browser", "zenity", "kdialog"] | None


@dataclass(frozen=True, slots=True)
class ProjectWorkspaceDirectoryListing:
    project_id: str
    platform: WorkspacePlatform
    separator: str
    configured_root: str
    logical_root: str
    directories: tuple[str, ...]
    native_picker_available: bool
    unavailable_reason: str | None

    def to_public_dict(self) -> dict[str, str | bool | tuple[str, ...] | None]:
        return asdict(self)


class NativePickerUnavailable(RuntimeError):
    pass


class NativePickerCancelled(RuntimeError):
    pass


def describe_workspace_root(
    root: PurePath,
    *,
    system_name: str | None = None,
) -> WorkspaceRootDescription:
    name = (system_name or platform_module.system()).strip().casefold()
    if name == "windows":
        workspace_platform: WorkspacePlatform = "windows"
        separator = "\\"
    elif name == "linux":
        workspace_platform = "linux"
        separator = "/"
    else:
        workspace_platform = "other"
        separator = "\\" if isinstance(root, PureWindowsPath) else "/"
    return WorkspaceRootDescription(
        platform=workspace_platform,
        separator=separator,
        configured_root=str(root),
    )


def native_picker_capability(
    *,
    system_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    executable_finder: ExecutableFinder = shutil.which,
) -> NativePickerCapability:
    environment = os.environ if environ is None else environ
    name = (system_name or platform_module.system()).strip().casefold()
    if name == "windows":
        session_name = environment.get("SESSIONNAME", "").strip().casefold()
        if not session_name or session_name == "services":
            return NativePickerCapability(
                False,
                "interactive Windows desktop is unavailable",
                None,
                None,
            )
        executable = executable_finder("powershell.exe") or executable_finder("pwsh.exe")
        if executable is None:
            return NativePickerCapability(False, "PowerShell is unavailable", None, None)
        return NativePickerCapability(True, None, executable, "windows-folder-browser")
    if name == "linux":
        if not (environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY")):
            return NativePickerCapability(
                False,
                "interactive Linux display is unavailable",
                None,
                None,
            )
        zenity = executable_finder("zenity")
        if zenity is not None:
            return NativePickerCapability(True, None, zenity, "zenity")
        kdialog = executable_finder("kdialog")
        if kdialog is not None:
            return NativePickerCapability(True, None, kdialog, "kdialog")
        return NativePickerCapability(False, "zenity and kdialog are unavailable", None, None)
    return NativePickerCapability(False, "native directory picker is unsupported", None, None)


def build_native_picker_command(
    sessions_root: PurePath,
    capability: NativePickerCapability,
) -> tuple[str, ...]:
    if not capability.available or capability.executable is None or capability.picker is None:
        raise NativePickerUnavailable(
            capability.unavailable_reason or "native directory picker is unavailable"
        )
    if capability.picker == "windows-folder-browser":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$dialog.SelectedPath = $args[0]; "
            "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Write-Output $dialog.SelectedPath }"
        )
        return (
            capability.executable,
            "-NoProfile",
            "-STA",
            "-Command",
            script,
            str(sessions_root),
        )
    if capability.picker == "zenity":
        selected_root = f"{str(sessions_root).rstrip('/')}/"
        return (
            capability.executable,
            "--file-selection",
            "--directory",
            "--filename",
            selected_root,
        )
    return (capability.executable, "--getexistingdirectory", str(sessions_root))


def pick_native_directory(
    sessions_root: Path,
    capability: NativePickerCapability,
) -> Path | None:
    command = build_native_picker_command(sessions_root, capability)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise NativePickerUnavailable("native directory picker timed out") from exc
    except (OSError, UnicodeError) as exc:
        raise NativePickerUnavailable("native directory picker failed to start") from exc
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        raise NativePickerUnavailable("native directory picker failed")
    selected = completed.stdout.strip().splitlines()
    return Path(selected[0]) if selected else None


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
        return self._metadata_for(
            tenant_id, project_id, session_id, safe_path, destination, mime_type
        )

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

    def list_session_directories(
        self,
        tenant_id: UUID,
        project_id: str,
    ) -> ProjectWorkspaceDirectoryListing:
        project = _safe_workspace_segment(project_id)
        sessions_root = self.project_sessions_root(tenant_id, project)
        root_description = describe_workspace_root(self._root)
        capability = native_picker_capability()
        directories: list[str] = []
        if sessions_root.exists():
            for candidate in sorted(
                islice(sessions_root.iterdir(), _MAX_DIRECTORY_SCAN),
                key=lambda item: (item.name.casefold(), item.name),
            ):
                if _SAFE_SEGMENT.fullmatch(candidate.name) is None:
                    continue
                try:
                    if candidate.is_symlink() or candidate.is_junction() or not candidate.is_dir():
                        continue
                    resolved = candidate.resolve(strict=True)
                except OSError:
                    continue
                if resolved != candidate or resolved.parent != sessions_root:
                    continue
                directories.append(candidate.name)
                if len(directories) >= _MAX_DIRECTORY_RESULTS:
                    break
        separator = root_description.separator
        logical_root = separator.join((str(tenant_id), "projects", project, "sessions"))
        return ProjectWorkspaceDirectoryListing(
            project_id=project,
            platform=root_description.platform,
            separator=separator,
            configured_root=root_description.configured_root,
            logical_root=logical_root,
            directories=tuple(directories),
            native_picker_available=capability.available,
            unavailable_reason=capability.unavailable_reason,
        )

    def select_native_session_directory(
        self,
        tenant_id: UUID,
        project_id: str,
    ) -> str:
        sessions_root = self.project_sessions_root(tenant_id, project_id)
        capability = native_picker_capability()
        if not capability.available:
            raise NativePickerUnavailable(
                capability.unavailable_reason or "native directory picker is unavailable"
            )
        sessions_root.mkdir(parents=True, exist_ok=True)
        selected = pick_native_directory(sessions_root, capability)
        if selected is None:
            raise NativePickerCancelled("native directory selection was cancelled")
        return self._validated_selected_session(sessions_root, selected)

    def project_sessions_root(self, tenant_id: UUID, project_id: str) -> Path:
        project = _safe_workspace_segment(project_id)
        root = self._root
        for component in (str(tenant_id), "projects", project, "sessions"):
            root = root / component
            if root.is_symlink() or root.is_junction():
                raise ValueError("workspace path aliases another scope")
        resolved = root.resolve()
        if resolved != root or not resolved.is_relative_to(self._root):
            raise ValueError("workspace path escapes authorized scope")
        return resolved

    def session_root(self, tenant_id: UUID, project_id: str, session_id: str) -> Path:
        session = _safe_workspace_segment(session_id)
        root = self.project_sessions_root(tenant_id, project_id) / session
        if root.is_symlink() or root.is_junction():
            raise ValueError("workspace path aliases another scope")
        resolved = root.resolve()
        if resolved != root or not resolved.is_relative_to(self._root):
            raise ValueError("workspace path escapes authorized scope")
        return resolved

    @staticmethod
    def _validated_selected_session(sessions_root: Path, selected: Path) -> str:
        if not selected.is_absolute():
            raise ValueError("selected workspace directory must be absolute")
        if selected.is_symlink() or selected.is_junction() or not selected.is_dir():
            raise ValueError("selected workspace directory is not a safe directory")
        resolved = selected.resolve(strict=True)
        if resolved != selected or resolved.parent != sessions_root:
            raise ValueError("selected workspace directory escapes sessions root")
        if _SAFE_SEGMENT.fullmatch(resolved.name) is None:
            raise ValueError("selected workspace directory has an unsafe session name")
        return resolved.name

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
