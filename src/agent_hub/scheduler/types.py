from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_hub.domain.runs import TaskMode

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


class ScheduleMisfirePolicy(StrEnum):
    FIRE_ONCE = "fire_once"
    SKIP = "skip"


MisfirePolicy = ScheduleMisfirePolicy


class ScheduleKind(StrEnum):
    ONE_TIME = "one_time"
    CRON = "cron"


class ScheduleStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class OneTimeScheduleSpec:
    run_at: datetime
    timezone: str
    misfire_policy: ScheduleMisfirePolicy = ScheduleMisfirePolicy.FIRE_ONCE

    def __post_init__(self) -> None:
        _timezone(self.timezone)
        if self.run_at.tzinfo is None:
            raise ValueError("run_at must be timezone-aware")

    def first_fire_after(self, now: datetime) -> datetime:
        del now
        return self.run_at.astimezone(UTC)

    def next_after(self, fire_time: datetime) -> datetime | None:
        del fire_time
        return None


@dataclass(frozen=True, slots=True)
class CronScheduleSpec:
    expression: str
    timezone: str
    misfire_policy: ScheduleMisfirePolicy = ScheduleMisfirePolicy.FIRE_ONCE

    def __post_init__(self) -> None:
        _timezone(self.timezone)
        _parse_cron(self.expression)

    def first_fire_after(self, now: datetime) -> datetime:
        return _next_cron_fire(self.expression, self.timezone, after=now)

    def next_after(self, fire_time: datetime) -> datetime | None:
        return _next_cron_fire(self.expression, self.timezone, after=fire_time)


@dataclass(frozen=True, slots=True)
class CronSchedule:
    hour: int
    minute: int

    def __post_init__(self) -> None:
        if type(self.hour) is not int or not 0 <= self.hour <= 23:
            raise ValueError("cron hour is invalid")
        if type(self.minute) is not int or not 0 <= self.minute <= 59:
            raise ValueError("cron minute is invalid")

    def as_spec(
        self,
        *,
        timezone: str,
        misfire_policy: ScheduleMisfirePolicy = ScheduleMisfirePolicy.FIRE_ONCE,
    ) -> CronScheduleSpec:
        return CronScheduleSpec(
            expression=f"{self.minute} {self.hour} * * *",
            timezone=timezone,
            misfire_policy=misfire_policy,
        )


@dataclass(frozen=True, slots=True)
class ScheduleDefinition:
    tenant_id: UUID
    owner_id: UUID
    name: str
    mode: TaskMode
    workflow: str
    message: str
    budget: int = 16_384
    spec: OneTimeScheduleSpec | CronScheduleSpec | None = None
    idempotency_key: str = ""
    timezone: str | None = None
    kind: ScheduleKind | None = None
    fire_at: datetime | None = None
    cron: CronSchedule | None = None
    id: UUID = field(default_factory=uuid4)
    next_fire_at: datetime | None = None
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    user_visible: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        spec = self.spec
        if spec is None:
            if self.kind is ScheduleKind.ONE_TIME:
                if self.fire_at is None or self.timezone is None:
                    raise ValueError("one-time schedules require fire_at and timezone")
                spec = OneTimeScheduleSpec(run_at=self.fire_at, timezone=self.timezone)
            elif self.kind is ScheduleKind.CRON:
                if self.cron is None or self.timezone is None:
                    raise ValueError("cron schedules require cron and timezone")
                spec = self.cron.as_spec(timezone=self.timezone)
            else:
                raise ValueError("schedule kind is invalid")
            object.__setattr__(self, "spec", spec)
        if self.timezone is None:
            object.__setattr__(self, "timezone", spec.timezone)
        if self.kind is None:
            object.__setattr__(
                self,
                "kind",
                ScheduleKind.ONE_TIME if isinstance(spec, OneTimeScheduleSpec) else ScheduleKind.CRON,
            )
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", self.name)
        _safe_text(self.name, name="schedule name", max_bytes=256)
        _safe_text(self.message, name="schedule message", max_bytes=65_536)
        _safe_identifier(self.workflow, name="workflow")
        _safe_idempotency_key(self.idempotency_key)
        if type(self.budget) is not int or not 1 <= self.budget <= 10_000_000:
            raise ValueError("budget must be a positive bounded integer")
        if self.next_fire_at is not None and self.next_fire_at.tzinfo is None:
            raise ValueError("next_fire_at must be timezone-aware")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def timezone_name(self) -> str:
        assert self.spec is not None
        return self.spec.timezone

    @property
    def misfire_policy(self) -> ScheduleMisfirePolicy:
        assert self.spec is not None
        return self.spec.misfire_policy

    @property
    def schedule_kind(self) -> ScheduleKind:
        assert self.spec is not None
        if isinstance(self.spec, OneTimeScheduleSpec):
            return ScheduleKind.ONE_TIME
        return ScheduleKind.CRON


@dataclass(frozen=True, slots=True)
class TaskRequest:
    tenant_id: UUID
    actor_id: UUID
    message: str
    mode: TaskMode
    workflow: str
    budget: int
    idempotency_key: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _safe_text(self.message, name="task message", max_bytes=65_536)
        _safe_identifier(self.workflow, name="workflow")
        _safe_idempotency_key(self.idempotency_key)
        if type(self.budget) is not int or not 1 <= self.budget <= 10_000_000:
            raise ValueError("budget must be a positive bounded integer")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ScheduledTaskRequest(TaskRequest):
    pass


@dataclass(frozen=True, slots=True)
class ScheduleBudget:
    token_budget: int = 16_384

    def __post_init__(self) -> None:
        if type(self.token_budget) is not int or not 1 <= self.token_budget <= 10_000_000:
            raise ValueError("token budget is invalid")


@dataclass(frozen=True, slots=True)
class ScheduleSubmission:
    schedule_id: UUID
    fire_time: datetime
    run_id: UUID
    idempotency_key: str
    duplicate: bool = False


def deterministic_idempotency_key(
    *,
    tenant_id: UUID,
    schedule_id: UUID,
    fire_time: datetime,
    base_key: str,
) -> str:
    _safe_idempotency_key(base_key)
    if fire_time.tzinfo is None:
        raise ValueError("fire_time must be timezone-aware")
    normalized_fire_time = fire_time.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(
        f"{tenant_id}:{schedule_id}:{normalized_fire_time}:{base_key}".encode()
    ).hexdigest()[:32]
    return f"schedule:{digest}"


def _safe_idempotency_key(value: str) -> str:
    if _SAFE_KEY.fullmatch(value) is None:
        raise ValueError("idempotency key must be safe")
    return value


def _safe_identifier(value: str, *, name: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe identifier")
    return value


def _safe_text(value: str, *, name: str, max_bytes: int) -> str:
    if not value.strip() or len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} must be nonblank and bounded")
    return value


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ValueError("timezone must be a valid IANA timezone") from None


def _parse_cron(expression: str) -> tuple[frozenset[int], frozenset[int]]:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron expression must have five fields")
    minutes = _parse_cron_field(fields[0], minimum=0, maximum=59, name="cron minute")
    hours = _parse_cron_field(fields[1], minimum=0, maximum=23, name="cron hour")
    for index, field_text in enumerate(fields[2:], start=3):
        if field_text != "*":
            raise ValueError(f"cron field {index} currently supports only '*'")
    return minutes, hours


def _parse_cron_field(field_text: str, *, minimum: int, maximum: int, name: str) -> frozenset[int]:
    if field_text == "*":
        return frozenset(range(minimum, maximum + 1))
    if field_text.startswith("*/"):
        step = _parse_int(field_text[2:], name=name)
        if step <= 0:
            raise ValueError(f"{name} step is invalid")
        return frozenset(range(minimum, maximum + 1, step))
    value = _parse_int(field_text, name=name)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} is invalid")
    return frozenset({value})


def _parse_int(text: str, *, name: str) -> int:
    if not text.isdecimal():
        raise ValueError(f"{name} is invalid")
    return int(text)


def _next_cron_fire(expression: str, timezone: str, *, after: datetime) -> datetime:
    if after.tzinfo is None:
        raise ValueError("cron reference time must be timezone-aware")
    minutes, hours = _parse_cron(expression)
    zone = _timezone(timezone)
    local_after = after.astimezone(zone).replace(second=0, microsecond=0)
    candidate = local_after + timedelta(minutes=1)
    max_minutes = 366 * 24 * 60
    for _ in range(max_minutes):
        if candidate.minute in minutes and candidate.hour in hours:
            return candidate.astimezone(UTC)
        candidate += timedelta(minutes=1)
    raise ValueError("cron expression did not produce a fire time")
