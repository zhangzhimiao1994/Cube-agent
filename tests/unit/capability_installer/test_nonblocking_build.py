from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import cast

import pytest

from agent_hub.capability_installer.catalog import (
    CliArtifactEnvironmentRecipe,
    TrustedCapabilityCatalog,
)
from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    CliArtifactSpec,
    EnvironmentRecord,
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


async def test_environment_build_is_nonblocking_and_cancel_drains_before_restore(
    tmp_path: Path,
) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    class Manager(RecordingEnvironmentManager):
        def deploy_cli(self, spec: CliArtifactSpec) -> EnvironmentRecord:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(2), "environment build blocked the request event loop"
            return super().deploy_cli(spec)

    manager = Manager(tmp_path / "environments", active_version="1.0.0")
    entry = _managed_entry(
        CliArtifactEnvironmentRecipe(
            version="2.0.0",
            artifact_path=tmp_path / "tool",
            sha256="a" * 64,
            executable_name="tool",
        )
    )
    service = CapabilityInstallerService(
        TrustedCapabilityCatalog((entry,)),
        environment_manager=cast(CapabilityEnvironmentManager, manager),
    )
    admin = InstallingAdminService()
    plan = service.plan(entry.id, query="nonblocking-owned-build")
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
        await asyncio.wait_for(started.wait(), 3)
        assert not task.done(), "build prevented other requests from making progress"
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not manager.restores, "rollback raced with a live build"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.active_version == "1.0.0"
        assert manager.restores == [(tenant_environment_id(TENANT_ID, entry.id), "1.0.0")]
        assert admin.current is None and admin.state is None
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
