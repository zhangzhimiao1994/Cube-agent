"""Shared original-run admission pools, physical attempts and audit events.

Revision ID: 0035_run_admission
Revises: 0034_authenticated_inputs
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0035_run_admission'
down_revision: str | Sequence[str] | None = '0034_authenticated_inputs'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('agent_hub_run_admission_pools',
        sa.Column('run_id', sa.UUID(), sa.ForeignKey('agent_hub_runs.id', ondelete='CASCADE'),
                  primary_key=True),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('authorization_turn_id', sa.UUID(), nullable=False),
        sa.Column('actor_id', sa.UUID(), nullable=False),
        sa.Column('actor_role', sa.String(32), nullable=False),
        sa.Column('message_sha256', sa.String(64), nullable=False),
        sa.Column('scope_sha256', sa.String(64), nullable=False),
        sa.Column('config_sha256', sa.String(64), nullable=False),
        sa.Column('original_token_grant', sa.BigInteger(), nullable=False),
        sa.Column('original_deadline', sa.DateTime(timezone=True), nullable=False),
        sa.Column('review_token_limit', sa.BigInteger(), nullable=False),
        sa.Column('review_count_limit', sa.BigInteger(), nullable=False),
        sa.Column('review_cost_limit', sa.Numeric(13, 6), nullable=False),
        sa.Column('token_charged', sa.BigInteger(), nullable=False),
        sa.Column('review_token_charged', sa.BigInteger(), nullable=False),
        sa.Column('review_count_charged', sa.BigInteger(), nullable=False),
        sa.Column('review_cost_charged', sa.Numeric(13, 6), nullable=False),
        sa.Column('closed', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint('run_id', 'tenant_id', name='uq_admission_pool_owner'),
        sa.ForeignKeyConstraint(['authorization_turn_id', 'tenant_id', 'run_id'],
            ['agent_hub_authenticated_inputs.turn_id', 'agent_hub_authenticated_inputs.tenant_id',
             'agent_hub_authenticated_inputs.run_id'], name='fk_admission_pool_original'),
        sa.CheckConstraint('original_token_grant BETWEEN 1 AND 10000000 AND '
            'review_token_limit BETWEEN 1 AND original_token_grant AND review_count_limit > 0',
            name='ck_admission_pool_limits'),
        sa.CheckConstraint('token_charged BETWEEN 0 AND original_token_grant AND '
            'review_token_charged BETWEEN 0 AND review_token_limit AND '
            'review_token_charged <= token_charged AND '
            'review_count_charged BETWEEN 0 AND review_count_limit AND '
            'review_cost_limit BETWEEN 0 AND 1000000 AND '
            'review_cost_charged BETWEEN 0 AND review_cost_limit', name='ck_admission_pool_charges'),
        sa.CheckConstraint("config_sha256 ~ '^[0-9a-f]{64}$' AND "
            "message_sha256 ~ '^[0-9a-f]{64}$' AND scope_sha256 ~ '^[0-9a-f]{64}$'",
            name='ck_admission_pool_digests'))
    op.create_table('agent_hub_run_admission_attempts',
        sa.Column('attempt_id', sa.UUID(), primary_key=True),
        sa.Column('run_id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('purpose', sa.String(16), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('request_sha256', sa.String(64), nullable=False),
        sa.Column('deployment', sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column('worker_id', sa.String(128), nullable=False),
        sa.Column('worker_lease_token', sa.UUID(), nullable=False),
        sa.Column('token_hold', sa.BigInteger(), nullable=False),
        sa.Column('review_cost_hold', sa.Numeric(13, 6), nullable=True),
        sa.Column('physical_transport_id', sa.UUID(), nullable=True),
        sa.Column('receipt_sha256', sa.String(64), nullable=True),
        sa.Column('receipt', sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['run_id', 'tenant_id'],
            ['agent_hub_run_admission_pools.run_id', 'agent_hub_run_admission_pools.tenant_id'],
            name='fk_admission_attempt_pool', ondelete='CASCADE'),
        sa.UniqueConstraint('attempt_id', 'run_id', 'tenant_id', name='uq_admission_attempt_owner'),
        sa.UniqueConstraint('physical_transport_id', name='uq_admission_physical_transport'),
        sa.CheckConstraint("purpose IN ('primary', 'review')", name='ck_admission_attempt_purpose'),
        sa.CheckConstraint("state IN ('prepared', 'issued', 'completed', 'cancelled', 'unknown', "
            "'invalid')", name='ck_admission_attempt_state'),
        sa.CheckConstraint('token_hold BETWEEN 1 AND 10000000 AND '
            "((purpose = 'primary' AND review_cost_hold IS NULL) OR "
            "(purpose = 'review' AND review_cost_hold BETWEEN 0 AND 1000000))",
            name='ck_admission_attempt_hold'),
        sa.CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$' AND "
            "(receipt_sha256 IS NULL OR receipt_sha256 ~ '^[0-9a-f]{64}$')",
            name='ck_admission_attempt_digests'))
    op.create_index('ix_admission_attempt_run', 'agent_hub_run_admission_attempts',
                    ['tenant_id', 'run_id'])
    op.create_table('agent_hub_run_admission_events',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('run_id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('attempt_id', sa.UUID(), nullable=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('evidence', sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['run_id', 'tenant_id'],
            ['agent_hub_run_admission_pools.run_id', 'agent_hub_run_admission_pools.tenant_id'],
            name='fk_admission_event_pool', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['attempt_id', 'run_id', 'tenant_id'],
            ['agent_hub_run_admission_attempts.attempt_id', 'agent_hub_run_admission_attempts.run_id',
             'agent_hub_run_admission_attempts.tenant_id'], name='fk_admission_event_attempt',
             ondelete='CASCADE'),
        sa.UniqueConstraint('run_id', 'attempt_id', 'kind', name='uq_admission_event_transition'),
        sa.CheckConstraint("kind IN ('opened', 'prepared', 'issued', 'completed', 'cancelled', "
            "'unknown', 'invalid')", name='ck_admission_event_kind'))
    op.create_index('ix_admission_event_run', 'agent_hub_run_admission_events', ['tenant_id', 'run_id'])


def downgrade() -> None:
    op.execute('''
        DO $$ BEGIN
            LOCK TABLE agent_hub_run_admission_pools, agent_hub_run_admission_attempts,
                       agent_hub_run_admission_events IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM agent_hub_run_admission_pools)
                OR EXISTS (SELECT 1 FROM agent_hub_run_admission_attempts)
                OR EXISTS (SELECT 1 FROM agent_hub_run_admission_events) THEN
                RAISE EXCEPTION 'Cannot downgrade: admission evidence exists'
                    USING ERRCODE = 'check_violation';
            END IF;
        END; $$
    ''')
    op.drop_table('agent_hub_run_admission_events')
    op.drop_table('agent_hub_run_admission_attempts')
    op.drop_table('agent_hub_run_admission_pools')
