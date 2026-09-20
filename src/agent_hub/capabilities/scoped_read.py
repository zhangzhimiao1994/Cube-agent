"""Read files only within the workspace or attachments authorized by a stored run."""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path, PureWindowsPath
from typing import Protocol, cast
from uuid import UUID

from agent_hub.capabilities.tools.workspace_read import WorkspaceReadResult

_SEGMENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_ATTACHMENT = re.compile(r"att_[a-f0-9]{32}")
_MAX_BYTES = 65_536


class ScopedReadError(RuntimeError):
    """A stored run does not authorize this file read."""


class _RunRepository(Protocol):
    async def get(self, tenant_id: UUID, run_id: UUID) -> object: ...


async def read_scoped_file(
    *,
    repository: object,
    tenant_id: UUID,
    run_id: UUID,
    project_root: Path | None,
    attachment_root: Path | None,
    path: str,
) -> WorkspaceReadResult:
    # Import lazily: runs.__init__ imports the harness gateway, which imports runtime.
    from agent_hub.runs.workspace import workspace_selection

    try:
        record = await cast(_RunRepository, repository).get(tenant_id, run_id)
        if getattr(record, "tenant_id", None) != tenant_id or getattr(record, "id", None) != run_id:
            raise ScopedReadError
        routing = getattr(record, "routing_decision", None)
        if not isinstance(routing, Mapping):
            raise ScopedReadError
        project = routing.get("project_id")
        session = routing.get("workspace_session_id")
        profile = routing.get("sandbox_profile")
        permissions = routing.get("requested_permissions")
        if (
            not isinstance(project, str) or _SEGMENT.fullmatch(project) is None
            or not isinstance(session, str) or _SEGMENT.fullmatch(session) is None
            or profile not in ("read_only", "restricted", "workspace_write")
            or not isinstance(permissions, (list, tuple))
            or not all(isinstance(item, str) for item in permissions)
            or "workspace.read" not in permissions
        ):
            raise ScopedReadError
        # Reuse the submission policy without allowing its implicit permission defaults.
        workspace_selection(
            project_id=project, session_id=session, sandbox_profile=profile,
            requested_permissions=permissions,
        )
        parts = _path_parts(path)
        if parts[0] == str(tenant_id):
            attachments = routing.get("attachment_ids")
            if not isinstance(attachments, (list, tuple)) or len(parts) < 2:
                raise ScopedReadError
            member = parts[1]
            attachment_id = member.split(".", 1)[0]
            if _ATTACHMENT.fullmatch(attachment_id) is None or attachment_id not in attachments:
                raise ScopedReadError
            if len(parts) == 2:
                if member not in (
                    f"{attachment_id}.bin", f"{attachment_id}.json",
                    f"{attachment_id}.manifest.json",
                ):
                    raise ScopedReadError
            elif member != attachment_id:
                raise ScopedReadError
            root = attachment_root
            scoped_parts = parts
        else:
            root = project_root
            scoped_parts = (str(tenant_id), "projects", project, "sessions", session, *parts)
        if root is None:
            raise ScopedReadError
        data = _read_no_links(root, scoped_parts)
        return WorkspaceReadResult(
            relative_path="/".join(parts),
            text=data[:_MAX_BYTES].decode("utf-8", errors="replace"),
            truncated=len(data) > _MAX_BYTES,
        )
    except Exception:  # noqa: BLE001 - fail closed with a redacted boundary error.
        # Repository, filesystem and parser errors must not disclose other scopes or host paths.
        raise ScopedReadError("workspace read denied or scoped file unavailable") from None


def _path_parts(path: str) -> tuple[str, ...]:
    windows = PureWindowsPath(path)
    if not path or windows.drive or windows.root or "\x00" in path:
        raise ScopedReadError
    parts = tuple(path.replace("\\", "/").split("/"))
    if any(
        part in ("", ".", "..") or ":" in part or part.endswith((" ", "."))
        or PureWindowsPath(part).is_reserved()
        for part in parts
    ):
        raise ScopedReadError
    return parts


def _is_link(value: os.stat_result) -> bool:
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _regular_file(value: os.stat_result) -> None:
    if _is_link(value) or not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise ScopedReadError


def _read_no_links(root: Path, parts: tuple[str, ...]) -> bytes:
    if os.name == "posix" and os.open in os.supports_dir_fd:
        # Hold directory descriptors so an ancestor rename/link swap cannot redirect the read.
        with ExitStack() as stack:
            nofollow, directory_flag, nonblock = (
                cast(int, getattr(os, name)) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")
            )
            flags = os.O_RDONLY | directory_flag | nofollow
            directory = os.open(root, flags)
            stack.callback(os.close, directory)
            for part in parts[:-1]:
                directory = os.open(part, flags, dir_fd=directory)
                stack.callback(os.close, directory)
            fd = os.open(
                parts[-1], os.O_RDONLY | nofollow | nonblock,
                dir_fd=directory,
            )
            with os.fdopen(fd, "rb") as handle:
                _regular_file(os.fstat(handle.fileno()))
                return handle.read(_MAX_BYTES + 1)
    if os.name != "nt":
        raise ScopedReadError
    return _read_windows_locked(root, parts)


def _read_windows_locked(root: Path, parts: tuple[str, ...]) -> bytes:
    if sys.platform != "win32":
        raise ScopedReadError
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("created", wintypes.FILETIME),
            ("accessed", wintypes.FILETIME),
            ("modified", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    absolute_root = Path(os.path.abspath(root))
    if re.fullmatch(r"[A-Za-z]:", absolute_root.drive) is None:
        # Network/device namespaces do not provide the local-filesystem locking contract.
        raise ScopedReadError
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(FileInformation))
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFileType.argtypes = (wintypes.HANDLE,)
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL

    def open_locked(path: Path, *, directory: bool) -> int:
        # FILE_SHARE_READ only: existing/new write or delete handles cannot coexist.
        # GENERIC_READ is required: attribute-only handles do not enforce these sharing checks.
        raw = kernel.CreateFileW(
            "\\\\?\\" + str(path), 0x80000000, 0x1, None,
            3, 0x00200000 | 0x02000000, None,
        )
        if raw is None or raw == ctypes.c_void_p(-1).value:
            raise ScopedReadError
        handle = cast(int, raw)
        try:
            info = FileInformation()
            if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                raise ScopedReadError
            if info.attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ScopedReadError
            if bool(info.attributes & stat.FILE_ATTRIBUTE_DIRECTORY) != directory:
                raise ScopedReadError
            if kernel.GetFileType(handle) != 1 or (not directory and info.links != 1):
                raise ScopedReadError
            return handle
        except BaseException:
            kernel.CloseHandle(handle)
            raise

    # Lock from the drive anchor, not just root: a junction above root must not redirect opens.
    with ExitStack() as stack:
        current = Path(absolute_root.anchor)
        stack.callback(kernel.CloseHandle, open_locked(current, directory=True))
        for part in (*absolute_root.parts[1:], *parts[:-1]):
            current = current / part
            stack.callback(kernel.CloseHandle, open_locked(current, directory=True))
        raw_file = open_locked(current / parts[-1], directory=False)
        try:
            fd = msvcrt.open_osfhandle(raw_file, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(raw_file)
            raise
        try:
            handle = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        # fdopen owns the native file handle; all ancestor handles remain held until it closes.
        with handle:
            _regular_file(os.fstat(handle.fileno()))
            return handle.read(_MAX_BYTES + 1)
