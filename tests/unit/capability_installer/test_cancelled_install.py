from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from agent_hub.api.routers.admin import PluginResourceRequest
from agent_hub.capability_installer.catalog import (
    CliArtifactEnvironmentRecipe,
    TrustedCapabilityCatalog,
)
from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    tenant_environment_id,
)
from agent_hub.capability_installer.service import CapabilityInstallerService
from tests.unit.capability_installer.test_service import (
    ACTOR_ID,
    TENANT_ID,
    InstallingAdminService,
    RecordingEnvironmentManager,
    _managed_entry,
)


@pytest.mark.parametrize("replaced", [False, True])
async def test_repeated_cancel_waits_for_owned_plugin_and_environment_rollback(
    tmp_path: Path, replaced: bool
) -> None:
    recipe = CliArtifactEnvironmentRecipe(
        version="2.0.0", artifact_path=tmp_path / "tool", sha256="a" * 64, executable_name="tool"
    )
    entry = _managed_entry(recipe)
    previous = entry.plugin.model_copy(update={"name": "Previous plugin"}) if replaced else None
    starting, restoring, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Admin(InstallingAdminService):
        async def start_plugin(self, plugin_id: str, **kwargs: object) -> PluginResourceRequest:
            starting.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled activation must not return")

        async def delete_plugin(self, plugin_id: str, **kwargs: object) -> None:
            restoring.set()
            await release.wait()
            await super().delete_plugin(plugin_id, **kwargs)

        async def upsert_plugin(
            self, plugin: PluginResourceRequest, *, tenant_id: object, actor_id: object
        ) -> None:
            if plugin.name == "Previous plugin":
                restoring.set()
                await release.wait()
            self.current = plugin

    manager = RecordingEnvironmentManager(tmp_path / "environments", active_version="1.0.0")
    service = CapabilityInstallerService(
        TrustedCapabilityCatalog((entry,)),
        environment_manager=cast(CapabilityEnvironmentManager, manager),
    )
    admin = Admin(existing=previous)
    plan = service.plan(entry.id, query="owned-cancel-fixture")
    task = asyncio.create_task(
        service.install(
            admin,
            entry_id=entry.id,
            query=plan.query,
            plan_id=plan.id,
            confirm=True,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )
    )
    try:
        await asyncio.wait_for(starting.wait(), 1)
        task.cancel()
        await asyncio.wait_for(restoring.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation returned while owned rollback was incomplete"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert admin.current == previous and admin.state is None
        assert manager.active_version == "1.0.0"
        assert manager.restores == [(tenant_environment_id(TENANT_ID, entry.id), "1.0.0")]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("stage", ["plugin", "state"])
@pytest.mark.parametrize("replaced", [False, True])
async def test_cancel_during_effective_write_drains_and_restores_every_snapshot(
    tmp_path: Path, stage: str, replaced: bool
) -> None:
    entry = _managed_entry(
        CliArtifactEnvironmentRecipe(
            version="2.0.0",
            artifact_path=tmp_path / "tool",
            sha256="a" * 64,
            executable_name="tool",
        )
    )
    previous = entry.plugin.model_copy(update={"name": "Previous plugin"}) if replaced else None
    previous_state = (
        {"entry_id": entry.id, "environment_version": "1.0.0", "keep": "prior-state"}
        if replaced
        else None
    )
    written, release = asyncio.Event(), asyncio.Event()

    class Admin(InstallingAdminService):
        async def upsert_plugin(
            self, plugin: PluginResourceRequest, *, tenant_id: object, actor_id: object
        ) -> None:
            self.current = plugin
            if stage == "plugin" and plugin.name != "Previous plugin":
                written.set()
                await release.wait()

        async def upsert_capability_install_state(
            self, entry_id: str, state: dict[str, str], **kwargs: object
        ) -> None:
            self.state = dict(state)
            if stage == "state" and state != previous_state:
                written.set()
                await release.wait()

    manager = RecordingEnvironmentManager(tmp_path / "environments", active_version="1.0.0")
    service = CapabilityInstallerService(
        TrustedCapabilityCatalog((entry,)),
        environment_manager=cast(CapabilityEnvironmentManager, manager),
    )
    admin = Admin(existing=previous)
    admin.state = None if previous_state is None else dict(previous_state)
    plan = service.plan(entry.id, query="effective-write-cancel")
    task = asyncio.create_task(
        service.install(
            admin,
            entry_id=entry.id,
            query=plan.query,
            plan_id=plan.id,
            confirm=True,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )
    )
    try:
        await asyncio.wait_for(written.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not manager.restores, (
            "live write was abandoned before compensation"
        )
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert admin.current == previous and admin.state == previous_state
        assert manager.active_version == "1.0.0"
        assert manager.restores == [(tenant_environment_id(TENANT_ID, entry.id), "1.0.0")]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
