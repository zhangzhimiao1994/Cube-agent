"""Qualified clock DDL; migration wrapper delegates here without changing legacy rows."""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0037_execution_clock"
down_revision: str | Sequence[str] | None = "0036_review_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_hub_runs") as batch:
        batch.add_column(sa.Column("execution_clock_sha256", sa.String(64)))
        batch.create_unique_constraint("uq_run_execution_clock_binding", ["id", "execution_clock_sha256"])
    op.create_table("agent_hub_run_execution_clocks",
        sa.Column("run_id", sa.UUID(), sa.ForeignKey("agent_hub_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("authorization_turn_id", sa.UUID(), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("binding_sha256", sa.String(64), nullable=False),
        sa.Column("policy", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("hard_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("spent_seconds", sa.Float, nullable=False),
        sa.Column("active_since", sa.DateTime(timezone=True)),
        sa.Column("worker_id", sa.String(128)), sa.Column("worker_token", sa.UUID()),
        sa.Column("progress_units", sa.Integer, nullable=False),
        sa.Column("checkpoint_id", sa.UUID()), sa.Column("checkpoint_sha256", sa.String(64)),
        sa.Column("version", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(["run_id", "binding_sha256"],
            ["agent_hub_runs.id", "agent_hub_runs.execution_clock_sha256"], name="fk_execution_clock_witness"),
        sa.ForeignKeyConstraint(["authorization_turn_id", "tenant_id", "run_id"],
            ["agent_hub_authenticated_inputs.turn_id", "agent_hub_authenticated_inputs.tenant_id",
             "agent_hub_authenticated_inputs.run_id"], name="fk_execution_clock_original"),
        sa.CheckConstraint("spent_seconds >= 0 AND progress_units >= 0 AND version > 0",
                           name="ck_execution_clock_accounting"),
        sa.CheckConstraint("(active_since IS NULL AND worker_id IS NULL AND worker_token IS NULL) OR "
            "(active_since IS NOT NULL AND worker_id IS NOT NULL AND worker_token IS NOT NULL)",
            name="ck_execution_clock_owned_interval"))


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        # Match application lock order and retain both locks through all DDL.
        # A closed/parked clock or orphan witness is still durable recovery data.
        op.execute("""
            DO $$ BEGIN
                LOCK TABLE agent_hub_runs IN ACCESS EXCLUSIVE MODE;
                LOCK TABLE agent_hub_run_execution_clocks IN ACCESS EXCLUSIVE MODE;
                IF EXISTS (SELECT 1 FROM agent_hub_run_execution_clocks)
                    OR EXISTS (SELECT 1 FROM agent_hub_runs WHERE execution_clock_sha256 IS NOT NULL) THEN
                    RAISE EXCEPTION 'Cannot downgrade: execution clock recovery state exists'
                        USING ERRCODE = 'check_violation';
                END IF;
            END; $$
        """)
    elif (connection.scalar(sa.text("SELECT 1 FROM agent_hub_run_execution_clocks LIMIT 1")) is not None
            or connection.scalar(sa.text(
                "SELECT 1 FROM agent_hub_runs WHERE execution_clock_sha256 IS NOT NULL LIMIT 1")) is not None):
        # Local SQLite migration coverage has no PG table locks/SQLSTATE. It must
        # still refuse data loss; its existing datetime/type behaviour is unchanged.
        raise RuntimeError("Cannot downgrade: execution clock recovery state exists")
    op.drop_table("agent_hub_run_execution_clocks")
    with op.batch_alter_table("agent_hub_runs") as batch:
        batch.drop_constraint("uq_run_execution_clock_binding", type_="unique")
        batch.drop_column("execution_clock_sha256")
