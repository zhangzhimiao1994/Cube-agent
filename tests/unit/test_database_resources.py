import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic
from typing import cast

import pytest
from sqlalchemy import CheckConstraint, Table
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agent_hub.db.migrations import resolve_database_url
from agent_hub.db.models import AdminResourceRow
from agent_hub.db.session import Database, build_database, build_session_factory
from agent_hub.settings import Settings


async def test_database_exposes_and_disposes_its_owned_engine() -> None:
    database = build_database("postgresql+asyncpg://user:password@localhost:5432/database")

    assert isinstance(database.engine, AsyncEngine)
    assert database.session_factory.kw["bind"] is database.engine

    await database.dispose()


async def test_runtime_engine_hides_sql_parameter_values() -> None:
    database = build_database("postgresql+asyncpg://user:password@localhost:5432/database")
    try:
        assert database.engine.sync_engine.hide_parameters is True
    finally:
        await database.dispose()


async def test_session_factory_uses_caller_owned_engine() -> None:
    database = build_database("postgresql+asyncpg://user:password@localhost:5432/database")
    try:
        session_factory = build_session_factory(database.engine)

        assert session_factory.kw["bind"] is database.engine
    finally:
        await database.dispose()


class HangingEngine:
    @asynccontextmanager
    async def connect(self) -> AsyncIterator[None]:
        await asyncio.Event().wait()
        yield None


async def test_readiness_timeout_bounds_a_hanging_connection_attempt() -> None:
    database = Database(
        engine=cast(AsyncEngine, HangingEngine()),
        session_factory=cast(async_sessionmaker[AsyncSession], None),
    )
    started_at = monotonic()

    with pytest.raises(TimeoutError, match="Database did not become ready"):
        async with asyncio.timeout(0.2):
            await database.wait_until_ready(timeout_seconds=0.05, retry_interval_seconds=0.01)

    assert monotonic() - started_at < 0.15


def test_configured_migration_url_overrides_application_settings() -> None:
    settings = Settings.model_validate(
        {"database_url": "postgresql+asyncpg://app:app@localhost/application"}
    )

    assert (
        resolve_database_url("postgresql+asyncpg://test:test@localhost/test", settings)
        == "postgresql+asyncpg://test:test@localhost/test"
    )


def test_migration_url_uses_application_settings_when_not_explicitly_configured() -> None:
    settings = Settings.model_validate(
        {"database_url": "postgresql+asyncpg://app:app@localhost/application"}
    )

    assert resolve_database_url(None, settings) == "postgresql+asyncpg://app:app@localhost/application"


def test_admin_resource_kind_constraint_allows_all_persistent_admin_resources() -> None:
    table = cast(Table, AdminResourceRow.__table__)
    constraints = [
        constraint
        for constraint in table.constraints
        if constraint.name == "ck_agent_hub_admin_resources_kind"
    ]

    assert constraints
    constraint = cast(CheckConstraint, constraints[0])
    sqltext = str(constraint.sqltext)
    for kind in (
        "workflow",
        "agent",
        "main_agent",
        "skill",
        "mcp",
        "memory",
        "hermes",
        "audit",
        "log",
        "setting",
        "channel",
    ):
        assert kind in sqltext
