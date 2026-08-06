"""Add channel inbound deduplication.

Revision ID: 0008_channel_dedup
Revises: 0007_runs
Create Date: 2026-08-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_channel_dedup"
down_revision: str | Sequence[str] | None = "0007_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_hub_channel_dedup",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("tenant_external_id", sa.String(length=256), nullable=False),
        sa.Column("event_id", sa.String(length=256), nullable=False),
        sa.Column("message_id", sa.String(length=256), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('reserved', 'submitted', 'completed')",
            name="ck_agent_hub_channel_dedup_status",
        ),
        sa.UniqueConstraint(
            "channel",
            "tenant_external_id",
            "event_id",
            name="uq_agent_hub_channel_dedup_event",
        ),
        sa.UniqueConstraint(
            "channel",
            "tenant_external_id",
            "message_id",
            name="uq_agent_hub_channel_dedup_message",
        ),
    )
    op.create_index(
        "ix_agent_hub_channel_dedup_cleanup",
        "agent_hub_channel_dedup",
        ["status", "completed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_hub_channel_dedup_cleanup", table_name="agent_hub_channel_dedup")
    op.drop_table("agent_hub_channel_dedup")
