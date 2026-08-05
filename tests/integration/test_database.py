import asyncio
from io import StringIO
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.db.models import ConfigRevisionRow, TenantRow
from agent_hub.db.session import build_database
from alembic import command


@pytest.mark.integration
async def test_tenant_can_be_persisted_and_loaded_by_slug(db_session: AsyncSession) -> None:
    session = db_session
    tenant = TenantRow(slug="default", name="Default")
    session.add(tenant)
    await session.commit()

    stored_tenant = await session.scalar(select(TenantRow).where(TenantRow.slug == "default"))

    assert stored_tenant is not None
    assert stored_tenant.name == "Default"


@pytest.mark.integration
async def test_database_readiness_confirms_postgres(database_url: str) -> None:
    database = build_database(database_url)
    try:
        await database.wait_until_ready(timeout_seconds=5)
    finally:
        await database.dispose()


@pytest.mark.integration
def test_migrations_downgrade_to_base_and_upgrade_to_head(alembic_config: Config) -> None:
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")


@pytest.mark.integration
def test_encrypted_secrets_migration_downgrades_and_reupgrades(
    alembic_config: Config, database_url: str
) -> None:
    command.downgrade(alembic_config, "0003_one_published")
    assert asyncio.run(_table_exists(database_url, "agent_hub_secrets")) is False

    command.upgrade(alembic_config, "0004_encrypted_secrets")
    assert asyncio.run(_table_exists(database_url, "agent_hub_secrets")) is True


@pytest.mark.integration
def test_encrypted_secrets_migration_generates_offline_sql(alembic_config: Config) -> None:
    output = StringIO()
    alembic_config.output_buffer = output

    command.upgrade(
        alembic_config,
        "0003_one_published:0004_encrypted_secrets",
        sql=True,
    )

    generated = output.getvalue()
    assert "CREATE TABLE agent_hub_secrets" in generated
    assert "uq_agent_hub_secrets_tenant_fingerprint" in generated


@pytest.mark.integration
def test_published_unique_index_migration_rejects_corrupt_history(
    alembic_config: Config, database_url: str
) -> None:
    command.downgrade(alembic_config, "0002_config_status_check")
    tenant_id = asyncio.run(_seed_duplicate_published(database_url))
    try:
        with pytest.raises(RuntimeError, match="multiple published revisions"):
            command.upgrade(alembic_config, "head")
    finally:
        asyncio.run(_delete_tenant(database_url, tenant_id))
        command.upgrade(alembic_config, "head")


async def _seed_duplicate_published(database_url: str) -> UUID:
    database = build_database(database_url)
    tenant_id = uuid4()
    try:
        async with database.session_factory() as session:
            session.add(TenantRow(id=tenant_id, slug=f"corrupt-{tenant_id}", name="Corrupt"))
            await session.commit()
            session.add_all(
                [
                    ConfigRevisionRow(
                        tenant_id=tenant_id,
                        version=version,
                        status="published",
                        document={"models": {}, "agents": []},
                        created_by=uuid4(),
                    )
                    for version in (1, 2)
                ]
            )
            await session.commit()
    finally:
        await database.dispose()
    return tenant_id


async def _delete_tenant(database_url: str, tenant_id: UUID) -> None:
    database = build_database(database_url)
    try:
        async with database.session_factory() as session:
            await session.execute(
                delete(ConfigRevisionRow).where(ConfigRevisionRow.tenant_id == tenant_id)
            )
            await session.execute(delete(TenantRow).where(TenantRow.id == tenant_id))
            await session.commit()
    finally:
        await database.dispose()


async def _table_exists(database_url: str, table_name: str) -> bool:
    database = build_database(database_url)
    try:
        async with database.session_factory() as session:
            relation = await session.scalar(
                text("SELECT to_regclass(:table_name)"),
                {"table_name": table_name},
            )
            return relation is not None
    finally:
        await database.dispose()
