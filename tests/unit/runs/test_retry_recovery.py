"""A recovery retry only absorbs approved, preexecution guard-failure diagnostics."""
from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from agent_hub.db.models import RunOutboxRow, RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunNotFound, RunRepository
from agent_hub.runtime.contracts import RuntimeCheckpoint
from agent_hub.runtime.failure_reason import RECOVERY_BLOCKED_FAILURE_REASON
from tests.unit.runs.test_recovery_guard import _FakeRunRow, _FakeTransaction


class Session(_FakeTransaction):
    def __init__(self, connection: Connection, row: _FakeRunRow) -> None:
        self.connection = connection
        self.row: _FakeRunRow | None = row
        self.added: list[RunOutboxRow] = []
        self.statements: list[Any] = []

    async def scalar(self, statement: Any) -> Any:
        self.statements.append(statement)
        if statement.column_descriptions[0].get("entity") is RunRow:
            return self.row
        return self.connection.scalar(statement)

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        return self.connection.execute(statement)

    def add(self, item: RunOutboxRow) -> None:
        self.added.append(item)


class Repository(RunRepository):
    def __init__(self, session: Session, checkpoint: RuntimeCheckpoint) -> None:
        super().__init__(cast(Any, lambda: session))
        self.checkpoint: RuntimeCheckpoint | None = checkpoint

    async def latest_checkpoint(self, session: Any, *, tenant_id: Any, run_id: Any) -> RuntimeCheckpoint | None:
        del session, tenant_id, run_id
        return self.checkpoint


@pytest.fixture
def recovery_case() -> Iterator[tuple[Repository, Session, _FakeRunRow]]:
    run, tenant, actor = uuid4(), uuid4(), uuid4()
    row = _FakeRunRow(id=run, tenant_id=tenant, actor_id=actor, actor_role="operator",
        request="same approved project", mode=TaskMode.HYBRID.value,
        status=RunStatus.FAILED.value, version=9, created_at=datetime.now(UTC), routing_decision={
            "source": "self_repair", "self_repair_accepted": True, "self_repair_source_run_id": str(run),
            "self_repair_recovery_baseline_sequence": 175, "self_repair_attempt": 1,
            "self_repair_context": {"instruction": "Approved guidance", "attempt": 1},
            "project_id": "same-project", "workspace_session_id": "same-session"})
    checkpoint = RuntimeCheckpoint(id=uuid4(), runtime_type="hybrid", runtime_version="1.0",
        run_id=run, tenant_id=tenant, mode=TaskMode.HYBRID, state={"ledger": "unchanged"})
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.connect() as connection:
            connection.execute(text("CREATE TABLE agent_hub_run_checkpoints (run_id CHAR(32), sequence INTEGER)"))
            connection.execute(text("CREATE TABLE agent_hub_run_events (tenant_id CHAR(32), run_id CHAR(32), sequence INTEGER, kind TEXT, payload JSON)"))
            connection.execute(text("INSERT INTO agent_hub_run_checkpoints VALUES (:run, 171)"), {"run": run.hex})
            for sequence, kind in ((172, "step.failed"), (173, "runtime.failed"),
                                   (174, "repair.classified"), (175, "terminal.notified"), (176, "runtime.failed")):
                payload = {"reason": RECOVERY_BLOCKED_FAILURE_REASON if sequence == 176 else "original diagnostic"}
                connection.execute(text("INSERT INTO agent_hub_run_events VALUES (:tenant, :run, :sequence, :kind, :payload)"),
                    {"tenant": tenant.hex, "run": run.hex, "sequence": sequence, "kind": kind, "payload": json.dumps(payload)})
            session = Session(connection, row)
            yield Repository(session, checkpoint), session, row
    finally:
        engine.dispose()


async def retry(repository: Repository, row: _FakeRunRow, **changes: Any) -> Any:
    return await repository.retry_self_repair_recovery_and_enqueue(
        **{"tenant_id": row.tenant_id, "actor_id": row.actor_id, "run_id": row.id, "version": 9, **changes})


@pytest.mark.asyncio
async def test_guard_failure_retry_preserves_approved_context_checkpoint_and_history(recovery_case: Any) -> None:
    repository, session, row = recovery_case
    routing = deepcopy(row.routing_decision)
    assert repository.checkpoint is not None
    checkpoint = repository.checkpoint.to_payload()
    events = session.connection.execute(text("SELECT * FROM agent_hub_run_events ORDER BY sequence")).all()
    result = await retry(repository, row)
    assert result.id == row.id and result.status is RunStatus.QUEUED and result.version == 10
    assert result.routing_decision == {**routing, "self_repair_recovery_baseline_sequence": 176}
    assert repository.checkpoint.to_payload() == checkpoint
    assert session.connection.execute(text("SELECT * FROM agent_hub_run_events ORDER BY sequence")).all() == events
    assert len(session.added) == 1
    outbox = session.added[0]
    assert outbox.tenant_id == row.tenant_id and outbox.run_id == row.id
    assert outbox.task_name == "agent_hub.runs.execute" and outbox.payload == {"run_id": str(row.id)}
    assert outbox.idempotency_key == f"{row.tenant_id}:{row.id}:recovery-retry:9"
    assert session.statements[0]._for_update_arg is not None
    with pytest.raises(RunConflict):
        await retry(repository, row)
    assert len(session.added) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", (
    "actor", "tenant", "run", "missing", "stale", "cancelled", "queued", "ordinary",
    "unapproved", "foreign_source_run", "no_baseline", "bool_baseline", "no_checkpoint",
    "blocked_conversation", "worker_id", "worker_lease_token", "worker_lease_expires_at", "worker_heartbeat_at",
    "empty_tail", "other_failure", "tool_tail", "unknown_tail", "notification_tail", "unsafe_prefix",
))
async def test_retry_refuses_every_non_diagnostic_or_unowned_path(recovery_case: Any, change: str) -> None:
    repository, session, row = recovery_case
    changes: dict[str, Any] = {}
    assert row.routing_decision is not None
    if change in {"actor", "tenant", "run"}:
        changes[{"actor": "actor_id", "tenant": "tenant_id", "run": "run_id"}[change]] = uuid4()
    elif change == "missing":
        session.row = None
    elif change == "stale":
        changes["version"] = 8
    elif change in {"cancelled", "queued"}:
        row.status = change
    elif change == "ordinary":
        row.routing_decision["source"] = "manual"
    elif change == "unapproved":
        row.routing_decision["self_repair_accepted"] = False
    elif change == "foreign_source_run":
        row.routing_decision["self_repair_source_run_id"] = str(uuid4())
    elif change in {"no_baseline", "bool_baseline"}:
        row.routing_decision["self_repair_recovery_baseline_sequence"] = 0 if change == "no_baseline" else True
    elif change == "no_checkpoint":
        repository.checkpoint = None
    elif change == "blocked_conversation":
        row.blocked_by_run_id = uuid4()
    elif change.startswith("worker_"):
        setattr(row, change, "worker" if change == "worker_id" else
                uuid4() if change == "worker_lease_token" else datetime.now(UTC))
    elif change == "empty_tail":
        session.connection.execute(text("DELETE FROM agent_hub_run_events WHERE sequence > 175"))
    elif change == "other_failure":
        session.connection.execute(text("UPDATE agent_hub_run_events SET payload=:payload WHERE sequence=176"),
                                   {"payload": json.dumps({"reason": "unknown model outcome"})})
    elif change == "unsafe_prefix":
        session.connection.execute(text("UPDATE agent_hub_run_events SET kind='artifact.created' WHERE sequence=174"))
    else:
        kind = {"tool_tail": "tool.completed", "unknown_tail": "model.unknown", "notification_tail": "terminal.notified"}[change]
        session.connection.execute(text("UPDATE agent_hub_run_events SET kind=:kind WHERE sequence=176"), {"kind": kind})
    before = deepcopy(row)
    error = RunNotFound if change in {"actor", "tenant", "run", "missing"} else RunConflict
    with pytest.raises(error):
        await retry(repository, row, **changes)
    assert row == before and session.added == []
