import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import BootstrapCodeRow, ConfigRevisionRow, SecretRow, TenantRow, UserRow
from agent_hub.db.session import Database, build_database
from alembic import command

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DATABASE_URL = "postgresql+asyncpg://agent_hub_test:agent_hub_test@127.0.0.1:{port}/agent_hub_test"


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get(
        "AGENT_HUB_TEST_DATABASE_URL",
        COMPOSE_DATABASE_URL.format(port=os.environ.get("AGENT_HUB_TEST_POSTGRES_PORT", "54329")),
    )


@pytest.fixture(scope="session")
def redis_url() -> str:
    return os.environ.get(
        "AGENT_HUB_TEST_REDIS_URL",
        f"redis://127.0.0.1:{os.environ.get('AGENT_HUB_TEST_REDIS_PORT', '56379')}/15",
    )


@pytest.fixture
def alembic_config(database_url: str) -> Config:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture(scope="session", autouse=True)
def migrated_database(database_url: str) -> Iterator[None]:
    asyncio.run(_wait_for_database(database_url))
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    yield


async def _wait_for_database(database_url: str) -> None:
    database = build_database(database_url)
    try:
        await database.wait_until_ready()
    finally:
        await database.dispose()


@pytest.fixture
async def db_session(database_url: str) -> AsyncIterator[AsyncSession]:
    database: Database = build_database(database_url)
    try:
        async with database.session_factory() as session:
            await session.rollback()
            await session.execute(delete(BootstrapCodeRow))
            await session.execute(delete(SecretRow))
            await session.execute(delete(ConfigRevisionRow))
            await session.execute(delete(UserRow))
            await session.execute(delete(TenantRow))
            await session.commit()
            try:
                yield session
            finally:
                await session.rollback()
                await session.execute(delete(BootstrapCodeRow))
                await session.execute(delete(SecretRow))
                await session.execute(delete(ConfigRevisionRow))
                await session.execute(delete(UserRow))
                await session.execute(delete(TenantRow))
                await session.commit()
    finally:
        await database.dispose()


@pytest.fixture
async def secret_session_factory(
    database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database = build_database(database_url)
    try:
        await _clean_database(database.session_factory)
        yield database.session_factory
    finally:
        await _clean_database(database.session_factory)
        await database.dispose()


@pytest.fixture
async def auth_session_factory(
    database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database = build_database(database_url)
    try:
        await _clean_database(database.session_factory)
        yield database.session_factory
    finally:
        await _clean_database(database.session_factory)
        await database.dispose()


async def _clean_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(delete(BootstrapCodeRow))
        await session.execute(delete(SecretRow))
        await session.execute(delete(ConfigRevisionRow))
        await session.execute(delete(UserRow))
        await session.execute(delete(TenantRow))
