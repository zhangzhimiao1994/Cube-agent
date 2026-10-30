"""Private pre-transport storage; review is never a sandbox permission grant.

All mutations lock RunRow -> pool -> attempt. Authorization rows are read only
after the RunRow lock (their writers also own that lock). Execution fences use
clock_timestamp AFTER lock waits, never PostgreSQL's transaction-start now().

The narrow *_in_session entries neither begin nor commit a transaction. A caller
must own an active transaction, call admission before locking its own intent
rows, and roll the entire transaction back on failure. This allows future review
events and holds to commit together. Public wrappers own their whole transaction.
An in-session issued=True is tentative until the caller commits; I/O must wait.
Pricing, conservative bounds, transport coverage and permits are separate work.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, NotRequired, TypedDict, Unpack, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunAdmissionEventRow,
    RunAdmissionPoolRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunRow,
)
from agent_hub.domain.runs import RunStatus
from agent_hub.runs.authorization import _AuthenticatedInput, _validated_persisted_input

type Purpose = Literal['primary', 'review']
type FinishOutcome = Literal['completed', 'not_issued', 'unknown']
type AttemptState = Literal['prepared', 'issued', 'completed', 'cancelled', 'unknown', 'invalid']
_INVALID = 'invalid admission binding'
_CONFLICT = 'admission state conflict'
_MAX_INT64 = 2**63 - 1
_MAX_TOKENS = 10_000_000
_IDENTITY = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_TERMINAL = frozenset({'completed', 'cancelled', 'unknown', 'invalid'})
# The current worker claims QUEUED/RETRYING into RUNNING before runtime entry.
# STEP_RETRYING and final synthesis/delivery do not change the RunRow status;
# renew_active_worker_lease only renews RUNNING. Domain enums alone grant nothing.
_EXECUTABLE_RUN_STATUSES = frozenset({RunStatus.RUNNING.value})


class AdmissionConflict(RuntimeError):
    """A fixed, private-safe refusal; never includes caller values or lease tokens."""


def _uuid(value: object) -> None:
    if type(value) is not UUID:
        raise ValueError(_INVALID)


def _identity(value: object) -> None:
    if type(value) is not str or _IDENTITY.fullmatch(value) is None:
        raise ValueError(_INVALID)


def _digest(value: object) -> None:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError(_INVALID)


def _positive(value: object, maximum: int = _MAX_INT64) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(_INVALID)
    return value


def _token_hold(value: object) -> int:
    return _positive(value, _MAX_TOKENS)


def _cost(value: object) -> Decimal:
    if type(value) is not Decimal:
        raise ValueError(_INVALID)
    exponent = value.as_tuple().exponent
    if (not value.is_finite() or value < 0 or value > 1_000_000
            or not isinstance(exponent, int) or exponent < -6):
        raise ValueError(_INVALID)
    return value


@dataclass(frozen=True, slots=True)
class _WorkerLease:
    worker_id: str
    token: UUID = field(repr=False)

    def __post_init__(self) -> None:
        _identity(self.worker_id)
        _uuid(self.token)


@dataclass(frozen=True, slots=True)
class DeploymentIdentity:
    """Already-resolved server identity, not a logical alias or pricing promise."""

    provider_id: str
    deployment_id: str
    model_id: str

    def __post_init__(self) -> None:
        for value in (self.provider_id, self.deployment_id, self.model_id):
            _identity(value)


def _deployment(value: DeploymentIdentity) -> dict[str, object]:
    if type(value) is not DeploymentIdentity:
        raise ValueError(_INVALID)
    value.__post_init__()
    return {'provider_id': value.provider_id, 'deployment_id': value.deployment_id,
                'model_id': value.model_id}


def _stored_deployment(row: RunAdmissionAttemptRow) -> DeploymentIdentity:
    try:
        if type(row.deployment) is not dict or row.deployment.keys() != {
            'provider_id', 'deployment_id', 'model_id',
        }:
            raise ValueError
        return DeploymentIdentity(cast(str, row.deployment['provider_id']),
            cast(str, row.deployment['deployment_id']), cast(str, row.deployment['model_id']))
    except (KeyError, TypeError, ValueError):
        raise AdmissionConflict(_CONFLICT) from None


@dataclass(frozen=True, slots=True)
class UsageObservation:
    """Untrusted fields: incomplete/invalid usage keeps the full attempt debit."""

    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cost_usd: Decimal | None


def _known_token_usage(usage: UsageObservation | None) -> bool:
    if type(usage) is not UsageObservation:
        return False
    if any(type(value) is not int or value < 0
           for value in (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)):
        return False
    return usage.total_tokens == cast(int, usage.prompt_tokens) + cast(int, usage.completion_tokens)


def _known_usage(usage: UsageObservation | None) -> bool:
    if not _known_token_usage(usage):
        return False
    assert usage is not None
    try:
        _cost(usage.cost_usd)
    except ValueError:
        return False
    return True


def _over_bound_usage(
    usage: UsageObservation | None, token_hold: int, review_cost_hold: Decimal | None,
) -> bool:
    if type(usage) is not UsageObservation:
        return False
    if _known_token_usage(usage) and cast(int, usage.total_tokens) > token_hold:
        return True
    actual_cost = usage.cost_usd
    # Actual evidence is not a new grant: storage precision/maximum must never
    # discard finite nonnegative proof that the conservative cost hold broke.
    if (review_cost_hold is not None and type(actual_cost) is Decimal
            and actual_cost.is_finite() and actual_cost >= 0):
        return actual_cost > review_cost_hold
    return False


def _observed_value(value: object) -> object:
    if value is None or type(value) in (bool, int):
        # Bound pathological integers without losing their identity to a normal zero charge.
        if type(value) is int and value.bit_length() > 4096:
            raw = value.to_bytes((value.bit_length() + 8) // 8, 'big', signed=True)
            return {'invalid_integer_bits': value.bit_length(),
                    'sha256': hashlib.sha256(raw).hexdigest()}
        return value
    if type(value) is Decimal:
        text = str(value)
        key = 'decimal'
    elif type(value) is float:
        return {'invalid_float': str(value)}
    elif type(value) is str:
        encoded = value.encode('utf-8', 'surrogatepass')
        if len(value) > 1024:
            return {'invalid_length': len(value), 'sha256': hashlib.sha256(encoded).hexdigest()}
        # JSONB cannot store NUL or unpaired surrogates, even in escaped JSON.
        return {'invalid_string_utf8_hex': encoded.hex()}
    else:
        return {'invalid_type': type(value).__name__.encode('unicode_escape').decode('ascii')[:64]}
    if len(text) > 1024:
        return {'invalid_length': len(text), 'sha256': hashlib.sha256(text.encode()).hexdigest()}
    return {key: text}


def _observation(usage: UsageObservation | None) -> dict[str, object] | None:
    if usage is None:
        return None
    if type(usage) is not UsageObservation:
        return {'invalid_type': type(usage).__name__.encode('unicode_escape').decode('ascii')[:64]}
    return {name: _observed_value(getattr(usage, name)) for name in
            ('prompt_tokens', 'completion_tokens', 'total_tokens', 'cost_usd')}


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf-8')


@dataclass(frozen=True, slots=True)
class PoolSnapshot:
    tenant_id: UUID
    run_id: UUID
    authorization_turn_id: UUID
    config_sha256: str
    original_token_grant: int
    original_deadline: datetime
    review_token_limit: int
    review_count_limit: int
    review_cost_limit: Decimal
    token_charged: int
    review_token_charged: int
    review_count_charged: int
    review_cost_charged: Decimal
    closed: bool

    @property
    def tokens_remaining(self) -> int:
        return self.original_token_grant - self.token_charged


@dataclass(frozen=True, slots=True)
class AttemptSnapshot:
    attempt_id: UUID
    purpose: Purpose
    state: AttemptState
    request_sha256: str
    deployment: DeploymentIdentity
    token_hold: int
    review_cost_hold: Decimal | None
    physical_transport_id: UUID | None
    receipt_sha256: str | None
    receipt_bytes: bytes | None = field(repr=False)


type AttemptReservation = AttemptSnapshot


class _ExecutionArguments(TypedDict):
    tenant_id: UUID
    run_id: UUID
    authorization_turn_id: UUID
    lease: _WorkerLease
    config_sha256: str


class _OpenArguments(_ExecutionArguments):
    original_token_grant: int
    original_deadline: datetime
    review_token_limit: int
    review_count_limit: int
    review_cost_limit: Decimal


class _ReserveArguments(_ExecutionArguments):
    attempt_id: UUID
    purpose: Purpose
    request_sha256: str
    deployment: DeploymentIdentity
    token_hold: int
    review_cost_hold: Decimal | None


class _IssueArguments(_ExecutionArguments):
    attempt_id: UUID
    physical_transport_id: UUID


class _FinishArguments(TypedDict):
    tenant_id: UUID
    run_id: UUID
    attempt_id: UUID
    lease: _WorkerLease
    deployment: DeploymentIdentity
    outcome: FinishOutcome
    usage: NotRequired[UsageObservation | None]


def _binding(tenant_id: UUID, run_id: UUID, turn: UUID, lease: _WorkerLease, config: str) -> None:
    for identity in (tenant_id, run_id, turn):
        _uuid(identity)
    if type(lease) is not _WorkerLease:
        raise ValueError(_INVALID)
    lease.__post_init__()
    _digest(config)


def _pool_snapshot(row: RunAdmissionPoolRow) -> PoolSnapshot:
    return PoolSnapshot(UUID(str(row.tenant_id)), UUID(str(row.run_id)),
        UUID(str(row.authorization_turn_id)), row.config_sha256, row.original_token_grant,
        row.original_deadline, row.review_token_limit, row.review_count_limit, row.review_cost_limit,
        row.token_charged, row.review_token_charged, row.review_count_charged,
        row.review_cost_charged, row.closed)


def _attempt_snapshot(row: RunAdmissionAttemptRow) -> AttemptSnapshot:
    return AttemptSnapshot(UUID(str(row.attempt_id)), cast(Purpose, row.purpose),
        cast(AttemptState, row.state), row.request_sha256,
        _stored_deployment(row),
        row.token_hold, row.review_cost_hold,
        None if row.physical_transport_id is None else UUID(str(row.physical_transport_id)),
        row.receipt_sha256, None if row.receipt is None else _canonical(row.receipt))


class RunAdmissionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self._session_factory() as session, session.begin():
                yield session
        except SQLAlchemyError:
            raise AdmissionConflict(_CONFLICT) from None

    async def _locked(
        self, session: AsyncSession, tenant_id: UUID, run_id: UUID,
    ) -> tuple[RunRow, RunAdmissionPoolRow | None]:
        _uuid(tenant_id)
        _uuid(run_id)
        if not session.in_transaction():
            raise AdmissionConflict(_CONFLICT)
        run = await session.scalar(select(RunRow).where(
            RunRow.tenant_id == tenant_id, RunRow.id == run_id).with_for_update()
            .execution_options(populate_existing=True))
        if run is None:
            raise AdmissionConflict(_CONFLICT)
        pool = await session.scalar(select(RunAdmissionPoolRow).where(
            RunAdmissionPoolRow.tenant_id == tenant_id, RunAdmissionPoolRow.run_id == run_id)
            .with_for_update().execution_options(populate_existing=True))
        return run, pool

    async def _fence(
        self, session: AsyncSession, run: RunRow, pool: RunAdmissionPoolRow | None,
        turn_id: UUID, lease: _WorkerLease, config: str,
    ) -> tuple[_AuthenticatedInput, datetime, int, float]:
        rows = (await session.execute(select(RunInputAuthorizationRow, RunAuthenticatedInputRow)
            .join(RunAuthenticatedInputRow,
                  RunAuthenticatedInputRow.turn_id == RunInputAuthorizationRow.turn_id)
            .where(RunInputAuthorizationRow.run_id == run.id)
            .execution_options(populate_existing=True))).one_or_none()
        evidence = None if rows is None else _validated_persisted_input(run, *rows)
        # Must be after ALL admission locks and current-link reads, including lock waits.
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        if (evidence is None or evidence.turn_id != turn_id
                or run.status not in _EXECUTABLE_RUN_STATUSES
                or run.mode not in ('direct', 'dispatch', 'discuss', 'hybrid')
                or run.blocked_by_run_id is not None or run.worker_id != lease.worker_id
                or run.worker_lease_token != lease.token or run.worker_lease_expires_at is None
                or run.worker_lease_expires_at <= now):
            raise AdmissionConflict(_CONFLICT)
        try:
            route = run.routing_decision or {}
            if type(route) is not dict:
                raise ValueError
            ceiling = min(_positive(route.get('runtime_token_absolute_tokens')),
                          _positive(route.get('runtime_plan_token_budget')), _MAX_TOKENS)
            timeout = route.get('runtime_timeout_absolute_seconds',
                                route.get('runtime_timeout_seconds'))
            if type(timeout) not in (int, float):
                raise ValueError
            timeout_number = cast(int | float, timeout)
            if not math.isfinite(timeout_number) or timeout_number <= 0:
                raise ValueError
            timeout_seconds = min(float(timeout_number), 3600.0)
        except (ValueError, TypeError, OverflowError):
            raise AdmissionConflict(_CONFLICT) from None
        if pool is not None and (
            (pool.authorization_turn_id, pool.actor_id, pool.actor_role, pool.message_sha256,
             pool.scope_sha256, pool.config_sha256)
            != (evidence.turn_id, evidence.actor_id, evidence.actor_role.value,
                evidence.message_sha256, evidence.scope_sha256, config)
            or pool.original_deadline <= now or pool.original_token_grant > ceiling
        ):
            raise AdmissionConflict(_CONFLICT)
        return evidence, now, ceiling, timeout_seconds

    async def _attempt(
        self, session: AsyncSession, pool: RunAdmissionPoolRow, attempt_id: UUID,
    ) -> RunAdmissionAttemptRow:
        _uuid(attempt_id)
        row = await session.scalar(select(RunAdmissionAttemptRow).where(
            RunAdmissionAttemptRow.tenant_id == pool.tenant_id,
            RunAdmissionAttemptRow.run_id == pool.run_id,
            RunAdmissionAttemptRow.attempt_id == attempt_id).with_for_update()
            .execution_options(populate_existing=True))
        if row is None:
            raise AdmissionConflict(_CONFLICT)
        _stored_deployment(row)
        return row

    async def _event(
        self, session: AsyncSession, pool: RunAdmissionPoolRow, kind: str,
        attempt: RunAdmissionAttemptRow | None, evidence: dict[str, object],
    ) -> None:
        session.add(RunAdmissionEventRow(run_id=pool.run_id, tenant_id=pool.tenant_id,
            attempt_id=None if attempt is None else attempt.attempt_id, kind=kind, evidence=evidence))
        await session.flush()

    async def open_pool(self, **arguments: Unpack[_OpenArguments]) -> PoolSnapshot:
        async with self._transaction() as session:
            return await self._open_pool_in_session(session, **arguments)

    async def _open_pool_in_session(
        self, session: AsyncSession, **a: Unpack[_OpenArguments],
    ) -> PoolSnapshot:
        _binding(a['tenant_id'], a['run_id'], a['authorization_turn_id'], a['lease'],
                 a['config_sha256'])
        grant = _token_hold(a['original_token_grant'])
        review_tokens = _positive(a['review_token_limit'], grant)
        review_count = _positive(a['review_count_limit'])
        review_cost = _cost(a['review_cost_limit'])
        deadline = a['original_deadline']
        if type(deadline) is not datetime or deadline.utcoffset() != timedelta(0):
            raise ValueError(_INVALID)
        run, pool = await self._locked(session, a['tenant_id'], a['run_id'])
        evidence, now, ceiling, timeout = await self._fence(session, run, pool,
            a['authorization_turn_id'], a['lease'], a['config_sha256'])
        if grant > ceiling or not now < deadline <= now + timedelta(seconds=timeout):
            raise AdmissionConflict(_CONFLICT)
        if pool is not None:
            if (pool.original_token_grant, pool.original_deadline, pool.review_token_limit,
                pool.review_count_limit, pool.review_cost_limit) != (
                    grant, deadline, review_tokens, review_count, review_cost):
                raise AdmissionConflict(_CONFLICT)
            return _pool_snapshot(pool)
        pool = RunAdmissionPoolRow(run_id=run.id, tenant_id=run.tenant_id,
            authorization_turn_id=evidence.turn_id, actor_id=evidence.actor_id,
            actor_role=evidence.actor_role.value, message_sha256=evidence.message_sha256,
            scope_sha256=evidence.scope_sha256, config_sha256=a['config_sha256'],
            original_token_grant=grant, original_deadline=deadline, review_token_limit=review_tokens,
            review_count_limit=review_count, review_cost_limit=review_cost, token_charged=0,
            review_token_charged=0, review_count_charged=0, review_cost_charged=Decimal(0), closed=False)
        session.add(pool)
        await session.flush()
        await self._event(session, pool, 'opened', None, {
            'authorization_turn_id': str(evidence.turn_id), 'config_sha256': pool.config_sha256,
            'original_token_grant': grant, 'original_deadline': deadline.isoformat(),
            'review_token_limit': review_tokens, 'review_count_limit': review_count,
            'review_cost_limit': str(review_cost)})
        return _pool_snapshot(pool)

    async def reserve(self, **arguments: Unpack[_ReserveArguments]) -> AttemptReservation:
        async with self._transaction() as session:
            return await self._reserve_in_session(session, **arguments)

    async def _reserve_in_session(
        self, session: AsyncSession, **a: Unpack[_ReserveArguments],
    ) -> AttemptReservation:
        _binding(a['tenant_id'], a['run_id'], a['authorization_turn_id'], a['lease'],
                 a['config_sha256'])
        _uuid(a['attempt_id'])
        _digest(a['request_sha256'])
        deployment = _deployment(a['deployment'])
        hold = _token_hold(a['token_hold'])
        purpose = a['purpose']
        if type(purpose) is not str or purpose not in ('primary', 'review'):
            raise ValueError(_INVALID)
        cost = None if purpose == 'primary' else _cost(a['review_cost_hold'])
        if purpose == 'primary' and a['review_cost_hold'] is not None:
            raise ValueError(_INVALID)
        run, pool = await self._locked(session, a['tenant_id'], a['run_id'])
        if pool is None:
            raise AdmissionConflict(_CONFLICT)
        await self._fence(session, run, pool, a['authorization_turn_id'], a['lease'], a['config_sha256'])
        row = await session.scalar(select(RunAdmissionAttemptRow).where(
            RunAdmissionAttemptRow.attempt_id == a['attempt_id'])
            .execution_options(populate_existing=True))
        if row is not None:
            if (row.tenant_id, row.run_id, row.purpose, row.request_sha256, row.deployment,
                row.worker_id, row.worker_lease_token, row.token_hold, row.review_cost_hold) != (
                    a['tenant_id'], a['run_id'], purpose, a['request_sha256'], deployment,
                    a['lease'].worker_id, a['lease'].token, hold, cost):
                raise AdmissionConflict(_CONFLICT)
            return _attempt_snapshot(row)
        if pool.closed or pool.token_charged + hold > pool.original_token_grant:
            raise AdmissionConflict(_CONFLICT)
        if purpose == 'review':
            assert cost is not None
            if (pool.review_token_charged + hold > pool.review_token_limit
                    or pool.review_count_charged >= pool.review_count_limit
                    or pool.review_cost_charged + cost > pool.review_cost_limit):
                raise AdmissionConflict(_CONFLICT)
            pool.review_token_charged += hold
            pool.review_count_charged += 1
            pool.review_cost_charged += cost
        pool.token_charged += hold
        row = RunAdmissionAttemptRow(attempt_id=a['attempt_id'], run_id=pool.run_id,
            tenant_id=pool.tenant_id, purpose=purpose, state='prepared',
            request_sha256=a['request_sha256'], deployment=deployment,
            worker_id=a['lease'].worker_id, worker_lease_token=a['lease'].token,
            token_hold=hold, review_cost_hold=cost)
        session.add(row)
        await session.flush()
        await self._event(session, pool, 'prepared', row, {'purpose': purpose,
            'request_sha256': row.request_sha256, 'deployment': deployment, 'token_hold': hold,
            'review_cost_hold': None if cost is None else str(cost)})
        return _attempt_snapshot(row)

    async def mark_issued(self, **arguments: Unpack[_IssueArguments]) -> bool:
        async with self._transaction() as session:
            return await self._mark_issued_in_session(session, **arguments)

    async def _mark_issued_in_session(
        self, session: AsyncSession, **a: Unpack[_IssueArguments],
    ) -> bool:
        try:
            _binding(a['tenant_id'], a['run_id'], a['authorization_turn_id'], a['lease'],
                     a['config_sha256'])
            _uuid(a['attempt_id'])
            _uuid(a['physical_transport_id'])
            run, pool = await self._locked(session, a['tenant_id'], a['run_id'])
            if pool is None:
                raise AdmissionConflict(_CONFLICT)
            row = await self._attempt(session, pool, a['attempt_id'])
            await self._fence(session, run, pool, a['authorization_turn_id'],
                              a['lease'], a['config_sha256'])
            if (row.worker_id, row.worker_lease_token) != (a['lease'].worker_id, a['lease'].token):
                raise AdmissionConflict(_CONFLICT)
            if row.physical_transport_id is not None:
                if row.physical_transport_id != a['physical_transport_id']:
                    raise AdmissionConflict(_CONFLICT)
                return False
            if pool.closed or row.state != 'prepared':
                raise AdmissionConflict(_CONFLICT)
            row.state, row.physical_transport_id = 'issued', a['physical_transport_id']
            await self._event(session, pool, 'issued', row,
                              {'physical_transport_id': str(a['physical_transport_id'])})
            return True
        except SQLAlchemyError:
            # Translate only. The caller still owns its entire transaction and
            # must roll it back; do not begin/commit/rollback or retry here.
            raise AdmissionConflict(_CONFLICT) from None

    async def finish(self, **arguments: Unpack[_FinishArguments]) -> AttemptSnapshot:
        async with self._transaction() as session:
            return await self._finish_in_session(session, **arguments)

    async def _finish_in_session(
        self, session: AsyncSession, **a: Unpack[_FinishArguments],
    ) -> AttemptSnapshot:
        _uuid(a['attempt_id'])
        if type(a['lease']) is not _WorkerLease:
            raise ValueError(_INVALID)
        a['lease'].__post_init__()
        deployment = _deployment(a['deployment'])
        outcome, usage = a['outcome'], a.get('usage')
        if type(outcome) is not str or outcome not in ('completed', 'not_issued', 'unknown'):
            raise ValueError(_INVALID)
        if outcome != 'completed' and usage is not None:
            raise ValueError(_INVALID)
        _, pool = await self._locked(session, a['tenant_id'], a['run_id'])
        if pool is None:
            raise AdmissionConflict(_CONFLICT)
        row = await self._attempt(session, pool, a['attempt_id'])
        if (row.worker_id, row.worker_lease_token, row.deployment) != (
                a['lease'].worker_id, a['lease'].token, deployment):
            raise AdmissionConflict(_CONFLICT)
        receipt: dict[str, object] = {'outcome': outcome, 'deployment': deployment,
            'attempt_id': str(row.attempt_id), 'physical_transport_id': (
                None if row.physical_transport_id is None else str(row.physical_transport_id)),
            'usage': _observation(usage)}
        receipt_sha = hashlib.sha256(_canonical(receipt)).hexdigest()
        if row.state in _TERMINAL:
            if row.receipt_sha256 != receipt_sha or row.receipt != receipt:
                raise AdmissionConflict(_CONFLICT)
            return _attempt_snapshot(row)
        if outcome == 'not_issued':
            if row.state != 'prepared' or row.physical_transport_id is not None:
                raise AdmissionConflict(_CONFLICT)
            row.state = 'cancelled'
            self._refund(pool, row, 0, Decimal(0), cancel=True)
        else:
            if row.state != 'issued' or row.physical_transport_id is None:
                raise AdmissionConflict(_CONFLICT)
            if outcome == 'completed' and _over_bound_usage(usage, row.token_hold, row.review_cost_hold):
                # Either independently valid component can prove the bound broke,
                # even when another receipt component is missing/invalid.
                row.state, pool.closed = 'invalid', True
            elif outcome == 'unknown' or not _known_usage(usage):
                # Consumed UNKNOWN is terminal for THIS attempt, not authority to
                # replay its logical/effect work. Other authorized primary work
                # may still use the original remaining balance and deadline.
                row.state = 'unknown'
            else:
                row.state = 'completed'
                assert usage is not None
                self._refund(pool, row, cast(int, usage.total_tokens), cast(Decimal, usage.cost_usd))
        row.receipt, row.receipt_sha256 = receipt, receipt_sha
        await self._event(session, pool, row.state, row,
                          {'receipt_sha256': receipt_sha, 'receipt': receipt})
        return _attempt_snapshot(row)

    @staticmethod
    def _refund(
        pool: RunAdmissionPoolRow, row: RunAdmissionAttemptRow, tokens: int, cost: Decimal,
        *, cancel: bool = False,
    ) -> None:
        pool.token_charged -= row.token_hold - tokens
        if row.purpose == 'review':
            pool.review_token_charged -= row.token_hold - tokens
            pool.review_cost_charged -= cast(Decimal, row.review_cost_hold) - cost
            if cancel:
                pool.review_count_charged -= 1

    async def recover_issued(self, tenant_id: UUID, run_id: UUID, attempt_id: UUID) -> AttemptSnapshot:
        async with self._transaction() as session:
            return await self._recover_issued_in_session(session, tenant_id, run_id, attempt_id)

    async def _recover_issued_in_session(
        self, session: AsyncSession, tenant_id: UUID, run_id: UUID, attempt_id: UUID,
    ) -> AttemptSnapshot:
        _, pool = await self._locked(session, tenant_id, run_id)
        if pool is None:
            raise AdmissionConflict(_CONFLICT)
        row = await self._attempt(session, pool, attempt_id)
        if row.state in _TERMINAL:
            return _attempt_snapshot(row)
        if row.state != 'issued' or row.physical_transport_id is None:
            raise AdmissionConflict(_CONFLICT)
        return await self._finish_in_session(session, tenant_id=tenant_id, run_id=run_id,
            attempt_id=attempt_id, lease=_WorkerLease(row.worker_id, UUID(str(row.worker_lease_token))),
            deployment=_stored_deployment(row),
            outcome='unknown')

    async def get_pool(self, tenant_id: UUID, run_id: UUID) -> PoolSnapshot | None:
        _uuid(tenant_id)
        _uuid(run_id)
        async with self._session_factory() as session:
            row = await session.scalar(select(RunAdmissionPoolRow).where(
                RunAdmissionPoolRow.tenant_id == tenant_id, RunAdmissionPoolRow.run_id == run_id))
            return None if row is None else _pool_snapshot(row)

    async def get_attempt(
        self, tenant_id: UUID, run_id: UUID, attempt_id: UUID,
    ) -> AttemptSnapshot | None:
        for identity in (tenant_id, run_id, attempt_id):
            _uuid(identity)
        async with self._session_factory() as session:
            row = await session.scalar(select(RunAdmissionAttemptRow).where(
                RunAdmissionAttemptRow.tenant_id == tenant_id,
                RunAdmissionAttemptRow.run_id == run_id,
                RunAdmissionAttemptRow.attempt_id == attempt_id))
            return None if row is None else _attempt_snapshot(row)
