from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from agent_hub.channels.base import (
    AttachmentKind,
    Channel,
    ConversationType,
    InboundAttachment,
    InboundMessage,
)
from agent_hub.channels.directives import (
    ChannelDirectiveError,
    apply_channel_command_aliases,
    command_help_text,
    is_channel_help_request,
    parse_channel_directives,
    parse_command_aliases,
)
from agent_hub.channels.submitter import RunServiceInboundSubmitter
from agent_hub.domain.runs import TaskMode

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
RUN_ID = UUID("11111111-1111-4111-8111-111111111111")


@dataclass(slots=True)
class SubmittedRun:
    id: UUID


class RecordingRunService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def submit(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        attachment_ids: tuple[str, ...] = (),
        channel_context: dict[str, str] | None = None,
        vibe_coding: bool = False,
        idempotency_key: str | None = None,
    ) -> SubmittedRun:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "message": message,
                "mode": mode,
                "attachment_ids": attachment_ids,
                "channel_context": channel_context,
                "vibe_coding": vibe_coding,
                "idempotency_key": idempotency_key,
            }
        )
        return SubmittedRun(RUN_ID)


@dataclass(frozen=True, slots=True)
class StubSystemSettings:
    vibe_coding_enabled: bool = False


class StubSettingsService:
    def __init__(self, *, vibe_coding_enabled: bool) -> None:
        self.settings = StubSystemSettings(vibe_coding_enabled=vibe_coding_enabled)

    async def get_settings(self) -> StubSystemSettings:
        return self.settings


async def test_submitter_forwards_agent_hub_attachment_ids_and_manifest() -> None:
    run_service = RecordingRunService()
    submitter = RunServiceInboundSubmitter(run_service=run_service, tenant_id=TENANT_ID)
    message = InboundMessage(
        channel=Channel.CUSTOM_WEBHOOK,
        tenant_external_id="tenant_1",
        sender_external_id="user_1",
        conversation_external_id="conv_1",
        message_id="msg_1",
        event_id="evt_1",
        conversation_type=ConversationType.PRIVATE,
        text="/dispatch Review this image",
        mentions_bot=True,
        attachments=(
            InboundAttachment(
                kind=AttachmentKind.IMAGE,
                external_key="att_0123456789abcdef0123456789abcdef",
                filename="screen.png",
                declared_mime="image/png",
            ),
            InboundAttachment(
                kind=AttachmentKind.FILE,
                external_key="platform_file_1",
                filename="notes.txt",
                declared_mime="text/plain",
            ),
        ),
        received_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    run_id = await submitter.submit(message, idempotency_key="idem_1")

    assert run_id == RUN_ID
    assert len(run_service.calls) == 1
    call = run_service.calls[0]
    assert call["mode"] is TaskMode.DISPATCH
    assert call["attachment_ids"] == ("att_0123456789abcdef0123456789abcdef",)
    assert "Review this image" in str(call["message"])
    assert "Channel attachments:" in str(call["message"])
    assert "filename=screen.png" in str(call["message"])
    assert "external_key=platform_file_1" in str(call["message"])
    context = call["channel_context"]
    assert isinstance(context, dict)
    assert context["source_channel"] == "custom_webhook"
    assert context["channel_message_id"] == "msg_1"


async def test_submitter_parses_channel_directives_for_mode_skills_mcp_and_plugins() -> None:
    run_service = RecordingRunService()
    submitter = RunServiceInboundSubmitter(run_service=run_service, tenant_id=TENANT_ID)
    message = InboundMessage(
        channel=Channel.FEISHU,
        tenant_external_id="tenant_1",
        sender_external_id="user_1",
        conversation_external_id="conv_1",
        message_id="msg_1",
        event_id="evt_1",
        conversation_type=ConversationType.PRIVATE,
        text="/dispatch &deep-research &pdf /#filesystem @github Review this repo",
        mentions_bot=True,
        received_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    await submitter.submit(message, idempotency_key="idem_1")

    call = run_service.calls[0]
    assert call["mode"] is TaskMode.DISPATCH
    assert call["message"] == "Review this repo"
    context = call["channel_context"]
    assert isinstance(context, dict)
    assert context["requested_skills"] == "deep-research,pdf"
    assert context["requested_mcp_servers"] == "filesystem"
    assert context["requested_plugins"] == "github"


async def test_submitter_parses_english_channel_language_directives_for_mode_and_vibe() -> None:
    run_service = RecordingRunService()
    submitter = RunServiceInboundSubmitter(
        run_service=run_service,
        tenant_id=TENANT_ID,
        settings_service=StubSettingsService(vibe_coding_enabled=True),
    )
    message = InboundMessage(
        channel=Channel.FEISHU,
        tenant_external_id="tenant_1",
        sender_external_id="user_1",
        conversation_external_id="conv_1",
        message_id="msg_1",
        event_id="evt_1",
        conversation_type=ConversationType.PRIVATE,
        text="//hybrid //vi Refactor this module with context compression",
        mentions_bot=True,
        received_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    directives = parse_channel_directives(message.text)
    await submitter.submit(message, idempotency_key="idem_1")

    assert directives.mode is TaskMode.HYBRID
    assert directives.vibe_coding is True
    assert directives.task_text == "Refactor this module with context compression"
    call = run_service.calls[0]
    assert call["mode"] is TaskMode.HYBRID
    assert call["vibe_coding"] is True
    assert call["message"] == "Refactor this module with context compression"
    context = call["channel_context"]
    assert isinstance(context, dict)
    assert context["requested_channel_features"] == "vibe_coding"


def test_channel_directives_accept_custom_aliases_and_show_help() -> None:
    aliases = parse_command_aliases("方案=//派单, 讨论=//讨论, 代码=//vi, 菜单=//帮助")

    normalized = apply_channel_command_aliases("方案 写一个中秋晚会方案", aliases)
    directives = parse_channel_directives(normalized)
    help_text = command_help_text(aliases)

    assert normalized == "//派单 写一个中秋晚会方案"
    assert apply_channel_command_aliases("菜单", aliases) == "//帮助"
    assert is_channel_help_request("菜单", aliases)
    assert is_channel_help_request("//help", aliases)
    assert directives.mode is TaskMode.DISPATCH
    assert directives.task_text == "写一个中秋晚会方案"
    assert "方案=//派单" in help_text
    assert "代码=//vi" in help_text


async def test_submitter_parses_chinese_channel_language_directives_and_rejects_disabled_vibe() -> (
    None
):
    run_service = RecordingRunService()
    submitter = RunServiceInboundSubmitter(
        run_service=run_service,
        tenant_id=TENANT_ID,
        settings_service=StubSettingsService(vibe_coding_enabled=False),
    )
    message = InboundMessage(
        channel=Channel.FEISHU,
        tenant_external_id="tenant_1",
        sender_external_id="user_1",
        conversation_external_id="conv_1",
        message_id="msg_1",
        event_id="evt_1",
        conversation_type=ConversationType.PRIVATE,
        text="//讨论 //代码协作 评审这个实现方案",
        mentions_bot=True,
        received_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    directives = parse_channel_directives(message.text)

    assert directives.mode is TaskMode.DISCUSS
    assert directives.vibe_coding is True
    assert directives.task_text == "评审这个实现方案"
    try:
        await submitter.submit(message, idempotency_key="idem_1")
    except ChannelDirectiveError as error:
        assert error.reason == "vibe_coding_disabled"
    else:  # pragma: no cover - assertion clarity
        raise AssertionError(
            "submitter accepted channel Vibe Coding while the system switch is off"
        )
    assert run_service.calls == []


async def test_submitter_rejects_malformed_channel_directives() -> None:
    run_service = RecordingRunService()
    submitter = RunServiceInboundSubmitter(run_service=run_service, tenant_id=TENANT_ID)
    message = InboundMessage(
        channel=Channel.FEISHU,
        tenant_external_id="tenant_1",
        sender_external_id="user_1",
        conversation_external_id="conv_1",
        message_id="msg_1",
        event_id="evt_1",
        conversation_type=ConversationType.PRIVATE,
        text="/dispatch /#bad! &skill_ok @github Review this repo",
        mentions_bot=True,
        received_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    directives = parse_channel_directives(message.text)

    assert directives.invalid_reason == "invalid_directive"
    try:
        await submitter.submit(message, idempotency_key="idem_1")
    except ChannelDirectiveError as error:
        assert error.reason == "invalid_directive"
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("submitter accepted malformed channel directive")
    assert run_service.calls == []
