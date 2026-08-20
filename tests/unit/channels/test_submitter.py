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
        self.choice_calls: list[dict[str, object]] = []
        self.choice_run_id: UUID | None = None

    async def submit(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        attachment_ids: tuple[str, ...] = (),
        conversation_id: str | None = None,
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
                "conversation_id": conversation_id,
                "channel_context": channel_context,
                "vibe_coding": vibe_coding,
                "idempotency_key": idempotency_key,
            }
        )
        return SubmittedRun(RUN_ID)

    async def choose_latest_choice_for_conversation(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        conversation_id: str,
        choice_key: str,
        operator_note: str | None = None,
    ) -> SubmittedRun | None:
        self.choice_calls.append(
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "conversation_id": conversation_id,
                "choice_key": choice_key,
                "operator_note": operator_note,
            }
        )
        if self.choice_run_id is None:
            return None
        return SubmittedRun(self.choice_run_id)


@dataclass(frozen=True, slots=True)
class StubSystemSettings:
    vibe_coding_enabled: bool = False


class StubSettingsService:
    def __init__(self, *, vibe_coding_enabled: bool) -> None:
        self.settings = StubSystemSettings(vibe_coding_enabled=vibe_coding_enabled)

    async def get_settings(self) -> StubSystemSettings:
        return self.settings


def _message(text: str, *, attachments: tuple[InboundAttachment, ...] = ()) -> InboundMessage:
    return InboundMessage(
        channel=Channel.FEISHU,
        tenant_external_id="tenant_1",
        sender_external_id="user_1",
        conversation_external_id="conv_1",
        message_id="msg_1",
        event_id="evt_1",
        conversation_type=ConversationType.PRIVATE,
        text=text,
        mentions_bot=True,
        attachments=attachments,
        received_at=datetime(2026, 8, 11, tzinfo=UTC),
    )


async def test_submitter_forwards_channel_text_to_main_agent_entry_with_attachments() -> None:
    run_service = RecordingRunService()
    submitter = RunServiceInboundSubmitter(run_service=run_service, tenant_id=TENANT_ID)
    message = _message(
        "/dispatch Review this image",
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
    )

    run_id = await submitter.submit(message, idempotency_key="idem_1")

    assert run_id == RUN_ID
    assert len(run_service.calls) == 1
    call = run_service.calls[0]
    assert call["mode"] is TaskMode.AUTO
    assert call["attachment_ids"] == ("att_0123456789abcdef0123456789abcdef",)
    assert isinstance(call["conversation_id"], str)
    assert str(call["conversation_id"]).startswith("ch-feishu-")
    assert "/dispatch Review this image" in str(call["message"])
    assert "Channel attachments:" in str(call["message"])
    assert "filename=screen.png" in str(call["message"])
    assert "external_key=platform_file_1" in str(call["message"])
    context = call["channel_context"]
    assert isinstance(context, dict)
    assert context["source_channel"] == "feishu"
    assert context["channel_entry_policy"] == "main_agent_decides"
    assert context["channel_message_id"] == "msg_1"


async def test_submitter_uses_same_internal_conversation_for_same_channel_thread() -> None:
    run_service = RecordingRunService()
    submitter = RunServiceInboundSubmitter(run_service=run_service, tenant_id=TENANT_ID)

    await submitter.submit(_message("第一轮"), idempotency_key="idem_1")
    await submitter.submit(_message("第二轮"), idempotency_key="idem_2")

    assert run_service.calls[0]["conversation_id"] == run_service.calls[1]["conversation_id"]


async def test_submitter_consumes_numeric_choice_when_waiting_run_exists() -> None:
    run_service = RecordingRunService()
    run_service.choice_run_id = RUN_ID
    submitter = RunServiceInboundSubmitter(run_service=run_service, tenant_id=TENANT_ID)

    run_id = await submitter.submit(_message("2"), idempotency_key="idem_choice")

    assert run_id == RUN_ID
    assert run_service.calls == []
    assert len(run_service.choice_calls) == 1
    assert run_service.choice_calls[0]["choice_key"] == "2"


async def test_submitter_consumes_common_numeric_choice_phrases() -> None:
    for text in ("选2", "选 2", "2.", "2、", "第2项"):
        run_service = RecordingRunService()
        run_service.choice_run_id = RUN_ID
        submitter = RunServiceInboundSubmitter(run_service=run_service, tenant_id=TENANT_ID)

        run_id = await submitter.submit(_message(text), idempotency_key=f"idem_choice_{text}")

        assert run_id == RUN_ID
        assert run_service.calls == []
        assert len(run_service.choice_calls) == 1
        assert run_service.choice_calls[0]["choice_key"] == "2"


async def test_submitter_keeps_numeric_text_as_message_without_waiting_run() -> None:
    run_service = RecordingRunService()
    submitter = RunServiceInboundSubmitter(run_service=run_service, tenant_id=TENANT_ID)

    await submitter.submit(_message("2"), idempotency_key="idem_plain")

    assert run_service.choice_calls[0]["choice_key"] == "2"
    assert run_service.calls[0]["message"] == "2"


async def test_submitter_extracts_leading_resource_hints_without_changing_message_text() -> None:
    run_service = RecordingRunService()
    submitter = RunServiceInboundSubmitter(run_service=run_service, tenant_id=TENANT_ID)

    await submitter.submit(
        _message("@github &deep-research &pdf #filesystem Review this repo"),
        idempotency_key="idem_1",
    )

    call = run_service.calls[0]
    assert call["mode"] is TaskMode.AUTO
    assert call["message"] == "@github &deep-research &pdf #filesystem Review this repo"
    context = call["channel_context"]
    assert isinstance(context, dict)
    assert context["channel_entry_policy"] == "main_agent_decides"
    assert context["requested_skills"] == "deep-research,pdf"
    assert context["requested_mcp_servers"] == "filesystem"
    assert context["requested_plugins"] == "github"


async def test_submitter_ignores_resource_symbols_after_task_text_begins() -> None:
    run_service = RecordingRunService()
    submitter = RunServiceInboundSubmitter(run_service=run_service, tenant_id=TENANT_ID)

    await submitter.submit(
        _message("请分析 @someone 的账号、#一级标题、C# 示例和 & 符号，不要当成资源调用"),
        idempotency_key="idem_1",
    )

    call = run_service.calls[0]
    assert call["mode"] is TaskMode.AUTO
    context = call["channel_context"]
    assert isinstance(context, dict)
    assert context["channel_entry_policy"] == "main_agent_decides"
    assert "requested_skills" not in context
    assert "requested_mcp_servers" not in context
    assert "requested_plugins" not in context


async def test_submitter_keeps_channel_language_directives_as_raw_text_for_main_agent() -> None:
    run_service = RecordingRunService()
    submitter = RunServiceInboundSubmitter(
        run_service=run_service,
        tenant_id=TENANT_ID,
        settings_service=StubSettingsService(vibe_coding_enabled=True),
    )

    await submitter.submit(
        _message("//hybrid //vi Refactor this module with context compression"),
        idempotency_key="idem_1",
    )

    call = run_service.calls[0]
    assert call["mode"] is TaskMode.AUTO
    assert call["vibe_coding"] is False
    assert call["message"] == "//hybrid //vi Refactor this module with context compression"
    context = call["channel_context"]
    assert isinstance(context, dict)
    assert context["channel_entry_policy"] == "main_agent_decides"
    assert "requested_channel_features" not in context


async def test_submitter_no_longer_rejects_disabled_vibe_channel_keyword() -> None:
    run_service = RecordingRunService()
    submitter = RunServiceInboundSubmitter(
        run_service=run_service,
        tenant_id=TENANT_ID,
        settings_service=StubSettingsService(vibe_coding_enabled=False),
    )

    await submitter.submit(_message("//讨论 //代码协作 评审这个实现方案"), idempotency_key="idem_1")

    call = run_service.calls[0]
    assert call["mode"] is TaskMode.AUTO
    assert call["vibe_coding"] is False
    assert call["message"] == "//讨论 //代码协作 评审这个实现方案"
    context = call["channel_context"]
    assert isinstance(context, dict)
    assert context["channel_entry_policy"] == "main_agent_decides"
