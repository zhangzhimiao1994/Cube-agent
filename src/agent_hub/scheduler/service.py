from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from agent_hub.domain.runs import TaskMode
from agent_hub.scheduler.types import (
    CronScheduleSpec,
    OneTimeScheduleSpec,
    ScheduleDefinition,
    ScheduleMisfirePolicy,
    ScheduleStatus,
    TaskRequest,
    deterministic_idempotency_key,
)

_DEFAULT_CREATION_TIME = datetime(2026, 8, 6, tzinfo=UTC)


class SubmittedRunLike:
    id: UUID


type TaskSubmissionCallable = Callable[[TaskRequest], Awaitable[object]]


class SchedulerService:
    """Turns schedules into ordinary task submissions.

    The scheduler does not execute runtimes or call model/capability layers directly. Its only side
    effect is the injected task submission boundary, so routing, capacity, capability policy, and
    approval checks remain owned by the normal run submission/execution path.
    """

    def __init__(
        self,
        submit_task: TaskSubmissionCallable,
    ) -> None:
        self._submit_task = submit_task
        self._schedules: dict[tuple[UUID, UUID], ScheduleDefinition] = {}
        self._submitted_fires: set[tuple[UUID, UUID, datetime]] = set()
        self._lock = asyncio.Lock()

    async def create_schedule(
        self,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        name: str,
        message: str,
        mode: TaskMode,
        workflow: str,
        budget: int,
        spec: OneTimeScheduleSpec | CronScheduleSpec,
        idempotency_key: str,
        now: datetime = _DEFAULT_CREATION_TIME,
        user_visible: bool = False,
        metadata: Mapping[str, object] | None = None,
    ) -> ScheduleDefinition:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        next_fire_at = spec.first_fire_after(now).astimezone(UTC)
        schedule = ScheduleDefinition(
            tenant_id=tenant_id,
            owner_id=owner_id,
            name=name,
            message=message,
            mode=mode,
            workflow=workflow,
            budget=budget,
            spec=spec,
            idempotency_key=idempotency_key,
            next_fire_at=next_fire_at,
            user_visible=user_visible,
            metadata={} if metadata is None else metadata,
        )
        async with self._lock:
            key = (tenant_id, schedule.id)
            if key in self._schedules:
                raise ValueError("schedule already exists")
            self._schedules[key] = schedule
        return schedule

    async def add_schedule(
        self,
        schedule: ScheduleDefinition,
        *,
        now: datetime = _DEFAULT_CREATION_TIME,
    ) -> ScheduleDefinition:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        assert schedule.spec is not None
        next_fire_at = schedule.next_fire_at
        if next_fire_at is None and schedule.status is ScheduleStatus.ACTIVE:
            next_fire_at = schedule.spec.first_fire_after(now)
        if next_fire_at is None:
            stored = replace(schedule, next_fire_at=None)
        else:
            stored = replace(schedule, next_fire_at=next_fire_at.astimezone(UTC))
        async with self._lock:
            key = (stored.tenant_id, stored.id)
            if key in self._schedules:
                raise ValueError("schedule already exists")
            self._schedules[key] = stored
        return stored

    async def get_schedule(self, tenant_id: UUID, schedule_id: UUID) -> ScheduleDefinition:
        async with self._lock:
            schedule = self._schedules.get((tenant_id, schedule_id))
            if schedule is None:
                raise KeyError("schedule not found")
            return schedule

    async def list_schedules(self, *, tenant_id: UUID) -> tuple[ScheduleDefinition, ...]:
        async with self._lock:
            return tuple(
                schedule
                for key, schedule in self._schedules.items()
                if key[0] == tenant_id
            )

    async def delete_schedule(self, *, tenant_id: UUID, schedule_id: UUID) -> None:
        async with self._lock:
            try:
                del self._schedules[(tenant_id, schedule_id)]
            except KeyError:
                raise KeyError("schedule not found") from None
            self._submitted_fires = {
                key
                for key in self._submitted_fires
                if key[0] != tenant_id or key[1] != schedule_id
            }

    async def next_fire(self, schedule_id: UUID, *, tenant_id: UUID) -> datetime | None:
        async with self._lock:
            for (stored_tenant_id, _), schedule in self._schedules.items():
                if schedule.id == schedule_id and stored_tenant_id == tenant_id:
                    if schedule.next_fire_at is None:
                        return None
                    return schedule.next_fire_at.astimezone(ZoneInfo(schedule.timezone_name))
            return None

    async def submission_count(
        self,
        schedule_id: UUID,
        fire_time: datetime,
        *,
        tenant_id: UUID,
    ) -> int:
        if fire_time.tzinfo is None:
            raise ValueError("fire_time must be timezone-aware")
        normalized = fire_time.astimezone(UTC)
        async with self._lock:
            return sum(
                1
                for submitted_tenant_id, submitted_schedule_id, submitted_fire_time in self._submitted_fires
                if submitted_schedule_id == schedule_id and submitted_fire_time == normalized
                and submitted_tenant_id == tenant_id
            )

    async def schedule_follow_up(
        self,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        run_id: UUID,
        message: str,
        due_at: datetime | None = None,
        at: datetime | None = None,
        mode: TaskMode = TaskMode.AUTO,
        workflow: str = "follow_up",
        budget: int = 512,
        idempotency_key: str = "",
    ) -> ScheduleDefinition:
        fire_time = due_at if due_at is not None else at
        if fire_time is None:
            raise ValueError("follow-up due time is required")
        if fire_time.tzinfo is None:
            raise ValueError("follow-up due time must be timezone-aware")
        timezone = getattr(fire_time.tzinfo, "key", "UTC")
        follow_up_key = idempotency_key or f"follow-up:{run_id}"
        return await self.create_schedule(
            tenant_id=tenant_id,
            owner_id=owner_id,
            name=f"follow-up-{run_id}",
            message=message,
            mode=mode,
            workflow=workflow,
            budget=budget,
            spec=OneTimeScheduleSpec(run_at=fire_time, timezone=timezone),
            idempotency_key=follow_up_key,
            now=fire_time,
            user_visible=True,
            metadata={"follow_up_for_run_id": str(run_id)},
        )

    async def tick(
        self,
        tenant_id: UUID | None = None,
        *,
        now: datetime,
    ) -> tuple[UUID, ...]:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        fired: list[UUID] = []
        while True:
            request = await self._next_due_request(tenant_id=tenant_id, now=now.astimezone(UTC))
            if request is None:
                return tuple(fired)
            try:
                run_id = await self._submit(request)
                del run_id
            except Exception:
                await self._release_fire(request)
                raise
            await self._complete_fire(request)
            schedule_id = UUID(str(request.metadata["schedule_id"]))
            fired.append(schedule_id)

    async def _next_due_request(self, *, tenant_id: UUID | None, now: datetime) -> TaskRequest | None:
        async with self._lock:
            for (stored_tenant_id, schedule_id), schedule in tuple(self._schedules.items()):
                if tenant_id is not None and stored_tenant_id != tenant_id:
                    continue
                if schedule.status is not ScheduleStatus.ACTIVE or schedule.next_fire_at is None:
                    continue
                fire_time = schedule.next_fire_at.astimezone(UTC)
                if fire_time > now:
                    continue
                if (
                    isinstance(schedule.spec, CronScheduleSpec)
                    and schedule.misfire_policy is ScheduleMisfirePolicy.SKIP
                    and now > fire_time
                ):
                    self._advance_schedule(stored_tenant_id, schedule, now=now, skipped=True)
                    continue
                if (
                    isinstance(schedule.spec, OneTimeScheduleSpec)
                    and schedule.misfire_policy is ScheduleMisfirePolicy.SKIP
                    and now > fire_time
                ):
                    self._advance_schedule(stored_tenant_id, schedule, now=now, skipped=True)
                    continue
                metadata = {
                    **schedule.metadata,
                    "schedule_id": str(schedule.id),
                    "fire_time": fire_time.isoformat(),
                    "timezone": schedule.timezone_name,
                    "user_visible": schedule.user_visible,
                    "advance_after": (
                        now.isoformat()
                        if isinstance(schedule.spec, CronScheduleSpec) and now > fire_time
                        else fire_time.isoformat()
                    ),
                }
                idempotency_key = deterministic_idempotency_key(
                    tenant_id=stored_tenant_id,
                    schedule_id=schedule_id,
                    fire_time=fire_time,
                    base_key=schedule.idempotency_key,
                )
                request = TaskRequest(
                    tenant_id=stored_tenant_id,
                    actor_id=schedule.owner_id,
                    message=schedule.message,
                    mode=schedule.mode,
                    workflow=schedule.workflow,
                    budget=schedule.budget,
                    idempotency_key=idempotency_key,
                    metadata=metadata,
                )
                key = (stored_tenant_id, schedule_id, fire_time)
                if key in self._submitted_fires:
                    continue
                self._submitted_fires.add(key)
                return request
            return None

    async def _submit(self, request: TaskRequest) -> UUID:
        submitted = await self._submit_task(request)
        if isinstance(submitted, UUID):
            return submitted
        submitted_id = getattr(submitted, "id", None)
        if isinstance(submitted_id, UUID):
            return submitted_id
        raise TypeError("task submission must return a UUID or object with UUID id")

    async def _release_fire(self, request: TaskRequest) -> None:
        async with self._lock:
            self._submitted_fires.discard(_fire_identity(request))

    async def _complete_fire(self, request: TaskRequest) -> None:
        async with self._lock:
            tenant_id, schedule_id, _fire_time = _fire_identity(request)
            schedule = self._schedules.get((tenant_id, schedule_id))
            if schedule is not None:
                advance_after = datetime.fromisoformat(
                    str(request.metadata.get("advance_after", request.metadata["fire_time"]))
                ).astimezone(UTC)
                self._advance_schedule(tenant_id, schedule, now=advance_after, skipped=False)

    def _advance_schedule(
        self,
        tenant_id: UUID,
        schedule: ScheduleDefinition,
        *,
        now: datetime,
        skipped: bool,
    ) -> None:
        assert schedule.spec is not None
        next_fire_at = schedule.spec.next_after(now)
        if skipped:
            while next_fire_at is not None and next_fire_at <= now:
                next_fire_at = schedule.spec.next_after(next_fire_at)
        key = (tenant_id, schedule.id)
        if next_fire_at is None:
            self._schedules[key] = replace(
                schedule,
                status=ScheduleStatus.COMPLETED,
                next_fire_at=None,
            )
            return
        self._schedules[key] = replace(schedule, next_fire_at=next_fire_at.astimezone(UTC))


__all__ = ["SchedulerService", "TaskSubmissionCallable"]


def _fire_identity(request: TaskRequest) -> tuple[UUID, UUID, datetime]:
    return (
        request.tenant_id,
        UUID(str(request.metadata["schedule_id"])),
        datetime.fromisoformat(str(request.metadata["fire_time"])).astimezone(UTC),
    )
