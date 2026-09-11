"""Track run worker leases for safe recovery.

Revision ID: 0022_run_worker_leases
Revises: 0021_plugin_key_resources
Create Date: 2026-09-12 03:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_run_worker_leases"
down_revision: str | Sequence[str] | None = "0021_plugin_key_resources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_hub_runs",
        sa.Column("worker_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "agent_hub_runs",
        sa.Column("worker_lease_token", sa.UUID(), nullable=True),
    )
    op.add_column(
        "agent_hub_runs",
        sa.Column("worker_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "agent_hub_runs",
        sa.Column("worker_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_agent_hub_runs_worker_recovery",
        "agent_hub_runs",
        ["status", "worker_lease_expires_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_hub_runs_worker_recovery", table_name="agent_hub_runs")
    op.drop_column("agent_hub_runs", "worker_heartbeat_at")
    op.drop_column("agent_hub_runs", "worker_lease_expires_at")
    op.drop_column("agent_hub_runs", "worker_lease_token")
    op.drop_column("agent_hub_runs", "worker_id")


__all__: Sequence[str] = ("downgrade", "upgrade")
