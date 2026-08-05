from base64 import b64encode
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import UniqueConstraint

import agent_hub.db.models as db_models
import agent_hub.security.secrets as secrets_module
from agent_hub.security.secrets import (
    SealedSecret,
    SecretCipher,
    SecretDecryptionError,
    SecretReference,
    SecretValidationError,
)

MASTER_KEY = bytes(range(32))
OTHER_KEY = bytes(reversed(range(32)))


def test_secret_cipher_round_trip_is_bound_to_context() -> None:
    cipher = SecretCipher(MASTER_KEY)

    sealed = cipher.seal("correct horse battery staple", context="tenant-a")

    assert cipher.open(sealed, context="tenant-a") == "correct horse battery staple"
    assert sealed.key_id == "v1"
    assert len(sealed.nonce) == 16
    assert len(sealed.fingerprint) == 64


def test_sealing_same_plaintext_uses_random_ciphertext_but_stable_fingerprint() -> None:
    cipher = SecretCipher(MASTER_KEY)

    first = cipher.seal("same secret", context="tenant-a")
    second = cipher.seal("same secret", context="tenant-a")

    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert first.fingerprint == second.fingerprint
    assert cipher.seal("different secret", context="tenant-a").fingerprint != first.fingerprint


@pytest.mark.parametrize("key", [b"", b"short", bytes(31), bytes(33)])
def test_secret_cipher_rejects_non_aes256_keys(key: bytes) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        SecretCipher(key)


@pytest.mark.parametrize("plaintext", ["", " ", "\t\r\n"])
def test_seal_rejects_empty_or_whitespace_secret(plaintext: str) -> None:
    with pytest.raises(SecretValidationError, match="non-blank"):
        SecretCipher(MASTER_KEY).seal(plaintext, context="tenant-a")


def test_seal_rejects_secret_larger_than_64_kib_utf8() -> None:
    with pytest.raises(SecretValidationError, match="65536"):
        SecretCipher(MASTER_KEY).seal("x" * 65_537, context="tenant-a")


@pytest.mark.parametrize(
    "mutate,context",
    [
        (lambda sealed: sealed, "tenant-b"),
        (
            lambda sealed: replace(
                sealed,
                ciphertext=b64encode(b"tampered ciphertext").decode("ascii"),
            ),
            "tenant-a",
        ),
        (lambda sealed: replace(sealed, nonce="not/base64!"), "tenant-a"),
        (lambda sealed: replace(sealed, nonce=b64encode(b"too short").decode("ascii")), "tenant-a"),
        (lambda sealed: replace(sealed, fingerprint="z" * 64), "tenant-a"),
    ],
)
def test_open_safely_rejects_wrong_context_tampering_and_invalid_encoding(
    mutate: object, context: str
) -> None:
    cipher = SecretCipher(MASTER_KEY)
    sealed = cipher.seal("never reveal this", context="tenant-a")
    changed = mutate(sealed)  # type: ignore[operator]

    with pytest.raises(SecretDecryptionError) as captured:
        cipher.open(changed, context=context)

    message = str(captured.value)
    assert "never reveal this" not in message
    assert sealed.ciphertext not in message


def test_open_rejects_ciphertext_from_different_key() -> None:
    sealed = SecretCipher(MASTER_KEY).seal("private", context="tenant-a")

    with pytest.raises(SecretDecryptionError, match="could not be decrypted"):
        SecretCipher(OTHER_KEY).open(sealed, context="tenant-a")


def test_open_rejects_unknown_key_id_without_exposing_it() -> None:
    cipher = SecretCipher(MASTER_KEY)
    sealed = replace(cipher.seal("private", context="tenant-a"), key_id="retired-key")

    with pytest.raises(SecretDecryptionError, match="key is not available") as captured:
        cipher.open(sealed, context="tenant-a")

    assert "retired-key" not in str(captured.value)


def test_open_rejects_valid_authenticated_non_utf8_plaintext() -> None:
    nonce = bytes(range(12))
    context = "tenant-a"
    raw_ciphertext = AESGCM(MASTER_KEY).encrypt(
        nonce,
        b"\xff\xfe",
        b"agent-hub-secret-v1\x00" + context.encode("utf-8"),
    )
    sealed = SealedSecret(
        key_id="v1",
        nonce=b64encode(nonce).decode("ascii"),
        ciphertext=b64encode(raw_ciphertext).decode("ascii"),
        fingerprint="0" * 64,
    )

    with pytest.raises(SecretDecryptionError, match="could not be decrypted"):
        SecretCipher(MASTER_KEY).open(sealed, context=context)


def test_sealed_secret_repr_and_str_redact_all_payload_fields() -> None:
    sealed = SecretCipher(MASTER_KEY).seal("super private value", context="tenant-a")

    rendered = f"{sealed!r} {sealed!s}"

    assert "super private value" not in rendered
    assert sealed.nonce not in rendered
    assert sealed.ciphertext not in rendered
    assert sealed.fingerprint not in rendered


def test_secret_reference_format_parse_and_round_trip() -> None:
    tenant_id = uuid4()
    secret_id = uuid4()
    reference = SecretReference(tenant_id=tenant_id, secret_id=secret_id)

    assert str(reference) == f"secret://{secret_id}"
    assert SecretReference.parse(tenant_id, str(reference)) == reference
    assert reference.reference == str(reference)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://00000000-0000-0000-0000-000000000000",
        "secret://",
        "secret://not-a-uuid",
        "secret://00000000-0000-0000-0000-000000000000/extra",
        "secret://00000000-0000-0000-0000-000000000000?query=yes",
        "secret://00000000000000000000000000000000",
        "secret://AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
    ],
)
def test_secret_reference_parse_rejects_malformed_values(value: str) -> None:
    with pytest.raises(ValueError, match="invalid secret reference"):
        SecretReference.parse(UUID(int=1), value)


def test_secret_reference_is_immutable_and_repr_contains_no_encrypted_material() -> None:
    reference = SecretReference(UUID(int=1), UUID(int=2))

    with pytest.raises(AttributeError):
        reference.secret_id = UUID(int=3)  # type: ignore[misc]

    assert "fingerprint" not in repr(reference)
    assert "ciphertext" not in repr(reference)
    assert "nonce" not in repr(reference)


def test_secret_row_schema_has_only_encrypted_payload_and_tenant_deduplication() -> None:
    secret_row_type = getattr(db_models, "SecretRow", None)

    assert secret_row_type is not None
    table = secret_row_type.__table__
    assert set(table.columns.keys()) == {
        "id",
        "tenant_id",
        "fingerprint",
        "key_id",
        "nonce",
        "ciphertext",
        "created_by",
        "created_at",
    }
    assert all(
        not column.nullable
        for column in table.columns
        if column.name not in {"created_by"}
    )
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("tenant_id", "fingerprint") in unique_columns

    row = secret_row_type(
        tenant_id=UUID(int=1),
        fingerprint="f" * 64,
        key_id="v1",
        nonce="bm9uY2U=",
        ciphertext="Y2lwaGVydGV4dA==",
    )
    rendered = repr(row)
    assert "bm9uY2U=" not in rendered
    assert "Y2lwaGVydGV4dA==" not in rendered
    assert "f" * 64 not in rendered


def test_secret_storage_api_is_available() -> None:
    assert getattr(secrets_module, "SecretRepository", None) is not None
    assert getattr(secrets_module, "SecretService", None) is not None
    assert getattr(secrets_module, "SecretNotFoundError", None) is not None
