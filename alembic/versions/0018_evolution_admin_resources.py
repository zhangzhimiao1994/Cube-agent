"""Allow persistent evolution admin resources.

Revision ID: 0018_evolution_admin_resources
Revises: 0017_schedule_admin_resources
Create Date: 2026-08-14
"""

from collections.abc import Sequence

from alembic import op

revision = "0018_evolution_admin_resources"
down_revision = "0017_schedule_admin_resources"
branch_labels = None
depends_on = None

_CURRENT_KINDS = "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', 'channel', 'openclaw', 'openclaw_session', 'schedule')"
_NEXT_KINDS = "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', 'channel', 'openclaw', 'openclaw_session', 'schedule', 'evolution')"


def upgrade() -> None:
    op.drop_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        _NEXT_KINDS,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        type_="check",
    )
    op.execute("DELETE FROM agent_hub_admin_resources WHERE kind = 'evolution'")
    op.create_check_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        _CURRENT_KINDS,
    )


__all__: Sequence[str] = ("downgrade", "upgrade")