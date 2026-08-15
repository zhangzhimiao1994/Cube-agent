from __future__ import annotations

from enum import StrEnum

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FeishuTransport(StrEnum):
    WEBHOOK = "webhook"
    WEBSOCKET = "websocket"
    BOTH = "both"


class FeishuSettings(BaseSettings):
    """Feishu receiver settings.

    The prefix is intentionally separate from the application-wide settings so the
    connector process can be configured independently.
    """

    model_config = SettingsConfigDict(
        env_prefix="FEISHU_",
        env_file=".env",
        hide_input_in_errors=True,
        validate_default=True,
    )

    app_id: str = Field(default="cli_a_test", min_length=1, max_length=128)
    app_secret: SecretStr = SecretStr("development-only")
    verification_token: SecretStr = SecretStr("test-token")
    encrypt_key: SecretStr = SecretStr("test-encrypt-key")
    bot_open_id: str | None = Field(default=None, max_length=256)
    allowed_tenant_keys: frozenset[str] = Field(default=frozenset(), max_length=256)
    transport: FeishuTransport = FeishuTransport.WEBHOOK
    webhook_path: str = "/channels/feishu/events"
    command_aliases: str = Field(default="", max_length=4096)
    timestamp_tolerance_seconds: int = Field(default=300, ge=1, le=3600)
    websocket_reconnect_min_seconds: float = Field(default=0.1, ge=0.0, le=60.0)
    websocket_reconnect_max_seconds: float = Field(default=5.0, ge=0.0, le=300.0)

    @field_validator("webhook_path")
    @classmethod
    def validate_webhook_path(cls, value: str) -> str:
        if not value.startswith("/") or "//" in value:
            raise ValueError("webhook_path must be an absolute single path")
        return value

    def enabled_transports(self) -> frozenset[FeishuTransport]:
        if self.transport is FeishuTransport.BOTH:
            return frozenset({FeishuTransport.WEBHOOK, FeishuTransport.WEBSOCKET})
        return frozenset({self.transport})

    def app_secret_value(self) -> str:
        return self.app_secret.get_secret_value()

    def verification_token_value(self) -> str:
        return self.verification_token.get_secret_value()

    def encrypt_key_value(self) -> str:
        return self.encrypt_key.get_secret_value()


__all__ = ["FeishuSettings", "FeishuTransport"]
