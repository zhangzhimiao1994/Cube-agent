"""Allow at most one published configuration revision per tenant.

Revision ID: 0003_one_published
Revises: 0002_config_status_check
Create Date: 2026-08-05 00:00:02.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_one_published"
down_revision: str | Sequence[str] | None = "0002_config_status_check"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_agent_hub_config_revisions_one_published_per_tenant"


def upgrade() -> None:
    connection = op.get_bind()
    duplicate_tenants = connection.execute(
        sa.text(
            """
            SELECT tenant_id
            FROM agent_hub_config_revisions
            WHERE status = 'published'
            GROUP BY tenant_id
            HAVING count(*) > 1
            ORDER BY tenant_id
            """
        )
    ).scalars().all()
    if duplicate_tenants:
        tenants = ", ".join(str(tenant_id) for tenant_id in duplicate_tenants)
        raise RuntimeError(
            "cannot enforce one current configuration: multiple published revisions "
            f"exist for tenant(s): {tenants}"
        )

    op.create_index(
        INDEX_NAME,
        "agent_hub_config_revisions",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="agent_hub_config_revisions")
