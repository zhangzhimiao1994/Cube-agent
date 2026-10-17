"""Dedicated private provider continuation storage.

Revision ID: 0033_provider_continuations
Revises: 0032_plugin_handover
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_provider_continuations"
down_revision: str | Sequence[str] | None = "0032_plugin_handover"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_hub_provider_continuations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), sa.ForeignKey("agent_hub_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ledger_key", sa.String(128), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("tenant_id", "run_id", "ledger_key", name="uq_provider_continuations_ledger"),
        sa.CheckConstraint("byte_size > 0 AND byte_size <= 1048576", name="ck_provider_continuation_size"),
        sa.CheckConstraint("octet_length(payload) = byte_size", name="ck_provider_continuation_payload_size"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_provider_continuation_sha256"),
        sa.CheckConstraint("ledger_key ~ '^[a-z0-9][a-z0-9_.:-]{0,127}$'", name="ck_provider_continuation_ledger"),
    )
    op.create_index("ix_provider_continuations_run", "agent_hub_provider_continuations", ["tenant_id", "run_id"])


def downgrade() -> None:
    # Refuse to silently destroy private recovery data while references may exist.
    op.execute("""
        DO $$ BEGIN
            LOCK TABLE agent_hub_provider_continuations IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM agent_hub_provider_continuations) THEN
                RAISE EXCEPTION 'Cannot downgrade: provider continuations exist'
                    USING ERRCODE = 'check_violation';
            END IF;
        END; $$
    """)
    op.drop_index("ix_provider_continuations_run", table_name="agent_hub_provider_continuations")
    op.drop_table("agent_hub_provider_continuations")
