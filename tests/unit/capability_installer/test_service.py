from __future__ import annotations

from uuid import UUID

import pytest

from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceRequest
from agent_hub.capability_installer.catalog import CapabilityCatalogEntry, TrustedCapabilityCatalog
from agent_hub.capability_installer.service import (
    CapabilityInstallerService,
    CapabilityInstallUnavailable,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")


class RecordingAdminService:
    def __init__(self) -> None:
        self.upserts = 0

    async def list_plugins(self, *, tenant_id: UUID) -> tuple[object, ...]:
        del tenant_id
        return ()

    async def upsert_plugin(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.upserts += 1


@pytest.mark.asyncio
async def test_installer_rejects_configured_http_capability_without_real_endpoint() -> None:
    installer = CapabilityInstallerService()
    plan = installer.plan("office_doc_search", query="读取 Office 文档并搜索")
    admin = RecordingAdminService()

    with pytest.raises(CapabilityInstallUnavailable, match="真实 HTTP endpoint"):
        await installer.install(
            admin,
            entry_id="office_doc_search",
            query="读取 Office 文档并搜索",
            plan_id=plan.id,
            confirm=True,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )

    assert admin.upserts == 0


@pytest.mark.asyncio
async def test_installer_rejects_local_command_capability_when_command_is_missing() -> None:
    installer = CapabilityInstallerService(
        catalog=TrustedCapabilityCatalog(
            (
                CapabilityCatalogEntry(
                    id="missing_cli",
                    name_cn="缺失命令能力",
                    summary_cn="测试缺失本地命令时不会写入插件。",
                    risks=("code_execution",),
                    permission_summary=("运行本地命令",),
                    plugin=PluginResourceRequest(
                        id="missing-cli",
                        name="Missing CLI",
                        resource_config={"command": "agent-hub-test-missing-cli"},
                        capabilities=[
                            PluginCapabilityRequest(
                                id="missing.run",
                                adapter="local_command",
                                permission_class="plugin.use",
                                sandbox_profile="local_process",
                            )
                        ],
                    ),
                ),
            )
        ),
        command_resolver=lambda _command: None,
    )
    plan = installer.plan("missing_cli", query="需要缺失命令能力")
    admin = RecordingAdminService()

    with pytest.raises(CapabilityInstallUnavailable, match="agent-hub-test-missing-cli"):
        await installer.install(
            admin,
            entry_id="missing_cli",
            query="需要缺失命令能力",
            plan_id=plan.id,
            confirm=True,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )

    assert admin.upserts == 0


@pytest.mark.asyncio
async def test_installer_rejects_local_command_capability_when_dependency_command_is_missing() -> None:
    installer = CapabilityInstallerService(
        command_resolver=lambda command: "C:/tools/strix.exe" if command == "strix" else None
    )
    plan = installer.plan("security_testing", query="需要 strix 自动化渗透能力")
    admin = RecordingAdminService()

    with pytest.raises(CapabilityInstallUnavailable, match="docker"):
        await installer.install(
            admin,
            entry_id="security_testing",
            query="需要 strix 自动化渗透能力",
            plan_id=plan.id,
            confirm=True,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
        )

    assert admin.upserts == 0
