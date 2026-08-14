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
        Index("ix_agent_hub_runs_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=True)
    request: Mapped[str] = mapped_column(Text)
    mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    routing_decision: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
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
            "kind IN ('workflow', 'agent', 'main_agent', 'skill', 'mcp', 'memory', 'hermes', 'audit', 'log', 'setting', 'channel', 'openclaw', 'openclaw_session', 'schedule', 'evolution')",
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
