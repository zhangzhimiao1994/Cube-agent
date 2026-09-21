from __future__ import annotations

import asyncio
import json
from importlib import import_module
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
