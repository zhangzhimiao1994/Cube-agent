"""Allow immutable Skill source revision admin resources.

Revision ID: 0030_skill_source_revisions
Revises: 0029_project_workspaces
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030_skill_source_revisions"
down_revision: str | Sequence[str] | None = "0029_project_workspaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_KINDS = "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'skill_source', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', 'channel', 'openclaw', 'openclaw_session', 'schedule', 'evolution', 'plugin', 'plugin_signing_key', 'capability_install')"
_NEXT_KINDS = "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'skill_source', 'skill_source_revision', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', 'channel', 'openclaw', 'openclaw_session', 'schedule', 'evolution', 'plugin', 'plugin_signing_key', 'capability_install')"


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
    op.execute(
        "DELETE FROM agent_hub_admin_resources "
        "WHERE kind = 'setting' "
        "AND resource_id LIKE 'skill-source-recovery-%'"
    )
    op.execute(
        "UPDATE agent_hub_admin_resources "
        "SET payload = payload - 'active_revision_id' "
        "WHERE kind = 'skill_source' "
        "AND payload ? 'active_revision_id'"
    )
    op.execute(
        "DELETE FROM agent_hub_admin_resources WHERE kind = 'skill_source_revision'"
    )
    op.create_check_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        _CURRENT_KINDS,
    )
