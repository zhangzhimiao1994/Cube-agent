"""Allow persistent plugin handover admin resources.

Revision ID: 0032_plugin_handover
Revises: 0031_question_search
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0032_plugin_handover"
down_revision: str | Sequence[str] | None = "0031_question_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_KINDS = (
    "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'skill_source', "
    "'skill_source_revision', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', "
    "'channel', 'openclaw', 'openclaw_session', 'schedule', 'evolution', 'plugin', "
    "'plugin_signing_key', 'capability_install')"
)
_NEXT_KINDS = (
    "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'skill_source', "
    "'skill_source_revision', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', "
    "'channel', 'openclaw', 'openclaw_session', 'schedule', 'evolution', 'plugin', "
    "'plugin_signing_key', 'capability_install', 'plugin_handover')"
)


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
    # Hold the DDL lock through the guard and CHECK replacement to prevent new handovers.
    op.execute(
        """
        DO $$
        BEGIN
            LOCK TABLE agent_hub_admin_resources IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (
                SELECT 1 FROM agent_hub_admin_resources
                WHERE kind = 'plugin_handover'
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade: plugin_handover records exist'
                    USING ERRCODE = 'check_violation';
            END IF;
        END;
        $$
        """
    )
    op.drop_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_hub_admin_resources_kind",
        "agent_hub_admin_resources",
        _CURRENT_KINDS,
    )
