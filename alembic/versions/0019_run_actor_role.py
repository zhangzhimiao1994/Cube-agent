"""Persist run submitter role snapshots.

Revision ID: 0019_run_actor_role
Revises: 0018_evolution_admin_resources
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "0019_run_actor_role"
down_revision = "0018_evolution_admin_resources"
branch_labels = None
depends_on = None

_ROLE_CHECK = "actor_role IS NULL OR actor_role IN ('super_admin', 'admin', 'operator', 'viewer')"


def upgrade() -> None:
    op.add_column("agent_hub_runs", sa.Column("actor_role", sa.String(length=32), nullable=True))
    op.create_check_constraint(
        "ck_agent_hub_runs_actor_role",
        "agent_hub_runs",
        _ROLE_CHECK,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_hub_runs_actor_role",
        "agent_hub_runs",
        type_="check",
    )
    op.drop_column("agent_hub_runs", "actor_role")


__all__: Sequence[str] = ("downgrade", "upgrade")
