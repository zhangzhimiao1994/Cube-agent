"""Persist ordered messages queued behind active conversation runs.

Revision ID: 0028_conversation_run_queue
Revises: 0027_skill_sources
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0028_conversation_run_queue"
down_revision: str | Sequence[str] | None = "0027_skill_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_hub_runs",
        sa.Column("blocked_by_run_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_hub_runs_blocked_by_run",
        "agent_hub_runs",
        "agent_hub_runs",
        ["blocked_by_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_agent_hub_runs_blocked_by_run_id",
        "agent_hub_runs",
        ["blocked_by_run_id"],
    )
    op.create_table(
        "agent_hub_conversation_queue_items",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column(
            "predecessor_run_id",
            sa.UUID(),
            sa.ForeignKey("agent_hub_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "successor_run_id",
            sa.UUID(),
            sa.ForeignKey("agent_hub_runs.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "attachments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "references",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("failure_detail", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('queued', 'redirecting', 'released', 'cancelled', "
            "'running', 'completed', 'failed')",
            name="ck_agent_hub_conversation_queue_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_agent_hub_conversation_queue_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_agent_hub_conversation_queue_order",
        "agent_hub_conversation_queue_items",
        ["tenant_id", "conversation_id", "position"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_hub_conversation_queue_order",
        table_name="agent_hub_conversation_queue_items",
    )
    op.drop_table("agent_hub_conversation_queue_items")
    op.drop_index("ix_agent_hub_runs_blocked_by_run_id", table_name="agent_hub_runs")
    op.drop_constraint(
        "fk_agent_hub_runs_blocked_by_run",
        "agent_hub_runs",
        type_="foreignkey",
    )
    op.drop_column("agent_hub_runs", "blocked_by_run_id")
