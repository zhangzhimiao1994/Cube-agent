from __future__ import annotations

import asyncio
import logging
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    EnvironmentBuildError,
    EnvironmentRecord,
    _safe_component,
    tenant_environment_id,
)
from agent_hub.plugins import runtime as plugin_runtime
from agent_hub.plugins.runtime import LocalCommandPluginAdapter, PluginInvocationContext
from agent_hub.process_namespace import BubblewrapStdioProcessFactory, ProcessNamespaceUnavailable
from agent_hub.runtime.contracts import JsonValue

if TYPE_CHECKING:
    from agent_hub.execution_scope_policy import ExecutionScopeBinding

_LOG = logging.getLogger(__name__)
_PYTHON_EXECUTABLE = re.compile(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", re.IGNORECASE)


async def _run_sync_owned[T](operation: Callable[[], T]) -> T:
    """Cancellation cannot abandon a thread that may still own filesystem state."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def managed_python_argv(record: EnvironmentRecord, argv: tuple[str, ...]) -> tuple[str, ...]:
    """Disable interpreter bytecode without passing Python flags to a console-script CLI."""
    if (
        record.kind == "python"
        and argv
        and argv[0] == str(record.path / record.executable)
        and _PYTHON_EXECUTABLE.fullmatch(Path(record.executable).name)
    ):
        return argv[0], "-B", *argv[1:]
    return argv


class WorkspaceScopeProcessFactory(BubblewrapStdioProcessFactory):
    """Exempt only an operator's canonical project cwd from its trusted parent root."""

    def __init__(
        self,
        *,
        bubblewrap_executable: Path,
        working_directory: Path,
        approved_programs: Sequence[Path],
        runtime_readonly_paths: Sequence[Path],
        protected_paths: Sequence[Path],
        authorized_workspace_root: Path | None = None,
    ) -> None:
        self._prepared_mounts: ContextVar[tuple[tuple[Path, Path], ...] | None] = ContextVar(
            "prepared_scope_mounts", default=None
        )
        self._workspace_root = authorized_workspace_root
        self._workspace_identity: tuple[Path, int, int] | None = None
        self._workspace_cwd = False
        self._ordinary_mount_paths = (*approved_programs, *runtime_readonly_paths)
        self._cwd_protections = tuple(protected_paths)
        if authorized_workspace_root is not None:
            root = authorized_workspace_root
            self._workspace_identity = self._identity(root)
            if self._workspace_identity[0] != root or not root.is_dir():
                raise ProcessNamespaceUnavailable("Authorized workspace root alias is unsupported")
            if working_directory == root:
                raise ProcessNamespaceUnavailable("Authorized workspace cwd must be a strict child")
            if working_directory.is_relative_to(root):
                if (
                    not working_directory.is_dir()
                    or working_directory.resolve(strict=True) != working_directory
                    or any(part.lower() == ".env" for part in working_directory.parts)
                ):
                    raise ProcessNamespaceUnavailable(
                        "Authorized workspace cwd alias is unsupported"
                    )
                self._workspace_cwd = True
                # Equivalent aliases remain protected and therefore cannot gain this exception.
                self._cwd_protections = tuple(path for path in protected_paths if path != root)
        super().__init__(
            bubblewrap_executable=bubblewrap_executable,
            working_directory=working_directory,
            approved_programs=approved_programs,
            runtime_readonly_paths=runtime_readonly_paths,
            protected_paths=protected_paths,
        )

    def _validated_mounts(self) -> tuple[tuple[Path, Path], ...]:
        prepared = self._prepared_mounts.get()
        if prepared is not None:
            return prepared
        if (
            self._workspace_root is not None
            and self._identity(self._workspace_root) != self._workspace_identity
        ):
            raise ProcessNamespaceUnavailable("Authorized workspace root identity changed")
        if not self._workspace_cwd:
            return super()._validated_mounts()
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
        # Validate explicit runtime/program inputs even if they duplicate the cwd mount.
        for path in self._ordinary_mount_paths:
            source, destination = plugin_runtime._readonly_bind_mount(path)
            plugin_runtime._validate_runtime_readonly_mount(
                source, destination, self._protected_identities
            )
        mounts = tuple(plugin_runtime._readonly_bind_mount(path) for path in self._mounts)
        for source, destination in mounts:
            protections = (
                tuple(path.resolve() for path in self._cwd_protections)
                if destination == self._working_directory
                else self._protected_identities
            )
            plugin_runtime._validate_runtime_readonly_mount(source, destination, protections)
        return mounts

    def _inspect_mount(self, source: Path, *, deadline: float) -> None:
        if self._prepared_mounts.get() is None:
            super()._inspect_mount(source, deadline=deadline)

    async def __call__(
        self,
        program: str,
        *args: str,
        stdin: int,
        stdout: int,
        stderr: int,
        env: Mapping[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        def prepare() -> tuple[tuple[Path, Path], ...]:
            # A child task may inherit its parent's context during spawning.
            token = self._prepared_mounts.set(None)
            try:
                mounts = self._validated_mounts()
                deadline = monotonic() + self._inspection_timeout_seconds
                for source, _ in mounts:
                    self._inspect_mount(source, deadline=deadline)
                return mounts
            finally:
                self._prepared_mounts.reset(token)

        mounts = await _run_sync_owned(prepare)
        # Only this call consumes its freshly inspected mounts. Keep the original
        # namespace authorization/argv/spawn path, without repeating disk scans.
        token = self._prepared_mounts.set(mounts)
        try:
            return await super().__call__(
                program, *args, stdin=stdin, stdout=stdout, stderr=stderr, env=env
            )
        finally:
            self._prepared_mounts.reset(token)


class _ManagedVersionFactory(WorkspaceScopeProcessFactory):
    """Reuse bwrap admission and spawning, exempting only one verified version mount."""

    def __init__(
        self,
        *,
        ordinary_factory: BubblewrapStdioProcessFactory,
        version: Path,
        manager_root: Path,
        revalidate: Callable[[], None],
        bubblewrap_executable: Path,
        working_directory: Path,
        approved_programs: Sequence[Path],
        runtime_readonly_paths: Sequence[Path],
        protected_paths: Sequence[Path],
        attested_python: bool,
        authorized_workspace_root: Path | None,
    ) -> None:
        self._ordinary_factory = ordinary_factory
        self._version = version
        self._revalidate = revalidate
        self._attested_python = attested_python
        roots = tuple(path.resolve() for path in protected_paths)
        if sum(path == manager_root for path in protected_paths) != 1:
            raise ProcessNamespaceUnavailable("Managed manager root protection is ambiguous")
        remaining = tuple(path for path in protected_paths if path != manager_root)
        if any(
            path.is_relative_to(manager_root) or manager_root.is_relative_to(path)
            for path in (path.resolve() for path in remaining)
        ):
            raise ProcessNamespaceUnavailable("Managed version overlaps other protected storage")
        self._full_paths = tuple(protected_paths)
        self._full_roots = roots
        super().__init__(
            bubblewrap_executable=bubblewrap_executable,
            working_directory=working_directory,
            approved_programs=approved_programs,
            runtime_readonly_paths=(*runtime_readonly_paths, version),
            protected_paths=remaining,
            authorized_workspace_root=authorized_workspace_root,
        )

    def _validated_mounts(self) -> tuple[tuple[Path, Path], ...]:
        prepared = self._prepared_mounts.get()
        if prepared is not None:
            return prepared
        self._ordinary_factory._validated_mounts()
        if tuple(path.resolve() for path in self._full_paths) != self._full_roots:
            raise ProcessNamespaceUnavailable("Managed protected storage identity changed")
        self._revalidate()
        # Commands inside this version are covered by its single directory mount.
        self._mounts = tuple(
            path
            for path in self._mounts
            if path == self._version or not path.is_relative_to(self._version)
        )
        return super()._validated_mounts()

    def _inspect_mount(self, source: Path, *, deadline: float) -> None:
        if self._prepared_mounts.get() is not None:
            return
        if source == self._version and self._attested_python:
            self._revalidate()
            if monotonic() >= deadline:
                raise ProcessNamespaceUnavailable("Managed Python tree inspection timed out")
            return
        super()._inspect_mount(source, deadline=deadline)

    async def __call__(
        self,
        program: str,
        *args: str,
        stdin: int,
        stdout: int,
        stderr: int,
        env: Mapping[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        # The native factory owns its fixed environment allowlist. Bytecode is also
        # disabled by -B for interpreters; console scripts keep the readonly tree.
        del env
        return await super().__call__(
            program, *args, stdin=stdin, stdout=stdout, stderr=stderr, env={}
        )


class ManagedEnvironmentPluginAdapter:
    """Operator-bound installed commands. No host fallback or caller-selected roots."""

    def __init__(
        self,
        *,
        binding: ExecutionScopeBinding,
        manager: CapabilityEnvironmentManager,
        ordinary_factory: BubblewrapStdioProcessFactory,
        bubblewrap_executable: Path,
        native_probe_program: Path,
        runtime_readonly_paths: Sequence[Path],
        protected_paths: Sequence[Path],
        authorized_workspace_root: Path | None = None,
    ) -> None:
        self._binding = binding
        self._manager = manager
        self._root = manager.root
        self._ordinary_factory = ordinary_factory
        self._launcher = bubblewrap_executable
        self._probe = native_probe_program
        self._runtime_paths = tuple(runtime_readonly_paths)
        self._protected_paths = tuple(protected_paths)
        self._authorized_workspace_root = authorized_workspace_root

    def _active(self) -> EnvironmentRecord:
        logical_id = self._binding.managed_environment_id
        if logical_id is None:
            raise RuntimeCapabilityError("Managed execution scope binding unavailable")
        capability_id = tenant_environment_id(self._binding.tenant_id, logical_id)
        try:
            if self._manager.root != self._root or self._root.resolve(strict=True) != self._root:
                raise EnvironmentBuildError("Managed environment root identity changed")
            record = self._manager.active(capability_id)
            _safe_component(record.version, "managed version")
            expected = self._root / capability_id / "versions" / record.version
            if record.capability_id != capability_id or record.path != expected:
                raise EnvironmentBuildError("Managed environment version identity changed")
            if expected.resolve(strict=True) != expected:
                raise EnvironmentBuildError("Managed environment version alias is unsupported")
            command = record.path / record.executable
            if not command.is_relative_to(expected) or not command.is_file():
                raise EnvironmentBuildError("Managed environment command alias is unsupported")
            if any(part.lower() == ".env" for part in expected.parts):
                raise EnvironmentBuildError("Managed environment .env alias is unsupported")
            if record.kind not in {"cli", "python"}:
                raise EnvironmentBuildError("Managed environment kind is unsupported")
            if record.kind == "python" and record.attestation_sha256 is None:
                raise EnvironmentBuildError("Python tree attestation missing; rebuild required")
            if command.resolve(strict=True) != command and not (
                record.kind == "python" and _PYTHON_EXECUTABLE.fullmatch(command.name)
            ):
                raise EnvironmentBuildError("Managed environment command alias is unsupported")
            if record.kind == "python":
                # Attestation permits interpreter links only. Their external targets
                # still need explicit ordinary mounts under the FULL protection set.
                interpreters = (command, expected / "bin/python", expected / "Scripts/python.exe")
                mounts = self._ordinary_factory._validated_mounts()
                for interpreter in interpreters:
                    if not interpreter.is_file():
                        continue
                    canonical = interpreter.resolve(strict=True)
                    if canonical.is_relative_to(expected):
                        continue
                    plugin_runtime._validate_runtime_readonly_mount(
                        canonical,
                        canonical,
                        tuple(path.resolve() for path in self._protected_paths),
                    )
                    if not any(
                        source == destination and canonical.is_relative_to(source)
                        for source, destination in mounts
                    ):
                        raise EnvironmentBuildError(
                            "Managed Python interpreter requires an explicit runtime mount"
                        )
            return record
        except (
            EnvironmentBuildError,
            KeyError,
            OSError,
            ValueError,
            RuntimeCapabilityError,
        ) as error:
            raise RuntimeCapabilityError(f"Managed environment unavailable: {error}") from error

    def _check_plugin(self, plugin: PluginResourceResponse, record: EnvironmentRecord) -> None:
        if plugin.id != self._binding.source_id:
            raise RuntimeCapabilityError("Managed execution scope source mismatch")
        command = plugin_runtime._local_command_string(plugin.resource_config.get("command"))
        if command != str(record.path / record.executable):
            raise RuntimeCapabilityError(
                "Managed execution scope command is not the installed path"
            )
        if plugin.credential_ref is not None or any(
            plugin.resource_config.get(field) for field in ("model", "model_id", "model_provider")
        ):
            raise RuntimeCapabilityError(
                "Plugin model or credentials unsupported in deny-net scope"
            )
        for field in ("env_passthrough", "required_env_any"):
            if plugin_runtime._local_command_string_list(plugin.resource_config.get(field), field):
                raise RuntimeCapabilityError("Plugin environment requirements unsupported in scope")
        for required in plugin_runtime._local_command_string_list(
            plugin.resource_config.get("required_commands"), "required_commands"
        ):
            resolved = plugin_runtime._resolve_local_command(required, shutil.which)
            if resolved is None or Path(resolved) not in self._binding.approved_programs:
                raise RuntimeCapabilityError("Required command is not operator approved")

    async def _with_version(
        self,
        plugin: PluginResourceResponse,
        operation: Callable[
            [LocalCommandPluginAdapter, _ManagedVersionFactory], Awaitable[Mapping[str, JsonValue]]
        ],
    ) -> Mapping[str, JsonValue]:
        def prepare() -> tuple[EnvironmentRecord, _ManagedVersionFactory, Callable[[], None]]:
            record = self._active()
            self._check_plugin(plugin, record)

            def revalidate() -> None:
                if self._active() != record:
                    raise RuntimeCapabilityError("Managed active environment changed")
                self._check_plugin(plugin, record)

            factory = _ManagedVersionFactory(
                ordinary_factory=self._ordinary_factory,
                version=record.path,
                manager_root=self._root,
                revalidate=revalidate,
                bubblewrap_executable=self._launcher,
                working_directory=self._binding.working_directory,
                approved_programs=(
                    *self._binding.approved_programs,
                    self._probe,
                    record.path / record.executable,
                ),
                runtime_readonly_paths=self._runtime_paths,
                protected_paths=self._protected_paths,
                attested_python=record.kind == "python",
                authorized_workspace_root=self._authorized_workspace_root,
            )
            return record, factory, revalidate

        record, factory, revalidate = await _run_sync_owned(prepare)

        async def runner(
            argv: tuple[str, ...],
            *,
            timeout_seconds: float,
            environment: Mapping[str, str],
            max_output_bytes: int,
        ) -> tuple[int, str, str]:
            del environment
            await _run_sync_owned(revalidate)
            return await plugin_runtime._run_local_command(
                managed_python_argv(record, argv),
                timeout_seconds=timeout_seconds,
                environment={"PYTHONDONTWRITEBYTECODE": "1"},
                max_output_bytes=max_output_bytes,
                process_factory=factory,
            )

        # Keep even Docker prerequisite probes inside the existing namespace factory.
        delegate = LocalCommandPluginAdapter(
            command_runner=runner,
            runtime_probe=lambda executable, timeout, environment: (
                plugin_runtime._probe_local_runtime(
                    executable, timeout, environment, process_factory=factory
                )
            ),
        )
        owner = f"managed-scope:{self._binding.tenant_id}:{uuid4().hex}"
        try:
            await _run_sync_owned(
                lambda: self._manager.add_reference(record.capability_id, record.version, owner)
            )
        except (EnvironmentBuildError, OSError, ValueError) as error:
            raise RuntimeCapabilityError("Managed environment reference unavailable") from error

        async def execute() -> Mapping[str, JsonValue]:
            await _run_sync_owned(revalidate)
            return await operation(delegate, factory)

        task = asyncio.create_task(execute())
        cancelled = False
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if not cancelled:
                        task.cancel()
                    cancelled = True
            result = task.result()
            if cancelled:
                raise asyncio.CancelledError
        except BaseException:
            # An exceptional runner result cannot attest completion of child/drain.
            _LOG.warning(
                "Managed environment reference retained: %s %s %s",
                record.capability_id,
                record.version,
                owner,
            )
            raise
        await _run_sync_owned(
            lambda: self._manager.remove_reference(record.capability_id, record.version, owner)
        )
        return result

    async def preflight(self, *, plugin: PluginResourceResponse) -> None:
        async def probe(
            delegate: LocalCommandPluginAdapter, factory: _ManagedVersionFactory
        ) -> Mapping[str, JsonValue]:
            await delegate.preflight(plugin=plugin)
            try:
                process = await factory(
                    str(self._probe),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except Exception as error:
                raise RuntimeCapabilityError("Plugin namespace probe unavailable") from error
            try:
                exit_code = await asyncio.wait_for(process.wait(), min(plugin.timeout_seconds, 5))
                if exit_code != 0:
                    raise RuntimeCapabilityError("Plugin namespace probe failed")
            except BaseException:
                await plugin_runtime._cleanup_local_command(process, [])
                raise
            return {}

        await self._with_version(plugin, probe)

    async def invoke(
        self,
        *,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        arguments: Mapping[str, JsonValue],
        context: PluginInvocationContext,
    ) -> Mapping[str, JsonValue]:
        if context.tenant_id != self._binding.tenant_id:
            raise RuntimeCapabilityError("Managed execution scope tenant mismatch")

        async def invoke(
            delegate: LocalCommandPluginAdapter, factory: _ManagedVersionFactory
        ) -> Mapping[str, JsonValue]:
            return await delegate.invoke(
                plugin=plugin, capability=capability, arguments=arguments, context=context
            )

        return await self._with_version(plugin, invoke)

    def descriptor(self) -> Mapping[str, JsonValue]:
        return LocalCommandPluginAdapter().descriptor()
