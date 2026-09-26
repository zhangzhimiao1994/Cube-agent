"""Index runs by conversation for complete history loading.

Revision ID: 0025_run_conversation_index
Revises: 0024_capability_install
Create Date: 2026-09-26 03:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025_run_conversation_index"
down_revision: str | Sequence[str] | None = "0024_capability_install"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_agent_hub_runs_tenant_conversation_created"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} ON agent_hub_runs "
            "(tenant_id, (routing_decision ->> 'conversation_id'), created_at, id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
