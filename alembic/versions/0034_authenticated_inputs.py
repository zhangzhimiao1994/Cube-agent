"""Private authenticated originals and the current run input link.

Revision ID: 0034_authenticated_inputs
Revises: 0033_provider_continuations
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0034_authenticated_inputs'
down_revision: str | Sequence[str] | None = '0033_provider_continuations'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'agent_hub_authenticated_inputs',
        sa.Column('turn_id', sa.UUID(), primary_key=True),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('run_id', sa.UUID(), sa.ForeignKey('agent_hub_runs.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('actor_id', sa.UUID(), nullable=False),
        sa.Column('actor_role', sa.String(32), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('message_sha256', sa.String(64), nullable=False),
        sa.Column('input_scope', sa.Text(), nullable=False),
        sa.Column('scope_sha256', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint('turn_id', 'tenant_id', 'run_id', name='uq_authenticated_input_binding'),
        sa.CheckConstraint("actor_role IN ('super_admin', 'admin', 'operator', 'viewer')",
                           name='ck_authenticated_input_role'),
        sa.CheckConstraint("message_sha256 ~ '^[0-9a-f]{64}$'",
                           name='ck_authenticated_input_message_hash'),
        sa.CheckConstraint("scope_sha256 ~ '^[0-9a-f]{64}$'",
                           name='ck_authenticated_input_scope_hash'),
    )
    op.create_index('ix_authenticated_inputs_run', 'agent_hub_authenticated_inputs',
                    ['tenant_id', 'run_id'])
    op.create_table(
        'agent_hub_run_input_authorizations',
        sa.Column('run_id', sa.UUID(), sa.ForeignKey('agent_hub_runs.id', ondelete='CASCADE'),
                  primary_key=True),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('turn_id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ['turn_id', 'tenant_id', 'run_id'],
            ['agent_hub_authenticated_inputs.turn_id', 'agent_hub_authenticated_inputs.tenant_id',
             'agent_hub_authenticated_inputs.run_id'],
            name='fk_run_input_authorization_binding', ondelete='CASCADE'),
        sa.UniqueConstraint('turn_id', name='uq_run_input_authorization_turn'),
    )


def downgrade() -> None:
    op.execute('''
        DO $$ BEGIN
            LOCK TABLE agent_hub_authenticated_inputs,
                       agent_hub_run_input_authorizations IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM agent_hub_authenticated_inputs)
                OR EXISTS (SELECT 1 FROM agent_hub_run_input_authorizations) THEN
                RAISE EXCEPTION 'Cannot downgrade: authenticated input provenance exists'
                    USING ERRCODE = 'check_violation';
            END IF;
        END; $$
    ''')
    op.drop_table('agent_hub_run_input_authorizations')
    op.drop_index('ix_authenticated_inputs_run', table_name='agent_hub_authenticated_inputs')
    op.drop_table('agent_hub_authenticated_inputs')
