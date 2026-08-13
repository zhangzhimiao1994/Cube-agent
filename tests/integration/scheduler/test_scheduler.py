from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.scheduler.service import SchedulerService
from agent_hub.scheduler.types import (
    CronSchedule,
    CronScheduleSpec,
    OneTimeScheduleSpec,
    ScheduleDefinition,
    ScheduleKind,
    ScheduleMisfirePolicy,
    ScheduleStatus,
    TaskRequest,
)

TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")
OWNER_A = UUID("33333333-3333-4333-8333-333333333333")


@dataclass(frozen=True, slots=True)
class Submitted:
    id: UUID


class RecordingSubmitter:
    def __init__(self) -> None:
        self.calls: list[TaskRequest] = []
        self._by_key: dict[str, Submitted] = {}
        self.fail_once = False

    async def submit(self, request: TaskRequest) -> object:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("submitter unavailable")
        self.calls.append(request)
        return self._by_key.setdefault(request.idempotency_key, Submitted(uuid4()))


async def test_repeated_scheduler_tick_submits_once() -> None:
    submitter = RecordingSubmitter()
    scheduler = SchedulerService(submitter.submit)
    fire_time = datetime(2026, 8, 7, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    schedule = await scheduler.add_schedule(
        ScheduleDefinition(
            tenant_id=TENANT_A,
            owner_id=OWNER_A,
            name="daily-report",
            mode=TaskMode.DISPATCH,
            workflow="daily_report",
            message="create daily report",
            timezone="Asia/Shanghai",
            kind=ScheduleKind.ONE_TIME,
            fire_at=fire_time,
        ),
        now=datetime(2026, 8, 7, 8, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    first = await scheduler.tick(now=fire_time)
    second = await scheduler.tick(now=fire_time)

    assert len(first) == 1
    assert second == ()
    assert await scheduler.submission_count(schedule.id, fire_time, tenant_id=TENANT_A) == 1
    assert len(submitter.calls) == 1
    assert submitter.calls[0].mode is TaskMode.DISPATCH
    assert submitter.calls[0].metadata["schedule_id"] == str(schedule.id)
    assert submitter.calls[0].idempotency_key.startswith("schedule:")
    assert len(f"{TENANT_A}:{submitter.calls[0].idempotency_key}") <= 128


async def test_failed_scheduler_submission_is_retried_on_next_tick() -> None:
    submitter = RecordingSubmitter()
    submitter.fail_once = True
    scheduler = SchedulerService(submitter.submit)
    fire_time = datetime(2026, 8, 7, 9, tzinfo=ZoneInfo("UTC"))
    await scheduler.add_schedule(
        ScheduleDefinition(
            tenant_id=TENANT_A,
            owner_id=OWNER_A,
            name="retry-report",
            mode=TaskMode.DISPATCH,
            workflow="retry_report",
            message="create retry report",
            timezone="UTC",
            kind=ScheduleKind.ONE_TIME,
            fire_at=fire_time,
        ),
        now=datetime(2026, 8, 7, 8, tzinfo=ZoneInfo("UTC")),
    )

    with pytest.raises(RuntimeError, match="submitter unavailable"):
        await scheduler.tick(now=fire_time)
    retried = await scheduler.tick(now=fire_time)

    assert len(retried) == 1
    assert len(submitter.calls) == 1


async def test_scheduler_submits_through_ordinary_task_submitter_without_executor_bypass() -> None:
    submitter = RecordingSubmitter()
    scheduler = SchedulerService(submitter.submit)
    fire_time = datetime(2026, 8, 7, 10, tzinfo=ZoneInfo("UTC"))
    await scheduler.add_schedule(
        ScheduleDefinition(
            tenant_id=TENANT_A,
            owner_id=OWNER_A,
            name="auto-routing",
            mode=TaskMode.AUTO,
            workflow="default_workflow",
            message="route this through normal capacity and policy",
            timezone="UTC",
            kind=ScheduleKind.ONE_TIME,
            fire_at=fire_time,
        ),
        now=datetime(2026, 8, 7, 9, tzinfo=ZoneInfo("UTC")),
    )

    await scheduler.tick(now=fire_time)

    assert len(submitter.calls) == 1
    assert submitter.calls[0].tenant_id == TENANT_A
    assert submitter.calls[0].actor_id == OWNER_A
    assert submitter.calls[0].message == "route this through normal capacity and policy"
    assert submitter.calls[0].mode is TaskMode.AUTO
    assert submitter.calls[0].workflow == "default_workflow"
    assert submitter.calls[0].budget == 16_384
    assert submitter.calls[0].metadata["schedule_id"]


async def test_cron_schedule_uses_iana_timezone_and_advances_deterministically() -> None:
    submitter = RecordingSubmitter()
    scheduler = SchedulerService(submitter.submit)
    now = datetime(2026, 8, 7, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    schedule = await scheduler.add_schedule(
        ScheduleDefinition(
            tenant_id=TENANT_A,
            owner_id=OWNER_A,
            name="morning-report",
            mode=TaskMode.DIRECT,
            workflow="morning_report",
            message="summarize yesterday",
            timezone="Asia/Shanghai",
            kind=ScheduleKind.CRON,
            cron=CronSchedule(hour=9, minute=0),
        ),
        now=now,
    )

    assert await scheduler.next_fire(schedule.id, tenant_id=TENANT_A) == datetime(
        2026, 8, 7, 9, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    await scheduler.tick(now=datetime(2026, 8, 7, 9, tzinfo=ZoneInfo("Asia/Shanghai")))

    assert await scheduler.next_fire(schedule.id, tenant_id=TENANT_A) == datetime(
        2026, 8, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai")
    )


async def test_one_time_skip_misfire_does_not_submit_late_work() -> None:
    submitter = RecordingSubmitter()
    scheduler = SchedulerService(submitter.submit)
    fire_time = datetime(2026, 8, 7, 9, tzinfo=ZoneInfo("UTC"))
    schedule = await scheduler.add_schedule(
        ScheduleDefinition(
            tenant_id=TENANT_A,
            owner_id=OWNER_A,
            name="skip-late-report",
            mode=TaskMode.DIRECT,
            workflow="late_report",
            message="late report",
            spec=OneTimeScheduleSpec(
                run_at=fire_time,
                timezone="UTC",
                misfire_policy=ScheduleMisfirePolicy.SKIP,
            ),
            idempotency_key="skip-late-report",
        ),
        now=datetime(2026, 8, 7, 8, tzinfo=ZoneInfo("UTC")),
    )

    fired = await scheduler.tick(now=datetime(2026, 8, 7, 10, tzinfo=ZoneInfo("UTC")))
    stored = await scheduler.get_schedule(TENANT_A, schedule.id)

    assert fired == ()
    assert submitter.calls == []
    assert stored.status is ScheduleStatus.COMPLETED


async def test_cron_fire_once_misfire_coalesces_missed_fires() -> None:
    submitter = RecordingSubmitter()
    scheduler = SchedulerService(submitter.submit)
    schedule = await scheduler.add_schedule(
        ScheduleDefinition(
            tenant_id=TENANT_A,
            owner_id=OWNER_A,
            name="coalesce-report",
            mode=TaskMode.DIRECT,
            workflow="coalesce_report",
            message="coalesce report",
            spec=CronScheduleSpec(
                expression="0 9 * * *",
                timezone="UTC",
                misfire_policy=ScheduleMisfirePolicy.FIRE_ONCE,
            ),
            idempotency_key="coalesce-report",
        ),
        now=datetime(2026, 8, 7, 8, tzinfo=ZoneInfo("UTC")),
    )

    fired = await scheduler.tick(now=datetime(2026, 8, 10, 12, tzinfo=ZoneInfo("UTC")))

    assert fired == (schedule.id,)
    assert len(submitter.calls) == 1
    assert await scheduler.next_fire(schedule.id, tenant_id=TENANT_A) == datetime(
        2026, 8, 11, 9, tzinfo=ZoneInfo("UTC")
    )


async def test_scheduler_lists_are_tenant_isolated() -> None:
    scheduler = SchedulerService(RecordingSubmitter().submit)
    fire_time = datetime(2026, 8, 7, 9, tzinfo=ZoneInfo("UTC"))
    await scheduler.add_schedule(
        ScheduleDefinition(
            tenant_id=TENANT_A,
            owner_id=OWNER_A,
            name="tenant-a",
            mode=TaskMode.DIRECT,
            workflow="tenant_a",
            message="tenant a work",
            timezone="UTC",
            kind=ScheduleKind.ONE_TIME,
            fire_at=fire_time,
        ),
        now=fire_time,
    )
    await scheduler.add_schedule(
        ScheduleDefinition(
            tenant_id=TENANT_B,
            owner_id=OWNER_A,
            name="tenant-b",
            mode=TaskMode.DIRECT,
            workflow="tenant_b",
            message="tenant b work",
            timezone="UTC",
            kind=ScheduleKind.ONE_TIME,
            fire_at=fire_time,
        ),
        now=fire_time,
    )

    assert [schedule.name for schedule in await scheduler.list_schedules(tenant_id=TENANT_A)] == [
        "tenant-a"
    ]


async def test_scheduler_diagnostic_helpers_are_tenant_scoped() -> None:
    submitter = RecordingSubmitter()
    scheduler = SchedulerService(submitter.submit)
    schedule_id = uuid4()
    fire_time = datetime(2026, 8, 7, 9, tzinfo=ZoneInfo("UTC"))
    for tenant_id, name in ((TENANT_A, "tenant-a-same-id"), (TENANT_B, "tenant-b-same-id")):
        await scheduler.add_schedule(
            ScheduleDefinition(
                id=schedule_id,
                tenant_id=tenant_id,
                owner_id=OWNER_A,
                name=name,
                mode=TaskMode.DIRECT,
                workflow=name.replace("-", "_"),
                message=f"{name} work",
                timezone="UTC",
                kind=ScheduleKind.ONE_TIME,
                fire_at=fire_time,
            ),
            now=fire_time,
        )

    await scheduler.tick(tenant_id=TENANT_A, now=fire_time)

    assert await scheduler.submission_count(schedule_id, fire_time, tenant_id=TENANT_A) == 1
    assert await scheduler.submission_count(schedule_id, fire_time, tenant_id=TENANT_B) == 0
    assert await scheduler.next_fire(schedule_id, tenant_id=TENANT_B) == fire_time


async def test_long_base_idempotency_key_is_hashed_into_safe_task_request_key() -> None:
    submitter = RecordingSubmitter()
    scheduler = SchedulerService(submitter.submit)
    fire_time = datetime(2026, 8, 7, 9, tzinfo=ZoneInfo("UTC"))
    await scheduler.add_schedule(
        ScheduleDefinition(
            tenant_id=TENANT_A,
            owner_id=OWNER_A,
            name="long-key-report",
            mode=TaskMode.DIRECT,
            workflow="long_key_report",
            message="long key report",
            timezone="UTC",
            kind=ScheduleKind.ONE_TIME,
            fire_at=fire_time,
            idempotency_key="x" * 256,
        ),
        now=fire_time,
    )

    await scheduler.tick(now=fire_time)

    assert submitter.calls[0].idempotency_key.startswith("schedule:")
    assert len(submitter.calls[0].idempotency_key) <= 64
    assert len(f"{TENANT_A}:{submitter.calls[0].idempotency_key}") <= 128


async def test_unfinished_task_can_schedule_user_visible_follow_up() -> None:
    submitter = RecordingSubmitter()
    scheduler = SchedulerService(submitter.submit)
    fire_time = datetime(2026, 8, 7, 11, tzinfo=ZoneInfo("UTC"))
    schedule = await scheduler.schedule_follow_up(
        tenant_id=TENANT_A,
        owner_id=OWNER_A,
        run_id=uuid4(),
        message="please review the unfinished task",
        at=fire_time,
    )

    submissions = await scheduler.tick(now=fire_time)

    assert schedule.user_visible is True
    assert schedule.status is ScheduleStatus.ACTIVE
    assert len(submissions) == 1
    assert submitter.calls[0].message == "please review the unfinished task"


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="IANA"):
        ScheduleDefinition(
            tenant_id=TENANT_A,
            owner_id=OWNER_A,
            name="bad-timezone",
            mode=TaskMode.DIRECT,
            workflow="bad_timezone",
            message="bad timezone",
            timezone="Mars/Olympus",
            kind=ScheduleKind.CRON,
            cron=CronSchedule(hour=9, minute=0),
        )
