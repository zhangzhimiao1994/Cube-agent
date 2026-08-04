from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.db.models import ConfigRevisionRow


class ConfigStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


class ConfigRepositoryError(RuntimeError):
    pass


class ConfigNotFoundError(ConfigRepositoryError):
    pass


class ConfigStatusError(ConfigRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class ConfigRevision:
    id: UUID
    tenant_id: UUID
    version: int
    status: ConfigStatus
    document: dict[str, object]
    created_by: UUID | None
    created_at: datetime


def _advisory_lock_key(tenant_id: UUID) -> int:
    upper = tenant_id.int >> 64
    lower = tenant_id.int & ((1 << 64) - 1)
    unsigned = upper ^ lower
    return unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned


def _revision(row: ConfigRevisionRow) -> ConfigRevision:
    return ConfigRevision(
        id=row.id,
        tenant_id=row.tenant_id,
        version=row.version,
        status=ConfigStatus(row.status),
        document=deepcopy(row.document),
        created_by=row.created_by,
        created_at=row.created_at,
    )


class ConfigRepository:
    async def lock_tenant(self, session: AsyncSession, tenant_id: UUID) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _advisory_lock_key(tenant_id)},
        )

    async def next_version(self, session: AsyncSession, tenant_id: UUID) -> int:
        latest = await session.scalar(
            select(func.max(ConfigRevisionRow.version)).where(
                ConfigRevisionRow.tenant_id == tenant_id
            )
        )
        return (latest or 0) + 1

    async def create(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        version: int,
        status: ConfigStatus,
        document: dict[str, object],
        actor_id: UUID,
    ) -> ConfigRevision:
        row = ConfigRevisionRow(
            tenant_id=tenant_id,
            version=version,
            status=status.value,
            document=deepcopy(document),
            created_by=actor_id,
        )
        session.add(row)
        await session.flush()
        return _revision(row)

    async def get_version(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        version: int,
        *,
        for_update: bool = False,
    ) -> ConfigRevisionRow | None:
        statement = select(ConfigRevisionRow).where(
            ConfigRevisionRow.tenant_id == tenant_id,
            ConfigRevisionRow.version == version,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(ConfigRevisionRow | None, await session.scalar(statement))

    async def get_current_row(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        for_update: bool = False,
    ) -> ConfigRevisionRow | None:
        statement = select(ConfigRevisionRow).where(
            ConfigRevisionRow.tenant_id == tenant_id,
            ConfigRevisionRow.status == ConfigStatus.PUBLISHED.value,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(ConfigRevisionRow | None, await session.scalar(statement))

    async def get_current(
        self, session: AsyncSession, tenant_id: UUID
    ) -> ConfigRevision | None:
        row = await self.get_current_row(session, tenant_id)
        return None if row is None else _revision(row)

    async def read_version(
        self, session: AsyncSession, tenant_id: UUID, version: int
    ) -> ConfigRevision | None:
        row = await self.get_version(session, tenant_id, version)
        return None if row is None else _revision(row)

    async def list_versions(
        self, session: AsyncSession, tenant_id: UUID
    ) -> list[ConfigRevision]:
        rows = await session.scalars(
            select(ConfigRevisionRow)
            .where(ConfigRevisionRow.tenant_id == tenant_id)
            .order_by(ConfigRevisionRow.version)
        )
        return [_revision(row) for row in rows]

    def materialize(self, row: ConfigRevisionRow) -> ConfigRevision:
        return _revision(row)

    def set_status(
        self,
        row: ConfigRevisionRow,
        status: Literal[ConfigStatus.PUBLISHED, ConfigStatus.SUPERSEDED],
    ) -> None:
        row.status = status.value
