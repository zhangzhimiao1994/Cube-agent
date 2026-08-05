from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
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
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("agent_hub_tenants.id"))
    username: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    feishu_open_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[str] = mapped_column(String(32))


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
