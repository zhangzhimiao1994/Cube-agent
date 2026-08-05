import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import SecretRow, TenantRow
from agent_hub.security.secrets import (
    SealedSecret,
    SecretCipher,
    SecretDecryptionError,
    SecretNotFoundError,
    SecretReference,
    SecretService,
)

MASTER_KEY = bytes(range(32))


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
