"""Explicit safe wire models for the foundation API."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.config.schema import PlatformConfig


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetupRequest(APIModel):
    code: SecretStr = Field(min_length=43, max_length=43, repr=False)
    username: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]{2,63}$",
    )
    password: SecretStr = Field(min_length=12, max_length=1024, repr=False)


class LoginRequest(APIModel):
    tenant_id: UUID
    username: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]{2,63}$",
    )
    password: SecretStr = Field(min_length=12, max_length=1024, repr=False)


class PrincipalResponse(APIModel):
    user_id: UUID
    tenant_id: UUID
    role: Role

    @classmethod
    def from_principal(cls, principal: AuthenticatedPrincipal) -> "PrincipalResponse":
        return cls(
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            role=principal.role,
        )


class TokenResponse(APIModel):
    access_token: str
    token_type: str = "bearer"
    principal: PrincipalResponse


class ValidationResponse(APIModel):
    valid: Literal[True] = True


class ConfigRevisionResponse(APIModel):
    id: UUID
    version: int
    status: ConfigStatus
    document: PlatformConfig
    created_by: UUID | None
    created_at: datetime

    @classmethod
    def from_revision(cls, revision: ConfigRevision) -> "ConfigRevisionResponse":
        return cls(
            id=revision.id,
            version=revision.version,
            status=revision.status,
            document=PlatformConfig.model_validate(revision.document),
            created_by=revision.created_by,
            created_at=revision.created_at,
        )


class PublishedRevisionResponse(ConfigRevisionResponse):
    notification_status: Literal["sent", "failed"] = "sent"

    @classmethod
    def from_published(
        cls, revision: ConfigRevision, notification_status: Literal["sent", "failed"] = "sent"
    ) -> "PublishedRevisionResponse":
        base = ConfigRevisionResponse.from_revision(revision)
        return cls(**base.model_dump(), notification_status=notification_status)


class ConfigHistoryResponse(APIModel):
    items: list[ConfigRevisionResponse]
    limit: int
    offset: int


class DiffAdded(APIModel):
    path: str
    value: object


class DiffRemoved(APIModel):
    path: str
    old_value: object


class DiffChanged(APIModel):
    path: str
    from_value: object = Field(validation_alias="from", serialization_alias="from")
    to: object


class ConfigDiffResponse(APIModel):
    from_version: int
    to_version: int
    added: list[DiffAdded]
    removed: list[DiffRemoved]
    changed: list[DiffChanged]
