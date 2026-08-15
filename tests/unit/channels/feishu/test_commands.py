from __future__ import annotations

import random
import tarfile
import zipfile
from collections.abc import AsyncIterator
from io import BytesIO
from uuid import UUID

import pytest

from agent_hub.api.routers.admin import InMemoryAdminResourceService
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.channels.base import (
    AttachmentKind,
    Channel,
    ConversationType,
    InboundAttachment,
    InboundMessage,
)
from agent_hub.channels.feishu.cards import (
    FeishuActionError,
    FeishuCardBuilder,
    InMemoryFeishuActionStore,
    consume_approval_action,
    consume_mode_action,
)
from agent_hub.channels.feishu.commands import (
    FeishuCommandKind,
    FeishuCommandParser,
)
from agent_hub.channels.feishu.settings import FeishuSettings
from agent_hub.channels.feishu.skill_install import FeishuSkillCommandHandler
from agent_hub.domain.runs import TaskMode

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")


def principal(role: Role) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(user_id=USER_ID, tenant_id=TENANT_ID, role=role)


class FakeFeishuFileClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.resource_types: list[str] = []

    async def tenant_access_token(self, tenant_external_id: str) -> str:
        assert tenant_external_id == "tenant_1"
        return "tenant-token"

    async def download_resource(
        self,
        *,
        message_id: str,
        resource_key: str,
        tenant_access_token: str,
        resource_type: str = "image",
    ) -> AsyncIterator[bytes]:
        assert message_id == "om_skill"
        assert resource_key == "file_1"
        assert tenant_access_token == "tenant-token"
        self.resource_types.append(resource_type)
        yield self.payload


def skill_archive() -> bytes:
    manifest = (
        "name: feishu_writer\n"
        "version: 1.0.0\n"
        "entry_point: main.py\n"
        "compatible_runtime: python3\n"
        "declared_tools: []\n"
        "network_policy:\n"
        "  mode: none\n"
        "  allow_hosts: []\n"
        "writable_paths: []\n"
        "env_secret_refs: []\n"
        "dependency_lock_hash: e3b0c44298fc1c149afbf4c8996fb924"
        "27ae41e4649b934ca495991b7852b855\n"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("skill.yaml", manifest)
        archive.writestr("main.py", "print('ok')\n")
    return buffer.getvalue()


def instruction_skill_bundle_archive() -> bytes:
    docs = {
        "writer": "---\nname: feishu-writer-skill\ndescription: Writer skill.\n---\n\nWrite drafts.\n",
        "reviewer": "---\nname: feishu-reviewer-skill\ndescription: Review checklist.\n---\n\nReview outputs.\n",
    }
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for folder, content in docs.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(f"all-skills/{folder}/SKILL.md")
            info.size = len(data)
            archive.addfile(info, BytesIO(data))
    return buffer.getvalue()


def large_instruction_skill_bundle_archive() -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for index in range(2):
            skill_name = f"feishu-large-skill-{index}"
            skill_md = (
                "---\n"
                f"name: {skill_name}\n"
                "description: Large Feishu Skill bundle.\n"
                "---\n\n"
                "Use this skill with large reference material.\n"
            ).encode()
            skill_info = tarfile.TarInfo(f"all-skills/{skill_name}/SKILL.md")
            skill_info.size = len(skill_md)
            archive.addfile(skill_info, BytesIO(skill_md))
            reference = random.Random(index).randbytes(1_200_000)
            ref_info = tarfile.TarInfo(f"all-skills/{skill_name}/references/source.txt")
            ref_info.size = len(reference)
            archive.addfile(ref_info, BytesIO(reference))
    payload = buffer.getvalue()
    assert len(payload) > 2_000_000
    assert len(payload) < 20_000_000
    return payload


def inbound_skill_file_message(text: str, *, filename: str = "feishu-writer.zip") -> InboundMessage:
    return InboundMessage(
        channel=Channel.FEISHU,
        tenant_external_id="tenant_1",
        sender_external_id="ou_admin",
        conversation_external_id="oc_1",
        message_id="om_skill",
        event_id="evt_skill",
        conversation_type=ConversationType.PRIVATE,
        text=text,
        mentions_bot=False,
        attachments=(
            InboundAttachment(
                kind=AttachmentKind.FILE,
                external_key="file_1",
                filename=filename,
                declared_mime="application/zip",
            ),
        ),
        received_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )


def test_non_admin_cannot_publish_config() -> None:
    result = FeishuCommandParser().parse(
        "/config publish 12",
        principal(Role.OPERATOR),
    )

    assert result.error_code == "permission_denied"


def test_admin_can_publish_config_revision() -> None:
    result = FeishuCommandParser().parse(
        "/config publish 12",
        principal(Role.ADMIN),
    )

    assert result.accepted is True
    assert result.command is not None
    assert result.command.kind is FeishuCommandKind.CONFIG
    assert result.command.name == "publish"
    assert result.command.arguments == ("12",)


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("/auto", TaskMode.AUTO),
        ("/direct", TaskMode.DIRECT),
        ("/dispatch", TaskMode.DISPATCH),
        ("/discuss", TaskMode.DISCUSS),
        ("/hybrid", TaskMode.HYBRID),
    ],
)
def test_mode_commands_are_explicit(text: str, mode: TaskMode) -> None:
    result = FeishuCommandParser().parse(text, principal(Role.OPERATOR))

    assert result.accepted is True
    assert result.command is not None
    assert result.command.kind is FeishuCommandKind.MODE
    assert result.command.mode is mode


def test_unknown_flags_and_unsafe_arguments_are_rejected() -> None:
    parser = FeishuCommandParser()

    assert parser.parse("/status --json", principal(Role.OPERATOR)).error_code == "unknown_flag"
    assert (
        parser.parse("/config publish revision: 1", principal(Role.ADMIN)).error_code
        == "invalid_argument"
    )
    assert parser.parse("/config raw foo", principal(Role.ADMIN)).error_code == "unknown_subcommand"


def test_run_commands_require_specific_permissions() -> None:
    parser = FeishuCommandParser()

    assert parser.parse("/cancel run_1", principal(Role.VIEWER)).error_code == "permission_denied"
    result = parser.parse("/cancel run_1", principal(Role.OPERATOR))

    assert result.accepted is True
    assert result.command is not None
    assert result.command.name == "cancel"


def test_skill_and_mcp_use_structured_subcommands() -> None:
    parser = FeishuCommandParser()

    assert parser.parse("/skill approve pkg_1", principal(Role.ADMIN)).accepted is True
    assert parser.parse("/skill install pkg_1", principal(Role.ADMIN)).accepted is True
    assert parser.parse("/mcp enable server_1", principal(Role.ADMIN)).accepted is True
    assert parser.parse("/skill approve pkg_1", principal(Role.OPERATOR)).error_code == (
        "permission_denied"
    )


async def test_feishu_skill_install_uploads_attached_archive_for_scan_only() -> None:
    service = InMemoryAdminResourceService()
    file_client = FakeFeishuFileClient(skill_archive())
    handler = FeishuSkillCommandHandler(
        admin_service=service,
        media_client_factory=lambda _settings: file_client,
    )

    result = await handler.handle(
        inbound_skill_file_message("/skill install"),
        settings=FeishuSettings(),
    )

    assert result is not None
    assert result.handled is True
    assert "feishu_writer" in result.reply_text
    assert "待审批" in result.reply_text
    assert file_client.resource_types == ["file"]
    skills = await service.list_skills()
    assert len(skills) == 1
    assert skills[0].name == "feishu_writer"
    assert skills[0].status == "scanned"


async def test_feishu_skill_install_uploads_instruction_bundle_for_scan_only() -> None:
    service = InMemoryAdminResourceService()
    file_client = FakeFeishuFileClient(instruction_skill_bundle_archive())
    handler = FeishuSkillCommandHandler(
        admin_service=service,
        media_client_factory=lambda _settings: file_client,
    )

    result = await handler.handle(
        inbound_skill_file_message("/skill install", filename="all-skills.tar.gz"),
        settings=FeishuSettings(),
    )

    assert result is not None
    assert result.handled is True
    assert "feishu-writer-skill" in result.reply_text
    assert "feishu-reviewer-skill" in result.reply_text
    skills = await service.list_skills()
    assert [skill.name for skill in skills] == ["feishu-writer-skill", "feishu-reviewer-skill"]
    assert all(skill.status == "scanned" for skill in skills)

async def test_feishu_skill_install_accepts_large_multi_skill_archives_by_default() -> None:
    service = InMemoryAdminResourceService()
    file_client = FakeFeishuFileClient(large_instruction_skill_bundle_archive())
    handler = FeishuSkillCommandHandler(
        admin_service=service,
        media_client_factory=lambda _settings: file_client,
    )

    result = await handler.handle(
        inbound_skill_file_message("/skill install", filename="large-all-skills.tar.gz"),
        settings=FeishuSettings(),
    )

    assert result is not None
    assert result.handled is True
    assert "feishu-large-skill-0" in result.reply_text
    assert "feishu-large-skill-1" in result.reply_text
    skills = await service.list_skills()
    assert [skill.name for skill in skills] == ["feishu-large-skill-0", "feishu-large-skill-1"]


async def test_feishu_skill_install_requires_file_attachment() -> None:
    handler = FeishuSkillCommandHandler(
        admin_service=InMemoryAdminResourceService(),
        media_client_factory=lambda _settings: FakeFeishuFileClient(skill_archive()),
    )
    message = inbound_skill_file_message("/skill install").model_copy(update={"attachments": ()})

    result = await handler.handle(message, settings=FeishuSettings())

    assert result is not None
    assert result.handled is True
    assert "请附加" in result.reply_text


async def test_feishu_skill_approval_is_not_executed_from_channel_message() -> None:
    handler = FeishuSkillCommandHandler(
        admin_service=InMemoryAdminResourceService(),
        media_client_factory=lambda _settings: FakeFeishuFileClient(skill_archive()),
    )

    result = await handler.handle(
        inbound_skill_file_message("/skill approve skill_1"),
        settings=FeishuSettings(),
    )

    assert result is not None
    assert result.handled is True
    assert "管理端审批" in result.reply_text


def test_mode_card_action_is_one_time_and_bound_to_actor() -> None:
    store = InMemoryFeishuActionStore()
    card = FeishuCardBuilder(store).mode_choice_card(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        run_id=RUN_ID,
        options=(TaskMode.DIRECT, TaskMode.DISPATCH),
        reason="low_confidence",
    )
    actions = card.payload["actions"]
    assert isinstance(actions, list)
    action = actions[0]
    assert isinstance(action, dict)
    value = action["value"]
    assert isinstance(value, dict)
    action_id = value["action_id"]
    assert isinstance(action_id, str)

    with pytest.raises(FeishuActionError, match="mismatch"):
        consume_mode_action(
            store,
            action_id,
            tenant_id=TENANT_ID,
            user_id=OTHER_USER_ID,
            run_id=RUN_ID,
            mode=TaskMode.DIRECT,
            index=1,
        )

    consume_mode_action(
        store,
        action_id,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        run_id=RUN_ID,
        mode=TaskMode.DIRECT,
        index=1,
    )
    with pytest.raises(FeishuActionError, match="not found"):
        consume_mode_action(
            store,
            action_id,
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            run_id=RUN_ID,
            mode=TaskMode.DIRECT,
            index=1,
        )


def test_approval_card_action_expires() -> None:
    now = 10.0
    store = InMemoryFeishuActionStore(monotonic=lambda: now)
    card = FeishuCardBuilder(store, action_ttl_seconds=1).approval_card(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        run_id=RUN_ID,
        approval_id="approval_1",
        summary="read external URL",
    )
    actions = card.payload["actions"]
    assert isinstance(actions, list)
    action = actions[0]
    assert isinstance(action, dict)
    value = action["value"]
    assert isinstance(value, dict)
    action_id = value["action_id"]
    assert isinstance(action_id, str)
    now = 12.0

    with pytest.raises(FeishuActionError, match="expired"):
        consume_approval_action(
            store,
            action_id,
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            run_id=RUN_ID,
            approval_id="approval_1",
            decision="approve",
        )
