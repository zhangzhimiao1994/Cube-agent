import pytest
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.db.models import TenantRow
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
