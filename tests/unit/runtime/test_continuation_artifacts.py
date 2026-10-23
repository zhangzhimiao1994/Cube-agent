from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.db.models import RuntimeArtifactRow, RuntimeArtifactWriteRow
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.artifacts import (
    ArtifactReference,
    ArtifactRepository,
    ArtifactRepositoryError,
    InMemoryArtifactRepository,
)
from agent_hub.runtime.continuation_artifacts import load_continuation, save_continuation
from agent_hub.runtime.contracts import Artifact

TENANT_ID = UUID("00000000-0000-4000-8000-000000000051")
RUN_ID = UUID("00000000-0000-4000-8000-000000000052")
CHUNK_BYTES = 64 * 1024
MAX_BYTES = 64 * 1024 * 1024


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class RecordingRepository(InMemoryArtifactRepository):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.reserved: list[tuple[ArtifactReference, UUID]] = []
        self.aborted: list[tuple[ArtifactReference, UUID]] = []
        self.stored: list[Artifact] = []
        self.reads = 0

    async def reserve_write(
        self, tenant_id: UUID, run_id: UUID, reference: ArtifactReference, *, write_id: UUID
    ) -> None:
        self.reserved.append((reference, write_id))
        await super().reserve_write(tenant_id, run_id, reference, write_id=write_id)

    async def put(
        self, tenant_id: UUID, run_id: UUID, artifact: Artifact, *, write_id: UUID | None = None
    ) -> None:
        await super().put(tenant_id, run_id, artifact, write_id=write_id)
        self.stored.append(artifact)

    async def abort_write(
        self, tenant_id: UUID, run_id: UUID, reference: ArtifactReference, *, write_id: UUID
    ) -> bool:
        self.aborted.append((reference, write_id))
        return await super().abort_write(tenant_id, run_id, reference, write_id=write_id)

    async def get_many(
        self, tenant_id: UUID, run_id: UUID, references: tuple[ArtifactReference, ...]
    ) -> tuple[Artifact, ...]:
        self.reads += 1
        return await super().get_many(tenant_id, run_id, references)


async def save(repository: ArtifactRepository, value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], await save_continuation(
        repository, tenant_id=TENANT_ID, run_id=RUN_ID, value=value
    ))


async def load(repository: ArtifactRepository, reference: Any, **scope: Any) -> Any:
    return await load_continuation(
        repository, tenant_id=scope.get("tenant_id", TENANT_ID),
        run_id=scope.get("run_id", RUN_ID), reference=reference,
    )


@pytest.mark.parametrize("value", [None, True, 17, -2.5, "", (), {"a": (1, None)}])
async def test_json_values_round_trip(value: Any) -> None:
    repository = InMemoryArtifactRepository()
    assert await load(repository, await save(repository, value)) == value


async def test_large_unicode_is_canonical_chunked_and_checkpoint_contains_only_references() -> None:
    repository = RecordingRepository()
    value = {"z": "\u754c" * 800_000, "a": "x" * (CHUNK_BYTES - 3)}
    reference = await save(repository, value)
    raw = canonical(value)

    assert set(reference) == {"version", "content_sha256", "total_bytes", "segments"}
    assert reference["version"] == 1
    assert reference["total_bytes"] == len(raw)
    assert reference["content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(reference["segments"]) == (len(raw) + CHUNK_BYTES - 1) // CHUNK_BYTES
    assert len(canonical(reference)) < 16_000
    assert await load(repository, reference) == value
    reordered = await save(InMemoryArtifactRepository(), dict(reversed(list(value.items()))))
    assert reordered["content_sha256"] == reference["content_sha256"]
    assert reordered["total_bytes"] == reference["total_bytes"]
    assert len(repository.reserved) == len(repository.stored)
    pieces = []
    for index, artifact in enumerate(repository.stored):
        assert len(canonical(artifact.to_payload())) < 1024 * 1024
        assert set(reference["segments"][index]) == {"id", "sha256"}
        assert reference["segments"][index] == ArtifactReference(
            artifact.id, artifact.content_sha256
        ).to_payload()
        assert all(
            len(item.encode("utf-8")) < 512_000
            for item in artifact.content.values() if isinstance(item, str)
        )
        piece = base64.b64decode(artifact.content["data"], validate=True)  # type: ignore[arg-type]
        assert 0 < len(piece) <= CHUNK_BYTES
        pieces.append(piece)
    assert b"".join(pieces) == raw


@pytest.mark.parametrize("mutation", [
    {"version": True}, {"version": 2}, {"version": "1"},
    {"total_bytes": True}, {"total_bytes": 0}, {"total_bytes": -1},
    {"total_bytes": MAX_BYTES + 1}, {"total_bytes": 1.0},
    {"content_sha256": "A" * 64}, {"content_sha256": 0},
    {"content_sha256": "0" * 63}, {"extra": "forbidden"},
    {"segments": []}, {"segments": "not-a-list"},
])
async def test_bad_checkpoint_is_rejected_before_repository_read(mutation: dict[str, Any]) -> None:
    repository = RecordingRepository()
    reference = await save(repository, "ok")
    reference.update(mutation)
    with pytest.raises(ArtifactRepositoryError):
        await load(repository, reference)
    assert repository.reads == 0


@pytest.mark.parametrize("bad_ref", [
    None, {}, {"id": str(uuid4())}, {"id": "bad", "sha256": "0" * 64},
    {"id": 1, "sha256": "0" * 64},
    {"id": str(uuid4()), "sha256": True},
    {"id": str(uuid4()), "sha256": "0" * 64, "extra": 1},
    {"id": "00000000000040008000000000000051", "sha256": "0" * 64},
])
async def test_every_reference_is_validated_before_fetching(bad_ref: Any) -> None:
    repository = RecordingRepository()
    reference = await save(repository, "x" * CHUNK_BYTES)
    reference["segments"][-1] = bad_ref
    with pytest.raises(ArtifactRepositoryError):
        await load(repository, reference)
    assert repository.reads == 0


@pytest.mark.parametrize("kind", ["duplicate", "missing", "too-many", "missing-field"])
async def test_reference_cardinality_is_checked_before_fetching(kind: str) -> None:
    repository = RecordingRepository()
    reference = await save(repository, "x" * CHUNK_BYTES)
    if kind == "duplicate":
        reference["segments"][1] = reference["segments"][0]
    elif kind == "missing":
        reference["segments"].pop()
    elif kind == "too-many":
        reference["segments"] = reference["segments"] * 513
    else:
        del reference["content_sha256"]
    with pytest.raises(ArtifactRepositoryError):
        await load(repository, reference)
    assert repository.reads == 0


@pytest.mark.parametrize("kind", ["reorder", "hash", "bytes", "missing", "tenant", "run"])
async def test_tampered_or_out_of_scope_references_cannot_load(kind: str) -> None:
    repository = InMemoryArtifactRepository()
    reference = await save(repository, "x" * CHUNK_BYTES)
    scope: dict[str, UUID] = {}
    if kind == "reorder":
        reference["segments"].reverse()
    elif kind == "hash":
        reference["content_sha256"] = "0" * 64
    elif kind == "bytes":
        reference["total_bytes"] += 1
    elif kind == "missing":
        reference["segments"][0]["id"] = str(uuid4())
    else:
        scope[f"{kind}_id"] = uuid4()
    with pytest.raises(ArtifactRepositoryError):
        await load(repository, reference, **scope)


@pytest.mark.parametrize("value", [
    float("nan"), float("inf"), {1: "key"}, {"data": object()}, b"bytes", "\ud800",
])
async def test_non_json_values_fail_without_reservations_or_source_details(value: Any) -> None:
    repository = RecordingRepository()
    with pytest.raises(ArtifactRepositoryError) as caught:
        await save(repository, value)
    assert repository.reserved == []
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert caught.value.__suppress_context__


async def test_cycles_and_oversize_json_fail_before_any_writes() -> None:
    repository = RecordingRepository()
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    for value in (cyclic, "x" * MAX_BYTES):
        with pytest.raises(ArtifactRepositoryError):
            await save(repository, value)
    assert repository.reserved == []


@pytest.mark.parametrize("limits", [
    {"max_artifacts_per_run": 1}, {"max_artifact_bytes": 1000},
    {"max_total_bytes_per_run": 90_000}, {"max_write_reservations_per_run": 1},
])
async def test_existing_repository_quotas_are_obeyed_and_partial_writes_reclaimed(
    limits: dict[str, int],
) -> None:
    repository = RecordingRepository(**limits)
    with pytest.raises(ArtifactRepositoryError):
        await save(repository, "x" * (CHUNK_BYTES * 2))
    assert repository.aborted == list(reversed(repository.reserved))
    for ref, _ in repository.reserved:
        with pytest.raises(ArtifactRepositoryError):
            await repository.get_many(TENANT_ID, RUN_ID, (ref,))


@pytest.mark.parametrize("phase", ["reserve", "put"])
@pytest.mark.parametrize("after", [False, True])
async def test_write_failures_abort_even_uncertain_writes_and_preserve_other_data(
    phase: str, after: bool,
) -> None:
    class FailingRepository(RecordingRepository):
        async def reserve_write(
            self, tenant_id: UUID, run_id: UUID, reference: ArtifactReference, *, write_id: UUID
        ) -> None:
            if phase == "reserve" and len(self.reserved) == 1 and not after:
                self.reserved.append((reference, write_id))
                raise RuntimeError("private-source-error")
            await super().reserve_write(tenant_id, run_id, reference, write_id=write_id)
            if phase == "reserve" and len(self.reserved) == 2:
                raise RuntimeError("private-source-error")

        async def put(
            self, tenant_id: UUID, run_id: UUID, artifact: Artifact,
            *, write_id: UUID | None = None,
        ) -> None:
            if phase == "put" and len(self.reserved) == 2 and not after:
                raise RuntimeError("private-source-error")
            await super().put(tenant_id, run_id, artifact, write_id=write_id)
            if phase == "put" and len(self.reserved) == 2:
                raise RuntimeError("private-source-error")

    repository = FailingRepository()
    permanent = Artifact(id=uuid4(), type="text", producer="other", content={"text": "keep"})
    await repository.put(TENANT_ID, RUN_ID, permanent)
    with pytest.raises(ArtifactRepositoryError) as caught:
        await save(repository, "x" * CHUNK_BYTES)
    assert "private-source-error" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert caught.value.__context__ is None
    assert repository.aborted == list(reversed(repository.reserved))
    permanent_ref = ArtifactReference(permanent.id, permanent.content_sha256)
    assert await repository.get_many(TENANT_ID, RUN_ID, (permanent_ref,)) == (permanent,)
    for ref, _ in repository.reserved:
        with pytest.raises(ArtifactRepositoryError):
            await repository.get_many(TENANT_ID, RUN_ID, (ref,))


@pytest.mark.parametrize("phase", ["reserve", "put"])
async def test_repeated_cancellation_waits_for_owned_write_cleanup(phase: str) -> None:
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    finish_cleanup = asyncio.Event()

    class BlockingRepository(RecordingRepository):
        async def reserve_write(
            self, tenant_id: UUID, run_id: UUID, reference: ArtifactReference, *, write_id: UUID
        ) -> None:
            await super().reserve_write(tenant_id, run_id, reference, write_id=write_id)
            if phase == "reserve" and len(self.reserved) == 2:
                entered.set()
                await asyncio.Event().wait()

        async def put(
            self, tenant_id: UUID, run_id: UUID, artifact: Artifact,
            *, write_id: UUID | None = None,
        ) -> None:
            await super().put(tenant_id, run_id, artifact, write_id=write_id)
            if phase == "put" and len(self.reserved) == 2:
                entered.set()
                await asyncio.Event().wait()

        async def abort_write(
            self, tenant_id: UUID, run_id: UUID, reference: ArtifactReference, *, write_id: UUID
        ) -> bool:
            cleaning.set()
            await finish_cleanup.wait()
            return await super().abort_write(tenant_id, run_id, reference, write_id=write_id)

    repository = BlockingRepository()
    task = asyncio.create_task(save(repository, "x" * CHUNK_BYTES))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        finish_cleanup.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    assert repository.aborted == list(reversed(repository.reserved))
    for ref, _ in repository.reserved:
        with pytest.raises(ArtifactRepositoryError):
            await repository.get_many(TENANT_ID, RUN_ID, (ref,))


@pytest.mark.parametrize("key", [
    "native_assistant", "raw_prompt", "credentials", "reasoning_content",
    "hidden_reasoning", "authorization", "access_token", "Native-Assistant", "Credentials",
])
async def test_forbidden_native_or_sensitive_state_is_rejected_before_encoding(key: str) -> None:
    repository = RecordingRepository()
    with pytest.raises(ArtifactRepositoryError):
        await save(repository, {"reviewed": ({key: "private-source-value"},)})
    assert repository.reserved == []


@pytest.mark.parametrize("kind", ["missing", "reversed", "wrong-type", "corrupt-hash"])
async def test_repository_results_are_independently_validated(kind: str) -> None:
    class BadReadRepository(InMemoryArtifactRepository):
        async def get_many(
            self, tenant_id: UUID, run_id: UUID, references: tuple[ArtifactReference, ...]
        ) -> tuple[Artifact, ...]:
            artifacts = await super().get_many(tenant_id, run_id, references)
            if kind == "missing":
                return artifacts[:-1]
            if kind == "reversed":
                return tuple(reversed(artifacts))
            first = artifacts[0]
            update = {"type": "text"} if kind == "wrong-type" else {"content_sha256": "0" * 64}
            return (first.model_copy(update=update), *artifacts[1:])

    repository = BadReadRepository()
    reference = await save(repository, "x" * CHUNK_BYTES)
    with pytest.raises(ArtifactRepositoryError):
        await load(repository, reference)


@pytest.mark.parametrize("field,value", [
    ("tenant_id", str(uuid4())), ("run_id", str(uuid4())),
    ("segment_index", True), ("segment_index", 9), ("segment_count", 9),
    ("total_bytes", True), ("total_bytes", MAX_BYTES + 1),
    ("content_sha256", "0" * 64), ("data", "!"),
    ("data", base64.b64encode(b"x" * (CHUNK_BYTES + 1)).decode("ascii")),
], ids=[
    "tenant", "run", "bool-index", "index", "count", "bool-bytes", "oversize-bytes",
    "hash", "invalid-base64", "oversize-segment",
])
async def test_even_rehashed_malformed_segment_bodies_are_rejected(field: str, value: Any) -> None:
    repository = RecordingRepository()
    reference = await save(repository, "ok")
    original = repository.stored[0]
    content = dict(original.content)
    content[field] = value
    replacement = Artifact(
        id=uuid4(), type=original.type, producer=original.producer, content=content
    )
    await repository.put(TENANT_ID, RUN_ID, replacement)
    reference["segments"][0] = ArtifactReference(
        replacement.id, replacement.content_sha256
    ).to_payload()
    with pytest.raises(ArtifactRepositoryError):
        await load(repository, reference)


async def test_load_accepts_frozen_checkpoint_containers() -> None:
    from types import MappingProxyType

    repository = InMemoryArtifactRepository()
    reference = await save(repository, (1, "ok"))
    reference["segments"] = tuple(MappingProxyType(item) for item in reference["segments"])
    assert await load(repository, MappingProxyType(reference)) == (1, "ok")


@pytest.mark.parametrize("length,count", [(CHUNK_BYTES - 2, 1), (CHUNK_BYTES - 1, 2)])
async def test_segment_boundary_counts_canonical_bytes(length: int, count: int) -> None:
    repository = RecordingRepository()
    value = "x" * length
    reference = await save(repository, value)
    assert reference["total_bytes"] == length + 2
    assert len(reference["segments"]) == count
    assert await load(repository, reference) == value


@pytest.mark.parametrize("raw", [
    b'\xff', b'{"x":1,"x":2}', b'NaN', b'1e999', b'{"z":1, "a":2}',
    b'"\\ud800"', b'null\n', b'{"native_assistant":{}}', b'{"credentials":"no"}',
    b'[' * 129 + b'0' + b']' * 129,
], ids=[
    "utf8", "duplicate-key", "nan", "infinity", "noncanonical", "surrogate",
    "trailing-space", "native", "credentials", "depth",
])
async def test_rehashed_noncanonical_or_forbidden_json_cannot_load(raw: bytes) -> None:
    repository = RecordingRepository()
    reference = await save(repository, "ok")
    original = repository.stored[0]
    digest = hashlib.sha256(raw).hexdigest()
    body = dict(original.content)
    body.update(content_sha256=digest, total_bytes=len(raw), data=base64.b64encode(raw).decode())
    replacement = Artifact(
        id=uuid4(), type=original.type, producer=original.producer, content=body,
    )
    await repository.put(TENANT_ID, RUN_ID, replacement)
    reference.update(content_sha256=digest, total_bytes=len(raw))
    reference["segments"] = [ArtifactReference(
        replacement.id, replacement.content_sha256,
    ).to_payload()]
    with pytest.raises(ArtifactRepositoryError) as caught:
        await load(repository, reference)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


async def test_raw_content_hash_is_verified_even_when_segment_envelope_is_valid() -> None:
    repository = RecordingRepository()
    reference = await save(repository, "ok")
    original = repository.stored[0]
    body = dict(original.content)
    body["content_sha256"] = "0" * 64
    replacement = Artifact(
        id=uuid4(), type=original.type, producer=original.producer, content=body,
    )
    await repository.put(TENANT_ID, RUN_ID, replacement)
    reference["content_sha256"] = "0" * 64
    reference["segments"][0] = ArtifactReference(
        replacement.id, replacement.content_sha256,
    ).to_payload()
    with pytest.raises(ArtifactRepositoryError):
        await load(repository, reference)


async def test_segment_envelope_hash_is_recomputed_without_trusting_repository() -> None:
    class CorruptRepository(InMemoryArtifactRepository):
        async def get_many(
            self, tenant_id: UUID, run_id: UUID, references: tuple[ArtifactReference, ...]
        ) -> tuple[Artifact, ...]:
            stored, = await super().get_many(tenant_id, run_id, references)
            content = dict(stored.content)
            content["data"] = base64.b64encode(b'"no"').decode()
            return (stored.model_copy(update={"content": content}),)

    repository = CorruptRepository()
    reference = await save(repository, "ok")
    with pytest.raises(ArtifactRepositoryError):
        await load(repository, reference)


@pytest.mark.parametrize("permanent", [False, True])
async def test_cleanup_retains_segments_adopted_by_another_writer(permanent: bool) -> None:
    survivor = uuid4()

    class SharedRepository(RecordingRepository):
        async def put(
            self, tenant_id: UUID, run_id: UUID, artifact: Artifact,
            *, write_id: UUID | None = None,
        ) -> None:
            await super().put(tenant_id, run_id, artifact, write_id=write_id)
            if len(self.reserved) == 1:
                await super().put(
                    tenant_id, run_id, artifact, write_id=None if permanent else survivor,
                )
            else:
                raise RuntimeError("private-source-error")

    repository = SharedRepository()
    with pytest.raises(ArtifactRepositoryError):
        await save(repository, "x" * CHUNK_BYTES)
    assert repository.aborted == list(reversed(repository.reserved))
    first_ref, first_owner = repository.reserved[0]
    surviving, = await repository.get_many(TENANT_ID, RUN_ID, (first_ref,))
    with pytest.raises(ArtifactRepositoryError):
        await repository.put(TENANT_ID, RUN_ID, surviving, write_id=first_owner)
    with pytest.raises(ArtifactRepositoryError):
        await repository.get_many(TENANT_ID, RUN_ID, (repository.reserved[1][0],))
    if not permanent:
        assert await repository.abort_write(TENANT_ID, RUN_ID, first_ref, write_id=survivor)


async def test_cleanup_failure_does_not_skip_remaining_owned_writes_or_leak_errors() -> None:
    class CleanupErrorRepository(RecordingRepository):
        async def put(
            self, tenant_id: UUID, run_id: UUID, artifact: Artifact,
            *, write_id: UUID | None = None,
        ) -> None:
            await super().put(tenant_id, run_id, artifact, write_id=write_id)
            if len(self.reserved) == 2:
                raise RuntimeError("private-write-error")

        async def abort_write(
            self, tenant_id: UUID, run_id: UUID, reference: ArtifactReference, *, write_id: UUID
        ) -> bool:
            await super().abort_write(tenant_id, run_id, reference, write_id=write_id)
            raise RuntimeError("private-cleanup-error")

    repository = CleanupErrorRepository()
    with pytest.raises(ArtifactRepositoryError) as caught:
        await save(repository, "x" * CHUNK_BYTES)
    assert "private" not in str(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert repository.aborted == list(reversed(repository.reserved))
    for ref, _ in repository.reserved:
        with pytest.raises(ArtifactRepositoryError):
            await repository.get_many(TENANT_ID, RUN_ID, (ref,))


@pytest.mark.parametrize("cancelled", [False, True])
async def test_read_errors_are_sanitized_but_cancellation_propagates(cancelled: bool) -> None:
    class ReadErrorRepository(RecordingRepository):
        async def get_many(
            self, tenant_id: UUID, run_id: UUID, references: tuple[ArtifactReference, ...]
        ) -> tuple[Artifact, ...]:
            if cancelled:
                raise asyncio.CancelledError
            raise RuntimeError("private-read-error")

    repository = ReadErrorRepository()
    reference = await save(repository, "ok")
    with pytest.raises(asyncio.CancelledError if cancelled else ArtifactRepositoryError) as caught:
        await load(repository, reference)
    assert "private" not in str(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert repository.aborted == []


async def test_exact_codec_ceiling_reaches_repository_without_expanding_its_quota() -> None:
    repository = RecordingRepository()
    reference = {
        "version": 1, "content_sha256": "0" * 64, "total_bytes": MAX_BYTES,
        "segments": [{"id": str(uuid4()), "sha256": "0" * 64} for _ in range(1024)],
    }
    with pytest.raises(ArtifactRepositoryError):
        await load(repository, reference)
    assert repository.reads == 1


@pytest.mark.parametrize("scope", [{"tenant_id": "bad"}, {"run_id": "bad"}])
async def test_invalid_scope_fails_before_repository_access(scope: dict[str, str]) -> None:
    repository = RecordingRepository()
    reference = await save(repository, "ok")
    with pytest.raises(ArtifactRepositoryError):
        await load(repository, reference, **scope)
    assert repository.reads == 0


async def test_postgres_codec_storage_stays_out_of_public_run_artifacts() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    transaction = AsyncMock()
    transaction.__aexit__.return_value = False
    session.begin = MagicMock(return_value=transaction)
    factory = MagicMock(return_value=session)
    # Scope lock and reservation count, followed by the put scope lock.
    session.scalar.side_effect = [RUN_ID, 0, RUN_ID]
    usage = MagicMock()
    usage.one.return_value = (0, 0)
    session.execute.return_value = usage
    rows: list[Any] = []
    session.add.side_effect = rows.append

    def get_row(model: type[Any], identity: tuple[UUID, UUID, UUID]) -> Any:
        for row in rows:
            row_id = row.write_id if isinstance(row, RuntimeArtifactWriteRow) else row.id
            if type(row) is model and (row.tenant_id, row.run_id, row_id) == identity:
                return row
        return None

    session.get.side_effect = get_row
    repository = PostgresArtifactRepository(factory)
    reference = await save(repository, {"reviewed_result": "persistable data"})
    assert {type(row) for row in rows} == {RuntimeArtifactRow, RuntimeArtifactWriteRow}
    assert all(row.tenant_id == TENANT_ID and row.run_id == RUN_ID for row in rows)
    owner = next(row for row in rows if isinstance(row, RuntimeArtifactWriteRow))
    assert owner.status == "written"

    session.scalar.side_effect = None
    session.scalar.return_value = RUN_ID

    def query_rows(statement: Any) -> MagicMock:
        result = MagicMock()
        entity = statement.column_descriptions[0]["entity"]
        result.all.return_value = [row for row in rows if type(row) is entity]
        return result

    session.scalars.side_effect = query_rows
    assert await load(repository, reference) == {"reviewed_result": "persistable data"}
    runs = RunRepository(factory)
    assert await runs.artifacts(TENANT_ID, RUN_ID) == ()
    assert await runs.raw_artifacts(TENANT_ID, RUN_ID) == ()
    assert await runs.artifact_ids(TENANT_ID, RUN_ID) == ()
    assert await runs.events(TENANT_ID, RUN_ID) == ()
