"""Application settings."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables and a local .env file."""

    model_config = SettingsConfigDict(env_prefix="AGENT_HUB_", env_file=".env")

    environment: str = "development"
    database_url: str = "postgresql+asyncpg://agent_hub:agent_hub@localhost/agent_hub"
    redis_url: str = "redis://localhost:6379/0"
    jwt_signing_key: SecretStr = SecretStr(
        "base64url:YWdlbnQtaHViLWRldmVsb3BtZW50LWtleS0wMDAwMDE"
    )
    master_key: SecretStr = SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
