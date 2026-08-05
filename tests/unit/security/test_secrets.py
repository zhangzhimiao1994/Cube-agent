from base64 import b64encode
from collections.abc import Callable
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
ROTATED_KEY = b"r" * 32
FINGERPRINT_KEY = b"f" * 32
OpenFailureFactory = Callable[
    [SecretCipher, SealedSecret],
    tuple[SecretCipher, SealedSecret, str],
]


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


def test_fingerprint_is_tenant_local() -> None:
    cipher = SecretCipher(MASTER_KEY)

    tenant_a = cipher.seal("same secret", context="tenant-a")
    tenant_b = cipher.seal("same secret", context="tenant-b")

    assert tenant_a.fingerprint != tenant_b.fingerprint


def test_cipher_rotation_uses_keyring_and_stable_independent_fingerprint_key() -> None:
    v1 = SecretCipher(MASTER_KEY, key_id="v1", fingerprint_key=FINGERPRINT_KEY)
    old = v1.seal("rotated secret", context="tenant-a")
    v2 = SecretCipher(
        ROTATED_KEY,
        key_id="v2",
        decryption_keys={"v1": MASTER_KEY},
        fingerprint_key=FINGERPRINT_KEY,
    )

    resealed = v2.seal("rotated secret", context="tenant-a")

    assert v2.open(old, context="tenant-a") == "rotated secret"
    assert resealed.key_id == "v2"
    assert resealed.fingerprint == old.fingerprint
    with pytest.raises(SecretDecryptionError) as captured:
        SecretCipher(
            ROTATED_KEY,
            key_id="v2",
            fingerprint_key=FINGERPRINT_KEY,
        ).open(old, context="tenant-a")
    assert str(captured.value) == "secret could not be decrypted"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fingerprint_key": b"short"},
        {"decryption_keys": {"v0": b"short"}},
        {"decryption_keys": {"unsafe key id": MASTER_KEY}},
        {"key_id": "v1", "decryption_keys": {"v1": OTHER_KEY}},
    ],
)
def test_cipher_rejects_invalid_rotation_configuration(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SecretCipher(MASTER_KEY, **kwargs)  # type: ignore[arg-type]


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


def test_seal_accepts_exact_64_kib_utf8_boundary() -> None:
    cipher = SecretCipher(MASTER_KEY)
    plaintext = "é" * 32_768

    sealed = cipher.seal(plaintext, context="tenant-a")

    assert cipher.open(sealed, context="tenant-a") == plaintext


def test_seal_rejects_multibyte_secret_over_64_kib() -> None:
    with pytest.raises(SecretValidationError, match="65536"):
        SecretCipher(MASTER_KEY).seal("é" * 32_769, context="tenant-a")


class _OversizedWhitespace(str):
    def strip(self, chars: str | None = None) -> str:
        raise AssertionError("strip must not run for oversized text")

    def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
        raise AssertionError("encode must not run for oversized text")


def test_seal_fast_rejects_oversized_whitespace_before_strip_or_encode() -> None:
    plaintext = _OversizedWhitespace(" " * 65_537)

    with pytest.raises(SecretValidationError, match="65536"):
        SecretCipher(MASTER_KEY).seal(plaintext, context="tenant-a")


def test_seal_rejects_unpaired_surrogate_without_disclosing_input() -> None:
    plaintext = "private-\ud800-value"

    with pytest.raises(SecretValidationError) as captured:
        SecretCipher(MASTER_KEY).seal(plaintext, context="tenant-a")

    assert str(captured.value) == "secret could not be encoded"
    assert "private" not in str(captured.value)


@pytest.mark.parametrize(
    "changed",
    [
        SealedSecret("v1", "A" * 17, "A" * 24, "0" * 64),
        SealedSecret("v1", "A" * 16, "A" * 24, "0" * 65),
        SealedSecret("v1", "A" * 16, "A" * 87_405, "0" * 64),
        SealedSecret("k" * 65, "A" * 16, "A" * 24, "0" * 64),
    ],
    ids=["nonce", "fingerprint", "ciphertext", "key-id"],
)
def test_open_rejects_oversized_stored_fields_before_base64_decode(
    changed: SealedSecret, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_decode(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("base64 decode must not run for oversized fields")

    monkeypatch.setattr(
        "agent_hub.security.secrets.base64.b64decode",
        fail_decode,
    )

    with pytest.raises(SecretDecryptionError) as captured:
        SecretCipher(MASTER_KEY).open(changed, context="tenant-a")

    assert str(captured.value) == "secret could not be decrypted"


def test_open_rejects_oversized_decoded_ciphertext_before_aes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cipher = SecretCipher(MASTER_KEY)
    sealed = cipher.seal("small", context="tenant-a")
    oversized = replace(
        sealed,
        ciphertext=b64encode(bytes(65_553)).decode("ascii"),
    )

    def fail_decrypt(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("AES decrypt must not run for oversized ciphertext")

    monkeypatch.setattr(AESGCM, "decrypt", fail_decrypt)

    with pytest.raises(SecretDecryptionError) as captured:
        cipher.open(oversized, context="tenant-a")

    assert str(captured.value) == "secret could not be decrypted"


@pytest.mark.parametrize(
    "failure_factory",
    [
        pytest.param(
            lambda cipher, sealed: (
                cipher,
                replace(sealed, key_id="retired-key"),
                "tenant-a",
            ),
            id="unknown-key-id",
        ),
        pytest.param(
            lambda cipher, sealed: (
                cipher,
                replace(sealed, nonce="not/base64!"),
                "tenant-a",
            ),
            id="invalid-base64",
        ),
        pytest.param(
            lambda cipher, sealed: (
                cipher,
                replace(sealed, nonce=b64encode(b"too short").decode("ascii")),
                "tenant-a",
            ),
            id="invalid-nonce-length",
        ),
        pytest.param(
            lambda cipher, sealed: (cipher, sealed, "tenant-b"),
            id="authentication-wrong-context",
        ),
        pytest.param(
            lambda cipher, sealed: (SecretCipher(OTHER_KEY), sealed, "tenant-a"),
            id="authentication-wrong-key",
        ),
        pytest.param(
            lambda cipher, sealed: (
                cipher,
                replace(
                    sealed,
                    ciphertext=b64encode(b"tampered ciphertext").decode("ascii"),
                ),
                "tenant-a",
            ),
            id="authentication-tampered-ciphertext",
        ),
        pytest.param(
            lambda cipher, sealed: (
                cipher,
                replace(sealed, fingerprint="0" * 64),
                "tenant-a",
            ),
            id="fingerprint-mismatch",
        ),
        pytest.param(
            lambda cipher, sealed: (
                cipher,
                _authenticated_non_utf8_secret(),
                "tenant-a",
            ),
            id="non-utf8-plaintext",
        ),
    ],
)
def test_open_uses_one_safe_error_for_all_decryption_failures(
    failure_factory: OpenFailureFactory,
) -> None:
    cipher = SecretCipher(MASTER_KEY)
    sealed = cipher.seal("never reveal this", context="tenant-a")
    decrypting_cipher, changed, context = failure_factory(cipher, sealed)

    with pytest.raises(SecretDecryptionError) as captured:
        decrypting_cipher.open(changed, context=context)

    assert type(captured.value) is SecretDecryptionError
    assert str(captured.value) == "secret could not be decrypted"
    assert "never reveal this" not in str(captured.value)
    assert sealed.ciphertext not in str(captured.value)
    assert "retired-key" not in str(captured.value)


def _authenticated_non_utf8_secret() -> SealedSecret:
    nonce = bytes(range(12))
    context = "tenant-a"
    raw_ciphertext = AESGCM(MASTER_KEY).encrypt(
        nonce,
        b"\xff\xfe",
        b"agent-hub-secret-v1\x00" + context.encode("utf-8"),
    )
    return SealedSecret(
        key_id="v1",
        nonce=b64encode(nonce).decode("ascii"),
        ciphertext=b64encode(raw_ciphertext).decode("ascii"),
        fingerprint="0" * 64,
    )


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
