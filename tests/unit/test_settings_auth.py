import base64
import secrets
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from agent_hub.auth.models import Role
from agent_hub.auth.tokens import AccessTokenService
from agent_hub.settings import Settings


def _canonical_key(raw: bytes) -> str:
    return "base64url:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_default_development_settings_cannot_sign_tokens() -> None:
    settings = Settings.model_validate({})
    key = settings.jwt_signing_key_value()

    with pytest.raises(ValueError):
        AccessTokenService(key)

    assert key not in repr(settings)


def test_production_rejects_the_default_placeholder() -> None:
    placeholder = "development-only-change-me"

    with pytest.raises(ValidationError) as captured:
        Settings.model_validate({"environment": "production"})

    assert placeholder not in str(captured.value)
    assert placeholder not in repr(captured.value)


@pytest.mark.parametrize(
    ("environment", "key"),
    [
        (" production ", SecretStr(" \t ")),
        ("PRODUCTION", SecretStr(" development-only-change-me ")),
        (
            " StAgInG ",
            SecretStr(
                "\tbase64url:YWdlbnQtaHViLWRldmVsb3BtZW50LWtleS0wMDAwMDE\n"
            ),
        ),
        ("staging", SecretStr(" base64url:" + "A" * 43 + " ")),
        ("Production", SecretStr("\nhex:" + "00" * 32 + "\t")),
    ],
)
def test_nonlocal_environments_reject_known_insecure_keys_without_echoing_them(
    environment: str, key: SecretStr
) -> None:
    raw_key = key.get_secret_value()
    with pytest.raises(ValidationError) as captured:
        Settings.model_validate(
            {"environment": environment, "jwt_signing_key": key}
        )

    assert raw_key not in str(captured.value)
    assert raw_key not in repr(captured.value)
    if raw_key.strip():
        assert raw_key.strip() not in str(captured.value)
        assert raw_key.strip() not in repr(captured.value)


def test_production_accepts_a_locally_generated_canonical_key() -> None:
    key = _canonical_key(secrets.token_bytes(32))

    settings = Settings.model_validate(
        {
            "environment": "production",
            "jwt_signing_key": SecretStr(key),
        }
    )
    service = AccessTokenService(settings.jwt_signing_key_value())
    token = service.encode(uuid4(), uuid4(), Role.VIEWER)

    assert token
    assert key not in repr(settings)


def test_settings_preserve_whitespace_for_downstream_canonical_validation() -> None:
    canonical_key = _canonical_key(secrets.token_bytes(32))
    configured_key = f" {canonical_key} "
    settings = Settings.model_validate(
        {
            "environment": "development",
            "jwt_signing_key": SecretStr(configured_key),
        }
    )

    assert settings.jwt_signing_key_value() == configured_key
    with pytest.raises(ValueError):
        AccessTokenService(settings.jwt_signing_key_value())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bootstrap_tenant_slug", "Bad Slug"),
        ("bootstrap_tenant_name", ""),
        ("trusted_proxy_ips", ["not-an-ip"]),
        ("trusted_proxy_ips", "not-a-collection"),
    ],
)
def test_network_and_bootstrap_settings_reject_invalid_values(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: value})


def test_trusted_proxy_limit_accepts_32_and_rejects_33() -> None:
    accepted = [f"192.0.2.{index}" for index in range(1, 33)]
    settings = Settings.model_validate({"trusted_proxy_ips": accepted})

    assert len(settings.trusted_proxy_ips) == 32
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {"trusted_proxy_ips": [*accepted, "198.51.100.1"]}
        )


@pytest.mark.parametrize(
    "name", [" ", "\tName", "Name\n", "x" * 201]
)
def test_bootstrap_tenant_name_rejects_blank_padding_and_overflow(name: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"bootstrap_tenant_name": name})


@pytest.mark.parametrize("name", ["x", "x" * 200])
def test_bootstrap_tenant_name_accepts_length_boundaries(name: str) -> None:
    assert Settings.model_validate({"bootstrap_tenant_name": name}).bootstrap_tenant_name == name


def test_settings_validation_does_not_echo_jwt_key() -> None:
    key = "base64url:" + "SECRET_JWT_VALUE" * 4
    with pytest.raises(ValidationError) as captured:
        Settings.model_validate(
            {
                "environment": "production",
                "jwt_signing_key": SecretStr(key),
                "trusted_proxy_ips": ["invalid"],
            }
        )

    assert key not in str(captured.value)
    assert key not in repr(captured.value)
