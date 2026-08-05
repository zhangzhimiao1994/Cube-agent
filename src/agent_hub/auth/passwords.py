"""Argon2id local-password hashing with a bounded input policy."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

_MIN_PASSWORD_CHARS = 12
_MAX_PASSWORD_CHARS = 1024
_MAX_PASSWORD_BYTES = 4096


class PasswordValidationError(ValueError):
    pass


class PasswordService:
    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher(
            time_cost=3,
            memory_cost=65_536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        _validate_password(password)
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        if not isinstance(password_hash, str) or not _is_valid_password(password):
            return False
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError, TypeError, ValueError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        if not isinstance(password_hash, str):
            return False
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except (InvalidHashError, TypeError, ValueError):
            return False


def _validate_password(password: str) -> None:
    if not _is_valid_password(password):
        raise PasswordValidationError(
            "password must be a non-blank UTF-8 string of 12-1024 characters and at most 4096 bytes"
        )


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
