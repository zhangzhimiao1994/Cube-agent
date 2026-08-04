import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.db.models import TenantRow


@pytest.mark.integration
async def test_tenant_can_be_persisted_and_loaded_by_slug(db_session: AsyncSession) -> None:
    session = db_session
    tenant = TenantRow(slug="default", name="Default")
    session.add(tenant)
    await session.commit()

    stored_tenant = await session.scalar(select(TenantRow).where(TenantRow.slug == "default"))

    assert stored_tenant is not None
    assert stored_tenant.name == "Default"
