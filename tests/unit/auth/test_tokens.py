import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from agent_hub.auth.models import Role
from agent_hub.auth.tokens import AccessTokenService, InvalidTokenError, TokenBackendError

NOW = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)
KEY = b"k" * 32


def _asymmetric_encodings() -> list[bytes]:
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent-hub-test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public_key)
        .serial_number(1)
        .not_valid_before(NOW - timedelta(minutes=1))
        .not_valid_after(NOW + timedelta(days=1))
        .sign(private_key, algorithm=None)
    )
    return [
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
        certificate.public_bytes(serialization.Encoding.PEM),
        private_key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
        certificate.public_bytes(serialization.Encoding.DER),
        public_key.public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        ),
    ]


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


def _assert_production_frames_do_not_contain(error: BaseException, secret: object) -> None:
    rendered_secret = repr(secret)
    traceback = error.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_hub/" in filename:
            assert all(
                rendered_secret not in repr(value)
                for value in traceback.tb_frame.f_locals.values()
            )
        traceback = traceback.tb_next
    assert error.__cause__ is None
    assert error.__context__ is None


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


@pytest.mark.parametrize("key", [b"r" * 31, b"r" * 65])
def test_out_of_range_raw_signing_key_is_rejected(key: bytes) -> None:
    with pytest.raises(ValueError, match="32-64 bytes") as captured:
        AccessTokenService(key)
    _assert_production_frames_do_not_contain(captured.value, key)


@pytest.mark.parametrize("key", [b"r" * 32, b"r" * 64])
def test_raw_symmetric_keys_at_supported_boundaries_are_accepted(key: bytes) -> None:
    service = AccessTokenService(key, clock=lambda: NOW)

    assert service.decode(service.encode(uuid4(), uuid4(), Role.VIEWER)).role is Role.VIEWER


@pytest.mark.parametrize(
    "key",
    [
        "base64url:" + base64.urlsafe_b64encode(b"b" * 32).decode("ascii").rstrip("="),
        "hex:" + (b"h" * 64).hex(),
    ],
)
def test_canonical_text_symmetric_keys_are_accepted(key: str) -> None:
    service = AccessTokenService(key, clock=lambda: NOW)

    assert service.decode(service.encode(uuid4(), uuid4(), Role.VIEWER)).role is Role.VIEWER


@pytest.mark.parametrize(
    "key",
    [
        "x" * 32,
        '{"kty":"RSA","n":"fake-material-long-enough"}',
        "-----BEGIN PRIVATE KEY-----\nnot-real-but-long-enough",
        "sk-ssh-ed25519@openssh.com AAAAC3NzaC1lZDI1NTE5AAAAfake",
        "base64url:" + base64.urlsafe_b64encode(b"b" * 32).decode("ascii"),
        "base64url:" + base64.urlsafe_b64encode(b"b" * 31).decode("ascii").rstrip("="),
        "base64url:" + base64.urlsafe_b64encode(b"b" * 65).decode("ascii").rstrip("="),
        "hex:" + "AA" * 32,
        "hex:" + (b"h" * 31).hex(),
        "hex:" + (b"h" * 65).hex(),
    ],
)
def test_noncanonical_or_out_of_range_text_keys_are_rejected_without_frames(
    key: str,
) -> None:
    with pytest.raises(ValueError) as captured:
        AccessTokenService(key)

    _assert_production_frames_do_not_contain(captured.value, key)


@pytest.mark.parametrize(
    "key",
    [
        b'\xef\xbb\xbf {"kty":"RSA","n":"fake-material-long-enough"}',
        b'{"keys":[{"kty":"RSA","n":"fake-material"}]}',
        b"sk-ssh-ed25519@openssh.com AAAAC3NzaC1lZDI1NTE5AAAAfake",
        b"sk-ecdsa-sha2-nistp256@openssh.com AAAAE2VjZHNhLXNoYTItfake",
        b"---- BEGIN SSH2 PUBLIC KEY ----\nComment: fake\nAAAAfake",
        *_asymmetric_encodings(),
    ],
)
def test_structured_or_asymmetric_bytes_are_rejected_without_key_frames(
    key: bytes,
) -> None:
    with pytest.raises(ValueError) as captured:
        AccessTokenService(key)

    _assert_production_frames_do_not_contain(captured.value, key)


def test_canonical_text_cannot_wrap_structured_key_material() -> None:
    der_public_key = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key = (
        "base64url:"
        + base64.urlsafe_b64encode(der_public_key).decode("ascii").rstrip("=")
    )

    with pytest.raises(ValueError, match="symmetric") as captured:
        AccessTokenService(key)

    _assert_production_frames_do_not_contain(captured.value, key)


@pytest.mark.parametrize(
    "key",
    [
        b"-----BEGIN PRIVATE KEY-----\nnot-real-but-long-enough",
        b"ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCfake",
        b'{"kty":"RSA","n":"fake-material-long-enough"}',
    ],
)
def test_hs256_rejects_asymmetric_key_material_without_retaining_it(key: bytes) -> None:
    with pytest.raises(ValueError, match="symmetric") as captured:
        AccessTokenService(key)

    _assert_production_frames_do_not_contain(captured.value, key)


def test_actual_pyjwt_encode_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = b"sensitive-signing-key-material!!"
    service = AccessTokenService(key, clock=lambda: NOW)

    def fail_encode(*args: object, **kwargs: object) -> str:
        raise jwt.InvalidKeyError("forced invalid key")

    monkeypatch.setattr(jwt, "encode", fail_encode)
    with pytest.raises(TokenBackendError, match="token backend failed") as captured:
        service.encode(uuid4(), uuid4(), Role.VIEWER)

    _assert_production_frames_do_not_contain(captured.value, key)


def test_unknown_jwt_runtime_propagates_without_retaining_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = b"sensitive-signing-key-material!!"
    service = AccessTokenService(key, clock=lambda: NOW)

    def fail_encode(*args: object, **kwargs: object) -> str:
        raise RuntimeError("forced unknown token failure")

    monkeypatch.setattr(jwt, "encode", fail_encode)
    with pytest.raises(RuntimeError, match="forced unknown token failure") as captured:
        service.encode(uuid4(), uuid4(), Role.VIEWER)

    _assert_production_frames_do_not_contain(captured.value, key)


def test_unknown_jwt_decode_runtime_propagates_without_retaining_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "sensitive.header.payload.signature"
    service = AccessTokenService(KEY, clock=lambda: NOW)

    def fail_decode(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("forced unknown decode failure")

    monkeypatch.setattr(jwt, "decode", fail_decode)
    with pytest.raises(RuntimeError, match="forced unknown decode failure") as captured:
        service.decode(token)

    _assert_production_frames_do_not_contain(captured.value, token)


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


def test_decode_rejects_signed_extra_claim_without_retaining_token() -> None:
    payload = _valid_payload()
    payload["admin"] = True
    token = _encode_payload(payload)

    with pytest.raises(InvalidTokenError, match="invalid access token") as captured:
        _service().decode(token)

    _assert_production_frames_do_not_contain(captured.value, token)


@pytest.mark.parametrize(
    "constructor",
    [
        lambda key: AccessTokenService(key, algorithm="HS512"),
        lambda key: AccessTokenService(key, leeway_seconds=61),
        lambda key: AccessTokenService(key, issuer=""),
    ],
)
def test_constructor_errors_do_not_retain_signing_key(constructor: object) -> None:
    key = b"sensitive-signing-key-material!!"

    with pytest.raises(ValueError) as captured:
        constructor(key)  # type: ignore[operator]

    _assert_production_frames_do_not_contain(captured.value, key)


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


def test_decode_error_frames_do_not_retain_the_token() -> None:
    token = _encode_payload(_valid_payload(), b"x" * 32)

    with pytest.raises(InvalidTokenError) as captured:
        _service().decode(token)

    _assert_production_frames_do_not_contain(captured.value, token)
