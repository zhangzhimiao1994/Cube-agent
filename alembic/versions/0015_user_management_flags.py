"""Add production user management flags.

Revision ID: 0015_user_management_flags
Revises: 0014_channel_admin_resources
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "0015_user_management_flags"
down_revision = "0014_channel_admin_resources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_hub_users",
        sa.Column("disabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "agent_hub_users",
        sa.Column("protected", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute("UPDATE agent_hub_users SET protected = true WHERE role = 'super_admin'")


def downgrade() -> None:
    op.drop_column("agent_hub_users", "protected")
    op.drop_column("agent_hub_users", "disabled")


__all__: Sequence[str] = ("downgrade", "upgrade")
