"""Transactional local bootstrap and login orchestration."""

import base64
import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import (
    AuthenticatedPrincipal,
    AuthenticationPersistenceError,
    AuthResult,
    BootstrapClosed,
    InvalidBootstrapCode,
    InvalidCredentials,
    Role,
    UsernameValidationError,
)
from agent_hub.auth.passwords import PasswordService
from agent_hub.auth.tokens import AccessTokenService
from agent_hub.db.models import BootstrapCodeRow, UserRow

_BOOTSTRAP_BYTES = 32
_BOOTSTRAP_CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_USERNAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{2,63}\Z")
_MIN_BOOTSTRAP_TTL_SECONDS = 60
_MAX_BOOTSTRAP_TTL_SECONDS = 3600
_BOOTSTRAP_LOCK_KEY = 0x4148554241555448
_DUMMY_PASSWORD = "agent hub dummy password value"
_INVALID_BOOTSTRAP_HASH = hashlib.sha256(b"invalid-bootstrap-code").hexdigest()


class AuthService:
    """Own short-lived sessions for bootstrap and tenant-scoped local login."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bootstrap_tenant_id: UUID,
        password_service: PasswordService,
        token_service: AccessTokenService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(bootstrap_tenant_id, UUID):
            raise TypeError("bootstrap tenant id must be a UUID")
        self._session_factory = session_factory
        self._bootstrap_tenant_id = bootstrap_tenant_id
        self._passwords = password_service
        self._tokens = token_service
        self._clock = clock or (lambda: datetime.now(UTC))
        self._dummy_password_hash = password_service.hash(_DUMMY_PASSWORD)

    async def issue_bootstrap_code(self, ttl_seconds: int = 900) -> str:
        _validate_bootstrap_ttl(ttl_seconds)
        code = _encode_bootstrap_code(secrets.token_bytes(_BOOTSTRAP_BYTES))
        code_hash = _hash_bootstrap_code(code)
        now = _utc_clock_value(self._clock())
        try:
            async with self._session_factory() as session, session.begin():
                await _lock_bootstrap(session)
                if await _has_super_admin(session):
                    raise BootstrapClosed("bootstrap is closed")
                session.add(
                    BootstrapCodeRow(
                        id=uuid4(),
                        code_hash=code_hash,
                        tenant_id=self._bootstrap_tenant_id,
                        expires_at=now + timedelta(seconds=ttl_seconds),
                    )
                )
                await session.flush()
        except SQLAlchemyError:
            raise AuthenticationPersistenceError(
                "authentication state could not be stored"
            ) from None
        return code

    async def consume_bootstrap_code(self, code: str, username: str, password: str) -> AuthResult:
        _validate_username(username)
        password_hash = self._passwords.hash(password)
        code_hash = _try_hash_canonical_bootstrap_code(code)
        lookup_hash = code_hash or _INVALID_BOOTSTRAP_HASH
        now = _utc_clock_value(self._clock())
        try:
            async with self._session_factory() as session, session.begin():
                await _lock_bootstrap(session)
                if await _has_super_admin(session):
                    raise InvalidBootstrapCode("invalid bootstrap code")
                row = await session.scalar(
                    select(BootstrapCodeRow)
                    .where(
                        BootstrapCodeRow.tenant_id == self._bootstrap_tenant_id,
                        BootstrapCodeRow.code_hash == lookup_hash,
                    )
                    .with_for_update()
                )
                if row is None or row.consumed_at is not None or row.expires_at <= now:
                    raise InvalidBootstrapCode("invalid bootstrap code")
                user_id = uuid4()
                row.consumed_at = now
                session.add(
                    UserRow(
                        id=user_id,
                        tenant_id=self._bootstrap_tenant_id,
                        username=username,
                        password_hash=password_hash,
                        role=Role.SUPER_ADMIN.value,
                    )
                )
                await session.flush()
                principal = AuthenticatedPrincipal(
                    user_id=user_id,
                    tenant_id=self._bootstrap_tenant_id,
                    role=Role.SUPER_ADMIN,
                )
                result = AuthResult(
                    principal=principal,
                    access_token=self._tokens.encode(
                        principal.user_id, principal.tenant_id, principal.role
                    ),
                )
        except SQLAlchemyError:
            raise AuthenticationPersistenceError(
                "authentication state could not be stored"
            ) from None
        return result

    async def login(self, tenant_id: UUID, username: str, password: str) -> AuthResult:
        row: UserRow | None = None
        try:
            async with self._session_factory() as session:
                if isinstance(tenant_id, UUID) and _is_valid_username(username):
                    row = await session.scalar(
                        select(UserRow).where(
                            UserRow.tenant_id == tenant_id,
                            UserRow.username == username,
                        )
                    )
                password_hash = (
                    row.password_hash
                    if row is not None and row.password_hash is not None
                    else self._dummy_password_hash
                )
                verified = self._passwords.verify(password_hash, password)
                if row is None or row.password_hash is None or not verified:
                    raise InvalidCredentials("invalid credentials")
                try:
                    role = Role(row.role)
                except ValueError:
                    raise InvalidCredentials("invalid credentials") from None
                if self._passwords.needs_rehash(row.password_hash):
                    row.password_hash = self._passwords.hash(password)
                    await session.commit()
                principal = AuthenticatedPrincipal(row.id, row.tenant_id, role)
        except SQLAlchemyError:
            raise AuthenticationPersistenceError("authentication state could not be read") from None
        return AuthResult(
            principal=principal,
            access_token=self._tokens.encode(
                principal.user_id, principal.tenant_id, principal.role
            ),
        )

    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        return self._tokens.decode(token).principal


async def _lock_bootstrap(session: AsyncSession) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _BOOTSTRAP_LOCK_KEY},
    )


async def _has_super_admin(session: AsyncSession) -> bool:
    user_id = await session.scalar(
        select(UserRow.id).where(UserRow.role == Role.SUPER_ADMIN.value).limit(1)
    )
    return user_id is not None


def _validate_bootstrap_ttl(ttl_seconds: int) -> None:
    if (
        not isinstance(ttl_seconds, int)
        or isinstance(ttl_seconds, bool)
        or not _MIN_BOOTSTRAP_TTL_SECONDS <= ttl_seconds <= _MAX_BOOTSTRAP_TTL_SECONDS
    ):
        raise ValueError("bootstrap code TTL must be 60-3600 seconds")


def _validate_username(username: str) -> None:
    if not _is_valid_username(username):
        raise UsernameValidationError(
            "username must be a lowercase safe identifier of 3-64 characters"
        )


def _is_valid_username(username: object) -> bool:
    return (
        isinstance(username, str)
        and _USERNAME_PATTERN.fullmatch(username) is not None
        and ".." not in username
        and "--" not in username
        and "__" not in username
    )


def _encode_bootstrap_code(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _hash_bootstrap_code(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _try_hash_canonical_bootstrap_code(code: object) -> str | None:
    if not isinstance(code, str) or _BOOTSTRAP_CODE_PATTERN.fullmatch(code) is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(code + "=")
    except (ValueError, TypeError):
        return None
    if len(raw) != _BOOTSTRAP_BYTES or _encode_bootstrap_code(raw) != code:
        return None
    return _hash_bootstrap_code(code)


def _utc_clock_value(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("authentication clock must return a timezone-aware datetime")
    return value.astimezone(UTC)
