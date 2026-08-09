"""Add persistent admin resources.

Revision ID: 0011_admin_resources
Revises: 0010_channel_tenant_msg
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_admin_resources"
down_revision = "0010_channel_tenant_msg"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_hub_admin_resources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "kind IN ('workflow', 'skill', 'mcp', 'memory', 'hermes', 'audit')",
            name="ck_agent_hub_admin_resources_kind",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "kind",
            "resource_id",
            name="uq_agent_hub_admin_resources_tenant_kind_resource",
        ),
    )
    op.create_index(
        "ix_agent_hub_admin_resources_tenant_kind",
        "agent_hub_admin_resources",
        ["tenant_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_hub_admin_resources_tenant_kind",
        table_name="agent_hub_admin_resources",
    )
    op.drop_table("agent_hub_admin_resources")


__all__: Sequence[str] = ("downgrade", "upgrade")
