"""Transactional local bootstrap and login orchestration."""

import asyncio
import base64
import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from typing import NoReturn
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import (
    AuthenticatedPrincipal,
    AuthenticationBusy,
    AuthenticationOperationError,
    AuthenticationPersistenceError,
    AuthResult,
    BootstrapClosed,
    InvalidBootstrapCode,
    InvalidCredentials,
    Role,
    UsernameValidationError,
)
from agent_hub.auth.passwords import (
    PasswordBackendError,
    PasswordService,
    PasswordValidationError,
)
from agent_hub.auth.tokens import AccessTokenService, InvalidTokenError, TokenBackendError
from agent_hub.db.models import BootstrapCodeRow, UserRow

_BOOTSTRAP_BYTES = 32
_BOOTSTRAP_CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_USERNAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{2,63}\Z")
_MIN_BOOTSTRAP_TTL_SECONDS = 60
_MAX_BOOTSTRAP_TTL_SECONDS = 3600
_BOOTSTRAP_LOCK_KEY = 0x4148554241555448
_INVALID_BOOTSTRAP_HASH = hashlib.sha256(b"invalid-bootstrap-code").hexdigest()


class _Failure(Enum):
    BOOTSTRAP_CLOSED = auto()
    INVALID_BOOTSTRAP = auto()
    INVALID_CREDENTIALS = auto()
    INVALID_PASSWORD = auto()
    INVALID_USERNAME = auto()
    BUSY = auto()
    CANCELLED = auto()
    OPERATION = auto()
    PERSISTENCE = auto()


class AuthService:
    """Own short-lived sessions for bootstrap and tenant-scoped local login.

    The service bounds Argon2 work globally per ``PasswordService`` instance.
    Task 7's HTTP ingress must additionally apply per-IP rate limiting.
    """

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

    async def issue_bootstrap_code(self, ttl_seconds: int = 900) -> str:
        _validate_bootstrap_ttl(ttl_seconds)
        outcome = await self._try_issue_bootstrap_code(ttl_seconds)
        if outcome is _Failure.BOOTSTRAP_CLOSED:
            del outcome
            _raise_bootstrap_closed()
        if outcome is _Failure.PERSISTENCE:
            del outcome
            _raise_authentication_persistence("authentication state could not be stored")
        if outcome is _Failure.OPERATION:
            del outcome
            _raise_authentication_operation()
        if outcome is _Failure.CANCELLED:
            del outcome
            _raise_cancelled()
        assert isinstance(outcome, str)
        return outcome

    async def _try_issue_bootstrap_code(self, ttl_seconds: int) -> str | _Failure:
        code = None
        session = None
        try:
            async with self._session_factory() as session, session.begin():
                await _lock_bootstrap(session)
                if await _has_super_admin(session):
                    return _Failure.BOOTSTRAP_CLOSED
                code = _encode_bootstrap_code(secrets.token_bytes(_BOOTSTRAP_BYTES))
                session.add(
                    BootstrapCodeRow(
                        id=uuid4(),
                        code_hash=_hash_bootstrap_code(code),
                        tenant_id=self._bootstrap_tenant_id,
                        expires_at=_utc_clock_value(self._clock())
                        + timedelta(seconds=ttl_seconds),
                    )
                )
                await session.flush()
            assert isinstance(code, str)
            return code
        except SQLAlchemyError:
            return _Failure.PERSISTENCE
        except asyncio.CancelledError:
            return _Failure.CANCELLED
        finally:
            del code, session

    async def consume_bootstrap_code(
        self, code: str, username: str, password: str
    ) -> AuthResult:
        try:
            outcome = await self._try_consume_bootstrap_code(code, username, password)
        finally:
            del code, username, password
        if isinstance(outcome, AuthResult):
            return outcome
        failure = outcome
        del outcome
        if failure is _Failure.INVALID_USERNAME:
            del failure
            _raise_username_validation()
        if failure is _Failure.INVALID_PASSWORD:
            del failure
            _raise_password_validation()
        if failure is _Failure.BUSY:
            del failure
            _raise_authentication_busy()
        if failure is _Failure.PERSISTENCE:
            del failure
            _raise_authentication_persistence("authentication state could not be stored")
        if failure is _Failure.OPERATION:
            del failure
            _raise_authentication_operation()
        if failure is _Failure.CANCELLED:
            del failure
            _raise_cancelled()
        del failure
        _raise_invalid_bootstrap()

    async def _try_consume_bootstrap_code(
        self, code: object, username: object, password: object
    ) -> AuthResult | _Failure:
        access_token = None
        code_hash = None
        lookup_hash = None
        password_hash = None
        principal = None
        result = None
        row = None
        session = None
        user_id = None
        try:
            if not _is_valid_username(username):
                return _Failure.INVALID_USERNAME
            assert isinstance(username, str)
            code_hash = _try_hash_canonical_bootstrap_code(code)
            lookup_hash = code_hash or _INVALID_BOOTSTRAP_HASH
            async with self._session_factory() as session, session.begin():
                await _lock_bootstrap(session)
                if await _has_super_admin(session):
                    return _Failure.INVALID_BOOTSTRAP
                row = await session.scalar(
                    select(BootstrapCodeRow)
                    .where(
                        BootstrapCodeRow.tenant_id == self._bootstrap_tenant_id,
                        BootstrapCodeRow.code_hash == lookup_hash,
                    )
                    .with_for_update()
                )
                now = _utc_clock_value(self._clock())
                if row is None or row.consumed_at is not None or row.expires_at <= now:
                    return _Failure.INVALID_BOOTSTRAP
                try:
                    password_hash = await self._passwords.hash_async(password)  # type: ignore[arg-type]
                except PasswordValidationError:
                    return _Failure.INVALID_PASSWORD
                except AuthenticationBusy:
                    return _Failure.BUSY
                except PasswordBackendError:
                    return _Failure.OPERATION
                user_id = uuid4()
                principal = AuthenticatedPrincipal(
                    user_id=user_id,
                    tenant_id=self._bootstrap_tenant_id,
                    role=Role.SUPER_ADMIN,
                )
                access_token = _try_encode_token(self._tokens, principal)
                if access_token is None:
                    return _Failure.OPERATION
                row.consumed_at = now
                session.add(
                    UserRow(
                        id=user_id,
                        tenant_id=self._bootstrap_tenant_id,
                        username=username,
                        password_hash=password_hash,
                        role=Role.SUPER_ADMIN.value,
                        protected=True,
                    )
                )
                await session.flush()
                result = AuthResult(principal=principal, access_token=access_token)
            return result
        except SQLAlchemyError:
            return _Failure.PERSISTENCE
        except asyncio.CancelledError:
            return _Failure.CANCELLED
        finally:
            del (
                access_token,
                code,
                code_hash,
                lookup_hash,
                password,
                password_hash,
                principal,
                result,
                row,
                session,
                user_id,
                username,
            )

    async def login(
        self, tenant_id: UUID, username: str, password: str
    ) -> AuthResult:
        try:
            outcome = await self._try_login(tenant_id, username, password)
        finally:
            del username, password
        if isinstance(outcome, AuthResult):
            return outcome
        failure = outcome
        del outcome
        if failure is _Failure.BUSY:
            del failure
            _raise_authentication_busy()
        if failure is _Failure.PERSISTENCE:
            del failure
            _raise_authentication_persistence("authentication state could not be read")
        if failure is _Failure.OPERATION:
            del failure
            _raise_authentication_operation()
        if failure is _Failure.CANCELLED:
            del failure
            _raise_cancelled()
        del failure
        _raise_invalid_credentials()

    async def _try_login(
        self, tenant_id: object, username: object, password: object
    ) -> AuthResult | _Failure:
        access_token = None
        changed = None
        new_hash = None
        old_hash = None
        principal = None
        result = None
        role = None
        row = None
        session = None
        statement = None
        verified = False
        try:
            async with self._session_factory() as session, session.begin():
                row = None
                if isinstance(tenant_id, UUID) and _is_valid_username(username):
                    row = await session.scalar(
                        select(UserRow).where(
                            UserRow.tenant_id == tenant_id,
                            UserRow.username == username,
                        )
                    )
                old_hash = (
                    row.password_hash
                    if row is not None and row.password_hash is not None
                    else self._passwords.dummy_hash
                )
                try:
                    verified = await self._passwords.verify_async(
                        old_hash, password  # type: ignore[arg-type]
                    )
                except AuthenticationBusy:
                    return _Failure.BUSY
                except PasswordBackendError:
                    return _Failure.OPERATION
                if row is None or row.password_hash is None or row.disabled or not verified:
                    return _Failure.INVALID_CREDENTIALS
                try:
                    role = Role(row.role)
                except ValueError:
                    return _Failure.INVALID_CREDENTIALS
                principal = AuthenticatedPrincipal(row.id, row.tenant_id, role)
                access_token = _try_encode_token(self._tokens, principal)
                if access_token is None:
                    return _Failure.OPERATION
                if self._passwords.needs_rehash(old_hash):
                    try:
                        new_hash = await self._passwords.hash_async(password)  # type: ignore[arg-type]
                    except (AuthenticationBusy, PasswordValidationError):
                        return _Failure.BUSY
                    except PasswordBackendError:
                        return _Failure.OPERATION
                    statement = (
                        update(UserRow)
                        .where(UserRow.id == row.id, UserRow.password_hash == old_hash)
                        .values(password_hash=new_hash)
                        .execution_options(synchronize_session=False)
                    )
                    changed = await session.execute(statement)
                    if getattr(changed, "rowcount", 0) != 1:
                        return _Failure.INVALID_CREDENTIALS
                result = AuthResult(principal=principal, access_token=access_token)
            return result
        except SQLAlchemyError:
            return _Failure.PERSISTENCE
        except asyncio.CancelledError:
            return _Failure.CANCELLED
        finally:
            del (
                access_token,
                changed,
                new_hash,
                old_hash,
                password,
                principal,
                result,
                role,
                row,
                session,
                statement,
                tenant_id,
                username,
                verified,
            )

    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        principal = None
        try:
            principal = _try_decode_principal(self._tokens, token)
        finally:
            del token
        if principal is None:
            _raise_invalid_token()
        return principal


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


def _try_encode_token(
    token_service: AccessTokenService, principal: AuthenticatedPrincipal
) -> str | None:
    try:
        return token_service.encode(
            principal.user_id, principal.tenant_id, principal.role
        )
    except TokenBackendError:
        return None


def _try_decode_principal(
    token_service: AccessTokenService, token: str
) -> AuthenticatedPrincipal | None:
    try:
        try:
            return token_service.decode(token).principal
        except InvalidTokenError:
            return None
    finally:
        del token


def _validate_bootstrap_ttl(ttl_seconds: int) -> None:
    if (
        not isinstance(ttl_seconds, int)
        or isinstance(ttl_seconds, bool)
        or not _MIN_BOOTSTRAP_TTL_SECONDS
        <= ttl_seconds
        <= _MAX_BOOTSTRAP_TTL_SECONDS
    ):
        raise ValueError("bootstrap code TTL must be 60-3600 seconds")


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


def _raise_bootstrap_closed() -> NoReturn:
    raise BootstrapClosed("bootstrap is closed")


def _raise_invalid_bootstrap() -> NoReturn:
    raise InvalidBootstrapCode("invalid bootstrap code")


def _raise_invalid_credentials() -> NoReturn:
    raise InvalidCredentials("invalid credentials")


def _raise_username_validation() -> NoReturn:
    raise UsernameValidationError(
        "username must be a lowercase safe identifier of 3-64 characters"
    )


def _raise_password_validation() -> NoReturn:
    raise PasswordValidationError(
        "password must be a non-blank UTF-8 string of 12-1024 characters and at most 4096 bytes"
    )


def _raise_authentication_busy() -> NoReturn:
    raise AuthenticationBusy("authentication is busy")


def _raise_authentication_persistence(message: str) -> NoReturn:
    raise AuthenticationPersistenceError(message)


def _raise_authentication_operation() -> NoReturn:
    raise AuthenticationOperationError("authentication operation failed")


def _raise_cancelled() -> NoReturn:
    raise asyncio.CancelledError


def _raise_invalid_token() -> NoReturn:
    raise InvalidTokenError("invalid access token")
