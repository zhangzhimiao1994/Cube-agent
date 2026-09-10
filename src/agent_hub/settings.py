"""Application settings."""

import base64
import binascii
import re
from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_hub.security.network import canonical_ip


class Settings(BaseSettings):
    """Settings loaded from environment variables and a local .env file."""

    model_config = SettingsConfigDict(
        env_prefix="AGENT_HUB_",
        env_file=".env",
        hide_input_in_errors=True,
        validate_default=True,
    )

    _LOCAL_ENVIRONMENTS: ClassVar[frozenset[str]] = frozenset(
        {"development", "test"}
    )
    _INSECURE_JWT_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "development-only-change-me",
            "base64url:YWdlbnQtaHViLWRldmVsb3BtZW50LWtleS0wMDAwMDE",
            "base64url:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "hex:0000000000000000000000000000000000000000000000000000000000000000",
        }
    )

    environment: str = "development"
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://agent_hub:agent_hub@localhost/agent_hub"
    )
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    jwt_signing_key: SecretStr = SecretStr(
        "base64url:YWdlbnQtaHViLWRldmVsb3BtZW50LWtleS0wMDAwMDE"
    )
    master_key: SecretStr = SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    trusted_proxy_ips: frozenset[str] = Field(default=frozenset(), max_length=32)
    log_level: str = Field(default="WARNING", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    runtime_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    runtime_token_budget: int = Field(default=1_000_000, ge=1, le=10_000_000)
    web_dir: Path | None = None
    skill_store_dir: Path = Path("/var/lib/agent-hub/skills")
    plugin_package_store_dir: Path = Path("/var/lib/agent-hub/plugin-packages")
    plugin_package_subprocess_runner_enabled: bool = False
    plugin_package_subprocess_adapter_ids: frozenset[str] = Field(
        default=frozenset(),
        max_length=32,
    )
    plugin_package_subprocess_isolation_backend: Literal[
        "disabled",
        "bubblewrap",
    ] = "disabled"
    plugin_package_subprocess_bubblewrap_executable: Path | None = None
    plugin_package_subprocess_timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    plugin_package_subprocess_max_stdin_bytes: int = Field(
        default=262_144,
        ge=1,
        le=1_048_576,
    )
    plugin_package_subprocess_max_stdout_bytes: int = Field(
        default=262_144,
        ge=1,
        le=1_048_576,
    )
    attachment_store_dir: Path = Path("/var/lib/agent-hub/attachments")
    generated_artifact_dir: Path = Path("/var/lib/agent-hub/generated")
    project_workspace_dir: Path = Path("/var/lib/agent-hub/workspaces")
    litellm_health_url: str | None = None
    bootstrap_tenant_id: UUID = UUID("00000000-0000-4000-8000-000000000001")
    bootstrap_tenant_slug: str = Field(
        default="default", min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$"
    )
    bootstrap_tenant_name: str = Field(default="Default", min_length=1, max_length=200)

    @field_validator("jwt_signing_key", mode="after")
    @classmethod
    def reject_insecure_nonlocal_jwt_key(
        cls, value: SecretStr, info: ValidationInfo
    ) -> SecretStr:
        environment = str(info.data.get("environment", "development")).strip().lower()
        key = value.get_secret_value()
        normalized_key = key.strip()
        if environment not in cls._LOCAL_ENVIRONMENTS and (
            not normalized_key or normalized_key in cls._INSECURE_JWT_KEYS
        ):
            raise ValueError("a securely generated JWT signing key is required")
        return value

    def jwt_signing_key_value(self) -> str:
        """Return the configured value for strict validation by AccessTokenService."""

        return self.jwt_signing_key.get_secret_value()

    def database_url_value(self) -> str:
        """Return the database URL only at the connection boundary."""

        return self.database_url.get_secret_value()

    def redis_url_value(self) -> str:
        """Return the Redis URL only at the connection boundary."""

        return self.redis_url.get_secret_value()

    def master_key_bytes(self) -> bytes:
        """Return the configured AES-256 master key decoded from base64."""

        raw = self.master_key.get_secret_value().strip()
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("master_key must be canonical base64 for 32 bytes") from None
        if len(decoded) != 32:
            raise ValueError("master_key must decode to exactly 32 bytes")
        return decoded

    @field_validator("trusted_proxy_ips", mode="before")
    @classmethod
    def validate_trusted_proxy_ips(cls, values: object) -> frozenset[str]:
        if not isinstance(values, (list, tuple, set, frozenset)):
            raise ValueError(  # noqa: TRY004 -- Pydantic converts this to ValidationError.
                "trusted proxies must be a collection of IP addresses"
            )
        if len(values) > 32:
            raise ValueError("at most 32 trusted proxies may be configured")
        canonical: set[str] = set()
        for value in values:
            if not isinstance(value, str) or "%" in value:
                raise ValueError("trusted proxy must be an IP address")
            normalized = canonical_ip(value)
            if normalized is None:
                raise ValueError("trusted proxy must be an IP address")
            canonical.add(normalized)
        return frozenset(canonical)

    @field_validator("plugin_package_subprocess_adapter_ids", mode="before")
    @classmethod
    def validate_plugin_package_subprocess_adapter_ids(
        cls, values: object
    ) -> frozenset[str]:
        if values is None:
            return frozenset()
        if not isinstance(values, (list, tuple, set, frozenset)):
            raise ValueError(  # noqa: TRY004 - Pydantic converts this to ValidationError.
                "plugin package adapter ids must be a collection of adapter ids"
            )
        if len(values) > 32:
            raise ValueError("at most 32 plugin package adapter ids may be configured")
        adapter_ids: set[str] = set()
        pattern = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
        reserved = {"http_json"}
        for value in values:
            if (
                not isinstance(value, str)
                or value in reserved
                or pattern.fullmatch(value) is None
            ):
                raise ValueError("plugin package adapter id is invalid")
            adapter_ids.add(value)
        return frozenset(adapter_ids)

    @field_validator("plugin_package_subprocess_bubblewrap_executable", mode="after")
    @classmethod
    def validate_plugin_package_subprocess_bubblewrap_executable(
        cls, value: Path | None
    ) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("plugin package bubblewrap executable must be absolute")
        return value

    @field_validator("bootstrap_tenant_name", mode="after")
    @classmethod
    def validate_bootstrap_tenant_name(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("bootstrap tenant name must be unpadded and non-blank")
        return value

    @field_validator("litellm_health_url", mode="after")
    @classmethod
    def validate_litellm_health_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("litellm_health_url must start with http:// or https://")
        return normalized


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
