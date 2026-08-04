import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic.config import Config
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.db.models import ConfigRevisionRow, TenantRow, UserRow
from agent_hub.db.session import build_session_factory
from alembic import command

COMPOSE_DATABASE_URL = (
    "postgresql+asyncpg://agent_hub_test:agent_hub_test@127.0.0.1:54329/agent_hub_test"
)


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("AGENT_HUB_TEST_DATABASE_URL", COMPOSE_DATABASE_URL)


@pytest.fixture(scope="session", autouse=True)
def migrated_database(database_url: str) -> Iterator[None]:
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    yield


@pytest.fixture
async def db_session(database_url: str) -> AsyncIterator[AsyncSession]:
    session_factory = build_session_factory(database_url)
    async with session_factory() as session:
        await session.execute(delete(ConfigRevisionRow))
        await session.execute(delete(UserRow))
        await session.execute(delete(TenantRow))
        await session.commit()
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(delete(ConfigRevisionRow))
            await session.execute(delete(UserRow))
            await session.execute(delete(TenantRow))
            await session.commit()
