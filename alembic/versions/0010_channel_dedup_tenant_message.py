"""Tenant-scope channel message deduplication.

Revision ID: 0010_channel_tenant_msg
Revises: 0009_fix_channel_dedup
Create Date: 2026-08-07 00:10:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_channel_tenant_msg"
down_revision: str | Sequence[str] | None = "0009_fix_channel_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_hub_channel_dedup "
        "DROP CONSTRAINT IF EXISTS uq_agent_hub_channel_dedup_message"
    )
    op.create_unique_constraint(
        "uq_agent_hub_channel_dedup_message",
        "agent_hub_channel_dedup",
        ["channel", "tenant_external_id", "message_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_agent_hub_channel_dedup_message",
        "agent_hub_channel_dedup",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_agent_hub_channel_dedup_message",
        "agent_hub_channel_dedup",
        ["channel", "tenant_external_id", "message_id"],
    )
