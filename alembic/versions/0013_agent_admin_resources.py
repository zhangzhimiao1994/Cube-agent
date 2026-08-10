"""Allow persistent agent and main agent admin resources.

Revision ID: 0013_agent_admin_resources
Revises: 0012_admin_resource_logs
Create Date: 2026-08-10
"""

from collections.abc import Sequence

from alembic import op

revision = "0013_agent_admin_resources"
down_revision = "0012_admin_resource_logs"
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
        "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting')",
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
        "kind IN ('workflow', 'skill', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting')",
    )


__all__: Sequence[str] = ("downgrade", "upgrade")
