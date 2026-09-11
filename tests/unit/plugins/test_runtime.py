from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import UUID

import pytest

from agent_hub.api.routers.admin import (
    PLUGIN_PACKAGE_DEPENDENCIES_UNSUPPORTED_REASON,
    PluginCapabilityRequest,
    PluginPackageDependency,
    PluginPackageMetadata,
    PluginResourceRequest,
    PluginResourceResponse,
)
from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.capabilities.types import PolicyEffect
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.plugins.contracts import (
    adapter_descriptor_with_contract,
    http_json_adapter_descriptor,
)
from agent_hub.plugins.dependency_policy import (
    PluginPackageDependencyPolicy,
    plugin_package_dependency_cache_signature_payload_sha256,
    plugin_package_dependency_lock,
)
from agent_hub.plugins.runtime import (
    BubblewrapPluginPackageProcessLauncher,
    HttpJsonPluginAdapter,
    PluginInvocationContext,
    PluginPackageAdapter,
    PluginPackageExecutionTarget,
    PythonSubprocessPluginPackageRunner,
    RuntimePluginService,
    _plugin_package_execution_target,
    _plugin_package_subprocess_registration_status,
    build_plugin_package_subprocess_adapters,
    build_runtime_plugin_service,
)
from agent_hub.runtime.contracts import JsonValue

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


class FakeAdminService:
    def __init__(self, plugins: tuple[PluginResourceResponse, ...]) -> None:
        self.plugins = plugins
        self.calls = 0
        self.audit_events: list[dict[str, object]] = []
        self.audit_tenant_ids: list[UUID | None] = []

    async def list_plugins(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[PluginResourceResponse, ...]:
        del tenant_id
        self.calls += 1
        return self.plugins

    async def record_audit_event(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        details: dict[str, object] | None = None,
        tenant_id: UUID | None = None,
    ) -> dict[str, object]:
        self.audit_tenant_ids.append(tenant_id)
        event: dict[str, object] = {
            "actor": actor,
            "action": action,
            "resource": resource,
            "details": {} if details is None else dict(details),
        }
        self.audit_events.append(event)
        return event


class AllowingPolicyGateway:
    def __init__(self) -> None:
        self.requests = 0

    async def invoke(self, request: Any, *, role: Role) -> CapabilityResult:
        del role
        self.requests += 1
        return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)


class UnavailableRuntimeCapabilityGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        del tenant_id, name
        self.calls.append("available")
        return False

    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        del tenant_id, run_id, actor, name, arguments, idempotency_key
        self.calls.append("execute")
        return {}


class TenantMappedPluginAdminService(FakeAdminService):
    def __init__(
        self,
        plugins_by_tenant: dict[UUID, tuple[PluginResourceResponse, ...]],
    ) -> None:
        super().__init__(())
        self.plugins_by_tenant = plugins_by_tenant
        self.tenant_ids: list[UUID] = []

    async def list_plugins(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[PluginResourceResponse, ...]:
        assert tenant_id is not None
        self.tenant_ids.append(tenant_id)
        return self.plugins_by_tenant.get(tenant_id, ())


class BlockingTenantMappedPluginAdminService(TenantMappedPluginAdminService):
    def __init__(
        self,
        plugins_by_tenant: dict[UUID, tuple[PluginResourceResponse, ...]],
    ) -> None:
        super().__init__(plugins_by_tenant)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def list_plugins(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[PluginResourceResponse, ...]:
        assert tenant_id is not None
        self.entered.set()
        await self.release.wait()
        return await super().list_plugins(tenant_id=tenant_id)


class FailingOnceTenantMappedPluginAdminService(TenantMappedPluginAdminService):
    def __init__(
        self,
        plugins_by_tenant: dict[UUID, tuple[PluginResourceResponse, ...]],
    ) -> None:
        super().__init__(plugins_by_tenant)
        self.failures_remaining = 1

    async def list_plugins(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[PluginResourceResponse, ...]:
        assert tenant_id is not None
        self.tenant_ids.append(tenant_id)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("temporary plugin reload failure")
        return self.plugins_by_tenant.get(tenant_id, ())


@dataclass
class RecordingPluginAdapter:
    calls: list[tuple[str, str, Mapping[str, JsonValue], PluginInvocationContext]]
    result: Mapping[str, JsonValue] | None = None
    descriptor_payload: Mapping[str, JsonValue] | None = None

    async def invoke(
        self,
        *,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        arguments: Mapping[str, JsonValue],
        context: PluginInvocationContext,
    ) -> Mapping[str, JsonValue]:
        self.calls.append((plugin.id, capability.id, arguments, context))
        if self.result is not None:
            return self.result
        return {
            "ok": True,
            "plugin_id": plugin.id,
            "capability_id": capability.id,
        }

    def descriptor(self) -> Mapping[str, JsonValue]:
        if self.descriptor_payload is not None:
            return self.descriptor_payload
        return {
            "id": "plugin_runtime",
            "name": "Plugin Runtime",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {"type": "object", "additionalProperties": True},
            "argument_schema": {"type": "object", "additionalProperties": True},
        }


@dataclass
class RecordingPluginPackageRunner:
    calls: list[
        tuple[
            PluginPackageExecutionTarget,
            str,
            str,
            Mapping[str, JsonValue],
            PluginInvocationContext,
        ]
    ]
    result: object | None = None
    failure: Exception | None = None

    async def invoke(
        self,
        *,
        target: PluginPackageExecutionTarget,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        arguments: Mapping[str, JsonValue],
        context: PluginInvocationContext,
    ) -> Mapping[str, JsonValue]:
        if self.failure is not None:
            raise self.failure
        self.calls.append((target, plugin.id, capability.id, arguments, context))
        if self.result is not None:
            return cast(Mapping[str, JsonValue], self.result)
        return {
            "ok": True,
            "entrypoint": str(target.entrypoint),
        }


def _argv_contains_ordered_pair(
    argv: tuple[str, ...],
    flag: str,
    source: Path,
    destination: Path,
) -> bool:
    expected = (flag, str(source), str(destination))
    return _argv_contains_ordered_args(argv, expected)


def _argv_contains_ordered_args(
    argv: tuple[str, ...],
    expected: tuple[str, ...],
) -> bool:
    size = len(expected)
    return any(
        argv[index : index + size] == expected for index in range(len(argv) - size + 1)
    )


def _argv_index(argv: tuple[str, ...], expected: tuple[str, ...]) -> int:
    size = len(expected)
    for index in range(len(argv) - size + 1):
        if argv[index : index + size] == expected:
            return index
    raise AssertionError(f"missing argv sequence: {expected!r}")


def plugin(
    plugin_id: str,
    *,
    enabled: bool = True,
    status: str = "running",
    health: str = "healthy",
    capability_id: str = "calendar.create_event",
    adapter: str = "plugin_runtime",
    endpoint_url: str | None = None,
    domain_allowlist: tuple[str, ...] = (),
    timeout_seconds: float = 10,
    credential_ref: str | None = None,
    credential_header: str = "X-Plugin-Credential",
    credential_scheme: str = "Bearer",
    permission_class: str = "calendar.write",
    sandbox_profile: str = "remote_connector",
    policy_effect: Literal["inherit", "allow", "require_approval", "deny"] = "inherit",
    replay_safe: bool = False,
    resource_config: Mapping[str, JsonValue] | None = None,
    capability_config: Mapping[str, JsonValue] | None = None,
    input_schema: Mapping[str, JsonValue] | None = None,
    output_schema: Mapping[str, JsonValue] | None = None,
    package_metadata: PluginPackageMetadata | None = None,
    content_sha256: str | None = None,
) -> PluginResourceResponse:
    return PluginResourceResponse(
        **PluginResourceRequest(
            id=plugin_id,
            name=plugin_id,
            enabled=enabled,
            endpoint_url=endpoint_url,
            domain_allowlist=list(domain_allowlist),
            timeout_seconds=timeout_seconds,
            credential_ref=credential_ref,
            credential_header=credential_header,
            credential_scheme=credential_scheme,
            resource_config=dict(resource_config) if resource_config is not None else {},
            capabilities=[
                PluginCapabilityRequest(
                    id=capability_id,
                    adapter=adapter,
                    permission_class=permission_class,
                    sandbox_profile=sandbox_profile,
                    policy_effect=policy_effect,
                    replay_safe=replay_safe,
                    aliases=["calendar_create"],
                    capability_config=dict(capability_config)
                    if capability_config is not None
                    else {},
                    input_schema=dict(input_schema) if input_schema is not None else None,
                    output_schema=dict(output_schema) if output_schema is not None else None,
                )
            ],
        ).model_dump(),
        status=status,
        health=health,
        last_error_type=None,
        package_metadata=package_metadata,
        content_sha256=content_sha256,
    )


async def test_runtime_plugin_service_exposes_running_plugins_as_manifest() -> None:
    admin_service = FakeAdminService(
        (
            plugin("calendar"),
            plugin("stopped", status="stopped", health="stopped", capability_id="stopped.run"),
        )
    )

    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
    )

    assert service.is_available(TENANT_ID, "calendar.create_event") is True
    assert service.is_available(TENANT_ID, "stopped.run") is False
    manifest = service.capability_manifest_source().manifests_for_tenant(TENANT_ID)
    capability_items = cast(tuple[Mapping[str, object], ...], manifest["capabilities"])
    capabilities = {
        str(item["id"]): item
        for item in capability_items
    }
    assert capabilities["calendar.create_event"]["available"] is True
    assert capabilities["stopped.run"]["available"] is False
    assert admin_service.calls == 1


async def test_runtime_plugin_service_blocks_scan_only_adapter_packages() -> None:
    package_metadata = PluginPackageMetadata.model_validate(
        {
            "kind": "adapter_package",
            "package_version": "1.2.3",
            "adapter_id": "calendar_python",
            "sdk_api_version": "1.0",
            "signature": {
                "algorithm": "ed25519",
                "key_id": "calendar-prod",
                "value": "A" * 86,
            },
            "signature_verification": "verified",
            "runtime": "python",
            "entrypoint": "adapter/main.py",
            "isolation": "local_process",
            "install_mode": "scan_only",
        }
    )
    adapter = RecordingPluginAdapter(calls=[])
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (plugin("calendar", package_metadata=package_metadata),)
        ),
        adapters={"plugin_runtime": adapter},
    )

    manifest = service.capability_manifest_source().manifests_for_tenant(TENANT_ID)
    capability_items = cast(tuple[Mapping[str, object], ...], manifest["capabilities"])
    capabilities = {str(item["id"]): item for item in capability_items}

    assert service.is_available(TENANT_ID, "calendar.create_event") is False
    assert capabilities["calendar.create_event"]["available"] is False
    with pytest.raises(RuntimeCapabilityError, match="Plugin tool unavailable"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="tester",
            name="calendar.create_event",
            arguments={},
            idempotency_key="invoke-1",
        )
    assert adapter.calls == []


async def test_runtime_plugin_service_invokes_runtime_registered_adapter_package() -> None:
    package_metadata = PluginPackageMetadata.model_validate(
        {
            "kind": "adapter_package",
            "package_version": "1.2.3",
            "adapter_id": "calendar_python",
            "sdk_api_version": "1.0",
            "signature": {
                "algorithm": "ed25519",
                "key_id": "calendar-prod",
                "value": "A" * 86,
            },
            "signature_verification": "verified",
            "approval_state": "approved",
            "runtime": "python",
            "entrypoint": "adapter/main.py",
            "isolation": "local_process",
            "install_mode": "runtime_registered",
        }
    )
    adapter = RecordingPluginAdapter(
        calls=[],
        descriptor_payload={
            "id": "calendar_python",
            "name": "Calendar Python",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {
                "type": "object",
                "properties": {
                    "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                },
                "additionalProperties": True,
            },
            "argument_schema": {"type": "object", "additionalProperties": True},
        },
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    adapter="calendar_python",
                    sandbox_profile="local_process",
                    package_metadata=package_metadata,
                ),
            )
        ),
        adapters={"calendar_python": adapter},
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="tester",
        name="calendar.create_event",
        arguments={"title": "review"},
        idempotency_key="invoke-1",
    )

    assert result["ok"] is True
    assert adapter.calls[0][0] == "calendar"
    assert adapter.calls[0][1] == "calendar.create_event"


def verified_package_with_artifact(
    *,
    content_sha256: str,
    storage_key: str,
    entrypoint: str = "adapter/main.py",
) -> PluginPackageMetadata:
    return PluginPackageMetadata.model_validate(
        {
            "kind": "adapter_package",
            "package_version": "1.2.3",
            "adapter_id": "calendar_python",
            "sdk_api_version": "1.0",
            "signature": {
                "algorithm": "ed25519",
                "key_id": "calendar-prod",
                "value": "A" * 86,
            },
            "signature_verification": "verified",
            "approval_state": "approved",
            "runtime": "python",
            "entrypoint": entrypoint,
            "isolation": "local_process",
            "install_mode": "runtime_registered",
            "artifact": {
                "storage_key": storage_key,
                "content_sha256": content_sha256,
                "file_count": 2,
                "total_size_bytes": 64,
                "stored_at": "2026-09-09T04:00:00Z",
                "quarantine_state": "stored",
            },
        }
    )


def write_dependency_cache_manifest(
    cache_root: Path,
    *,
    lock_hash: str,
    dependencies: list[dict[str, str]],
) -> Path:
    cache_entry = cache_root / lock_hash
    cache_entry.mkdir(parents=True)
    marker_path = cache_entry / "site-packages" / "dependency_cache_marker.py"
    marker_bytes = b"READY = True\n"
    marker_path.parent.mkdir(parents=True)
    marker_path.write_bytes(marker_bytes)
    artifact_entries: list[dict[str, object]] = []
    artifact_files: list[dict[str, object]] = []
    for dependency in dependencies:
        artifact_name = dependency["name"]
        artifact_version = dependency["version"]
        artifact_relative_path = f"artifacts/{artifact_name}-{artifact_version}.whl"
        artifact_path = cache_entry / "artifacts" / f"{artifact_name}-{artifact_version}.whl"
        artifact_bytes = f"{artifact_name}=={artifact_version}\n".encode()
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(artifact_bytes)
        artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
        artifact_entry = {
            "kind": dependency["kind"],
            "source": dependency["source"],
            "name": artifact_name,
            "version": artifact_version,
            "path": artifact_relative_path,
            "sha256": artifact_digest,
            "size_bytes": len(artifact_bytes),
        }
        artifact_entries.append(artifact_entry)
        artifact_files.append(
            {
                "path": artifact_relative_path,
                "sha256": artifact_digest,
                "size_bytes": len(artifact_bytes),
            }
        )
    files = [
        *artifact_files,
        {
            "path": "site-packages/dependency_cache_marker.py",
            "sha256": hashlib.sha256(marker_bytes).hexdigest(),
            "size_bytes": len(marker_bytes),
        },
    ]
    dependency_lock = plugin_package_dependency_lock(
        tuple(SimpleNamespace(**dependency) for dependency in dependencies)
    )
    assert dependency_lock is not None
    signature_sha256 = plugin_package_dependency_cache_signature_payload_sha256(
        dependency_lock,
        dependencies,
        files,
        artifact_entries,
    )
    assert signature_sha256 is not None
    (cache_entry / "dependency-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": lock_hash,
                "dependencies": dependencies,
                "artifacts": artifact_entries,
                "files": files,
                "cache_signature": {
                    "schema_version": 1,
                    "algorithm": "sha256",
                    "payload_sha256": signature_sha256,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return cache_entry


def test_plugin_package_execution_target_resolves_artifact_entrypoint(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    (artifact_root / "adapter").mkdir(parents=True)
    (artifact_root / "adapter" / "main.py").write_text("def invoke():\n    return {}\n")
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    )

    target = _plugin_package_execution_target(
        plugin(
            "calendar",
            adapter="calendar_python",
            sandbox_profile="in_process",
            package_metadata=package_metadata,
            content_sha256=content_sha256,
        ),
        tenant_id=TENANT_ID,
        package_store_dir=tmp_path,
    )

    assert target.root == artifact_root
    assert target.entrypoint == artifact_root / "adapter" / "main.py"


@pytest.mark.parametrize(
    ("content_sha256", "storage_key", "error"),
    [
        (
            "a" * 64,
            f"{OTHER_TENANT_ID}/calendar/{'a' * 64}",
            "Plugin package artifact storage key is invalid",
        ),
        (
            "b" * 64,
            f"{TENANT_ID}/calendar/{'a' * 64}",
            "Plugin package artifact digest does not match plugin content",
        ),
    ],
)
def test_plugin_package_execution_target_rejects_mismatched_artifact_metadata(
    tmp_path: Path,
    content_sha256: str,
    storage_key: str,
    error: str,
) -> None:
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=storage_key,
    )

    with pytest.raises(RuntimeCapabilityError, match=error):
        _plugin_package_execution_target(
            plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="in_process",
                package_metadata=package_metadata,
                content_sha256="a" * 64,
            ),
            tenant_id=TENANT_ID,
            package_store_dir=tmp_path,
        )


def test_plugin_package_execution_target_rejects_missing_entrypoint(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    artifact_root.mkdir(parents=True)
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin package entrypoint is unavailable"):
        _plugin_package_execution_target(
            plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="in_process",
                package_metadata=package_metadata,
                content_sha256=content_sha256,
            ),
            tenant_id=TENANT_ID,
            package_store_dir=tmp_path,
        )


def test_plugin_package_execution_target_rejects_missing_artifact_metadata(
    tmp_path: Path,
) -> None:
    package_metadata = verified_package_with_artifact(
        content_sha256="a" * 64,
        storage_key=f"{TENANT_ID}/calendar/{'a' * 64}",
    ).model_copy(update={"artifact": None})

    with pytest.raises(RuntimeCapabilityError, match="Plugin package artifact is unavailable"):
        _plugin_package_execution_target(
            plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="in_process",
                package_metadata=package_metadata,
                content_sha256="a" * 64,
            ),
            tenant_id=TENANT_ID,
            package_store_dir=tmp_path,
        )


def test_plugin_package_execution_target_rejects_package_dependencies_with_stable_reason(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    (artifact_root / "adapter").mkdir(parents=True)
    (artifact_root / "adapter" / "main.py").write_text("def invoke():\n    return {}\n")
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    ).model_copy(
        update={
            "dependencies": (
                PluginPackageDependency(name="requests", version="2.32.0"),
            )
        }
    )

    with pytest.raises(RuntimeCapabilityError) as exc_info:
        _plugin_package_execution_target(
            plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="local_process",
                package_metadata=package_metadata,
                content_sha256=content_sha256,
            ),
            tenant_id=TENANT_ID,
            package_store_dir=tmp_path,
        )
    assert str(exc_info.value) == PLUGIN_PACKAGE_DEPENDENCIES_UNSUPPORTED_REASON


def test_plugin_package_execution_target_allows_ready_offline_dependency_cache(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / "packages" / str(TENANT_ID) / "calendar" / content_sha256
    (artifact_root / "adapter").mkdir(parents=True)
    (artifact_root / "adapter" / "main.py").write_text("def invoke():\n    return {}\n")
    lock_hash = hashlib.sha256(b"python pypi requests==2.32.0\n").hexdigest()
    dependency_root = write_dependency_cache_manifest(
        tmp_path / "dependency-cache",
        lock_hash=lock_hash,
        dependencies=[
            {
                "kind": "python",
                "source": "pypi",
                "name": "requests",
                "version": "2.32.0",
            }
        ],
    )
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    ).model_copy(
        update={
            "activation_state": "eligible",
            "activation_reason": None,
            "dependencies": (
                PluginPackageDependency(name="Requests", version="2.32.0"),
            ),
        }
    )

    resource = plugin(
        "calendar",
        adapter="calendar_python",
        sandbox_profile="local_process",
        package_metadata=verified_package_with_artifact(
            content_sha256=content_sha256,
            storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
        ),
        content_sha256=content_sha256,
    ).model_copy(update={"package_metadata": package_metadata})

    target = _plugin_package_execution_target(
        resource,
        tenant_id=TENANT_ID,
        package_store_dir=tmp_path / "packages",
        dependency_policy=PluginPackageDependencyPolicy(
            install_policy="offline_cache",
            allowlist=frozenset({"python:pypi:requests==2.32.0"}),
            cache_dir=tmp_path / "dependency-cache",
        ),
    )

    assert target.root == artifact_root
    assert target.entrypoint == artifact_root / "adapter" / "main.py"
    assert target.dependency_root == dependency_root


def test_plugin_package_execution_target_rejects_missing_artifact_directory(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin package artifact is unavailable"):
        _plugin_package_execution_target(
            plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="in_process",
                package_metadata=package_metadata,
                content_sha256=content_sha256,
            ),
            tenant_id=TENANT_ID,
            package_store_dir=tmp_path,
        )


def test_plugin_package_execution_target_rejects_entrypoint_escaping_artifact_root(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    artifact_root.mkdir(parents=True)
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
        entrypoint="../main.py",
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin package entrypoint is unavailable"):
        _plugin_package_execution_target(
            plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="in_process",
                package_metadata=package_metadata,
                content_sha256=content_sha256,
            ),
            tenant_id=TENANT_ID,
            package_store_dir=tmp_path,
        )


def test_plugin_package_execution_target_rejects_symlinked_entrypoint_outside_artifact_root(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    (artifact_root / "adapter").mkdir(parents=True)
    outside_entrypoint = tmp_path / "outside.py"
    outside_entrypoint.write_text("def invoke():\n    return {}\n")
    try:
        (artifact_root / "adapter" / "main.py").symlink_to(outside_entrypoint)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin package path is invalid"):
        _plugin_package_execution_target(
            plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="in_process",
                package_metadata=package_metadata,
                content_sha256=content_sha256,
            ),
            tenant_id=TENANT_ID,
            package_store_dir=tmp_path,
        )


@pytest.mark.parametrize(
    ("package_update", "error"),
    [
        (
            {
                "kind": "manifest_only",
                "activation_state": "not_applicable",
                "activation_reason": "manifest-only package has no executable activation target",
            },
            "Plugin package artifact is unavailable",
        ),
        (
            {
                "install_mode": "scan_only",
                "activation_state": "verified_scan_only",
                "activation_reason": (
                    "package signature is verified, but install_mode=scan_only prevents activation"
                ),
            },
            "Plugin package is not eligible for executable activation",
        ),
        (
            {
                "signature_verification": "not_verified",
                "activation_state": "blocked_unverified_signature",
                "activation_reason": "package signature has not been verified by the server",
            },
            "Plugin package is not eligible for executable activation",
        ),
        (
            {
                "approval_state": "pending",
                "approval_reason": "adapter package requires plugin approval before activation",
                "activation_state": "blocked_pending_approval",
                "activation_reason": "adapter package requires plugin approval before activation",
            },
            "Plugin package is not eligible for executable activation",
        ),
    ],
)
def test_plugin_package_execution_target_rejects_non_executable_package_state(
    tmp_path: Path,
    package_update: Mapping[str, object],
    error: str,
) -> None:
    content_sha256 = "a" * 64
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    ).model_copy(update=package_update)

    with pytest.raises(RuntimeCapabilityError, match=error):
        _plugin_package_execution_target(
            plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="in_process",
                package_metadata=package_metadata,
                content_sha256=content_sha256,
            ),
            tenant_id=TENANT_ID,
            package_store_dir=tmp_path,
        )


async def test_plugin_package_adapter_invokes_runner_with_execution_target(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    (artifact_root / "adapter").mkdir(parents=True)
    (artifact_root / "adapter" / "main.py").write_text("def invoke():\n    return {}\n")
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    )
    runner = RecordingPluginPackageRunner(calls=[], result={"ok": True, "source": "package"})
    adapter = PluginPackageAdapter(
        adapter_id="calendar_python",
        package_store_dir=tmp_path,
        runner=runner,
    )
    context = PluginInvocationContext(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="tester",
        idempotency_key="invoke-1",
    )

    result = await adapter.invoke(
        plugin=plugin(
            "calendar",
            adapter="calendar_python",
            sandbox_profile="in_process",
            package_metadata=package_metadata,
            content_sha256=content_sha256,
        ),
        capability=PluginCapabilityRequest(
            id="calendar.create_event",
            adapter="calendar_python",
            permission_class="calendar.write",
            sandbox_profile="in_process",
        ),
        arguments={"title": "review"},
        context=context,
    )

    assert result == {"ok": True, "source": "package"}
    assert runner.calls[0][0].root == artifact_root
    assert runner.calls[0][0].entrypoint == artifact_root / "adapter" / "main.py"
    assert runner.calls[0][1:] == (
        "calendar",
        "calendar.create_event",
        {"title": "review"},
        context,
    )


async def test_plugin_package_adapter_passes_ready_dependency_cache_to_runner(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    package_store_dir = tmp_path / "packages"
    artifact_root = package_store_dir / str(TENANT_ID) / "calendar" / content_sha256
    (artifact_root / "adapter").mkdir(parents=True)
    (artifact_root / "adapter" / "main.py").write_text("def invoke():\n    return {}\n")
    lock_hash = hashlib.sha256(b"python pypi requests==2.32.0\n").hexdigest()
    dependency_root = write_dependency_cache_manifest(
        tmp_path / "dependency-cache",
        lock_hash=lock_hash,
        dependencies=[
            {
                "kind": "python",
                "source": "pypi",
                "name": "requests",
                "version": "2.32.0",
            }
        ],
    )
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    ).model_copy(
        update={
            "activation_state": "eligible",
            "activation_reason": None,
            "dependencies": (
                PluginPackageDependency(name="Requests", version="2.32.0"),
            ),
        }
    )
    runner = RecordingPluginPackageRunner(calls=[], result={"ok": True})
    adapter = PluginPackageAdapter(
        adapter_id="calendar_python",
        package_store_dir=package_store_dir,
        runner=runner,
        dependency_policy=PluginPackageDependencyPolicy(
            install_policy="offline_cache",
            allowlist=frozenset({"python:pypi:requests==2.32.0"}),
            cache_dir=tmp_path / "dependency-cache",
        ),
    )
    context = PluginInvocationContext(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="tester",
        idempotency_key="invoke-1",
    )
    resource = plugin(
        "calendar",
        adapter="calendar_python",
        sandbox_profile="local_process",
        package_metadata=verified_package_with_artifact(
            content_sha256=content_sha256,
            storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
        ),
        content_sha256=content_sha256,
    ).model_copy(update={"package_metadata": package_metadata})

    result = await adapter.invoke(
        plugin=resource,
        capability=PluginCapabilityRequest(
            id="calendar.create_event",
            adapter="calendar_python",
            permission_class="calendar.write",
            sandbox_profile="local_process",
        ),
        arguments={"title": "review"},
        context=context,
    )

    assert result == {"ok": True}
    assert runner.calls[0][0].root == artifact_root
    assert runner.calls[0][0].dependency_root == dependency_root


async def test_plugin_package_adapter_fails_closed_before_runner_when_target_invalid(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    )
    runner = RecordingPluginPackageRunner(calls=[])
    adapter = PluginPackageAdapter(
        adapter_id="calendar_python",
        package_store_dir=tmp_path,
        runner=runner,
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin package artifact is unavailable"):
        await adapter.invoke(
            plugin=plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="in_process",
                package_metadata=package_metadata,
                content_sha256=content_sha256,
            ),
            capability=PluginCapabilityRequest(
                id="calendar.create_event",
                adapter="calendar_python",
                permission_class="calendar.write",
                sandbox_profile="in_process",
            ),
            arguments={"title": "review"},
            context=PluginInvocationContext(
                tenant_id=TENANT_ID,
                user_id=TENANT_ID,
                run_id=TENANT_ID,
                actor="tester",
                idempotency_key="invoke-1",
            ),
        )

    assert runner.calls == []


async def test_plugin_package_adapter_rejects_capability_adapter_mismatch_before_runner(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    (artifact_root / "adapter").mkdir(parents=True)
    (artifact_root / "adapter" / "main.py").write_text("def invoke():\n    return {}\n")
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    )
    runner = RecordingPluginPackageRunner(calls=[])
    adapter = PluginPackageAdapter(
        adapter_id="calendar_python",
        package_store_dir=tmp_path,
        runner=runner,
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin package adapter mismatch"):
        await adapter.invoke(
            plugin=plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="in_process",
                package_metadata=package_metadata,
                content_sha256=content_sha256,
            ),
            capability=PluginCapabilityRequest(
                id="calendar.create_event",
                adapter="other_python",
                permission_class="calendar.write",
                sandbox_profile="in_process",
            ),
            arguments={"title": "review"},
            context=PluginInvocationContext(
                tenant_id=TENANT_ID,
                user_id=TENANT_ID,
                run_id=TENANT_ID,
                actor="tester",
                idempotency_key="invoke-1",
            ),
        )

    assert runner.calls == []


async def test_plugin_package_adapter_rejects_package_adapter_id_mismatch_before_runner(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    (artifact_root / "adapter").mkdir(parents=True)
    (artifact_root / "adapter" / "main.py").write_text("def invoke():\n    return {}\n")
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    ).model_copy(update={"adapter_id": "other_python"})
    runner = RecordingPluginPackageRunner(calls=[])
    adapter = PluginPackageAdapter(
        adapter_id="calendar_python",
        package_store_dir=tmp_path,
        runner=runner,
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin package adapter mismatch"):
        await adapter.invoke(
            plugin=plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="in_process",
                package_metadata=package_metadata,
                content_sha256=content_sha256,
            ),
            capability=PluginCapabilityRequest(
                id="calendar.create_event",
                adapter="calendar_python",
                permission_class="calendar.write",
                sandbox_profile="in_process",
            ),
            arguments={"title": "review"},
            context=PluginInvocationContext(
                tenant_id=TENANT_ID,
                user_id=TENANT_ID,
                run_id=TENANT_ID,
                actor="tester",
                idempotency_key="invoke-1",
            ),
        )

    assert runner.calls == []


async def test_plugin_package_adapter_uses_context_tenant_to_resolve_target(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    (artifact_root / "adapter").mkdir(parents=True)
    (artifact_root / "adapter" / "main.py").write_text("def invoke():\n    return {}\n")
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    )
    runner = RecordingPluginPackageRunner(calls=[])
    adapter = PluginPackageAdapter(
        adapter_id="calendar_python",
        package_store_dir=tmp_path,
        runner=runner,
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin package artifact storage key is invalid"):
        await adapter.invoke(
            plugin=plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="in_process",
                package_metadata=package_metadata,
                content_sha256=content_sha256,
            ),
            capability=PluginCapabilityRequest(
                id="calendar.create_event",
                adapter="calendar_python",
                permission_class="calendar.write",
                sandbox_profile="in_process",
            ),
            arguments={"title": "review"},
            context=PluginInvocationContext(
                tenant_id=OTHER_TENANT_ID,
                user_id=TENANT_ID,
                run_id=TENANT_ID,
                actor="tester",
                idempotency_key="invoke-1",
            ),
        )

    assert runner.calls == []


async def test_plugin_package_adapter_rejects_runner_non_mapping_result(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    (artifact_root / "adapter").mkdir(parents=True)
    (artifact_root / "adapter" / "main.py").write_text("def invoke():\n    return {}\n")
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    )
    runner = RecordingPluginPackageRunner(calls=[], result=["not", "mapping"])
    adapter = PluginPackageAdapter(
        adapter_id="calendar_python",
        package_store_dir=tmp_path,
        runner=runner,
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin result is invalid"):
        await adapter.invoke(
            plugin=plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="in_process",
                package_metadata=package_metadata,
                content_sha256=content_sha256,
            ),
            capability=PluginCapabilityRequest(
                id="calendar.create_event",
                adapter="calendar_python",
                permission_class="calendar.write",
                sandbox_profile="in_process",
            ),
            arguments={"title": "review"},
            context=PluginInvocationContext(
                tenant_id=TENANT_ID,
                user_id=TENANT_ID,
                run_id=TENANT_ID,
                actor="tester",
                idempotency_key="invoke-1",
            ),
        )

    assert len(runner.calls) == 1


async def test_plugin_package_adapter_wraps_runner_failure_without_leaking_details(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    (artifact_root / "adapter").mkdir(parents=True)
    (artifact_root / "adapter" / "main.py").write_text("def invoke():\n    return {}\n")
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    )
    runner = RecordingPluginPackageRunner(
        calls=[],
        failure=ValueError("secret detail from package runner"),
    )
    adapter = PluginPackageAdapter(
        adapter_id="calendar_python",
        package_store_dir=tmp_path,
        runner=runner,
    )

    with pytest.raises(RuntimeCapabilityError) as error:
        await adapter.invoke(
            plugin=plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="in_process",
                package_metadata=package_metadata,
                content_sha256=content_sha256,
            ),
            capability=PluginCapabilityRequest(
                id="calendar.create_event",
                adapter="calendar_python",
                permission_class="calendar.write",
                sandbox_profile="in_process",
            ),
            arguments={"title": "review"},
            context=PluginInvocationContext(
                tenant_id=TENANT_ID,
                user_id=TENANT_ID,
                run_id=TENANT_ID,
                actor="tester",
                idempotency_key="invoke-1",
            ),
        )

    assert str(error.value) == "Plugin tool failed"


async def test_runtime_plugin_service_can_invoke_registered_package_adapter_runner(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    (artifact_root / "adapter").mkdir(parents=True)
    (artifact_root / "adapter" / "main.py").write_text("def invoke():\n    return {}\n")
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    )
    runner = RecordingPluginPackageRunner(calls=[])
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    adapter="calendar_python",
                    sandbox_profile="local_process",
                    package_metadata=package_metadata,
                    content_sha256=content_sha256,
                ),
            )
        ),
        adapters={
            "calendar_python": PluginPackageAdapter(
                adapter_id="calendar_python",
                package_store_dir=tmp_path,
                runner=runner,
            )
        },
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="tester",
        name="calendar.create_event",
        arguments={"title": "review"},
        idempotency_key="invoke-1",
    )

    assert result["ok"] is True
    assert runner.calls[0][0].root == artifact_root


async def test_runtime_plugin_service_invokes_allowlisted_package_adapter_through_subprocess(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    entrypoint = artifact_root / "adapter" / "main.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text(
        "import json\n"
        "import sys\n"
        "payload = json.load(sys.stdin)\n"
        "json.dump({\n"
        "    'remote_id': 'evt_' + payload['arguments']['title'],\n"
        "    'actor': payload['context']['actor'],\n"
        "    'zone': payload['resource_config']['zone'],\n"
        "    'mode': payload['capability_config']['mode'],\n"
        "}, sys.stdout)\n"
    )
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    )
    admin_service = FakeAdminService(
        (
            plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="local_process",
                package_metadata=package_metadata,
                content_sha256=content_sha256,
                resource_config={"zone": "utc"},
                capability_config={"mode": "smoke"},
                output_schema={
                    "type": "object",
                    "properties": {
                        "remote_id": {"type": "string"},
                        "actor": {"type": "string"},
                        "zone": {"type": "string"},
                        "mode": {"type": "string"},
                    },
                    "required": ("remote_id", "actor", "zone", "mode"),
                    "additionalProperties": False,
                },
            ),
        )
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        adapters={
            "calendar_python": PluginPackageAdapter(
                adapter_id="calendar_python",
                package_store_dir=tmp_path,
                runner=PythonSubprocessPluginPackageRunner(
                    python_executable=sys.executable,
                    timeout_seconds=2,
                ),
            )
        },
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="scheduler",
        name="calendar.create_event",
        arguments={"title": "review"},
        idempotency_key="plugin_1",
    )

    assert result == {
        "remote_id": "evt_review",
        "actor": "scheduler",
        "zone": "utc",
        "mode": "smoke",
    }
    assert admin_service.audit_events == [
        {
            "actor": "scheduler",
            "action": "plugin.invoke.succeeded",
            "resource": "plugin:calendar:calendar.create_event",
            "details": {
                "plugin_id": "calendar",
                "capability_id": "calendar.create_event",
                "adapter": "calendar_python",
                "permission_class": "calendar.write",
                "sandbox_profile": "local_process",
                "replay_safe": False,
                "run_id": str(TENANT_ID),
                "user_id": str(TENANT_ID),
                "idempotency_key": "plugin_1",
            },
        }
    ]
    assert "review" not in repr(admin_service.audit_events)


async def test_runtime_plugin_service_rejects_package_adapter_result_that_violates_output_schema(
    tmp_path: Path,
) -> None:
    content_sha256 = "a" * 64
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    (artifact_root / "adapter").mkdir(parents=True)
    (artifact_root / "adapter" / "main.py").write_text("def invoke():\n    return {}\n")
    package_metadata = verified_package_with_artifact(
        content_sha256=content_sha256,
        storage_key=f"{TENANT_ID}/calendar/{content_sha256}",
    )
    runner = RecordingPluginPackageRunner(calls=[], result={"ok": "yes"})
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    adapter="calendar_python",
                    sandbox_profile="local_process",
                    package_metadata=package_metadata,
                    content_sha256=content_sha256,
                    output_schema={
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ("ok",),
                        "additionalProperties": False,
                    },
                ),
            )
        ),
        adapters={
            "calendar_python": PluginPackageAdapter(
                adapter_id="calendar_python",
                package_store_dir=tmp_path,
                runner=runner,
            )
        },
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin result does not match output schema"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="tester",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="invoke-1",
        )


async def test_python_subprocess_plugin_package_runner_sends_stable_json_request(
    tmp_path: Path,
) -> None:
    entrypoint = tmp_path / "adapter.py"
    entrypoint.write_text(
        "import json\n"
        "import sys\n"
        "payload = json.load(sys.stdin)\n"
        "json.dump({\n"
        "    'ok': True,\n"
        "    'keys': sorted(payload),\n"
        "    'plugin_id': payload['plugin_id'],\n"
        "    'capability_id': payload['capability_id'],\n"
        "    'title': payload['arguments']['title'],\n"
        "    'resource_zone': payload['resource_config']['zone'],\n"
        "    'capability_mode': payload['capability_config']['mode'],\n"
        "    'actor': payload['context']['actor'],\n"
        "    'tenant_id': payload['context']['tenant_id'],\n"
        "}, sys.stdout)\n"
    )
    runner = PythonSubprocessPluginPackageRunner(
        python_executable=sys.executable,
        timeout_seconds=2,
    )

    result = await runner.invoke(
        target=PluginPackageExecutionTarget(root=tmp_path, entrypoint=entrypoint),
        plugin=plugin(
            "calendar",
            adapter="calendar_python",
            resource_config={"zone": "utc"},
        ),
        capability=PluginCapabilityRequest(
            id="calendar.create_event",
            adapter="calendar_python",
            permission_class="calendar.write",
            sandbox_profile="in_process",
            capability_config={"mode": "dry_run"},
        ),
        arguments={"title": "review"},
        context=PluginInvocationContext(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="tester",
            idempotency_key="invoke-1",
        ),
    )

    assert result == {
        "ok": True,
        "keys": (
            "arguments",
            "capability_config",
            "capability_id",
            "context",
            "plugin_id",
            "resource_config",
            "schema_version",
        ),
        "plugin_id": "calendar",
        "capability_id": "calendar.create_event",
        "title": "review",
        "resource_zone": "utc",
        "capability_mode": "dry_run",
        "actor": "tester",
        "tenant_id": str(TENANT_ID),
    }


async def test_python_subprocess_plugin_package_runner_imports_dependency_cache(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    dependency_root = tmp_path / "dependency-cache"
    entrypoint = package_root / "adapter" / "main.py"
    entrypoint.parent.mkdir(parents=True)
    dependency_root.mkdir()
    (dependency_root / "offline_dep.py").write_text(
        "VALUE = 'from-offline-cache'\n",
        encoding="utf-8",
    )
    entrypoint.write_text(
        "import json\n"
        "import sys\n"
        "import offline_dep\n"
        "json.load(sys.stdin)\n"
        "json.dump({'value': offline_dep.VALUE}, sys.stdout)\n",
        encoding="utf-8",
    )
    runner = PythonSubprocessPluginPackageRunner(
        python_executable=sys.executable,
        timeout_seconds=2,
    )

    result = await runner.invoke(
        target=PluginPackageExecutionTarget(
            root=package_root,
            entrypoint=entrypoint,
            dependency_root=dependency_root,
        ),
        plugin=plugin("calendar", adapter="calendar_python"),
        capability=PluginCapabilityRequest(
            id="calendar.create_event",
            adapter="calendar_python",
            permission_class="calendar.write",
            sandbox_profile="local_process",
        ),
        arguments={"title": "x"},
        context=PluginInvocationContext(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="tester",
            idempotency_key="invoke-1",
        ),
    )

    assert result == {"value": "from-offline-cache"}


async def test_python_subprocess_plugin_package_runner_uses_package_root_and_minimal_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_HUB_SECRET_TOKEN", "must-not-leak")
    monkeypatch.setenv("PYTHONPATH", "must-not-leak")
    entrypoint = tmp_path / "adapter.py"
    entrypoint.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "json.load(sys.stdin)\n"
        "json.dump({\n"
        "    'cwd': os.getcwd(),\n"
        "    'secret_present': 'AGENT_HUB_SECRET_TOKEN' in os.environ,\n"
        "    'pythonpath_present': 'PYTHONPATH' in os.environ,\n"
        "    'isolated': sys.flags.isolated,\n"
        "}, sys.stdout)\n"
    )
    runner = PythonSubprocessPluginPackageRunner(
        python_executable=sys.executable,
        timeout_seconds=2,
    )

    result = await runner.invoke(
        target=PluginPackageExecutionTarget(root=tmp_path, entrypoint=entrypoint),
        plugin=plugin("calendar", adapter="calendar_python"),
        capability=PluginCapabilityRequest(
            id="calendar.create_event",
            adapter="calendar_python",
            permission_class="calendar.write",
            sandbox_profile="in_process",
        ),
        arguments={"title": "review"},
        context=PluginInvocationContext(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="tester",
            idempotency_key="invoke-1",
        ),
    )

    assert Path(cast(str, result["cwd"])) == tmp_path
    assert result["secret_present"] is False
    assert result["pythonpath_present"] is False
    assert result["isolated"] == 1
    assert os.environ["AGENT_HUB_SECRET_TOKEN"] == "must-not-leak"


async def test_python_subprocess_plugin_package_runner_filters_custom_environment(
    tmp_path: Path,
) -> None:
    entrypoint = tmp_path / "adapter.py"
    entrypoint.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "json.load(sys.stdin)\n"
        "json.dump({\n"
        "    'secret_present': 'AGENT_HUB_SECRET_TOKEN' in os.environ,\n"
        "    'pythonpath_present': 'PYTHONPATH' in os.environ,\n"
        "    'lang': os.environ.get('LANG'),\n"
        "    'python_no_user_site': os.environ.get('PYTHONNOUSERSITE'),\n"
        "}, sys.stdout)\n"
    )
    runner = PythonSubprocessPluginPackageRunner(
        python_executable=sys.executable,
        timeout_seconds=2,
        environment={
            "AGENT_HUB_SECRET_TOKEN": "must-not-leak",
            "PYTHONPATH": "must-not-leak",
            "LANG": "C.UTF-8",
        },
    )

    result = await runner.invoke(
        target=PluginPackageExecutionTarget(root=tmp_path, entrypoint=entrypoint),
        plugin=plugin("calendar", adapter="calendar_python"),
        capability=PluginCapabilityRequest(
            id="calendar.create_event",
            adapter="calendar_python",
            permission_class="calendar.write",
            sandbox_profile="in_process",
        ),
        arguments={"title": "review"},
        context=PluginInvocationContext(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="tester",
            idempotency_key="invoke-1",
        ),
    )

    assert result == {
        "secret_present": False,
        "pythonpath_present": False,
        "lang": "C.UTF-8",
        "python_no_user_site": "1",
    }


@pytest.mark.parametrize(
    ("script", "error"),
    [
        ("import sys; sys.stdout.write('not json')", "Plugin result is invalid"),
        ("import json, sys; json.dump(['not', 'object'], sys.stdout)", "Plugin result is invalid"),
        (
            "import sys; sys.stderr.write('secret detail from plugin'); sys.exit(7)",
            "Plugin tool failed",
        ),
    ],
)
async def test_python_subprocess_plugin_package_runner_fails_closed_for_bad_process_result(
    tmp_path: Path,
    script: str,
    error: str,
) -> None:
    entrypoint = tmp_path / "adapter.py"
    entrypoint.write_text(script)
    runner = PythonSubprocessPluginPackageRunner(
        python_executable=sys.executable,
        timeout_seconds=2,
    )

    with pytest.raises(RuntimeCapabilityError) as failure:
        await runner.invoke(
            target=PluginPackageExecutionTarget(root=tmp_path, entrypoint=entrypoint),
            plugin=plugin("calendar", adapter="calendar_python"),
            capability=PluginCapabilityRequest(
                id="calendar.create_event",
                adapter="calendar_python",
                permission_class="calendar.write",
                sandbox_profile="in_process",
            ),
            arguments={"title": "review"},
            context=PluginInvocationContext(
                tenant_id=TENANT_ID,
                user_id=TENANT_ID,
                run_id=TENANT_ID,
                actor="tester",
                idempotency_key="invoke-1",
            ),
        )

    assert str(failure.value) == error


async def test_python_subprocess_plugin_package_runner_times_out(
    tmp_path: Path,
) -> None:
    entrypoint = tmp_path / "adapter.py"
    entrypoint.write_text("import time; time.sleep(5)")
    runner = PythonSubprocessPluginPackageRunner(
        python_executable=sys.executable,
        timeout_seconds=0.05,
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin tool timed out"):
        await runner.invoke(
            target=PluginPackageExecutionTarget(root=tmp_path, entrypoint=entrypoint),
            plugin=plugin("calendar", adapter="calendar_python"),
            capability=PluginCapabilityRequest(
                id="calendar.create_event",
                adapter="calendar_python",
                permission_class="calendar.write",
                sandbox_profile="in_process",
            ),
            arguments={"title": "review"},
            context=PluginInvocationContext(
                tenant_id=TENANT_ID,
                user_id=TENANT_ID,
                run_id=TENANT_ID,
                actor="tester",
                idempotency_key="invoke-1",
            ),
        )


async def test_python_subprocess_plugin_package_runner_kills_child_when_stdout_exceeds_limit(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "continued-after-oversized-stdout.txt"
    entrypoint = tmp_path / "adapter.py"
    entrypoint.write_text(
        "import json\n"
        "import pathlib\n"
        "import sys\n"
        "import time\n"
        "json.load(sys.stdin)\n"
        "sys.stdout.write('x' * 4096)\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.5)\n"
        f"pathlib.Path({marker.name!r}).write_text('not killed')\n"
        "json.dump({'ok': True}, sys.stdout)\n"
    )
    runner = PythonSubprocessPluginPackageRunner(
        python_executable=sys.executable,
        timeout_seconds=2,
        max_stdout_bytes=128,
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin result is invalid"):
        await runner.invoke(
            target=PluginPackageExecutionTarget(root=tmp_path, entrypoint=entrypoint),
            plugin=plugin("calendar", adapter="calendar_python"),
            capability=PluginCapabilityRequest(
                id="calendar.create_event",
                adapter="calendar_python",
                permission_class="calendar.write",
                sandbox_profile="in_process",
            ),
            arguments={"title": "review"},
            context=PluginInvocationContext(
                tenant_id=TENANT_ID,
                user_id=TENANT_ID,
                run_id=TENANT_ID,
                actor="tester",
                idempotency_key="invoke-1",
            ),
        )

    assert not marker.exists()


async def test_python_subprocess_plugin_package_runner_discards_stderr(
    tmp_path: Path,
) -> None:
    entrypoint = tmp_path / "adapter.py"
    entrypoint.write_text(
        "import json\n"
        "import sys\n"
        "sys.stderr.write('secret detail from plugin' * 200000)\n"
        "json.load(sys.stdin)\n"
        "json.dump({'ok': True}, sys.stdout)\n"
    )
    runner = PythonSubprocessPluginPackageRunner(
        python_executable=sys.executable,
        timeout_seconds=2,
    )

    result = await runner.invoke(
        target=PluginPackageExecutionTarget(root=tmp_path, entrypoint=entrypoint),
        plugin=plugin("calendar", adapter="calendar_python"),
        capability=PluginCapabilityRequest(
            id="calendar.create_event",
            adapter="calendar_python",
            permission_class="calendar.write",
            sandbox_profile="in_process",
        ),
        arguments={"title": "review"},
        context=PluginInvocationContext(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="tester",
            idempotency_key="invoke-1",
        ),
    )

    assert result == {"ok": True}


async def test_python_subprocess_plugin_package_runner_rejects_oversized_stdin_before_spawn(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "started.txt"
    entrypoint = tmp_path / "adapter.py"
    entrypoint.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('started')\n"
        "import json\n"
        "import sys\n"
        "json.load(sys.stdin)\n"
        "json.dump({'ok': True}, sys.stdout)\n"
    )
    runner = PythonSubprocessPluginPackageRunner(
        python_executable=sys.executable,
        timeout_seconds=2,
        max_stdin_bytes=128,
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin request is too large"):
        await runner.invoke(
            target=PluginPackageExecutionTarget(root=tmp_path, entrypoint=entrypoint),
            plugin=plugin("calendar", adapter="calendar_python"),
            capability=PluginCapabilityRequest(
                id="calendar.create_event",
                adapter="calendar_python",
                permission_class="calendar.write",
                sandbox_profile="in_process",
            ),
            arguments={"title": "x" * 512},
            context=PluginInvocationContext(
                tenant_id=TENANT_ID,
                user_id=TENANT_ID,
                run_id=TENANT_ID,
                actor="tester",
                idempotency_key="invoke-1",
            ),
        )

    assert not marker.exists()


async def test_python_subprocess_plugin_package_runner_rejects_entrypoint_outside_root_before_spawn(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    marker = tmp_path / "started.txt"
    entrypoint = tmp_path / "outside.py"
    entrypoint.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('started')\n"
        "import json\n"
        "import sys\n"
        "json.dump({'ok': True}, sys.stdout)\n"
    )
    runner = PythonSubprocessPluginPackageRunner(
        python_executable=sys.executable,
        timeout_seconds=2,
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin package path is invalid"):
        await runner.invoke(
            target=PluginPackageExecutionTarget(root=package_root, entrypoint=entrypoint),
            plugin=plugin("calendar", adapter="calendar_python"),
            capability=PluginCapabilityRequest(
                id="calendar.create_event",
                adapter="calendar_python",
                permission_class="calendar.write",
                sandbox_profile="local_process",
            ),
            arguments={"title": "x"},
            context=PluginInvocationContext(
                tenant_id=TENANT_ID,
                user_id=TENANT_ID,
                run_id=TENANT_ID,
                actor="tester",
                idempotency_key="invoke-1",
            ),
        )

    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="bubblewrap integration is POSIX-only")
async def test_python_subprocess_plugin_package_runner_with_bubblewrap_blocks_writes_and_network(
    tmp_path: Path,
) -> None:
    bubblewrap_executable = shutil.which("bwrap")
    if bubblewrap_executable is None:
        pytest.skip("bubblewrap is not installed")
    package_root = tmp_path / "package"
    entrypoint = package_root / "adapter" / "main.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text(
        "import json\n"
        "import socket\n"
        "import sys\n"
        "write_blocked = False\n"
        "network_blocked = False\n"
        "try:\n"
        "    open('blocked.txt', 'w').write('x')\n"
        "except OSError:\n"
        "    write_blocked = True\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), 1)\n"
        "except OSError:\n"
        "    network_blocked = True\n"
        "json.dump({'write_blocked': write_blocked, 'network_blocked': network_blocked}, sys.stdout)\n"
    )
    runner = PythonSubprocessPluginPackageRunner(
        process_launcher=BubblewrapPluginPackageProcessLauncher(
            bubblewrap_executable=Path(bubblewrap_executable)
        ),
        timeout_seconds=5,
    )

    result = await runner.invoke(
        target=PluginPackageExecutionTarget(root=package_root, entrypoint=entrypoint),
        plugin=plugin("calendar", adapter="calendar_python"),
        capability=PluginCapabilityRequest(
            id="calendar.create_event",
            adapter="calendar_python",
            permission_class="calendar.write",
            sandbox_profile="local_process",
        ),
        arguments={"title": "isolated"},
        context=PluginInvocationContext(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="tester",
            idempotency_key="invoke-1",
        ),
    )

    assert result == {"write_blocked": True, "network_blocked": True}
    assert not (package_root / "blocked.txt").exists()


def test_bubblewrap_plugin_package_launcher_binds_runtime_without_network(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    dependency_root = tmp_path / "dependency-cache"
    runtime_root = tmp_path / "python-runtime"
    entrypoint = package_root / "adapter" / "main.py"
    launcher = BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=tmp_path / "bwrap",
        readonly_bind_paths=(runtime_root,),
    )

    argv = launcher.argv(
        python_executable=str(runtime_root / "bin" / "python"),
        target=PluginPackageExecutionTarget(
            root=package_root,
            entrypoint=entrypoint,
            dependency_root=dependency_root,
        ),
    )

    assert argv[0] == str(tmp_path / "bwrap")
    assert "--unshare-net" in argv
    assert _argv_contains_ordered_pair(argv, "--ro-bind", package_root, package_root)
    assert _argv_contains_ordered_pair(argv, "--ro-bind", dependency_root, dependency_root)
    assert _argv_contains_ordered_pair(argv, "--ro-bind", runtime_root, runtime_root)
    assert _argv_contains_ordered_args(argv, ("--dev", "/dev"))
    assert _argv_contains_ordered_args(argv, ("--proc", "/proc"))
    assert _argv_index(argv, ("--tmpfs", "/tmp")) < _argv_index(
        argv,
        ("--ro-bind", str(package_root), str(package_root)),
    )
    assert argv[-6] == str(runtime_root / "bin" / "python")
    assert argv[-5] == "-I"
    assert argv[-4] == "-c"
    assert argv[-2:] == (str(dependency_root), str(entrypoint))


def test_build_plugin_package_subprocess_adapters_requires_explicit_enablement(
    tmp_path: Path,
) -> None:
    assert (
        build_plugin_package_subprocess_adapters(
            enabled=False,
            adapter_ids=("calendar_python",),
            package_store_dir=tmp_path,
        )
        == {}
    )


def test_plugin_package_subprocess_registration_status_reports_disabled(
    tmp_path: Path,
) -> None:
    assert (
        _plugin_package_subprocess_registration_status(
            enabled=False,
            adapter_ids=("calendar_python",),
            isolation_backend="bubblewrap",
            bubblewrap_executable=tmp_path / "bwrap",
        )
        == "disabled"
    )


def test_plugin_package_subprocess_registration_status_reports_missing_adapter_ids(
    tmp_path: Path,
) -> None:
    bubblewrap_executable = tmp_path / "bwrap"
    bubblewrap_executable.write_text("")
    if os.name == "posix":
        bubblewrap_executable.chmod(0o755)

    assert (
        _plugin_package_subprocess_registration_status(
            enabled=True,
            adapter_ids=(),
            isolation_backend="bubblewrap",
            bubblewrap_executable=bubblewrap_executable,
        )
        == "no_adapter_ids"
    )


def test_plugin_package_subprocess_registration_status_reports_unsupported_isolation_backend(
    tmp_path: Path,
) -> None:
    assert (
        _plugin_package_subprocess_registration_status(
            enabled=True,
            adapter_ids=("calendar_python",),
            isolation_backend="disabled",
            bubblewrap_executable=tmp_path / "bwrap",
        )
        == "unsupported_isolation_backend"
    )


def test_plugin_package_subprocess_registration_status_reports_relative_launcher() -> None:
    assert (
        _plugin_package_subprocess_registration_status(
            enabled=True,
            adapter_ids=("calendar_python",),
            isolation_backend="bubblewrap",
            bubblewrap_executable=Path("bwrap"),
        )
        == "launcher_path_not_absolute"
    )


@pytest.mark.skipif(os.name != "posix", reason="bubblewrap registration is POSIX-only")
def test_plugin_package_subprocess_registration_status_reports_ready(tmp_path: Path) -> None:
    bubblewrap_executable = tmp_path / "bwrap"
    bubblewrap_executable.write_text("")
    bubblewrap_executable.chmod(0o755)

    assert (
        _plugin_package_subprocess_registration_status(
            enabled=True,
            adapter_ids=("calendar_python",),
            isolation_backend="bubblewrap",
            bubblewrap_executable=bubblewrap_executable,
        )
        == "ready"
    )


def test_plugin_package_subprocess_registration_status_rejects_bubblewrap_on_non_posix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bubblewrap_executable = tmp_path / "bwrap"
    bubblewrap_executable.write_text("")
    monkeypatch.setattr("agent_hub.plugins.runtime.os.name", "nt")

    assert (
        _plugin_package_subprocess_registration_status(
            enabled=True,
            adapter_ids=("calendar_python",),
            isolation_backend="bubblewrap",
            bubblewrap_executable=bubblewrap_executable,
        )
        == "unsupported_isolation_backend"
    )


def test_plugin_package_subprocess_registration_status_reports_launcher_reason(
    tmp_path: Path,
) -> None:
    assert (
        _plugin_package_subprocess_registration_status(
            enabled=True,
            adapter_ids=("calendar_python",),
            isolation_backend="bubblewrap",
            bubblewrap_executable=tmp_path / "missing-bwrap",
        )
        == "launcher_not_found"
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable bit only")
def test_plugin_package_subprocess_registration_status_reports_nonexecutable_launcher(
    tmp_path: Path,
) -> None:
    bubblewrap_executable = tmp_path / "bwrap"
    bubblewrap_executable.write_text("")
    bubblewrap_executable.chmod(0o644)

    assert (
        _plugin_package_subprocess_registration_status(
            enabled=True,
            adapter_ids=("calendar_python",),
            isolation_backend="bubblewrap",
            bubblewrap_executable=bubblewrap_executable,
        )
        == "launcher_not_executable"
    )


def test_build_plugin_package_subprocess_adapters_requires_isolation_launcher(
    tmp_path: Path,
) -> None:
    assert (
        build_plugin_package_subprocess_adapters(
            enabled=True,
            adapter_ids=("calendar_python",),
            package_store_dir=tmp_path,
            isolation_backend="disabled",
            bubblewrap_executable=None,
        )
        == {}
    )


def test_build_plugin_package_subprocess_adapters_requires_absolute_launcher(
    tmp_path: Path,
) -> None:
    assert (
        build_plugin_package_subprocess_adapters(
            enabled=True,
            adapter_ids=("calendar_python",),
            package_store_dir=tmp_path,
            isolation_backend="bubblewrap",
            bubblewrap_executable=Path("bwrap"),
        )
        == {}
    )


def test_build_plugin_package_subprocess_adapters_requires_existing_launcher_file(
    tmp_path: Path,
) -> None:
    assert (
        build_plugin_package_subprocess_adapters(
            enabled=True,
            adapter_ids=("calendar_python",),
            package_store_dir=tmp_path,
            isolation_backend="bubblewrap",
            bubblewrap_executable=tmp_path / "missing-bwrap",
        )
        == {}
    )


def test_build_plugin_package_subprocess_adapters_accepts_dependency_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bubblewrap_executable = tmp_path / "bwrap"
    bubblewrap_executable.write_text("")
    dependency_policy = PluginPackageDependencyPolicy(
        install_policy="offline_cache",
        allowlist=frozenset({"python:pypi:requests==2.32.0"}),
        cache_dir=tmp_path / "dependency-cache",
    )
    monkeypatch.setattr("agent_hub.plugins.runtime.os.name", "posix")
    monkeypatch.setattr("agent_hub.plugins.runtime.os.access", lambda path, mode: True)

    adapters = build_plugin_package_subprocess_adapters(
        enabled=True,
        adapter_ids=("calendar_python",),
        package_store_dir=tmp_path,
        isolation_backend="bubblewrap",
        bubblewrap_executable=bubblewrap_executable,
        dependency_policy=dependency_policy,
    )

    assert cast(Any, adapters["calendar_python"])._dependency_policy is dependency_policy


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable bit only")
def test_build_plugin_package_subprocess_adapters_requires_executable_launcher(
    tmp_path: Path,
) -> None:
    bubblewrap_executable = tmp_path / "bwrap"
    bubblewrap_executable.write_text("")
    bubblewrap_executable.chmod(0o644)

    assert (
        build_plugin_package_subprocess_adapters(
            enabled=True,
            adapter_ids=("calendar_python",),
            package_store_dir=tmp_path,
            isolation_backend="bubblewrap",
            bubblewrap_executable=bubblewrap_executable,
        )
        == {}
    )


@pytest.mark.skipif(os.name != "posix", reason="bubblewrap registration is POSIX-only")
def test_build_plugin_package_subprocess_adapters_registers_allowed_adapter_ids(
    tmp_path: Path,
) -> None:
    bubblewrap_executable = tmp_path / "bwrap"
    bubblewrap_executable.write_text("")
    bubblewrap_executable.chmod(0o755)
    dependency_policy = PluginPackageDependencyPolicy(
        install_policy="offline_cache",
        allowlist=frozenset({"python:pypi:requests==2.32.0"}),
        cache_dir=tmp_path / "dependency-cache",
    )

    adapters = build_plugin_package_subprocess_adapters(
        enabled=True,
        adapter_ids=("calendar_python", "crm-python"),
        package_store_dir=tmp_path,
        isolation_backend="bubblewrap",
        bubblewrap_executable=bubblewrap_executable,
        timeout_seconds=1,
        max_stdout_bytes=1024,
        dependency_policy=dependency_policy,
    )

    assert tuple(adapters) == ("calendar_python", "crm-python")
    assert cast(Any, adapters["calendar_python"])._dependency_policy is dependency_policy
    calendar_descriptor = cast(Any, adapters["calendar_python"]).descriptor()
    assert calendar_descriptor["id"] == "calendar_python"
    capability_schema = cast(Mapping[str, object], calendar_descriptor["capability_schema"])
    capability_properties = cast(Mapping[str, object], capability_schema["properties"])
    assert capability_properties["sandbox_profile"] == {
        "type": "string",
        "enum": ("local_process",),
    }
    assert calendar_descriptor["capability_contract"] == {
        "schema_version": 1,
        "declared_sandbox_profiles": ("local_process",),
        "runtime_sandbox_profiles": ("local_process", "remote_connector"),
    }
    assert cast(Any, adapters["crm-python"]).descriptor()["id"] == "crm-python"


def test_build_plugin_package_subprocess_adapters_rejects_reserved_adapter_id(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="reserved"):
        build_plugin_package_subprocess_adapters(
            enabled=True,
            adapter_ids=("http_json",),
            package_store_dir=tmp_path,
            isolation_backend="bubblewrap",
            bubblewrap_executable=tmp_path / "bwrap",
        )


async def test_runtime_plugin_service_rechecks_runtime_registered_package_metadata() -> None:
    package_metadata = PluginPackageMetadata.model_construct(
        schema_version=1,
        kind="adapter_package",
        package_version="1.2.3",
        adapter_id="calendar_python",
        sdk_api_version="1.0",
        signature={
            "algorithm": "ed25519",
            "key_id": "calendar-prod",
            "value": "A" * 86,
        },
        signature_verification="not_verified",
        verified_public_key_sha256=None,
        approval_state="pending",
        approval_reason="",
        approved_by=None,
        approved_at=None,
        activation_state="eligible",
        activation_reason="stale eligible state",
        runtime="python",
        entrypoint="adapter/main.py",
        isolation="local_process",
        install_mode="runtime_registered",
    )
    adapter = RecordingPluginAdapter(
        calls=[],
        descriptor_payload={
            "id": "calendar_python",
            "name": "Calendar Python",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {
                "type": "object",
                "properties": {
                    "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                },
                "additionalProperties": True,
            },
            "argument_schema": {"type": "object", "additionalProperties": True},
        },
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    adapter="calendar_python",
                    sandbox_profile="local_process",
                    package_metadata=package_metadata,
                ),
            )
        ),
        adapters={"calendar_python": adapter},
    )

    manifest = service.capability_manifest_source().manifests_for_tenant(TENANT_ID)
    capability_items = cast(tuple[Mapping[str, object], ...], manifest["capabilities"])
    capabilities = {str(item["id"]): item for item in capability_items}

    assert service.is_available(TENANT_ID, "calendar.create_event") is False
    assert capabilities["calendar.create_event"]["available"] is False
    assert capabilities["calendar.create_event"]["availability_reason"] == (
        "plugin_package_signature_unverified"
    )
    with pytest.raises(RuntimeCapabilityError, match="Plugin tool unavailable"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="tester",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="invoke-1",
        )
    assert adapter.calls == []


async def test_runtime_plugin_service_blocks_runtime_registered_package_dependencies() -> None:
    dependency_lock_hash = hashlib.sha256(
        b"python pypi requests==2.32.0\npython pypi zlib==1.0\n"
    ).hexdigest()
    package_metadata = verified_package_with_artifact(
        content_sha256="a" * 64,
        storage_key=f"{TENANT_ID}/calendar/{'a' * 64}",
    ).model_copy(
        update={
            "artifact": None,
            "dependencies": (
                PluginPackageDependency(name="Zlib", version="1.0"),
                PluginPackageDependency(name="requests", version="2.32.0"),
            ),
        }
    )
    adapter = RecordingPluginAdapter(
        calls=[],
        descriptor_payload={
            "id": "calendar_python",
            "name": "Calendar Python",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {
                "type": "object",
                "properties": {
                    "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                },
                "additionalProperties": True,
            },
            "argument_schema": {"type": "object", "additionalProperties": True},
        },
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    adapter="calendar_python",
                    sandbox_profile="local_process",
                    package_metadata=package_metadata,
                    content_sha256="a" * 64,
                ),
            )
        ),
        adapters={"calendar_python": adapter},
    )

    manifest = service.capability_manifest_source().manifests_for_tenant(TENANT_ID)
    capability_items = cast(tuple[Mapping[str, object], ...], manifest["capabilities"])
    capabilities = {str(item["id"]): item for item in capability_items}

    assert service.is_available(TENANT_ID, "calendar.create_event") is False
    assert capabilities["calendar.create_event"]["available"] is False
    assert capabilities["calendar.create_event"]["availability_reason"] == (
        "plugin_package_dependencies_unsupported"
    )
    assert capabilities["calendar.create_event"]["package_dependency_lock"] == {
        "status": "unsupported",
        "install_policy": "not_configured",
        "cache_status": "missing",
        "allowlist_status": "missing",
        "sha256": dependency_lock_hash,
        "dependency_count": 2,
        "dependencies": (
            {
                "kind": "python",
                "source": "pypi",
                "name": "requests",
                "version": "2.32.0",
            },
            {
                "kind": "python",
                "source": "pypi",
                "name": "zlib",
                "version": "1.0",
            },
        ),
    }
    with pytest.raises(RuntimeCapabilityError, match="Plugin tool unavailable"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="tester",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="invoke-1",
        )
    assert adapter.calls == []


async def test_runtime_plugin_service_allows_ready_offline_runtime_registered_dependencies(
    tmp_path: Path,
) -> None:
    dependency_lock_hash = hashlib.sha256(
        b"python pypi requests==2.32.0\npython pypi zlib==1.0\n"
    ).hexdigest()
    write_dependency_cache_manifest(
        tmp_path / "dependency-cache",
        lock_hash=dependency_lock_hash,
        dependencies=[
            {
                "kind": "python",
                "source": "pypi",
                "name": "requests",
                "version": "2.32.0",
            },
            {
                "kind": "python",
                "source": "pypi",
                "name": "zlib",
                "version": "1.0",
            },
        ],
    )
    package_metadata = verified_package_with_artifact(
        content_sha256="a" * 64,
        storage_key=f"{TENANT_ID}/calendar/{'a' * 64}",
    ).model_copy(
        update={
            "artifact": None,
            "dependencies": (
                PluginPackageDependency(name="Zlib", version="1.0"),
                PluginPackageDependency(name="requests", version="2.32.0"),
            ),
        }
    )
    adapter = RecordingPluginAdapter(
        calls=[],
        descriptor_payload={
            "id": "calendar_python",
            "name": "Calendar Python",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {
                "type": "object",
                "properties": {
                    "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                },
                "additionalProperties": True,
            },
            "argument_schema": {"type": "object", "additionalProperties": True},
        },
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    adapter="calendar_python",
                    sandbox_profile="local_process",
                    policy_effect="allow",
                    package_metadata=package_metadata,
                    content_sha256="a" * 64,
                ),
            )
        ),
        adapters={"calendar_python": adapter},
        dependency_policy=PluginPackageDependencyPolicy(
            install_policy="offline_cache",
            allowlist=frozenset(
                {
                    "python:pypi:requests==2.32.0",
                    "python:pypi:zlib==1.0",
                }
            ),
            cache_dir=tmp_path / "dependency-cache",
        ),
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="tester",
        name="calendar.create_event",
        arguments={"title": "review"},
        idempotency_key="invoke-1",
    )
    manifest = service.capability_manifest_source().manifests_for_tenant(TENANT_ID)
    capability_items = cast(tuple[Mapping[str, object], ...], manifest["capabilities"])
    capabilities = {str(item["id"]): item for item in capability_items}

    assert result["ok"] is True
    assert adapter.calls[0][0] == "calendar"
    assert adapter.calls[0][1] == "calendar.create_event"
    assert service.is_available(TENANT_ID, "calendar.create_event") is True
    assert len(service.capability_policy_rules(TENANT_ID)) == 3
    assert capabilities["calendar.create_event"]["available"] is True
    assert capabilities["calendar.create_event"]["availability_reason"] is None
    assert capabilities["calendar.create_event"]["package_dependency_lock"] == {
        "status": "unsupported",
        "install_policy": "offline_cache",
        "cache_status": "present",
        "allowlist_status": "allowed",
        "sha256": dependency_lock_hash,
        "dependency_count": 2,
        "dependencies": (
            {
                "kind": "python",
                "source": "pypi",
                "name": "requests",
                "version": "2.32.0",
            },
            {
                "kind": "python",
                "source": "pypi",
                "name": "zlib",
                "version": "1.0",
            },
        ),
    }


async def test_runtime_plugin_service_blocks_runtime_registered_package_without_adapter() -> None:
    package_metadata = PluginPackageMetadata.model_validate(
        {
            "kind": "adapter_package",
            "package_version": "1.2.3",
            "adapter_id": "calendar_python",
            "sdk_api_version": "1.0",
            "signature": {
                "algorithm": "ed25519",
                "key_id": "calendar-prod",
                "value": "A" * 86,
            },
            "signature_verification": "verified",
            "approval_state": "approved",
            "runtime": "python",
            "entrypoint": "adapter/main.py",
            "isolation": "local_process",
            "install_mode": "runtime_registered",
        }
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    adapter="calendar_python",
                    sandbox_profile="local_process",
                    package_metadata=package_metadata,
                ),
            )
        ),
    )

    manifest = service.capability_manifest_source().manifests_for_tenant(TENANT_ID)
    capability_items = cast(tuple[Mapping[str, object], ...], manifest["capabilities"])
    capabilities = {str(item["id"]): item for item in capability_items}

    assert service.is_available(TENANT_ID, "calendar.create_event") is False
    assert capabilities["calendar.create_event"]["available"] is False
    assert capabilities["calendar.create_event"]["availability_reason"] == (
        "plugin_package_adapter_unavailable"
    )
    with pytest.raises(RuntimeCapabilityError, match="Plugin tool unavailable"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="tester",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="invoke-1",
        )


async def test_runtime_plugin_service_refreshes_stale_runtime_registered_trust() -> None:
    now = 1000.0

    def monotonic() -> float:
        return now

    eligible_package = PluginPackageMetadata.model_validate(
        {
            "kind": "adapter_package",
            "package_version": "1.2.3",
            "adapter_id": "calendar_python",
            "sdk_api_version": "1.0",
            "signature": {
                "algorithm": "ed25519",
                "key_id": "calendar-prod",
                "value": "A" * 86,
            },
            "signature_verification": "verified",
            "approval_state": "approved",
            "runtime": "python",
            "entrypoint": "adapter/main.py",
            "isolation": "local_process",
            "install_mode": "runtime_registered",
        }
    )
    stale_package = eligible_package.model_copy(
        update={
            "signature_verification": "untrusted_key",
            "activation_state": "blocked_untrusted_key",
            "activation_reason": "package signature key is not trusted for this tenant",
        }
    )
    adapter = RecordingPluginAdapter(
        calls=[],
        descriptor_payload={
            "id": "calendar_python",
            "name": "Calendar Python",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {
                "type": "object",
                "properties": {
                    "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                },
                "additionalProperties": True,
            },
            "argument_schema": {"type": "object", "additionalProperties": True},
        },
    )
    admin_service = FakeAdminService(
        (
            plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="local_process",
                package_metadata=eligible_package,
            ),
        )
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        adapters={"calendar_python": adapter},
        cache_ttl_seconds=30,
        monotonic=monotonic,
    )
    admin_service.plugins = (
        plugin(
            "calendar",
            adapter="calendar_python",
            sandbox_profile="local_process",
            package_metadata=stale_package,
        ),
    )

    assert service.is_available(TENANT_ID, "calendar.create_event") is True

    now = 1031.0
    with pytest.raises(RuntimeCapabilityError, match="Plugin tool unavailable"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="tester",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="invoke-1",
        )

    assert admin_service.calls == 2
    assert adapter.calls == []
    assert service.is_available(TENANT_ID, "calendar.create_event") is False


async def test_runtime_plugin_service_refreshes_expired_package_signature_trust_before_ttl() -> None:
    now = 1000.0

    def monotonic() -> float:
        return now

    eligible_package = PluginPackageMetadata.model_validate(
        {
            "kind": "adapter_package",
            "package_version": "1.2.3",
            "adapter_id": "calendar_python",
            "sdk_api_version": "1.0",
            "signature": {
                "algorithm": "ed25519",
                "key_id": "calendar-prod",
                "value": "A" * 86,
            },
            "signature_verification": "verified",
            "signature_trust_expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            "approval_state": "approved",
            "runtime": "python",
            "entrypoint": "adapter/main.py",
            "isolation": "local_process",
            "install_mode": "runtime_registered",
        }
    )
    stale_package = eligible_package.model_copy(
        update={
            "signature_verification": "untrusted_key",
            "activation_state": "blocked_untrusted_key",
            "activation_reason": "package signature key is not trusted for this tenant",
        }
    )
    adapter = RecordingPluginAdapter(
        calls=[],
        descriptor_payload={
            "id": "calendar_python",
            "name": "Calendar Python",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {
                "type": "object",
                "properties": {
                    "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                },
                "additionalProperties": True,
            },
            "argument_schema": {"type": "object", "additionalProperties": True},
        },
    )
    admin_service = FakeAdminService(
        (
            plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="local_process",
                package_metadata=eligible_package,
            ),
        )
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        adapters={"calendar_python": adapter},
        cache_ttl_seconds=3600,
        monotonic=monotonic,
    )
    admin_service.plugins = (
        plugin(
            "calendar",
            adapter="calendar_python",
            sandbox_profile="local_process",
            package_metadata=stale_package,
        ),
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin tool unavailable"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="tester",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="invoke-1",
        )

    assert admin_service.calls == 2
    assert adapter.calls == []
    assert service.is_available(TENANT_ID, "calendar.create_event") is False


async def test_runtime_plugin_service_marks_cached_expired_signature_trust_unavailable_without_reload() -> None:
    expired_package = PluginPackageMetadata.model_validate(
        {
            "kind": "adapter_package",
            "package_version": "1.2.3",
            "adapter_id": "calendar_python",
            "sdk_api_version": "1.0",
            "signature": {
                "algorithm": "ed25519",
                "key_id": "calendar-prod",
                "value": "A" * 86,
            },
            "signature_verification": "verified",
            "signature_trust_expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            "approval_state": "approved",
            "runtime": "python",
            "entrypoint": "adapter/main.py",
            "isolation": "local_process",
            "install_mode": "runtime_registered",
        }
    )
    adapter = RecordingPluginAdapter(
        calls=[],
        descriptor_payload={
            "id": "calendar_python",
            "name": "Calendar Python",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {
                "type": "object",
                "properties": {
                    "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                },
                "additionalProperties": True,
            },
            "argument_schema": {"type": "object", "additionalProperties": True},
        },
    )
    admin_service = FakeAdminService(
        (
            plugin(
                "calendar",
                adapter="calendar_python",
                sandbox_profile="local_process",
                policy_effect="allow",
                package_metadata=expired_package,
            ),
        )
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        adapters={"calendar_python": adapter},
        cache_ttl_seconds=3600,
    )

    manifest = service.capability_manifest_source().manifests_for_tenant(TENANT_ID)
    capability_items = cast(tuple[Mapping[str, object], ...], manifest["capabilities"])
    capabilities = {str(item["id"]): item for item in capability_items}

    assert admin_service.calls == 1
    assert service.is_available(TENANT_ID, "calendar.create_event") is False
    assert service.capability_policy_rules(TENANT_ID) == ()
    assert capabilities["calendar.create_event"]["available"] is False
    assert capabilities["calendar.create_event"]["availability_reason"] == (
        "plugin_package_signature_untrusted"
    )


async def test_runtime_plugin_service_reloads_and_invokes_plugins_for_requested_tenant() -> None:
    class TenantPluginAdminService(FakeAdminService):
        def __init__(self) -> None:
            super().__init__((plugin("bootstrap-calendar"),))
            self.tenant_ids: list[UUID | None] = []

        async def list_plugins(
            self,
            *,
            tenant_id: UUID | None = None,
        ) -> tuple[PluginResourceResponse, ...]:
            self.tenant_ids.append(tenant_id)
            if tenant_id == OTHER_TENANT_ID:
                return (
                    plugin(
                        "tenant-calendar",
                        capability_id="tenant_calendar.create_event",
                    ),
                )
            return await super().list_plugins(tenant_id=tenant_id)

    admin_service = TenantPluginAdminService()
    adapter = RecordingPluginAdapter(calls=[])
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        adapters={"plugin_runtime": adapter},
    )

    await service.reload(OTHER_TENANT_ID)
    result = await service.invoke(
        tenant_id=OTHER_TENANT_ID,
        user_id=OTHER_TENANT_ID,
        run_id=OTHER_TENANT_ID,
        actor="tenant-admin",
        name="tenant_calendar.create_event",
        arguments={"title": "tenant review"},
        idempotency_key="plugin_tenant_1",
    )

    assert admin_service.tenant_ids == [TENANT_ID, OTHER_TENANT_ID]
    assert service.is_available(OTHER_TENANT_ID, "tenant_calendar.create_event") is True
    assert service.is_available(TENANT_ID, "tenant_calendar.create_event") is False
    assert service.is_available(OTHER_TENANT_ID, "calendar.create_event") is False
    assert result == {
        "ok": True,
        "plugin_id": "tenant-calendar",
        "capability_id": "tenant_calendar.create_event",
    }
    assert admin_service.audit_tenant_ids == [OTHER_TENANT_ID]
    assert adapter.calls[0][3].tenant_id == OTHER_TENANT_ID


async def test_runtime_plugin_reload_without_tenant_refreshes_loaded_tenants() -> None:
    admin_service = TenantMappedPluginAdminService(
        {
            TENANT_ID: (),
            OTHER_TENANT_ID: (
                plugin(
                    "tenant-calendar",
                    capability_id="tenant_calendar.create_event",
                ),
            ),
        }
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
    )
    await service.ensure_tenant_loaded(OTHER_TENANT_ID)

    admin_service.plugins_by_tenant[OTHER_TENANT_ID] = ()
    await service.reload()

    assert admin_service.tenant_ids == [
        TENANT_ID,
        OTHER_TENANT_ID,
        TENANT_ID,
        OTHER_TENANT_ID,
    ]
    assert service.manifests_for_tenant(OTHER_TENANT_ID) == {
        "schema_version": 1,
        "capabilities": (),
    }


async def test_runtime_plugin_service_coalesces_concurrent_reload_for_same_tenant() -> None:
    admin_service = BlockingTenantMappedPluginAdminService(
        {
            OTHER_TENANT_ID: (
                plugin(
                    "tenant-calendar",
                    capability_id="tenant_calendar.create_event",
                ),
            ),
        }
    )
    service = RuntimePluginService(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
    )

    reloads = [
        asyncio.create_task(service.reload(OTHER_TENANT_ID)),
        asyncio.create_task(service.reload(OTHER_TENANT_ID)),
    ]
    await admin_service.entered.wait()
    admin_service.release.set()
    await asyncio.gather(*reloads)

    assert admin_service.tenant_ids == [OTHER_TENANT_ID]


async def test_runtime_plugin_reload_waiter_cancellation_does_not_cancel_shared_reload() -> None:
    admin_service = BlockingTenantMappedPluginAdminService(
        {
            OTHER_TENANT_ID: (
                plugin(
                    "tenant-calendar",
                    capability_id="tenant_calendar.create_event",
                ),
            ),
        }
    )
    service = RuntimePluginService(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
    )

    leader = asyncio.create_task(service.reload(OTHER_TENANT_ID))
    await admin_service.entered.wait()
    follower = asyncio.create_task(service.reload(OTHER_TENANT_ID))
    await asyncio.sleep(0)

    follower.cancel()
    try:
        await follower
    except asyncio.CancelledError:
        pass
    admin_service.release.set()
    await leader

    assert admin_service.tenant_ids == [OTHER_TENANT_ID]
    assert service.is_available(OTHER_TENANT_ID, "tenant_calendar.create_event") is True


async def test_runtime_plugin_reload_cleans_inflight_task_after_only_waiter_cancelled() -> None:
    admin_service = BlockingTenantMappedPluginAdminService(
        {
            OTHER_TENANT_ID: (
                plugin(
                    "tenant-calendar",
                    capability_id="tenant_calendar.create_event",
                ),
            ),
        }
    )
    service = RuntimePluginService(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
    )

    waiter = asyncio.create_task(service.reload(OTHER_TENANT_ID))
    await admin_service.entered.wait()
    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass
    admin_service.release.set()
    for _ in range(10):
        if service._reload_tasks_by_tenant == {}:
            break
        await asyncio.sleep(0)

    assert service._reload_tasks_by_tenant == {}
    assert service.is_available(OTHER_TENANT_ID, "tenant_calendar.create_event") is True


async def test_runtime_plugin_reload_failure_does_not_block_later_retry() -> None:
    admin_service = FailingOnceTenantMappedPluginAdminService(
        {
            OTHER_TENANT_ID: (
                plugin(
                    "tenant-calendar",
                    capability_id="tenant_calendar.create_event",
                ),
            ),
        }
    )
    service = RuntimePluginService(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
    )

    await service.reload(OTHER_TENANT_ID)
    await service.reload(OTHER_TENANT_ID)

    assert admin_service.tenant_ids == [OTHER_TENANT_ID, OTHER_TENANT_ID]
    assert service.is_available(OTHER_TENANT_ID, "tenant_calendar.create_event") is True


async def test_runtime_plugin_manifest_includes_capability_schemas() -> None:
    admin_service = FakeAdminService(
        (
            PluginResourceResponse(
                **PluginResourceRequest(
                    id="calendar",
                    name="calendar",
                    capabilities=[
                        PluginCapabilityRequest(
                            id="calendar.create_event",
                            adapter="http_json",
                            permission_class="calendar.write",
                            sandbox_profile="remote_connector",
                            input_schema={
                                "type": "object",
                                "required": ("title",),
                                "properties": {"title": {"type": "string"}},
                            },
                            output_schema={
                                "type": "object",
                                "properties": {"remote_id": {"type": "string"}},
                            },
                        )
                    ],
                ).model_dump(),
                status="running",
                health="healthy",
                last_error_type=None,
            ),
        )
    )

    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
    )

    manifest = service.capability_manifest_source().manifests_for_tenant(TENANT_ID)
    capability = cast(tuple[Mapping[str, object], ...], manifest["capabilities"])[0]

    assert capability["input_schema"] == {
        "type": "object",
        "required": ("title",),
        "properties": {"title": {"type": "string"}},
    }
    assert capability["output_schema"] == {
        "type": "object",
        "properties": {"remote_id": {"type": "string"}},
    }


def test_http_json_plugin_adapter_exposes_safe_descriptor() -> None:
    descriptor = HttpJsonPluginAdapter().descriptor()

    assert descriptor["id"] == "http_json"
    assert descriptor == adapter_descriptor_with_contract(http_json_adapter_descriptor())
    assert "Authorization" not in repr(descriptor)
    resource_schema = cast(Mapping[str, object], descriptor["resource_schema"])
    assert resource_schema["required"] == ("endpoint_url", "domain_allowlist")
    properties = cast(Mapping[str, object], resource_schema["properties"])
    assert "endpoint_url" in properties
    assert "credential_ref" in properties
    assert "credential_header" in properties
    capability_schema = cast(Mapping[str, object], descriptor["capability_schema"])
    capability_properties = cast(Mapping[str, object], capability_schema["properties"])
    assert capability_properties["policy_effect"] == {
        "type": "string",
        "enum": ("inherit", "allow", "require_approval", "deny"),
        "default": "inherit",
    }


async def test_runtime_plugin_service_lists_registered_adapter_descriptors() -> None:
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(()),
        adapters={"plugin_runtime": RecordingPluginAdapter([])},
    )

    descriptors = {str(item["id"]): item for item in service.adapter_descriptors()}

    assert "http_json" in descriptors
    assert descriptors["plugin_runtime"]["id"] == "plugin_runtime"
    assert descriptors["plugin_runtime"]["resource_schema"] == {
        "type": "object",
        "additionalProperties": True,
    }
    assert descriptors["plugin_runtime"]["capability_contract"] == {
        "schema_version": 1,
        "declared_sandbox_profiles": (),
        "runtime_sandbox_profiles": ("remote_connector",),
    }


async def test_runtime_plugin_service_invoke_fails_closed_until_backend_is_installed() -> None:
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService((plugin("calendar"),)),
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin backend unavailable"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="plugin_1",
        )


async def test_runtime_plugin_service_invokes_registered_adapter_for_running_capability() -> None:
    adapter = RecordingPluginAdapter([])
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService((plugin("calendar"),)),
        adapters={"plugin_runtime": adapter},
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="scheduler",
        name="calendar.create_event",
        arguments={"title": "review"},
        idempotency_key="plugin_1",
    )

    assert result == {
        "ok": True,
        "plugin_id": "calendar",
        "capability_id": "calendar.create_event",
    }
    assert len(adapter.calls) == 1
    plugin_id, capability_id, arguments, context = adapter.calls[0]
    assert plugin_id == "calendar"
    assert capability_id == "calendar.create_event"
    assert arguments == {"title": "review"}
    assert context.tenant_id == TENANT_ID
    assert context.user_id == TENANT_ID
    assert context.run_id == TENANT_ID
    assert context.actor == "scheduler"
    assert context.idempotency_key == "plugin_1"


async def test_runtime_plugin_service_allows_remote_connector_sandbox_profile() -> None:
    adapter = RecordingPluginAdapter([])
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    sandbox_profile="remote_connector",
                ),
            )
        ),
        adapters={"plugin_runtime": adapter},
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="scheduler",
        name="calendar.create_event",
        arguments={"title": "review"},
        idempotency_key="plugin_1",
    )

    assert result["ok"] is True
    assert len(adapter.calls) == 1


async def test_runtime_plugin_service_allows_adapter_declared_sandbox_profile() -> None:
    adapter = RecordingPluginAdapter(
        [],
        descriptor_payload={
            "id": "local_runtime",
            "name": "Local Runtime",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {
                "type": "object",
                "properties": {
                    "sandbox_profile": {
                        "type": "string",
                        "enum": ("in_process",),
                    },
                },
                "additionalProperties": True,
            },
            "argument_schema": {"type": "object", "additionalProperties": True},
        },
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    adapter="local_runtime",
                    sandbox_profile="in_process",
                ),
            )
        ),
        adapters={"local_runtime": adapter},
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="scheduler",
        name="calendar.create_event",
        arguments={"title": "review"},
        idempotency_key="plugin_1",
    )

    assert result["ok"] is True
    assert len(adapter.calls) == 1


async def test_runtime_plugin_service_rejects_http_read_sandbox_profile_without_adapter_declaration() -> None:
    adapter = RecordingPluginAdapter([])
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService((plugin("calendar", sandbox_profile="http_read"),)),
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin sandbox profile unsupported"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []


async def test_runtime_plugin_service_rejects_unknown_adapter_declared_sandbox_profile() -> None:
    adapter = RecordingPluginAdapter(
        [],
        descriptor_payload={
            "id": "host_runtime",
            "name": "Host Runtime",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {
                "type": "object",
                "properties": {
                    "sandbox_profile": {
                        "type": "string",
                        "enum": ("host_shell",),
                    },
                },
                "additionalProperties": True,
            },
            "argument_schema": {"type": "object", "additionalProperties": True},
        },
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    adapter="host_runtime",
                    sandbox_profile="host_shell",
                ),
            )
        ),
        adapters={"host_runtime": adapter},
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin sandbox profile unsupported"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []


async def test_runtime_plugin_service_rejects_default_plugin_sandbox_profile_before_adapter() -> None:
    adapter = RecordingPluginAdapter([])
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    sandbox_profile="plugin",
                ),
            )
        ),
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin sandbox profile unsupported"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []


async def test_runtime_plugin_service_exposes_policy_parts_from_permission_class() -> None:
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService((plugin("calendar"),)),
        adapters={"plugin_runtime": RecordingPluginAdapter([])},
    )

    assert service.capability_policy_parts(TENANT_ID, "calendar.create_event") == (
        "calendar",
        "write",
        "plugin/calendar/calendar/create_event",
    )


async def test_runtime_plugin_service_preserves_generic_plugin_policy_parts() -> None:
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    permission_class="plugin.use",
                ),
            )
        ),
        adapters={"plugin_runtime": RecordingPluginAdapter([])},
    )

    assert service.capability_policy_parts(TENANT_ID, "calendar.create_event") == (
        "plugin",
        "use",
        "plugin/calendar/create_event",
    )


async def test_runtime_plugin_service_resolves_aliases_to_canonical_plugin_policy_parts() -> None:
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    permission_class="plugin.use",
                    policy_effect="deny",
                ),
            )
        ),
        adapters={"plugin_runtime": RecordingPluginAdapter([])},
    )

    assert service.capability_policy_parts(TENANT_ID, "calendar_create") == (
        "plugin",
        "use",
        "plugin/calendar/create_event",
    )


async def test_runtime_stack_rejects_plugin_envelope_sandbox_mismatch_from_runtime_service() -> None:
    adapter = RecordingPluginAdapter([])
    plugin_service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService((plugin("calendar"),)),
        adapters={"plugin_runtime": adapter},
    )
    runtime = UnavailableRuntimeCapabilityGateway()
    policy = AllowingPolicyGateway()
    gateway = HarnessToolGateway(
        runtime,
        policy_gateway=policy,
        plugin_backend=plugin_service,
    )

    result = await gateway.invoke(
        TENANT_ID,
        HarnessToolCallRequest(
            run_id=TENANT_ID,
            actor="scheduler",
            tool_name="calendar.create_event",
            arguments={"title": "review"},
            approval_required=False,
            sandbox="read_only",
            idempotency_key="plugin_1",
        ),
        user_id=TENANT_ID,
        role=Role.OPERATOR,
    )

    assert result.status == "failed"
    assert result.failure_reason == "tool sandbox does not match declared sandbox profile"
    assert adapter.calls == []
    assert policy.requests == 0
    assert runtime.calls == []


async def test_runtime_plugin_service_exposes_explicit_policy_effect_rules() -> None:
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    permission_class="calendar.write",
                    policy_effect="require_approval",
                ),
            )
        ),
        adapters={"plugin_runtime": RecordingPluginAdapter([])},
    )

    assert service.capability_policy_rules(TENANT_ID) == (
        CapabilityRule(
            tenant_id=TENANT_ID,
            role=Role.SUPER_ADMIN,
            agent_id=None,
            capability="calendar",
            operation="write",
            resource_prefix="plugin/calendar/calendar/create_event",
            effect=PolicyEffect.REQUIRE_APPROVAL,
        ),
        CapabilityRule(
            tenant_id=TENANT_ID,
            role=Role.ADMIN,
            agent_id=None,
            capability="calendar",
            operation="write",
            resource_prefix="plugin/calendar/calendar/create_event",
            effect=PolicyEffect.REQUIRE_APPROVAL,
        ),
        CapabilityRule(
            tenant_id=TENANT_ID,
            role=Role.OPERATOR,
            agent_id=None,
            capability="calendar",
            operation="write",
            resource_prefix="plugin/calendar/calendar/create_event",
            effect=PolicyEffect.REQUIRE_APPROVAL,
        ),
    )


async def test_runtime_plugin_service_omits_inherited_policy_effect_rules() -> None:
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService((plugin("calendar", policy_effect="inherit"),)),
        adapters={"plugin_runtime": RecordingPluginAdapter([])},
    )

    assert service.capability_policy_rules(TENANT_ID) == ()


async def test_runtime_plugin_service_records_invocation_audit_without_payloads() -> None:
    adapter = RecordingPluginAdapter([], result={"remote_id": "evt_1", "secret": "do-not-leak"})
    admin_service = FakeAdminService(
        (
            plugin(
                "calendar",
                permission_class="plugin.use",
                replay_safe=True,
            ),
        )
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        adapters={"plugin_runtime": adapter},
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="scheduler",
        name="calendar.create_event",
        arguments={"title": "Mofang review", "secret": "input-do-not-leak"},
        idempotency_key="plugin_1",
    )

    assert result == {"remote_id": "evt_1", "secret": "do-not-leak"}
    assert admin_service.audit_events == [
        {
            "actor": "scheduler",
            "action": "plugin.invoke.succeeded",
            "resource": "plugin:calendar:calendar.create_event",
            "details": {
                "plugin_id": "calendar",
                "capability_id": "calendar.create_event",
                "adapter": "plugin_runtime",
                "permission_class": "plugin.use",
                "sandbox_profile": "remote_connector",
                "replay_safe": True,
                "run_id": str(TENANT_ID),
                "user_id": str(TENANT_ID),
                "idempotency_key": "plugin_1",
            },
        }
    ]
    assert "Mofang review" not in repr(admin_service.audit_events)
    assert "input-do-not-leak" not in repr(admin_service.audit_events)
    assert "do-not-leak" not in repr(admin_service.audit_events)


async def test_runtime_plugin_service_records_validation_failure_audit_without_payloads() -> None:
    adapter = RecordingPluginAdapter([])
    admin_service = FakeAdminService(
        (
            plugin(
                "calendar",
                input_schema={
                    "type": "object",
                    "required": ("title",),
                    "properties": {"title": {"type": "string"}},
                    "additionalProperties": False,
                },
            ),
        )
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin arguments do not match input schema"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": 123, "secret": "input-do-not-leak"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []
    assert admin_service.audit_events == [
        {
            "actor": "scheduler",
            "action": "plugin.invoke.failed",
            "resource": "plugin:calendar:calendar.create_event",
            "details": {
                "plugin_id": "calendar",
                "capability_id": "calendar.create_event",
                "adapter": "plugin_runtime",
                "permission_class": "calendar.write",
                "sandbox_profile": "remote_connector",
                "replay_safe": False,
                "run_id": str(TENANT_ID),
                "user_id": str(TENANT_ID),
                "idempotency_key": "plugin_1",
            },
        }
    ]
    assert "input-do-not-leak" not in repr(admin_service.audit_events)


async def test_runtime_plugin_service_records_missing_adapter_audit() -> None:
    admin_service = FakeAdminService((plugin("calendar"),))
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin backend unavailable"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "Mofang review", "secret": "input-do-not-leak"},
            idempotency_key="plugin_1",
        )

    assert admin_service.audit_events == [
        {
            "actor": "scheduler",
            "action": "plugin.invoke.failed",
            "resource": "plugin:calendar:calendar.create_event",
            "details": {
                "plugin_id": "calendar",
                "capability_id": "calendar.create_event",
                "adapter": "plugin_runtime",
                "permission_class": "calendar.write",
                "sandbox_profile": "remote_connector",
                "replay_safe": False,
                "run_id": str(TENANT_ID),
                "user_id": str(TENANT_ID),
                "idempotency_key": "plugin_1",
            },
        }
    ]
    assert "input-do-not-leak" not in repr(admin_service.audit_events)


async def test_runtime_plugin_service_rejects_unsupported_sandbox_profile_before_adapter() -> None:
    adapter = RecordingPluginAdapter([])
    admin_service = FakeAdminService(
        (
            plugin(
                "calendar",
                sandbox_profile="host_shell",
            ),
        )
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin sandbox profile unsupported"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "Mofang review", "secret": "input-do-not-leak"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []
    assert admin_service.audit_events == [
        {
            "actor": "scheduler",
            "action": "plugin.invoke.failed",
            "resource": "plugin:calendar:calendar.create_event",
            "details": {
                "plugin_id": "calendar",
                "capability_id": "calendar.create_event",
                "adapter": "plugin_runtime",
                "permission_class": "calendar.write",
                "sandbox_profile": "host_shell",
                "replay_safe": False,
                "run_id": str(TENANT_ID),
                "user_id": str(TENANT_ID),
                "idempotency_key": "plugin_1",
            },
        }
    ]
    assert "Mofang review" not in repr(admin_service.audit_events)
    assert "input-do-not-leak" not in repr(admin_service.audit_events)


async def test_runtime_plugin_service_allows_adapter_declared_http_read_sandbox_profile() -> None:
    adapter = RecordingPluginAdapter(
        [],
        descriptor_payload={
            "id": "plugin_runtime",
            "name": "Plugin Runtime",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {
                "type": "object",
                "properties": {
                    "sandbox_profile": {
                        "type": "string",
                        "enum": ("http_read",),
                    }
                },
                "additionalProperties": True,
            },
            "argument_schema": {"type": "object", "additionalProperties": True},
        },
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService((plugin("calendar", sandbox_profile="http_read"),)),
        adapters={"plugin_runtime": adapter},
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="scheduler",
        name="calendar.create_event",
        arguments={"title": "Mofang review"},
        idempotency_key="plugin_1",
    )

    assert result["ok"] is True
    assert adapter.calls[0][1] == "calendar.create_event"


async def test_runtime_plugin_service_allows_adapter_declared_local_process_sandbox_profile() -> None:
    adapter = RecordingPluginAdapter(
        [],
        descriptor_payload={
            "id": "plugin_runtime",
            "name": "Plugin Runtime",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {
                "type": "object",
                "properties": {
                    "sandbox_profile": {
                        "type": "string",
                        "enum": ("local_process",),
                    }
                },
                "additionalProperties": True,
            },
            "argument_schema": {"type": "object", "additionalProperties": True},
        },
    )
    admin_service = FakeAdminService((plugin("calendar", sandbox_profile="local_process"),))
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        adapters={"plugin_runtime": adapter},
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="scheduler",
        name="calendar.create_event",
        arguments={"title": "Mofang review"},
        idempotency_key="plugin_1",
    )

    assert result["ok"] is True
    assert len(adapter.calls) == 1


async def test_runtime_plugin_service_rejects_arguments_that_violate_input_schema() -> None:
    adapter = RecordingPluginAdapter([])
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    input_schema={
                        "type": "object",
                        "required": ("title",),
                        "properties": {"title": {"type": "string"}},
                        "additionalProperties": False,
                    },
                ),
            )
        ),
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(
        RuntimeCapabilityError,
        match="Plugin arguments do not match input schema",
    ) as exc_info:
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": 123, "secret": "do-not-leak"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []
    assert "do-not-leak" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


async def test_runtime_plugin_service_rejects_arguments_that_violate_adapter_argument_schema() -> None:
    adapter = RecordingPluginAdapter(
        [],
        descriptor_payload={
            "id": "plugin_runtime",
            "name": "Plugin Runtime",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {"type": "object", "additionalProperties": True},
            "argument_schema": {
                "type": "object",
                "required": ("query",),
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    )
    admin_service = FakeAdminService((plugin("calendar"),))
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(
        RuntimeCapabilityError,
        match="Plugin arguments do not match adapter schema",
    ) as exc_info:
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "review", "secret": "do-not-leak"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []
    assert "do-not-leak" not in str(exc_info.value)
    assert admin_service.audit_events[0]["action"] == "plugin.invoke.failed"


async def test_runtime_plugin_service_rejects_invalid_adapter_argument_schema_before_execution() -> None:
    adapter = RecordingPluginAdapter(
        [],
        descriptor_payload={
            "id": "plugin_runtime",
            "name": "Plugin Runtime",
            "description": None,
            "resource_schema": {"type": "object", "additionalProperties": True},
            "capability_schema": {"type": "object", "additionalProperties": True},
            "argument_schema": {
                "type": "object",
                "properties": {"query": {"$ref": "#/$defs/query"}},
            },
        },
    )
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService((plugin("calendar"),)),
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(
        RuntimeCapabilityError,
        match="Plugin adapter argument schema is invalid",
    ) as exc_info:
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"query": "review"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


async def test_runtime_plugin_service_rejects_invalid_input_schema_before_adapter_execution() -> None:
    adapter = RecordingPluginAdapter([])
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (plugin("calendar", input_schema={"type": "not-a-json-schema-type"}),)
        ),
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin input schema is invalid") as exc_info:
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


async def test_runtime_plugin_service_rejects_input_schema_references() -> None:
    adapter = RecordingPluginAdapter([])
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (plugin("calendar", input_schema={"properties": {"title": {"$ref": "#/missing"}}}),)
        ),
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin input schema is invalid") as exc_info:
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


async def test_runtime_plugin_service_rejects_results_that_violate_output_schema() -> None:
    adapter = RecordingPluginAdapter([], result={"secret": "do-not-leak", "remote_id": 123})
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    output_schema={
                        "type": "object",
                        "required": ("remote_id",),
                        "properties": {"remote_id": {"type": "string"}},
                        "additionalProperties": False,
                    },
                ),
            )
        ),
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(
        RuntimeCapabilityError,
        match="Plugin result does not match output schema",
    ) as exc_info:
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="plugin_1",
        )

    assert len(adapter.calls) == 1
    assert "do-not-leak" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


async def test_runtime_plugin_service_rejects_invalid_output_schema_before_adapter_execution() -> None:
    adapter = RecordingPluginAdapter([], result={"remote_id": "evt_1"})
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (plugin("calendar", output_schema={"type": "not-a-json-schema-type"}),)
        ),
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin output schema is invalid") as exc_info:
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize("reference_keyword", ("$ref", "$dynamicRef", "$recursiveRef"))
async def test_runtime_plugin_service_rejects_output_schema_references(
    reference_keyword: str,
) -> None:
    adapter = RecordingPluginAdapter([], result={"remote_id": "evt_1"})
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    output_schema={"properties": {"remote_id": {reference_keyword: "#/missing"}}},
                ),
            )
        ),
        adapters={"plugin_runtime": adapter},
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin output schema is invalid") as exc_info:
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


async def test_runtime_plugin_service_rejects_unavailable_plugin_capability() -> None:
    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (plugin("calendar", status="stopped", health="stopped"),)
        ),
        adapters={"plugin_runtime": RecordingPluginAdapter([])},
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin tool unavailable"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "review"},
            idempotency_key="plugin_1",
        )


async def test_http_json_plugin_adapter_posts_context_to_allowed_endpoint() -> None:
    posts: list[tuple[str, Mapping[str, JsonValue], float, Mapping[str, str]]] = []

    async def post_json(
        url: str,
        payload: Mapping[str, JsonValue],
        timeout_seconds: float,
        headers: Mapping[str, str],
    ) -> Mapping[str, JsonValue]:
        posts.append((url, payload, timeout_seconds, headers))
        return {"created": True, "remote_id": "evt_1"}

    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    adapter="http_json",
                    endpoint_url="https://plugins.example/invoke",
                    domain_allowlist=("plugins.example",),
                    timeout_seconds=3,
                ),
            )
        ),
        adapters={"http_json": HttpJsonPluginAdapter(post_json=post_json)},
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="scheduler",
        name="calendar.create_event",
        arguments={"title": "review"},
        idempotency_key="plugin_1",
    )

    assert result == {"created": True, "remote_id": "evt_1"}
    assert len(posts) == 1
    url, payload, timeout_seconds, headers = posts[0]
    assert url == "https://plugins.example/invoke"
    assert timeout_seconds == 3
    assert headers == {}
    assert payload["plugin_id"] == "calendar"
    assert payload["capability_id"] == "calendar.create_event"
    assert payload["arguments"] == {"title": "review"}
    assert payload["context"] == {
        "tenant_id": str(TENANT_ID),
        "user_id": str(TENANT_ID),
        "run_id": str(TENANT_ID),
        "actor": "scheduler",
        "idempotency_key": "plugin_1",
    }


async def test_http_json_plugin_adapter_posts_descriptor_configs_to_allowed_endpoint() -> None:
    posts: list[tuple[str, Mapping[str, JsonValue], float, Mapping[str, str]]] = []

    async def post_json(
        url: str,
        payload: Mapping[str, JsonValue],
        timeout_seconds: float,
        headers: Mapping[str, str],
    ) -> Mapping[str, JsonValue]:
        posts.append((url, payload, timeout_seconds, headers))
        return {"indexed": True}

    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "search",
                    adapter="http_json",
                    capability_id="search.query",
                    endpoint_url="https://plugins.example/invoke",
                    domain_allowlist=("plugins.example",),
                    resource_config={
                        "base_url": "https://search.internal",
                        "indexes": ("docs", "tickets"),
                    },
                    capability_config={
                        "method": "semantic",
                        "max_results": 8,
                    },
                ),
            )
        ),
        adapters={"http_json": HttpJsonPluginAdapter(post_json=post_json)},
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="scheduler",
        name="search.query",
        arguments={"query": "adapter contract"},
        idempotency_key="plugin_1",
    )

    assert result == {"indexed": True}
    assert len(posts) == 1
    payload = posts[0][1]
    assert payload["resource_config"] == {
        "base_url": "https://search.internal",
        "indexes": ["docs", "tickets"],
    }
    assert payload["capability_config"] == {
        "method": "semantic",
        "max_results": 8,
    }


async def test_http_json_plugin_adapter_requires_allowed_endpoint_domain() -> None:
    async def post_json(
        url: str,
        payload: Mapping[str, JsonValue],
        timeout_seconds: float,
        headers: Mapping[str, str],
    ) -> Mapping[str, JsonValue]:
        del url, payload, timeout_seconds, headers
        return {"created": True}

    adapter = HttpJsonPluginAdapter(post_json=post_json)
    target = plugin(
        "calendar",
        adapter="http_json",
        endpoint_url="https://plugins.example/invoke",
        domain_allowlist=("api.example",),
    )

    with pytest.raises(RuntimeCapabilityError, match="Plugin endpoint not allowed"):
        await adapter.invoke(
            plugin=target,
            capability=target.capabilities[0],
            arguments={"title": "review"},
            context=PluginInvocationContext(
                tenant_id=TENANT_ID,
                user_id=TENANT_ID,
                run_id=TENANT_ID,
                actor="scheduler",
                idempotency_key="plugin_1",
            ),
        )


async def test_http_json_plugin_adapter_resolves_secret_into_configured_header() -> None:
    posts: list[tuple[str, Mapping[str, JsonValue], float, Mapping[str, str]]] = []
    resolved_refs: list[str] = []

    async def post_json(
        url: str,
        payload: Mapping[str, JsonValue],
        timeout_seconds: float,
        headers: Mapping[str, str],
    ) -> Mapping[str, JsonValue]:
        posts.append((url, payload, timeout_seconds, headers))
        return {"ok": True}

    async def resolve_secret(ref: str) -> str:
        resolved_refs.append(ref)
        return "sk-plugin-secret"

    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FakeAdminService(
            (
                plugin(
                    "calendar",
                    adapter="http_json",
                    endpoint_url="https://plugins.example/invoke",
                    domain_allowlist=("plugins.example",),
                    credential_ref="secret://calendar",
                    credential_header="X-Plugin-Key",
                    credential_scheme="",
                ),
            )
        ),
        adapters={
            "http_json": HttpJsonPluginAdapter(
                post_json=post_json,
                secret_resolver=resolve_secret,
            )
        },
    )

    result = await service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="scheduler",
        name="calendar.create_event",
        arguments={"title": "review"},
        idempotency_key="plugin_1",
    )

    assert result == {"ok": True}
    assert resolved_refs == ["secret://calendar"]
    assert posts[0][3] == {"X-Plugin-Key": "sk-plugin-secret"}
    assert "sk-plugin-secret" not in str(posts[0][1])


async def test_runtime_plugin_service_fails_closed_when_admin_listing_fails() -> None:
    class FailingAdminService:
        async def list_plugins(
            self,
            *,
            tenant_id: UUID | None = None,
        ) -> tuple[PluginResourceResponse, ...]:
            del tenant_id
            raise RuntimeError("raw plugin db failure")

    service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=FailingAdminService(),
    )

    assert service.manifests_for_tenant(TENANT_ID) == {"schema_version": 1, "capabilities": ()}
    assert service.is_available(TENANT_ID, "calendar.create_event") is False
