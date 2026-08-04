"""Constrain configuration revisions to supported lifecycle states.

Revision ID: 0002_config_status_check
Revises: 0001_initial
Create Date: 2026-08-05 00:00:01.000000
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_config_status_check"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_agent_hub_config_revisions_status",
        "agent_hub_config_revisions",
        "status IN ('draft', 'published', 'superseded')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_hub_config_revisions_status",
        "agent_hub_config_revisions",
        type_="check",
    )
