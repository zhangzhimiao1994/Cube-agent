from __future__ import annotations

import shlex
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agent_hub.api.errors import PublicAPIError
from agent_hub.channels.base import AttachmentKind, InboundAttachment, InboundMessage
from agent_hub.channels.feishu.settings import FeishuSettings
from agent_hub.skills.package import InvalidSkillPackage

_DEFAULT_MAX_SKILL_DOWNLOAD_BYTES = 20_000_000


class FeishuSkillMediaClient(Protocol):
    async def tenant_access_token(self, tenant_external_id: str) -> str: ...

    def download_resource(
        self,
        *,
        message_id: str,
        resource_key: str,
        tenant_access_token: str,
        resource_type: str = "image",
    ) -> AsyncIterator[bytes]: ...


class FeishuSkillAdminService(Protocol):
    async def upload_skill_archive(
        self,
        filename: str,
        archive_bytes: bytes,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class FeishuSkillCommandResult:
    handled: bool
    reply_text: str


class FeishuSkillCommandHandler:
    def __init__(
        self,
        *,
        admin_service: FeishuSkillAdminService,
        media_client_factory: Callable[[FeishuSettings], FeishuSkillMediaClient],
        max_download_bytes: int = _DEFAULT_MAX_SKILL_DOWNLOAD_BYTES,
    ) -> None:
        if max_download_bytes <= 0:
            raise ValueError("max_download_bytes must be positive")
        self._admin_service = admin_service
        self._media_client_factory = media_client_factory
        self._max_download_bytes = max_download_bytes

    async def handle(
        self,
        message: InboundMessage,
        *,
        settings: FeishuSettings,
    ) -> FeishuSkillCommandResult | None:
        command = _skill_command(message.text)
        if command is None:
            return None
        if command in {"approve", "disable"}:
            return FeishuSkillCommandResult(
                handled=True,
                reply_text="Skill 已进入受保护流程，请在管理端审批或禁用，飞书消息不会直接授予执行权限。",
            )
        if command not in {"install", "upload"}:
            return None
        attachment = _first_file_attachment(message.attachments)
        if attachment is None:
            return FeishuSkillCommandResult(
                handled=True,
                reply_text="请附加一个 .zip、.tar、.tar.gz 或 .tgz Skill 压缩包后再发送 /skill install。",
            )
        try:
            archive_bytes = await self._download_file(settings, message, attachment)
            uploaded = await self._admin_service.upload_skill_archive(
                attachment.filename or attachment.external_key,
                archive_bytes,
            )
        except InvalidSkillPackage as error:
            return FeishuSkillCommandResult(
                handled=True,
                reply_text=f"Skill 压缩包无效：{error}",
            )
        except PublicAPIError as error:
            if error.status_code == 409 and error.code == "skill_version_choice_required":
                skill_name = error.details.get("skill_name") if error.details else None
                target = f"「{skill_name}」" if skill_name else "同名 Skill"
                return FeishuSkillCommandResult(
                    handled=True,
                    reply_text=(
                        f"检测到 {target} 已存在且内容不同。请在 Web 管理端选择"
                        "“覆盖当前版本”或“保存为新版本”。"
                    ),
                )
            raise
        items = _skill_upload_items(uploaded)
        if not items:
            return FeishuSkillCommandResult(
                handled=True,
                reply_text="Skill 压缩包已读取，但没有发现可安装的 Skill。",
            )
        names = ", ".join(str(item.name) for item in items)
        ids = ", ".join(str(item.id) for item in items)
        return FeishuSkillCommandResult(
            handled=True,
            reply_text=f"Skill 已扫描入库，待审批：{names}。ID：{ids}",
        )

    async def _download_file(
        self,
        settings: FeishuSettings,
        message: InboundMessage,
        attachment: InboundAttachment,
    ) -> bytes:
        client = self._media_client_factory(settings)
        token = await client.tenant_access_token(message.tenant_external_id)
        chunks: list[bytes] = []
        total = 0
        async for chunk in client.download_resource(
            message_id=message.message_id,
            resource_key=attachment.external_key,
            tenant_access_token=token,
            resource_type="file",
        ):
            total += len(chunk)
            if total > self._max_download_bytes:
                raise InvalidSkillPackage("skill archive exceeds size limit")
            chunks.append(bytes(chunk))
        return b"".join(chunks)


def _skill_command(text: str) -> str | None:
    try:
        tokens = shlex.split(text, comments=False, posix=True)
    except ValueError:
        return None
    if len(tokens) < 2 or tokens[0].lower() != "/skill":
        return None
    command = tokens[1].lower()
    if command in {"install", "upload", "approve", "disable"}:
        return command
    return None


def _skill_upload_items(uploaded: object) -> Sequence[Any]:
    items = getattr(uploaded, "items", ())
    if isinstance(items, Sequence):
        return items
    return ()


def _first_file_attachment(
    attachments: tuple[InboundAttachment, ...],
) -> InboundAttachment | None:
    return next(
        (attachment for attachment in attachments if attachment.kind is AttachmentKind.FILE),
        None,
    )


__all__ = [
    "FeishuSkillCommandHandler",
    "FeishuSkillCommandResult",
]
