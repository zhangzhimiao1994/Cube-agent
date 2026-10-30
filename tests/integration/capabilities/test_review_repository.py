"""Synthetic dedicated PG only; storage, never provider/tool execution."""

import asyncio
import json
import os
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.review_contracts import (
    ApprovalReviewConfig,
    ModelReviewVerdict,
    freeze_review_action,
)
from agent_hub.capabilities.review_repository import (
    ReviewConflict,
    ReviewRepository,
    _CurrentReviewBinding,
    _ReviewReservation,
    authorization_binding_sha256,
    review_config_sha256,
)
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunAdmissionPoolRow,
    RunApprovalRow,
    RunInputAuthorizationRow,
    RunReviewEffectRow,
    RunReviewEventRow,
    RunReviewIntentRow,
    RunRow,
)
from agent_hub.db.session import build_database
from agent_hub.models.run_admission_repository import (
    DeploymentIdentity,
    RunAdmissionRepository,
    UsageObservation,
    _WorkerLease,
)
from agent_hub.runs.authorization import (
    _capture_authenticated_input,
    _persist_authenticated_input,
)


@pytest.fixture(scope="session")
def database_url():
    value = os.environ.get("AGENT_HUB_TEST_DATABASE_URL")
    assert value, "explicit dedicated test database binding required"
    url = make_url(value)
    assert url.drivername == "postgresql+asyncpg"
    assert url.username == url.database == "agent_hub_test"
    assert (url.host in ("127.0.0.1", "localhost") and url.port is not None) or (
        url.host is None and str(url.query.get("host", "")).startswith("/run/")
    )
    return value


@dataclass
class Context:
    factory: object
    tenant: UUID
    run: UUID
    evidence: object
    config: ApprovalReviewConfig
    reservation: _ReviewReservation

    def snapshot(self, **changes):
        req_changes = changes.pop("request", {})
        req = CapabilityRequest(
            tenant_id=self.tenant,
            user_id=self.evidence.actor_id,
            run_id=self.run,
            agent_id="agent-a",
            capability="workspace",
            operation="write",
            resource="private-file",
            arguments={"text": "private-content"},
            idempotency_key="key-a",
        ).model_copy(update=req_changes)
        values = {
            "role": Role.OPERATOR,
            "authorization_turn_id": self.evidence.turn_id,
            "authorization_sha256": authorization_binding_sha256(self.evidence),
            "config_sha256": review_config_sha256(self.config),
            "policy_sha256": "c" * 64,
            "source_binding": {"root": "private-source"},
            "normalized_effect": {"file": "private-file"},
        }
        values.update(changes)
        return freeze_review_action(req, **values)

    def repo(self):
        return ReviewRepository(self.factory)

    def binding(self):
        return _CurrentReviewBinding(self.snapshot(), self.config, self.reservation.lease)

    async def begin(self, **changes):
        values = {
            "snapshot": self.snapshot(),
            "config": self.config,
            "reservation": self.reservation,
            "review_id": uuid4(),
        }
        values.update(changes)
        return await self.repo().begin_intent(**values)

    async def issue(self, intent, physical=None):
        return await self.repo().mark_issued(
            tenant_id=self.tenant,
            run_id=self.run,
            review_id=intent.review_id,
            physical_identity=physical or uuid4(),
            lease=self.reservation.lease,
        )

    def receipt(self, intent, outcome="allow", usage="known", **changes):
        verdict = (
            None
            if outcome not in ("allow", "deny", "needs_human")
            else ModelReviewVerdict(
                1,
                outcome,
                "low",
                "authorized_action" if outcome == "allow" else "outside_authorized_scope",
                "Private bounded rationale.",
                intent.action_digest,
            )
        )
        values = {
            "tenant_id": self.tenant,
            "run_id": self.run,
            "review_id": intent.review_id,
            "attempt_id": intent.attempt_id,
            "request_sha256": self.reservation.request_sha256,
            "physical_identity": intent.physical_identity,
            "lease": self.reservation.lease,
            "actual_model_identity": self.reservation.deployment,
            "outcome": outcome,
            "verdict": verdict,
            "usage": UsageObservation(2, 3, 5, Decimal("0.01")) if usage == "known" else usage,
        }
        values.update(changes)
        return values

    async def issued(self, **changes):
        intent = await self.begin(**changes)
        await self.issue(intent)
        return await self.repo().get(self.tenant, self.run, intent.review_id)

    async def allow(self):
        intent = await self.issued()
        return await self.repo().finish(**self.receipt(intent))


@pytest.fixture
async def ctx(database_url):
    db = build_database(database_url)
    tenant, run = uuid4(), uuid4()
    e = _capture_authenticated_input(
        AuthenticatedPrincipal(tenant_id=tenant, user_id=uuid4(), role=Role.OPERATOR),
        "private user message",
        {"project": "synthetic"},
    )
    c = ApprovalReviewConfig(
        engine="rules_then_model",
        reviewer_logical_model="reviewer",
        max_reviews_per_run=20,
        max_cost_usd_per_run="10.00",
    )
    lease = _WorkerLease("synthetic-worker", uuid4())
    async with db.session_factory() as session, session.begin():
        now = await session.scalar(select(func.clock_timestamp()))
        row = RunRow(
            id=run,
            tenant_id=tenant,
            actor_id=e.actor_id,
            actor_role=e.actor_role.value,
            request=e.message,
            status="running",
            mode="direct",
            worker_id=lease.worker_id,
            worker_lease_token=lease.token,
            worker_lease_expires_at=now + timedelta(minutes=10),
            routing_decision={
                "runtime_token_absolute_tokens": 1000,
                "runtime_plan_token_budget": 1000,
                "runtime_timeout_absolute_seconds": 600,
            },
        )
        session.add(row)
        await session.flush()
        await _persist_authenticated_input(session, row, e)
    await RunAdmissionRepository(db.session_factory).open_pool(
        tenant_id=tenant,
        run_id=run,
        authorization_turn_id=e.turn_id,
        config_sha256=review_config_sha256(c),
        lease=lease,
        original_token_grant=1000,
        original_deadline=now + timedelta(minutes=9),
        review_token_limit=1000,
        review_count_limit=20,
        review_cost_limit=Decimal(10),
    )
    context = Context(
        db.session_factory,
        tenant,
        run,
        e,
        c,
        _ReviewReservation(
            "e" * 64,
            DeploymentIdentity("synthetic", "dedicated-review", "review-model"),
            30,
            Decimal("0.2"),
            lease,
        ),
    )
    try:
        yield context
    finally:
        async with db.session_factory() as session, session.begin():
            # Original input FK is intentionally restrictive: remove pool before inputs.
            await session.execute(
                delete(RunAdmissionPoolRow).where(RunAdmissionPoolRow.run_id == run)
            )
            await session.execute(delete(RunRow).where(RunRow.id == run))
        await db.dispose()


async def counts(ctx):
    async with ctx.factory() as s:
        return [
            await s.scalar(select(func.count()).select_from(row).where(row.run_id == ctx.run))
            for row in (
                RunReviewIntentRow,
                RunReviewEffectRow,
                RunReviewEventRow,
                RunAdmissionAttemptRow,
                RunApprovalRow,
            )
        ]


async def test_begin_atomic_rollback_same_effect_and_ordinary_approval_untouched(ctx):
    with pytest.raises(RuntimeError, match="abort"):
        async with ctx.factory() as s, s.begin():
            await ctx.repo()._begin_intent_in_session(
                s,
                snapshot=ctx.snapshot(),
                config=ctx.config,
                reservation=ctx.reservation,
                review_id=uuid4(),
            )
            raise RuntimeError("abort")
    assert await counts(ctx) == [0, 0, 0, 0, 0]
    pool = await RunAdmissionRepository(ctx.factory).get_pool(ctx.tenant, ctx.run)
    assert (pool.token_charged, pool.review_count_charged) == (0, 0)
    a, b = await asyncio.gather(
        ctx.begin(),
        ctx.begin(
            snapshot=ctx.snapshot(request={"agent_id": "agent-b", "idempotency_key": "key-b"})
        ),
    )
    assert sorted([a.created, b.created]) == [False, True]
    assert a.review_id == b.review_id
    assert await counts(ctx) == [1, 1, 1, 1, 0]
    pool = await RunAdmissionRepository(ctx.factory).get_pool(ctx.tenant, ctx.run)
    assert (pool.token_charged, pool.review_count_charged) == (30, 1)


async def test_begin_plus_issue_and_finish_caller_rollback(ctx):
    review_id, physical = uuid4(), uuid4()
    with pytest.raises(RuntimeError, match="abort"):
        async with ctx.factory() as s, s.begin():
            intent = await ctx.repo()._begin_intent_in_session(
                s,
                snapshot=ctx.snapshot(),
                config=ctx.config,
                reservation=ctx.reservation,
                review_id=review_id,
            )
            assert await ctx.repo()._mark_issued_in_session(
                s,
                tenant_id=ctx.tenant,
                run_id=ctx.run,
                review_id=review_id,
                lease=ctx.reservation.lease,
                physical_identity=physical,
            )
            raise RuntimeError("abort")
    assert await counts(ctx) == [0, 0, 0, 0, 0]
    intent = await ctx.issued()
    with pytest.raises(RuntimeError, match="abort"):
        async with ctx.factory() as s, s.begin():
            await ctx.repo()._finish_in_session(s, **ctx.receipt(intent))
            raise RuntimeError("abort")
    assert (await ctx.repo().get(ctx.tenant, ctx.run, intent.review_id)).status == "issued"
    attempt = await RunAdmissionRepository(ctx.factory).get_attempt(
        ctx.tenant, ctx.run, intent.attempt_id
    )
    assert attempt.state == "issued"


async def test_duplicate_issue_exactly_once_identity_conflict(ctx):
    intent = await ctx.begin()
    physical = uuid4()
    values = await asyncio.gather(ctx.issue(intent, physical), ctx.issue(intent, physical))
    assert sorted(values) == [False, True]
    with pytest.raises(ReviewConflict, match="^review state conflict$"):
        await ctx.issue(intent, uuid4())
    assert await counts(ctx) == [1, 1, 2, 1, 0]


async def test_strict_allow_known_accounting_private_summary_and_one_use_race(ctx):
    intent = await ctx.allow()
    assert intent.status == "allow"
    public = intent.public_summary()
    assert set(public) == {
        "review_id",
        "run_id",
        "tenant_id",
        "attempt_id",
        "action_digest",
        "effect_digest",
        "authorization_sha256",
        "config_sha256",
        "policy_sha256",
        "source_sha256",
        "request_sha256",
        "model_identity",
        "physical_identity",
        "status",
        "risk",
        "reason_code",
        "consumed",
    }
    dumped = json.dumps(public) + repr(intent)
    for secret in [
        "private user message",
        "private-content",
        "private-source",
        "Private bounded rationale.",
        str(ctx.reservation.lease.token),
    ]:
        assert secret not in dumped
    pool = await RunAdmissionRepository(ctx.factory).get_pool(ctx.tenant, ctx.run)
    assert (pool.token_charged, pool.review_cost_charged) == (5, Decimal("0.01"))
    values = await asyncio.gather(
        *[
            ctx.repo().consume_permit(
                tenant_id=ctx.tenant,
                run_id=ctx.run,
                review_id=intent.review_id,
                current_binding=ctx.binding(),
            )
            for _ in range(2)
        ]
    )
    assert sorted(values) == [False, True]
    assert not await ctx.repo().consume_permit(
        tenant_id=ctx.tenant,
        run_id=ctx.run,
        review_id=intent.review_id,
        current_binding=ctx.binding(),
    )
    assert await counts(ctx) == [1, 1, 4, 1, 0]


@pytest.mark.parametrize(
    "outcome",
    ["allow", "deny", "needs_human", "timeout", "unavailable", "invalid_response", "unknown"],
)
async def test_unknown_billing_never_mints_allow_or_fabricates_deny(ctx, outcome):
    intent = await ctx.issued()
    result = await ctx.repo().finish(**ctx.receipt(intent, outcome=outcome, usage=None))
    assert result.status == ("unknown" if outcome == "allow" else outcome)
    assert not await ctx.repo().consume_permit(
        tenant_id=ctx.tenant,
        run_id=ctx.run,
        review_id=intent.review_id,
        current_binding=ctx.binding(),
    )
    pool = await RunAdmissionRepository(ctx.factory).get_pool(ctx.tenant, ctx.run)
    assert (pool.token_charged, pool.review_cost_charged) == (30, Decimal("0.2"))
    assert (await ctx.begin()).review_id == intent.review_id
    assert await counts(ctx) == [1, 1, 3, 1, 0]


async def test_unknown_cold_recovery_no_refill_reissue_or_late_allow(ctx):
    intent = await ctx.issued()
    result = await ctx.repo().recover_issued(ctx.tenant, ctx.run, intent.review_id)
    assert result.status == "unknown"
    assert (
        await ctx.repo().recover_issued(ctx.tenant, ctx.run, intent.review_id)
    ).status == "unknown"
    with pytest.raises(ReviewConflict):
        await ctx.repo().finish(**ctx.receipt(intent))
    assert not await ctx.issue(intent, intent.physical_identity)
    assert (await ctx.begin()).created is False
    pool = await RunAdmissionRepository(ctx.factory).get_pool(ctx.tenant, ctx.run)
    assert pool.token_charged == 30


async def test_terminal_exact_repeat_and_conflicting_replacement(ctx):
    intent = await ctx.issued()
    receipt = ctx.receipt(intent, outcome="deny")
    first = await ctx.repo().finish(**receipt)
    assert (await ctx.repo().finish(**receipt)) == first
    with pytest.raises(ReviewConflict):
        await ctx.repo().finish(**ctx.receipt(intent))
    assert await counts(ctx) == [1, 1, 3, 1, 0]
    assert (
        await ctx.repo().lookup_effect(
            ctx.tenant, ctx.run, ctx.evidence.turn_id, intent.effect_digest
        )
    ).status == "deny"


async def test_prepared_cancel_releases_hold_preserves_effect_and_history(ctx):
    intent = await ctx.begin()
    result = await ctx.repo().finish(**ctx.receipt(intent, outcome="cancelled", usage=None))
    assert result.status == "cancelled"
    assert (await ctx.begin()).created is False
    pool = await RunAdmissionRepository(ctx.factory).get_pool(ctx.tenant, ctx.run)
    assert (pool.token_charged, pool.review_count_charged) == (0, 0)
    with pytest.raises(ReviewConflict):
        await ctx.issue(intent)
    assert await counts(ctx) == [1, 1, 2, 1, 0]


@pytest.mark.parametrize(
    "change",
    [
        "role",
        "turn",
        "lease",
        "expired_lease",
        "deadline",
        "closed",
        "cancelled",
        "config",
        "policy",
        "source",
        "effect",
        "action",
        "binding_lease",
    ],
)
async def test_changed_current_security_binding_cannot_consume(ctx, change):
    intent = await ctx.allow()
    binding = ctx.binding()
    async with ctx.factory() as s, s.begin():
        row = await s.get(RunRow, ctx.run)
        pool = await s.get(RunAdmissionPoolRow, ctx.run)
        now = await s.scalar(select(func.clock_timestamp()))
        if change == "role":
            row.actor_role = "admin"
        if change == "turn":
            new = _capture_authenticated_input(
                AuthenticatedPrincipal(
                    tenant_id=ctx.tenant, user_id=ctx.evidence.actor_id, role=Role.OPERATOR
                ),
                "new input",
                {"project": "new"},
            )
            await _persist_authenticated_input(s, row, new, submitted_message="new input")
        if change == "lease":
            row.worker_lease_token = uuid4()
        if change == "expired_lease":
            row.worker_lease_expires_at = now - timedelta(seconds=1)
        if change == "deadline":
            pool.original_deadline = now - timedelta(seconds=1)
        if change == "closed":
            pool.closed = True
        if change == "cancelled":
            row.status = "cancelled"
    if change == "config":
        binding = replace(binding, config=replace(ctx.config, timeout_seconds=20))
    if change == "policy":
        binding = replace(binding, snapshot=ctx.snapshot(policy_sha256="f" * 64))
    if change == "source":
        binding = replace(binding, snapshot=ctx.snapshot(source_binding={"new": 1}))
    if change == "effect":
        binding = replace(binding, snapshot=ctx.snapshot(normalized_effect={"new": 1}))
    if change == "action":
        binding = replace(binding, snapshot=ctx.snapshot(request={"idempotency_key": "new"}))
    if change == "binding_lease":
        binding = replace(binding, lease=_WorkerLease("synthetic-worker", uuid4()))
    assert not await ctx.repo().consume_permit(
        tenant_id=ctx.tenant, run_id=ctx.run, review_id=intent.review_id, current_binding=binding
    )
    assert not (await ctx.repo().get(ctx.tenant, ctx.run, intent.review_id)).consumed


@pytest.mark.parametrize(
    "change",
    ["action_digest", "high_allow", "model", "request", "attempt", "physical", "missing_verdict"],
)
async def test_forged_terminal_identity_or_verdict_fails_without_settlement(ctx, change):
    intent = await ctx.issued()
    receipt = ctx.receipt(intent)
    if change in ("action_digest", "high_allow"):
        v = receipt["verdict"]
        object.__setattr__(
            v,
            "action_digest" if change == "action_digest" else "risk",
            "f" * 64 if change == "action_digest" else "high",
        )
    if change == "model":
        receipt["actual_model_identity"] = DeploymentIdentity("synthetic", "other", "model")
    if change == "request":
        receipt["request_sha256"] = "f" * 64
    if change == "attempt":
        receipt["attempt_id"] = uuid4()
    if change == "physical":
        receipt["physical_identity"] = uuid4()
    if change == "missing_verdict":
        receipt["verdict"] = None
    with pytest.raises((ValueError, ReviewConflict)):
        await ctx.repo().finish(**receipt)
    assert (await ctx.repo().get(ctx.tenant, ctx.run, intent.review_id)).status == "issued"
    assert (
        await RunAdmissionRepository(ctx.factory).get_pool(ctx.tenant, ctx.run)
    ).token_charged == 30


@pytest.mark.parametrize(
    "change",
    [
        "cross_run",
        "cross_tenant",
        "user",
        "role",
        "authorization",
        "config",
        "no_current",
        "no_pool",
    ],
)
async def test_missing_or_cross_provenance_begin_fails_before_paid_reservation(ctx, change):
    a = ctx.snapshot()
    if change == "cross_run":
        a = ctx.snapshot(request={"run_id": uuid4()})
    if change == "cross_tenant":
        a = ctx.snapshot(request={"tenant_id": uuid4()})
    if change == "user":
        a = ctx.snapshot(request={"user_id": uuid4()})
    if change == "role":
        a = ctx.snapshot(role=Role.ADMIN)
    if change == "authorization":
        a = ctx.snapshot(authorization_sha256="f" * 64)
    if change == "config":
        a = ctx.snapshot(config_sha256="f" * 64)
    async with ctx.factory() as s, s.begin():
        if change == "no_current":
            await s.execute(
                delete(RunInputAuthorizationRow).where(RunInputAuthorizationRow.run_id == ctx.run)
            )
        if change == "no_pool":
            await s.execute(
                delete(RunAdmissionPoolRow).where(RunAdmissionPoolRow.run_id == ctx.run)
            )
    with pytest.raises((ReviewConflict, ValueError)):
        await ctx.begin(snapshot=a)
    assert await counts(ctx) == [0, 0, 0, 0, 0]


async def test_breaker_shared_instances_neutral_errors_and_same_effect_no_extra_count(ctx):
    outcomes = ["deny", "timeout", "deny", "invalid_response", "deny"]
    for i, outcome in enumerate(outcomes):
        intent = await ctx.issued(snapshot=ctx.snapshot(normalized_effect={"target": i}))
        await ctx.repo().finish(**ctx.receipt(intent, outcome=outcome, usage=None))
    assert await ctx.repo().breaker_open(ctx.tenant, ctx.run, ctx.evidence.turn_id, ctx.config)
    same = await ctx.begin(
        snapshot=ctx.snapshot(
            normalized_effect={"target": 4},
            request={"agent_id": "another", "idempotency_key": "another"},
        )
    )
    assert (same.created, same.status) == (False, "deny")
    with pytest.raises(ReviewConflict):
        await ctx.begin(snapshot=ctx.snapshot(normalized_effect={"target": "new"}))
    assert await counts(ctx) == [5, 5, 15, 5, 0]


@pytest.mark.parametrize(
    "usage",
    [UsageObservation(20, 20, 40, Decimal("0.01")), UsageObservation(2, 3, 5, Decimal("0.3"))],
)
async def test_overbound_allow_closes_pool_and_cannot_consume(ctx, usage):
    intent = await ctx.issued()
    result = await ctx.repo().finish(**ctx.receipt(intent, usage=usage))
    assert result.status == "unknown"
    assert not await ctx.repo().consume_permit(
        tenant_id=ctx.tenant,
        run_id=ctx.run,
        review_id=intent.review_id,
        current_binding=ctx.binding(),
    )
    pool = await RunAdmissionRepository(ctx.factory).get_pool(ctx.tenant, ctx.run)
    assert pool.closed and pool.token_charged == 30


async def test_populated_downgrade_refuses_and_model_migration_parity(ctx):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    from agent_hub.db.base import Base

    path = Path(__file__).resolve().parents[3] / "alembic/versions/0036_review_ledger.py"
    spec = importlib.util.spec_from_file_location("review_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    await ctx.begin()
    async with ctx.factory() as s:
        conn = await s.connection()

        def verify(sync):
            inspector = inspect(sync)
            for name in (
                "agent_hub_review_intents",
                "agent_hub_review_effects",
                "agent_hub_review_events",
            ):
                actual = {
                    c["name"]: (str(c["type"]), c["nullable"]) for c in inspector.get_columns(name)
                }
                expected = {
                    c.name: (str(c.type), c.nullable) for c in Base.metadata.tables[name].columns
                }
                assert actual == expected

        await conn.run_sync(verify)
        with pytest.raises(Exception, match="Cannot downgrade: review evidence exists"):
            await conn.run_sync(
                lambda sync: _downgrade(mod, Operations(MigrationContext.configure(sync)))
            )
        await s.rollback()
    assert await counts(ctx) == [1, 1, 1, 1, 0]


def _downgrade(mod, operations):
    # Exercise actual migration, not a copied guard SQL.
    previous = mod.op
    try:
        mod.op = operations
        mod.downgrade()
    finally:
        mod.op = previous


@pytest.mark.parametrize("outcome", ["unknown", "timeout", "unavailable", "invalid_response"])
async def test_error_usage_observation_never_refunds_unproven_issue(ctx, outcome):
    intent = await ctx.issued()
    result = await ctx.repo().finish(**ctx.receipt(intent, outcome=outcome))
    assert result.status == outcome
    pool = await RunAdmissionRepository(ctx.factory).get_pool(ctx.tenant, ctx.run)
    assert (pool.token_charged, pool.review_cost_charged) == (30, Decimal("0.2"))
