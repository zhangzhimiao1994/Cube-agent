"""Persist tenant-scoped project workspaces independently from conversations.

Revision ID: 0029_project_workspaces
Revises: 0028_conversation_run_queue
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_project_workspaces"
down_revision: str | Sequence[str] | None = "0028_conversation_run_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_hub_project_workspaces",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(),
            sa.ForeignKey("agent_hub_tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("workspace_path", sa.String(64), nullable=False),
        sa.Column(
            "legacy_workspace_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            name="uq_agent_hub_project_workspaces_tenant_project",
        ),
    )
    op.create_index(
        "ix_agent_hub_project_workspaces_tenant_updated",
        "agent_hub_project_workspaces",
        ["tenant_id", "updated_at"],
    )
    op.execute(
        """
        WITH workspace_counts AS (
            SELECT tenant_id, project_id, COUNT(DISTINCT workspace_path) AS workspace_count
            FROM agent_hub_conversations
            GROUP BY tenant_id, project_id
        ),
        latest_project AS (
            SELECT DISTINCT ON (tenant_id, project_id)
                tenant_id, project_id,
                COALESCE(NULLIF(project_label, ''), project_id) AS label,
                workspace_path, created_at, updated_at
            FROM agent_hub_conversations
            ORDER BY tenant_id, project_id, updated_at DESC, id DESC
        )
        INSERT INTO agent_hub_project_workspaces
            (id, tenant_id, project_id, label, workspace_path,
             legacy_workspace_count, created_at, updated_at)
        SELECT gen_random_uuid(), latest_project.tenant_id, latest_project.project_id,
            latest_project.label, latest_project.workspace_path,
            workspace_counts.workspace_count,
            latest_project.created_at, latest_project.updated_at
        FROM latest_project
        JOIN workspace_counts
          ON workspace_counts.tenant_id = latest_project.tenant_id
         AND workspace_counts.project_id = latest_project.project_id
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_hub_project_workspaces_tenant_updated",
        table_name="agent_hub_project_workspaces",
    )
    op.drop_table("agent_hub_project_workspaces")
