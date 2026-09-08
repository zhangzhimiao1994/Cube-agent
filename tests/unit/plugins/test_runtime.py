from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

import pytest

from agent_hub.api.routers.admin import (
    PluginCapabilityRequest,
    PluginPackageMetadata,
    PluginResourceRequest,
    PluginResourceResponse,
)
from agent_hub.auth.models import Role
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.capabilities.types import PolicyEffect
from agent_hub.plugins.contracts import (
    adapter_descriptor_with_contract,
    http_json_adapter_descriptor,
)
from agent_hub.plugins.runtime import (
    HttpJsonPluginAdapter,
    PluginInvocationContext,
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
            "isolation": "in_process",
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
                    "sandbox_profile": {"type": "string", "enum": ("in_process",)}
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
                    sandbox_profile="in_process",
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
        isolation="in_process",
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
                    "sandbox_profile": {"type": "string", "enum": ("in_process",)}
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
                    sandbox_profile="in_process",
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
        "plugin_package_not_eligible"
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
            "isolation": "in_process",
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
                    sandbox_profile="in_process",
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
        "plugin_package_not_eligible"
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
            "isolation": "in_process",
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
                    "sandbox_profile": {"type": "string", "enum": ("in_process",)}
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
                sandbox_profile="in_process",
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
            sandbox_profile="in_process",
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


async def test_runtime_plugin_service_rejects_adapter_declared_local_process_sandbox_profile() -> None:
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

    with pytest.raises(RuntimeCapabilityError, match="Plugin sandbox profile unsupported"):
        await service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="scheduler",
            name="calendar.create_event",
            arguments={"title": "Mofang review"},
            idempotency_key="plugin_1",
        )

    assert adapter.calls == []
    details = cast(dict[str, object], admin_service.audit_events[0]["details"])
    assert details["sandbox_profile"] == "local_process"


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
