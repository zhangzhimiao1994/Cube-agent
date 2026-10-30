"""Private review ledger and STORAGE-only one-use consumption.

Review is not a sandbox permission grant. No live Root, current administrator
configuration, tool dispatch or provider transport is attested by this module.
Task4/5 trusted loaders must revalidate those facts before actual dispatch.
All writes lock RunRow -> pool -> effect/intent -> attempt and compose 2B inside
one caller transaction. In-session results are tentative until caller commit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from functools import wraps
from typing import Literal, cast
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import Role
from agent_hub.capabilities.review_contracts import (
    ApprovalReviewConfig,
    FrozenReviewAction,
    ModelReviewVerdict,
    freeze_review_action,
    parse_model_review,
)
from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunAdmissionPoolRow,
    RunReviewEffectRow,
    RunReviewEventRow,
    RunReviewIntentRow,
    RunRow,
)
from agent_hub.models.run_admission_repository import (
    AdmissionConflict,
    DeploymentIdentity,
    RunAdmissionRepository,
    UsageObservation,
    _cost,
    _deployment,
    _digest,
    _known_usage,
    _observation,
    _over_bound_usage,
    _stored_deployment,
    _token_hold,
    _uuid,
    _WorkerLease,
)
from agent_hub.models.run_admission_repository import (
    _canonical as _admission_canonical,
)
from agent_hub.runs.authorization import _AuthenticatedInput, _validate_authenticated_input

_INVALID = "invalid review binding"
_CONFLICT = "review state conflict"
_MAX_ACTION_BYTES = 65536


class ReviewConflict(RuntimeError):
    """Fixed private-safe error; a failed caller transaction requires rollback."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def authorization_binding_sha256(evidence: _AuthenticatedInput) -> str:
    """Pure schema1 hash of validated evidence; this confers no authority.

    Exact keys: schema_version, tenant_id, user_id, actor_role, turn_id,
    message_sha256, scope_sha256. UTF8, sorted compact JSON, no NaN.
    begin always reloads current DB evidence under RunRow lock.
    """
    try:
        _validate_authenticated_input(
            evidence, evidence.tenant_id, evidence.actor_id, evidence.actor_role, evidence.message
        )
        return _sha(
            _canonical(
                {
                    "schema_version": 1,
                    "tenant_id": str(evidence.tenant_id),
                    "user_id": str(evidence.actor_id),
                    "actor_role": evidence.actor_role.value,
                    "turn_id": str(evidence.turn_id),
                    "message_sha256": evidence.message_sha256,
                    "scope_sha256": evidence.scope_sha256,
                }
            )
        )
    except (ValueError, TypeError, AttributeError):
        raise ValueError(_INVALID) from None


def review_config_sha256(config: ApprovalReviewConfig) -> str:
    try:
        if type(config) is not ApprovalReviewConfig:
            raise ValueError
        validated = ApprovalReviewConfig.from_payload(config.to_payload())
        return _sha(_canonical(validated.to_payload()))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(_INVALID) from None


def _validated_action(action: FrozenReviewAction) -> FrozenReviewAction:
    try:
        if (
            type(action) is not FrozenReviewAction
            or type(action._payload_bytes) is not bytes
            or not 1 <= len(action._payload_bytes) <= _MAX_ACTION_BYTES
        ):
            raise ValueError
        p = json.loads(action._payload_bytes)
        if (
            type(p) is not dict
            or p.keys()
            != {
                "schema_version",
                "request",
                "role",
                "authorization_turn_id",
                "authorization_sha256",
                "policy_sha256",
                "config_sha256",
                "source_binding",
                "normalized_effect",
            }
            or type(p["schema_version"]) is not int
            or p["schema_version"] != 1
            or _canonical(p) != action._payload_bytes
            or "\x00" in action._payload_bytes.decode("utf-8")
            or "\\u0000" in action._payload_bytes.decode("utf-8")
        ):
            raise ValueError
        # Rebuild ALL fields via Task1, including strict CapabilityRequest validation.
        rebuilt = freeze_review_action(
            action.request(),
            role=Role(p["role"]),
            authorization_turn_id=UUID(p["authorization_turn_id"]),
            authorization_sha256=p["authorization_sha256"],
            policy_sha256=p["policy_sha256"],
            config_sha256=p["config_sha256"],
            source_binding=p["source_binding"],
            normalized_effect=p["normalized_effect"],
        )
        if rebuilt != action:
            raise ValueError
        return rebuilt
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        UnicodeError,
        RecursionError,
        OverflowError,
    ):
        raise ValueError(_INVALID) from None


def _validated_verdict(verdict: ModelReviewVerdict, action_digest: str) -> ModelReviewVerdict:
    try:
        if type(verdict) is not ModelReviewVerdict:
            raise ValueError
        return parse_model_review(
            _canonical(verdict.to_payload()).decode(), expected_action_digest=action_digest
        )
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise ValueError(_INVALID) from None


def _breaker_open(history: Sequence[str], config: ApprovalReviewConfig) -> bool:
    """Oldest first; only explicit usable allow / strict deny occupy the window.

    Errors/UNKNOWN/needs_human/cancelled neither reset streak nor evict denials.
    Only explicit usable allow resets streak; last W conclusive outcomes define
    the denial window. An effect contributes at most one terminal review.
    """
    review_config_sha256(config)
    conclusive = [state for state in history if state in ("allow", "deny")]
    streak = 0
    for state in conclusive:
        streak = streak + 1 if state == "deny" else 0
    return (
        streak >= config.denial_breaker_consecutive
        or conclusive[-config.denial_breaker_window :].count("deny")
        >= config.denial_breaker_denials
    )


@dataclass(frozen=True, slots=True)
class _ReviewReservation:
    request_sha256: str
    deployment: DeploymentIdentity
    token_hold: int
    cost_hold: Decimal
    lease: _WorkerLease = field(repr=False)

    def __post_init__(self) -> None:
        try:
            _digest(self.request_sha256)
            _deployment(self.deployment)
            _token_hold(self.token_hold)
            _cost(self.cost_hold)
            if type(self.lease) is not _WorkerLease:
                raise ValueError
            self.lease.__post_init__()
        except (ValueError, TypeError, AttributeError):
            raise ValueError(_INVALID) from None


@dataclass(frozen=True, slots=True)
class _CurrentReviewBinding:
    """Trusted loader input, never exposed user input or live OS attestation."""

    snapshot: FrozenReviewAction = field(repr=False)
    config: ApprovalReviewConfig = field(repr=False)
    lease: _WorkerLease = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReviewIntent:
    """Public-safe durable result. created=False never authorizes a paid call."""

    review_id: UUID
    tenant_id: UUID
    run_id: UUID
    attempt_id: UUID
    action_digest: str
    effect_digest: str
    authorization_sha256: str
    config_sha256: str
    policy_sha256: str
    source_sha256: str
    request_sha256: str
    model_identity: DeploymentIdentity
    physical_identity: UUID | None
    status: str
    risk: str | None
    reason_code: str | None
    consumed: bool
    created: bool = False

    def public_summary(self) -> dict[str, object]:
        return {
            "review_id": str(self.review_id),
            "tenant_id": str(self.tenant_id),
            "run_id": str(self.run_id),
            "attempt_id": str(self.attempt_id),
            "action_digest": self.action_digest,
            "effect_digest": self.effect_digest,
            "authorization_sha256": self.authorization_sha256,
            "config_sha256": self.config_sha256,
            "policy_sha256": self.policy_sha256,
            "source_sha256": self.source_sha256,
            "request_sha256": self.request_sha256,
            "model_identity": _deployment(self.model_identity),
            "physical_identity": None
            if self.physical_identity is None
            else str(self.physical_identity),
            "status": self.status,
            "risk": self.risk,
            "reason_code": self.reason_code,
            "consumed": self.consumed,
        }


type ReviewOutcome = Literal[
    "allow",
    "deny",
    "needs_human",
    "timeout",
    "unavailable",
    "invalid_response",
    "unknown",
    "cancelled",
]
_OUTCOMES = frozenset(
    {
        "allow",
        "deny",
        "needs_human",
        "timeout",
        "unavailable",
        "invalid_response",
        "unknown",
        "cancelled",
    }
)


def _accounting_outcome(outcome: ReviewOutcome) -> Literal["completed", "not_issued", "unknown"]:
    if outcome == "cancelled":
        return "not_issued"
    return "completed" if outcome in ("allow", "deny", "needs_human") else "unknown"


def _private_errors[**P, T](operation: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    @wraps(operation)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await operation(*args, **kwargs)
        except (SQLAlchemyError, AdmissionConflict):
            raise ReviewConflict(_CONFLICT) from None
        except ValueError:
            raise ValueError(_INVALID) from None

    return wrapped


def _require_transaction(session: AsyncSession) -> None:
    if not session.in_transaction():
        raise ReviewConflict(_CONFLICT)


def _attempt_id(review_id: UUID) -> UUID:
    _uuid(review_id)
    return uuid5(review_id, "review-model-request/schema1")


def _stored(row: RunReviewIntentRow) -> tuple[FrozenReviewAction, ApprovalReviewConfig]:
    try:
        a = _validated_action(
            FrozenReviewAction(
                row.action_digest, row.effect_digest, row.private_action.encode("utf-8")
            )
        )
        c = ApprovalReviewConfig.from_payload(row.private_config)
        p, req = a.to_payload(), a.request()
        if (
            row.attempt_id != _attempt_id(UUID(str(row.review_id)))
            or (
                req.tenant_id,
                req.run_id,
                p["authorization_turn_id"],
                p["authorization_sha256"],
                p["config_sha256"],
                p["policy_sha256"],
                _sha(_canonical(p["source_binding"])),
            )
            != (
                row.tenant_id,
                row.run_id,
                str(row.authorization_turn_id),
                row.authorization_sha256,
                row.config_sha256,
                row.policy_sha256,
                row.source_sha256,
            )
            or review_config_sha256(c) != row.config_sha256
            or c.engine != "rules_then_model"
        ):
            raise ValueError
        _digest(row.request_sha256)
        return a, c
    except (ValueError, TypeError, AttributeError):
        raise ReviewConflict(_CONFLICT) from None


def _receipt(row: RunReviewIntentRow) -> dict[str, object]:
    try:
        if row.private_receipt is None or len(row.private_receipt.encode()) > 16384:
            raise ValueError
        p = json.loads(row.private_receipt)
        if (
            type(p) is not dict
            or p.keys() != {"evidence", "accounting_receipt_sha256"}
            or _canonical(p).decode() != row.private_receipt
            or _sha(_canonical(p)) != row.receipt_sha256
        ):
            raise ValueError
        return cast(dict[str, object], p)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ReviewConflict(_CONFLICT) from None


def _check_attempt(row: RunReviewIntentRow, attempt: RunAdmissionAttemptRow) -> None:
    if (
        attempt.purpose != "review"
        or attempt.request_sha256 != row.request_sha256
        or attempt.physical_transport_id != row.physical_identity
    ):
        raise ReviewConflict(_CONFLICT)
    _stored_deployment(attempt)


def _snapshot(
    row: RunReviewIntentRow, attempt: RunAdmissionAttemptRow, *, created: bool = False
) -> ReviewIntent:
    _stored(row)
    _check_attempt(row, attempt)
    return ReviewIntent(
        UUID(str(row.review_id)),
        UUID(str(row.tenant_id)),
        UUID(str(row.run_id)),
        UUID(str(row.attempt_id)),
        row.action_digest,
        row.effect_digest,
        row.authorization_sha256,
        row.config_sha256,
        row.policy_sha256,
        row.source_sha256,
        row.request_sha256,
        _stored_deployment(attempt),
        None if row.physical_identity is None else UUID(str(row.physical_identity)),
        row.status,
        row.risk,
        row.reason_code,
        row.consumed,
        created,
    )


class ReviewRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._budget = RunAdmissionRepository(session_factory)

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self._session_factory() as session, session.begin():
                yield session
        except (SQLAlchemyError, AdmissionConflict):
            raise ReviewConflict(_CONFLICT) from None

    async def _effect(
        self, session: AsyncSession, tenant_id: UUID, turn_id: UUID, effect: str
    ) -> RunReviewEffectRow | None:
        return cast(
            RunReviewEffectRow | None,
            await session.scalar(
                select(RunReviewEffectRow)
                .where(
                    RunReviewEffectRow.tenant_id == tenant_id,
                    RunReviewEffectRow.authorization_turn_id == turn_id,
                    RunReviewEffectRow.effect_digest == effect,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )

    async def _intent(
        self, session: AsyncSession, tenant_id: UUID, run_id: UUID, review_id: UUID
    ) -> RunReviewIntentRow:
        _uuid(review_id)
        row = await session.scalar(
            select(RunReviewIntentRow)
            .where(
                RunReviewIntentRow.tenant_id == tenant_id,
                RunReviewIntentRow.run_id == run_id,
                RunReviewIntentRow.review_id == review_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise ReviewConflict(_CONFLICT)
        _stored(row)
        return row

    async def _locked(
        self, session: AsyncSession, tenant_id: UUID, run_id: UUID, review_id: UUID
    ) -> tuple[RunRow, RunAdmissionPoolRow, RunReviewIntentRow]:
        _require_transaction(session)
        for value in (tenant_id, run_id, review_id):
            _uuid(value)
        run, pool = await self._budget._locked(session, tenant_id, run_id)
        if pool is None:
            raise ReviewConflict(_CONFLICT)
        # Run lock serializes all original-authorization writes. Locate effect
        # without locking intent, then lock effect -> intent -> budget attempt.
        effect = await session.scalar(
            select(RunReviewEffectRow)
            .where(
                RunReviewEffectRow.tenant_id == tenant_id,
                RunReviewEffectRow.run_id == run_id,
                RunReviewEffectRow.review_id == review_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if effect is None:
            raise ReviewConflict(_CONFLICT)
        row = await self._intent(session, tenant_id, run_id, review_id)
        if (effect.authorization_turn_id, effect.effect_digest) != (
            row.authorization_turn_id,
            row.effect_digest,
        ):
            raise ReviewConflict(_CONFLICT)
        return run, pool, row

    async def _event(
        self, session: AsyncSession, row: RunReviewIntentRow, kind: str, evidence: dict[str, object]
    ) -> None:
        session.add(
            RunReviewEventRow(
                tenant_id=row.tenant_id,
                run_id=row.run_id,
                authorization_turn_id=row.authorization_turn_id,
                review_id=row.review_id,
                kind=kind,
                evidence=evidence,
            )
        )
        await session.flush()

    async def _history(self, session: AsyncSession, tenant_id: UUID, turn_id: UUID) -> list[str]:
        events = (
            await session.scalars(
                select(RunReviewEventRow)
                .where(
                    RunReviewEventRow.tenant_id == tenant_id,
                    RunReviewEventRow.authorization_turn_id == turn_id,
                    RunReviewEventRow.kind == "terminal",
                )
                .order_by(RunReviewEventRow.sequence)
            )
        ).all()
        return [cast(str, event.evidence.get("status")) for event in events]

    async def begin_intent(
        self,
        *,
        snapshot: FrozenReviewAction,
        config: ApprovalReviewConfig,
        reservation: _ReviewReservation,
        review_id: UUID,
    ) -> ReviewIntent:
        async with self._transaction() as session:
            return await self._begin_intent_in_session(
                session,
                snapshot=snapshot,
                config=config,
                reservation=reservation,
                review_id=review_id,
            )

    @_private_errors
    async def _begin_intent_in_session(
        self,
        session: AsyncSession,
        *,
        snapshot: FrozenReviewAction,
        config: ApprovalReviewConfig,
        reservation: _ReviewReservation,
        review_id: UUID,
    ) -> ReviewIntent:
        _require_transaction(session)
        a = _validated_action(snapshot)
        config_sha = review_config_sha256(config)
        if type(reservation) is not _ReviewReservation or config.engine != "rules_then_model":
            raise ValueError(_INVALID)
        reservation.__post_init__()
        _uuid(review_id)
        p, req = a.to_payload(), a.request()
        turn = UUID(cast(str, p["authorization_turn_id"]))
        run, pool = await self._budget._locked(session, req.tenant_id, req.run_id)
        if pool is None:
            raise ReviewConflict(_CONFLICT)
        evidence, _, _, _ = await self._budget._fence(
            session, run, pool, turn, reservation.lease, config_sha
        )
        if (
            req.user_id != evidence.actor_id
            or p["role"] != evidence.actor_role.value
            or p["authorization_sha256"] != authorization_binding_sha256(evidence)
            or p["config_sha256"] != config_sha
            or pool.review_count_limit > config.max_reviews_per_run
            or pool.review_cost_limit > Decimal(config.max_cost_usd_per_run)
            or reservation.token_hold > config.max_input_tokens + config.max_output_tokens
        ):
            raise ReviewConflict(_CONFLICT)
        effect = await self._effect(session, req.tenant_id, turn, a.effect_digest)
        if effect is not None:
            if effect.run_id != req.run_id:
                raise ReviewConflict(_CONFLICT)
            existing = await self._intent(
                session, req.tenant_id, req.run_id, UUID(str(effect.review_id))
            )
            attempt = await self._budget._attempt(session, pool, UUID(str(existing.attempt_id)))
            if existing.status not in ("prepared", "issued"):
                self._terminal_proof(existing, attempt)
            return _snapshot(existing, attempt)
        if _breaker_open(await self._history(session, req.tenant_id, turn), config):
            raise ReviewConflict(_CONFLICT)
        attempt_id = _attempt_id(review_id)
        # New intent and effect claim cannot survive if budget reserve fails.
        await self._budget._reserve_in_session(
            session,
            tenant_id=req.tenant_id,
            run_id=req.run_id,
            authorization_turn_id=turn,
            config_sha256=config_sha,
            lease=reservation.lease,
            attempt_id=attempt_id,
            purpose="review",
            request_sha256=reservation.request_sha256,
            deployment=reservation.deployment,
            token_hold=reservation.token_hold,
            review_cost_hold=reservation.cost_hold,
        )
        row = RunReviewIntentRow(
            review_id=review_id,
            tenant_id=req.tenant_id,
            run_id=req.run_id,
            authorization_turn_id=turn,
            attempt_id=attempt_id,
            action_digest=a.action_digest,
            effect_digest=a.effect_digest,
            authorization_sha256=p["authorization_sha256"],
            config_sha256=config_sha,
            policy_sha256=p["policy_sha256"],
            source_sha256=_sha(_canonical(p["source_binding"])),
            request_sha256=reservation.request_sha256,
            private_action=a._payload_bytes.decode(),
            private_config=config.to_payload(),
            status="prepared",
            consumed=False,
        )
        session.add(row)
        await session.flush()
        session.add(
            RunReviewEffectRow(
                tenant_id=req.tenant_id,
                authorization_turn_id=turn,
                effect_digest=a.effect_digest,
                run_id=req.run_id,
                review_id=review_id,
            )
        )
        await self._event(
            session,
            row,
            "prepared",
            {
                "action_digest": a.action_digest,
                "request_sha256": reservation.request_sha256,
                "attempt_id": str(attempt_id),
            },
        )
        attempt = await self._budget._attempt(session, pool, attempt_id)
        return _snapshot(row, attempt, created=True)

    async def mark_issued(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        review_id: UUID,
        physical_identity: UUID,
        lease: _WorkerLease,
    ) -> bool:
        async with self._transaction() as session:
            return await self._mark_issued_in_session(
                session,
                tenant_id=tenant_id,
                run_id=run_id,
                review_id=review_id,
                physical_identity=physical_identity,
                lease=lease,
            )

    @_private_errors
    async def _mark_issued_in_session(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        review_id: UUID,
        physical_identity: UUID,
        lease: _WorkerLease,
    ) -> bool:
        _uuid(physical_identity)
        run, pool, row = await self._locked(session, tenant_id, run_id, review_id)
        attempt = await self._budget._attempt(session, pool, UUID(str(row.attempt_id)))
        _check_attempt(row, attempt)
        # Even duplicate requests must prove original private lease identity.
        if type(lease) is not _WorkerLease:
            raise ValueError(_INVALID)
        lease.__post_init__()
        if (attempt.worker_id, attempt.worker_lease_token) != (lease.worker_id, lease.token):
            raise ReviewConflict(_CONFLICT)
        if row.physical_identity is not None:
            if row.physical_identity != physical_identity:
                raise ReviewConflict(_CONFLICT)
            return False
        await self._budget._fence(
            session, run, pool, UUID(str(row.authorization_turn_id)), lease, row.config_sha256
        )
        if row.status != "prepared":
            raise ReviewConflict(_CONFLICT)
        issued = await self._budget._mark_issued_in_session(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
            authorization_turn_id=UUID(str(row.authorization_turn_id)),
            lease=lease,
            config_sha256=row.config_sha256,
            attempt_id=UUID(str(row.attempt_id)),
            physical_transport_id=physical_identity,
        )
        if not issued:
            raise ReviewConflict(_CONFLICT)
        row.status, row.physical_identity = "issued", physical_identity
        await self._event(session, row, "issued", {"physical_identity": str(physical_identity)})
        return True

    async def finish(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        review_id: UUID,
        attempt_id: UUID,
        request_sha256: str,
        physical_identity: UUID | None,
        lease: _WorkerLease,
        actual_model_identity: DeploymentIdentity,
        outcome: ReviewOutcome,
        usage: UsageObservation | None = None,
        verdict: ModelReviewVerdict | None = None,
    ) -> ReviewIntent:
        async with self._transaction() as session:
            return await self._finish_in_session(
                session,
                tenant_id=tenant_id,
                run_id=run_id,
                review_id=review_id,
                attempt_id=attempt_id,
                request_sha256=request_sha256,
                physical_identity=physical_identity,
                lease=lease,
                actual_model_identity=actual_model_identity,
                outcome=outcome,
                usage=usage,
                verdict=verdict,
            )

    @_private_errors
    async def _finish_in_session(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        review_id: UUID,
        attempt_id: UUID,
        request_sha256: str,
        physical_identity: UUID | None,
        lease: _WorkerLease,
        actual_model_identity: DeploymentIdentity,
        outcome: ReviewOutcome,
        usage: UsageObservation | None = None,
        verdict: ModelReviewVerdict | None = None,
    ) -> ReviewIntent:
        _, pool, row = await self._locked(session, tenant_id, run_id, review_id)
        attempt = await self._budget._attempt(session, pool, UUID(str(row.attempt_id)))
        _check_attempt(row, attempt)
        _uuid(attempt_id)
        _digest(request_sha256)
        if physical_identity is not None:
            _uuid(physical_identity)
        if type(lease) is not _WorkerLease:
            raise ValueError(_INVALID)
        lease.__post_init__()
        if (
            attempt_id,
            request_sha256,
            physical_identity,
            _deployment(actual_model_identity),
            lease.worker_id,
            lease.token,
        ) != (
            row.attempt_id,
            row.request_sha256,
            row.physical_identity,
            attempt.deployment,
            attempt.worker_id,
            attempt.worker_lease_token,
        ):
            raise ReviewConflict(_CONFLICT)
        if type(outcome) is not str or outcome not in _OUTCOMES:
            raise ValueError(_INVALID)
        v = None
        if outcome in ("allow", "deny", "needs_human"):
            v = _validated_verdict(cast(ModelReviewVerdict, verdict), row.action_digest)
            if v.decision != outcome:
                raise ValueError(_INVALID)
        elif verdict is not None:
            raise ValueError(_INVALID)
        if outcome == "cancelled" and usage is not None:
            raise ValueError(_INVALID)
        # Bounded observation contains identity for malformed values without provider body.
        evidence: dict[str, object] = {
            "outcome": outcome,
            "attempt_id": str(attempt_id),
            "request_sha256": request_sha256,
            "physical_identity": (None if physical_identity is None else str(physical_identity)),
            "model_identity": _deployment(actual_model_identity),
            "usage": _observation(usage),
            "verdict": None if v is None else v.to_payload(),
        }
        if row.status not in ("prepared", "issued"):
            existing = _receipt(row)
            if existing["evidence"] != evidence:
                raise ReviewConflict(_CONFLICT)
            self._terminal_proof(row, attempt)
            return _snapshot(row, attempt)
        if outcome == "cancelled":
            if row.status != "prepared" or physical_identity is not None:
                raise ReviewConflict(_CONFLICT)
        else:
            if row.status != "issued" or physical_identity is None:
                raise ReviewConflict(_CONFLICT)
            # Receipt may arrive after cancellation/lease loss: account facts only.
        budget_outcome = _accounting_outcome(outcome)
        if budget_outcome == "unknown" and _over_bound_usage(
            usage, attempt.token_hold, attempt.review_cost_hold
        ):
            budget_outcome = "completed"
        settled = await self._budget._finish_in_session(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
            attempt_id=attempt_id,
            lease=lease,
            deployment=actual_model_identity,
            outcome=budget_outcome,
            usage=usage if budget_outcome == "completed" else None,
        )
        state = outcome
        if outcome == "allow" and (
            settled.state != "completed" or pool.closed or not _known_usage(usage)
        ):
            state = "unknown"
        row.status = state
        row.risk = "unknown" if v is None or state != outcome else v.risk
        row.reason_code = (
            v.reason_code
            if v is not None and state == outcome
            else "accounting_unknown"
            if outcome == "allow"
            else "review_" + outcome
        )
        receipt: dict[str, object] = {
            "evidence": evidence,
            "accounting_receipt_sha256": settled.receipt_sha256,
        }
        raw = _canonical(receipt)
        if len(raw) > 16384:
            raise ValueError(_INVALID)
        row.private_receipt, row.receipt_sha256 = raw.decode(), _sha(raw)
        await self._event(
            session,
            row,
            "terminal",
            {
                "status": state,
                "receipt_sha256": row.receipt_sha256,
                "risk": row.risk,
                "reason_code": row.reason_code,
            },
        )
        return _snapshot(row, attempt)

    @staticmethod
    def _terminal_proof(row: RunReviewIntentRow, attempt: RunAdmissionAttemptRow) -> None:
        """Revalidate committed verdict/accounting, not caller dataclass or raw state."""
        try:
            receipt = _receipt(row)
            evidence = receipt["evidence"]
            if type(evidence) is not dict or evidence.keys() != {
                "outcome",
                "attempt_id",
                "request_sha256",
                "physical_identity",
                "model_identity",
                "usage",
                "verdict",
            }:
                raise ValueError
            if (
                receipt["accounting_receipt_sha256"] != attempt.receipt_sha256
                or attempt.receipt is None
                or _sha(_admission_canonical(attempt.receipt)) != attempt.receipt_sha256
                or (
                    evidence["attempt_id"],
                    evidence["request_sha256"],
                    evidence["physical_identity"],
                    evidence["model_identity"],
                )
                != (
                    str(row.attempt_id),
                    row.request_sha256,
                    None if row.physical_identity is None else str(row.physical_identity),
                    attempt.deployment,
                )
            ):
                raise ValueError
            outcome = evidence["outcome"]
            v = None
            if outcome in ("allow", "deny", "needs_human"):
                v = parse_model_review(
                    _canonical(evidence["verdict"]).decode(),
                    expected_action_digest=row.action_digest,
                )
                if v.decision != outcome:
                    raise ValueError
            elif outcome not in _OUTCOMES or evidence["verdict"] is not None:
                raise ValueError
            if (
                (row.status == "allow" and (outcome != "allow" or attempt.state != "completed"))
                or (row.status != outcome and not (outcome == "allow" and row.status == "unknown"))
                or (row.status == "cancelled" and attempt.state != "cancelled")
                or (
                    row.status != "cancelled"
                    and attempt.state not in ("completed", "unknown", "invalid")
                )
            ):
                raise ValueError
            risk = "unknown" if v is None or row.status != outcome else v.risk
            reason = (
                v.reason_code
                if v is not None and row.status == outcome
                else "accounting_unknown"
                if outcome == "allow"
                else "review_" + cast(str, outcome)
            )
            if (row.risk, row.reason_code) != (risk, reason):
                raise ValueError
            if row.status == "allow":
                # Budget receipt must refer to the same issued model/physical UUID,
                # and retain the exact complete usage whose conservative bound held.
                ar = attempt.receipt
                if (
                    ar.get("outcome") != "completed"
                    or ar.get("usage") != evidence["usage"]
                    or ar.get("attempt_id") != str(row.attempt_id)
                    or ar.get("physical_transport_id") != str(row.physical_identity)
                    or ar.get("deployment") != attempt.deployment
                ):
                    raise ValueError
                obs = evidence["usage"]
                if type(obs) is not dict or type(obs.get("cost_usd")) is not dict:
                    raise ValueError
                cost = Decimal(obs["cost_usd"]["decimal"])
                usage = UsageObservation(
                    obs["prompt_tokens"], obs["completion_tokens"], obs["total_tokens"], cost
                )
                if (
                    not _known_usage(usage)
                    or cast(int, usage.total_tokens) > attempt.token_hold
                    or attempt.review_cost_hold is None
                    or cost > attempt.review_cost_hold
                ):
                    raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            raise ReviewConflict(_CONFLICT) from None

    async def recover_issued(self, tenant_id: UUID, run_id: UUID, review_id: UUID) -> ReviewIntent:
        async with self._transaction() as session:
            return await self._recover_issued_in_session(session, tenant_id, run_id, review_id)

    @_private_errors
    async def _recover_issued_in_session(
        self, session: AsyncSession, tenant_id: UUID, run_id: UUID, review_id: UUID
    ) -> ReviewIntent:
        _, pool, row = await self._locked(session, tenant_id, run_id, review_id)
        attempt = await self._budget._attempt(session, pool, UUID(str(row.attempt_id)))
        _check_attempt(row, attempt)
        if row.status not in ("prepared", "issued"):
            self._terminal_proof(row, attempt)
            return _snapshot(row, attempt)
        if row.status != "issued" or row.physical_identity is None:
            raise ReviewConflict(_CONFLICT)
        return await self._finish_in_session(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
            review_id=review_id,
            attempt_id=UUID(str(row.attempt_id)),
            request_sha256=row.request_sha256,
            physical_identity=UUID(str(row.physical_identity)),
            lease=_WorkerLease(attempt.worker_id, UUID(str(attempt.worker_lease_token))),
            actual_model_identity=_stored_deployment(attempt),
            outcome="unknown",
        )

    async def consume_permit(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        review_id: UUID,
        current_binding: _CurrentReviewBinding,
    ) -> bool:
        """Storage one-use consumption ONLY. No actual tool/OS dispatch here."""
        async with self._transaction() as session:
            return await self._consume_permit_in_session(
                session,
                tenant_id=tenant_id,
                run_id=run_id,
                review_id=review_id,
                current_binding=current_binding,
            )

    @_private_errors
    async def _consume_permit_in_session(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        review_id: UUID,
        current_binding: _CurrentReviewBinding,
    ) -> bool:
        _require_transaction(session)
        try:
            if type(current_binding) is not _CurrentReviewBinding:
                return False
            a = _validated_action(current_binding.snapshot)
            config_sha = review_config_sha256(current_binding.config)
            lease = current_binding.lease
            if type(lease) is not _WorkerLease:
                return False
            lease.__post_init__()
            run, pool, row = await self._locked(session, tenant_id, run_id, review_id)
            attempt = await self._budget._attempt(session, pool, UUID(str(row.attempt_id)))
            _check_attempt(row, attempt)
            # clock_timestamp is sampled after ALL locks and the current-link read.
            evidence, _, _, _ = await self._budget._fence(
                session, run, pool, UUID(str(row.authorization_turn_id)), lease, config_sha
            )
            if (
                row.status != "allow"
                or row.consumed
                or pool.closed
                or a.action_digest != row.action_digest
                or a.effect_digest != row.effect_digest
                or config_sha != row.config_sha256
                or authorization_binding_sha256(evidence) != row.authorization_sha256
                or (attempt.worker_id, attempt.worker_lease_token) != (lease.worker_id, lease.token)
            ):
                return False
            self._terminal_proof(row, attempt)
            event = await session.scalar(
                select(RunReviewEventRow).where(
                    RunReviewEventRow.review_id == row.review_id,
                    RunReviewEventRow.kind == "terminal",
                )
            )
            if event is None or event.evidence != {
                "status": "allow",
                "receipt_sha256": row.receipt_sha256,
                "risk": row.risk,
                "reason_code": row.reason_code,
            }:
                return False
        except (ReviewConflict, AdmissionConflict, ValueError):
            return False
        row.consumed = True
        await self._event(
            session,
            row,
            "consumed",
            {
                "action_digest": row.action_digest,
                "effect_digest": row.effect_digest,
                "receipt_sha256": row.receipt_sha256,
            },
        )
        return True

    @_private_errors
    async def get(self, tenant_id: UUID, run_id: UUID, review_id: UUID) -> ReviewIntent:
        # A coherent read also follows the mutation lock order; it confers no permission.
        async with self._transaction() as session:
            _, pool, row = await self._locked(session, tenant_id, run_id, review_id)
            attempt = await self._budget._attempt(session, pool, UUID(str(row.attempt_id)))
            if row.status not in ("prepared", "issued"):
                self._terminal_proof(row, attempt)
            return _snapshot(row, attempt)

    @_private_errors
    async def lookup_effect(
        self, tenant_id: UUID, run_id: UUID, authorization_turn_id: UUID, effect_digest: str
    ) -> ReviewIntent | None:
        """Durable denial/UNKNOWN lookup under original provenance; no live permit."""
        _uuid(authorization_turn_id)
        _digest(effect_digest)
        async with self._transaction() as session:
            _, pool = await self._budget._locked(session, tenant_id, run_id)
            if pool is None or pool.authorization_turn_id != authorization_turn_id:
                raise ReviewConflict(_CONFLICT)
            effect = await self._effect(session, tenant_id, authorization_turn_id, effect_digest)
            if effect is None:
                return None
            if effect.run_id != run_id:
                raise ReviewConflict(_CONFLICT)
            row = await self._intent(session, tenant_id, run_id, UUID(str(effect.review_id)))
            attempt = await self._budget._attempt(session, pool, UUID(str(row.attempt_id)))
            if row.status not in ("prepared", "issued"):
                self._terminal_proof(row, attempt)
            return _snapshot(row, attempt)

    @_private_errors
    async def breaker_open(
        self,
        tenant_id: UUID,
        run_id: UUID,
        authorization_turn_id: UUID,
        config: ApprovalReviewConfig,
    ) -> bool:
        _uuid(authorization_turn_id)
        sha = review_config_sha256(config)
        async with self._transaction() as session:
            _, pool = await self._budget._locked(session, tenant_id, run_id)
            if pool is None or (pool.authorization_turn_id, pool.config_sha256) != (
                authorization_turn_id,
                sha,
            ):
                raise ReviewConflict(_CONFLICT)
            return _breaker_open(
                await self._history(session, tenant_id, authorization_turn_id), config
            )
