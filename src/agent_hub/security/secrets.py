"""Secret encryption and storage primitives."""

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NoReturn
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import SecretRow

_DOMAIN_SEPARATOR = b"agent-hub-secret-v1"
_FINGERPRINT_KEY_LABEL = _DOMAIN_SEPARATOR + b"\x00fingerprint-key"
# Fingerprint scheme v2 is the first production scheme; see the hardening plan.
_FINGERPRINT_SCHEME_VERSION = b"v2"
_FINGERPRINT_DOMAIN = (
    b"agent-hub-secret-fingerprint-" + _FINGERPRINT_SCHEME_VERSION
)
_MAX_SECRET_BYTES = 65_536
_MAX_CONTEXT_BYTES = 1_024
_NONCE_BYTES = 12
_MAX_CIPHERTEXT_BYTES = _MAX_SECRET_BYTES + 16  # AES-GCM appends a 16-byte tag.
# Canonical base64 expands n bytes to 4 * ceil(n / 3) characters.
_MAX_CIPHERTEXT_B64_CHARS = 4 * ((_MAX_CIPHERTEXT_BYTES + 2) // 3)
_MIN_CIPHERTEXT_B64_CHARS = 24  # Canonical base64 for the minimum 16-byte tag.
_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_REFERENCE_PATTERN = re.compile(
    r"secret://[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_DECRYPTION_FAILED = object()


class SecretValidationError(ValueError):
    pass


class SecretDecryptionError(RuntimeError):
    pass


class SecretNotFoundError(LookupError):
    pass


class SecretPersistenceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class SealedSecret:
    key_id: str
    nonce: str
    ciphertext: str
    fingerprint: str

    def __repr__(self) -> str:
        return "SealedSecret(<redacted>)"

    __str__ = __repr__


class SecretCipher:
    """Encrypt secrets with an active key and decrypt with a validated keyring.

    ``fingerprint_key`` is 32-byte root material, not the direct HMAC key. Every
    path domain-derives the effective HMAC key from that root. Rotations must
    explicitly reuse the initial stable root and provide retired AES keys via
    ``decryption_keys``. A first single-key deployment defaults the root to
    ``master_key``; rotating that deployment passes the initial master key as
    ``fingerprint_key``.
    """

    def __init__(
        self,
        master_key: bytes,
        *,
        key_id: str = "v1",
        decryption_keys: Mapping[str, bytes] | None = None,
        fingerprint_key: bytes | None = None,
    ) -> None:
        _validate_key(master_key, description="AES-256 master key")
        _validate_key_id(key_id)
        if decryption_keys and fingerprint_key is None:
            raise ValueError(
                "fingerprint_key is required when decryption_keys are configured"
            )
        self._aead_keys = {key_id: AESGCM(master_key)}
        for decrypt_key_id, decrypt_key in (decryption_keys or {}).items():
            _validate_key_id(decrypt_key_id)
            if decrypt_key_id == key_id:
                raise ValueError("active key_id must not appear in decryption_keys")
            _validate_key(decrypt_key, description="AES-256 decryption key")
            self._aead_keys[decrypt_key_id] = AESGCM(decrypt_key)
        fingerprint_root = master_key if fingerprint_key is None else fingerprint_key
        _validate_key(fingerprint_root, description="fingerprint key")
        self._fingerprint_key = hmac.digest(
            fingerprint_root, _FINGERPRINT_KEY_LABEL, hashlib.sha256
        )
        self._key_id = key_id

    def seal(self, plaintext: str, *, context: str) -> SealedSecret:
        encoded = _try_validated_plaintext(plaintext)
        if isinstance(encoded, str):
            validation_message = encoded
            del plaintext, context, encoded
            _raise_secret_validation_error(validation_message)
        context_bytes = _try_validated_context(context)
        if context_bytes is None:
            del plaintext, context, encoded
            _raise_secret_validation_error("secret context is invalid")
        del plaintext, context
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = self._aead_keys[self._key_id].encrypt(
            nonce, encoded, _aad(context_bytes)
        )
        return SealedSecret(
            key_id=self._key_id,
            nonce=_canonical_b64encode(nonce),
            ciphertext=_canonical_b64encode(ciphertext),
            fingerprint=self._fingerprint(encoded, context_bytes),
        )

    def open(self, sealed: SealedSecret, *, context: str) -> str:
        opened = self._try_open(sealed, context)
        if opened is _DECRYPTION_FAILED:
            del sealed, context
            _raise_secret_decryption_error()
        assert isinstance(opened, str)
        return opened

    def _try_open(
        self, sealed: SealedSecret, context: str
    ) -> str | object:
        try:
            _validate_sealed_fields(sealed)
            context_bytes = _try_validated_context(context)
            if context_bytes is None:
                return _DECRYPTION_FAILED
            aead = self._aead_keys.get(sealed.key_id)
            if aead is None:
                return _DECRYPTION_FAILED
            nonce = _canonical_b64decode(sealed.nonce)
            ciphertext = _canonical_b64decode(sealed.ciphertext)
            if (
                len(nonce) != _NONCE_BYTES
                or len(ciphertext) < 16
                or len(ciphertext) > _MAX_CIPHERTEXT_BYTES
            ):
                return _DECRYPTION_FAILED
            plaintext = aead.decrypt(nonce, ciphertext, _aad(context_bytes))
            decoded = plaintext.decode("utf-8")
            if not hmac.compare_digest(
                self._fingerprint(plaintext, context_bytes), sealed.fingerprint
            ):
                return _DECRYPTION_FAILED
            if isinstance(_try_validated_plaintext(decoded), str):
                return _DECRYPTION_FAILED
            return decoded
        except (InvalidTag, UnicodeDecodeError, ValueError, TypeError):
            return _DECRYPTION_FAILED

    def _fingerprint(self, plaintext: bytes, context: bytes) -> str:
        fingerprint_input = (
            _FINGERPRINT_DOMAIN
            + len(context).to_bytes(4, "big")
            + context
            + plaintext
        )
        return hmac.digest(
            self._fingerprint_key, fingerprint_input, hashlib.sha256
        ).hex()


@dataclass(frozen=True, slots=True, repr=False)
class SecretReference:
    tenant_id: UUID
    secret_id: UUID

    @property
    def reference(self) -> str:
        return f"secret://{self.secret_id}"

    @classmethod
    def parse(cls, tenant_id: UUID, value: str) -> "SecretReference":
        if not isinstance(value, str) or _REFERENCE_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid secret reference")
        try:
            secret_id = UUID(value.removeprefix("secret://"))
        except ValueError:
            raise ValueError("invalid secret reference") from None
        if str(secret_id) != value.removeprefix("secret://"):
            raise ValueError("invalid secret reference")
        return cls(tenant_id=tenant_id, secret_id=secret_id)

    def __str__(self) -> str:
        return self.reference

    def __repr__(self) -> str:
        return (
            f"SecretReference(tenant_id={self.tenant_id!r}, "
            f"reference={self.reference!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _StoredSecret:
    secret_id: UUID
    tenant_id: UUID
    sealed: SealedSecret

    def __repr__(self) -> str:
        return (
            f"_StoredSecret(secret_id={self.secret_id!r}, "
            f"tenant_id={self.tenant_id!r}, sealed=<redacted>)"
        )


class SecretRepository:
    async def create_or_get_id(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        actor_id: UUID | None,
        sealed: SealedSecret,
    ) -> UUID:
        statement = (
            insert(SecretRow)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                fingerprint=sealed.fingerprint,
                key_id=sealed.key_id,
                nonce=sealed.nonce,
                ciphertext=sealed.ciphertext,
                created_by=actor_id,
            )
            .on_conflict_do_nothing(
                constraint="uq_agent_hub_secrets_tenant_fingerprint"
            )
            .returning(SecretRow.id)
        )
        secret_id = await session.scalar(statement)
        if secret_id is not None:
            return secret_id

        existing_id = await session.scalar(
            select(SecretRow.id).where(
                SecretRow.tenant_id == tenant_id,
                SecretRow.fingerprint == sealed.fingerprint,
            )
        )
        if existing_id is None:
            raise RuntimeError("secret deduplication conflict could not be resolved")
        return existing_id

    async def get(
        self, session: AsyncSession, *, tenant_id: UUID, secret_id: UUID
    ) -> _StoredSecret | None:
        result = await session.execute(
            select(SecretRow).where(
                SecretRow.tenant_id == tenant_id,
                SecretRow.id == secret_id,
            )
        )
        row = result.scalars().one_or_none()
        if row is None:
            return None
        return _StoredSecret(
            secret_id=row.id,
            tenant_id=row.tenant_id,
            sealed=SealedSecret(
                key_id=row.key_id,
                nonce=row.nonce,
                ciphertext=row.ciphertext,
                fingerprint=row.fingerprint,
            ),
        )


class SecretService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: SecretCipher,
        repository: SecretRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._repository = repository or SecretRepository()

    async def create_or_get(
        self, tenant_id: UUID, actor_id: UUID | None, plaintext: str
    ) -> SecretReference:
        sealed = self._try_seal_for_storage(tenant_id, plaintext)
        del plaintext
        if isinstance(sealed, str):
            validation_message = sealed
            del sealed
            _raise_secret_validation_error(validation_message)
        try:
            async with self._session_factory() as session, session.begin():
                secret_id = await self._repository.create_or_get_id(
                    session,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    sealed=sealed,
                )
            return SecretReference(tenant_id=tenant_id, secret_id=secret_id)
        except SQLAlchemyError:
            pass
        del sealed
        _raise_secret_persistence_error()

    def _try_seal_for_storage(
        self, tenant_id: UUID, plaintext: str
    ) -> SealedSecret | str:
        try:
            return self._cipher.seal(plaintext, context=str(tenant_id))
        except SecretValidationError as error:
            return str(error)

    async def resolve(
        self, tenant_id: UUID, reference: SecretReference | UUID | str
    ) -> str:
        secret_id = self._secret_id(tenant_id, reference)
        async with self._session_factory() as session:
            stored = await self._repository.get(
                session, tenant_id=tenant_id, secret_id=secret_id
            )
        if stored is None:
            raise SecretNotFoundError("secret was not found")
        return self._cipher.open(stored.sealed, context=str(tenant_id))

    async def fingerprint(
        self, tenant_id: UUID, reference: SecretReference | UUID | str
    ) -> str:
        """Return non-secret credential metadata for capacity deduplication.

        The fingerprint is a keyed, tenant-local digest generated when the secret
        was sealed. It lets the model capacity layer deduplicate shared API keys
        without decrypting or exposing the key value.
        """

        secret_id = self._secret_id(tenant_id, reference)
        async with self._session_factory() as session:
            stored = await self._repository.get(
                session, tenant_id=tenant_id, secret_id=secret_id
            )
        if stored is None:
            raise SecretNotFoundError("secret was not found")
        return stored.sealed.fingerprint

    @staticmethod
    def _secret_id(
        tenant_id: UUID, reference: SecretReference | UUID | str
    ) -> UUID:
        if isinstance(reference, SecretReference):
            if reference.tenant_id != tenant_id:
                raise SecretNotFoundError("secret was not found")
            return reference.secret_id
        if isinstance(reference, UUID):
            return reference
        return SecretReference.parse(tenant_id, reference).secret_id


def _try_validated_plaintext(plaintext: str) -> bytes | str:
    if not isinstance(plaintext, str):
        return "secret must be a non-blank string"
    if len(plaintext) > _MAX_SECRET_BYTES:
        return "secret must be at most 65536 UTF-8 bytes"
    if not plaintext.strip():
        return "secret must be a non-blank string"
    try:
        encoded = plaintext.encode("utf-8")
    except UnicodeEncodeError:
        return "secret could not be encoded"
    if len(encoded) > _MAX_SECRET_BYTES:
        return "secret must be at most 65536 UTF-8 bytes"
    return encoded


def _raise_secret_validation_error(message: str) -> NoReturn:
    raise SecretValidationError(message)


def _raise_secret_decryption_error() -> NoReturn:
    raise SecretDecryptionError("secret could not be decrypted")


def _raise_secret_persistence_error() -> NoReturn:
    raise SecretPersistenceError("secret could not be stored")


def _validate_key(key: bytes, *, description: str) -> None:
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError(f"{description} must be exactly 32 bytes")


def _validate_key_id(key_id: str) -> None:
    if not isinstance(key_id, str) or _KEY_ID_PATTERN.fullmatch(key_id) is None:
        raise ValueError("key_id must be 1-64 safe characters")


def _validate_sealed_fields(sealed: SealedSecret) -> None:
    if (
        not isinstance(sealed.key_id, str)
        or len(sealed.key_id) > 64
        or _KEY_ID_PATTERN.fullmatch(sealed.key_id) is None
        or not isinstance(sealed.nonce, str)
        or len(sealed.nonce) != 16
        or not isinstance(sealed.fingerprint, str)
        or len(sealed.fingerprint) != 64
        or re.fullmatch(r"[0-9a-f]{64}", sealed.fingerprint) is None
        or not isinstance(sealed.ciphertext, str)
        or not (
            _MIN_CIPHERTEXT_B64_CHARS
            <= len(sealed.ciphertext)
            <= _MAX_CIPHERTEXT_B64_CHARS
        )
    ):
        raise ValueError


def _aad(context: bytes) -> bytes:
    return _DOMAIN_SEPARATOR + b"\x00" + context


def _try_validated_context(context: str) -> bytes | None:
    if not isinstance(context, str) or len(context) > _MAX_CONTEXT_BYTES:
        return None
    try:
        encoded = context.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > _MAX_CONTEXT_BYTES:
        return None
    return encoded


def _canonical_b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _canonical_b64decode(value: str) -> bytes:
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (AttributeError, UnicodeEncodeError, binascii.Error, ValueError):
        raise ValueError from None
    if _canonical_b64encode(decoded) != value:
        raise ValueError
    return decoded
