"""Persistent tenant-scoped conversation metadata.

Revision ID: 0026_conversation_metadata
Revises: 0025_run_conversation_index
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_conversation_metadata"
down_revision: str | Sequence[str] | None = "0025_run_conversation_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_hub_conversations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(),
            sa.ForeignKey("agent_hub_tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("project_label", sa.String(80), nullable=True),
        sa.Column("workspace_path", sa.String(64), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "conversation_id",
            name="uq_agent_hub_conversations_tenant_conversation",
        ),
    )
    op.create_index(
        "ix_agent_hub_conversations_tenant_archived_updated",
        "agent_hub_conversations",
        ["tenant_id", "archived_at", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_hub_conversations_tenant_archived_updated",
        table_name="agent_hub_conversations",
    )
    op.drop_table("agent_hub_conversations")
