"""Store tenant-scoped encrypted secret payloads.

Revision ID: 0004_encrypted_secrets
Revises: 0003_one_published
Create Date: 2026-08-05 00:00:03.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_encrypted_secrets"
down_revision: str | Sequence[str] | None = "0003_one_published"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_hub_secrets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("nonce", sa.String(length=16), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["agent_hub_tenants.id"],
            name="fk_agent_hub_secrets_tenant_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_hub_secrets"),
        sa.UniqueConstraint(
            "tenant_id",
            "fingerprint",
            name="uq_agent_hub_secrets_tenant_fingerprint",
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_hub_secrets")
