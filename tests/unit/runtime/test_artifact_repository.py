import asyncio
from uuid import UUID, uuid4

import pytest

from agent_hub.runtime.artifacts import (
    ArtifactReference,
    ArtifactRepositoryError,
    InMemoryArtifactRepository,
)
from agent_hub.runtime.contracts import Artifact

TENANT_ID = UUID("00000000-0000-4000-8000-000000000031")
RUN_ID = UUID("00000000-0000-4000-8000-000000000032")


def artifact(text: str) -> Artifact:
    return Artifact(
        id=uuid4(),
        type="text",
        producer="writer",
        content={"text": text},
    )


async def test_repository_round_trips_only_inside_tenant_run_scope() -> None:
    repository = InMemoryArtifactRepository()
    stored = artifact("safe")
    reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)
    await repository.put(TENANT_ID, RUN_ID, stored)

    assert await repository.get_many(TENANT_ID, RUN_ID, (reference,)) == (stored,)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.get_many(uuid4(), RUN_ID, (reference,))
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.get_many(TENANT_ID, uuid4(), (reference,))


async def test_repository_rejects_hash_mismatch_without_returning_content() -> None:
    repository = InMemoryArtifactRepository()
    stored = artifact("safe")
    await repository.put(TENANT_ID, RUN_ID, stored)
    reference = ArtifactReference(id=stored.id, sha256="0" * 64)

    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.get_many(TENANT_ID, RUN_ID, (reference,))


async def test_repository_capacity_failure_keeps_prior_writes_readable() -> None:
    repository = InMemoryArtifactRepository(max_artifacts_per_run=1)
    first = artifact("first")
    second = artifact("second")
    first_reference = ArtifactReference(id=first.id, sha256=first.content_sha256)
    await repository.put(TENANT_ID, RUN_ID, first)

    with pytest.raises(ArtifactRepositoryError, match="capacity"):
        await repository.put(TENANT_ID, RUN_ID, second)

    assert await repository.get_many(TENANT_ID, RUN_ID, (first_reference,)) == (first,)


async def test_repository_total_byte_limit_is_atomic() -> None:
    first = artifact("first")
    repository = InMemoryArtifactRepository(
        max_total_bytes_per_run=len(str(first.to_payload()).encode("utf-8")) + 128
    )
    await repository.put(TENANT_ID, RUN_ID, first)
    second = artifact("x" * 256)

    with pytest.raises(ArtifactRepositoryError, match="capacity"):
        await repository.put(TENANT_ID, RUN_ID, second)

    reference = ArtifactReference(id=first.id, sha256=first.content_sha256)
    assert await repository.get_many(TENANT_ID, RUN_ID, (reference,)) == (first,)


async def test_repository_rejects_duplicate_batch_references() -> None:
    repository = InMemoryArtifactRepository()
    stored = artifact("safe")
    reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)
    await repository.put(TENANT_ID, RUN_ID, stored)

    with pytest.raises(ArtifactRepositoryError, match="references"):
        await repository.get_many(TENANT_ID, RUN_ID, (reference, reference))


async def test_repository_accepts_idempotent_second_put_without_using_capacity() -> None:
    repository = InMemoryArtifactRepository(max_artifacts_per_run=1)
    stored = artifact("safe")
    reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)

    await repository.put(TENANT_ID, RUN_ID, stored)
    await repository.put(TENANT_ID, RUN_ID, stored)

    assert await repository.get_many(TENANT_ID, RUN_ID, (reference,)) == (stored,)


async def test_conditional_rollback_removes_only_the_matching_write_owner() -> None:
    repository = InMemoryArtifactRepository()
    stored = artifact("safe")
    reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)
    first_owner = uuid4()
    second_owner = uuid4()

    await repository.put(TENANT_ID, RUN_ID, stored, write_id=first_owner)
    await repository.put(TENANT_ID, RUN_ID, stored, write_id=second_owner)

    assert not await repository.delete_if_hash(
        TENANT_ID, RUN_ID, reference, write_id=first_owner
    )
    assert await repository.get_many(TENANT_ID, RUN_ID, (reference,)) == (stored,)
    assert await repository.delete_if_hash(
        TENANT_ID, RUN_ID, reference, write_id=second_owner
    )
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.get_many(TENANT_ID, RUN_ID, (reference,))


async def test_conditional_rollback_never_deletes_a_permanent_or_hash_mismatched_write() -> None:
    repository = InMemoryArtifactRepository()
    stored = artifact("safe")
    reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)
    owner = uuid4()
    await repository.put(TENANT_ID, RUN_ID, stored)
    await repository.put(TENANT_ID, RUN_ID, stored, write_id=owner)

    assert not await repository.delete_if_hash(
        TENANT_ID,
        RUN_ID,
        ArtifactReference(id=stored.id, sha256="0" * 64),
        write_id=owner,
    )
    assert not await repository.delete_if_hash(
        TENANT_ID, RUN_ID, reference, write_id=owner
    )
    assert await repository.get_many(TENANT_ID, RUN_ID, (reference,)) == (stored,)


async def test_aborted_reservation_fences_a_late_put() -> None:
    repository = InMemoryArtifactRepository()
    stored = artifact("late")
    reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)
    write_id = uuid4()

    await repository.reserve_write(TENANT_ID, RUN_ID, reference, write_id=write_id)
    assert await repository.abort_write(
        TENANT_ID, RUN_ID, reference, write_id=write_id
    )
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.put(TENANT_ID, RUN_ID, stored, write_id=write_id)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.get_many(TENANT_ID, RUN_ID, (reference,))


async def test_put_abort_race_never_leaves_an_uncommitted_artifact() -> None:
    repository = InMemoryArtifactRepository()
    for index in range(32):
        stored = artifact(f"race-{index}")
        reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)
        write_id = uuid4()
        await repository.reserve_write(TENANT_ID, RUN_ID, reference, write_id=write_id)

        await asyncio.gather(
            repository.put(TENANT_ID, RUN_ID, stored, write_id=write_id),
            repository.abort_write(TENANT_ID, RUN_ID, reference, write_id=write_id),
            return_exceptions=True,
        )

        with pytest.raises(ArtifactRepositoryError, match="unavailable"):
            await repository.get_many(TENANT_ID, RUN_ID, (reference,))


async def test_committed_or_other_owned_artifact_cannot_be_removed_by_abort() -> None:
    repository = InMemoryArtifactRepository()
    stored = artifact("shared")
    reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)
    first_owner = uuid4()
    committed_owner = uuid4()
    await repository.reserve_write(
        TENANT_ID, RUN_ID, reference, write_id=first_owner
    )
    await repository.reserve_write(
        TENANT_ID, RUN_ID, reference, write_id=committed_owner
    )
    await repository.put(TENANT_ID, RUN_ID, stored, write_id=first_owner)
    await repository.put(TENANT_ID, RUN_ID, stored, write_id=committed_owner)
    await repository.commit_write(
        TENANT_ID, RUN_ID, reference, write_id=committed_owner
    )

    assert not await repository.abort_write(
        TENANT_ID, RUN_ID, reference, write_id=first_owner
    )
    assert not await repository.abort_write(
        TENANT_ID, RUN_ID, reference, write_id=committed_owner
    )
    assert await repository.get_many(TENANT_ID, RUN_ID, (reference,)) == (stored,)
