from __future__ import annotations

import asyncio
import math
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from time import monotonic

from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins.runtime import (
    _ensure_private_package_mount,
    _readonly_bind_mount,
    _validate_runtime_readonly_mount,
)


class ProcessNamespaceUnavailable(OSError):
    """The selected namespace could not be established; never permits host fallback."""


class BubblewrapStdioProcessFactory:
    """Explicit readonly execution scope for CLI and MCP; not a readiness receipt."""

    def __init__(
        self,
        *,
        bubblewrap_executable: Path,
        working_directory: Path,
        approved_programs: Sequence[Path],
        runtime_readonly_paths: Sequence[Path],
        protected_paths: Sequence[Path] = (),
        inspection_timeout_seconds: float = 10,
    ) -> None:
        if not math.isfinite(inspection_timeout_seconds) or inspection_timeout_seconds <= 0:
            raise ValueError("Namespace inspection timeout must be positive and finite")
        if not approved_programs:
            raise ValueError("Namespace requires explicitly approved programs")
        self._launcher = bubblewrap_executable
        self._working_directory = working_directory
        self._programs = frozenset(approved_programs)
        self._protected_paths = tuple(protected_paths)
        self._protected_identities = tuple(path.resolve() for path in protected_paths)
        self._inspection_timeout_seconds = inspection_timeout_seconds
        self._mounts = tuple(
            dict.fromkeys((working_directory, *approved_programs, *runtime_readonly_paths))
        )
        try:
            self._identities = {
                path: self._identity(path) for path in (*self._mounts, bubblewrap_executable)
            }
            self._validated_mounts()
        except (OSError, RuntimeCapabilityError) as error:
            raise ProcessNamespaceUnavailable(str(error)) from error

    @staticmethod
    def _identity(path: Path) -> tuple[Path, int, int]:
        if not path.is_absolute():
            raise ProcessNamespaceUnavailable("Namespace path is not absolute")
        canonical = path.resolve(strict=True)
        info = canonical.lstat()
        return canonical, info.st_dev, info.st_ino

    def _validated_mounts(self) -> tuple[tuple[Path, Path], ...]:
        if any(
            path.resolve() != identity
            for path, identity in zip(
                self._protected_paths, self._protected_identities, strict=True
            )
        ):
            raise ProcessNamespaceUnavailable("Namespace protected storage identity changed")
        for path, identity in self._identities.items():
            if self._identity(path) != identity:
                raise ProcessNamespaceUnavailable("Namespace path identity changed")
        mounts = tuple(_readonly_bind_mount(path) for path in self._mounts)
        for source, destination in mounts:
            _validate_runtime_readonly_mount(source, destination, self._protected_identities)
        return mounts

    def _inspect_mount(self, source: Path, *, deadline: float) -> None:
        if monotonic() >= deadline:
            raise ProcessNamespaceUnavailable("Namespace mount inspection timed out")
        info = source.lstat()
        if stat.S_ISDIR(info.st_mode):
            _ensure_private_package_mount(source, deadline=deadline)
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ProcessNamespaceUnavailable("Namespace mount contains unsafe filesystem entries")

    async def __call__(
        self,
        program: str,
        *args: str,
        stdin: int,
        stdout: int,
        stderr: int,
        env: Mapping[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        uid = getattr(os, "geteuid", lambda: -1)()
        if sys.platform != "linux" or uid <= 0:
            raise ProcessNamespaceUnavailable("Namespace requires nonroot Linux execution")
        if Path(program) not in self._programs:
            raise ProcessNamespaceUnavailable("Namespace program is not approved")
        if (
            stdin not in (asyncio.subprocess.PIPE, asyncio.subprocess.DEVNULL)
            or stdout not in (asyncio.subprocess.PIPE, asyncio.subprocess.DEVNULL)
            or stderr
            not in (asyncio.subprocess.PIPE, asyncio.subprocess.DEVNULL, asyncio.subprocess.STDOUT)
        ):
            raise ProcessNamespaceUnavailable("Namespace host descriptor is not allowed")
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
        }
        for key, value in ({} if env is None else env).items():
            if key not in environment:
                raise ProcessNamespaceUnavailable("Namespace environment key is not approved")
            if key not in {"HOME", "TMPDIR"}:
                environment[key] = value
        try:
            mounts = self._validated_mounts()
            deadline = monotonic() + self._inspection_timeout_seconds
            for source, _ in mounts:
                self._inspect_mount(source, deadline=deadline)
        except (OSError, RuntimeCapabilityError) as error:
            raise ProcessNamespaceUnavailable(str(error)) from error
        argv = (
            str(self._launcher),
            "--die-with-parent",
            "--new-session",
            "--unshare-net",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--clearenv",
            "--tmpfs",
            "/tmp",
            *(
                argument
                for source, destination in mounts
                for argument in ("--ro-bind", str(source), str(destination))
            ),
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--remount-ro",
            "/",
            "--chdir",
            str(self._working_directory),
            *(
                argument
                for key, value in environment.items()
                for argument in ("--setenv", key, value)
            ),
            "--",
            program,
            *args,
        )
        # User-selected values reach the child only after bwrap establishes the boundary.
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
