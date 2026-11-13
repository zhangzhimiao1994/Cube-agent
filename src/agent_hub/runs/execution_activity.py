"""Read-only occupancy projection. It grants no execution or recovery authority."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.auth.models import Role
from agent_hub.db.models import RunAdmissionPoolRow
from agent_hub.domain.runs import RunStatus

_TERMINAL = frozenset({'completed', 'failed', 'cancelled'})


class ExecutionActivity(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    is_active: bool
    deadline_at: datetime | None
    evaluated_at: datetime
    reason: Literal['active', 'terminal', 'deadline_elapsed', 'worker_not_released']


class ActivitySubject(Protocol):
    id: UUID
    tenant_id: UUID
    actor_id: UUID | None
    actor_role: Role | str | None
    status: RunStatus | str


def execution_is_active(record: ActivitySubject) -> bool:
    if record.status in _TERMINAL:
        return False
    activity = getattr(record, 'execution_activity', None)
    return activity is None or activity.is_active


async def read_execution_activities(
    session: AsyncSession, records: Sequence[ActivitySubject],
) -> tuple[ExecutionActivity, ...]:
    """One bounded batch, exact durable pool ownership, no route/clock guesses."""
    if not records:
        return ()
    pools = (await session.scalars(select(RunAdmissionPoolRow).where(
        RunAdmissionPoolRow.tenant_id.in_({r.tenant_id for r in records}),
        RunAdmissionPoolRow.run_id.in_({r.id for r in records}),
    ))).all()
    by_id = {(p.tenant_id, p.run_id): p for p in pools}
    now = datetime.now(UTC)
    result = []
    for record in records:
        pool = by_id.get((record.tenant_id, record.id))
        deadline = None
        role = record.actor_role.value if isinstance(record.actor_role, Role) else record.actor_role
        if pool is not None and pool.actor_id == record.actor_id and pool.actor_role == role:
            candidate = pool.original_deadline
            if isinstance(candidate, datetime) and candidate.utcoffset() is not None:
                deadline = candidate
        # Preserve the original worker-release fence, including uncertain/stale ownership.
        held = any(getattr(record, key, None) is not None for key in (
            'worker_id', 'worker_lease_token', 'worker_lease_expires_at', 'worker_heartbeat_at',
        ))
        if record.status in _TERMINAL:
            active, reason = False, 'terminal'
        elif held:
            active, reason = True, 'worker_not_released'
        elif deadline is not None and deadline <= now:
            active, reason = False, 'deadline_elapsed'
        else:
            active, reason = True, 'active'
        result.append(ExecutionActivity(is_active=active, deadline_at=deadline,
            evaluated_at=now, reason=reason))
    return tuple(result)
