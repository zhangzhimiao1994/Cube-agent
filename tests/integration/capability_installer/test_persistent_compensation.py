"""Real PG compensation checks with one-shot after-commit service wrappers.

The wrappers await real admin commits before raising ConnectionError. They model
an unconfirmed call result, not a wire-level PostgreSQL COMMIT-ack failure.
CLI smoke/preflight checks read local fixture bytes; no process or network probe
is performed. Parent integration fixtures own the isolated database lifecycle.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.api.routers.admin import (
    PersistentAdminResourceService,
    PluginCapabilityRequest,
    PluginPackageMetadata,
    PluginResourceRequest,
    PluginResourceResponse,
)
from agent_hub.capability_installer.catalog import (
    CapabilityCatalogEntry,
    CapabilityInstallPlan,
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
from agent_hub.config.service import ConfigService
from agent_hub.db.models import AdminResourceRow, TenantRow
from agent_hub.security.secrets import SecretCipher, SecretService
from tests.integration.capabilities.test_approval import (
    run_session_factory as shared_run_session_factory,
)

pytestmark = pytest.mark.integration
run_session_factory = shared_run_session_factory

ENTRY_ID = "persistent_compensation"
PLUGIN_ID = "persistent-compensation"
OLD_CLI = b"offline compensation SDK version 1; never executed\n"
NEW_CLI = b"offline compensation SDK version 2; never executed\n"
type FaultMethod = Literal[
    "upsert_plugin", "upsert_capability_install_state", "delete_capability_install_state"
]


@dataclass(frozen=True)
class InstallContext:
    factory: async_sessionmaker[AsyncSession]
    tenant_id: UUID
    actor_id: UUID
    admin: PersistentAdminResourceService
    manager: CapabilityEnvironmentManager
    installer: CapabilityInstallerService
    entry: CapabilityCatalogEntry
    old_artifact: Path

    @property
    def environment_id(self) -> str:
        return tenant_environment_id(self.tenant_id, ENTRY_ID)


@dataclass(frozen=True)
class Snapshot:
    payloads: dict[str, dict[str, object]]
    active: EnvironmentRecord | None


def _local_cli_probe(executable: Path, _cwd: Path) -> None:
    assert executable.read_bytes() in (OLD_CLI, NEW_CLI)


@pytest.fixture
async def install_context(
    run_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path,
) -> AsyncIterator[InstallContext]:
    tenant_id, actor_id = uuid4(), uuid4()
    async with run_session_factory() as session, session.begin():
        session.add(TenantRow(id=tenant_id, slug=f"compensation-{tenant_id.hex}", name="Test"))
    old_artifact, new_artifact = tmp_path / "old-cli", tmp_path / "new-cli"
    old_artifact.write_bytes(OLD_CLI)
    new_artifact.write_bytes(NEW_CLI)
    manager = CapabilityEnvironmentManager(tmp_path / "sdk", cli_probe=_local_cli_probe)
    entry = CapabilityCatalogEntry(
        id=ENTRY_ID,
        name_cn="Persistent compensation fixture",
        summary_cn="Offline SDK and real admin persistence compensation",
        risks=("code_execution",),
        permission_summary=("Execute the offline fixture",),
        environment_recipe=CliArtifactEnvironmentRecipe(
            version="2.0.0", artifact_path=new_artifact,
            sha256=hashlib.sha256(NEW_CLI).hexdigest(), executable_name="tool",
        ),
        plugin=PluginResourceRequest(
            id=PLUGIN_ID, name="New SDK plugin", version="2.0.0",
            resource_config={"command": "managed-by-environment"},
            capabilities=[PluginCapabilityRequest(
                id="fixture.run", adapter="local_command", permission_class="plugin.use",
                sandbox_profile="local_process", policy_effect="require_approval",
            )],
        ),
    )
    admin = PersistentAdminResourceService(
        config_service=ConfigService(run_session_factory),
        secret_service=SecretService(run_session_factory, SecretCipher(b"x" * 32)),
        tenant_id=tenant_id, actor_id=actor_id, session_factory=run_session_factory,
        skill_store_dir=tmp_path / "skills",
    )
    installer = CapabilityInstallerService(
        TrustedCapabilityCatalog((entry,)), environment_manager=manager, environment={},
    )
    try:
        yield InstallContext(
            run_session_factory, tenant_id, actor_id, admin, manager, installer, entry, old_artifact,
        )
    finally:
        # The reused run fixture does not delete AdminResourceRow (including audit rows).
        async with run_session_factory() as session, session.begin():
            await session.execute(delete(AdminResourceRow).where(
                AdminResourceRow.tenant_id == tenant_id,
            ))
            await session.execute(delete(TenantRow).where(TenantRow.id == tenant_id))


def _request(plugin: PluginResourceResponse) -> PluginResourceRequest:
    return PluginResourceRequest.model_validate(
        plugin.model_dump(include=set(PluginResourceRequest.model_fields)),
    )


async def _snapshot(context: InstallContext) -> Snapshot:
    # A separate session observes committed data, not the method's return value/cache.
    async with context.factory() as session:
        rows = (await session.scalars(select(AdminResourceRow).where(
            AdminResourceRow.tenant_id == context.tenant_id,
            AdminResourceRow.kind.in_(("plugin", "capability_install")),
        ))).all()
        assert all(row.resource_id == (
            PLUGIN_ID if row.kind == "plugin" else ENTRY_ID
        ) for row in rows)
        assert len(rows) == len({row.kind for row in rows})
        payloads = {row.kind: deepcopy(row.payload) for row in rows}
    try:
        active = context.manager.active(context.environment_id)
    except KeyError:
        active = None
    return Snapshot(payloads, active)


async def _seed_previous(
    context: InstallContext, *, replaced: bool, blocked_package: bool = True,
) -> None:
    assert (await _snapshot(context)).payloads == {}
    if not replaced:
        return
    previous = context.manager.deploy_cli(CliArtifactSpec(
        context.environment_id, "1.0.0", context.old_artifact,
        hashlib.sha256(OLD_CLI).hexdigest(), "tool",
    ))
    await context.admin.upsert_plugin(
        context.entry.plugin.model_copy(update={
            "name": "Previous SDK plugin", "version": "1.0.0",
            "description": "Preserve the exact previous plugin request",
            "resource_config": {"command": str(previous.path / previous.executable)},
        }),
        tenant_id=context.tenant_id, actor_id=context.actor_id,
        source_filename="previous.zip", content_sha256="b" * 64,
        package_metadata=PluginPackageMetadata(
            kind="adapter_package" if blocked_package else "manifest_only",
            package_version="1.0.0", runtime="python",
            entrypoint="previous.py", isolation="local_process",
        ),
    )
    context.manager.add_reference(context.environment_id, "1.0.0", "previous-owner")
    await context.admin.upsert_capability_install_state(
        ENTRY_ID,
        {
            "entry_id": ENTRY_ID, "plugin_id": PLUGIN_ID, "plan_id": "previous-plan",
            "query": "previous query", "environment_managed": "true",
            "environment_capability_id": context.environment_id, "environment_version": "1.0.0",
            "environment_reference_owner": "previous-owner",
            "environment_pinned_versions": '["1.0.0"]', "keep": "exact previous state",
        },
        tenant_id=context.tenant_id, actor_id=context.actor_id,
    )


async def _install(context: InstallContext) -> tuple[CapabilityInstallPlan, PluginResourceResponse]:
    async def offline_preflight(plugin: PluginResourceResponse) -> None:
        assert Path(str(plugin.resource_config["command"])).read_bytes() == NEW_CLI

    plan = context.installer.plan(ENTRY_ID, query="persistent compensation fixture")
    installed_plan, plugin = await context.installer.install(
        context.admin, entry_id=ENTRY_ID, query=plan.query, plan_id=plan.id, confirm=True,
        tenant_id=context.tenant_id, actor_id=context.actor_id,
        activation_preflight=offline_preflight,
    )
    assert installed_plan.status == "installed"
    assert isinstance(plugin, PluginResourceResponse)
    return installed_plan, plugin


def _fail_once_after_commit(
    context: InstallContext, monkeypatch: pytest.MonkeyPatch, method: FaultMethod,
) -> list[Snapshot]:
    original = getattr(context.admin, method)
    committed: list[Snapshot] = []

    async def after_commit(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        if not committed:
            committed.append(await _snapshot(context))
            raise ConnectionError(f"after-commit {method} acknowledgement lost")
        return result

    monkeypatch.setattr(context.admin, method, after_commit)
    return committed


async def _assert_restored(context: InstallContext, before: Snapshot) -> None:
    assert await _snapshot(context) == before
    plugins = await context.admin.list_plugins(tenant_id=context.tenant_id)
    assert all(isinstance(plugin, PluginResourceResponse) for plugin in plugins)
    if "plugin" in before.payloads:
        assert len(plugins) == 1
        assert plugins[0].model_dump(mode="json") == before.payloads["plugin"]
        assert plugins[0].source_filename is not None
        assert plugins[0].content_sha256 is not None
        assert plugins[0].package_metadata is not None
        expected_approval = (
            "pending" if plugins[0].package_metadata.kind == "adapter_package" else "not_required"
        )
        assert plugins[0].package_metadata.approval_state == expected_approval
        assert plugins[0].status == "stopped", "compensation must not activate a stopped baseline"
    else:
        assert plugins == ()
    assert await context.admin.get_capability_install_state(
        ENTRY_ID, tenant_id=context.tenant_id,
    ) == before.payloads.get("capability_install")
    if before.active is not None:
        assert (before.active.path / before.active.executable).read_bytes() in (OLD_CLI, NEW_CLI)


async def _check_install_after_commit(
    context: InstallContext, monkeypatch: pytest.MonkeyPatch,
    *, replaced: bool, method: Literal["upsert_plugin", "upsert_capability_install_state"],
    blocked_package: bool = True,
) -> None:
    await _seed_previous(context, replaced=replaced, blocked_package=blocked_package)
    before = await _snapshot(context)
    committed = _fail_once_after_commit(context, monkeypatch, method)

    with pytest.raises(ConnectionError, match=f"after-commit {method} acknowledgement lost"):
        await _install(context)

    assert len(committed) == 1
    observed = committed[0]
    assert observed.active is not None and observed.active.version == "2.0.0"
    assert (observed.active.path / observed.active.executable).read_bytes() == NEW_CLI
    assert observed.payloads["plugin"]["version"] == "2.0.0"
    assert observed.payloads["plugin"]["resource_config"] == {
        "command": str(observed.active.path / observed.active.executable),
    }
    if method == "upsert_plugin":
        assert observed.payloads.get("capability_install") == before.payloads.get("capability_install")
    else:
        state = observed.payloads["capability_install"]
        plan = context.installer.plan(ENTRY_ID, query="persistent compensation fixture")
        assert state["plan_id"] == plan.id
        assert json.loads(str(state["confirmed_plan"])) == plan.model_dump(
            mode="json", exclude={"status"},
        )
        assert state["environment_capability_id"] == context.environment_id
        assert state["environment_version"] == "2.0.0"
        assert state["replaced_existing"] == ("true" if replaced else "false")
    await _assert_restored(context, before)


async def _check_rollback_state_delete_after_commit(
    context: InstallContext, monkeypatch: pytest.MonkeyPatch, *, replaced: bool,
) -> None:
    await _seed_previous(context, replaced=replaced)
    _, plugin = await _install(context)
    # Restore a response with real response-only metadata; activation must stay blocked.
    await context.admin.upsert_plugin(
        _request(plugin), tenant_id=context.tenant_id, actor_id=context.actor_id,
        source_filename="installed.zip", content_sha256="c" * 64,
        package_metadata=PluginPackageMetadata(
            kind="adapter_package", package_version="2.0.0", runtime="python",
            entrypoint="installed.py", isolation="local_process",
        ),
    )
    before = await _snapshot(context)
    plugins = await context.admin.list_plugins(tenant_id=context.tenant_id)
    assert len(plugins) == 1 and isinstance(plugins[0], PluginResourceResponse)
    assert plugins[0].source_filename == "installed.zip"
    assert plugins[0].content_sha256 == "c" * 64
    committed = _fail_once_after_commit(context, monkeypatch, "delete_capability_install_state")

    with pytest.raises(ConnectionError, match="after-commit delete_capability_install_state"):
        await context.installer.rollback(
            context.admin, entry_id=ENTRY_ID, query="persistent compensation fixture",
            tenant_id=context.tenant_id, actor_id=context.actor_id,
        )

    assert len(committed) == 1
    observed = committed[0]
    assert "capability_install" not in observed.payloads
    if replaced:
        assert observed.active is not None and observed.active.version == "1.0.0"
        assert observed.payloads["plugin"]["version"] == "1.0.0"
    else:
        assert observed.active is None
        assert observed.payloads == {}
    await _assert_restored(context, before)


async def test_first_install_plugin_after_commit_error_restores_persistent_state(
    install_context: InstallContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _check_install_after_commit(
        install_context, monkeypatch, replaced=False, method="upsert_plugin",
    )


async def test_overwrite_plugin_after_commit_error_restores_persistent_state(
    install_context: InstallContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _check_install_after_commit(
        install_context, monkeypatch, replaced=True, method="upsert_plugin",
    )


async def test_first_install_state_after_commit_error_restores_persistent_state(
    install_context: InstallContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _check_install_after_commit(
        install_context, monkeypatch, replaced=False, method="upsert_capability_install_state",
    )


async def test_overwrite_state_after_commit_error_restores_persistent_state(
    install_context: InstallContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _check_install_after_commit(
        install_context, monkeypatch, replaced=True, method="upsert_capability_install_state",
    )


async def test_stopped_baseline_is_not_activated_by_compensation(
    install_context: InstallContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _check_install_after_commit(
        install_context, monkeypatch, replaced=True, method="upsert_capability_install_state",
        blocked_package=False,
    )


async def test_first_install_rollback_state_delete_error_restores_response_and_sdk(
    install_context: InstallContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _check_rollback_state_delete_after_commit(install_context, monkeypatch, replaced=False)


async def test_overwrite_rollback_state_delete_error_restores_response_and_sdk(
    install_context: InstallContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _check_rollback_state_delete_after_commit(install_context, monkeypatch, replaced=True)
