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


async def test_repository_accepts_canonical_uuid_strings_at_runtime_boundaries() -> None:
    repository = InMemoryArtifactRepository()
    stored = artifact("safe")
    reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)

    await repository.put(str(TENANT_ID), str(RUN_ID), stored)  # type: ignore[arg-type]

    assert await repository.get_many(str(TENANT_ID), str(RUN_ID), (reference,)) == (  # type: ignore[arg-type]
        stored,
    )
    with pytest.raises(ArtifactRepositoryError, match="scope"):
        await repository.get_many("not-a-uuid", str(RUN_ID), (reference,))  # type: ignore[arg-type]


async def test_repository_accepts_uuid_like_database_values_at_runtime_boundaries() -> None:
    class DatabaseUUID:
        def __init__(self, value: UUID) -> None:
            self._value = value

        def __str__(self) -> str:
            return str(self._value)

    repository = InMemoryArtifactRepository()
    stored = artifact("safe")
    reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)

    await repository.put(DatabaseUUID(TENANT_ID), DatabaseUUID(RUN_ID), stored)  # type: ignore[arg-type]

    assert await repository.get_many(
        DatabaseUUID(TENANT_ID),  # type: ignore[arg-type]
        DatabaseUUID(RUN_ID),  # type: ignore[arg-type]
        (reference,),
    ) == (stored,)


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

    assert not await repository.abort_write(TENANT_ID, RUN_ID, reference, write_id=first_owner)
    assert await repository.get_many(TENANT_ID, RUN_ID, (reference,)) == (stored,)
    assert await repository.abort_write(TENANT_ID, RUN_ID, reference, write_id=second_owner)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.get_many(TENANT_ID, RUN_ID, (reference,))


async def test_conditional_rollback_never_deletes_a_permanent_or_hash_mismatched_write() -> None:
    repository = InMemoryArtifactRepository()
    stored = artifact("safe")
    reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)
    owner = uuid4()
    await repository.put(TENANT_ID, RUN_ID, stored)
    await repository.put(TENANT_ID, RUN_ID, stored, write_id=owner)

    assert not await repository.abort_write(
        TENANT_ID,
        RUN_ID,
        ArtifactReference(id=stored.id, sha256="0" * 64),
        write_id=owner,
    )
    assert not await repository.abort_write(TENANT_ID, RUN_ID, reference, write_id=owner)
    assert await repository.get_many(TENANT_ID, RUN_ID, (reference,)) == (stored,)


async def test_aborted_reservation_fences_a_late_put() -> None:
    repository = InMemoryArtifactRepository()
    stored = artifact("late")
    reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)
    write_id = uuid4()

    await repository.reserve_write(TENANT_ID, RUN_ID, reference, write_id=write_id)
    assert await repository.abort_write(TENANT_ID, RUN_ID, reference, write_id=write_id)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.put(TENANT_ID, RUN_ID, stored, write_id=write_id)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.get_many(TENANT_ID, RUN_ID, (reference,))


async def test_put_abort_race_never_leaves_an_uncommitted_artifact() -> None:
    repository = InMemoryArtifactRepository()
    for index in range(64):
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


async def test_other_owned_or_permanent_artifact_cannot_be_removed_by_abort() -> None:
    repository = InMemoryArtifactRepository()
    stored = artifact("shared")
    reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)
    first_owner = uuid4()
    second_owner = uuid4()
    await repository.reserve_write(TENANT_ID, RUN_ID, reference, write_id=first_owner)
    await repository.reserve_write(TENANT_ID, RUN_ID, reference, write_id=second_owner)
    await repository.put(TENANT_ID, RUN_ID, stored, write_id=first_owner)
    await repository.put(TENANT_ID, RUN_ID, stored, write_id=second_owner)
    await repository.put(TENANT_ID, RUN_ID, stored)

    assert not await repository.abort_write(TENANT_ID, RUN_ID, reference, write_id=first_owner)
    assert not await repository.abort_write(TENANT_ID, RUN_ID, reference, write_id=second_owner)
    assert await repository.get_many(TENANT_ID, RUN_ID, (reference,)) == (stored,)


async def test_find_references_filters_scope_and_labels_without_exposing_or_changing_bodies() -> (
    None
):
    repository = InMemoryArtifactRepository()
    stored = Artifact(
        id=uuid4(),
        type="python_resolution_checkpoint",
        producer="python_resolution",
        content={"needs": ["PRIVATE_BODY_SENTINEL"], "environment": "safe-data"},
    )
    await repository.put(TENANT_ID, RUN_ID, stored)
    for tenant, run, kind, producer in (
        (uuid4(), RUN_ID, stored.type, stored.producer),
        (TENANT_ID, uuid4(), stored.type, stored.producer),
        (TENANT_ID, RUN_ID, "other_kind", stored.producer),
        (TENANT_ID, RUN_ID, stored.type, "other_producer"),
    ):
        await repository.put(
            tenant,
            run,
            Artifact(
                id=uuid4(), type=kind, producer=producer, content={"value": "foreign-private"}
            ),
        )
    refs = await repository.find_references(
        TENANT_ID, RUN_ID, type=stored.type, producer=stored.producer
    )
    assert refs == (ArtifactReference(id=stored.id, sha256=stored.content_sha256),)
    assert set(refs[0].to_payload()) == {"id", "sha256"}
    assert "PRIVATE" not in str(refs[0].to_payload())
    assert await repository.get_many(TENANT_ID, RUN_ID, refs) == (stored,)
    assert (
        await repository.find_references(
            TENANT_ID, RUN_ID, type="missing", producer=stored.producer
        )
        == ()
    )
    assert (
        await repository.find_references(
            uuid4(), RUN_ID, type=stored.type, producer=stored.producer
        )
        == ()
    )
    assert await repository.get_many(TENANT_ID, RUN_ID, refs) == (stored,)


async def test_find_references_pages_beyond_default_128_with_stable_uuid_keyset() -> None:
    repository = InMemoryArtifactRepository()
    stored = [
        Artifact(id=UUID(int=index), type="checkpoint", producer="owner", content={"n": index})
        for index in range(1, 260)
    ]
    for value in reversed(stored):
        await repository.put(TENANT_ID, RUN_ID, value)
    pages = []
    cursor = None
    while True:
        page = await repository.find_references(
            TENANT_ID, RUN_ID, type="checkpoint", producer="owner", after=cursor
        )
        if not page:
            break
        pages.append(page)
        cursor = page[-1].id
    assert [len(page) for page in pages] == [128, 128, 3]
    refs = tuple(ref for page in pages for ref in page)
    assert [ref.id for ref in refs] == [value.id for value in stored]
    assert await repository.get_many(TENANT_ID, RUN_ID, refs) == tuple(stored)
    assert await repository.find_references(
        TENANT_ID, RUN_ID, type="checkpoint", producer="owner", limit=1, after=UUID(int=127)
    ) == (refs[127],)


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", True),
        ("type", None),
        ("type", ""),
        ("type", "bad space"),
        ("type", "x" * 129),
        ("producer", False),
        ("producer", "bad\x00name"),
        ("producer", "*"),
        ("limit", True),
        ("limit", False),
        ("limit", 0),
        ("limit", -1),
        ("limit", 1.0),
        ("limit", "1"),
        ("limit", 3),
        ("after", True),
        ("after", str(uuid4())),
        ("after", 1),
    ],
)
async def test_find_references_rejects_invalid_query_without_mutating_storage(
    field: str, value: object
) -> None:
    repository = InMemoryArtifactRepository(max_batch_size=2)
    stored = artifact("unchanged")
    await repository.put(TENANT_ID, RUN_ID, stored)
    query = {"type": stored.type, "producer": stored.producer, "limit": 2, field: value}
    with pytest.raises(ArtifactRepositoryError, match="query"):
        await repository.find_references(TENANT_ID, RUN_ID, **query)
    refs = (ArtifactReference(id=stored.id, sha256=stored.content_sha256),)
    assert await repository.get_many(TENANT_ID, RUN_ID, refs) == (stored,)


async def test_find_references_tracks_actual_put_and_abort_without_creating_a_write() -> None:
    repository = InMemoryArtifactRepository(max_batch_size=2)
    stored = artifact("owned")
    ref = ArtifactReference(id=stored.id, sha256=stored.content_sha256)
    write_id = uuid4()
    await repository.reserve_write(TENANT_ID, RUN_ID, ref, write_id=write_id)
    query = {"type": stored.type, "producer": stored.producer, "limit": 2}
    assert await repository.find_references(TENANT_ID, RUN_ID, **query) == ()
    await repository.put(TENANT_ID, RUN_ID, stored, write_id=write_id)
    assert await repository.find_references(TENANT_ID, RUN_ID, **query) == (ref,)
    assert await repository.abort_write(TENANT_ID, RUN_ID, ref, write_id=write_id)
    assert await repository.find_references(TENANT_ID, RUN_ID, **query) == ()
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.put(TENANT_ID, RUN_ID, stored, write_id=write_id)


async def test_find_references_keeps_legal_nul_content_readable() -> None:
    repository = InMemoryArtifactRepository()
    value = Artifact(
        id=UUID(int=1), type="checkpoint", producer="owner", content={"value": "left\x00right"}
    )
    await repository.put(TENANT_ID, RUN_ID, value)
    refs = await repository.find_references(TENANT_ID, RUN_ID, type="checkpoint", producer="owner")
    assert refs == (ArtifactReference(id=value.id, sha256=value.content_sha256),)
    assert await repository.get_many(TENANT_ID, RUN_ID, refs) == (value,)
