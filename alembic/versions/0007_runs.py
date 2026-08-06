"""Persist durable run execution state.

Revision ID: 0007_runs
Revises: 0006_username_check
Create Date: 2026-08-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_runs"
down_revision: str | Sequence[str] | None = "0006_username_check"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RUN_STATUSES = (
    "'queued', 'planning', 'waiting_user_mode', 'running', "
    "'waiting_approval', 'retrying', 'paused', 'synthesizing', "
    "'completed', 'failed', 'cancelled'"
)
RUN_MODES = "'direct', 'dispatch', 'discuss', 'hybrid'"


def upgrade() -> None:
    op.create_table(
        "agent_hub_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("routing_decision", postgresql.JSONB(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(f"status IN ({RUN_STATUSES})", name="ck_agent_hub_runs_status"),
        sa.CheckConstraint(f"mode IS NULL OR mode IN ({RUN_MODES})", name="ck_agent_hub_runs_mode"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_agent_hub_runs_tenant_idempotency_key",
        ),
    )
    op.create_index(
        "ix_agent_hub_runs_tenant_status",
        "agent_hub_runs",
        ["tenant_id", "status"],
    )

    op.create_table(
        "agent_hub_run_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", sa.String(length=128), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_hub_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "step_id", name="uq_agent_hub_run_steps_run_step"),
    )
    op.create_index(
        "ix_agent_hub_run_steps_tenant_status",
        "agent_hub_run_steps",
        ["tenant_id", "status"],
    )

    op.create_table(
        "agent_hub_run_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_hub_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_hub_run_events_run_sequence"),
    )
    op.create_index(
        "ix_agent_hub_run_events_run_sequence",
        "agent_hub_run_events",
        ["run_id", "sequence"],
    )

    op.create_table(
        "agent_hub_run_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=128), nullable=False),
        sa.Column("producer", sa.String(length=128), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_hub_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "id", name="uq_agent_hub_run_artifacts_run_id"),
        sa.UniqueConstraint(
            "run_id",
            "content_sha256",
            name="uq_agent_hub_run_artifacts_run_hash",
        ),
    )

    op.create_table(
        "agent_hub_run_checkpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("runtime_type", sa.String(length=128), nullable=False),
        sa.Column("runtime_version", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("state_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_hub_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "run_id",
            "sequence",
            name="uq_agent_hub_run_checkpoints_run_sequence",
        ),
    )
    op.create_index(
        "ix_agent_hub_run_checkpoints_run_sequence",
        "agent_hub_run_checkpoints",
        ["run_id", "sequence"],
    )

    op.create_table(
        "agent_hub_run_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_hub_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "approval_id", name="uq_agent_hub_approvals_run_approval"),
    )

    op.create_table(
        "agent_hub_run_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("provider_id", sa.String(length=128), nullable=False),
        sa.Column("cost_usd", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_hub_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_hub_run_usage_run_sequence"),
    )
    op.create_index(
        "ix_agent_hub_run_usage_tenant_run",
        "agent_hub_run_usage",
        ["tenant_id", "run_id"],
    )

    op.create_table(
        "agent_hub_run_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_name", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("delivered", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["agent_hub_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_hub_run_outbox_idempotency_key"),
    )
    op.create_index(
        "ix_agent_hub_run_outbox_delivered",
        "agent_hub_run_outbox",
        ["delivered", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_hub_run_outbox_delivered", table_name="agent_hub_run_outbox")
    op.drop_table("agent_hub_run_outbox")
    op.drop_index("ix_agent_hub_run_usage_tenant_run", table_name="agent_hub_run_usage")
    op.drop_table("agent_hub_run_usage")
    op.drop_table("agent_hub_run_approvals")
    op.drop_index(
        "ix_agent_hub_run_checkpoints_run_sequence",
        table_name="agent_hub_run_checkpoints",
    )
    op.drop_table("agent_hub_run_checkpoints")
    op.drop_table("agent_hub_run_artifacts")
    op.drop_index("ix_agent_hub_run_events_run_sequence", table_name="agent_hub_run_events")
    op.drop_table("agent_hub_run_events")
    op.drop_index("ix_agent_hub_run_steps_tenant_status", table_name="agent_hub_run_steps")
    op.drop_table("agent_hub_run_steps")
    op.drop_index("ix_agent_hub_runs_tenant_status", table_name="agent_hub_runs")
    op.drop_table("agent_hub_runs")
