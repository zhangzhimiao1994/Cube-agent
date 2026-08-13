"""Allow persistent OpenClaw admin resources.

Revision ID: 0016_openclaw_admin_resources
Revises: 0015_user_management_flags
Create Date: 2026-08-13
"""

from collections.abc import Sequence

from alembic import op

revision = "0016_openclaw_admin_resources"
down_revision = "0015_user_management_flags"
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
        "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', 'channel', 'openclaw')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        type_="check",
    )
    op.execute("DELETE FROM agent_hub_admin_resources WHERE kind = 'openclaw'")
    op.create_check_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', 'channel')",
    )


__all__: Sequence[str] = ("downgrade", "upgrade")
