from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.contracts import EventKind, RunEvent


class RecordingSession:
    def __init__(self) -> None:
        self.statement: Any | None = None

    async def execute(self, statement: Any) -> None:
        self.statement = statement


def _compiled_upsert_statement(session: RecordingSession) -> str:
    assert session.statement is not None
    return str(session.statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


@pytest.mark.asyncio
async def test_capability_approval_resolution_upsert_refreshes_row_action_on_conflict() -> None:
    session = RecordingSession()

    await RunRepository._upsert_capability_approval(
        session,  # type: ignore[arg-type]
        uuid4(),
        uuid4(),
        approval_id="approval-1",
        approval_fingerprint="fingerprint-1",
        status="approved",
    )

    statement = _compiled_upsert_statement(session)
    assert "ON CONFLICT (run_id, approval_id) DO UPDATE" in statement
    assert "SET action = " in statement


@pytest.mark.asyncio
async def test_capability_approval_review_upsert_refreshes_row_action_on_conflict() -> None:
    session = RecordingSession()

    await RunRepository._upsert_capability_approval_review(
        session,  # type: ignore[arg-type]
        uuid4(),
        uuid4(),
        approval_id="approval-1",
        approval_fingerprint="fingerprint-1",
        reviewer="auto_policy",
        reason="safe",
        status="approved",
    )

    statement = _compiled_upsert_statement(session)
    assert "ON CONFLICT (run_id, approval_id) DO UPDATE" in statement
    assert "SET action = " in statement


@pytest.mark.asyncio
async def test_runtime_approval_event_upsert_refreshes_row_action_on_conflict() -> None:
    session = RecordingSession()
    run_id = uuid4()

    await RunRepository._persist_approval(
        session,  # type: ignore[arg-type]
        uuid4(),
        run_id,
        RunEvent(
            kind=EventKind.APPROVAL_RESOLVED,
            sequence=2,
            run_id=run_id,
            actor="operator",
            approval_id="approval-1",
            decision="approved",
        ),
    )

    statement = _compiled_upsert_statement(session)
    assert "ON CONFLICT (run_id, approval_id) DO UPDATE" in statement
    assert "SET action = " in statement
