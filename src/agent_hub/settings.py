"""Application settings."""

from functools import lru_cache
from typing import ClassVar

from pydantic import SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    database_url: str = "postgresql+asyncpg://agent_hub:agent_hub@localhost/agent_hub"
    redis_url: str = "redis://localhost:6379/0"
    jwt_signing_key: SecretStr = SecretStr("development-only-change-me")
    master_key: SecretStr = SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    @field_validator("jwt_signing_key", mode="after")
    @classmethod
    def reject_insecure_nonlocal_jwt_key(
        cls, value: SecretStr, info: ValidationInfo
    ) -> SecretStr:
        environment = str(info.data.get("environment", "development")).strip().lower()
        key = value.get_secret_value()
        if environment not in cls._LOCAL_ENVIRONMENTS and (
            not key.strip() or key in cls._INSECURE_JWT_KEYS
        ):
            raise ValueError("a securely generated JWT signing key is required")
        return value

    def jwt_signing_key_value(self) -> str:
        """Return the configured value for strict validation by AccessTokenService."""

        return self.jwt_signing_key.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
