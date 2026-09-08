"""Allow plugin signing key admin resources.

Revision ID: 0021_plugin_signing_key_admin_resources
Revises: 0020_plugin_admin_resources
Create Date: 2026-09-09 00:08:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021_plugin_signing_key_admin_resources"
down_revision: str | Sequence[str] | None = "0020_plugin_admin_resources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_KINDS = "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', 'channel', 'openclaw', 'openclaw_session', 'schedule', 'evolution', 'plugin')"
_NEXT_KINDS = "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', 'channel', 'openclaw', 'openclaw_session', 'schedule', 'evolution', 'plugin', 'plugin_signing_key')"


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
    op.execute("DELETE FROM agent_hub_admin_resources WHERE kind = 'plugin_signing_key'")
    op.create_check_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        _CURRENT_KINDS,
    )
