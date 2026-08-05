"""Add bootstrap authentication state and stable user roles.

Revision ID: 0005_auth
Revises: 0004_encrypted_secrets
Create Date: 2026-08-05 00:00:04.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_auth"
down_revision: str | Sequence[str] | None = "0004_encrypted_secrets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM agent_hub_users
                    WHERE role NOT IN ('super_admin', 'admin', 'operator', 'viewer')
                ) THEN
                    RAISE EXCEPTION 'agent_hub_users contains invalid roles; repair before upgrade';
                END IF;
            END
            $$
            """
        )
    )
    op.create_check_constraint(
        "ck_agent_hub_users_role",
        "agent_hub_users",
        "role IN ('super_admin', 'admin', 'operator', 'viewer')",
    )
    op.create_table(
        "agent_hub_bootstrap_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "code_hash ~ '^[0-9a-f]{64}$'",
            name="ck_agent_hub_bootstrap_codes_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["agent_hub_tenants.id"],
            name="fk_agent_hub_bootstrap_codes_tenant_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_hub_bootstrap_codes"),
        sa.UniqueConstraint(
            "code_hash", name="uq_agent_hub_bootstrap_codes_code_hash"
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_hub_bootstrap_codes")
    op.drop_constraint(
        "ck_agent_hub_users_role", "agent_hub_users", type_="check"
    )
