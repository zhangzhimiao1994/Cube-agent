import base64
import secrets
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_hub.auth.models import Role
from agent_hub.auth.tokens import AccessTokenService
from agent_hub.settings import Settings


def _canonical_key(raw: bytes) -> str:
    return "base64url:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_default_development_settings_cannot_sign_tokens() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    key = settings.jwt_signing_key_value()

    with pytest.raises(ValueError):
        AccessTokenService(key)

    assert key not in repr(settings)


def test_production_rejects_the_default_placeholder() -> None:
    placeholder = "development-only-change-me"

    with pytest.raises(ValidationError) as captured:
        Settings(environment="production", _env_file=None)  # type: ignore[call-arg]

    assert placeholder not in str(captured.value)
    assert placeholder not in repr(captured.value)


@pytest.mark.parametrize(
    ("environment", "key"),
    [
        ("production", ""),
        ("production", "development-only-change-me"),
        (
            "production",
            "base64url:YWdlbnQtaHViLWRldmVsb3BtZW50LWtleS0wMDAwMDE",
        ),
        ("staging", "base64url:" + "A" * 43),
        ("production", "hex:" + "00" * 32),
    ],
)
def test_nonlocal_environments_reject_known_insecure_keys_without_echoing_them(
    environment: str, key: str
) -> None:
    with pytest.raises(ValidationError) as captured:
        Settings(
            environment=environment,
            jwt_signing_key=key,
            _env_file=None,  # type: ignore[call-arg]
        )

    if key:
        assert key not in str(captured.value)
        assert key not in repr(captured.value)


def test_production_accepts_a_locally_generated_canonical_key() -> None:
    key = _canonical_key(secrets.token_bytes(32))

    settings = Settings(
        environment="production",
        jwt_signing_key=key,
        _env_file=None,  # type: ignore[call-arg]
    )
    service = AccessTokenService(settings.jwt_signing_key_value())
    token = service.encode(uuid4(), uuid4(), Role.VIEWER)

    assert token
    assert key not in repr(settings)
