"""Tenant-scoped project workspace registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import ProjectWorkspaceRow
from agent_hub.runs.workspace import workspace_selection


@dataclass(frozen=True, slots=True)
class ProjectWorkspaceRecord:
    id: UUID
    tenant_id: UUID
    project_id: str
    label: str
    workspace_path: str
    legacy_workspace_count: int
    created_at: datetime
    updated_at: datetime


class ProjectWorkspaceNotFound(RuntimeError):
    """The project workspace does not exist in the requested tenant."""


class ProjectWorkspaceConflict(RuntimeError):
    """The tenant already has a project workspace with this id."""


def normalize_project_workspace(
    *, project_id: str, label: str | None, workspace_path: str
) -> tuple[str, str, str]:
    selection = workspace_selection(
        project_id=project_id,
        project_label=label,
        session_id=workspace_path,
    )
    return selection.project_id, selection.project_label, selection.session_id


class ProjectWorkspaceRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        tenant_id: UUID,
        project_id: str,
        label: str | None,
        workspace_path: str,
    ) -> ProjectWorkspaceRecord:
        normalized_id, normalized_label, normalized_path = normalize_project_workspace(
            project_id=project_id,
            label=label,
            workspace_path=workspace_path,
        )
        row = ProjectWorkspaceRow(
            id=uuid4(),
            tenant_id=tenant_id,
            project_id=normalized_id,
            label=normalized_label,
            workspace_path=normalized_path,
        )
        try:
            async with self._session_factory() as session, session.begin():
                session.add(row)
                await session.flush()
                await session.refresh(row)
        except IntegrityError as error:
            diagnostic = getattr(error.orig, "diag", None)
            constraint_name = getattr(error.orig, "constraint_name", None) or getattr(
                diagnostic, "constraint_name", None
            )
            if constraint_name == (
                "uq_agent_hub_project_workspaces_tenant_project"
            ):
                raise ProjectWorkspaceConflict(normalized_id) from error
            raise
        return _record(row)

    async def list(self, tenant_id: UUID) -> tuple[ProjectWorkspaceRecord, ...]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ProjectWorkspaceRow)
                .where(ProjectWorkspaceRow.tenant_id == tenant_id)
                .order_by(ProjectWorkspaceRow.updated_at.desc(), ProjectWorkspaceRow.id.desc())
            )
            return tuple(_record(row) for row in rows)

    async def find(self, tenant_id: UUID, project_id: str) -> ProjectWorkspaceRecord | None:
        normalized_id, _, _ = normalize_project_workspace(
            project_id=project_id,
            label=None,
            workspace_path="workspace",
        )
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ProjectWorkspaceRow).where(
                    ProjectWorkspaceRow.tenant_id == tenant_id,
                    ProjectWorkspaceRow.project_id == normalized_id,
                )
            )
        return None if row is None else _record(row)

    async def get(self, tenant_id: UUID, project_id: str) -> ProjectWorkspaceRecord:
        record = await self.find(tenant_id, project_id)
        if record is None:
            raise ProjectWorkspaceNotFound(project_id)
        return record


def _record(row: ProjectWorkspaceRow) -> ProjectWorkspaceRecord:
    return ProjectWorkspaceRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        label=row.label,
        workspace_path=row.workspace_path,
        legacy_workspace_count=row.legacy_workspace_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
