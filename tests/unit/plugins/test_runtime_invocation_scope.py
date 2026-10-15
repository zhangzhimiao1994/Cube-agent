from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from agent_hub.api.routers.admin import (
    PersistentAdminResourceService,
    PluginCapabilityRequest,
    PluginPackageMetadata,
    PluginResourceResponse,
)
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins.runtime import PluginInvocationContext, RuntimePluginService
from agent_hub.runtime.contracts import JsonValue

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT = UUID("22222222-2222-4222-8222-222222222222")


def plugin(*, packaged: bool = True) -> PluginResourceResponse:
    package = PluginPackageMetadata.model_validate({
        "kind": "adapter_package", "package_version": "1.0.0", "adapter_id": "test_python",
        "sdk_api_version": "1.0", "runtime": "python", "entrypoint": "adapter/main.py",
        "isolation": "local_process", "install_mode": "runtime_registered",
        "signature": {"algorithm": "ed25519", "key_id": "test-key", "value": "A" * 86},
        "signature_verification": "verified", "approval_state": "approved",
        "artifact": {"storage_key": f"{TENANT}/test-plugin/{'a' * 64}",
                     "content_sha256": "a" * 64, "file_count": 1, "total_size_bytes": 10,
                     "stored_at": "2026-10-15T00:00:00Z", "quarantine_state": "stored"},
    }) if packaged else None
    return PluginResourceResponse(
        id="test-plugin", name="Test", enabled=True, status="running", health="healthy",
        capabilities=[PluginCapabilityRequest(
            id="test.action", adapter="test_python", sandbox_profile="local_process",
            output_schema={"type": "object", "required": ("ok",),
                           "properties": {"ok": {"const": True}}},
        )],
        package_metadata=package, content_sha256="a" * 64 if packaged else None,
    )


class LegacyAdmin:
    def __init__(self, cached: PluginResourceResponse) -> None:
        self.cached = cached
        self.loads = 0
        self.load_failed = False

    async def list_plugins(
        self, *, tenant_id: UUID | None = None,
    ) -> tuple[PluginResourceResponse, ...]:
        self.loads += 1
        if self.load_failed:
            raise RuntimeError("PRIVATE reload failure")
        return (self.cached,)


class ScopedAdmin(LegacyAdmin):
    def __init__(self, cached: PluginResourceResponse) -> None:
        super().__init__(cached)
        self.current: PluginResourceResponse | None = cached.model_copy(deep=True)
        self.proof_live = True
        self.scope_failed = False
        self.active = False
        self.lock = asyncio.Lock()
        self.entered: list[UUID] = []
        self.exit_errors: list[type[BaseException] | None] = []
        self.audit_scope_states: list[bool] = []

    async def record_audit_event(self, **kwargs: object) -> object:
        self.audit_scope_states.append(self.active)
        return {}

    @asynccontextmanager
    async def plugin_invocation_scope(
        self, plugin: PluginResourceResponse, *, tenant_id: UUID,
    ) -> AsyncIterator[None]:
        async with self.lock:
            self.entered.append(tenant_id)
            if (self.scope_failed or not self.proof_live or self.current is None
                or self.current.model_dump(mode="json") != plugin.model_dump(mode="json")):
                raise RuntimeError("PRIVATE proof or registration failure")
            self.active = True
            try:
                yield
            except BaseException as error:
                self.exit_errors.append(type(error))
                raise
            else:
                self.exit_errors.append(None)
            finally:
                self.active = False


class Adapter:
    def __init__(self, authority: ScopedAdmin | None = None) -> None:
        self.authority = authority
        self.calls = 0
        self.inside_scope: list[bool] = []
        self.result: Mapping[str, JsonValue] = {"ok": True}
        self.failure: Exception | None = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    def descriptor(self) -> Mapping[str, JsonValue]:
        return {
            "id": "test_python", "resource_schema": {"type": "object"},
            "capability_schema": {"type": "object", "properties": {
                "sandbox_profile": {"type": "string", "enum": ("local_process",)}}},
            "argument_schema": {"type": "object"},
        }

    async def invoke(
        self, *, plugin: PluginResourceResponse, capability: PluginCapabilityRequest,
        arguments: Mapping[str, JsonValue], context: PluginInvocationContext,
    ) -> Mapping[str, JsonValue]:
        self.calls += 1
        if self.authority is not None:
            self.inside_scope.append(self.authority.active)
        self.started.set()
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return self.result


async def invoke(service: RuntimePluginService, tenant: UUID = TENANT) -> Mapping[str, JsonValue]:
    return await service.invoke(
        tenant_id=tenant, user_id=tenant, run_id=tenant, actor="test",
        name="test.action", arguments={}, idempotency_key="test-invocation",
    )


def runtime(admin: LegacyAdmin, adapter: Adapter, *, ttl: float = 60) -> RuntimePluginService:
    return RuntimePluginService(
        tenant_id=TENANT, admin_service=admin, adapters={"test_python": adapter},
        cache_ttl_seconds=ttl, monotonic=lambda: 1,
    )


@pytest.mark.parametrize("revocation", ["grant", "key", "proof", "missing_row", "unavailable"])
async def test_warmed_ttl_cannot_authorize_a_revoked_registration(revocation: str) -> None:
    admin = ScopedAdmin(plugin())
    adapter = Adapter(admin)
    service = runtime(admin, adapter)
    await service.reload(TENANT)
    assert await invoke(service) == {"ok": True}
    if revocation == "missing_row":
        admin.current = None
    elif revocation == "unavailable":
        admin.scope_failed = True
    else:
        admin.proof_live = False
    with pytest.raises(RuntimeCapabilityError, match="current registration or approval unavailable"):
        await invoke(service)
    assert adapter.calls == 1
    assert admin.loads == 1
    assert admin.entered == [TENANT, TENANT]


@pytest.mark.parametrize("change", ["config", "disabled", "capability", "package"])
async def test_current_registration_must_match_the_cached_execution_snapshot(change: str) -> None:
    admin = ScopedAdmin(plugin())
    adapter = Adapter(admin)
    service = runtime(admin, adapter)
    await service.reload(TENANT)
    assert admin.current is not None
    if change == "config":
        admin.current.resource_config = {"changed": True}
    elif change == "disabled":
        admin.current.enabled = False
    elif change == "capability":
        admin.current.capabilities[0].permission_class = "changed.permission"
    else:
        admin.current.content_sha256 = "b" * 64
    with pytest.raises(RuntimeCapabilityError):
        await invoke(service)
    assert adapter.calls == 0


@pytest.mark.parametrize("ttl", [0, 60])
async def test_reload_failure_cannot_restore_stale_approval(ttl: float) -> None:
    admin = ScopedAdmin(plugin())
    adapter = Adapter(admin)
    service = runtime(admin, adapter, ttl=ttl)
    await service.reload(TENANT)
    admin.load_failed = True
    admin.proof_live = False
    await service.reload(TENANT)
    for _ in range(2):
        with pytest.raises(RuntimeCapabilityError):
            await invoke(service)
    assert adapter.calls == 0
    assert admin.entered == [TENANT, TENANT]
    admin.load_failed = False
    await service.reload(TENANT)
    with pytest.raises(RuntimeCapabilityError):
        await invoke(service)
    assert adapter.calls == 0
    assert admin.entered == [TENANT, TENANT, TENANT]


async def test_packaged_public_dto_with_no_current_proof_fails_closed() -> None:
    cached = plugin()
    assert "_generated_handover_id" not in cached.model_dump()
    admin = ScopedAdmin(cached)
    admin.proof_live = False
    adapter = Adapter()
    with pytest.raises(RuntimeCapabilityError):
        await invoke(runtime(admin, adapter))
    assert adapter.calls == 0


async def test_persistent_admin_without_scope_cannot_use_a_package_free_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PersistentWithoutScope(PersistentAdminResourceService):
        def __init__(self) -> None:
            pass

        async def list_plugins(
            self, *, tenant_id: UUID | None = None,
        ) -> tuple[PluginResourceResponse, ...]:
            return (plugin(packaged=False),)

    admin = PersistentWithoutScope()
    monkeypatch.setattr(admin, "plugin_invocation_scope", None, raising=False)
    adapter = Adapter()
    service = RuntimePluginService(
        tenant_id=TENANT, admin_service=admin,
        adapters={"test_python": adapter},
    )
    with pytest.raises(RuntimeCapabilityError):
        await invoke(service)
    assert adapter.calls == 0


async def test_plain_legacy_without_scope_remains_usable() -> None:
    adapter = Adapter()
    assert await invoke(runtime(LegacyAdmin(plugin(packaged=False)), adapter)) == {"ok": True}
    assert adapter.calls == 1


async def test_legacy_package_without_scope_preserves_ordinary_in_memory_behavior() -> None:
    adapter = Adapter()
    assert await invoke(runtime(LegacyAdmin(plugin()), adapter)) == {"ok": True}
    assert adapter.calls == 1


async def test_legacy_adapter_failure_keeps_its_original_classification() -> None:
    adapter = Adapter()
    adapter.failure = RuntimeCapabilityError("Plugin backend unavailable")
    with pytest.raises(RuntimeCapabilityError, match="^Plugin backend unavailable$"):
        await invoke(runtime(LegacyAdmin(plugin(packaged=False)), adapter))
    assert adapter.calls == 1


@pytest.mark.parametrize("failure", ["lookup", "factory", "enter", "not_callable", "not_scope"])
async def test_scope_acquisition_failures_are_safe_and_never_invoke_an_adapter(failure: str) -> None:
    class BrokenScopeAdmin(LegacyAdmin):
        @property
        def plugin_invocation_scope(self) -> object:
            if failure == "lookup":
                raise RuntimeError("PRIVATE scope lookup SQL")
            if failure == "not_callable":
                return "PRIVATE"

            def factory(plugin: PluginResourceResponse, *, tenant_id: UUID) -> object:
                if failure == "factory":
                    raise RuntimeError("PRIVATE scope creation SQL")
                if failure == "not_scope":
                    return None

                @asynccontextmanager
                async def rejected() -> AsyncIterator[None]:
                    raise RuntimeError("PRIVATE scope entry SQL")
                    yield

                return rejected()

            return factory

    adapter = Adapter()
    with pytest.raises(RuntimeCapabilityError, match="^Plugin current registration or approval unavailable$") as caught:
        await invoke(runtime(BrokenScopeAdmin(plugin()), adapter))
    assert "PRIVATE" not in str(caught.value)
    assert adapter.calls == 0


async def test_all_authoritative_invocations_use_the_requested_tenant_scope() -> None:
    admin = ScopedAdmin(plugin(packaged=False))
    adapter = Adapter(admin)
    service = runtime(admin, adapter)
    assert await invoke(service, OTHER_TENANT) == {"ok": True}
    assert await invoke(service, OTHER_TENANT) == {"ok": True}
    assert admin.entered == [OTHER_TENANT, OTHER_TENANT]
    assert adapter.inside_scope == [True, True]
    assert admin.audit_scope_states == [False, False]


async def test_scope_covers_adapter_await_and_revocation_serializes_after_admission() -> None:
    admin = ScopedAdmin(plugin())
    adapter = Adapter(admin)
    adapter.release.clear()
    service = runtime(admin, adapter)
    first = asyncio.create_task(invoke(service))
    await adapter.started.wait()
    revoke_requested = asyncio.Event()
    revoked = asyncio.Event()

    async def revoke() -> None:
        revoke_requested.set()
        async with admin.lock:
            admin.proof_live = False
            revoked.set()

    revoker = asyncio.create_task(revoke())
    await revoke_requested.wait()
    held_during_invoke = admin.lock.locked()
    revoked_during_invoke = revoked.is_set()
    adapter.release.set()
    assert await first == {"ok": True}
    await revoker
    assert held_during_invoke is True
    assert revoked_during_invoke is False
    with pytest.raises(RuntimeCapabilityError):
        await invoke(service)
    assert adapter.calls == 1


async def test_result_validation_and_cancellation_release_the_scope() -> None:
    admin = ScopedAdmin(plugin())
    adapter = Adapter(admin)
    adapter.result = {"ok": False}
    service = runtime(admin, adapter)
    with pytest.raises(RuntimeCapabilityError, match="result does not match"):
        await invoke(service)
    assert adapter.inside_scope == [True]
    assert admin.exit_errors == [RuntimeCapabilityError]
    assert admin.active is False
    assert admin.lock.locked() is False
    adapter.started.clear()
    adapter.release.clear()
    task = asyncio.create_task(invoke(service))
    await adapter.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admin.active is False
    assert admin.lock.locked() is False
    assert admin.exit_errors == [RuntimeCapabilityError, asyncio.CancelledError]


async def test_scope_exit_failure_is_safe_and_cannot_report_invocation_success() -> None:
    class ExitFailureAdmin(ScopedAdmin):
        @asynccontextmanager
        async def plugin_invocation_scope(
            self, plugin: PluginResourceResponse, *, tenant_id: UUID,
        ) -> AsyncIterator[None]:
            async with super().plugin_invocation_scope(plugin, tenant_id=tenant_id):
                yield
                raise RuntimeError("PRIVATE transaction close SQL")

    admin = ExitFailureAdmin(plugin())
    adapter = Adapter(admin)
    with pytest.raises(RuntimeCapabilityError, match="^Plugin current registration or approval unavailable$"):
        await invoke(runtime(admin, adapter))
    assert adapter.calls == 1
    assert admin.active is False
    assert admin.lock.locked() is False
    assert admin.audit_scope_states == [False]
