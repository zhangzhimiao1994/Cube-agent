from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import RunEventRow
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent
from agent_hub.runtime.failure_reason import runtime_failure_diagnostic_from_reason

REASONS = (
    "runtime recovery blocked: non-replayable event after checkpoint",
    "model gateway failed: model response failed (status=400)",
    "runtime orchestration checkpoint is incompatible",
    "hybrid dispatch failed: model gateway failed: model capacity unavailable",
    "CrewAI step timed out: step=draft actor=writer",
    "model response incomplete",
    "hybrid dispatch failed: model response incomplete",
)


@pytest.mark.parametrize("reason", REASONS)
async def test_persisted_failure_payload_replays_with_reason_and_public_diagnostic(reason: str) -> None:
    tenant_id, run_id = uuid4(), uuid4()
    session = AsyncMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    session.scalar.return_value = uuid4()
    factory = cast(async_sessionmaker[AsyncSession], MagicMock(return_value=session))
    repository = RunRepository(factory)
    original = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=10, run_id=run_id, reason=reason)

    await repository.persist_event(session, tenant_id=tenant_id, run_id=run_id, event=original)

    assert session.scalar.await_args is not None
    statement = session.scalar.await_args.args[0]
    # Capture the actual INSERT payload, not a hand-written approximation of enrichment.
    payload = json.loads(json.dumps(statement.compile().params["payload"]))
    expected = runtime_failure_diagnostic_from_reason(reason)
    assert payload["reason"] == reason and payload["payload"] == expected
    assert original.payload == {}
    rebuilt = RunEvent.from_payload(payload)
    assert rebuilt.reason == reason and dict(rebuilt.payload) == expected

    row = RunEventRow(
        id=uuid4(), tenant_id=tenant_id, run_id=run_id, sequence=10,
        kind="runtime.failed", payload=payload, created_at=datetime.now(UTC),
    )
    rows = MagicMock()
    rows.all.return_value = [row]
    session.scalars.return_value = rows
    replayed = await repository.raw_events(tenant_id, run_id)
    assert replayed == (rebuilt,)
    assert replayed[0].kind is EventKind.RUNTIME_FAILED
    public = await repository.events(tenant_id, run_id)
    assert public[0]["reason"] == reason
    assert public[0]["payload"] == expected


@pytest.mark.parametrize("pre_enriched", [False, True])
async def test_failure_enrichment_rejects_unvalidated_unknown_fields_before_insert(pre_enriched: bool) -> None:
    session = AsyncMock(spec=AsyncSession)
    repository = RunRepository(cast(async_sessionmaker[AsyncSession], MagicMock()))
    original = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(), reason=REASONS[0])
    payload: dict[str, object] = dict(runtime_failure_diagnostic_from_reason(REASONS[0])) if pre_enriched else {}
    forged = original.model_copy(update={"payload": {**payload, "uncontrolled_extension": "private"}})
    with pytest.raises(ValueError):
        await repository.persist_event(session, tenant_id=uuid4(), run_id=original.run_id, event=forged)
    session.scalar.assert_not_awaited()


@pytest.mark.parametrize("entry", ["reason_only", "producer", "producer_with_scope"])
async def test_step_then_runtime_incomplete_persists_and_public_replays_original_classification(
    entry: str,
) -> None:
    tenant_id, run_id = uuid4(), uuid4()
    reason = "model response incomplete"
    diagnostic = runtime_failure_diagnostic_from_reason(reason)
    scope = {
        "gateway_scope_phase": "transport", "gateway_scope_reason": "rejected_output",
        "gateway_scope_transport_entered_count": 1, "gateway_scope_failure_attempt_count": 1,
    } if entry == "producer_with_scope" else {}
    step = RunEvent(
        kind=EventKind.STEP_FAILED, sequence=131, run_id=run_id,
        step_id="draft", actor="writer", reason=reason,
        payload=cast(dict[str, JsonValue], {**diagnostic, **scope, "status": "blocked"}),
    )
    runtime = RunEvent(
        kind=EventKind.RUNTIME_FAILED, sequence=132, run_id=run_id, reason=reason,
        payload=cast(dict[str, JsonValue], {} if entry == "reason_only" else {**diagnostic, **scope}),
    )
    session = AsyncMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    session.scalar.return_value = uuid4()
    repository = RunRepository(cast(async_sessionmaker[AsyncSession], MagicMock(return_value=session)))
    stored: list[dict[str, object]] = []
    for event in (step, runtime):
        await repository.persist_event(session, tenant_id=tenant_id, run_id=run_id, event=event)
        assert session.scalar.await_args is not None
        statement = session.scalar.await_args.args[0]
        stored.append(json.loads(json.dumps(statement.compile().params["payload"])))

    rows = MagicMock()
    rows.all.return_value = [
        RunEventRow(
            id=uuid4(), tenant_id=tenant_id, run_id=run_id, sequence=event.sequence,
            kind=str(event.kind), payload=value, created_at=datetime.now(UTC),
        )
        for event, value in zip((step, runtime), stored, strict=True)
    ]
    session.scalars.return_value = rows
    replayed = await repository.raw_events(tenant_id, run_id)
    assert [event.kind for event in replayed] == [EventKind.STEP_FAILED, EventKind.RUNTIME_FAILED]
    assert [event.sequence for event in replayed] == [131, 132]
    public = await repository.events(tenant_id, run_id)
    for event, projected in zip(replayed, public, strict=True):
        assert event.reason == projected["reason"] == reason
        assert RunEvent.from_payload(event.to_payload()) == event
        public_payload = cast(dict[str, object], projected["payload"])
        for key, value in diagnostic.items():
            assert event.payload[key] == public_payload[key] == value
        for scope_key, scope_value in scope.items():
            assert event.payload[scope_key] == public_payload[scope_key] == scope_value
        assert event.payload["error_code"] == "model.incomplete_response"
        assert event.payload["compression_trigger"] == "incomplete_response"
    assert dict(runtime.payload) == ({} if entry == "reason_only" else {**diagnostic, **scope})


@pytest.mark.parametrize(("key", "value"), [
    ("compression_trigger", None), ("compression_trigger", True),
    ("compression_trigger", "structured_output_invalid"),
    ("error_code", "runtime.failed"), ("error_stage", "runtime"),
    ("error_category", "internal"), ("uncontrolled_extension", "private"),
])
async def test_repository_rejects_forged_incomplete_diagnostic_before_insert(
    key: str, value: object,
) -> None:
    reason = "model response incomplete"
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=132, run_id=uuid4(), reason=reason)
    payload: dict[str, object] = dict(runtime_failure_diagnostic_from_reason(reason))
    payload[key] = value
    forged = event.model_copy(update={"payload": payload})
    session = AsyncMock(spec=AsyncSession)
    repository = RunRepository(cast(async_sessionmaker[AsyncSession], MagicMock()))
    with pytest.raises(ValueError):
        await repository.persist_event(session, tenant_id=uuid4(), run_id=event.run_id, event=forged)
    session.scalar.assert_not_awaited()
