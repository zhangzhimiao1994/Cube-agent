"""Argon2id local-password hashing with bounded asynchronous admission."""

import asyncio
import re
from collections.abc import Callable
from typing import Final, NoReturn

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

from agent_hub.auth.models import AuthenticationBusy

_MIN_PASSWORD_CHARS = 12
_MAX_PASSWORD_CHARS = 1024
_MAX_PASSWORD_BYTES = 4096
_MAX_HASH_CHARS = 512
_MAX_MEMORY_COST = 262_144
_MAX_TIME_COST = 10
_MAX_PARALLELISM = 16
_ARGON2ID_ENCODING = re.compile(
    r"\$argon2id\$v=(?P<version>[0-9]+)\$"
    r"m=(?P<memory>[0-9]+),t=(?P<time>[0-9]+),p=(?P<parallelism>[0-9]+)\$"
    r"[A-Za-z0-9+/]{8,128}\$[A-Za-z0-9+/]{8,128}\Z"
)
_BUSY: Final[object] = object()


class PasswordValidationError(ValueError):
    pass


class PasswordService:
    def __init__(
        self,
        hasher: PasswordHasher | None = None,
        *,
        max_concurrency: int = 2,
        max_queue: int = 4,
        acquire_timeout_seconds: float = 0.1,
    ) -> None:
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer")
        if (
            not isinstance(max_queue, int)
            or isinstance(max_queue, bool)
            or max_queue < 0
        ):
            raise ValueError("max_queue must be a non-negative integer")
        if not 0 < acquire_timeout_seconds <= 5:
            raise ValueError("acquire timeout must be between 0 and 5 seconds")
        self._hasher = hasher or PasswordHasher(
            time_cost=3,
            memory_cost=65_536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._admission_capacity = max_concurrency + max_queue
        self._admitted = 0
        self._acquire_timeout_seconds = acquire_timeout_seconds

    def hash(self, password: str) -> str:
        if not _is_valid_password(password):
            del password
            _raise_password_validation_error()
        return self._hasher.hash(password)

    async def hash_async(self, password: str) -> str:
        if not _is_valid_password(password):
            del password
            _raise_password_validation_error()
        outcome = await self._run_bounded(self._hasher.hash, password)
        del password
        if outcome is _BUSY:
            del outcome
            _raise_authentication_busy()
        assert isinstance(outcome, str)
        return outcome

    def verify(self, password_hash: str, password: str) -> bool:
        if not _is_safe_argon2id_encoding(password_hash) or not _is_valid_password(password):
            return False
        return self._verify_prevalidated(password_hash, password)

    async def verify_async(self, password_hash: str, password: str) -> bool:
        if not _is_safe_argon2id_encoding(password_hash) or not _is_valid_password(password):
            return False
        outcome = await self._run_bounded(
            self._verify_prevalidated, password_hash, password
        )
        del password_hash, password
        if outcome is _BUSY:
            del outcome
            _raise_authentication_busy()
        assert isinstance(outcome, bool)
        return outcome

    def needs_rehash(self, password_hash: str) -> bool:
        if not _is_safe_argon2id_encoding(password_hash):
            return False
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except (InvalidHashError, TypeError, ValueError):
            return False

    def _verify_prevalidated(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError, TypeError, ValueError):
            return False

    async def _run_bounded(
        self, operation: Callable[..., object], *args: object
    ) -> object:
        if self._admitted >= self._admission_capacity:
            return _BUSY
        self._admitted += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(), timeout=self._acquire_timeout_seconds
                )
                acquired = True
            except TimeoutError:
                return _BUSY
            return await asyncio.to_thread(operation, *args)
        finally:
            if acquired:
                self._semaphore.release()
            self._admitted -= 1


def _is_valid_password(password: object) -> bool:
    if not isinstance(password, str):
        return False
    if not _MIN_PASSWORD_CHARS <= len(password) <= _MAX_PASSWORD_CHARS:
        return False
    if not password.strip():
        return False
    try:
        encoded = password.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= _MAX_PASSWORD_BYTES


def _is_safe_argon2id_encoding(password_hash: object) -> bool:
    if not isinstance(password_hash, str) or len(password_hash) > _MAX_HASH_CHARS:
        return False
    match = _ARGON2ID_ENCODING.fullmatch(password_hash)
    if match is None:
        return False
    return (
        int(match.group("version")) == 19
        and 8 <= int(match.group("memory")) <= _MAX_MEMORY_COST
        and 1 <= int(match.group("time")) <= _MAX_TIME_COST
        and 1 <= int(match.group("parallelism")) <= _MAX_PARALLELISM
    )


def _raise_password_validation_error() -> NoReturn:
    raise PasswordValidationError(
        "password must be a non-blank UTF-8 string of 12-1024 characters and at most 4096 bytes"
    )


def _raise_authentication_busy() -> NoReturn:
    raise AuthenticationBusy("authentication is busy")
