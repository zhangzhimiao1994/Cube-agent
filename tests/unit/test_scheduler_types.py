from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from agent_hub.scheduler.types import CronScheduleSpec, deterministic_idempotency_key


def test_schedule_idempotency_key_fits_run_outbox_prefix_limit() -> None:
    tenant_id = UUID("00000000-0000-4000-8000-000000000001")
    schedule_id = UUID("11111111-1111-4111-8111-111111111111")
    key = deterministic_idempotency_key(
        tenant_id=tenant_id,
        schedule_id=schedule_id,
        fire_time=datetime(2026, 8, 13, 9, tzinfo=UTC),
        base_key="server-schedule-probe:20260813T090000Z",
    )

    assert key.startswith("schedule:")
    assert len(key) <= 64
    assert len(f"{tenant_id}:{key}") <= 128


def test_weekly_cron_schedule_advances_to_selected_weekday() -> None:
    schedule = CronScheduleSpec(expression="0 9 * * 4", timezone="Asia/Shanghai")
    first = schedule.first_fire_after(datetime(2026, 8, 12, 10, tzinfo=ZoneInfo("Asia/Shanghai")))

    assert first == datetime(2026, 8, 13, 1, tzinfo=UTC)
    assert schedule.next_after(first) == datetime(2026, 8, 20, 1, tzinfo=UTC)
