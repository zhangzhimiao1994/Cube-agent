"""Allow trusted team Skill source admin resources.

Revision ID: 0027_skill_sources
Revises: 0026_conversation_metadata
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027_skill_sources"
down_revision: str | Sequence[str] | None = "0026_conversation_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_KINDS = "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', 'channel', 'openclaw', 'openclaw_session', 'schedule', 'evolution', 'plugin', 'plugin_signing_key', 'capability_install')"
_NEXT_KINDS = "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'skill_source', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', 'channel', 'openclaw', 'openclaw_session', 'schedule', 'evolution', 'plugin', 'plugin_signing_key', 'capability_install')"


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
    op.execute("DELETE FROM agent_hub_admin_resources WHERE kind = 'skill_source'")
    op.execute(
        "UPDATE agent_hub_admin_resources "
        "SET payload = payload - 'source' - 'archive_sha256' "
        "WHERE kind = 'skill' AND (payload ? 'source' OR payload ? 'archive_sha256')"
    )
    op.create_check_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        _CURRENT_KINDS,
    )
