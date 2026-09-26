"""Tenant-scoped persistent conversation metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import ConversationRow
from agent_hub.runs.workspace import workspace_selection


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    id: UUID
    tenant_id: UUID
    conversation_id: str
    title: str
    project_id: str
    project_label: str | None
    workspace_path: str
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ConversationNotFound(RuntimeError):
    """The conversation is not visible in the requested tenant."""


class ConversationConflict(RuntimeError):
    """The tenant already has a conversation with this id."""


class ConversationArchived(RuntimeError):
    """An archived conversation cannot accept additional runs."""


def normalize_conversation_id(value: str | None) -> str:
    raw = (value or f"conv-{uuid4().hex}").strip()
    if len(raw) < 4 or len(raw) > 128:
        raise ValueError("conversation_id must contain between 4 and 128 characters")
    if any(character in raw for character in ("/", "\\")) or ".." in raw:
        raise ValueError("conversation_id must not contain path traversal characters")
    return raw


def normalize_conversation_title(value: str | None) -> str:
    title = (value or "新会话").strip()
    if not title:
        raise ValueError("conversation title must not be blank")
    if len(title) > 200:
        raise ValueError("conversation title must not exceed 200 characters")
    return title


def normalize_conversation_workspace(
    *,
    project_id: str,
    project_label: str | None,
    workspace_path: str,
) -> tuple[str, str, str]:
    selection = workspace_selection(
        project_id=project_id,
        project_label=project_label,
        session_id=workspace_path,
    )
    return selection.project_id, selection.project_label, selection.session_id


class ConversationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        tenant_id: UUID,
        conversation_id: str | None,
        title: str | None,
        project_id: str,
        project_label: str | None,
        workspace_path: str,
    ) -> ConversationRecord:
        normalized_conversation_id = normalize_conversation_id(conversation_id)
        normalized_title = normalize_conversation_title(title)
        normalized_project, normalized_label, normalized_workspace = (
            normalize_conversation_workspace(
                project_id=project_id,
                project_label=project_label,
                workspace_path=workspace_path,
            )
        )
        row = ConversationRow(
            id=uuid4(),
            tenant_id=tenant_id,
            conversation_id=normalized_conversation_id,
            title=normalized_title,
            project_id=normalized_project,
            project_label=normalized_label,
            workspace_path=normalized_workspace,
        )
        try:
            async with self._session_factory() as session, session.begin():
                session.add(row)
                await session.flush()
                await session.refresh(row)
        except IntegrityError as error:
            raise ConversationConflict(normalized_conversation_id) from error
        return _record(row)

    async def list(
        self,
        tenant_id: UUID,
        *,
        archived: bool = False,
    ) -> tuple[ConversationRecord, ...]:
        archived_clause = (
            ConversationRow.archived_at.is_not(None)
            if archived
            else ConversationRow.archived_at.is_(None)
        )
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ConversationRow)
                .where(ConversationRow.tenant_id == tenant_id)
                .where(archived_clause)
                .order_by(ConversationRow.updated_at.desc(), ConversationRow.id.desc())
            )
            return tuple(_record(row) for row in rows)

    async def find(
        self,
        tenant_id: UUID,
        conversation_id: str,
    ) -> ConversationRecord | None:
        try:
            normalized_conversation_id = normalize_conversation_id(conversation_id)
        except ValueError:
            return None
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ConversationRow).where(
                    ConversationRow.tenant_id == tenant_id,
                    ConversationRow.conversation_id == normalized_conversation_id,
                )
            )
        return None if row is None else _record(row)

    async def get(self, tenant_id: UUID, conversation_id: str) -> ConversationRecord:
        record = await self.find(tenant_id, conversation_id)
        if record is None:
            raise ConversationNotFound(conversation_id)
        return record

    async def update(
        self,
        *,
        tenant_id: UUID,
        conversation_id: str,
        title: str | None = None,
        project_id: str | None = None,
        project_label: str | None = None,
        workspace_path: str | None = None,
        archived: bool | None = None,
    ) -> ConversationRecord:
        normalized_conversation_id = normalize_conversation_id(conversation_id)
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(ConversationRow)
                .where(
                    ConversationRow.tenant_id == tenant_id,
                    ConversationRow.conversation_id == normalized_conversation_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ConversationNotFound(normalized_conversation_id)
            if title is not None:
                row.title = normalize_conversation_title(title)
            if project_id is not None or project_label is not None or workspace_path is not None:
                normalized_project, normalized_label, normalized_workspace = (
                    normalize_conversation_workspace(
                        project_id=project_id or row.project_id,
                        project_label=project_label if project_label is not None else row.project_label,
                        workspace_path=workspace_path or row.workspace_path,
                    )
                )
                row.project_id = normalized_project
                row.project_label = normalized_label
                row.workspace_path = normalized_workspace
            if archived is not None:
                row.archived_at = datetime.now(UTC) if archived else None
            row.updated_at = datetime.now(UTC)
            await session.flush()
            await session.refresh(row)
            return _record(row)


def _record(row: ConversationRow) -> ConversationRecord:
    return ConversationRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        conversation_id=row.conversation_id,
        title=row.title,
        project_id=row.project_id,
        project_label=row.project_label,
        workspace_path=row.workspace_path,
        archived_at=row.archived_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
