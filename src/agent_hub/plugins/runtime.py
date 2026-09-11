from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import monotonic as default_monotonic
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from jsonschema import ValidationError  # type: ignore[import-untyped]
from jsonschema.protocols import Validator  # type: ignore[import-untyped]

from agent_hub.api.routers.admin import (
    PLUGIN_PACKAGE_DEPENDENCIES_UNSUPPORTED_REASON,
    SUPPORTED_PLUGIN_PACKAGE_SDK_API_VERSIONS,
    SUPPORTED_RUNTIME_REGISTERED_PACKAGE_ISOLATIONS,
    SUPPORTED_RUNTIME_REGISTERED_PACKAGE_RUNTIMES,
    PluginCapabilityRequest,
    PluginPackageMetadata,
    PluginResourceResponse,
)
from agent_hub.auth.models import Role
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.capabilities.tools.registry import PluginConfigCapabilityManifestSource
from agent_hub.capabilities.types import PolicyEffect
from agent_hub.plugins.contracts import (
    ADAPTER_DECLARABLE_PLUGIN_SANDBOX_PROFILES,
    SUPPORTED_PLUGIN_SANDBOX_PROFILES,
    adapter_declared_sandbox_profiles,
    adapter_descriptor_with_contract,
    http_json_adapter_descriptor,
)
from agent_hub.plugins.dependency_policy import (
    PluginPackageDependencyLike,
    PluginPackageDependencyPolicy,
    plugin_package_dependency_lock,
)
from agent_hub.plugins.schemas import PluginSchemaError, plugin_schema_validator
from agent_hub.runtime.contracts import JsonValue, _freeze_object, _mutable_json

_PLUGIN_PACKAGE_DEPENDENCY_BOOTSTRAP = (
    "import runpy, sys; "
    "sys.path.insert(0, sys.argv[1]); "
    "runpy.run_path(sys.argv[2], run_name='__main__')"
)


class PluginConfigService(Protocol):
    async def list_plugins(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> Sequence[Any]: ...


class PluginAuditRecorder(Protocol):
    async def record_audit_event(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        details: dict[str, object] | None = None,
        tenant_id: UUID | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class PluginInvocationContext:
    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    actor: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PluginPackageExecutionTarget:
    root: Path
    entrypoint: Path
    dependency_root: Path | None = None


class PluginAdapter(Protocol):
    async def invoke(
        self,
        *,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        arguments: Mapping[str, JsonValue],
        context: PluginInvocationContext,
    ) -> Mapping[str, JsonValue]: ...


type HttpJsonPost = Callable[
    [str, Mapping[str, JsonValue], float, Mapping[str, str]],
    Awaitable[Mapping[str, JsonValue]],
]
type PluginSecretResolver = Callable[[str], Awaitable[str]]


class PluginPackageRunner(Protocol):
    async def invoke(
        self,
        *,
        target: PluginPackageExecutionTarget,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        arguments: Mapping[str, JsonValue],
        context: PluginInvocationContext,
    ) -> Mapping[str, JsonValue]: ...


class PluginPackageProcessLauncher(Protocol):
    def argv(
        self,
        *,
        python_executable: str,
        target: PluginPackageExecutionTarget,
    ) -> tuple[str, ...]: ...


class HttpJsonPluginAdapter:
    def __init__(
        self,
        *,
        post_json: HttpJsonPost | None = None,
        secret_resolver: PluginSecretResolver | None = None,
    ) -> None:
        self._post_json = _httpx_post_json if post_json is None else post_json
        self._secret_resolver = secret_resolver

    async def invoke(
        self,
        *,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        arguments: Mapping[str, JsonValue],
        context: PluginInvocationContext,
    ) -> Mapping[str, JsonValue]:
        url = _plugin_endpoint_url(plugin)
        if not _endpoint_domain_allowed(url, plugin.domain_allowlist):
            raise RuntimeCapabilityError("Plugin endpoint not allowed")
        payload: Mapping[str, JsonValue] = {
            "plugin_id": plugin.id,
            "capability_id": capability.id,
            "arguments": arguments,
            "resource_config": plugin.resource_config,
            "capability_config": capability.capability_config,
            "context": {
                "tenant_id": str(context.tenant_id),
                "user_id": str(context.user_id),
                "run_id": str(context.run_id),
                "actor": context.actor,
                "idempotency_key": context.idempotency_key,
            },
        }
        json_payload = cast(Mapping[str, JsonValue], _mutable_json(cast(JsonValue, payload)))
        headers = await self._headers_for_plugin(plugin)
        try:
            result = await self._post_json(url, json_payload, plugin.timeout_seconds, headers)
        except TimeoutError as error:
            raise RuntimeCapabilityError("Plugin tool timed out") from error
        except httpx.TimeoutException as error:
            raise RuntimeCapabilityError("Plugin tool timed out") from error
        except RuntimeCapabilityError:
            raise
        except Exception as error:
            raise RuntimeCapabilityError("Plugin tool failed") from error
        if not isinstance(result, Mapping):
            raise RuntimeCapabilityError("Plugin result is invalid")
        return result

    async def _headers_for_plugin(self, plugin: PluginResourceResponse) -> Mapping[str, str]:
        credential_ref = plugin.credential_ref
        if credential_ref is None:
            return {}
        if self._secret_resolver is None:
            raise RuntimeCapabilityError("Plugin credential unavailable")
        try:
            credential = await self._secret_resolver(credential_ref)
        except Exception as error:
            raise RuntimeCapabilityError("Plugin credential unavailable") from error
        if not isinstance(credential, str) or not credential:
            raise RuntimeCapabilityError("Plugin credential unavailable")
        scheme = plugin.credential_scheme.strip()
        value = credential if not scheme else f"{scheme} {credential}"
        return {plugin.credential_header: value}

    def descriptor(self) -> Mapping[str, JsonValue]:
        return adapter_descriptor_with_contract(http_json_adapter_descriptor())


class PluginPackageAdapter:
    def __init__(
        self,
        *,
        adapter_id: str,
        package_store_dir: Path,
        runner: PluginPackageRunner,
        dependency_policy: PluginPackageDependencyPolicy | None = None,
    ) -> None:
        self._adapter_id = adapter_id
        self._package_store_dir = package_store_dir
        self._runner = runner
        self._dependency_policy = dependency_policy

    async def invoke(
        self,
        *,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        arguments: Mapping[str, JsonValue],
        context: PluginInvocationContext,
    ) -> Mapping[str, JsonValue]:
        package = plugin.package_metadata
        if (
            capability.adapter != self._adapter_id
            or package is None
            or package.adapter_id != self._adapter_id
        ):
            raise RuntimeCapabilityError("Plugin package adapter mismatch")
        target = _plugin_package_execution_target(
            plugin,
            tenant_id=context.tenant_id,
            package_store_dir=self._package_store_dir,
            dependency_policy=self._dependency_policy,
        )
        try:
            result = await self._runner.invoke(
                target=target,
                plugin=plugin,
                capability=capability,
                arguments=arguments,
                context=context,
            )
        except RuntimeCapabilityError:
            raise
        except Exception as error:
            raise RuntimeCapabilityError("Plugin tool failed") from error
        if not isinstance(result, Mapping):
            raise RuntimeCapabilityError("Plugin result is invalid")
        return result

    def descriptor(self) -> Mapping[str, JsonValue]:
        return adapter_descriptor_with_contract(_package_adapter_descriptor(self._adapter_id))


class BubblewrapPluginPackageProcessLauncher:
    def __init__(
        self,
        *,
        bubblewrap_executable: Path,
        readonly_bind_paths: Sequence[Path] | None = None,
    ) -> None:
        self._bubblewrap_executable = bubblewrap_executable
        self._readonly_bind_paths = tuple(readonly_bind_paths or ())

    def argv(
        self,
        *,
        python_executable: str,
        target: PluginPackageExecutionTarget,
    ) -> tuple[str, ...]:
        root = target.root
        dependency_bind_args: tuple[str, ...] = ()
        if target.dependency_root is not None:
            dependency_bind_args = (
                "--ro-bind",
                str(target.dependency_root),
                str(target.dependency_root),
            )
        readonly_bind_paths = self._readonly_bind_paths or (
            _default_python_runtime_readonly_bind_paths(python_executable)
        )
        readonly_bind_args = tuple(
            argument
            for path in readonly_bind_paths
            for argument in ("--ro-bind", str(path), str(path))
        )
        return (
            str(self._bubblewrap_executable),
            "--die-with-parent",
            "--new-session",
            "--unshare-net",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--tmpfs",
            "/tmp",
            "--ro-bind",
            str(root),
            str(root),
            *dependency_bind_args,
            *readonly_bind_args,
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--chdir",
            str(root),
            "--",
            python_executable,
            *_plugin_package_python_args(target),
        )


def _default_python_runtime_readonly_bind_paths(python_executable: str) -> tuple[Path, ...]:
    candidates = [
        Path(python_executable).resolve().parent,
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
    ]
    for system_path in (Path("/lib"), Path("/lib64"), Path("/usr/lib"), Path("/usr/lib64")):
        if system_path.exists():
            candidates.append(system_path.resolve())
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if path.exists() and key not in seen:
            deduped.append(path)
            seen.add(key)
    return tuple(deduped)


class PythonSubprocessPluginPackageRunner:
    def __init__(
        self,
        *,
        python_executable: str | None = None,
        process_launcher: PluginPackageProcessLauncher | None = None,
        timeout_seconds: float = 10,
        max_stdin_bytes: int = 262_144,
        max_stdout_bytes: int = 262_144,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._python_executable = sys.executable if python_executable is None else python_executable
        self._process_launcher = process_launcher
        self._timeout_seconds = max(0.001, timeout_seconds)
        self._max_stdin_bytes = max(1, max_stdin_bytes)
        self._max_stdout_bytes = max(1, max_stdout_bytes)
        self._environment = _minimal_python_subprocess_environment(environment)

    async def invoke(
        self,
        *,
        target: PluginPackageExecutionTarget,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        arguments: Mapping[str, JsonValue],
        context: PluginInvocationContext,
    ) -> Mapping[str, JsonValue]:
        _ensure_plugin_package_runner_target(target)
        request = _plugin_package_runner_request(
            plugin=plugin,
            capability=capability,
            arguments=arguments,
            context=context,
        )
        payload = json.dumps(
            request,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > self._max_stdin_bytes:
            raise RuntimeCapabilityError("Plugin request is too large")
        argv = (
            self._process_launcher.argv(
                python_executable=self._python_executable,
                target=target,
            )
            if self._process_launcher is not None
            else (self._python_executable, *_plugin_package_python_args(target))
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=target.root,
                env=self._environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as error:
            raise RuntimeCapabilityError("Plugin tool failed") from error
        try:
            stdout = await asyncio.wait_for(
                _communicate_with_bounded_stdout(
                    process,
                    payload,
                    max_stdout_bytes=self._max_stdout_bytes,
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as error:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(Exception):
                await process.wait()
            raise RuntimeCapabilityError("Plugin tool timed out") from error
        if process.returncode != 0:
            raise RuntimeCapabilityError("Plugin tool failed")
        try:
            decoded = json.loads(stdout.decode("utf-8"))
            return _freeze_object(decoded, name="plugin result")
        except Exception as error:
            raise RuntimeCapabilityError("Plugin result is invalid") from error


async def _communicate_with_bounded_stdout(
    process: asyncio.subprocess.Process,
    payload: bytes,
    *,
    max_stdout_bytes: int,
) -> bytes:
    if process.stdin is None or process.stdout is None:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(Exception):
            await process.wait()
        raise RuntimeCapabilityError("Plugin tool failed")
    try:
        process.stdin.write(payload)
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()
    except (BrokenPipeError, ConnectionResetError):
        pass
    chunks: list[bytes] = []
    total_size = 0
    while True:
        chunk = await process.stdout.read(max(1, min(65536, max_stdout_bytes + 1 - total_size)))
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > max_stdout_bytes:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(Exception):
                await process.wait()
            raise RuntimeCapabilityError("Plugin result is invalid")
        chunks.append(chunk)
    await process.wait()
    return b"".join(chunks)


def _ensure_plugin_package_runner_target(target: PluginPackageExecutionTarget) -> None:
    _ensure_runtime_path_inside(target.root, target.entrypoint)
    if not target.root.is_dir():
        raise RuntimeCapabilityError("Plugin package artifact is unavailable")
    if not target.entrypoint.is_file():
        raise RuntimeCapabilityError("Plugin package entrypoint is unavailable")
    if target.dependency_root is not None and not target.dependency_root.is_dir():
        raise RuntimeCapabilityError("Plugin dependency cache is unavailable")


def _plugin_package_python_args(target: PluginPackageExecutionTarget) -> tuple[str, ...]:
    if target.dependency_root is None:
        return ("-I", str(target.entrypoint))
    return (
        "-I",
        "-c",
        _PLUGIN_PACKAGE_DEPENDENCY_BOOTSTRAP,
        str(target.dependency_root),
        str(target.entrypoint),
    )


class RuntimePluginService:
    def __init__(
        self,
        *,
        tenant_id: UUID,
        admin_service: PluginConfigService,
        adapters: Mapping[str, PluginAdapter] | None = None,
        dependency_policy: PluginPackageDependencyPolicy | None = None,
        cache_ttl_seconds: float = 60.0,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._admin_service = admin_service
        self._adapters = _default_plugin_adapters(admin_service)
        self._adapters.update(adapters or {})
        self._dependency_policy = dependency_policy or PluginPackageDependencyPolicy()
        self._plugins_by_tenant: dict[UUID, tuple[PluginResourceResponse, ...]] = {}
        self._plugins_loaded_at: dict[UUID, float] = {}
        self._cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self._monotonic = default_monotonic if monotonic is None else monotonic
        self._reload_lock = asyncio.Lock()
        self._reload_tasks_by_tenant: dict[UUID, asyncio.Task[None]] = {}

    async def start(self) -> None:
        await self.reload(self._tenant_id)

    async def reload(self, tenant_id: UUID | None = None) -> None:
        if tenant_id is None:
            for target_tenant_id in self._loaded_tenant_ids():
                await self.reload(target_tenant_id)
            return
        async with self._reload_lock:
            task = self._reload_tasks_by_tenant.get(tenant_id)
            if task is None or task.done():
                task = asyncio.create_task(self._reload_tenant(tenant_id))
                self._reload_tasks_by_tenant[tenant_id] = task
                target_tenant_id = tenant_id

                def discard_completed_task(completed_task: asyncio.Future[None]) -> None:
                    self._discard_reload_task(target_tenant_id, completed_task)

                task.add_done_callback(discard_completed_task)
        await asyncio.shield(task)

    def _discard_reload_task(
        self,
        tenant_id: UUID,
        completed_task: asyncio.Future[None],
    ) -> None:
        if self._reload_tasks_by_tenant.get(tenant_id) is completed_task:
            del self._reload_tasks_by_tenant[tenant_id]

    async def _reload_tenant(self, target_tenant_id: UUID) -> None:
        try:
            self._plugins_by_tenant[target_tenant_id] = tuple(
                PluginResourceResponse.model_validate(plugin)
                for plugin in await self._admin_service.list_plugins(
                    tenant_id=target_tenant_id,
                )
            )
        except Exception:  # noqa: BLE001 - plugin runtime context must fail closed.
            self._plugins_by_tenant[target_tenant_id] = ()
        self._plugins_loaded_at[target_tenant_id] = self._monotonic()

    async def ensure_tenant_loaded(self, tenant_id: UUID) -> None:
        if tenant_id not in self._plugins_by_tenant or self._tenant_cache_is_stale(tenant_id):
            await self.reload(tenant_id)

    def _loaded_tenant_ids(self) -> tuple[UUID, ...]:
        return tuple(dict.fromkeys((self._tenant_id, *self._plugins_by_tenant)))

    def _tenant_cache_is_stale(self, tenant_id: UUID) -> bool:
        loaded_at = self._plugins_loaded_at.get(tenant_id)
        if loaded_at is None:
            return True
        if _plugin_cache_has_expired_package_signature_trust(self._plugins_by_tenant.get(tenant_id)):
            return True
        return self._monotonic() - loaded_at >= self._cache_ttl_seconds

    def capability_manifest_source(self) -> RuntimePluginService:
        return self

    def manifests_for_tenant(self, tenant_id: UUID) -> Mapping[str, JsonValue]:
        plugins = self._plugins_for_tenant(tenant_id)
        if plugins is None:
            return _empty_manifest()
        return PluginConfigCapabilityManifestSource(
            cast(
                Any,
                _plugins_with_runtime_activation(
                    plugins,
                    self._adapters,
                    dependency_policy=self._dependency_policy,
                ),
            ),
            dependency_policy=self._dependency_policy,
        ).manifests()

    def adapter_descriptors(self) -> tuple[Mapping[str, JsonValue], ...]:
        return tuple(
            _adapter_descriptor(adapter_id, adapter)
            for adapter_id, adapter in sorted(self._adapters.items())
        )

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        return self._available_plugin_capability(tenant_id, name) is not None

    def capability_policy_parts(self, tenant_id: UUID, name: str) -> tuple[str, str, str] | None:
        target = self._available_plugin_capability(tenant_id, name)
        if target is None:
            return None
        plugin, capability = target
        return _plugin_capability_policy_parts(plugin, capability)

    def sandbox_profile(self, tenant_id: UUID, name: str) -> str | None:
        target = self._available_plugin_capability(tenant_id, name)
        if target is None:
            return None
        _plugin, capability = target
        return capability.sandbox_profile

    def capability_policy_rules(self, tenant_id: UUID) -> tuple[CapabilityRule, ...]:
        plugins = self._plugins_for_tenant(tenant_id)
        if plugins is None:
            return ()
        rules: list[CapabilityRule] = []
        for plugin in plugins:
            if not _plugin_is_running(
                plugin,
                self._adapters,
                dependency_policy=self._dependency_policy,
            ):
                continue
            for capability in plugin.capabilities:
                effect = _plugin_policy_effect(capability.policy_effect)
                if effect is None:
                    continue
                parts = _plugin_capability_policy_parts(plugin, capability)
                if parts is None:
                    continue
                policy_capability, policy_operation, resource_prefix = parts
                for role in _PLUGIN_POLICY_ROLES:
                    rules.append(
                        CapabilityRule(
                            tenant_id=tenant_id,
                            role=role,
                            agent_id=None,
                            capability=policy_capability,
                            operation=policy_operation,
                            resource_prefix=resource_prefix,
                            effect=effect,
                        )
                    )
        return tuple(rules)

    async def invoke(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        await self.ensure_tenant_loaded(tenant_id)
        target = self._available_plugin_capability(tenant_id, name)
        if target is None:
            raise RuntimeCapabilityError("Plugin tool unavailable")
        plugin, capability = target
        context = PluginInvocationContext(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            actor=actor,
            idempotency_key=idempotency_key,
        )
        try:
            adapter = self._adapters.get(capability.adapter)
            if adapter is None:
                raise RuntimeCapabilityError("Plugin backend unavailable")
            descriptor = _adapter_descriptor(capability.adapter, adapter)
            _ensure_supported_plugin_sandbox_profile(
                capability.sandbox_profile,
                descriptor=descriptor,
            )
            argument_validator = _plugin_schema_validator(
                schema=_adapter_argument_schema(descriptor),
                invalid_schema_message="Plugin adapter argument schema is invalid",
            )
            input_validator = _plugin_schema_validator(
                schema=capability.input_schema,
                invalid_schema_message="Plugin input schema is invalid",
            )
            output_validator = _plugin_schema_validator(
                schema=capability.output_schema,
                invalid_schema_message="Plugin output schema is invalid",
            )
            _validate_plugin_payload(
                payload=arguments,
                validator=argument_validator,
                validation_message="Plugin arguments do not match adapter schema",
                include_location=True,
            )
            _validate_plugin_payload(
                payload=arguments,
                validator=input_validator,
                validation_message="Plugin arguments do not match input schema",
                include_location=True,
            )
            result = await adapter.invoke(
                plugin=plugin,
                capability=capability,
                arguments=arguments,
                context=context,
            )
            _validate_plugin_payload(
                payload=result,
                validator=output_validator,
                validation_message="Plugin result does not match output schema",
                include_location=False,
            )
        except Exception:
            await self._record_invocation_audit(
                plugin,
                capability,
                context,
                action="plugin.invoke.failed",
            )
            raise
        await self._record_invocation_audit(
            plugin,
            capability,
            context,
            action="plugin.invoke.succeeded",
        )
        return result

    def _plugins_for_tenant(self, tenant_id: UUID) -> tuple[PluginResourceResponse, ...] | None:
        return self._plugins_by_tenant.get(tenant_id)

    def _available_plugin_capability(
        self,
        tenant_id: UUID,
        name: str,
    ) -> tuple[PluginResourceResponse, PluginCapabilityRequest] | None:
        plugins = self._plugins_for_tenant(tenant_id)
        if plugins is None:
            return None
        for plugin in plugins:
            if not _plugin_is_running(
                plugin,
                self._adapters,
                dependency_policy=self._dependency_policy,
            ):
                continue
            for capability in plugin.capabilities:
                if capability.id == name or name in capability.aliases:
                    return plugin, capability
        return None

    async def _record_invocation_audit(
        self,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        context: PluginInvocationContext,
        *,
        action: str,
    ) -> None:
        recorder = getattr(self._admin_service, "record_audit_event", None)
        if not callable(recorder):
            return
        try:
            await cast(PluginAuditRecorder, self._admin_service).record_audit_event(
                actor=context.actor,
                action=action,
                resource=f"plugin:{plugin.id}:{capability.id}",
                tenant_id=context.tenant_id,
                details={
                    "plugin_id": plugin.id,
                    "capability_id": capability.id,
                    "adapter": capability.adapter,
                    "permission_class": capability.permission_class,
                    "sandbox_profile": capability.sandbox_profile,
                    "replay_safe": capability.replay_safe is True,
                    "run_id": str(context.run_id),
                    "user_id": str(context.user_id),
                    "idempotency_key": context.idempotency_key,
                },
            )
        except Exception:  # noqa: BLE001 - plugin execution must not leak audit backend failures.
            return


async def build_runtime_plugin_service(
    *,
    tenant_id: UUID,
    admin_service: PluginConfigService,
    adapters: Mapping[str, PluginAdapter] | None = None,
    dependency_policy: PluginPackageDependencyPolicy | None = None,
    cache_ttl_seconds: float = 60.0,
    monotonic: Callable[[], float] | None = None,
) -> RuntimePluginService:
    service = RuntimePluginService(
        tenant_id=tenant_id,
        admin_service=admin_service,
        adapters=adapters,
        dependency_policy=dependency_policy,
        cache_ttl_seconds=cache_ttl_seconds,
        monotonic=monotonic,
    )
    await service.start()
    return service


def _empty_manifest() -> Mapping[str, JsonValue]:
    return {
        "schema_version": 1,
        "capabilities": (),
    }


def _plugin_is_running(
    plugin: PluginResourceResponse,
    adapters: Mapping[str, PluginAdapter] | None = None,
    dependency_policy: PluginPackageDependencyPolicy | None = None,
) -> bool:
    return (
        plugin.enabled is True
        and plugin.status == "running"
        and plugin.health == "healthy"
        and not _plugin_package_blocks_runtime_activation(
            plugin,
            adapters or {},
            dependency_policy=dependency_policy,
        )
    )


def _plugin_package_blocks_runtime_activation(
    plugin: PluginResourceResponse,
    adapters: Mapping[str, PluginAdapter],
    dependency_policy: PluginPackageDependencyPolicy | None = None,
) -> bool:
    return (
        _runtime_package_activation_block_reason(
            plugin,
            adapters,
            dependency_policy=dependency_policy,
        )
        is not None
    )


def _plugins_with_runtime_activation(
    plugins: tuple[PluginResourceResponse, ...],
    adapters: Mapping[str, PluginAdapter],
    dependency_policy: PluginPackageDependencyPolicy | None = None,
) -> tuple[PluginResourceResponse, ...]:
    return tuple(
        _plugin_with_runtime_activation(
            plugin,
            adapters,
            dependency_policy=dependency_policy,
        )
        for plugin in plugins
    )


def _plugin_cache_has_expired_package_signature_trust(
    plugins: tuple[PluginResourceResponse, ...] | None,
) -> bool:
    if not plugins:
        return False
    now = datetime.now(UTC)
    for plugin in plugins:
        package = plugin.package_metadata
        if (
            package is not None
            and package.signature_trust_expires_at is not None
            and package.signature_trust_expires_at <= now
        ):
            return True
    return False


def _plugin_package_signature_trust_expired(plugin: PluginResourceResponse) -> bool:
    package = plugin.package_metadata
    return (
        package is not None
        and package.signature_verification == "verified"
        and package.signature_trust_expires_at is not None
        and package.signature_trust_expires_at <= datetime.now(UTC)
    )


def _plugin_with_expired_package_signature_trust(
    plugin: PluginResourceResponse,
) -> PluginResourceResponse:
    if not _plugin_package_signature_trust_expired(plugin):
        return plugin
    package = plugin.package_metadata
    if package is None:
        return plugin
    effective_package = type(package).model_validate(
        {
            **package.model_dump(mode="json"),
            "signature_verification": "untrusted_key",
            "signature_trust_expires_at": None,
        }
    )
    return plugin.model_copy(update={"package_metadata": effective_package})


def _plugin_with_runtime_activation(
    plugin: PluginResourceResponse,
    adapters: Mapping[str, PluginAdapter],
    dependency_policy: PluginPackageDependencyPolicy | None = None,
) -> PluginResourceResponse:
    plugin = _plugin_with_expired_package_signature_trust(plugin)
    package = plugin.package_metadata
    reason = _runtime_package_activation_block_reason(
        plugin,
        adapters,
        dependency_policy=dependency_policy,
    )
    if (
        reason is None
        or package is None
        or package.kind != "adapter_package"
        or package.activation_state != "eligible"
    ):
        return plugin
    return plugin.model_copy(
        update={
            "package_metadata": package.model_copy(
                update={
                    "activation_state": "blocked_unsupported_runtime",
                    "activation_reason": reason,
                }
            ),
        }
    )


def _runtime_package_activation_block_reason(
    plugin: PluginResourceResponse,
    adapters: Mapping[str, PluginAdapter],
    dependency_policy: PluginPackageDependencyPolicy | None = None,
) -> str | None:
    package = plugin.package_metadata
    if package is None or package.kind != "adapter_package":
        return None
    if _plugin_package_signature_trust_expired(plugin):
        return "package signature key is not trusted for this tenant"
    if package.activation_state != "eligible":
        return package.activation_reason or "plugin package is not eligible for activation"
    if package.install_mode != "runtime_registered":
        return "plugin package install mode is not supported for activation"
    if package.signature is None or package.signature_verification != "verified":
        return "adapter packages must include a trusted verified signature before activation"
    if package.approval_state != "approved":
        return "adapter package requires plugin approval before activation"
    if package.sdk_api_version not in SUPPORTED_PLUGIN_PACKAGE_SDK_API_VERSIONS:
        return "plugin package SDK API version is not supported for activation"
    if package.runtime not in SUPPORTED_RUNTIME_REGISTERED_PACKAGE_RUNTIMES:
        return "plugin package runtime is not supported for activation"
    if package.isolation not in SUPPORTED_RUNTIME_REGISTERED_PACKAGE_ISOLATIONS:
        return "plugin package isolation is not supported for activation"
    if package.dependencies:
        try:
            _plugin_package_dependency_root(
                package,
                dependency_policy=dependency_policy,
            )
        except RuntimeCapabilityError:
            return PLUGIN_PACKAGE_DEPENDENCIES_UNSUPPORTED_REASON
    adapter = adapters.get(package.adapter_id or "")
    if adapter is None:
        return "runtime-registered adapter package requires a registered adapter"
    descriptor = _adapter_descriptor(package.adapter_id or "", adapter)
    if descriptor.get("id") != package.adapter_id:
        return "runtime-registered adapter package descriptor id does not match package adapter_id"
    try:
        _ensure_supported_plugin_sandbox_profile(package.isolation, descriptor=descriptor)
    except RuntimeCapabilityError:
        return "runtime-registered adapter package isolation is not supported by adapter"
    if not plugin.capabilities:
        return "runtime-registered adapter packages must declare at least one capability"
    for capability in plugin.capabilities:
        if capability.adapter != package.adapter_id:
            return (
                "runtime-registered adapter packages must route capabilities "
                "through package adapter_id"
            )
        if capability.sandbox_profile != package.isolation:
            return "runtime-registered adapter package capabilities must use package isolation"
    return None


def _plugin_package_execution_target(
    plugin: PluginResourceResponse,
    *,
    tenant_id: UUID,
    package_store_dir: Path,
    dependency_policy: PluginPackageDependencyPolicy | None = None,
) -> PluginPackageExecutionTarget:
    package = plugin.package_metadata
    if package is None or package.kind != "adapter_package":
        raise RuntimeCapabilityError("Plugin package artifact is unavailable")
    dependency_root = _plugin_package_dependency_root(
        package,
        dependency_policy=dependency_policy,
    )
    if (
        package.activation_state != "eligible"
        or package.install_mode != "runtime_registered"
        or package.signature_verification != "verified"
        or package.approval_state != "approved"
        or package.sdk_api_version not in SUPPORTED_PLUGIN_PACKAGE_SDK_API_VERSIONS
        or package.runtime not in SUPPORTED_RUNTIME_REGISTERED_PACKAGE_RUNTIMES
        or package.isolation not in SUPPORTED_RUNTIME_REGISTERED_PACKAGE_ISOLATIONS
    ):
        raise RuntimeCapabilityError("Plugin package is not eligible for executable activation")
    artifact = package.artifact
    if artifact is None:
        raise RuntimeCapabilityError("Plugin package artifact is unavailable")
    if plugin.content_sha256 is None or artifact.content_sha256 != plugin.content_sha256:
        raise RuntimeCapabilityError("Plugin package artifact digest does not match plugin content")
    expected_storage_key = f"{tenant_id}/{plugin.id}/{artifact.content_sha256}"
    if artifact.storage_key != expected_storage_key:
        raise RuntimeCapabilityError("Plugin package artifact storage key is invalid")
    if package.entrypoint is None:
        raise RuntimeCapabilityError("Plugin package entrypoint is unavailable")
    root = package_store_dir.joinpath(*artifact.storage_key.split("/"))
    _ensure_runtime_path_inside(package_store_dir, root)
    if not root.is_dir():
        raise RuntimeCapabilityError("Plugin package artifact is unavailable")
    entrypoint_path = PurePosixPath(package.entrypoint)
    entrypoint_parts = entrypoint_path.parts
    if (
        not entrypoint_parts
        or any(part in {"", ".", ".."} for part in entrypoint_parts)
        or entrypoint_path.is_absolute()
    ):
        raise RuntimeCapabilityError("Plugin package entrypoint is unavailable")
    entrypoint = root.joinpath(*entrypoint_parts)
    _ensure_runtime_path_inside(root, entrypoint)
    if not entrypoint.is_file():
        raise RuntimeCapabilityError("Plugin package entrypoint is unavailable")
    return PluginPackageExecutionTarget(
        root=root,
        entrypoint=entrypoint,
        dependency_root=dependency_root,
    )


def _plugin_package_dependency_root(
    package: PluginPackageMetadata,
    *,
    dependency_policy: PluginPackageDependencyPolicy | None,
) -> Path | None:
    if not package.dependencies:
        return None
    if dependency_policy is None:
        raise RuntimeCapabilityError(PLUGIN_PACKAGE_DEPENDENCIES_UNSUPPORTED_REASON)
    dependency_lock = plugin_package_dependency_lock(
        cast(Sequence[PluginPackageDependencyLike], package.dependencies)
    )
    if dependency_lock is None or dependency_policy.cache_dir is None:
        raise RuntimeCapabilityError(PLUGIN_PACKAGE_DEPENDENCIES_UNSUPPORTED_REASON)
    install_policy, cache_status, allowlist_status = dependency_policy.evaluate(
        dependency_lock
    )
    if (
        install_policy != "offline_cache"
        or cache_status != "present"
        or allowlist_status != "allowed"
    ):
        raise RuntimeCapabilityError(PLUGIN_PACKAGE_DEPENDENCIES_UNSUPPORTED_REASON)
    dependency_root = dependency_policy.cache_dir / dependency_lock.sha256
    _ensure_runtime_path_inside(dependency_policy.cache_dir, dependency_root)
    return dependency_root


def _ensure_runtime_path_inside(root: Path, path: Path) -> None:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    try:
        path_resolved.relative_to(root_resolved)
    except ValueError:
        raise RuntimeCapabilityError("Plugin package path is invalid") from None


def _permission_class_parts(permission_class: str) -> tuple[str, str] | None:
    separator = "." if "." in permission_class else ":"
    parts = permission_class.split(separator, 1)
    if len(parts) != 2 or not all(parts):
        return None
    capability, operation = parts
    return capability, operation


def _plugin_policy_resource(
    plugin: PluginResourceResponse,
    capability: PluginCapabilityRequest,
) -> str:
    return f"plugin/{plugin.id}/{capability.id.replace('.', '/')}"


def _generic_plugin_policy_resource(capability: PluginCapabilityRequest) -> str:
    return f"plugin/{capability.id.replace('.', '/')}"


def _plugin_capability_policy_parts(
    plugin: PluginResourceResponse,
    capability: PluginCapabilityRequest,
) -> tuple[str, str, str] | None:
    if capability.permission_class == "plugin.use":
        return "plugin", "use", _generic_plugin_policy_resource(capability)
    permission = _permission_class_parts(capability.permission_class)
    if permission is None:
        return None
    policy_capability, policy_operation = permission
    return policy_capability, policy_operation, _plugin_policy_resource(plugin, capability)


def _plugin_policy_effect(value: str) -> PolicyEffect | None:
    if value == "inherit":
        return None
    try:
        return PolicyEffect(value)
    except ValueError:
        return PolicyEffect.DENY


def _ensure_supported_plugin_sandbox_profile(
    sandbox_profile: str,
    *,
    descriptor: Mapping[str, JsonValue],
) -> None:
    if sandbox_profile in SUPPORTED_PLUGIN_SANDBOX_PROFILES:
        return
    declared_profiles = (
        adapter_declared_sandbox_profiles(descriptor)
        & ADAPTER_DECLARABLE_PLUGIN_SANDBOX_PROFILES
    )
    if sandbox_profile in declared_profiles:
        return
    raise RuntimeCapabilityError("Plugin sandbox profile unsupported")


_PLUGIN_POLICY_ROLES = (Role.SUPER_ADMIN, Role.ADMIN, Role.OPERATOR)


def _default_plugin_adapters(admin_service: object) -> dict[str, PluginAdapter]:
    return {
        "http_json": HttpJsonPluginAdapter(
            secret_resolver=_secret_resolver(admin_service),
        )
    }


def _adapter_descriptor(adapter_id: str, adapter: PluginAdapter) -> Mapping[str, JsonValue]:
    descriptor = getattr(adapter, "descriptor", None)
    if callable(descriptor):
        try:
            payload = descriptor()
        except Exception:  # noqa: BLE001 - descriptor metadata must fail closed.
            payload = None
        if isinstance(payload, Mapping):
            return adapter_descriptor_with_contract(cast(Mapping[str, JsonValue], payload))
    return adapter_descriptor_with_contract({
        "id": adapter_id,
        "name": adapter_id,
        "description": None,
        "resource_schema": {
            "type": "object",
            "additionalProperties": True,
        },
        "capability_schema": {
            "type": "object",
            "additionalProperties": True,
        },
        "argument_schema": {
            "type": "object",
            "additionalProperties": True,
        },
    })


def _adapter_argument_schema(
    descriptor: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue] | None:
    argument_schema = descriptor.get("argument_schema")
    if isinstance(argument_schema, Mapping):
        return argument_schema
    return None


def _plugin_endpoint_url(plugin: PluginResourceResponse) -> str:
    url = plugin.endpoint_url
    if not isinstance(url, str) or not url.strip() or url != url.strip():
        raise RuntimeCapabilityError("Plugin endpoint unavailable")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.hostname is None:
        raise RuntimeCapabilityError("Plugin endpoint unavailable")
    return url


def _endpoint_domain_allowed(url: str, domain_allowlist: Sequence[str]) -> bool:
    host = urlsplit(url).hostname
    if host is None:
        return False
    normalized_host = host.lower()
    allowed = {
        domain.lower().strip()
        for domain in domain_allowlist
        if isinstance(domain, str) and domain.strip()
    }
    return normalized_host in allowed


def _package_adapter_descriptor(adapter_id: str) -> Mapping[str, JsonValue]:
    return {
        "id": adapter_id,
        "name": "Package Adapter",
        "description": "Invokes a verified local plugin package through the package runner.",
        "resource_schema": {"type": "object", "additionalProperties": True},
        "capability_schema": {
            "type": "object",
            "properties": {
                "sandbox_profile": {"type": "string", "enum": ("local_process",)}
            },
            "additionalProperties": True,
        },
        "argument_schema": {"type": "object", "additionalProperties": True},
    }


def _plugin_package_runner_request(
    *,
    plugin: PluginResourceResponse,
    capability: PluginCapabilityRequest,
    arguments: Mapping[str, JsonValue],
    context: PluginInvocationContext,
) -> Mapping[str, JsonValue]:
    payload: Mapping[str, JsonValue] = {
        "schema_version": 1,
        "plugin_id": plugin.id,
        "capability_id": capability.id,
        "arguments": arguments,
        "resource_config": plugin.resource_config,
        "capability_config": capability.capability_config,
        "context": {
            "tenant_id": str(context.tenant_id),
            "user_id": str(context.user_id),
            "run_id": str(context.run_id),
            "actor": context.actor,
            "idempotency_key": context.idempotency_key,
        },
    }
    return cast(Mapping[str, JsonValue], _mutable_json(cast(JsonValue, payload)))


def _minimal_python_subprocess_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    allowed_keys = {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "TEMP",
        "TMP",
        "WINDIR",
    }
    source = os.environ if environment is None else environment
    environment = {
        key: value
        for key, value in source.items()
        if key in allowed_keys and isinstance(value, str) and value
    }
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONUTF8"] = "1"
    return environment


def build_plugin_package_subprocess_adapters(
    *,
    enabled: bool,
    adapter_ids: Sequence[str],
    package_store_dir: Path,
    isolation_backend: Literal["disabled", "bubblewrap"] = "disabled",
    bubblewrap_executable: Path | None = None,
    python_executable: str | None = None,
    timeout_seconds: float = 10.0,
    max_stdin_bytes: int = 262_144,
    max_stdout_bytes: int = 262_144,
    dependency_policy: PluginPackageDependencyPolicy | None = None,
) -> dict[str, PluginAdapter]:
    if any(adapter_id == "http_json" for adapter_id in adapter_ids):
        raise ValueError("plugin package subprocess adapter id is reserved")
    if (
        _plugin_package_subprocess_registration_status(
            enabled=enabled,
            adapter_ids=adapter_ids,
            isolation_backend=isolation_backend,
            bubblewrap_executable=bubblewrap_executable,
        )
        != "ready"
    ):
        return {}
    assert bubblewrap_executable is not None
    runner = PythonSubprocessPluginPackageRunner(
        python_executable=python_executable,
        process_launcher=BubblewrapPluginPackageProcessLauncher(
            bubblewrap_executable=bubblewrap_executable
        ),
        timeout_seconds=timeout_seconds,
        max_stdin_bytes=max_stdin_bytes,
        max_stdout_bytes=max_stdout_bytes,
    )
    return {
        adapter_id: PluginPackageAdapter(
            adapter_id=adapter_id,
            package_store_dir=package_store_dir,
            runner=runner,
            dependency_policy=dependency_policy,
        )
        for adapter_id in adapter_ids
    }


def _plugin_package_subprocess_registration_status(
    *,
    enabled: bool,
    adapter_ids: Sequence[str],
    isolation_backend: Literal["disabled", "bubblewrap"],
    bubblewrap_executable: Path | None,
) -> str:
    if not enabled:
        return "disabled"
    if not adapter_ids:
        return "no_adapter_ids"
    if isolation_backend != "bubblewrap":
        return "unsupported_isolation_backend"
    if bubblewrap_executable is None:
        return "missing_isolation_launcher"
    if not bubblewrap_executable.is_absolute():
        return "launcher_path_not_absolute"
    if not bubblewrap_executable.is_file():
        return "launcher_not_found"
    if os.name != "posix":
        return "unsupported_isolation_backend"
    if os.name == "posix" and not os.access(bubblewrap_executable, os.X_OK):
        return "launcher_not_executable"
    return "ready"


def _plugin_schema_validator(
    *,
    schema: Mapping[str, JsonValue] | None,
    invalid_schema_message: str,
) -> Validator | None:
    failure = False
    try:
        return plugin_schema_validator(
            schema=schema,
            invalid_schema_message=invalid_schema_message,
        )
    except PluginSchemaError:
        failure = True
    if failure:
        raise RuntimeCapabilityError(invalid_schema_message)
    return None


def _validate_plugin_payload(
    *,
    payload: Mapping[str, JsonValue],
    validator: Validator | None,
    validation_message: str,
    include_location: bool,
) -> None:
    if validator is None:
        return
    instance_payload = cast(Any, _mutable_json(cast(JsonValue, payload)))
    failure: str | None = None
    try:
        validator.validate(instance_payload)
    except ValidationError as error:
        location = _validation_error_location(error) if include_location else ""
        reason = _validation_error_reason(error)
        failure = f"{validation_message}{location}: {reason}"
    if failure is not None:
        raise RuntimeCapabilityError(failure)


def _validation_error_location(error: ValidationError) -> str:
    path = ".".join(str(item) for item in error.absolute_path)
    if not path:
        return ""
    return f" at {path}"


def _validation_error_reason(error: ValidationError) -> str:
    match error.validator:
        case "additionalProperties":
            return "unexpected field"
        case "enum":
            return "unsupported value"
        case "format":
            return "invalid format"
        case "maxItems" | "minItems":
            return "invalid item count"
        case "maxLength" | "minLength":
            return "invalid string length"
        case "maximum" | "minimum" | "exclusiveMaximum" | "exclusiveMinimum":
            return "number is outside the allowed range"
        case "pattern":
            return "invalid string pattern"
        case "required":
            return "required field is missing"
        case "type":
            return "invalid type"
        case _:
            return "validation failed"


async def _httpx_post_json(
    url: str,
    payload: Mapping[str, JsonValue],
    timeout_seconds: float,
    headers: Mapping[str, str],
) -> Mapping[str, JsonValue]:
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, Mapping):
        raise RuntimeCapabilityError("Plugin result is invalid")
    return cast(Mapping[str, JsonValue], data)


def _secret_resolver(admin_service: object) -> PluginSecretResolver | None:
    resolver = getattr(admin_service, "resolve_secret_value", None)
    if not callable(resolver):
        return None
    return cast(PluginSecretResolver, resolver)


__all__ = [
    "HttpJsonPluginAdapter",
    "HttpJsonPost",
    "PluginAdapter",
    "PluginConfigService",
    "PluginInvocationContext",
    "PluginPackageAdapter",
    "PluginPackageExecutionTarget",
    "PluginPackageRunner",
    "PluginSecretResolver",
    "PythonSubprocessPluginPackageRunner",
    "RuntimePluginService",
    "_plugin_package_execution_target",
    "build_plugin_package_subprocess_adapters",
    "build_runtime_plugin_service",
]
