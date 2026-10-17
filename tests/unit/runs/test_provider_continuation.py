from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import replace
from importlib import import_module
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from tests.unit.runtime.test_provider_continuation import snapshot


def session_factory() -> tuple[MagicMock, AsyncMock, AsyncMock]:
    session = AsyncMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    transaction = AsyncMock()
    transaction.__aexit__.return_value = False
    session.begin = MagicMock(return_value=transaction)
    return MagicMock(return_value=session), session, transaction


def repository(factory: object, **limits: Any) -> Any:
    return import_module("agent_hub.runs.provider_continuation").PostgresProviderContinuationRepository(
        factory, **limits,
    )


def row_for(value: Any) -> Any:
    from agent_hub.db.models import ProviderContinuationRow
    from agent_hub.runtime.provider_continuation import encode_snapshot

    payload = encode_snapshot(value)
    return ProviderContinuationRow(
        id=value.id, tenant_id=value.tenant_id, run_id=value.run_id,
        ledger_key=value.ledger_key, sha256=value.sha256, payload=payload,
        byte_size=len(payload.encode()),
    )


@pytest.mark.parametrize("operation", ["put", "get"])
async def test_same_tenant_run_locked_before_private_access(operation: str) -> None:
    from agent_hub.runtime.provider_continuation import ProviderContinuationRepositoryError

    value = snapshot()
    factory, session, _ = session_factory()
    session.scalar.return_value = None
    with pytest.raises(ProviderContinuationRepositoryError):
        if operation == "put":
            await repository(factory).put(value)
        else:
            await repository(factory).get(value.tenant_id, value.run_id, value.id, value.sha256)
    session.scalar.assert_awaited_once()
    query = cast(Select[Any], session.scalar.await_args.args[0]).compile()
    assert "FOR UPDATE" in str(query) and "tenant_id" in str(query)
    assert value.tenant_id in query.params.values() and value.run_id in query.params.values()
    session.add.assert_not_called()
    session.get.assert_not_awaited()
    session.execute.assert_not_awaited()


async def test_put_scoped_ledger_quota_then_insert_canonical_private_text() -> None:
    value = snapshot()
    factory, session, transaction = session_factory()
    session.scalar.side_effect = [value.run_id, None]
    session.get.return_value = None
    result = MagicMock()
    result.one.return_value = (0, 0)
    session.execute.return_value = result
    await repository(factory).put(value)
    added = session.add.call_args.args[0]
    assert added.id == value.id and added.tenant_id == value.tenant_id and added.run_id == value.run_id
    assert added.ledger_key == value.ledger_key and added.sha256 == value.sha256
    assert added.payload == json.dumps(value.to_payload(), sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":"))
    assert added.byte_size == len(added.payload.encode())
    assert transaction.__aexit__.await_args.args[0] is None
    query = cast(Select[Any], session.scalar.await_args_list[1].args[0]).compile()
    assert {value.tenant_id, value.run_id, value.ledger_key} <= set(query.params.values())
    quota = cast(Select[Any], session.execute.await_args.args[0]).compile()
    assert value.tenant_id in quota.params.values() and value.run_id in quota.params.values()
    assert "sum(" in str(quota) and "count(" in str(quota)


async def test_get_scoped_canonical_record_roundtrip_without_insert() -> None:
    value = snapshot()
    factory, session, _ = session_factory()
    session.scalar.side_effect = [value.run_id, row_for(value)]
    assert await repository(factory).get(value.tenant_id, value.run_id, value.id, value.sha256) == value
    query = cast(Select[Any], session.scalar.await_args_list[1].args[0]).compile()
    assert {value.tenant_id, value.run_id, value.id} <= set(query.params.values())
    session.add.assert_not_called()
    session.execute.assert_not_awaited()


async def test_put_identical_is_idempotent_but_replacement_or_duplicate_ledger_rejected() -> None:
    from agent_hub.runtime.provider_continuation import ProviderContinuationRepositoryError

    value = snapshot()
    factory, session, _ = session_factory()
    session.scalar.return_value = value.run_id
    session.get.return_value = row_for(value)
    await repository(factory).put(value)
    session.add.assert_not_called()
    session.execute.assert_not_awaited()
    for changed in (replace(value, request_sha256="d" * 64), replace(value, run_id=uuid4())):
        with pytest.raises(ProviderContinuationRepositoryError):
            await repository(factory).put(changed)
    session.get.return_value = None
    session.scalar.side_effect = [value.run_id, row_for(value)]
    with pytest.raises(ProviderContinuationRepositoryError):
        await repository(factory).put(replace(value, id=uuid4()))
    session.add.assert_not_called()


@pytest.mark.parametrize("corruption", [
    "payload", "private_invalid", "row_hash", "size", "size_bool", "tenant", "run", "id",
    "ledger", "canonical", "duplicate_json", "missing",
])
async def test_get_fail_closed_on_tampered_or_foreign_private_row(corruption: str) -> None:
    from agent_hub.runtime.provider_continuation import ProviderContinuationRepositoryError

    value = snapshot()
    row = row_for(value)
    if corruption == "payload":
        row.payload = row.payload.replace("PRIVATE_REASONING_SENTINEL", "PRIVATE_TAMPERED")
        row.byte_size = len(row.payload.encode())
    elif corruption == "private_invalid":
        row.payload = '{"PRIVATE_INVALID":1}'
        row.byte_size = len(row.payload)
    elif corruption == "row_hash":
        row.sha256 = "0" * 64
    elif corruption == "size":
        row.byte_size += 1
    elif corruption == "size_bool":
        row.byte_size = True
    elif corruption in {"tenant", "run", "id"}:
        setattr(row, {"tenant": "tenant_id", "run": "run_id", "id": "id"}[corruption], uuid4())
    elif corruption == "ledger":
        row.ledger_key = "different"
    elif corruption == "canonical":
        row.payload += " "
        row.byte_size += 1
    elif corruption == "duplicate_json":
        row.payload = row.payload.replace('{"assistant":', '{"version":1,"assistant":', 1)
        row.byte_size = len(row.payload.encode())
    factory, session, _ = session_factory()
    session.scalar.side_effect = [value.run_id, None if corruption == "missing" else row]
    with pytest.raises(ProviderContinuationRepositoryError) as caught:
        await repository(factory).get(value.tenant_id, value.run_id, value.id, value.sha256)
    assert "PRIVATE" not in repr(caught.value) + str(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize("operation", ["put", "get"])
@pytest.mark.parametrize("point", ["lock", "commit"])
async def test_cancellation_propagates_from_lock_or_commit(operation: str, point: str) -> None:
    value = snapshot()
    factory, session, transaction = session_factory()
    if point == "lock":
        session.scalar.side_effect = asyncio.CancelledError()
    else:
        session.scalar.side_effect = [value.run_id, row_for(value)]
        session.get.return_value = row_for(value)
        transaction.__aexit__.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        if operation == "put":
            await repository(factory).put(value)
        else:
            await repository(factory).get(value.tenant_id, value.run_id, value.id, value.sha256)
    session.commit.assert_not_awaited()
    session.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("operation", ["put", "get"])
@pytest.mark.parametrize("point", ["lock", "commit"])
async def test_database_errors_do_not_expose_sql_parameters_or_context(operation: str, point: str) -> None:
    from agent_hub.runtime.provider_continuation import ProviderContinuationRepositoryError

    value = snapshot()
    factory, session, transaction = session_factory()
    private_error = SQLAlchemyError("PRIVATE_DRIVER_PAYLOAD")
    if point == "lock":
        session.scalar.side_effect = private_error
    else:
        session.scalar.side_effect = [value.run_id, row_for(value)]
        session.get.return_value = row_for(value)
        transaction.__aexit__.side_effect = private_error
    with pytest.raises(ProviderContinuationRepositoryError) as caught:
        if operation == "put":
            await repository(factory).put(value)
        else:
            await repository(factory).get(value.tenant_id, value.run_id, value.id, value.sha256)
    assert "PRIVATE" not in str(caught.value) + repr(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None


@pytest.mark.parametrize("limits", [
    {"max_snapshots_per_run": 1}, {"max_total_bytes_per_run": 1}, {"max_snapshot_bytes": 1},
])
async def test_postgres_quota_blocks_before_insert(limits: dict[str, int]) -> None:
    from agent_hub.runtime.provider_continuation import ProviderContinuationRepositoryError

    value = snapshot()
    factory, session, _ = session_factory()
    session.scalar.side_effect = [value.run_id, None]
    session.get.return_value = None
    usage = MagicMock()
    usage.one.return_value = (1, 1)
    session.execute.return_value = usage
    with pytest.raises(ProviderContinuationRepositoryError):
        await repository(factory, **limits).put(value)
    session.add.assert_not_called()


@pytest.mark.parametrize("bad", [0, -1, True, "1", 1.5])
def test_invalid_limits_do_not_open_database(bad: Any) -> None:
    factory = MagicMock()
    with pytest.raises(ValueError):
        repository(factory, max_snapshot_bytes=bad)
    factory.assert_not_called()


@pytest.mark.parametrize("operation", ["put", "get"])
@pytest.mark.parametrize("cleanup", ["rollback", "close", "both"])
async def test_body_cancellation_wins_over_cleanup_sql_errors(operation: str, cleanup: str) -> None:
    value = snapshot()
    factory, session, transaction = session_factory()
    cancellation = asyncio.CancelledError("original cancellation")
    if operation == "put":
        session.scalar.return_value = value.run_id
        session.get.side_effect = cancellation
    else:
        session.scalar.side_effect = [value.run_id, cancellation]
    if cleanup in {"rollback", "both"}:
        transaction.__aexit__.side_effect = IntegrityError("rollback", {"payload": "PRIVATE"}, RuntimeError("PRIVATE"))
    if cleanup in {"close", "both"}:
        session.__aexit__.side_effect = IntegrityError("close", {"payload": "PRIVATE"}, RuntimeError("PRIVATE"))
    with pytest.raises(asyncio.CancelledError) as caught:
        if operation == "put":
            await repository(factory).put(value)
        else:
            await repository(factory).get(value.tenant_id, value.run_id, value.id, value.sha256)
    assert caught.value is cancellation
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert "PRIVATE" not in "".join(traceback.format_exception(caught.value))
    session.add.assert_not_called()


@pytest.mark.parametrize("cleanup", ["rollback", "close"])
async def test_get_body_sql_failure_has_no_private_traceback_after_cleanup(cleanup: str) -> None:
    from agent_hub.runtime.provider_continuation import ProviderContinuationRepositoryError

    value = snapshot()
    factory, session, transaction = session_factory()
    session.scalar.side_effect = [value.run_id, IntegrityError("get", {"body": "PRIVATE_BODY"}, RuntimeError("PRIVATE"))]
    target = transaction if cleanup == "rollback" else session
    target.__aexit__.side_effect = IntegrityError("cleanup", {"body": "PRIVATE_CLEANUP"}, RuntimeError("PRIVATE"))
    with pytest.raises(ProviderContinuationRepositoryError) as caught:
        await repository(factory).get(value.tenant_id, value.run_id, value.id, value.sha256)
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert "PRIVATE" not in "".join(traceback.format_exception(caught.value))
