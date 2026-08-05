import asyncio
import traceback
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import agent_hub.security.secrets as secrets_module
from agent_hub.db.models import SecretRow, TenantRow
from agent_hub.security.secrets import (
    SealedSecret,
    SecretCipher,
    SecretDecryptionError,
    SecretNotFoundError,
    SecretReference,
    SecretRepository,
    SecretService,
    SecretValidationError,
)

MASTER_KEY = bytes(range(32))
ROTATED_KEY = b"r" * 32
FINGERPRINT_KEY = b"f" * 32


async def _create_tenant(
    session_factory: async_sessionmaker[AsyncSession], *, slug: str
) -> UUID:
    tenant_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(TenantRow(id=tenant_id, slug=slug, name=slug.title()))
    return tenant_id


@pytest.mark.integration
async def test_same_tenant_deduplicates_but_different_tenants_do_not(
    secret_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a = await _create_tenant(secret_session_factory, slug="secret-a")
    tenant_b = await _create_tenant(secret_session_factory, slug="secret-b")
    actor_id = uuid4()
    service = SecretService(secret_session_factory, SecretCipher(MASTER_KEY))

    first = await service.create_or_get(tenant_a, actor_id, "shared plaintext")
    duplicate = await service.create_or_get(tenant_a, actor_id, "shared plaintext")
    other_tenant = await service.create_or_get(tenant_b, actor_id, "shared plaintext")

    assert first == duplicate
    assert first != other_tenant
    async with secret_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(SecretRow)) == 2
        fingerprints = list(
            await session.scalars(select(SecretRow.fingerprint).order_by(SecretRow.tenant_id))
        )
        assert len(set(fingerprints)) == 2


@pytest.mark.integration
async def test_key_rotation_deduplicates_with_old_row_and_resolves_keyring_versions(
    secret_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _create_tenant(secret_session_factory, slug="rotation")
    v1_service = SecretService(
        secret_session_factory,
        SecretCipher(MASTER_KEY, key_id="v1"),
    )
    old_reference = await v1_service.create_or_get(
        tenant_id, uuid4(), "stable across rotation"
    )
    v2_cipher = SecretCipher(
        ROTATED_KEY,
        key_id="v2",
        decryption_keys={"v1": MASTER_KEY},
        fingerprint_key=MASTER_KEY,
    )
    v2_service = SecretService(secret_session_factory, v2_cipher)

    duplicate_reference = await v2_service.create_or_get(
        tenant_id, uuid4(), "stable across rotation"
    )
    new_reference = await v2_service.create_or_get(
        tenant_id, uuid4(), "created after rotation"
    )

    assert duplicate_reference == old_reference
    assert await v2_service.resolve(tenant_id, old_reference) == "stable across rotation"
    assert await v2_service.resolve(tenant_id, new_reference) == "created after rotation"
    async with secret_session_factory() as session:
        old_key_id = await session.scalar(
            select(SecretRow.key_id).where(SecretRow.id == old_reference.secret_id)
        )
        new_key_id = await session.scalar(
            select(SecretRow.key_id).where(SecretRow.id == new_reference.secret_id)
        )
    assert old_key_id == "v1"
    assert new_key_id == "v2"

    no_old_key_service = SecretService(
        secret_session_factory,
        SecretCipher(
            ROTATED_KEY,
            key_id="v2",
            fingerprint_key=FINGERPRINT_KEY,
        ),
    )
    with pytest.raises(SecretDecryptionError) as captured:
        await no_old_key_service.resolve(tenant_id, old_reference)
    assert str(captured.value) == "secret could not be decrypted"


@pytest.mark.integration
async def test_concurrent_create_or_get_is_atomic(
    secret_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _create_tenant(secret_session_factory, slug="secret-concurrent")
    service = SecretService(secret_session_factory, SecretCipher(MASTER_KEY))

    references = await asyncio.gather(
        *(
            service.create_or_get(tenant_id, uuid4(), "one concurrent secret")
            for _ in range(10)
        )
    )

    assert len(set(references)) == 1
    async with secret_session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(SecretRow).where(SecretRow.tenant_id == tenant_id)
        )
    assert count == 1


@pytest.mark.integration
async def test_resolve_enforces_tenant_isolation_and_hides_missing_details(
    secret_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a = await _create_tenant(secret_session_factory, slug="resolve-a")
    tenant_b = await _create_tenant(secret_session_factory, slug="resolve-b")
    service = SecretService(secret_session_factory, SecretCipher(MASTER_KEY))
    reference = await service.create_or_get(tenant_a, uuid4(), "resolved plaintext")

    assert await service.resolve(tenant_a, reference) == "resolved plaintext"
    assert await service.resolve(tenant_a, reference.secret_id) == "resolved plaintext"
    assert await service.resolve(tenant_a, str(reference)) == "resolved plaintext"

    missing_candidates: list[tuple[UUID, SecretReference | UUID | str]] = [
        (tenant_b, reference),
        (tenant_b, str(reference)),
        (tenant_a, uuid4()),
    ]
    for tenant_id, candidate in missing_candidates:
        with pytest.raises(SecretNotFoundError, match="secret was not found") as captured:
            await service.resolve(tenant_id, candidate)
        assert str(reference.secret_id) not in str(captured.value)
        assert str(tenant_a) not in str(captured.value)


@pytest.mark.integration
async def test_database_contains_only_encrypted_secret_and_ciphertext_is_tenant_bound(
    secret_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a = await _create_tenant(secret_session_factory, slug="encrypted-a")
    tenant_b = await _create_tenant(secret_session_factory, slug="encrypted-b")
    cipher = SecretCipher(MASTER_KEY)
    service = SecretService(secret_session_factory, cipher)
    plaintext = "plaintext-never-in-database"
    reference = await service.create_or_get(tenant_a, uuid4(), plaintext)

    async with secret_session_factory() as session:
        row = await session.scalar(select(SecretRow).where(SecretRow.id == reference.secret_id))
        assert row is not None
        serialized_columns = " ".join(
            str(getattr(row, column.name)) for column in SecretRow.__table__.columns
        )
        assert plaintext not in serialized_columns
        sealed = SealedSecret(
            key_id=row.key_id,
            nonce=row.nonce,
            ciphertext=row.ciphertext,
            fingerprint=row.fingerprint,
        )

    with pytest.raises(SecretDecryptionError):
        cipher.open(sealed, context=str(tenant_b))
    assert cipher.open(sealed, context=str(tenant_a)) == plaintext


@pytest.mark.integration
async def test_resolve_rejects_ciphertext_copied_to_another_tenant(
    secret_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a = await _create_tenant(secret_session_factory, slug="copied-a")
    tenant_b = await _create_tenant(secret_session_factory, slug="copied-b")
    service = SecretService(secret_session_factory, SecretCipher(MASTER_KEY))
    source_reference = await service.create_or_get(
        tenant_a, uuid4(), "tenant-a-only-plaintext"
    )
    copied_id = uuid4()

    async with secret_session_factory() as session, session.begin():
        source = await session.scalar(
            select(SecretRow).where(SecretRow.id == source_reference.secret_id)
        )
        assert source is not None
        session.add(
            SecretRow(
                id=copied_id,
                tenant_id=tenant_b,
                fingerprint="0" * 64,
                key_id=source.key_id,
                nonce=source.nonce,
                ciphertext=source.ciphertext,
                created_by=uuid4(),
            )
        )

    copied_reference = SecretReference(tenant_id=tenant_b, secret_id=copied_id)
    with pytest.raises(SecretDecryptionError) as captured:
        await service.resolve(tenant_b, copied_reference)

    assert type(captured.value) is SecretDecryptionError
    assert str(captured.value) == "secret could not be decrypted"
    assert "tenant-a-only-plaintext" not in str(captured.value)


@pytest.mark.integration
async def test_create_or_get_rejects_unpaired_surrogate_without_writing_a_row(
    secret_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _create_tenant(secret_session_factory, slug="surrogate")
    service = SecretService(secret_session_factory, SecretCipher(MASTER_KEY))
    plaintext = "private-\ud800-value"

    with pytest.raises(SecretValidationError) as captured:
        await service.create_or_get(tenant_id, uuid4(), plaintext)

    assert str(captured.value) == "secret could not be encoded"
    assert "private" not in str(captured.value)
    _assert_production_frames_hide_markers(captured.value, ("private",))
    async with secret_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(SecretRow)) == 0


@pytest.mark.integration
async def test_persistence_failure_is_sanitized_and_rolls_back(
    secret_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_tenant_id = uuid4()
    cipher = SecretCipher(MASTER_KEY)
    plaintext = "synthetic-sensitive-plaintext-marker"
    monkeypatch.setattr(
        "agent_hub.security.secrets.secrets.token_bytes",
        lambda size: b"\x01" * size,
    )
    expected = cipher.seal(plaintext, context=str(missing_tenant_id))
    service = SecretService(secret_session_factory, cipher)

    with pytest.raises(Exception) as captured:
        await service.create_or_get(missing_tenant_id, uuid4(), plaintext)

    persistence_error_type = getattr(
        secrets_module, "SecretPersistenceError", None
    )
    assert persistence_error_type is not None
    assert type(captured.value) is persistence_error_type
    assert str(captured.value) == "secret could not be stored"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    exposed = " ".join(
        [
            str(captured.value),
            repr(captured.value),
            "".join(traceback.format_exception(captured.value)),
        ]
    )
    sensitive_markers = (
        plaintext,
        expected.fingerprint,
        expected.nonce,
        expected.ciphertext,
    )
    assert all(marker not in exposed for marker in sensitive_markers)
    _assert_production_frames_hide_markers(captured.value, sensitive_markers)
    async with secret_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(SecretRow)) == 0


@pytest.mark.integration
async def test_repository_translates_orm_row_to_immutable_redacted_record(
    secret_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _create_tenant(secret_session_factory, slug="repository-boundary")
    service = SecretService(secret_session_factory, SecretCipher(MASTER_KEY))
    reference = await service.create_or_get(tenant_id, uuid4(), "repository secret")

    async with secret_session_factory() as session:
        stored = await SecretRepository().get(
            session,
            tenant_id=tenant_id,
            secret_id=reference.secret_id,
        )

    assert stored is not None
    assert not isinstance(stored, SecretRow)
    assert stored.secret_id == reference.secret_id
    assert stored.tenant_id == tenant_id
    assert isinstance(stored.sealed, SealedSecret)
    with pytest.raises(AttributeError):
        stored.secret_id = uuid4()  # type: ignore[misc]
    rendered = repr(stored)
    assert stored.sealed.nonce not in rendered
    assert stored.sealed.fingerprint not in rendered
    assert stored.sealed.ciphertext not in rendered


@pytest.mark.integration
async def test_database_unique_constraint_is_final_deduplication_defense(
    secret_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _create_tenant(secret_session_factory, slug="unique-defense")
    sealed = SecretCipher(MASTER_KEY).seal("duplicate", context=str(tenant_id))

    async with secret_session_factory() as session:
        session.add_all(
            [
                SecretRow(
                    tenant_id=tenant_id,
                    fingerprint=sealed.fingerprint,
                    key_id=sealed.key_id,
                    nonce=sealed.nonce,
                    ciphertext=sealed.ciphertext,
                ),
                SecretRow(
                    tenant_id=tenant_id,
                    fingerprint=sealed.fingerprint,
                    key_id=sealed.key_id,
                    nonce=replace(sealed, nonce="AAAAAAAAAAAAAAAA").nonce,
                    ciphertext=sealed.ciphertext,
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.commit()


def _assert_production_frames_hide_markers(
    error: BaseException,
    markers: tuple[str, ...],
) -> None:
    exposed: list[str] = []
    current_traceback = error.__traceback__
    while current_traceback is not None:
        frame = current_traceback.tb_frame
        module_name = str(frame.f_globals.get("__name__", ""))
        if module_name.startswith("agent_hub"):
            exposed.extend(
                f"{name}={value!r}" for name, value in frame.f_locals.items()
            )
        current_traceback = current_traceback.tb_next
    rendered = " ".join(exposed)
    assert all(marker not in rendered for marker in markers)
