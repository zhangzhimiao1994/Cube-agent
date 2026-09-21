"""Real PostgreSQL contract tests for private runtime artifacts; no memory fallback."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import import_module
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import MetaData, delete, func, select, text, update
from sqlalchemy import event as sql_event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.util import await_only

from agent_hub.db.models import RunRow, RuntimeArtifactRow, RuntimeArtifactWriteRow
from agent_hub.db.session import Database, build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunNotFound, RunRepository
from agent_hub.runtime.artifacts import (
    ArtifactReference,
    ArtifactRepository,
    ArtifactRepositoryError,
)
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent


def private_artifact(text: str = "private-runtime-payload", *, artifact_id: UUID | None = None) -> Artifact:
    return Artifact(
        id=artifact_id or uuid4(), type="text", producer="worker", content={"text": text},
    )


def reference(artifact: Artifact) -> ArtifactReference:
    return ArtifactReference(id=artifact.id, sha256=artifact.content_sha256)


def encoded_size(artifact: Artifact) -> int:
    return len(json.dumps(
        artifact.to_payload(), ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))


def repository(database: Database, **limits: int) -> ArtifactRepository:
    # Deferred import permits collection before implementation, never a skip/fallback.
    module = import_module("agent_hub.runs.artifacts")
    constructor = cast(Callable[..., ArtifactRepository], module.PostgresArtifactRepository)
    return constructor(database.session_factory, **limits)


@dataclass(frozen=True)
class ArtifactStore:
    database: Database
    database_url: str
    tenant_id: UUID
    run_id: UUID
    other_run_id: UUID
    foreign_tenant_id: UUID
    foreign_run_id: UUID


@pytest.fixture
async def store(database_url: str) -> AsyncIterator[ArtifactStore]:
    database = build_database(database_url)
    assert database.engine.dialect.name == "postgresql", "These tests require real PostgreSQL"
    runs = RunRepository(database.session_factory)
    tenant, foreign_tenant = uuid4(), uuid4()
    created: list[tuple[UUID, UUID]] = []
    try:
        for owner in (tenant, tenant, foreign_tenant):
            record = await runs.create_run(
                tenant_id=owner, actor_id=uuid4(), request="Private artifact repository contract",
                mode=TaskMode.DISPATCH, status=RunStatus.COMPLETED,
                idempotency_key=None, enqueue=False,
            )
            created.append((owner, record.id))
        yield ArtifactStore(
            database, database_url, tenant, created[0][1], created[1][1],
            foreign_tenant, created[2][1],
        )
    finally:
        try:
            for owner, run_id in created:
                try:
                    await runs.delete_run(owner, run_id)
                except RunNotFound:
                    pass
        finally:
            await database.dispose()


async def test_fresh_repository_and_connection_pool_hydrate_full_payload(store: ArtifactStore) -> None:
    artifact = Artifact(
        id=uuid4(), type="model_response", producer="worker", version=2,
        content={"text": "private nested payload", "items": ({"flag": True, "value": None},)},
        source_ids=(str(uuid4()),),
    )
    await repository(store.database).put(store.tenant_id, store.run_id, artifact, write_id=uuid4())
    fresh_database = build_database(store.database_url)
    try:
        hydrated = await repository(fresh_database).get_many(
            store.tenant_id, store.run_id, (reference(artifact),),
        )
        assert len(hydrated) == 1 and hydrated[0] is not artifact
        assert hydrated[0].to_payload() == artifact.to_payload()
        assert hydrated[0].recompute_content_sha256() == artifact.content_sha256
    finally:
        await fresh_database.dispose()


async def test_same_hash_different_ids_survive_ordered_hydration(store: ArtifactStore) -> None:
    first, second = private_artifact(), private_artifact()
    assert first.id != second.id and first.content_sha256 == second.content_sha256
    for artifact in (first, second):
        await repository(store.database).put(store.tenant_id, store.run_id, artifact)
    hydrated = await repository(store.database).get_many(
        store.tenant_id, store.run_id, (reference(second), reference(first)),
    )
    assert tuple(item.to_payload() for item in hydrated) == (second.to_payload(), first.to_payload())


async def test_same_id_different_hash_is_rejected_without_overwriting(store: ArtifactStore) -> None:
    first = private_artifact()
    changed = private_artifact("different private payload", artifact_id=first.id)
    await repository(store.database).put(store.tenant_id, store.run_id, first)
    await repository(store.database).put(store.tenant_id, store.run_id, first)
    with pytest.raises(ArtifactRepositoryError, match="unavailable") as error:
        await repository(store.database).put(store.tenant_id, store.run_id, changed)
    assert "different private payload" not in str(error.value) + repr(error.value)
    assert await repository(store.database).get_many(
        store.tenant_id, store.run_id, (reference(first),),
    ) == (first,)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository(store.database).get_many(store.tenant_id, store.run_id, (reference(changed),))


async def test_concurrent_conflicting_puts_commit_exactly_one_identity(store: ArtifactStore) -> None:
    first = private_artifact("candidate one")
    second = private_artifact("candidate two", artifact_id=first.id)
    results = await asyncio.gather(
        repository(store.database).put(store.tenant_id, store.run_id, first),
        repository(store.database).put(store.tenant_id, store.run_id, second),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, ArtifactRepositoryError) for result in results) == 1
    winner, loser = (first, second) if results[0] is None else (second, first)
    assert await repository(store.database).get_many(
        store.tenant_id, store.run_id, (reference(winner),),
    ) == (winner,)
    with pytest.raises(ArtifactRepositoryError):
        await repository(store.database).get_many(store.tenant_id, store.run_id, (reference(loser),))


async def test_tenant_and_run_scopes_isolate_reads_and_write_ownership(store: ArtifactStore) -> None:
    artifact = private_artifact()
    owner = uuid4()
    await repository(store.database).put(store.tenant_id, store.run_id, artifact, write_id=owner)
    for tenant, run_id in (
        (store.tenant_id, store.other_run_id),
        (store.foreign_tenant_id, store.foreign_run_id),
        (store.foreign_tenant_id, store.run_id),
    ):
        with pytest.raises(ArtifactRepositoryError):
            await repository(store.database).get_many(tenant, run_id, (reference(artifact),))
    with pytest.raises(ArtifactRepositoryError):
        await repository(store.database).put(store.foreign_tenant_id, store.run_id, artifact)
    with pytest.raises(ArtifactRepositoryError):
        await repository(store.database).reserve_write(
            store.foreign_tenant_id, store.run_id, reference(artifact), write_id=owner,
        )
    with pytest.raises(ArtifactRepositoryError):
        await repository(store.database).abort_write(
            store.foreign_tenant_id, store.run_id, reference(artifact), write_id=owner,
        )

    # Identical artifact/write IDs in valid scopes must not collide or share tombstones.
    for tenant, run_id in (
        (store.tenant_id, store.other_run_id), (store.foreign_tenant_id, store.foreign_run_id),
    ):
        scoped = private_artifact(str(run_id), artifact_id=artifact.id)
        await repository(store.database).put(tenant, run_id, scoped, write_id=owner)
        assert await repository(store.database).abort_write(
            tenant, run_id, reference(scoped), write_id=owner,
        )
    assert await repository(store.database).get_many(
        store.tenant_id, store.run_id, (reference(artifact),),
    ) == (artifact,)


async def test_private_put_never_enters_public_or_raw_run_artifacts_or_events(store: ArtifactStore) -> None:
    runs = RunRepository(store.database.session_factory)
    public = private_artifact("public accepted result")
    event = RunEvent(
        kind=EventKind.ARTIFACT_CREATED, sequence=1, run_id=store.run_id, artifact=public,
    )
    async with store.database.session_factory() as session, session.begin():
        await runs.persist_event(session, tenant_id=store.tenant_id, run_id=store.run_id, event=event)
    before_artifacts = await runs.artifacts(store.tenant_id, store.run_id)
    before_raw = await runs.raw_artifacts(store.tenant_id, store.run_id)
    before_events = await runs.events(store.tenant_id, store.run_id)
    before_raw_events = await runs.raw_events(store.tenant_id, store.run_id)
    assert before_artifacts and before_raw and before_events and before_raw_events
    marker = "PRIVATE_RUNTIME_ONLY_" + uuid4().hex
    private = private_artifact(marker)
    await repository(store.database).put(store.tenant_id, store.run_id, private, write_id=uuid4())
    assert await runs.artifact_ids(store.tenant_id, store.run_id) == (public.id,)
    assert await runs.artifacts(store.tenant_id, store.run_id) == before_artifacts
    assert await runs.raw_artifacts(store.tenant_id, store.run_id) == before_raw
    assert await runs.events(store.tenant_id, store.run_id) == before_events
    assert await runs.raw_events(store.tenant_id, store.run_id) == before_raw_events
    assert marker not in json.dumps((before_artifacts, before_raw, before_events), default=str)
    assert await repository(store.database).get_many(
        store.tenant_id, store.run_id, (reference(private),),
    ) == (private,)


@pytest.mark.parametrize("phase", ["unreserved", "reserved", "written"])
async def test_abort_tombstone_fences_late_put_across_instances(store: ArtifactStore, phase: str) -> None:
    artifact, owner = private_artifact(), uuid4()
    if phase == "reserved":
        await repository(store.database).reserve_write(
            store.tenant_id, store.run_id, reference(artifact), write_id=owner,
        )
    elif phase == "written":
        await repository(store.database).put(store.tenant_id, store.run_id, artifact, write_id=owner)
    assert await repository(store.database).abort_write(
        store.tenant_id, store.run_id, reference(artifact), write_id=owner,
    )
    assert not await repository(store.database).abort_write(
        store.tenant_id, store.run_id, reference(artifact), write_id=owner,
    )
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository(store.database).put(store.tenant_id, store.run_id, artifact, write_id=owner)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository(store.database).reserve_write(
            store.tenant_id, store.run_id, reference(artifact), write_id=owner,
        )
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository(store.database).get_many(store.tenant_id, store.run_id, (reference(artifact),))


async def test_aborting_one_of_two_written_owners_preserves_the_other(store: ArtifactStore) -> None:
    artifact, first_owner, second_owner = private_artifact(), uuid4(), uuid4()
    for owner in (first_owner, second_owner):
        await repository(store.database).put(store.tenant_id, store.run_id, artifact, write_id=owner)
    assert not await repository(store.database).abort_write(
        store.tenant_id, store.run_id, reference(artifact), write_id=first_owner,
    )
    assert await repository(store.database).get_many(
        store.tenant_id, store.run_id, (reference(artifact),),
    ) == (artifact,)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository(store.database).put(store.tenant_id, store.run_id, artifact, write_id=first_owner)
    assert await repository(store.database).abort_write(
        store.tenant_id, store.run_id, reference(artifact), write_id=second_owner,
    )
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository(store.database).get_many(store.tenant_id, store.run_id, (reference(artifact),))


async def test_owned_abort_cannot_remove_a_permanent_put(store: ArtifactStore) -> None:
    artifact, owner = private_artifact(), uuid4()
    await repository(store.database).put(store.tenant_id, store.run_id, artifact, write_id=owner)
    await repository(store.database).put(store.tenant_id, store.run_id, artifact)
    assert not await repository(store.database).abort_write(
        store.tenant_id, store.run_id, reference(artifact), write_id=owner,
    )
    assert await repository(store.database).get_many(
        store.tenant_id, store.run_id, (reference(artifact),),
    ) == (artifact,)


@pytest.mark.parametrize("via_repository", [False, True])
async def test_run_deletion_removes_private_rows_and_tombstones_by_cascade(
    store: ArtifactStore, via_repository: bool,
) -> None:
    artifact, aborted = private_artifact(), private_artifact("aborted")
    await repository(store.database).put(store.tenant_id, store.run_id, artifact, write_id=uuid4())
    assert await repository(store.database).abort_write(
        store.tenant_id, store.run_id, reference(aborted), write_id=uuid4(),
    )
    await repository(store.database).put(store.tenant_id, store.other_run_id, artifact)
    metadata = MetaData()
    async with store.database.engine.connect() as connection:
        await connection.run_sync(metadata.reflect)
    scoped_tables = [table for table in metadata.tables.values() if "run_id" in table.c]
    async with store.database.session_factory() as session:
        occupied = [table for table in scoped_tables if await session.scalar(
            select(func.count()).select_from(table).where(table.c.run_id == store.run_id),
        )]
    assert occupied, "Private writes must persist real scoped rows before cascade is exercised"
    if via_repository:
        await RunRepository(store.database.session_factory).delete_run(store.tenant_id, store.run_id)
    else:
        async with store.database.session_factory() as session, session.begin():
            await session.execute(delete(RunRow).where(RunRow.id == store.run_id))
    async with store.database.session_factory() as session:
        for table in scoped_tables:
            assert await session.scalar(
                select(func.count()).select_from(table).where(table.c.run_id == store.run_id),
            ) == 0, f"Run deletion left scoped rows in {table.name}"
    with pytest.raises(ArtifactRepositoryError):
        await repository(store.database).get_many(store.tenant_id, store.run_id, (reference(artifact),))
    with pytest.raises(ArtifactRepositoryError):
        await repository(store.database).put(store.tenant_id, store.run_id, artifact)
    assert await repository(store.database).get_many(
        store.tenant_id, store.other_run_id, (reference(artifact),),
    ) == (artifact,)


@pytest.mark.parametrize("limit", ["max_artifacts_per_run", "max_total_bytes_per_run"])
async def test_per_run_capacity_is_shared_idempotent_and_reclaimed_after_last_abort(
    store: ArtifactStore, limit: str,
) -> None:
    first, second, owner = private_artifact("first"), private_artifact("other"), uuid4()
    limits = {limit: 1 if limit == "max_artifacts_per_run" else encoded_size(first)}
    for _ in range(2):
        await repository(store.database, **limits).put(store.tenant_id, store.run_id, first, write_id=owner)
    with pytest.raises(ArtifactRepositoryError, match="capacity"):
        await repository(store.database, **limits).put(store.tenant_id, store.run_id, second)
    assert await repository(store.database, **limits).get_many(
        store.tenant_id, store.run_id, (reference(first),),
    ) == (first,)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository(store.database, **limits).get_many(store.tenant_id, store.run_id, (reference(second),))
    await repository(store.database, **limits).put(store.tenant_id, store.other_run_id, second)
    assert await repository(store.database, **limits).abort_write(
        store.tenant_id, store.run_id, reference(first), write_id=owner,
    )
    await repository(store.database, **limits).put(store.tenant_id, store.run_id, second)
    assert await repository(store.database, **limits).get_many(
        store.tenant_id, store.run_id, (reference(second),),
    ) == (second,)


async def test_concurrent_capacity_check_cannot_overfill_one_run(store: ArtifactStore) -> None:
    artifacts = (private_artifact("one"), private_artifact("two"))
    results = await asyncio.gather(*(
        repository(store.database, max_artifacts_per_run=1).put(store.tenant_id, store.run_id, artifact)
        for artifact in artifacts
    ), return_exceptions=True)
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, ArtifactRepositoryError) for result in results) == 1
    for artifact, result in zip(artifacts, results, strict=True):
        if result is None:
            assert await repository(store.database).get_many(
                store.tenant_id, store.run_id, (reference(artifact),),
            ) == (artifact,)
        else:
            with pytest.raises(ArtifactRepositoryError, match="unavailable"):
                await repository(store.database).get_many(store.tenant_id, store.run_id, (reference(artifact),))


async def test_artifact_utf8_byte_limit_accepts_exact_boundary_only(store: ArtifactStore) -> None:
    artifact = private_artifact("\u4e2d" * 50)
    size = encoded_size(artifact)
    with pytest.raises(ArtifactRepositoryError, match="capacity"):
        await repository(store.database, max_artifact_bytes=size - 1).put(store.tenant_id, store.run_id, artifact)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository(store.database).get_many(store.tenant_id, store.run_id, (reference(artifact),))
    await repository(store.database, max_artifact_bytes=size).put(store.tenant_id, store.run_id, artifact)
    assert await repository(store.database).get_many(
        store.tenant_id, store.run_id, (reference(artifact),),
    ) == (artifact,)


async def test_batch_limits_duplicates_and_wrong_hash_fail_closed(store: ArtifactStore) -> None:
    first, second = private_artifact("one"), private_artifact("two")
    for artifact in (first, second):
        await repository(store.database).put(store.tenant_id, store.run_id, artifact)
    reader = repository(store.database, max_batch_size=1)
    assert await reader.get_many(store.tenant_id, store.run_id, ()) == ()
    assert await reader.get_many(store.tenant_id, store.run_id, (reference(first),)) == (first,)
    with pytest.raises(ArtifactRepositoryError, match="references"):
        await reader.get_many(store.tenant_id, store.run_id, (reference(first), reference(second)))
    for refs in (
        (reference(first), reference(first)),
        (reference(first), ArtifactReference(id=second.id, sha256="0" * 64)),
        (reference(first), reference(private_artifact("missing"))),
    ):
        with pytest.raises(ArtifactRepositoryError):
            await repository(store.database).get_many(store.tenant_id, store.run_id, refs)
    assert await repository(store.database).get_many(
        store.tenant_id, store.run_id, (reference(first), reference(second)),
    ) == (first, second)


async def test_reservation_limit_counts_durable_tombstones_and_preserves_existing_fence(store: ArtifactStore) -> None:
    artifact, owner, rejected_owner = private_artifact(), uuid4(), uuid4()
    for _ in range(2):
        await repository(store.database, max_write_reservations_per_run=1).reserve_write(
            store.tenant_id, store.run_id, reference(artifact), write_id=owner,
        )
    assert await repository(store.database, max_write_reservations_per_run=1).abort_write(
        store.tenant_id, store.run_id, reference(artifact), write_id=owner,
    )
    with pytest.raises(ArtifactRepositoryError, match="capacity"):
        await repository(store.database, max_write_reservations_per_run=1).reserve_write(
            store.tenant_id, store.run_id, reference(artifact), write_id=rejected_owner,
        )
    with pytest.raises(ArtifactRepositoryError, match="capacity"):
        await repository(store.database, max_write_reservations_per_run=1).put(
            store.tenant_id, store.run_id, artifact, write_id=rejected_owner,
        )
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository(store.database, max_write_reservations_per_run=1).put(
            store.tenant_id, store.run_id, artifact, write_id=owner,
        )
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository(store.database).get_many(store.tenant_id, store.run_id, (reference(artifact),))
    await repository(store.database, max_write_reservations_per_run=1).reserve_write(
        store.tenant_id, store.other_run_id, reference(artifact), write_id=owner,
    )


@pytest.mark.parametrize("value", [1e20, -0.0, "left\x00right"], ids=["exponent", "negative-zero", "nul"])
async def test_roundtrip_preserves_canonical_json_bytes(store: ArtifactStore, value: JsonValue) -> None:
    artifact = Artifact(id=uuid4(), type="json", producer="worker", content={"value": value})
    original = json.dumps(
        artifact.to_payload(), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    await repository(store.database).put(store.tenant_id, store.run_id, artifact, write_id=uuid4())
    fresh_database = build_database(store.database_url)
    try:
        hydrated = await repository(fresh_database).get_many(
            store.tenant_id, store.run_id, (reference(artifact),),
        )
        assert len(hydrated) == 1
        encoded = json.dumps(
            hydrated[0].to_payload(), ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        assert encoded == original
        assert hydrated[0].content_sha256 == artifact.content_sha256
        assert hydrated[0].recompute_content_sha256() == artifact.content_sha256
    finally:
        await fresh_database.dispose()


@pytest.mark.parametrize("corruption", [
    "payload", "hash", "bytes", "noncanonical", "duplicate-key", "invalid-json", "nonfinite",
])
async def test_sql_tampering_is_rejected_without_returning_private_body(
    store: ArtifactStore, corruption: str,
) -> None:
    artifact = private_artifact("PRIVATE_TAMPER_BODY")
    await repository(store.database).put(store.tenant_id, store.run_id, artifact)
    canonical = json.dumps(artifact.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    changes: dict[str, object]
    if corruption == "hash":
        changes = {"content_sha256": "0" * 64}
    elif corruption == "bytes":
        changes = {"byte_size": len(canonical.encode("utf-8")) + 1}
    else:
        damaged = {
            "payload": canonical.replace("PRIVATE_TAMPER_BODY", "PRIVATE_CHANGED_BODY"),
            "noncanonical": " " + canonical,
            "duplicate-key": '{"version":1,' + canonical[1:],
            "invalid-json": canonical[:-1],
            "nonfinite": canonical.replace('"PRIVATE_TAMPER_BODY"', "NaN"),
        }[corruption]
        changes = {"payload": damaged, "byte_size": len(damaged.encode("utf-8"))}
    async with store.database.session_factory() as session, session.begin():
        await session.execute(update(RuntimeArtifactRow).where(
            RuntimeArtifactRow.tenant_id == store.tenant_id,
            RuntimeArtifactRow.run_id == store.run_id, RuntimeArtifactRow.id == artifact.id,
        ).values(**changes))
    with pytest.raises(ArtifactRepositoryError, match="unavailable") as captured:
        await repository(store.database).get_many(store.tenant_id, store.run_id, (reference(artifact),))
    assert "PRIVATE_" not in str(captured.value) + repr(captured.value)


@asynccontextmanager
async def paused_write_repository(
    store: ArtifactStore, phase: str, write_id: UUID,
) -> AsyncIterator[tuple[ArtifactRepository, asyncio.Event, asyncio.Event]]:
    reached, release = asyncio.Event(), asyncio.Event()

    class BarrierSession(Session):
        pass

    def pause(session: Session, *unused: object) -> None:
        if reached.is_set():
            return
        if phase == "after_flush" and not any(
            isinstance(row, RuntimeArtifactWriteRow) and row.write_id == write_id and row.status == "written"
            for row in (*session.new, *session.dirty)
        ):
            return
        if phase == "before_commit":
            session.flush()
        reached.set()
        # A real SQLAlchemy/asyncpg transaction pauses here, not a mocked receipt.
        await_only(release.wait())

    sql_event.listen(BarrierSession, phase, pause)
    factory = async_sessionmaker(
        store.database.engine, expire_on_commit=False, sync_session_class=BarrierSession,
    )
    try:
        yield repository(Database(store.database.engine, factory)), reached, release
    finally:
        release.set()
        sql_event.remove(BarrierSession, phase, pause)


@pytest.mark.parametrize("phase", ["after_flush", "before_commit", "after_commit"])
async def test_cancelled_write_boundaries_keep_unknown_orphans_private_and_other_owner_safe(
    store: ArtifactStore, phase: str,
) -> None:
    artifact, cancelled_owner, surviving_owner = private_artifact(), uuid4(), uuid4()
    async with paused_write_repository(store, phase, cancelled_owner) as (paused, reached, release):
        task = asyncio.create_task(paused.put(store.tenant_id, store.run_id, artifact, write_id=cancelled_owner))
        try:
            await asyncio.wait_for(reached.wait(), timeout=10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async with store.database.session_factory() as session:
        stored = await session.get(RuntimeArtifactRow, (store.tenant_id, store.run_id, artifact.id))
        owner = await session.get(RuntimeArtifactWriteRow, (store.tenant_id, store.run_id, cancelled_owner))
        if phase == "after_commit":
            # This barrier is after an actual COMMIT: cancellation is not rollback proof.
            assert stored is not None and owner is not None and owner.status == "written"
        else:
            assert stored is None and owner is None
    runs = RunRepository(store.database.session_factory)
    assert await runs.artifacts(store.tenant_id, store.run_id) == ()
    assert await runs.raw_artifacts(store.tenant_id, store.run_id) == ()
    assert await runs.events(store.tenant_id, store.run_id) == ()
    assert await runs.raw_events(store.tenant_id, store.run_id) == ()

    # Also proves the cancelled transaction released its run lock and pooled connection.
    await asyncio.wait_for(repository(store.database).put(
        store.tenant_id, store.run_id, artifact, write_id=surviving_owner,
    ), timeout=10)
    assert not await repository(store.database).abort_write(
        store.tenant_id, store.run_id, reference(artifact), write_id=cancelled_owner,
    )
    assert await repository(store.database).get_many(
        store.tenant_id, store.run_id, (reference(artifact),),
    ) == (artifact,)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository(store.database).put(store.tenant_id, store.run_id, artifact, write_id=cancelled_owner)


async def test_cancelled_flushed_owner_racing_abort_preserves_surviving_owner(store: ArtifactStore) -> None:
    artifact, survivor, stale = private_artifact(), uuid4(), uuid4()
    await repository(store.database).put(store.tenant_id, store.run_id, artifact, write_id=survivor)
    application_name = "artifact-abort-" + uuid4().hex
    engine = create_async_engine(
        store.database_url, hide_parameters=True,
        connect_args={"server_settings": {"application_name": application_name}},
    )
    contender = Database(engine, async_sessionmaker(engine, expire_on_commit=False))
    try:
        async with paused_write_repository(store, "after_flush", stale) as (paused, reached, release):
            writer = asyncio.create_task(paused.put(store.tenant_id, store.run_id, artifact, write_id=stale))
            aborter: asyncio.Task[bool] | None = None
            try:
                await asyncio.wait_for(reached.wait(), timeout=10)
                aborter = asyncio.create_task(repository(contender).abort_write(
                    store.tenant_id, store.run_id, reference(artifact), write_id=stale,
                ))
                # Observe an actual PG lock wait before cancellation, not just task scheduling.
                async with asyncio.timeout(10):
                    while True:
                        async with store.database.session_factory() as session:
                            blocked = await session.scalar(text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE application_name = :name AND wait_event_type = 'Lock'"
                            ), {"name": application_name})
                        if blocked:
                            break
                        assert not aborter.done()
                        await asyncio.sleep(0.01)
                writer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(writer, timeout=10)
                assert not await asyncio.wait_for(aborter, timeout=10)
            finally:
                release.set()
                tasks = [writer] if aborter is None else [writer, aborter]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await contender.dispose()
    assert await repository(store.database).get_many(
        store.tenant_id, store.run_id, (reference(artifact),),
    ) == (artifact,)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository(store.database).put(store.tenant_id, store.run_id, artifact, write_id=stale)
