"""Persisted malformed envelopes reject issuance without losing an existing hold."""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from agent_hub.db.models import RunAdmissionAttemptRow, RunExecutionClockRow, RunRow
from agent_hub.models.run_admission_repository import (
    AdmissionConflict,
    DeploymentIdentity,
    RunAdmissionRepository,
    _ExecutionArguments,
    _WorkerLease,
)
from tests.unit.runs.test_execution_clock_store import SQLClock, db  # noqa: F401


@pytest.mark.parametrize("corruption", [["not-a-server-envelope"], [{"execution_clock_version": 2}]])
async def test_persisted_route_list_cannot_issue_or_refund_held_tokens(
    db: SQLClock, corruption: list[object],  # noqa: F811
) -> None:
    run = await db.submit()
    await db.claim(run.id)
    async with db.factory.begin() as session:
        row = await session.get(RunRow, run.id)
        assert row is not None and row.routing_decision is not None
        row.routing_decision = {**row.routing_decision,
            "runtime_token_absolute_tokens": 100, "runtime_plan_token_budget": 100}
    async with db.factory() as session:
        clock = await session.get(RunExecutionClockRow, run.id)
        assert clock is not None
        turn = clock.authorization_turn_id
        original_h = clock.hard_deadline
        original_clock = (clock.binding_sha256, clock.policy, clock.spent_seconds, clock.version)
    repository = RunAdmissionRepository(db.factory)
    binding: _ExecutionArguments = {
        "tenant_id": db.tenant, "run_id": run.id, "authorization_turn_id": turn,
        "lease": _WorkerLease(db.worker, db.token), "config_sha256": "a"*64,
    }
    await repository.open_pool(**binding, original_token_grant=100,
        original_deadline=original_h, review_token_limit=80,
        review_count_limit=3, review_cost_limit=Decimal(1))
    attempt = await repository.reserve(**binding, attempt_id=uuid4(), purpose="primary",
        request_sha256="b"*64, deployment=DeploymentIdentity("synthetic", "deployment", "model"),
        token_hold=10, review_cost_hold=None)
    async with db.factory.begin() as session:
        await session.execute(update(RunRow).where(RunRow.id == run.id)
            .values(routing_decision=corruption))
    with pytest.raises(AdmissionConflict, match="^admission state conflict$"):
        await repository.mark_issued(**binding, attempt_id=attempt.attempt_id,
            physical_transport_id=uuid4())
    async with db.factory() as session:
        stored = await session.scalar(select(RunAdmissionAttemptRow).where(
            RunAdmissionAttemptRow.attempt_id == attempt.attempt_id))
        assert stored is not None and stored.state == "prepared"
        assert stored.physical_transport_id is None
        clock = await session.get(RunExecutionClockRow, run.id)
        assert clock is not None and clock.hard_deadline == original_h
        assert (clock.binding_sha256, clock.policy, clock.spent_seconds, clock.version) == original_clock
    pool = await repository.get_pool(db.tenant, run.id)
    assert pool is not None and pool.original_token_grant == 100 and pool.token_charged == 10
