"""Strict HS256 access-token issuing and validation.

Signing keys are either 32-64 raw bytes or canonical ``base64url:``/``hex:`` text.
"""

import base64
import binascii
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, NoReturn
from uuid import UUID, uuid4

import jwt
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization

from agent_hub.auth.models import AuthenticatedPrincipal, Role

_ALLOWED_ALGORITHMS: Final[tuple[str, ...]] = ("HS256",)
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 3600
_MAX_TOKEN_CHARS = 8192
_REQUIRED_CLAIMS: Final[tuple[str, ...]] = (
    "sub",
    "tenant_id",
    "role",
    "iat",
    "exp",
    "iss",
    "aud",
    "jti",
)
_TOKEN_BACKEND_FAILURE: Final[object] = object()
_BASE64URL_KEY = re.compile(r"[A-Za-z0-9_-]+\Z")
_HEX_KEY = re.compile(r"[0-9a-f]+\Z")
_MIN_KEY_BYTES = 32
_MAX_KEY_BYTES = 64


class InvalidTokenError(RuntimeError):
    pass


class TokenBackendError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DecodedAccessToken:
    user_id: UUID
    tenant_id: UUID
    role: Role
    issued_at: datetime
    expires_at: datetime
    token_id: UUID

    @property
    def principal(self) -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(self.user_id, self.tenant_id, self.role)


class AccessTokenService:
    """HS256 boundary; a future asymmetric implementation should be a separate class."""

    def __init__(
        self,
        signing_key: bytes | str,
        *,
        algorithm: str = "HS256",
        issuer: str = "agent-hub",
        audience: str = "agent-hub",
        clock: Callable[[], datetime] | None = None,
        leeway_seconds: int = 0,
    ) -> None:
        configuration_error = _configuration_error(
            algorithm, issuer, audience, leeway_seconds
        )
        key, key_error = _try_validated_key(signing_key)
        del signing_key
        if configuration_error is not None:
            del key, key_error
            _raise_token_configuration_error(configuration_error)
        if key_error is not None:
            del key, configuration_error
            _raise_token_configuration_error(key_error)
        assert key is not None
        self._key = key
        self._algorithm = algorithm
        self._issuer = issuer
        self._audience = audience
        self._clock = clock or (lambda: datetime.now(UTC))
        self._leeway = timedelta(seconds=leeway_seconds)

    def __repr__(self) -> str:
        return (
            f"AccessTokenService(algorithm={getattr(self, '_algorithm', '<invalid>')!r}, "
            f"issuer={getattr(self, '_issuer', '<invalid>')!r}, "
            f"audience={getattr(self, '_audience', '<invalid>')!r}, key=<redacted>)"
        )

    def encode(
        self,
        user_id: UUID,
        tenant_id: UUID,
        role: Role,
        *,
        ttl_seconds: int = 900,
    ) -> str:
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or not _MIN_TTL_SECONDS <= ttl_seconds <= _MAX_TTL_SECONDS
        ):
            raise ValueError("access token TTL must be 60-3600 seconds")
        if (
            not isinstance(user_id, UUID)
            or not isinstance(tenant_id, UUID)
            or not isinstance(role, Role)
        ):
            raise TypeError("access token subject, tenant, and role are invalid")
        now = _utc_clock_value(self._clock())
        expires_at = now + timedelta(seconds=ttl_seconds)
        payload: dict[str, object] = {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "role": role.value,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "iss": self._issuer,
            "aud": self._audience,
            "jti": str(uuid4()),
        }
        try:
            outcome = _try_jwt_encode(payload, self._key, self._algorithm)
        finally:
            del payload
        if outcome is _TOKEN_BACKEND_FAILURE:
            del outcome
            _raise_token_backend_error()
        assert isinstance(outcome, str)
        return outcome

    def decode(self, token: str) -> DecodedAccessToken:
        decoded = None
        payload = None
        try:
            payload = self._try_decode(token)
            if payload is None:
                _raise_invalid_token()
            decoded = self._try_materialize(payload)
            if decoded is None:
                _raise_invalid_token()
            return decoded
        finally:
            del decoded, payload, token

    def _try_decode(self, token: str) -> Mapping[str, object] | None:
        payload = None
        try:
            if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_CHARS:
                return None
            try:
                payload = jwt.decode(
                    token,
                    self._key,
                    algorithms=list(_ALLOWED_ALGORITHMS),
                    audience=self._audience,
                    issuer=self._issuer,
                    options={
                        "require": list(_REQUIRED_CLAIMS),
                        "strict_aud": True,
                        "verify_exp": False,
                        "verify_iat": False,
                    },
                )
            except (jwt.PyJWTError, TypeError, ValueError):
                return None
            return payload
        finally:
            del payload, token

    def _try_materialize(self, payload: Mapping[str, object]) -> DecodedAccessToken | None:
        try:
            try:
                if set(payload) != set(_REQUIRED_CLAIMS):
                    return None
                issued_timestamp = payload["iat"]
                expires_timestamp = payload["exp"]
                if (
                    not isinstance(issued_timestamp, int)
                    or isinstance(issued_timestamp, bool)
                    or not isinstance(expires_timestamp, int)
                    or isinstance(expires_timestamp, bool)
                ):
                    return None
                audience = payload["aud"]
                if not isinstance(audience, str) or audience != self._audience:
                    return None
                lifetime_seconds = expires_timestamp - issued_timestamp
                if not _MIN_TTL_SECONDS <= lifetime_seconds <= _MAX_TTL_SECONDS:
                    return None
                issued_at = datetime.fromtimestamp(issued_timestamp, UTC)
                expires_at = datetime.fromtimestamp(expires_timestamp, UTC)
                now = _utc_clock_value(self._clock())
                if issued_at > now + self._leeway or expires_at <= now - self._leeway:
                    return None
                if expires_at <= issued_at:
                    return None
                return DecodedAccessToken(
                    user_id=_canonical_uuid_claim(payload, "sub"),
                    tenant_id=_canonical_uuid_claim(payload, "tenant_id"),
                    role=Role(_claim_string(payload, "role")),
                    issued_at=issued_at,
                    expires_at=expires_at,
                    token_id=_canonical_uuid_claim(payload, "jti"),
                )
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                return None
        finally:
            del payload


def _try_validated_key(signing_key: object) -> tuple[bytes | None, str | None]:
    key = None
    try:
        if isinstance(signing_key, str):
            key = _try_decode_text_key(signing_key)
            if key is None:
                return None, "text signing keys must use canonical base64url: or hex: format"
        elif isinstance(signing_key, bytes):
            key = signing_key
        else:
            return None, "access token signing key must be raw bytes or canonical text"
        if not _MIN_KEY_BYTES <= len(key) <= _MAX_KEY_BYTES:
            return None, "access token signing key must decode to 32-64 bytes"
        if _is_structured_key_material(key):
            return None, "HS256 signing key must be symmetric key material"
        return key, None
    finally:
        del key, signing_key


def _try_decode_text_key(signing_key: str) -> bytes | None:
    encoded = None
    key = None
    try:
        if signing_key.startswith("base64url:"):
            encoded = signing_key.removeprefix("base64url:")
            if not encoded or _BASE64URL_KEY.fullmatch(encoded) is None:
                return None
            padding = "=" * (-len(encoded) % 4)
            try:
                key = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
            except (binascii.Error, ValueError):
                return None
            if base64.urlsafe_b64encode(key).decode("ascii").rstrip("=") != encoded:
                return None
            return key
        if signing_key.startswith("hex:"):
            encoded = signing_key.removeprefix("hex:")
            if not encoded or len(encoded) % 2 or _HEX_KEY.fullmatch(encoded) is None:
                return None
            key = bytes.fromhex(encoded)
            return key
        return None
    finally:
        del encoded, key, signing_key


def _is_structured_key_material(key: bytes) -> bool:
    candidate = None
    parser = None
    text = None
    try:
        try:
            text = key.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            candidate = text.lstrip("\ufeff \t\r\n").lower()
            if candidate.startswith(("{", "[")):
                return True
            markers = (
                "-----begin ",
                "---- begin ssh2 public key ----",
                "ssh-rsa ",
                "ssh-ed25519 ",
                "ecdsa-sha2-",
                "sk-ssh-",
                "sk-ecdsa-",
            )
            if any(marker in candidate for marker in markers):
                return True
        parsers: tuple[Callable[[bytes], object], ...] = (
            serialization.load_pem_public_key,
            _load_pem_private_key,
            serialization.load_der_public_key,
            _load_der_private_key,
            serialization.load_ssh_public_key,
            x509.load_pem_x509_certificate,
            x509.load_der_x509_certificate,
        )
        for parser in parsers:
            try:
                parser(key)
            except (ValueError, TypeError, UnsupportedAlgorithm):
                continue
            return True
        return False
    finally:
        del candidate, key, parser, text


def _load_pem_private_key(key: bytes) -> object:
    try:
        return serialization.load_pem_private_key(key, password=None)
    finally:
        del key


def _load_der_private_key(key: bytes) -> object:
    try:
        return serialization.load_der_private_key(key, password=None)
    finally:
        del key


def _try_jwt_encode(
    payload: dict[str, object], key: bytes, algorithm: str
) -> str | object:
    try:
        try:
            return jwt.encode(payload, key, algorithm=algorithm)
        except jwt.PyJWTError:
            return _TOKEN_BACKEND_FAILURE
    finally:
        del algorithm, key, payload


def _configuration_error(
    algorithm: object, issuer: object, audience: object, leeway_seconds: object
) -> str | None:
    if algorithm not in _ALLOWED_ALGORITHMS:
        return "access token algorithm is not allowed"
    if (
        not isinstance(issuer, str)
        or not issuer
        or not isinstance(audience, str)
        or not audience
    ):
        return "token issuer and audience must be non-empty"
    if (
        not isinstance(leeway_seconds, int)
        or isinstance(leeway_seconds, bool)
        or not 0 <= leeway_seconds <= 60
    ):
        return "token leeway must be 0-60 seconds"
    return None


def _raise_token_configuration_error(message: str) -> None:
    raise ValueError(message)


def _raise_invalid_token() -> NoReturn:
    raise InvalidTokenError("invalid access token")


def _raise_token_backend_error() -> NoReturn:
    raise TokenBackendError("token backend failed")


def _utc_clock_value(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("token clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _claim_string(payload: Mapping[str, object], claim: str) -> str:
    value = None
    try:
        value = payload[claim]
        if not isinstance(value, str):
            raise TypeError
        return value
    finally:
        del payload, value


def _canonical_uuid_claim(payload: Mapping[str, object], claim: str) -> UUID:
    parsed = None
    value = None
    try:
        value = _claim_string(payload, claim)
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError
        return parsed
    finally:
        del parsed, payload, value
