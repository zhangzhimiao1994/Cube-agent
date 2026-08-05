"""Secret encryption and storage primitives."""

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import SecretRow

_DOMAIN_SEPARATOR = b"agent-hub-secret-v1"
_FINGERPRINT_KEY_LABEL = _DOMAIN_SEPARATOR + b"\x00fingerprint-key"
_MAX_SECRET_BYTES = 65_536
_NONCE_BYTES = 12
_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_REFERENCE_PATTERN = re.compile(
    r"secret://[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\Z"
)


class SecretValidationError(ValueError):
    pass


class SecretDecryptionError(RuntimeError):
    pass


class SecretNotFoundError(LookupError):
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
    def __init__(self, master_key: bytes, *, key_id: str = "v1") -> None:
        if not isinstance(master_key, bytes) or len(master_key) != 32:
            raise ValueError("AES-256 master key must be exactly 32 bytes")
        if _KEY_ID_PATTERN.fullmatch(key_id) is None:
            raise ValueError("key_id must be 1-64 safe characters")
        self._aead = AESGCM(master_key)
        self._fingerprint_key = hmac.digest(
            master_key, _FINGERPRINT_KEY_LABEL, hashlib.sha256
        )
        self._key_id = key_id

    def seal(self, plaintext: str, *, context: str) -> SealedSecret:
        encoded = _validated_plaintext(plaintext)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, encoded, _aad(context))
        return SealedSecret(
            key_id=self._key_id,
            nonce=_canonical_b64encode(nonce),
            ciphertext=_canonical_b64encode(ciphertext),
            fingerprint=self._fingerprint(encoded),
        )

    def open(self, sealed: SealedSecret, *, context: str) -> str:
        if sealed.key_id != self._key_id:
            raise SecretDecryptionError("secret key is not available")
        try:
            nonce = _canonical_b64decode(sealed.nonce)
            ciphertext = _canonical_b64decode(sealed.ciphertext)
            if len(nonce) != _NONCE_BYTES or len(ciphertext) < 16:
                raise ValueError
            if re.fullmatch(r"[0-9a-f]{64}", sealed.fingerprint) is None:
                raise ValueError
            plaintext = self._aead.decrypt(nonce, ciphertext, _aad(context))
            decoded = plaintext.decode("utf-8")
            if not hmac.compare_digest(self._fingerprint(plaintext), sealed.fingerprint):
                raise ValueError
            _validated_plaintext(decoded)
            return decoded
        except (InvalidTag, UnicodeDecodeError, ValueError, TypeError):
            raise SecretDecryptionError("secret could not be decrypted") from None

    def _fingerprint(self, plaintext: bytes) -> str:
        return hmac.digest(self._fingerprint_key, plaintext, hashlib.sha256).hex()


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
    ) -> SecretRow | None:
        result = await session.execute(
            select(SecretRow).where(
                SecretRow.tenant_id == tenant_id,
                SecretRow.id == secret_id,
            )
        )
        return result.scalars().one_or_none()


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
        sealed = self._cipher.seal(plaintext, context=str(tenant_id))
        async with self._session_factory() as session, session.begin():
            secret_id = await self._repository.create_or_get_id(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                sealed=sealed,
            )
        return SecretReference(tenant_id=tenant_id, secret_id=secret_id)

    async def resolve(
        self, tenant_id: UUID, reference: SecretReference | UUID | str
    ) -> str:
        secret_id = self._secret_id(tenant_id, reference)
        async with self._session_factory() as session:
            row = await self._repository.get(
                session, tenant_id=tenant_id, secret_id=secret_id
            )
        if row is None:
            raise SecretNotFoundError("secret was not found")
        sealed = SealedSecret(
            key_id=row.key_id,
            nonce=row.nonce,
            ciphertext=row.ciphertext,
            fingerprint=row.fingerprint,
        )
        return self._cipher.open(sealed, context=str(tenant_id))

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


def _validated_plaintext(plaintext: str) -> bytes:
    if not isinstance(plaintext, str) or not plaintext.strip():
        raise SecretValidationError("secret must be a non-blank string")
    encoded = plaintext.encode("utf-8")
    if len(encoded) > _MAX_SECRET_BYTES:
        raise SecretValidationError("secret must be at most 65536 UTF-8 bytes")
    return encoded


def _aad(context: str) -> bytes:
    if not isinstance(context, str):
        raise TypeError("secret context must be a string")
    return _DOMAIN_SEPARATOR + b"\x00" + context.encode("utf-8")


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
