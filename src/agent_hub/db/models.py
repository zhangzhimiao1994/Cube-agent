from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from agent_hub.db.base import Base


class TenantRow(Base):
    __tablename__ = "agent_hub_tenants"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserRow(Base):
    __tablename__ = "agent_hub_users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "username"),
        CheckConstraint(
            "role IN ('super_admin', 'admin', 'operator', 'viewer')",
            name="ck_agent_hub_users_role",
        ),
        CheckConstraint(
            "username ~ '^[a-z][a-z0-9_-]{2,63}$' "
            "AND strpos(username, '..') = 0 "
            "AND strpos(username, '--') = 0 "
            "AND strpos(username, '__') = 0",
            name="ck_agent_hub_users_username",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("agent_hub_tenants.id"))
    username: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    feishu_open_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[str] = mapped_column(String(32))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    protected: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class BootstrapCodeRow(Base):
    __tablename__ = "agent_hub_bootstrap_codes"
    __table_args__ = (
        UniqueConstraint("code_hash", name="uq_agent_hub_bootstrap_codes_code_hash"),
        CheckConstraint(
            "code_hash ~ '^[0-9a-f]{64}$'",
            name="ck_agent_hub_bootstrap_codes_hash",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    code_hash: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("agent_hub_tenants.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SecretRow(Base):
    __tablename__ = "agent_hub_secrets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "fingerprint",
            name="uq_agent_hub_secrets_tenant_fingerprint",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("agent_hub_tenants.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[str] = mapped_column(String(16))
    ciphertext: Mapped[str] = mapped_column(Text)
    created_by: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ConfigRevisionRow(Base):
    __tablename__ = "agent_hub_config_revisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version"),
        CheckConstraint(
            "status IN ('draft', 'published', 'superseded')",
            name="ck_agent_hub_config_revisions_status",
        ),
        Index(
            "uq_agent_hub_config_revisions_one_published_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("agent_hub_tenants.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    document: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_by: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ConversationRow(Base):
    __tablename__ = "agent_hub_conversations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "conversation_id",
            name="uq_agent_hub_conversations_tenant_conversation",
        ),
        Index(
            "ix_agent_hub_conversations_tenant_archived_updated",
            "tenant_id",
            "archived_at",
            "updated_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_hub_tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    workspace_path: Mapped[str] = mapped_column(String(64), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RunRow(Base):
    __tablename__ = "agent_hub_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_agent_hub_runs_tenant_idempotency_key",
        ),
        CheckConstraint(
            "status IN ("
            "'queued', 'planning', 'waiting_user_mode', 'running', "
            "'waiting_approval', 'retrying', 'paused', 'synthesizing', "
            "'completed', 'failed', 'cancelled')",
            name="ck_agent_hub_runs_status",
        ),
        CheckConstraint(
            "mode IS NULL OR mode IN ('direct', 'dispatch', 'discuss', 'hybrid')",
            name="ck_agent_hub_runs_mode",
        ),
        CheckConstraint(
            "actor_role IS NULL OR actor_role IN ('super_admin', 'admin', 'operator', 'viewer')",
            name="ck_agent_hub_runs_actor_role",
        ),
        Index("ix_agent_hub_runs_tenant_status", "tenant_id", "status"),
        Index(
            "ix_agent_hub_runs_tenant_conversation_created",
            "tenant_id",
            text("(routing_decision ->> 'conversation_id')"),
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    request: Mapped[str] = mapped_column(Text)
    mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    routing_decision: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    blocked_by_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_hub_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    worker_lease_token: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=True
    )
    worker_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    worker_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConversationQueueItemRow(Base):
    __tablename__ = "agent_hub_conversation_queue_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_agent_hub_conversation_queue_tenant_idempotency",
        ),
        CheckConstraint(
            "status IN ('queued', 'redirecting', 'released', 'cancelled', "
            "'running', 'completed', 'failed')",
            name="ck_agent_hub_conversation_queue_status",
        ),
        Index(
            "ix_agent_hub_conversation_queue_order",
            "tenant_id",
            "conversation_id",
            "position",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    predecessor_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_hub_runs.id", ondelete="RESTRICT"), nullable=False
    )
    successor_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_hub_runs.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    attachments: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    references: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    position: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RunStepRow(Base):
    __tablename__ = "agent_hub_run_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "step_id", name="uq_agent_hub_run_steps_run_step"),
        Index("ix_agent_hub_run_steps_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_hub_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(String(128))
    actor: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RunEventRow(Base):
    __tablename__ = "agent_hub_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_hub_run_events_run_sequence"),
        Index("ix_agent_hub_run_events_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_hub_runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RunArtifactRow(Base):
    __tablename__ = "agent_hub_run_artifacts"
    __table_args__ = (
        UniqueConstraint("run_id", "id", name="uq_agent_hub_run_artifacts_run_id"),
        UniqueConstraint(
            "run_id",
            "content_sha256",
            name="uq_agent_hub_run_artifacts_run_hash",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_hub_runs.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(128))
    producer: Mapped[str] = mapped_column(String(128))
    content_sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RuntimeArtifactRow(Base):
    """Private hydration storage, never a public artifact publication."""

    __tablename__ = "agent_hub_runtime_artifacts"
    __table_args__ = (
        CheckConstraint("byte_size > 0", name="ck_runtime_artifact_byte_size"),
        Index("ix_runtime_artifacts_run", "run_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_hub_runs.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    content_sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(BigInteger)
    permanent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")


class RuntimeArtifactWriteRow(Base):
    """Durable owners and abort fences, independent of artifact row lifetime."""

    __tablename__ = "agent_hub_runtime_artifact_writes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('reserved', 'written', 'aborted')", name="ck_runtime_artifact_write_status"
        ),
        Index("ix_runtime_artifact_writes_run", "run_id"),
        Index("ix_runtime_artifact_write_owners", "tenant_id", "run_id", "artifact_id", "status"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_hub_runs.id", ondelete="CASCADE"), primary_key=True
    )
    write_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    artifact_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))


class RunCheckpointRow(Base):
    __tablename__ = "agent_hub_run_checkpoints"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_hub_run_checkpoints_run_sequence"),
        Index("ix_agent_hub_run_checkpoints_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_hub_runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    runtime_type: Mapped[str] = mapped_column(String(128))
    runtime_version: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(20))
    state: Mapped[dict[str, object]] = mapped_column(JSONB)
    state_sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RunApprovalRow(Base):
    __tablename__ = "agent_hub_run_approvals"
    __table_args__ = (
        UniqueConstraint("run_id", "approval_id", name="uq_agent_hub_approvals_run_approval"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_hub_runs.id", ondelete="CASCADE"), nullable=False
    )
    approval_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RunUsageRow(Base):
    __tablename__ = "agent_hub_run_usage"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_hub_run_usage_run_sequence"),
        Index("ix_agent_hub_run_usage_tenant_run", "tenant_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_hub_runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    provider_id: Mapped[str] = mapped_column(String(128))
    cost_usd: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RunOutboxRow(Base):
    __tablename__ = "agent_hub_run_outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_agent_hub_run_outbox_idempotency_key"),
        Index("ix_agent_hub_run_outbox_delivered", "delivered", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_hub_runs.id", ondelete="CASCADE"), nullable=False
    )
    task_name: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChannelInboundDedupRow(Base):
    __tablename__ = "agent_hub_channel_dedup"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "tenant_external_id",
            "event_id",
            name="uq_agent_hub_channel_dedup_event",
        ),
        UniqueConstraint(
            "channel",
            "tenant_external_id",
            "message_id",
            name="uq_agent_hub_channel_dedup_message",
        ),
        CheckConstraint(
            "status IN ('reserved', 'submitted', 'completed')",
            name="ck_agent_hub_channel_dedup_status",
        ),
        Index("ix_agent_hub_channel_dedup_cleanup", "status", "completed_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


ChannelDedupRow = ChannelInboundDedupRow


class AdminResourceRow(Base):
    __tablename__ = "agent_hub_admin_resources"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "kind",
            "resource_id",
            name="uq_agent_hub_admin_resources_tenant_kind_resource",
        ),
        CheckConstraint(
            "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'skill_source', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', 'channel', 'openclaw', 'openclaw_session', 'schedule', 'evolution', 'plugin', 'plugin_signing_key', 'capability_install')",
            name="ck_agent_hub_admin_resources_kind",
        ),
        Index("ix_agent_hub_admin_resources_tenant_kind", "tenant_id", "kind"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
