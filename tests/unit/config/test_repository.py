from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.config.repository import ConfigRepository
from agent_hub.db.models import ConfigRevisionRow


class MultipleRowsResultStub:
    def scalars(self) -> "MultipleRowsResultStub":
        return self

    def one_or_none(self) -> ConfigRevisionRow | None:
        raise MultipleResultsFound("multiple published revisions")


class CorruptCurrentSessionStub:
    async def scalar(self, statement: object) -> ConfigRevisionRow:
        return ConfigRevisionRow(
            id=uuid4(),
            tenant_id=uuid4(),
            version=1,
            status="published",
            document={"models": {}, "agents": []},
        )

    async def execute(self, statement: object) -> MultipleRowsResultStub:
        return MultipleRowsResultStub()


async def test_get_current_row_fails_loudly_for_multiple_published_rows() -> None:
    session = cast(AsyncSession, CorruptCurrentSessionStub())

    with pytest.raises(MultipleResultsFound):
        await ConfigRepository().get_current_row(session, uuid4())
