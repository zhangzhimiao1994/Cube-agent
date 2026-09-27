from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self, cast
from uuid import UUID

import pytest

from agent_hub import app as app_module
from agent_hub.api.routers import admin
from agent_hub.domain.runs import TaskMode
from agent_hub.scheduler.service import SchedulerService
from agent_hub.scheduler.types import CronScheduleSpec, OneTimeScheduleSpec, ScheduleDefinition

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
OWNER_ID = UUID("00000000-0000-4000-8000-000000000002")
SCHEDULE_ID = UUID("00000000-0000-4000-8000-000000000003")
RUN_ID = UUID("00000000-0000-4000-8000-000000000004")
FIRE_AT = datetime(2026, 9, 27, 1, 0, tzinfo=UTC)


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class _RowsResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: self._rows)


class _Session:
    def __init__(
        self,
        *,
        leader: bool,
        rows: list[Any],
        claim_delete_failures: int = 0,
    ) -> None:
        self.leader = leader
        self.rows = rows
        self.statements: list[str] = []
        self.begin_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.claim_delete_failures = claim_delete_failures

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def begin(self) -> None:
        self.begin_calls += 1

    async def execute(self, statement: object, parameters: object = None) -> object:
        rendered = str(statement)
        self.statements.append(rendered)
        if "pg_try_advisory_xact_lock" in rendered:
            return _ScalarResult(self.leader)
        if "DELETE FROM agent_hub_admin_resources" in rendered:
            if self.claim_delete_failures:
                self.claim_delete_failures -= 1
                raise RuntimeError("claim acknowledgement failed")
            assert isinstance(parameters, dict)
            resource_id = parameters["resource_id"]
            tenant_id = parameters["tenant_id"]
            self.rows[:] = [
                row
                for row in self.rows
                if row.resource_id != resource_id or row.tenant_id != tenant_id
            ]
            return _RowsResult([])
        return _RowsResult(self.rows)

    def add(self, row: Any) -> None:
        self.rows.append(row)

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


class _SessionFactory:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def __call__(self) -> _Session:
        return self.session


def _schedule() -> ScheduleDefinition:
    spec = OneTimeScheduleSpec(run_at=FIRE_AT, timezone="UTC")
    return ScheduleDefinition(
        id=SCHEDULE_ID,
        tenant_id=TENANT_ID,
        owner_id=OWNER_ID,
        name="nightly-report",
        message="generate report",
        mode=TaskMode.AUTO,
        workflow="scheduled_task",
        budget=512,
        spec=spec,
        idempotency_key="nightly-report",
        next_fire_at=FIRE_AT,
    )


def _cron_schedule() -> ScheduleDefinition:
    return ScheduleDefinition(
        id=SCHEDULE_ID,
        tenant_id=TENANT_ID,
        owner_id=OWNER_ID,
        name="minute-report",
        message="generate report",
        mode=TaskMode.AUTO,
        workflow="scheduled_task",
        budget=512,
        spec=CronScheduleSpec(expression="* * * * *", timezone="UTC"),
        idempotency_key="minute-report",
        next_fire_at=FIRE_AT,
    )


@pytest.mark.asyncio
async def test_scheduler_tick_loop_runs_immediately_and_survives_tick_failure() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def tick_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary database outage")

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if attempts == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await app_module._run_scheduler_tick_loop(
            tick_once,
            interval_seconds=2.5,
            sleep=sleep,
        )

    assert attempts == 2
    assert sleeps == [2.5, 2.5]


@pytest.mark.asyncio
async def test_persisted_scheduler_tick_uses_lock_and_persists_advanced_state() -> None:
    submitted: list[object] = []
    session: _Session

    async def submit(task: object) -> UUID:
        assert session.commit_calls == 1
        assert row.payload["status"] == "completed"
        assert row.payload["next_fire_at"] is None
        submitted.append(task)
        return RUN_ID

    schedule = _schedule()
    row = SimpleNamespace(
        tenant_id=TENANT_ID,
        resource_id=str(SCHEDULE_ID),
        payload=admin._schedule_to_payload(schedule),
    )
    session = _Session(leader=True, rows=[row])
    service = SchedulerService(submit)

    fired = await app_module._tick_persisted_schedules_once(
        cast(Any, _SessionFactory(session)),
        service,
        now=FIRE_AT,
    )

    assert fired == (SCHEDULE_ID,)
    assert len(submitted) == 1
    assert session.begin_calls == 2
    assert session.commit_calls == 2
    assert session.rollback_calls == 0
    assert any("pg_try_advisory_xact_lock" in statement for statement in session.statements)
    assert row.payload["status"] == "completed"
    assert row.payload["next_fire_at"] is None


@pytest.mark.asyncio
async def test_persisted_scheduler_tick_claims_only_requested_tenant() -> None:
    other_tenant_id = UUID("00000000-0000-4000-8000-000000000099")
    other_schedule = _schedule()
    other_schedule = ScheduleDefinition(
        id=UUID("00000000-0000-4000-8000-000000000098"),
        tenant_id=other_tenant_id,
        owner_id=other_schedule.owner_id,
        name=other_schedule.name,
        message=other_schedule.message,
        mode=other_schedule.mode,
        workflow=other_schedule.workflow,
        budget=other_schedule.budget,
        spec=other_schedule.spec,
        idempotency_key=other_schedule.idempotency_key,
        next_fire_at=other_schedule.next_fire_at,
    )
    own_row = SimpleNamespace(
        tenant_id=TENANT_ID,
        resource_id=str(SCHEDULE_ID),
        payload=admin._schedule_to_payload(_schedule()),
    )
    other_row = SimpleNamespace(
        tenant_id=other_tenant_id,
        resource_id=str(other_schedule.id),
        payload=admin._schedule_to_payload(other_schedule),
    )
    session = _Session(leader=True, rows=[own_row, other_row])
    submitted: list[object] = []

    async def submit(task: object) -> UUID:
        submitted.append(task)
        return RUN_ID

    fired = await app_module._tick_persisted_schedules_once(
        cast(Any, _SessionFactory(session)),
        SchedulerService(submit),
        now=FIRE_AT,
        tenant_id=TENANT_ID,
    )

    assert fired == (SCHEDULE_ID,)
    assert len(submitted) == 1
    assert own_row.payload["status"] == "completed"
    assert other_row.payload["status"] == "active"


@pytest.mark.asyncio
async def test_persisted_scheduler_tick_skips_when_another_instance_holds_lock() -> None:
    submitted: list[object] = []

    async def submit(task: object) -> UUID:
        submitted.append(task)
        return RUN_ID

    session = _Session(leader=False, rows=[])
    service = SchedulerService(submit)

    fired = await app_module._tick_persisted_schedules_once(
        cast(Any, _SessionFactory(session)),
        service,
        now=FIRE_AT,
    )

    assert fired == ()
    assert submitted == []
    assert session.begin_calls == 1
    assert session.commit_calls == 0
    assert session.rollback_calls == 1


@pytest.mark.asyncio
async def test_replace_schedules_reloads_database_truth_after_failed_persistence() -> None:
    submitted: list[object] = []

    async def submit(task: object) -> UUID:
        submitted.append(task)
        return RUN_ID

    service = SchedulerService(submit)
    schedule = _schedule()
    await service.replace_schedules((schedule,))
    assert await service.tick(now=FIRE_AT) == (SCHEDULE_ID,)

    await service.replace_schedules((schedule,))
    assert await service.tick(now=FIRE_AT) == (SCHEDULE_ID,)
    assert len(submitted) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule", [_schedule(), _cron_schedule()])
async def test_persisted_claim_retries_failed_submission_without_losing_fire(
    schedule: ScheduleDefinition,
) -> None:
    attempts: list[str] = []

    async def submit(task: object) -> UUID:
        idempotency_key = cast(Any, task).idempotency_key
        attempts.append(idempotency_key)
        if len(attempts) == 1:
            raise RuntimeError("run submission unavailable")
        return RUN_ID

    schedule_row = SimpleNamespace(
        tenant_id=TENANT_ID,
        resource_id=str(SCHEDULE_ID),
        payload=admin._schedule_to_payload(schedule),
    )
    session = _Session(leader=True, rows=[schedule_row])
    service = SchedulerService(submit)
    session_factory = cast(Any, _SessionFactory(session))

    first = await app_module._tick_persisted_schedules_once(
        session_factory,
        service,
        now=FIRE_AT,
    )
    claim_rows = [row for row in session.rows if row.payload.get("record_type") == "claim"]

    assert first == ()
    assert len(claim_rows) == 1
    assert schedule_row.payload["next_fire_at"] != FIRE_AT.isoformat()

    second = await app_module._tick_persisted_schedules_once(
        session_factory,
        service,
        now=FIRE_AT,
    )

    assert second == (SCHEDULE_ID,)
    assert attempts == [attempts[0], attempts[0]]
    assert all(row.payload.get("record_type") != "claim" for row in session.rows)


@pytest.mark.asyncio
async def test_persisted_claim_retry_after_ack_failure_is_run_idempotent() -> None:
    invocations: list[str] = []
    created_runs: dict[str, UUID] = {}

    async def submit(task: object) -> UUID:
        idempotency_key = cast(Any, task).idempotency_key
        invocations.append(idempotency_key)
        return created_runs.setdefault(idempotency_key, RUN_ID)

    schedule_row = SimpleNamespace(
        tenant_id=TENANT_ID,
        resource_id=str(SCHEDULE_ID),
        payload=admin._schedule_to_payload(_schedule()),
    )
    session = _Session(
        leader=True,
        rows=[schedule_row],
        claim_delete_failures=1,
    )
    service = SchedulerService(submit)
    session_factory = cast(Any, _SessionFactory(session))

    first = await app_module._tick_persisted_schedules_once(
        session_factory,
        service,
        now=FIRE_AT,
    )
    second = await app_module._tick_persisted_schedules_once(
        session_factory,
        service,
        now=FIRE_AT,
    )

    assert first == (SCHEDULE_ID,)
    assert second == (SCHEDULE_ID,)
    assert invocations == [invocations[0], invocations[0]]
    assert len(created_runs) == 1
    assert all(row.payload.get("record_type") != "claim" for row in session.rows)
