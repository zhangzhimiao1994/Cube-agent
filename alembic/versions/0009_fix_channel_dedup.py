"""Repair channel dedup schema compatibility.

Revision ID: 0009_fix_channel_dedup
Revises: 0008_channel_dedup
Create Date: 2026-08-07 00:05:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_fix_channel_dedup"
down_revision: str | Sequence[str] | None = "0008_channel_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_hub_channel_dedup "
        "ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "ALTER TABLE agent_hub_channel_dedup "
        "DROP CONSTRAINT IF EXISTS ck_agent_hub_channel_dedup_status"
    )
    op.execute(
        "ALTER TABLE agent_hub_channel_dedup "
        "ADD CONSTRAINT ck_agent_hub_channel_dedup_status "
        "CHECK (status IN ('reserved', 'submitted', 'completed'))"
    )
    op.execute("DROP INDEX IF EXISTS ix_agent_hub_channel_dedup_cleanup")
    op.execute(
        "CREATE INDEX ix_agent_hub_channel_dedup_cleanup "
        "ON agent_hub_channel_dedup (status, completed_at)"
    )
    op.execute(
        "ALTER TABLE agent_hub_channel_dedup "
        "DROP CONSTRAINT IF EXISTS uq_agent_hub_channel_dedup_message"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'uq_agent_hub_channel_dedup_message'
                  AND conrelid = 'agent_hub_channel_dedup'::regclass
            ) THEN
                ALTER TABLE agent_hub_channel_dedup
                ADD CONSTRAINT uq_agent_hub_channel_dedup_message
                UNIQUE (channel, tenant_external_id, message_id);
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_hub_channel_dedup_cleanup")
    op.execute(
        "CREATE INDEX ix_agent_hub_channel_dedup_cleanup "
        "ON agent_hub_channel_dedup (status, completed_at)"
    )
