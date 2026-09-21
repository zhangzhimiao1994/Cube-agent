"""Private runtime artifact hydration and durable write ownership.

Revision ID: 0023_runtime_artifacts
Revises: 0022_run_worker_leases
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_runtime_artifacts"
down_revision: str | Sequence[str] | None = "0022_run_worker_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_hub_runtime_artifacts",
        sa.Column("tenant_id", sa.UUID(), primary_key=True),
        sa.Column("run_id", sa.UUID(), sa.ForeignKey("agent_hub_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("permanent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("byte_size > 0", name="ck_runtime_artifact_byte_size"),
    )
    op.create_index("ix_runtime_artifacts_run", "agent_hub_runtime_artifacts", ["run_id"])
    op.create_table(
        "agent_hub_runtime_artifact_writes",
        sa.Column("tenant_id", sa.UUID(), primary_key=True),
        sa.Column("run_id", sa.UUID(), sa.ForeignKey("agent_hub_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("write_id", sa.UUID(), primary_key=True),
        sa.Column("artifact_id", sa.UUID(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "status IN ('reserved', 'written', 'aborted')", name="ck_runtime_artifact_write_status"
        ),
    )
    op.create_index("ix_runtime_artifact_writes_run", "agent_hub_runtime_artifact_writes", ["run_id"])
    op.create_index(
        "ix_runtime_artifact_write_owners", "agent_hub_runtime_artifact_writes",
        ["tenant_id", "run_id", "artifact_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("agent_hub_runtime_artifact_writes")
    op.drop_table("agent_hub_runtime_artifacts")
