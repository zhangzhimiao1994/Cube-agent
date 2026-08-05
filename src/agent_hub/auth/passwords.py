"""Argon2id local-password hashing with bounded asynchronous admission."""

import asyncio
import logging
import re
import threading
from collections.abc import Callable
from typing import Final, NoReturn

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import HashingError, InvalidHashError, VerificationError
from argon2.low_level import Type

from agent_hub.auth.models import AuthenticationBusy

_LOGGER = logging.getLogger(__name__)

_MIN_PASSWORD_CHARS = 12
_MAX_PASSWORD_CHARS = 1024
_MAX_PASSWORD_BYTES = 4096
_MAX_HASH_CHARS = 512
_MAX_MEMORY_COST = 65_536
_MAX_TIME_COST = 5
_MAX_PARALLELISM = 4
_ARGON2ID_ENCODING = re.compile(
    r"\$argon2id\$v=(?P<version>[0-9]+)\$"
    r"m=(?P<memory>[0-9]+),t=(?P<time>[0-9]+),p=(?P<parallelism>[0-9]+)\$"
    r"[A-Za-z0-9+/]{8,128}\$[A-Za-z0-9+/]{8,128}\Z"
)
_BUSY: Final[object] = object()
_CANCELLED: Final[object] = object()
_BACKEND_FAILURE: Final[object] = object()
_DUMMY_PASSWORD = "agent hub dummy password value"
_DEFAULT_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$/UuOtaxqYGUC5cqERobfOA$"
    "ejLTLxTVcey4qaTa6Jf3zCiI8k6qjqbuZQXIrdfR/i4"
)


class PasswordValidationError(ValueError):
    pass


class PasswordBackendError(RuntimeError):
    pass


class PasswordService:
    def __init__(
        self,
        hasher: PasswordHasher | None = None,
        *,
        dummy_hash: str | None = None,
        max_concurrency: int = 2,
        max_waiters: int = 4,
        acquire_timeout_seconds: float = 0.1,
    ) -> None:
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer")
        if (
            not isinstance(max_waiters, int)
            or isinstance(max_waiters, bool)
            or max_waiters < 0
        ):
            raise ValueError("max_waiters must be a non-negative integer")
        if not 0 < acquire_timeout_seconds <= 5:
            raise ValueError("acquire timeout must be between 0 and 5 seconds")
        supplied_hasher = hasher is not None
        self._hasher = hasher or PasswordHasher(
            time_cost=3,
            memory_cost=65_536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        if (
            self._hasher.memory_cost > _MAX_MEMORY_COST
            or self._hasher.time_cost > _MAX_TIME_COST
            or self._hasher.parallelism > _MAX_PARALLELISM
        ):
            raise ValueError("Argon2 profile exceeds the configured per-worker budget")
        if dummy_hash is None and not supplied_hasher:
            dummy_hash = _DEFAULT_DUMMY_HASH
        elif dummy_hash is None:
            dummy_hasher = PasswordHasher(
                time_cost=self._hasher.time_cost,
                memory_cost=self._hasher.memory_cost,
                parallelism=self._hasher.parallelism,
                hash_len=self._hasher.hash_len,
                salt_len=self._hasher.salt_len,
                encoding=self._hasher.encoding,
                type=self._hasher.type,
            )
            generated = _try_hash(dummy_hasher, _DUMMY_PASSWORD)
            del dummy_hasher
            if generated is _BACKEND_FAILURE:
                _raise_password_backend_error()
            assert isinstance(generated, str)
            dummy_hash = generated
        if not _dummy_matches_hasher_profile(dummy_hash, self._hasher):
            raise ValueError("dummy hash must match the configured Argon2 profile")
        self._dummy_hash = dummy_hash
        self._work_slots = threading.BoundedSemaphore(max_concurrency)
        self._admission_lock = threading.Lock()
        self._admission_capacity = max_concurrency + max_waiters
        self._admitted = 0
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._background_failure_lock = threading.Lock()
        self._background_failure_count = 0

    def hash(self, password: str) -> str:
        if not _is_valid_password(password):
            del password
            _raise_password_validation_error()
        try:
            outcome = _try_hash(self._hasher, password)
        finally:
            del password
        if outcome is _BACKEND_FAILURE:
            del outcome
            _raise_password_backend_error()
        assert isinstance(outcome, str)
        return outcome

    @property
    def dummy_hash(self) -> str:
        return self._dummy_hash

    @property
    def background_failure_count(self) -> int:
        with self._background_failure_lock:
            return self._background_failure_count

    async def hash_async(self, password: str) -> str:
        if not _is_valid_password(password):
            del password
            _raise_password_validation_error()
        try:
            outcome = await self._run_bounded(_try_hash, self._hasher, password)
        finally:
            del password
        if outcome is _CANCELLED:
            del outcome
            _raise_cancelled()
        if outcome is _BACKEND_FAILURE:
            del outcome
            _raise_password_backend_error()
        if outcome is _BUSY:
            del outcome
            _raise_authentication_busy()
        assert isinstance(outcome, str)
        return outcome

    def verify(self, password_hash: str, password: str) -> bool:
        if not _is_safe_argon2id_encoding(password_hash) or not _is_valid_password(password):
            return False
        try:
            return self._verify_prevalidated(password_hash, password)
        finally:
            del password_hash, password

    async def verify_async(self, password_hash: str, password: str) -> bool:
        if not _is_safe_argon2id_encoding(password_hash) or not _is_valid_password(password):
            return False
        try:
            outcome = await self._run_bounded(
                self._verify_prevalidated, password_hash, password
            )
        finally:
            del password_hash, password
        if outcome is _CANCELLED:
            del outcome
            _raise_cancelled()
        if outcome is _BUSY:
            del outcome
            _raise_authentication_busy()
        assert isinstance(outcome, bool)
        return outcome

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            if not _is_safe_argon2id_encoding(password_hash):
                return False
            try:
                return self._hasher.check_needs_rehash(password_hash)
            except (InvalidHashError, TypeError, ValueError):
                return False
        finally:
            del password_hash

    def _verify_prevalidated(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError, TypeError, ValueError):
            return False
        finally:
            del password_hash, password

    async def _run_bounded(
        self, operation: Callable[..., object], *args: object
    ) -> object:
        worker = None
        try:
            if not self._try_admit():
                return _BUSY
            worker = asyncio.create_task(
                asyncio.to_thread(self._run_admitted, operation, args)
            )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                worker.add_done_callback(self._retrieve_background_result)
                return _CANCELLED
        finally:
            del args, operation, worker

    def _try_admit(self) -> bool:
        with self._admission_lock:
            if self._admitted >= self._admission_capacity:
                return False
            self._admitted += 1
            return True

    def _run_admitted(
        self, operation: Callable[..., object], args: tuple[object, ...]
    ) -> object:
        acquired = False
        try:
            acquired = self._work_slots.acquire(timeout=self._acquire_timeout_seconds)
            if not acquired:
                return _BUSY
            return operation(*args)
        finally:
            if acquired:
                self._work_slots.release()
            with self._admission_lock:
                assert self._admitted > 0
                self._admitted -= 1
            del args, operation

    def _retrieve_background_result(self, task: asyncio.Task[object]) -> None:
        category = None
        try:
            try:
                result = task.result()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - caller can no longer receive failure
                category = "unexpected"
            else:
                if result is _BACKEND_FAILURE:
                    category = "password_backend"
            if category is None:
                return
            _LOGGER.warning(
                "background password worker failed",
                extra={"failure_category": category},
            )
            with self._background_failure_lock:
                self._background_failure_count += 1
        finally:
            del category, task


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


def _try_hash(hasher: PasswordHasher, password: str) -> str | object:
    try:
        return hasher.hash(password)
    except HashingError:
        return _BACKEND_FAILURE
    finally:
        del password


def _is_safe_argon2id_encoding(password_hash: object) -> bool:
    match = None
    try:
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
    finally:
        del match, password_hash


def _dummy_matches_hasher_profile(dummy_hash: object, hasher: PasswordHasher) -> bool:
    if not _is_safe_argon2id_encoding(dummy_hash):
        return False
    assert isinstance(dummy_hash, str)
    try:
        parameters = extract_parameters(dummy_hash)
    except InvalidHashError:
        return False
    return (
        parameters.type is hasher.type
        and parameters.time_cost == hasher.time_cost
        and parameters.memory_cost == hasher.memory_cost
        and parameters.parallelism == hasher.parallelism
        and parameters.hash_len == hasher.hash_len
        and parameters.salt_len == hasher.salt_len
    )


def _raise_password_validation_error() -> NoReturn:
    raise PasswordValidationError(
        "password must be a non-blank UTF-8 string of 12-1024 characters and at most 4096 bytes"
    )


def _raise_authentication_busy() -> NoReturn:
    raise AuthenticationBusy("authentication is busy")


def _raise_password_backend_error() -> NoReturn:
    raise PasswordBackendError("password backend failed")


def _raise_cancelled() -> NoReturn:
    raise asyncio.CancelledError
