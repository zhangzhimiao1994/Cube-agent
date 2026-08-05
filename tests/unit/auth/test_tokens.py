from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from agent_hub.auth.models import Role
from agent_hub.auth.tokens import AccessTokenService, InvalidTokenError

NOW = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)
KEY = b"k" * 32


def _service(*, now: datetime = NOW, key: bytes = KEY) -> AccessTokenService:
    return AccessTokenService(key, clock=lambda: now)


def _valid_payload() -> dict[str, object]:
    return {
        "sub": str(uuid4()),
        "tenant_id": str(uuid4()),
        "role": "viewer",
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(minutes=15)).timestamp()),
        "iss": "agent-hub",
        "aud": "agent-hub",
        "jti": str(uuid4()),
    }


def _encode_payload(payload: dict[str, object], key: bytes = KEY) -> str:
    return jwt.encode(payload, key, algorithm="HS256")


def test_access_token_round_trip_has_exact_claims() -> None:
    service = _service()
    user_id = uuid4()
    tenant_id = uuid4()

    token = service.encode(user_id, tenant_id, Role.OPERATOR, ttl_seconds=900)
    decoded = service.decode(token)
    raw = jwt.decode(
        token,
        KEY,
        algorithms=["HS256"],
        audience="agent-hub",
        issuer="agent-hub",
        options={"verify_exp": False, "verify_iat": False},
    )

    assert set(raw) == {"sub", "tenant_id", "role", "iat", "exp", "iss", "aud", "jti"}
    assert decoded.user_id == user_id
    assert decoded.tenant_id == tenant_id
    assert decoded.role is Role.OPERATOR
    assert decoded.issued_at == NOW
    assert decoded.expires_at == NOW + timedelta(seconds=900)
    assert token not in repr(decoded)


@pytest.mark.parametrize("ttl", [0, 59, 3601, True])
def test_encode_rejects_ttl_outside_the_supported_range(ttl: object) -> None:
    with pytest.raises(ValueError, match="TTL"):
        _service().encode(uuid4(), uuid4(), Role.VIEWER, ttl_seconds=ttl)  # type: ignore[arg-type]


def test_short_signing_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        AccessTokenService(b"development-only-change-me")


@pytest.mark.parametrize(
    "audience",
    [["agent-hub"], ["other", "agent-hub"]],
)
def test_decode_rejects_audience_lists_with_one_safe_failure(
    audience: list[str],
) -> None:
    payload = _valid_payload()
    payload["aud"] = audience
    token = _encode_payload(payload)

    with pytest.raises(InvalidTokenError, match="invalid access token") as captured:
        _service().decode(token)

    assert token not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("lifetime_seconds", "valid"),
    [(59, False), (60, True), (3600, True), (3601, False), (365 * 24 * 60 * 60, False)],
)
def test_decode_enforces_the_same_lifetime_bounds_as_encode(
    lifetime_seconds: int, valid: bool
) -> None:
    payload = _valid_payload()
    payload["exp"] = int(NOW.timestamp()) + lifetime_seconds
    token = _encode_payload(payload)

    if valid:
        assert _service().decode(token).expires_at == NOW + timedelta(
            seconds=lifetime_seconds
        )
    else:
        with pytest.raises(InvalidTokenError, match="invalid access token") as captured:
            _service().decode(token)
        assert token not in str(captured.value)
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.__setitem__("exp", int((NOW - timedelta(seconds=1)).timestamp())),
        lambda payload: payload.__setitem__("iat", int((NOW + timedelta(minutes=2)).timestamp())),
        lambda payload: payload.__setitem__("role", "owner"),
        lambda payload: payload.__setitem__("sub", "not-a-uuid"),
        lambda payload: payload.__setitem__("tenant_id", "not-a-uuid"),
        lambda payload: payload.__setitem__("sub", uuid4().hex),
        lambda payload: payload.pop("tenant_id"),
        lambda payload: payload.__setitem__("iat", "now"),
    ],
)
def test_invalid_claims_have_one_safe_failure(mutation: object) -> None:
    payload = _valid_payload()
    mutation(payload)  # type: ignore[operator]
    token = _encode_payload(payload)

    with pytest.raises(InvalidTokenError, match="invalid access token") as captured:
        _service().decode(token)

    assert token not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_wrong_signature_and_none_algorithm_have_one_safe_failure() -> None:
    wrong_signature = _encode_payload(_valid_payload(), b"x" * 32)
    none_algorithm = jwt.encode(_valid_payload(), key="", algorithm="none")

    for token in (wrong_signature, none_algorithm):
        with pytest.raises(InvalidTokenError, match="invalid access token") as captured:
            _service().decode(token)
        assert token not in str(captured.value)
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
