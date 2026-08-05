"""Strict HS256 access-token issuing and validation."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, NoReturn
from uuid import UUID, uuid4

import jwt

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


class InvalidTokenError(RuntimeError):
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
        payload = {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "role": role.value,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "iss": self._issuer,
            "aud": self._audience,
            "jti": str(uuid4()),
        }
        return jwt.encode(payload, self._key, algorithm=self._algorithm)

    def decode(self, token: str) -> DecodedAccessToken:
        payload = self._try_decode(token)
        del token
        if payload is None:
            _raise_invalid_token()
        decoded = self._try_materialize(payload)
        del payload
        if decoded is None:
            _raise_invalid_token()
        return decoded

    def _try_decode(self, token: str) -> Mapping[str, object] | None:
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

    def _try_materialize(self, payload: Mapping[str, object]) -> DecodedAccessToken | None:
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


def _try_validated_key(signing_key: object) -> tuple[bytes | None, str | None]:
    if isinstance(signing_key, str):
        try:
            key = signing_key.encode("utf-8")
        except UnicodeEncodeError:
            return None, "access token signing key is invalid"
    elif isinstance(signing_key, bytes):
        key = signing_key
    else:
        return None, "access token signing key must be bytes or text"
    if len(key) < 32:
        return None, "access token signing key must be at least 32 bytes"
    return key, None


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


def _utc_clock_value(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("token clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _claim_string(payload: Mapping[str, object], claim: str) -> str:
    value = payload[claim]
    if not isinstance(value, str):
        raise TypeError
    return value


def _canonical_uuid_claim(payload: Mapping[str, object], claim: str) -> UUID:
    value = _claim_string(payload, claim)
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError
    return parsed
