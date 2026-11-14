"""RunRow -> clock row locking; supplied timestamps never establish authority."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import DateTime, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunExecutionClockRow,
    RunInputAuthorizationRow,
    RunRow,
)
from agent_hub.runs.execution_clock import (
    ClockPolicy,
    ClockSnapshot,
    digest,
    positive,
    submission_binding,
)
from agent_hub.runtime.contracts import JsonValue, RuntimeCheckpoint


class ExecutionClockExpired(RuntimeError):
    pass


def tagged(row: RunRow) -> bool:
    value = (row.routing_decision or {}).get("execution_clock_version")
    requested = type(value) is int and value == 2
    if getattr(row, "execution_clock_sha256", None) is not None and not requested:
        raise ValueError("execution clock cannot become a legacy run")
    return requested


def utc(value: datetime) -> datetime:
    # SQLite drops tzinfo in the local SQL regression; PostgreSQL retains it.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def database_now(session: AsyncSession) -> datetime:
    value = (await session.execute(select(func.clock_timestamp(type_=DateTime(timezone=True))))).scalar_one()
    if not isinstance(value, datetime):
        raise TypeError("execution clock database time is invalid")
    return utc(value)


def snapshot(clock: RunExecutionClockRow) -> ClockSnapshot:
    return ClockSnapshot(ClockPolicy.from_payload(clock.policy), utc(clock.hard_deadline),
        clock.spent_seconds, None if clock.active_since is None else utc(clock.active_since),
        clock.worker_id, clock.worker_token, clock.progress_units, clock.version)


def binding_seal(row: RunRow, policy: ClockPolicy, turn: UUID, hard_deadline: datetime) -> str:
    """One immutable original seal, reused by witness and every clock consumer."""
    return digest({"version": 2, "run_id": str(row.id), "tenant_id": str(row.tenant_id),
        "actor_id": str(row.actor_id), "actor_role": row.actor_role,
        "request_sha256": hashlib.sha256(row.request.encode()).hexdigest(),
        "authorization_turn_id": str(turn),
        "created_at": utc(row.created_at).astimezone(UTC).isoformat(timespec="microseconds"),
        "policy": policy.payload(),
        "hard_deadline": utc(hard_deadline).astimezone(UTC).isoformat(timespec="microseconds")})


async def create(session: AsyncSession, row: RunRow, *, authorization_turn_id: UUID) -> None:
    if not tagged(row):
        return
    assert row.actor_id is not None
    binding = submission_binding(row.tenant_id, row.actor_id, row.request, row.routing_decision or {})
    if await binding.recheck() is not True:
        raise ValueError("execution clock submission policy changed")
    now = await database_now(session)
    if binding.activation_valid_until is not None and now >= binding.activation_valid_until:
        raise ValueError("execution clock submission profile expired")
    row.created_at = now
    hard_deadline = now+timedelta(seconds=binding.policy.authorization_seconds)
    row.execution_clock_sha256 = binding_seal(row, binding.policy, authorization_turn_id, hard_deadline)
    # Parent witness must exist before its non-null composite-FK child INSERT.
    await session.flush()
    session.add(RunExecutionClockRow(run_id=row.id, tenant_id=row.tenant_id,
        authorization_turn_id=authorization_turn_id, actor_id=row.actor_id,
        request_sha256=binding.request_sha256, binding_sha256=row.execution_clock_sha256,
        policy=binding.policy.payload(),
        hard_deadline=hard_deadline,
        spent_seconds=0., active_since=None, worker_id=None, worker_token=None,
        progress_units=0, checkpoint_id=None, checkpoint_sha256=None, version=1))
    await session.flush()


async def locked(session: AsyncSession, row: RunRow) -> RunExecutionClockRow | None:
    if not tagged(row):
        return None
    clock = await session.scalar(select(RunExecutionClockRow).where(
        RunExecutionClockRow.run_id == row.id, RunExecutionClockRow.tenant_id == row.tenant_id
    ).with_for_update())
    link = await session.scalar(select(RunInputAuthorizationRow).where(
        RunInputAuthorizationRow.run_id == row.id, RunInputAuthorizationRow.tenant_id == row.tenant_id))
    turn = None if link is None else link.turn_id
    route = row.routing_decision or {}
    if clock is None:
        raise ValueError("execution clock original binding changed")
    policy = ClockPolicy.from_payload(clock.policy)
    expected_hard = utc(row.created_at)+timedelta(seconds=policy.authorization_seconds)
    expected_seal = binding_seal(row, policy, clock.authorization_turn_id, utc(clock.hard_deadline))
    current_active = positive(route.get("runtime_timeout_absolute_seconds"))
    if (row.execution_clock_sha256 is None or utc(clock.hard_deadline) != expected_hard
            or clock.binding_sha256 != row.execution_clock_sha256 or clock.binding_sha256 != expected_seal
            or clock.actor_id != row.actor_id or clock.authorization_turn_id != turn
            or clock.request_sha256 != hashlib.sha256(row.request.encode()).hexdigest()
            or clock.policy["policy_sha256"] != digest(route.get("approval_review_profile"))
            or not current_active <= policy.active_seconds):
        raise ValueError("execution clock original binding changed")
    return clock


def debit(clock: RunExecutionClockRow, now: datetime) -> None:
    if clock.active_since is not None:
        elapsed = (now-utc(clock.active_since)).total_seconds()
        if elapsed < 0:
            raise ValueError("execution clock moved backwards")
        clock.spent_seconds += elapsed
        clock.active_since = now
    clock.version += 1


async def start(session: AsyncSession, row: RunRow, checkpoint: RuntimeCheckpoint | None) -> None:
    clock = await locked(session, row)
    if clock is None:
        return
    now = await database_now(session)
    # An unclosed crashed segment is charged through now, never refunded as human wait.
    debit(clock, now)
    expected_checkpoint = (None, None) if checkpoint is None else (checkpoint.id, checkpoint.state_sha256)
    if (clock.checkpoint_id, clock.checkpoint_sha256) != expected_checkpoint:
        raise ValueError("execution clock checkpoint binding changed")
    if snapshot(clock).remaining(now).effective_seconds <= 0:
        raise ExecutionClockExpired("execution clock or authorization exhausted")
    if row.worker_id is None or row.worker_lease_token is None:
        raise ValueError("execution clock requires a claimed worker")
    clock.active_since, clock.worker_id, clock.worker_token = now, row.worker_id, row.worker_lease_token
    await session.flush()


async def checkpoint(session: AsyncSession, row: RunRow, value: RuntimeCheckpoint) -> None:
    clock = await locked(session, row)
    if clock is None:
        return
    owned = (clock.worker_id, clock.worker_token) == (row.worker_id, row.worker_lease_token)
    route = row.routing_decision or {}
    if not owned and row.status == "waiting_approval":
        # Accounting may preserve an UNKNOWN audit checkpoint from the exact
        # draining worker. Only park/approval recovery checks establish quiet.
        owned = (route.get("approval_checkpoint_required") is True
            and route.get("approval_checkpoint_worker_token") == str(clock.worker_token)
            and (value.run_id, value.tenant_id, value.mode.value) == (row.id, row.tenant_id, row.mode))
    if not owned or clock.active_since is None:
        raise ValueError("execution clock checkpoint ownership changed")
    debit(clock, await database_now(session))
    clock.checkpoint_id, clock.checkpoint_sha256 = value.id, value.state_sha256


async def has_unsettled_attempts(session: AsyncSession, row: RunRow) -> bool:
    value = await session.scalar(select(RunAdmissionAttemptRow.attempt_id).where(
        RunAdmissionAttemptRow.run_id == row.id, RunAdmissionAttemptRow.tenant_id == row.tenant_id,
        RunAdmissionAttemptRow.state.in_(("prepared", "issued", "unknown"))).limit(1))
    return value is not None


async def park(session: AsyncSession, row: RunRow, *, worker_id: str, token: UUID) -> None:
    clock = await locked(session, row)
    if clock is None:
        return
    if row.status != "waiting_approval" or any(getattr(row, k) is not None for k in (
            "worker_id", "worker_lease_token", "worker_lease_expires_at", "worker_heartbeat_at")):
        raise ValueError("execution clock approval worker is not quiescent")
    if await has_unsettled_attempts(session, row):
        raise ValueError("execution clock has unresolved physical work")
    if clock.active_since is None:
        return  # Repeating the same quiet observation cannot refund/reset anything.
    if (clock.worker_id, clock.worker_token) != (worker_id, token):
        raise ValueError("execution clock approval ownership changed")
    debit(clock, await database_now(session))
    clock.active_since, clock.worker_id, clock.worker_token = None, None, None
    await session.flush()


def checkpoint_has_unsettled_effects(value: RuntimeCheckpoint) -> bool:
    """Only known ledger fields; this rejects uncertainty and never proves cleanup."""
    state = value.state
    for name in ("tool_ledger", "model_ledger", "tools", "models"):
        if name not in state:
            continue
        ledger = state[name]
        rows: Iterable[JsonValue]
        if isinstance(ledger, Mapping):
            rows = ledger.values()
        elif isinstance(ledger, (tuple, list)):
            rows = ledger
        else:
            return True
        for row in rows:
            if not isinstance(row, Mapping) or row.get("status") in {"unknown", "running", "issued"}:
                return True
    child = state.get("child_checkpoint")
    if child is not None:
        if not isinstance(child, Mapping):
            return True
        return checkpoint_has_unsettled_effects(RuntimeCheckpoint.from_payload(dict(child)))
    return False


async def close_segment(session: AsyncSession, row: RunRow, *, worker_id: str, token: UUID,
                        uncertain: bool) -> bool:
    """Settle only a proved quiet terminal interval; never refund holds or renew H."""
    clock = await locked(session, row)
    if clock is None:
        return False
    if uncertain or row.status not in {"completed", "failed", "cancelled", "paused"}:
        return False
    if any(getattr(row, key) is not None for key in (
            "worker_id", "worker_lease_token", "worker_lease_expires_at", "worker_heartbeat_at")):
        return False
    if clock.active_since is None:
        return True
    if (clock.worker_id, clock.worker_token) != (worker_id, token):
        raise ValueError("execution clock terminal ownership changed")
    if await has_unsettled_attempts(session, row):
        return False
    debit(clock, await database_now(session))
    clock.active_since, clock.worker_id, clock.worker_token = None, None, None
    await session.flush()
    return True
