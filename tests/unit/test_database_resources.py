from sqlalchemy.ext.asyncio import AsyncEngine

from agent_hub.db.migrations import resolve_database_url
from agent_hub.db.session import build_database
from agent_hub.settings import Settings


async def test_database_exposes_and_disposes_its_owned_engine() -> None:
    database = build_database("postgresql+asyncpg://user:password@localhost:5432/database")

    assert isinstance(database.engine, AsyncEngine)
    assert database.session_factory.kw["bind"] is database.engine

    await database.dispose()


def test_configured_migration_url_overrides_application_settings() -> None:
    settings = Settings(database_url="postgresql+asyncpg://app:app@localhost/application")

    assert (
        resolve_database_url("postgresql+asyncpg://test:test@localhost/test", settings)
        == "postgresql+asyncpg://test:test@localhost/test"
    )


def test_migration_url_uses_application_settings_when_not_explicitly_configured() -> None:
    settings = Settings(database_url="postgresql+asyncpg://app:app@localhost/application")

    assert resolve_database_url(None, settings) == "postgresql+asyncpg://app:app@localhost/application"
