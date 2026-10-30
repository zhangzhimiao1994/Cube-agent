"""Dedicated private review intents, effect claims and immutable events.

Revision ID: 0036_review_ledger
Revises: 0035_run_admission
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0036_review_ledger"
down_revision: str | Sequence[str] | None = "0035_run_admission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_hub_review_intents",
        sa.Column("review_id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("authorization_turn_id", sa.UUID(), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("action_digest", sa.String(64), nullable=False),
        sa.Column("effect_digest", sa.String(64), nullable=False),
        sa.Column("authorization_sha256", sa.String(64), nullable=False),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("policy_sha256", sa.String(64), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("private_action", sa.Text, nullable=False),
        sa.Column("private_config", JSONB, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("physical_identity", sa.UUID(), nullable=True),
        sa.Column("receipt_sha256", sa.String(64), nullable=True),
        sa.Column("private_receipt", sa.Text, nullable=True),
        sa.Column("risk", sa.String(16), nullable=True),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("consumed", sa.Boolean, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("review_id", "run_id", "tenant_id", name="uq_review_intent_owner"),
        sa.UniqueConstraint("attempt_id", name="uq_review_intent_attempt"),
        sa.ForeignKeyConstraint(
            ["attempt_id", "run_id", "tenant_id"],
            [
                "agent_hub_run_admission_attempts.attempt_id",
                "agent_hub_run_admission_attempts.run_id",
                "agent_hub_run_admission_attempts.tenant_id",
            ],
            name="fk_review_intent_attempt",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["authorization_turn_id", "tenant_id", "run_id"],
            [
                "agent_hub_authenticated_inputs.turn_id",
                "agent_hub_authenticated_inputs.tenant_id",
                "agent_hub_authenticated_inputs.run_id",
            ],
            name="fk_review_intent_original",
        ),
        sa.CheckConstraint(
            "status IN ('prepared','issued','allow','deny','needs_human','timeout',"
            "'unavailable','invalid_response','unknown','cancelled')",
            name="ck_review_intent_status",
        ),
        sa.CheckConstraint("NOT consumed OR status = 'allow'", name="ck_review_intent_consumed"),
        sa.CheckConstraint(
            "octet_length(private_action) BETWEEN 1 AND 65536 AND "
            "(private_receipt IS NULL OR octet_length(private_receipt) <= 16384)",
            name="ck_review_intent_bounds",
        ),
        sa.CheckConstraint(
            "action_digest ~ '^[0-9a-f]{64}$' AND effect_digest ~ '^[0-9a-f]{64}$' AND "
            "authorization_sha256 ~ '^[0-9a-f]{64}$' AND config_sha256 ~ '^[0-9a-f]{64}$' "
            "AND policy_sha256 ~ '^[0-9a-f]{64}$' AND source_sha256 ~ '^[0-9a-f]{64}$' "
            "AND request_sha256 ~ '^[0-9a-f]{64}$' AND "
            "(receipt_sha256 IS NULL OR receipt_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_review_intent_digests",
        ),
    )
    op.create_index(
        "ix_review_intent_original",
        "agent_hub_review_intents",
        ["tenant_id", "authorization_turn_id"],
    )
    op.create_table(
        "agent_hub_review_effects",
        sa.Column("tenant_id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("authorization_turn_id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("effect_digest", sa.String(64), primary_key=True, nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("review_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("review_id", name="uq_review_effect_intent"),
        sa.ForeignKeyConstraint(
            ["review_id", "run_id", "tenant_id"],
            [
                "agent_hub_review_intents.review_id",
                "agent_hub_review_intents.run_id",
                "agent_hub_review_intents.tenant_id",
            ],
            name="fk_review_effect_intent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["authorization_turn_id", "tenant_id", "run_id"],
            [
                "agent_hub_authenticated_inputs.turn_id",
                "agent_hub_authenticated_inputs.tenant_id",
                "agent_hub_authenticated_inputs.run_id",
            ],
            name="fk_review_effect_original",
        ),
        sa.CheckConstraint("effect_digest ~ '^[0-9a-f]{64}$'", name="ck_review_effect_digest"),
    )
    op.create_table(
        "agent_hub_review_events",
        sa.Column("sequence", sa.BigInteger, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("authorization_turn_id", sa.UUID(), nullable=False),
        sa.Column("review_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("evidence", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["review_id", "run_id", "tenant_id"],
            [
                "agent_hub_review_intents.review_id",
                "agent_hub_review_intents.run_id",
                "agent_hub_review_intents.tenant_id",
            ],
            name="fk_review_event_intent",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("review_id", "kind", name="uq_review_event_transition"),
        sa.CheckConstraint(
            "kind IN ('prepared','issued','terminal','consumed')", name="ck_review_event_kind"
        ),
    )
    op.create_index(
        "ix_review_event_original",
        "agent_hub_review_events",
        ["tenant_id", "authorization_turn_id", "sequence"],
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            LOCK TABLE agent_hub_review_intents, agent_hub_review_effects,
                       agent_hub_review_events IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM agent_hub_review_intents)
                OR EXISTS (SELECT 1 FROM agent_hub_review_effects)
                OR EXISTS (SELECT 1 FROM agent_hub_review_events) THEN
                RAISE EXCEPTION 'Cannot downgrade: review evidence exists'
                    USING ERRCODE = 'check_violation';
            END IF;
        END; $$
    """)
    op.drop_table("agent_hub_review_events")
    op.drop_table("agent_hub_review_effects")
    op.drop_table("agent_hub_review_intents")
