from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import UUID

import pytest

from agent_hub.api.routers.admin import (
    InMemoryAdminResourceService,
    PluginPackageMetadata,
    PluginResourceRequest,
    PluginResourceResponse,
)
from agent_hub.capability_installer.catalog import (
    CliArtifactEnvironmentRecipe,
    TrustedCapabilityCatalog,
)
from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    CliArtifactSpec,
    tenant_environment_id,
)
from agent_hub.capability_installer.service import (
    CapabilityInstallerService,
    CapabilityInstallUnavailable,
)
from tests.unit.capability_installer.test_service import (
    ACTOR_ID,
    TENANT_ID,
    InstallingAdminService,
    _managed_entry,
)


class TransactionAdmin(InstallingAdminService):
    def __init__(self, existing: PluginResourceRequest | None) -> None:
        super().__init__(existing=existing)
        self.calls: list[str] = []
        self.pause: str | None = None
        self.failure: str | None = None
        self.effective = asyncio.Event()
        self.release = asyncio.Event()

    async def list_plugins(self, *, tenant_id: object) -> tuple[PluginResourceRequest, ...]:
        return () if self.current is None else (self.current,)

    async def _after(self, stage: str) -> None:
        if self.pause == stage:
            self.effective.set()
            await self.release.wait()
        if self.failure == stage:
            self.failure = None
            raise RuntimeError(f"{stage} failed")

    async def upsert_plugin(
        self, plugin: PluginResourceRequest, *, tenant_id: object, actor_id: object
    ) -> None:
        self.calls.append("plugin")
        self.current = plugin
        await self._after("plugin")
        if plugin.name == "Managed Tool":
            await self._after("compensation")

    async def delete_plugin(self, plugin_id: str, **kwargs: object) -> None:
        self.calls.append("plugin")
        self.current = None
        await self._after("plugin")

    async def start_plugin(self, plugin_id: str, **kwargs: object) -> PluginResourceRequest:
        self.calls.append("start")
        return await super().start_plugin(plugin_id, **kwargs)

    async def upsert_capability_install_state(
        self, entry_id: str, state: dict[str, str], **kwargs: object
    ) -> None:
        self.calls.append("state")
        self.state = dict(state)
        await self._after("state")

    async def delete_capability_install_state(self, entry_id: str, **kwargs: object) -> None:
        self.calls.append("state-delete")
        await self._after("state-delete-before")
        self.state = None
        await self._after("state-delete")


async def _installed(
    tmp_path: Path, *, replaced: bool
) -> tuple[CapabilityInstallerService, TransactionAdmin, CapabilityEnvironmentManager, list[str]]:
    source = tmp_path / "trusted-tool"
    source.write_bytes(b"controlled CLI bytes; never executed")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    probes: list[str] = []
    manager = CapabilityEnvironmentManager(
        tmp_path / "environments", cli_probe=lambda *_: probes.append("build")
    )
    entry = _managed_entry(
        CliArtifactEnvironmentRecipe(
            version="2.0.0", artifact_path=source, sha256=digest, executable_name="tool"
        )
    )
    previous = None
    if replaced:
        record = manager.deploy_cli(
            CliArtifactSpec(
                tenant_environment_id(TENANT_ID, entry.id), "1.0.0", source, digest, "tool"
            )
        )
        previous = entry.plugin.model_copy(
            update={
                "name": "Previous plugin",
                "resource_config": {"command": str(record.path / "tool")},
            }
        )
    admin = TransactionAdmin(previous)
    installer = CapabilityInstallerService(
        TrustedCapabilityCatalog((entry,)), environment_manager=manager
    )
    await _install(installer, admin)
    return installer, admin, manager, probes


async def _install(
    installer: CapabilityInstallerService, admin: TransactionAdmin
) -> tuple[object, object]:
    plan = installer.plan("managed_tool", query="transaction fixture")
    return await installer.install(
        admin,
        entry_id="managed_tool",
        query=plan.query,
        plan_id=plan.id,
        confirm=True,
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )


async def _rollback(installer: CapabilityInstallerService, admin: TransactionAdmin) -> None:
    await installer.rollback(
        admin,
        entry_id="managed_tool",
        query="transaction fixture",
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )


@pytest.mark.parametrize("replaced", [False, True])
@pytest.mark.parametrize("stage", ["plugin", "state"])
async def test_install_write_then_raise_restores_every_attempted_effect(
    tmp_path: Path, replaced: bool, stage: str
) -> None:
    installer, admin, manager, _ = await _installed(tmp_path, replaced=replaced)
    await _rollback(installer, admin)
    previous_plugin = admin.current
    previous_state = {"plan_id": "previous-plan", "keep": "baseline"} if replaced else None
    admin.state = None if previous_state is None else dict(previous_state)
    admin.failure = stage

    # Models an ambiguous acknowledgement, not a real PostgreSQL lost-COMMIT-ack probe.
    with pytest.raises(RuntimeError, match=f"{stage} failed"):
        await _install(installer, admin)

    assert admin.current == previous_plugin
    assert admin.state == previous_state
    capability_id = tenant_environment_id(TENANT_ID, "managed_tool")
    if replaced:
        assert manager.active(capability_id).version == "1.0.0"
    else:
        with pytest.raises(KeyError):
            manager.active(capability_id)


@pytest.mark.parametrize("replaced", [False, True])
async def test_failed_plugin_compensation_does_not_skip_state_or_sdk_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replaced: bool
) -> None:
    installer, admin, manager, _ = await _installed(tmp_path, replaced=replaced)
    await _rollback(installer, admin)
    previous_plugin = admin.current
    previous_state = {"plan_id": "previous-plan", "keep": "baseline"} if replaced else None
    admin.state = None if previous_state is None else dict(previous_state)
    admin.failure = "state"
    upsert = admin.upsert_plugin

    async def fail_restore(plugin: PluginResourceRequest, **kwargs: Any) -> None:
        if plugin == previous_plugin:
            raise RuntimeError("plugin compensation failed")
        await upsert(plugin, **kwargs)

    async def fail_delete(plugin_id: str, **kwargs: object) -> None:
        raise RuntimeError("plugin compensation failed")

    monkeypatch.setattr(admin, "upsert_plugin", fail_restore)
    monkeypatch.setattr(admin, "delete_plugin", fail_delete)
    with pytest.raises(RuntimeError, match="plugin compensation failed"):
        await _install(installer, admin)

    assert admin.state == previous_state
    capability_id = tenant_environment_id(TENANT_ID, "managed_tool")
    if replaced:
        assert manager.active(capability_id).version == "1.0.0"
    else:
        with pytest.raises(KeyError):
            manager.active(capability_id)


class ResponseTransactionAdmin(InMemoryAdminResourceService):
    def __init__(self) -> None:
        super().__init__()
        self.failure: str | None = None

    def _after(self, stage: str) -> None:
        if self.failure == stage:
            self.failure = None
            raise RuntimeError(f"{stage} failed")

    async def upsert_capability_install_state(
        self,
        entry_id: str,
        state: dict[str, str],
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> dict[str, str]:
        result = await super().upsert_capability_install_state(
            entry_id, state, tenant_id=tenant_id, actor_id=actor_id
        )
        self._after("state")
        return result

    async def delete_capability_install_state(
        self,
        entry_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        await super().delete_capability_install_state(
            entry_id, tenant_id=tenant_id, actor_id=actor_id
        )
        self._after("state-delete")


async def _response_install(
    installer: CapabilityInstallerService, admin: ResponseTransactionAdmin
) -> None:
    async def preflight(plugin: PluginResourceResponse) -> None:
        pass

    plan = installer.plan("managed_tool", query="response transaction fixture")
    await installer.install(
        admin,
        entry_id="managed_tool",
        query=plan.query,
        plan_id=plan.id,
        confirm=True,
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        activation_preflight=preflight,
    )


@pytest.mark.parametrize("blocked_package", [False, True])
async def test_install_compensation_projects_real_response_and_keeps_package_metadata(
    tmp_path: Path,
    blocked_package: bool,
) -> None:
    installer, fixture, manager, _ = await _installed(tmp_path, replaced=True)
    await _rollback(installer, fixture)
    assert fixture.current is not None
    admin = ResponseTransactionAdmin()
    previous = await admin.upsert_plugin(
        fixture.current,
        source_filename="baseline.zip",
        content_sha256="b" * 64,
        package_metadata=PluginPackageMetadata(
            kind="adapter_package" if blocked_package else "manifest_only",
            package_version="1.0.0",
        ),
    )
    previous_state = {"plan_id": "previous-plan", "keep": "baseline"}
    await admin.upsert_capability_install_state("managed_tool", previous_state)
    admin.failure = "state"

    with pytest.raises(RuntimeError, match="state failed"):
        await _response_install(installer, admin)

    restored = admin.plugins[previous.id]
    request_fields = set(PluginResourceRequest.model_fields)
    assert restored.model_dump(include=request_fields) == previous.model_dump(
        include=request_fields
    )
    assert restored.source_filename == "baseline.zip"
    assert restored.content_sha256 == "b" * 64
    assert restored.package_metadata == previous.package_metadata
    assert restored.status == previous.status == "stopped", "compensation activated a stopped baseline"
    assert admin.capability_install_states["managed_tool"] == previous_state
    assert manager.active(tenant_environment_id(TENANT_ID, "managed_tool")).version == "1.0.0"


@pytest.mark.parametrize("replaced", [False, True])
async def test_rollback_compensation_projects_real_response_and_keeps_package_metadata(
    tmp_path: Path, replaced: bool
) -> None:
    installer, fixture, manager, _ = await _installed(tmp_path, replaced=replaced)
    await _rollback(installer, fixture)
    admin = ResponseTransactionAdmin()
    if fixture.current is not None:
        await admin.upsert_plugin(fixture.current)
    await _response_install(installer, admin)
    state = dict(admin.capability_install_states["managed_tool"])
    installed = admin.plugins["managed-tool"].model_copy(
        update={
            "source_filename": "installed.zip",
            "content_sha256": "c" * 64,
            "package_metadata": PluginPackageMetadata(package_version="2.0.0"),
        }
    )
    admin.plugins[installed.id] = installed
    assert installed.status == "running" and installed.health == "healthy"
    admin.failure = "state-delete"

    with pytest.raises(RuntimeError, match="state-delete failed"):
        await installer.rollback(
            admin,
            entry_id="managed_tool",
            query="response transaction fixture",
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )

    restored = admin.plugins[installed.id]
    request_fields = set(PluginResourceRequest.model_fields)
    assert restored.model_dump(include=request_fields) == installed.model_dump(
        include=request_fields
    )
    assert restored.source_filename == "installed.zip"
    assert restored.content_sha256 == "c" * 64
    assert restored.package_metadata == installed.package_metadata
    assert admin.capability_install_states["managed_tool"] == state
    assert manager.active(state["environment_capability_id"]).version == "2.0.0"
    if replaced:
        assert restored.status == "running" and restored.health == "healthy"
    else:
        # Recreating a deleted registration restores configuration, not runtime approval.
        assert restored.status == "stopped" and restored.health == "stopped"
        with pytest.raises(CapabilityInstallUnavailable):
            await _response_install(installer, admin)
        assert admin.plugins[installed.id] == restored
        assert admin.capability_install_states["managed_tool"] == state
        assert manager.active(state["environment_capability_id"]).version == "2.0.0"


@pytest.mark.parametrize("replaced", [False, True])
async def test_same_confirmed_plan_preserves_first_rollback_baseline(
    tmp_path: Path, replaced: bool
) -> None:
    installer, admin, manager, probes = await _installed(tmp_path, replaced=replaced)
    assert admin.state is not None
    first_state, first_plugin = dict(admin.state), admin.current
    first_calls, first_probes = list(admin.calls), list(probes)

    _, repeated_plugin = await _install(installer, admin)

    assert admin.state == first_state, "duplicate confirmation replaced the original baseline"
    assert repeated_plugin == first_plugin
    assert admin.calls == first_calls and probes == first_probes
    await _rollback(installer, admin)
    if replaced:
        assert admin.current is not None and admin.current.name == "Previous plugin"
        assert manager.active(first_state["environment_capability_id"]).version == "1.0.0"
    else:
        assert admin.current is None
        with pytest.raises(KeyError):
            manager.active(first_state["environment_capability_id"])


@pytest.mark.parametrize("changed", ["plugin", "environment", "missing-plugin"])
async def test_same_plan_does_not_silently_reinstall_drifted_state(
    tmp_path: Path, changed: str
) -> None:
    installer, admin, manager, probes = await _installed(tmp_path, replaced=True)
    assert admin.state is not None and admin.current is not None
    if changed == "plugin":
        admin.current = admin.current.model_copy(update={"name": "User edited"})
    elif changed == "missing-plugin":
        admin.current = None
    else:
        manager.restore_active(admin.state["environment_capability_id"], "1.0.0")
    previous_state, previous_plugin = dict(admin.state), admin.current
    previous_calls, previous_probes = list(admin.calls), list(probes)

    with pytest.raises(CapabilityInstallUnavailable):
        await _install(installer, admin)

    assert admin.state == previous_state and admin.current == previous_plugin
    assert admin.calls == previous_calls and probes == previous_probes


async def test_same_plan_without_environment_recipe_does_not_replace_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.capability_installer import service as service_module

    def forbidden_gate(*args: object, **kwargs: object) -> object:
        raise AssertionError("no-manager legacy path must not invent an SDK gate root")

    monkeypatch.setattr(service_module, "sdk_install_gate", forbidden_gate)
    source = tmp_path / "existing-tool"
    source.write_bytes(b"not executed")
    entry = _managed_entry(
        CliArtifactEnvironmentRecipe(
            version="2.0.0", artifact_path=source, sha256="a" * 64, executable_name="tool"
        )
    )
    entry = entry.model_copy(
        update={
            "environment_recipe": None,
            "plugin": entry.plugin.model_copy(update={"resource_config": {"command": str(source)}}),
        }
    )
    installer = CapabilityInstallerService(TrustedCapabilityCatalog((entry,)))
    admin = TransactionAdmin(None)
    await _install(installer, admin)
    assert admin.state is not None
    installed_state, calls = dict(admin.state), list(admin.calls)

    await _install(installer, admin)

    assert admin.state == installed_state and admin.calls == calls
    await _rollback(installer, admin)
    assert admin.current is None and admin.state is None


@pytest.mark.parametrize("field", ["confirmed_plan", "installed_plugin"])
async def test_same_plan_legacy_receipt_refuses_without_overwriting_baseline(
    tmp_path: Path, field: str
) -> None:
    installer, admin, _, probes = await _installed(tmp_path, replaced=True)
    assert admin.state is not None
    admin.state.pop(field)
    state, plugin, calls, builds = dict(admin.state), admin.current, list(admin.calls), list(probes)

    with pytest.raises(CapabilityInstallUnavailable):
        await _install(installer, admin)

    assert admin.state == state and admin.current == plugin
    assert admin.calls == calls and probes == builds


@pytest.mark.parametrize("replaced", [False, True])
@pytest.mark.parametrize("stage", ["plugin", "state-delete"])
async def test_rollback_repeated_cancel_drains_effective_write_to_completion(
    tmp_path: Path, replaced: bool, stage: str
) -> None:
    installer, admin, manager, _ = await _installed(tmp_path, replaced=replaced)
    assert admin.state is not None
    capability_id = admin.state["environment_capability_id"]
    admin.pause = stage
    task = asyncio.create_task(_rollback(installer, admin))
    try:
        await asyncio.wait_for(admin.effective.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "rollback abandoned an effective write after cancellation"
        admin.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert admin.state is None
        if replaced:
            assert admin.current is not None and admin.current.name == "Previous plugin"
            assert manager.active(capability_id).version == "1.0.0"
        else:
            assert admin.current is None
            with pytest.raises(KeyError):
                manager.active(capability_id)
    finally:
        admin.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("replaced", [False, True])
@pytest.mark.parametrize("stage", ["plugin", "state-delete-before", "state-delete"])
async def test_failed_rollback_restores_installed_plugin_environment_and_state(
    tmp_path: Path, replaced: bool, stage: str
) -> None:
    installer, admin, manager, _ = await _installed(tmp_path, replaced=replaced)
    assert admin.state is not None
    installed_state, installed_plugin = dict(admin.state), admin.current
    admin.failure = stage

    with pytest.raises(RuntimeError, match=f"{stage} failed"):
        await _rollback(installer, admin)

    assert admin.state == installed_state, "failed rollback lost its install record"
    assert admin.current == installed_plugin, "failed rollback left the previous/deleted plugin"
    assert manager.active(installed_state["environment_capability_id"]).version == "2.0.0"


async def test_rollback_repeated_cancel_during_compensation_waits_for_restoration(
    tmp_path: Path,
) -> None:
    installer, admin, manager, _ = await _installed(tmp_path, replaced=True)
    assert admin.state is not None
    state, plugin = dict(admin.state), admin.current
    admin.failure = "state-delete"
    admin.pause = "compensation"
    task = asyncio.create_task(_rollback(installer, admin))
    try:
        await asyncio.wait_for(admin.effective.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "rollback abandoned its compensation after cancellation"
        admin.release.set()
        with pytest.raises(RuntimeError, match="state-delete failed"):
            await task
        assert admin.state == state and admin.current == plugin
        assert manager.active(state["environment_capability_id"]).version == "2.0.0"
    finally:
        admin.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("first_operation", ["install", "rollback"])
async def test_two_sdk_service_instances_serialize_install_and_rollback(
    tmp_path: Path, first_operation: str
) -> None:
    installer, admin, manager, _ = await _installed(tmp_path, replaced=True)
    second = CapabilityInstallerService(
        TrustedCapabilityCatalog(installer.catalog_entries()),
        environment_manager=CapabilityEnvironmentManager(manager.root, cli_probe=lambda *_: None),
    )
    assert admin.state is not None
    if first_operation == "install":
        manager.restore_active(admin.state["environment_capability_id"], "1.0.0")
        admin.current = PluginResourceRequest.model_validate(
            json.loads(admin.state["previous_plugin"])
        )
        admin.state = None
    admin.calls.clear()
    admin.pause = "plugin"
    first = asyncio.create_task(
        _install(installer, admin) if first_operation == "install" else _rollback(installer, admin)
    )
    following: asyncio.Task[tuple[object, object]] | None = None
    try:
        await asyncio.wait_for(admin.effective.wait(), 2)
        following = asyncio.create_task(_install(second, admin))
        await asyncio.sleep(0.15)
        assert not following.done(), "second service bypassed the live SDK transaction"
        assert admin.calls == ["plugin"], "second service wrote inside the first transaction"
        admin.release.set()
        await asyncio.wait_for(first, 3)
        await asyncio.wait_for(following, 3)
        assert admin.state is not None
        assert admin.state["environment_previous_version"] == "1.0.0"
        assert admin.calls.count("start") == 1
    finally:
        admin.release.set()
        first.cancel()
        if following is not None:
            following.cancel()
        await asyncio.gather(
            first, *(() if following is None else (following,)), return_exceptions=True
        )


def _observe_gate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    entering: threading.Event | None = None,
    exiting: threading.Event | None = None,
    release_exit: threading.Event | None = None,
) -> None:
    from agent_hub.plugins import handover_confirmation

    original = handover_confirmation._shared_fs_lock_at_root

    class ObservedLock(AbstractContextManager[None]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.delegate = original(*args, **kwargs)

        def __enter__(self) -> None:
            if entering is not None:
                entering.set()
            self.delegate.__enter__()

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> bool | None:
            if exiting is not None:
                exiting.set()
            if release_exit is not None:
                assert release_exit.wait(3), "gate release was never unblocked"
            return self.delegate.__exit__(exc_type, exc, tb)

    monkeypatch.setattr(handover_confirmation, "_shared_fs_lock_at_root", ObservedLock)


async def test_sdk_gate_cancelled_waiter_drains_acquire_then_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_hub.capability_installer.install_gate import sdk_install_gate

    manager = CapabilityEnvironmentManager(tmp_path / "environments")
    holder = sdk_install_gate(manager, TENANT_ID, "managed-tool")
    await holder.__aenter__()
    entering = threading.Event()
    _observe_gate(monkeypatch, entering=entering)
    body_entered = False

    async def wait_for_gate() -> None:
        nonlocal body_entered
        async with sdk_install_gate(manager, TENANT_ID, "managed-tool"):
            body_entered = True

    waiter = asyncio.create_task(wait_for_gate())
    try:
        assert await asyncio.to_thread(entering.wait, 2)
        waiter.cancel()
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        assert not waiter.done(), "cancelled waiter abandoned its acquiring thread"
    finally:
        await holder.__aexit__(None, None, None)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, 2)
    assert not body_entered
    async with sdk_install_gate(manager, TENANT_ID, "managed-tool"):
        pass


async def test_sdk_gate_repeated_cancel_during_release_waits_and_unlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_hub.capability_installer.install_gate import sdk_install_gate

    manager = CapabilityEnvironmentManager(tmp_path / "environments")
    exiting, release_exit = threading.Event(), threading.Event()
    _observe_gate(monkeypatch, exiting=exiting, release_exit=release_exit)

    async def use_gate() -> None:
        async with sdk_install_gate(manager, TENANT_ID, "managed-tool"):
            pass

    task = asyncio.create_task(use_gate())
    try:
        assert await asyncio.to_thread(exiting.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancelled release abandoned its unlocking thread"
        release_exit.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    finally:
        release_exit.set()
        await asyncio.gather(task, return_exceptions=True)
    async with sdk_install_gate(manager, TENANT_ID, "managed-tool"):
        pass


async def test_sdk_gate_contention_timeout_is_safe_service_error(tmp_path: Path) -> None:
    from agent_hub.capability_installer.install_gate import sdk_install_gate

    installer, admin, manager, _ = await _installed(tmp_path, replaced=True)
    assert admin.state is not None
    state, plugin, calls = dict(admin.state), admin.current, list(admin.calls)
    async with sdk_install_gate(manager, TENANT_ID, "managed-tool"):
        with pytest.raises(CapabilityInstallUnavailable) as captured:
            await asyncio.wait_for(_install(installer, admin), 7)
    assert str(manager.root) not in str(captured.value)
    assert admin.state == state and admin.current == plugin and admin.calls == calls
    await _install(installer, admin)


async def test_sdk_gate_rejects_removed_root_without_recreating_it(tmp_path: Path) -> None:
    from agent_hub.capability_installer.install_gate import (
        SdkInstallGateUnavailable,
        sdk_install_gate,
    )

    manager = CapabilityEnvironmentManager(tmp_path / "environments")
    manager.root.rename(tmp_path / "moved-environments")
    with pytest.raises(SdkInstallGateUnavailable):
        async with sdk_install_gate(manager, TENANT_ID, "managed-tool"):
            pytest.fail("removed root was silently recreated")
    assert not manager.root.exists()


async def test_sdk_gate_excludes_an_independent_process(tmp_path: Path) -> None:
    from agent_hub.capability_installer.install_gate import sdk_install_gate

    manager = CapabilityEnvironmentManager(tmp_path / "environments")
    code = """
import asyncio
import sys
from pathlib import Path
from uuid import UUID
from agent_hub.capability_installer.environment import CapabilityEnvironmentManager
from agent_hub.capability_installer import install_gate
from agent_hub.plugins import handover_confirmation
assert callable(handover_confirmation._shared_fs_lock_at_root)
enter_gate = install_gate._enter_gate
def ready_enter(*args):
    print('waiting', flush=True)
    return enter_gate(*args)
install_gate._enter_gate = ready_enter
async def main():
    manager = CapabilityEnvironmentManager(Path(sys.argv[1]))
    async with install_gate.sdk_install_gate(manager, UUID(sys.argv[2]), 'managed-tool'):
        print('locked', flush=True)
asyncio.run(main())
"""
    process: asyncio.subprocess.Process | None = None
    locked: asyncio.Task[bytes] | None = None
    try:
        async with sdk_install_gate(manager, TENANT_ID, "managed-tool"):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                code,
                str(manager.root),
                str(TENANT_ID),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
            )
            assert process.stdout is not None
            # Startup includes importing the service graph; the lock signal is emitted
            # by its worker only after that graph is initialized.
            assert (await asyncio.wait_for(process.stdout.readline(), 20)).rstrip() == b"waiting"
            locked = asyncio.create_task(process.stdout.readline())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(locked), 0.15)
        line = (await asyncio.wait_for(locked, 8)).rstrip()
        if not line:
            _, stderr = await process.communicate()
            pytest.fail("SDK gate child exited before acquiring lock: " + stderr.decode())
        assert line == b"locked"
        assert await asyncio.wait_for(process.wait(), 3) == 0
    finally:
        if locked is not None:
            locked.cancel()
            await asyncio.gather(locked, return_exceptions=True)
        if process is not None:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await process.communicate()


async def test_sdk_install_pins_current_and_baseline_against_cleanup_and_rollback(
    tmp_path: Path,
) -> None:
    installer, admin, manager, _ = await _installed(tmp_path, replaced=True)
    assert admin.state is not None
    state = dict(admin.state)
    owner = state["environment_reference_owner"]
    capability_id = state["environment_capability_id"]
    assert json.loads(state["environment_pinned_versions"]) == ["1.0.0", "2.0.0"]
    assert manager.references(capability_id, "1.0.0") == (owner,)
    assert manager.references(capability_id, "2.0.0") == (owner,)
    assert manager.cleanup(capability_id, keep_versions=1) == ()

    await _rollback(installer, admin)

    assert manager.active(capability_id).version == "1.0.0"
    assert manager.cleanup(capability_id, keep_versions=1) == ()
    assert manager.list_versions(capability_id) == ("1.0.0", "2.0.0")
    assert manager.references(capability_id, "2.0.0") == (owner,)


async def test_sdk_pin_precedes_build_and_plugin_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, admin, manager, _ = await _installed(tmp_path, replaced=True)
    assert admin.state is not None
    capability_id = admin.state["environment_capability_id"]
    prior_owner = admin.state["environment_reference_owner"]
    manager.restore_active(capability_id, "1.0.0")
    admin.current = PluginResourceRequest.model_validate(json.loads(admin.state["previous_plugin"]))
    admin.state = None
    deploy = manager.deploy_cli
    upsert = admin.upsert_plugin
    owners: set[str] = set()

    def checked_build(spec: CliArtifactSpec) -> object:
        owners.update(set(manager.references(capability_id, "1.0.0")) - {prior_owner})
        assert len(owners) == 1, "baseline was not pinned by this operation before build"
        return deploy(spec)

    async def checked_write(plugin: PluginResourceRequest, **kwargs: Any) -> None:
        assert owners <= set(manager.references(capability_id, "2.0.0")), (
            "candidate was not pinned before plugin registration"
        )
        await upsert(plugin, **kwargs)

    monkeypatch.setattr(manager, "deploy_cli", checked_build)
    monkeypatch.setattr(admin, "upsert_plugin", checked_write)
    await _install(installer, admin)
    assert admin.state is not None and admin.state["environment_reference_owner"] in owners


@pytest.mark.parametrize("failure_stage", ["activation", "candidate-pin"])
async def test_sdk_failed_install_retains_unique_owned_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    installer, admin, manager, _ = await _installed(tmp_path, replaced=True)
    assert admin.state is not None
    capability_id = admin.state["environment_capability_id"]
    old_owner = admin.state["environment_reference_owner"]
    manager.restore_active(capability_id, "1.0.0")
    admin.current = PluginResourceRequest.model_validate(json.loads(admin.state["previous_plugin"]))
    admin.state = None
    baseline_plugin = admin.current
    if failure_stage == "activation":
        admin.fail_start = True
    else:
        original = manager.add_reference

        def uncertain_pin(capability_id: str, version: str, owner: str) -> None:
            original(capability_id, version, owner)
            if version == "2.0.0":
                raise OSError("reference persisted before acknowledgement failed")

        monkeypatch.setattr(manager, "add_reference", uncertain_pin)
    with pytest.raises((RuntimeError, CapabilityInstallUnavailable)):
        await _install(installer, admin)
    assert admin.state is None and admin.current == baseline_plugin
    assert manager.active(capability_id).version == "1.0.0"
    baseline_owners = set(manager.references(capability_id, "1.0.0"))
    candidate_owners = set(manager.references(capability_id, "2.0.0"))
    assert old_owner in baseline_owners and old_owner in candidate_owners
    assert len(baseline_owners - {old_owner}) == 1
    assert baseline_owners == candidate_owners
    assert manager.cleanup(capability_id, keep_versions=1) == ()


@pytest.mark.parametrize("operation", ["install", "replay", "rollback"])
async def test_sdk_active_tree_validation_does_not_run_on_request_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    installer, admin, manager, _ = await _installed(tmp_path, replaced=True)
    original = manager.active
    request_thread = threading.get_ident()

    def checked_active(capability_id: str) -> object:
        assert threading.get_ident() != request_thread, "active tree validation blocked API thread"
        return original(capability_id)

    monkeypatch.setattr(manager, "active", checked_active)
    if operation == "install":
        admin.state = None
    if operation == "rollback":
        await _rollback(installer, admin)
    else:
        await _install(installer, admin)


async def test_sdk_install_failure_restore_does_not_run_on_request_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, admin, manager, _ = await _installed(tmp_path, replaced=True)
    admin.state = None
    admin.fail_start = True
    original = manager.restore_active
    request_thread = threading.get_ident()

    def checked_restore(capability_id: str, version: str | None) -> object:
        assert threading.get_ident() != request_thread, "restore lock wait blocked API thread"
        return original(capability_id, version)

    monkeypatch.setattr(manager, "restore_active", checked_restore)
    with pytest.raises(RuntimeError, match="activation failed"):
        await _install(installer, admin)


async def test_backend_docker_probe_does_not_run_on_request_thread(tmp_path: Path) -> None:
    source = tmp_path / "controlled-command"
    source.write_bytes(b"not executed")
    entry = _managed_entry(
        CliArtifactEnvironmentRecipe(
            version="2.0.0", artifact_path=source, sha256="a" * 64, executable_name="tool"
        )
    )
    entry = entry.model_copy(
        update={
            "environment_recipe": None,
            "plugin": entry.plugin.model_copy(
                update={
                    "resource_config": {
                        "command": str(source),
                        "required_commands": ("docker",),
                    }
                }
            ),
        }
    )
    request_thread = threading.get_ident()
    probes: list[str] = []

    def checked_probe(executable: str) -> bool:
        assert threading.get_ident() != request_thread, "Docker backend probe blocked API thread"
        probes.append(executable)
        return True

    installer = CapabilityInstallerService(
        TrustedCapabilityCatalog((entry,)),
        command_resolver=lambda _: str(source),
        runtime_probe=checked_probe,
    )
    await _install(installer, TransactionAdmin(None))
    assert probes == [str(source)]
