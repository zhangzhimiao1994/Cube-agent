from types import TracebackType
from typing import Self, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.config.repository import ConfigRepository, ConfigStatusError
from agent_hub.config.service import ConfigService
from agent_hub.db.models import ConfigRevisionRow


class TransactionStub:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class SessionStub:
    def begin(self) -> TransactionStub:
        return TransactionStub()


class ArchivedSourceRepository(ConfigRepository):
    async def lock_tenant(self, session: AsyncSession, tenant_id: UUID) -> None:
        return None

    async def get_version(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        version: int,
        *,
        for_update: bool = False,
    ) -> ConfigRevisionRow | None:
        return ConfigRevisionRow(
            id=uuid4(),
            tenant_id=tenant_id,
            version=version,
            status="archived",
            document={"models": {}, "agents": []},
            created_by=uuid4(),
        )

    async def get_current_row(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        for_update: bool = False,
    ) -> ConfigRevisionRow | None:
        raise AssertionError("rollback must reject an unsupported source status first")


async def test_rollback_rejects_source_outside_published_statuses() -> None:
    service = ConfigService(ArchivedSourceRepository())
    session = cast(AsyncSession, SessionStub())

    with pytest.raises(ConfigStatusError, match="published or superseded"):
        await service.rollback(session, uuid4(), 1, uuid4())
