"""Allow persistent admin log and setting resources.

Revision ID: 0012_admin_resource_logs
Revises: 0011_admin_resources
Create Date: 2026-08-09
"""

from collections.abc import Sequence

from alembic import op

revision = "0012_admin_resource_logs"
down_revision = "0011_admin_resources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        "kind IN ('workflow', 'skill', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        "kind IN ('workflow', 'skill', 'mcp', 'memory', 'hermes', 'audit')",
    )


__all__: Sequence[str] = ("downgrade", "upgrade")
