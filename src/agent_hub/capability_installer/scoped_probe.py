from __future__ import annotations

import asyncio
import stat
from collections.abc import Callable, Sequence
from pathlib import Path

from agent_hub.capability_installer.environment import EnvironmentBuildError
from agent_hub.execution_scope_policy import read_execution_scope_policy
from agent_hub.plugins.runtime import (
    _readonly_bind_mount,
    _run_local_command,
    _validate_runtime_readonly_mount,
)
from agent_hub.process_namespace import BubblewrapStdioProcessFactory


def _canonical_path(path: Path) -> Path:
    if not path.is_absolute() or str(path) != str(path.resolve(strict=True)):
        raise ValueError("Probe path must be absolute and canonical")
    return path


def _validate_runtime_mounts(paths: Sequence[Path], protected_paths: Sequence[Path]) -> None:
    protected = tuple(path.resolve() for path in protected_paths)
    for path in paths:
        path.resolve(strict=True)
        source, destination = _readonly_bind_mount(path)
        _validate_runtime_readonly_mount(source, destination, protected)


def build_scoped_cli_probe(
    policy_file: Path, protected_paths: Sequence[Path]
) -> Callable[[Path, Path], None]:
    """Probe a parent-verified fresh artifact without granting execution readiness."""
    protected_arguments = tuple(protected_paths)

    def probe(executable: Path, staging: Path) -> None:
        try:
            _canonical_path(policy_file)
            if not stat.S_ISREG(policy_file.lstat().st_mode):
                raise ValueError("Probe policy must be a regular file")
            if any(not path.is_absolute() for path in protected_arguments):
                raise ValueError("Probe protected paths must be absolute")
            policy = read_execution_scope_policy(policy_file)
            for binding in policy.bindings:
                directory = _canonical_path(binding.working_directory)
                if not stat.S_ISDIR(directory.lstat().st_mode):
                    raise ValueError("Probe policy working directory unavailable")
            protected = (
                *protected_arguments,
                policy_file,
                *(binding.working_directory for binding in policy.bindings),
            )
        except (OSError, ValueError, RuntimeError):
            raise EnvironmentBuildError("Scoped CLI probe policy unavailable") from None

        try:
            staging = _canonical_path(staging)
            executable = _canonical_path(executable)
            if not stat.S_ISDIR(staging.lstat().st_mode):
                raise ValueError("Probe staging must be a directory")
            info = executable.lstat()
            if executable.parent != staging or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Probe requires a single-link artifact directly in staging")

            # Only the owned stage/program receive the ancestor exception. Runtime
            # mounts must first pass the complete protection set on every callback.
            _validate_runtime_mounts(policy.runtime_readonly_paths, protected)
            factory_protected = tuple(
                path
                for path in protected
                if path.resolve() == staging or not staging.is_relative_to(path.resolve())
            )
            factory = BubblewrapStdioProcessFactory(
                bubblewrap_executable=policy.bubblewrap_executable,
                working_directory=staging,
                approved_programs=(executable,),
                runtime_readonly_paths=policy.runtime_readonly_paths,
                protected_paths=factory_protected,
            )
            exit_code, _, _ = asyncio.run(
                _run_local_command(
                    (str(executable), "--version"),
                    timeout_seconds=10,
                    environment={},
                    max_output_bytes=65_536,
                    process_factory=factory,
                )
            )
            if exit_code != 0:
                raise EnvironmentBuildError("Scoped CLI probe failed")
        except (OSError, ValueError, RuntimeError):
            raise EnvironmentBuildError("Scoped CLI probe failed") from None

    return probe
