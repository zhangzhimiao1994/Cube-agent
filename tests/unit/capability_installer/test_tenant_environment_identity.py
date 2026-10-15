"""Real immutable environments must not share activation pointers across tenants."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from agent_hub.capability_installer.catalog import (
    CliArtifactEnvironmentRecipe,
    TrustedCapabilityCatalog,
)
from agent_hub.capability_installer.environment import CapabilityEnvironmentManager
from agent_hub.capability_installer.service import CapabilityInstallerService
from tests.unit.capability_installer.test_service import InstallingAdminService, _managed_entry

TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")


async def test_same_logical_capability_has_separate_tenant_environment_state(
    tmp_path: Path,
) -> None:
    source = tmp_path / "controlled-cli"
    source.write_bytes(b"controlled executable")
    manager = CapabilityEnvironmentManager(tmp_path / "environments", cli_probe=lambda *_: None)
    ids: list[str] = []
    admins: list[InstallingAdminService] = []
    installers: list[CapabilityInstallerService] = []
    for tenant, version in ((TENANT_A, "1.0.0"), (TENANT_B, "2.0.0")):
        entry = _managed_entry(
            CliArtifactEnvironmentRecipe(
                version=version,
                artifact_path=source,
                sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                executable_name="tool",
            )
        )
        installer = CapabilityInstallerService(
            catalog=TrustedCapabilityCatalog((entry,)),
            environment_manager=manager,
        )
        admin = InstallingAdminService()
        plan = installer.plan(entry.id, query="controlled")
        await installer.install(
            admin,
            entry_id=entry.id,
            query=plan.query,
            plan_id=plan.id,
            confirm=True,
            tenant_id=tenant,
            actor_id=tenant,
        )
        assert admin.state is not None
        ids.append(admin.state["environment_capability_id"])
        admins.append(admin)
        installers.append(installer)
    assert ids[0] != ids[1]
    assert manager.active(ids[0]).version == "1.0.0"
    assert manager.active(ids[1]).version == "2.0.0"
    await installers[1].rollback(
        admins[1],
        entry_id="managed_tool",
        query="controlled",
        tenant_id=TENANT_B,
        actor_id=TENANT_B,
    )
    assert manager.active(ids[0]).version == "1.0.0"
