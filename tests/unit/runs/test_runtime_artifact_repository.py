from __future__ import annotations

import asyncio
import json
from importlib import import_module
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, Cast

from agent_hub.db.models import RuntimeArtifactRow
from agent_hub.runtime.artifacts import (
    ArtifactReference,
    ArtifactRepository,
    ArtifactRepositoryError,
)
from agent_hub.runtime.contracts import Artifact, JsonValue


def make_repository(factory: object, **limits: Any) -> ArtifactRepository:
    constructor = import_module("agent_hub.runs.artifacts").PostgresArtifactRepository
    return cast(ArtifactRepository, constructor(factory, **limits))


def session_factory() -> tuple[MagicMock, AsyncMock, AsyncMock]:
    session = AsyncMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    transaction = AsyncMock()
    transaction.__aexit__.return_value = False
    session.begin = MagicMock(return_value=transaction)
    return MagicMock(return_value=session), session, transaction


async def invoke(repository: ArtifactRepository, operation: str) -> object:
    tenant_id, run_id, write_id = uuid4(), uuid4(), uuid4()
    artifact = Artifact(id=uuid4(), type="text", producer="worker", content={"text": "private"})
    ref = ArtifactReference(id=artifact.id, sha256=artifact.content_sha256)
    if operation == "put":
        await repository.put(tenant_id, run_id, artifact, write_id=write_id)
        return None
    if operation == "reserve_write":
        await repository.reserve_write(tenant_id, run_id, ref, write_id=write_id)
        return None
    if operation == "abort_write":
        return await repository.abort_write(tenant_id, run_id, ref, write_id=write_id)
    assert operation == "get_many"
    return await repository.get_many(tenant_id, run_id, (ref,))


@pytest.mark.parametrize("limit", [
    "max_artifacts_per_run", "max_total_bytes_per_run", "max_artifact_bytes",
    "max_batch_size", "max_write_reservations_per_run",
])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1"])
def test_limits_are_positive_strict_integers_before_opening_session(limit: str, value: object) -> None:
    factory = MagicMock()
    with pytest.raises(ValueError, match="positive integers"):
        make_repository(factory, **{limit: value})
    factory.assert_not_called()


@pytest.mark.parametrize("operation", ["put", "reserve_write", "abort_write", "get_many"])
async def test_cancellation_is_not_converted_or_committed(operation: str) -> None:
    factory, session, transaction = session_factory()
    session.scalar.side_effect = asyncio.CancelledError()
    repository = make_repository(cast(async_sessionmaker[AsyncSession], factory))
    with pytest.raises(asyncio.CancelledError):
        await invoke(repository, operation)
    assert transaction.__aexit__.await_args is not None
    assert transaction.__aexit__.await_args.args[0] is asyncio.CancelledError
    session.__aexit__.assert_awaited_once()
    session.commit.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.parametrize("operation", ["put", "reserve_write", "abort_write", "get_many"])
async def test_database_error_is_sanitized_and_transaction_unwound(operation: str) -> None:
    factory, session, transaction = session_factory()
    session.scalar.side_effect = SQLAlchemyError("PRIVATE_SQL_PAYLOAD_AND_CREDENTIAL_SENTINEL")
    repository = make_repository(factory)
    with pytest.raises(ArtifactRepositoryError) as captured:
        await invoke(repository, operation)
    assert "PRIVATE_SQL" not in str(captured.value) + repr(captured.value)
    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None
    assert transaction.__aexit__.await_args is not None
    assert transaction.__aexit__.await_args.args[0] is SQLAlchemyError
    session.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("operation", ["put", "reserve_write", "abort_write", "get_many"])
async def test_missing_tenant_run_pair_fails_before_private_storage_access(operation: str) -> None:
    factory, session, transaction = session_factory()
    session.scalar.return_value = None
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await invoke(make_repository(factory), operation)
    session.scalar.assert_awaited_once()
    session.add.assert_not_called()
    session.execute.assert_not_awaited()
    assert transaction.__aexit__.await_args is not None
    assert transaction.__aexit__.await_args.args[0] is ArtifactRepositoryError


@pytest.mark.parametrize("corruption", ["body", "id", "row_hash", "payload_hash", "byte_size"])
async def test_hydration_revalidates_persisted_identity_digest_and_size(corruption: str) -> None:
    artifact = Artifact(id=uuid4(), type="text", producer="worker", content={"text": "PRIVATE_BODY"})
    payload = artifact.to_payload()
    row = RuntimeArtifactRow(
        tenant_id=uuid4(), run_id=uuid4(), id=artifact.id,
        content_sha256=artifact.content_sha256, permanent=False,
        byte_size=len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()),
    )
    if corruption == "body":
        payload["content"] = {"text": "PRIVATE_CORRUPTION"}
    elif corruption == "id":
        payload["id"] = str(uuid4())
    elif corruption == "row_hash":
        row.content_sha256 = "0" * 64
    elif corruption == "payload_hash":
        payload["content_sha256"] = "0" * 64
    else:
        row.byte_size += 1
    row.payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    factory, session, _ = session_factory()
    session.scalar.return_value = row.run_id
    result = MagicMock()
    result.all.return_value = [row]
    session.scalars.return_value = result
    with pytest.raises(ArtifactRepositoryError, match="unavailable") as captured:
        await make_repository(factory).get_many(
            row.tenant_id, row.run_id, (ArtifactReference(id=artifact.id, sha256=artifact.content_sha256),),
        )
    assert "PRIVATE" not in str(captured.value) + repr(captured.value)


async def test_put_revalidates_constructed_artifact_before_database_access() -> None:
    factory = MagicMock()
    artifact = Artifact(id=uuid4(), type="text", producer="worker", content={"text": "original"})
    forged = artifact.model_copy(update={"content": {"text": "PRIVATE_FORGED_PAYLOAD"}})
    with pytest.raises(ArtifactRepositoryError, match="invalid") as captured:
        await make_repository(factory).put(uuid4(), uuid4(), forged)
    assert "PRIVATE" not in str(captured.value) + repr(captured.value)
    factory.assert_not_called()


@pytest.mark.parametrize("value", [1e20, -0.0, "left\x00right"], ids=["exponent", "negative-zero", "nul"])
async def test_canonical_text_payload_hydrates_without_numeric_or_string_coercion(value: JsonValue) -> None:
    artifact = Artifact(id=uuid4(), type="json", producer="worker", content={"value": value})
    canonical = json.dumps(
        artifact.to_payload(), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    )
    row = RuntimeArtifactRow(
        tenant_id=uuid4(), run_id=uuid4(), id=artifact.id, content_sha256=artifact.content_sha256,
        payload=canonical, byte_size=len(canonical.encode("utf-8")), permanent=False,
    )
    factory, session, _ = session_factory()
    session.scalar.return_value = row.run_id
    result = MagicMock()
    result.all.return_value = [row]
    session.scalars.return_value = result
    hydrated = await make_repository(factory).get_many(
        row.tenant_id, row.run_id, (ArtifactReference(id=artifact.id, sha256=artifact.content_sha256),),
    )
    assert len(hydrated) == 1
    assert json.dumps(
        hydrated[0].to_payload(), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ) == canonical
    assert hydrated[0].recompute_content_sha256() == artifact.content_sha256


def lookup_row(tenant: UUID, run: UUID, artifact: Artifact) -> RuntimeArtifactRow:
    payload = json.dumps(
        artifact.to_payload(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return RuntimeArtifactRow(
        tenant_id=tenant,
        run_id=run,
        id=artifact.id,
        content_sha256=artifact.content_sha256,
        payload=payload,
        byte_size=len(payload.encode()),
        permanent=True,
    )


def lookup_session(
    rows: list[RuntimeArtifactRow], owners: list[tuple[UUID, UUID]]
) -> tuple[MagicMock, AsyncMock]:
    """Evaluate the production SELECT at a local SQL expression seam, NOT PostgreSQL."""
    factory, session, _ = session_factory()

    def value(expr: Any, row: Any) -> Any:
        if isinstance(expr, BindParameter):
            return expr.value
        if isinstance(expr, Cast):
            encoded = value(expr.clause, row)
            # This seam is not PostgreSQL. Model its documented JSONB NUL
            # rejection so Python json.loads cannot make this regression green.
            if "\\u0000" in encoded:
                raise SQLAlchemyError("JSONB cannot convert a legal escaped NUL body")
            return json.loads(encoded)
        if isinstance(expr, BinaryExpression):
            left, right = value(expr.left, row), value(expr.right, row)
            if getattr(expr.operator, "opstring", None) == "->>":
                return left.get(right)
            assert expr.operator in (operators.eq, operators.gt, operators.in_op)
            return left in right if expr.operator is operators.in_op else expr.operator(left, right)
        return getattr(row, expr.key)

    def selected(statement: Any, candidates: list[Any]) -> list[Any]:
        result = [
            row for row in candidates if all(value(c, row) for c in statement._where_criteria)
        ]
        for clause in reversed(tuple(statement._order_by_clauses)):
            assert clause.element.key == "id"
            result.sort(key=lambda row: row.id, reverse=clause.modifier is operators.desc_op)
        limit = statement._limit_clause
        return result if limit is None else result[: limit.value]

    async def scope(statement: Any) -> UUID | None:
        assert statement._for_update_arg is not None
        assert statement.get_final_froms()[0].name == "agent_hub_runs"
        candidates = [MagicMock(tenant_id=tenant, id=run) for tenant, run in owners]
        scoped = selected(statement, candidates)
        return None if not scoped else scoped[0].id

    async def references(statement: Any) -> MagicMock:
        assert statement.get_final_froms()[0].name == RuntimeArtifactRow.__tablename__
        assert [column.key for column in statement.selected_columns] == ["id", "content_sha256"]
        result = MagicMock()
        result.all.return_value = [
            (row.id, row.content_sha256) for row in selected(statement, rows)
        ]
        return result

    async def bodies(statement: Any) -> MagicMock:
        assert statement.get_final_froms()[0].name == RuntimeArtifactRow.__tablename__
        result = MagicMock()
        result.all.return_value = selected(statement, rows)
        return result

    session.scalar.side_effect = scope
    session.execute.side_effect = references
    session.scalars.side_effect = bodies
    return factory, session


async def test_find_references_nul_body_fills_matching_pages_across_raw_batches() -> None:
    """Real hydration/filtering; PG JSONB failure only modeled at the SQL seam."""
    tenant, run = uuid4(), uuid4()
    values = [
        Artifact(
            id=UUID(int=index),
            type="checkpoint" if index in (2, 260, 389) else "other",
            producer="owner",
            content={"value": "left\x00right", "n": index},
        )
        for index in range(1, 391)
    ]
    rows = [lookup_row(tenant, run, value) for value in reversed(values)]
    before = [(row.id, row.payload, row.content_sha256, row.byte_size) for row in rows]
    assert all("\\u0000" in row.payload and "\x00" not in row.payload for row in rows)
    factory, session = lookup_session(rows, [(tenant, run)])
    reader = make_repository(factory, max_batch_size=2)
    first = await reader.find_references(tenant, run, type="checkpoint", producer="owner", limit=2)
    second = await reader.find_references(
        tenant, run, type="checkpoint", producer="owner", limit=2, after=first[-1].id
    )
    assert [tuple(ref.id.int for ref in page) for page in (first, second)] == [(2, 260), (389,)]
    assert (
        await reader.find_references(
            tenant, run, type="checkpoint", producer="owner", limit=2, after=second[-1].id
        )
        == ()
    )
    assert await reader.get_many(tenant, run, first) == (values[1], values[259])
    assert await reader.get_many(tenant, run, second) == (values[388],)
    assert "left" not in str([ref.to_payload() for ref in first + second])
    assert [(row.id, row.payload, row.content_sha256, row.byte_size) for row in rows] == before
    session.add.assert_not_called()
    session.flush.assert_not_awaited()
    session.delete.assert_not_awaited()


@pytest.mark.parametrize("failure", ["corrupt_nonmatching", "read_budget"])
async def test_find_references_scan_fails_closed_without_skipping_or_clamping(failure: str) -> None:
    tenant, run = uuid4(), uuid4()
    value = Artifact(
        id=UUID(int=1), type="other", producer="owner", content={"value": "left\x00right"}
    )
    row = lookup_row(tenant, run, value)
    if failure == "corrupt_nonmatching":
        payload = json.loads(row.payload)
        payload["content"] = {"value": "changed"}
        row.payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        row.byte_size = len(row.payload.encode())
    factory, session = lookup_session([row], [(tenant, run)])
    limits = {"max_total_bytes_per_run": row.byte_size - 1} if failure == "read_budget" else {}
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await make_repository(factory, **limits).find_references(
            tenant, run, type="checkpoint", producer="owner"
        )
    session.add.assert_not_called()
    session.delete.assert_not_awaited()


async def test_find_references_sql_seam_filters_scope_labels_and_pages_without_body_or_writes() -> (
    None
):
    tenant, run, foreign_tenant, other_run = uuid4(), uuid4(), uuid4(), uuid4()
    values = [
        Artifact(
            id=UUID(int=index),
            type="python_resolution_checkpoint",
            producer="python_resolution",
            content={"value": "PRIVATE_BODY", "n": index},
        )
        for index in range(1, 260)
    ]
    rows = [lookup_row(tenant, run, artifact) for artifact in reversed(values)]
    rows.extend(
        [
            lookup_row(foreign_tenant, run, values[0]),
            lookup_row(tenant, other_run, values[1]),
            lookup_row(
                tenant,
                run,
                Artifact(
                    id=uuid4(), type="other_kind", producer="python_resolution", content={"n": 0}
                ),
            ),
            lookup_row(
                tenant,
                run,
                Artifact(
                    id=uuid4(),
                    type="python_resolution_checkpoint",
                    producer="other_producer",
                    content={"n": 0},
                ),
            ),
        ]
    )
    before = [
        (row.tenant_id, row.run_id, row.id, row.content_sha256, row.payload, row.permanent)
        for row in rows
    ]
    factory, session = lookup_session(
        rows, [(tenant, run), (tenant, other_run), (foreign_tenant, run)]
    )
    repository = make_repository(factory)
    pages, cursor = [], None
    while True:
        page = await repository.find_references(
            tenant,
            run,
            type="python_resolution_checkpoint",
            producer="python_resolution",
            after=cursor,
        )
        if not page:
            break
        pages.append(page)
        cursor = page[-1].id
    assert [len(page) for page in pages] == [128, 128, 3]
    refs = tuple(ref for page in pages for ref in page)
    assert refs == tuple(
        ArtifactReference(id=value.id, sha256=value.content_sha256) for value in values
    )
    assert "PRIVATE_BODY" not in str([ref.to_payload() for ref in refs])
    assert await repository.get_many(tenant, run, refs) == tuple(values)
    assert await repository.find_references(
        tenant, other_run, type="python_resolution_checkpoint", producer="python_resolution"
    ) == (ArtifactReference(id=values[1].id, sha256=values[1].content_sha256),)
    assert (
        await repository.find_references(tenant, run, type="missing", producer="python_resolution")
        == ()
    )
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.find_references(
            uuid4(), run, type="python_resolution_checkpoint", producer="python_resolution"
        )
    assert [
        (row.tenant_id, row.run_id, row.id, row.content_sha256, row.payload, row.permanent)
        for row in rows
    ] == before
    session.add.assert_not_called()
    session.delete.assert_not_awaited()
    session.flush.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", True),
        ("type", ""),
        ("type", "bad space"),
        ("type", "x" * 129),
        ("producer", None),
        ("producer", "*"),
        ("producer", "bad\x00name"),
        ("limit", True),
        ("limit", 0),
        ("limit", -1),
        ("limit", 1.0),
        ("limit", "1"),
        ("limit", 3),
        ("after", False),
        ("after", str(uuid4())),
        ("tenant_id", False),
        ("run_id", "bad"),
    ],
)
async def test_find_references_invalid_query_opens_no_postgres_session(
    field: str, value: object
) -> None:
    factory = MagicMock()
    query = {
        "tenant_id": uuid4(),
        "run_id": uuid4(),
        "type": "checkpoint",
        "producer": "owner",
        "limit": 2,
        field: value,
    }
    with pytest.raises(ArtifactRepositoryError):
        await make_repository(factory, max_batch_size=2).find_references(**query)
    factory.assert_not_called()


@pytest.mark.parametrize("failure", ["sql", "cancel", "missing_run"])
async def test_find_references_retains_scoped_transaction_error_and_cancellation_boundaries(
    failure: str,
) -> None:
    factory, session, transaction = session_factory()
    session.scalar.return_value = None if failure == "missing_run" else uuid4()
    if failure == "sql":
        session.scalars.side_effect = SQLAlchemyError("PRIVATE_BODY_AND_SQL_SENTINEL")
    elif failure == "cancel":
        session.scalars.side_effect = asyncio.CancelledError()
    error = asyncio.CancelledError if failure == "cancel" else ArtifactRepositoryError
    with pytest.raises(error) as captured:
        await make_repository(factory).find_references(
            uuid4(), uuid4(), type="checkpoint", producer="owner"
        )
    assert "PRIVATE" not in str(captured.value) + repr(captured.value)
    if failure == "sql":
        assert captured.value.__context__ is None and captured.value.__cause__ is None
    if failure == "missing_run":
        session.scalars.assert_not_awaited()
    assert transaction.__aexit__.await_args is not None
    assert transaction.__aexit__.await_args.args[0] is not None
    session.add.assert_not_called()


@pytest.mark.parametrize(
    "identity,digest", [("PRIVATE_BAD_ID", "a" * 64), (uuid4(), "PRIVATE_BAD_HASH")]
)
async def test_find_references_rejects_corrupt_reference_columns_without_private_error_context(
    identity: object, digest: object
) -> None:
    factory, session, transaction = session_factory()
    session.scalar.return_value = uuid4()
    result = MagicMock()
    artifact = Artifact(id=uuid4(), type="checkpoint", producer="owner", content={"value": "safe"})
    row = lookup_row(uuid4(), uuid4(), artifact)
    row.id, row.content_sha256 = identity, digest
    result.all.return_value = [row]
    session.scalars.return_value = result
    with pytest.raises(ArtifactRepositoryError, match="unavailable") as captured:
        await make_repository(factory).find_references(
            uuid4(), uuid4(), type="checkpoint", producer="owner"
        )
    assert "PRIVATE" not in str(captured.value) + repr(captured.value)
    assert captured.value.__context__ is None and captured.value.__cause__ is None
    assert transaction.__aexit__.await_args is not None
    assert transaction.__aexit__.await_args.args[0] is ArtifactRepositoryError
