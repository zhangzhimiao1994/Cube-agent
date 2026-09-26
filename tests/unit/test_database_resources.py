import asyncio
import importlib.util
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from typing import cast

import pytest
from sqlalchemy import CheckConstraint, Table, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agent_hub.db.migrations import resolve_database_url
from agent_hub.db.models import AdminResourceRow, ConversationRow, RunRow
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
        "skill_source",
        "skill_source_revision",
        "mcp",
        "memory",
        "hermes",
        "audit",
        "log",
        "setting",
        "channel",
        "openclaw",
        "openclaw_session",
        "schedule",
        "evolution",
        "plugin",
        "plugin_signing_key",
        "capability_install",
    ):
        assert kind in sqltext


def test_latest_migration_allows_capability_install_admin_resources() -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0024_capability_install_admin_resources.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0024_capability_install_admin_resources", migration_path
    )
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "0024_capability_install"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0023_runtime_artifacts"
    assert "capability_install" in migration._NEXT_KINDS
    assert "capability_install" not in migration._CURRENT_KINDS


def test_skill_source_revision_migration_updates_and_restores_kind_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0030_skill_source_revisions.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0030_skill_source_revisions", migration_path
    )
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda *args, **kwargs: calls.append(("drop", *args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda *args, **kwargs: calls.append(("create", *args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda statement: calls.append(("execute", statement)),
    )

    migration.upgrade()
    upgrade_calls = list(calls)
    calls.clear()
    migration.downgrade()

    assert migration.revision == "0030_skill_source_revisions"
    assert migration.down_revision == "0029_project_workspaces"
    assert "skill_source_revision" in migration._NEXT_KINDS
    assert "skill_source_revision" not in migration._CURRENT_KINDS
    assert upgrade_calls[-1][0] == "create"
    assert "skill_source_revision" in upgrade_calls[-1][3]
    assert calls[1] == (
        "execute",
        (
            "DELETE FROM agent_hub_admin_resources "
            "WHERE kind = 'setting' "
            "AND resource_id LIKE 'skill-source-recovery-%'"
        ),
    )
    assert calls[2] == (
        "execute",
        (
            "UPDATE agent_hub_admin_resources "
            "SET payload = payload - 'active_revision_id' "
            "WHERE kind = 'skill_source' "
            "AND payload ? 'active_revision_id'"
        ),
    )
    assert calls[3] == (
        "execute",
        "DELETE FROM agent_hub_admin_resources WHERE kind = 'skill_source_revision'",
    )
    assert calls[-1][0] == "create"
    assert "skill_source_revision" not in calls[-1][3]


def test_run_conversation_index_covers_filter_and_chronological_order() -> None:
    table = cast(Table, RunRow.__table__)
    index = next(
        index
        for index in table.indexes
        if str(index.name) == "ix_agent_hub_runs_tenant_conversation_created"
    )

    assert [str(expression) for expression in index.expressions] == [
        "agent_hub_runs.tenant_id",
        "(routing_decision ->> 'conversation_id')",
        "agent_hub_runs.created_at",
        "agent_hub_runs.id",
    ]


def test_conversation_index_migration_uses_non_blocking_postgres_ddl() -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0025_run_conversation_index.py"
    )
    source = migration_path.read_text(encoding="utf-8")

    assert 'down_revision: str | Sequence[str] | None = "0024_capability_install"' in source
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in source
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in source
    assert "routing_decision ->> 'conversation_id'" in source


def test_conversation_metadata_model_is_unique_per_tenant_and_conversation() -> None:
    table = cast(Table, ConversationRow.__table__)

    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("tenant_id", "conversation_id") in unique_columns
    assert {column.name for column in table.columns} >= {
        "tenant_id",
        "conversation_id",
        "title",
        "project_id",
        "project_label",
        "workspace_path",
        "archived_at",
        "created_at",
        "updated_at",
    }


def test_conversation_metadata_migration_follows_current_head() -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0026_conversation_metadata.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0026_conversation_metadata", migration_path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "0026_conversation_metadata"
    assert migration.down_revision == "0025_run_conversation_index"


def test_skill_source_migration_follows_conversation_metadata() -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0027_skill_source_admin_resources.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0027_skill_sources", migration_path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "0027_skill_sources"
    assert migration.down_revision == "0026_conversation_metadata"
    assert "skill_source" in migration._NEXT_KINDS
    assert "skill_source" not in migration._CURRENT_KINDS
    migration_source = migration_path.read_text(encoding="utf-8")
    assert "payload - 'source' - 'archive_sha256'" in migration_source


def test_project_workspace_migration_records_legacy_workspace_conflicts() -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0029_project_workspaces.py"
    )
    source = migration_path.read_text(encoding="utf-8")

    assert 'down_revision: str | Sequence[str] | None = "0028_conversation_run_queue"' in source
    assert "COUNT(DISTINCT workspace_path) AS workspace_count" in source
    assert "legacy_workspace_count" in source
    assert "UPDATE agent_hub_conversations" not in source
