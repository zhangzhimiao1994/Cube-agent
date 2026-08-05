"""Enforce canonical local usernames.

Revision ID: 0006_username_check
Revises: 0005_auth
Create Date: 2026-08-05 00:00:05.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_username_check"
down_revision: str | Sequence[str] | None = "0005_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USERNAME_CHECK = (
    "username ~ '^[a-z][a-z0-9_-]{2,63}$' "
    "AND strpos(username, '..') = 0 "
    "AND strpos(username, '--') = 0 "
    "AND strpos(username, '__') = 0"
)


def upgrade() -> None:
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM agent_hub_users WHERE NOT ({_USERNAME_CHECK})
                ) THEN
                    RAISE EXCEPTION
                        'agent_hub_users contains invalid usernames; repair before upgrade';
                END IF;
            END
            $$
            """
        )
    )
    op.create_check_constraint(
        "ck_agent_hub_users_username",
        "agent_hub_users",
        _USERNAME_CHECK,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_hub_users_username", "agent_hub_users", type_="check"
    )
