"""Prepared real-PG lookup selector; local collection is NOT PG verification."""

from typing import TypedDict
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from agent_hub.db.models import RuntimeArtifactRow
from agent_hub.runtime.artifacts import ArtifactReference, ArtifactRepositoryError
from agent_hub.runtime.contracts import Artifact
from tests.integration.runs.test_runtime_artifact_repository import (
    ArtifactStore,
    repository,
)
from tests.integration.runs.test_runtime_artifact_repository import (
    store as store,  # noqa: PLC0414 - existing guarded real-PG fixture
)
from tests.unit.runs.project_auto_repair_typed_fixture import json_map

pytestmark = pytest.mark.integration


class LookupQuery(TypedDict):
    type: str
    producer: str


async def test_find_references_real_pg_scope_keyset_and_immutable_read(
    store: ArtifactStore,
) -> None:
    values = [
        Artifact(
            id=UUID(int=index),
            type="python_resolution_checkpoint",
            producer="python_resolution",
            content=json_map({"needs": ["PRIVATE_BODY_SENTINEL"], "index": index}),
        )
        for index in range(1, 131)
    ]
    writer = repository(store.database)
    for value in reversed(values):
        await writer.put(store.tenant_id, store.run_id, value)
    await writer.put(store.foreign_tenant_id, store.foreign_run_id, values[0])
    await writer.put(store.tenant_id, store.other_run_id, values[1])
    for kind, producer in (
        ("different_kind", "python_resolution"),
        ("python_resolution_checkpoint", "other_owner"),
    ):
        await writer.put(
            store.tenant_id,
            store.run_id,
            Artifact(
                id=uuid4(),
                type=kind,
                producer=producer,
                content={"value": "unrelated"},
            ),
        )

    async def snapshot() -> tuple[object, ...]:
        async with store.database.session_factory() as session:
            return tuple(
                (
                    await session.execute(
                        select(
                            RuntimeArtifactRow.id,
                            RuntimeArtifactRow.content_sha256,
                            RuntimeArtifactRow.byte_size,
                            RuntimeArtifactRow.payload,
                            RuntimeArtifactRow.permanent,
                        )
                        .where(
                            RuntimeArtifactRow.tenant_id == store.tenant_id,
                            RuntimeArtifactRow.run_id == store.run_id,
                        )
                        .order_by(RuntimeArtifactRow.id)
                    )
                ).all()
            )

    before = await snapshot()
    reader = repository(store.database)  # a fresh repository/worker, no memory index
    query: LookupQuery = {"type": "python_resolution_checkpoint", "producer": "python_resolution"}
    first = await reader.find_references(store.tenant_id, store.run_id, **query)
    second = await reader.find_references(
        store.tenant_id, store.run_id, after=first[-1].id, **query
    )
    assert [len(first), len(second)] == [128, 2]
    assert (
        await reader.find_references(store.tenant_id, store.run_id, after=second[-1].id, **query)
        == ()
    )
    refs = first + second
    assert refs == tuple(
        ArtifactReference(id=value.id, sha256=value.content_sha256) for value in values
    )
    assert await reader.get_many(store.tenant_id, store.run_id, refs) == tuple(values)
    assert "PRIVATE_BODY" not in str([ref.to_payload() for ref in refs])
    assert (
        await reader.find_references(
            store.tenant_id, store.run_id, type="absent", producer="python_resolution"
        )
        == ()
    )
    assert await reader.find_references(store.tenant_id, store.other_run_id, **query) == (refs[1],)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await reader.find_references(store.foreign_tenant_id, store.run_id, **query)
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await reader.get_many(
            store.tenant_id, store.run_id, (ArtifactReference(id=refs[0].id, sha256="0" * 64),)
        )
    assert await snapshot() == before


async def test_find_references_real_pg_legal_nul_body_and_sparse_matching_pages(
    store: ArtifactStore,
) -> None:
    """Prepare the Unicode counterexample on real Text storage, no JSON cast seam."""
    selected = {2, 130, 260}
    values = [
        Artifact(
            id=UUID(int=index),
            type="checkpoint" if index in selected else "other",
            producer="owner",
            content={"value": "left\x00right", "n": index},
        )
        for index in range(1, 262)
    ]
    writer = repository(store.database)
    for value in reversed(values):
        await writer.put(store.tenant_id, store.run_id, value)
    reader = repository(store.database)
    first = await reader.find_references(
        store.tenant_id, store.run_id, type="checkpoint", producer="owner", limit=2
    )
    second = await reader.find_references(
        store.tenant_id,
        store.run_id,
        type="checkpoint",
        producer="owner",
        limit=2,
        after=first[-1].id,
    )
    assert [tuple(ref.id.int for ref in page) for page in (first, second)] == [(2, 130), (260,)]
    assert (
        await reader.find_references(
            store.tenant_id,
            store.run_id,
            type="checkpoint",
            producer="owner",
            limit=2,
            after=second[-1].id,
        )
        == ()
    )
    assert await reader.get_many(store.tenant_id, store.run_id, first + second) == tuple(
        value for value in values if value.id.int in selected
    )
    async with store.database.session_factory() as session:
        rows = (
            await session.scalars(
                select(RuntimeArtifactRow).where(
                    RuntimeArtifactRow.tenant_id == store.tenant_id,
                    RuntimeArtifactRow.run_id == store.run_id,
                )
            )
        ).all()
    assert len(rows) == len(values)
    assert all("\\u0000" in row.payload and "\x00" not in row.payload for row in rows)
    assert "left" not in str([ref.to_payload() for ref in first + second])
