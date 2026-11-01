"""NEW real PG + actual Gateway/guarded MockTransport composition only."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import AuthenticatedPrincipal
from agent_hub.capabilities.model_review import ModelApprovalReviewService, _LiveReviewContext
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.capabilities.review_repository import (
    ReviewRepository,
    _CurrentReviewBinding,
    review_config_sha256,
)
from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunAdmissionPoolRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunReviewIntentRow,
    RunRow,
)
from agent_hub.models.admission_bounds import TrustedContextBound
from agent_hub.models.physical_budget import _bind_run_budget, _RunBudgetContext
from agent_hub.models.run_admission_repository import RunAdmissionRepository
from agent_hub.models.types import Deployment, ModelCapability
from agent_hub.runs.authorization import _capture_authenticated_input, _persist_authenticated_input
from tests.integration.capabilities.test_review_repository import (
    database_url as database_url,  # noqa: PLC0414 - imported pytest fixture
)
from tests.integration.models.run_admission_typed_fixture import Storage
from tests.integration.models.test_run_admission import prepare
from tests.integration.models.test_run_admission import (
    storage as storage,  # noqa: PLC0414 - imported pytest fixture
)
from tests.integration.runtime.dynamic_physical_typed_fixture import mapping, present
from tests.unit.capabilities.test_model_review import snapshot, verdict
from tests.unit.models.test_physical_budget import gateway, logical_request, response

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@dataclass
class Composition:
    factory: async_sessionmaker[AsyncSession]
    context: _RunBudgetContext
    primary: Deployment
    reviewer: Deployment


@pytest_asyncio.fixture(loop_scope="module")
async def composition(storage: Storage) -> AsyncIterator[Composition]:
    factory = storage[0]
    config = ApprovalReviewConfig(
        engine="rules_then_model",
        reviewer_logical_model="reviewer",
        max_input_tokens=20000,
        max_output_tokens=512,
        max_cost_usd_per_run="10.00",
    )
    budget, binding, opening = await prepare(
        storage,
        open_now=False,
        route={
            "runtime_token_absolute_tokens": 15000,
            "runtime_plan_token_budget": 15000,
            "runtime_timeout_absolute_seconds": 3600,
        },
        original_token_grant=15000,
        review_token_limit=15000,
        review_count_limit=8,
        review_cost_limit=Decimal(10),
    )
    binding["config_sha256"] = opening["config_sha256"] = review_config_sha256(config)
    pool = await budget.open_pool(**opening)
    from agent_hub.runs.authorization import _validated_persisted_input

    async with factory() as session:
        row = await session.get(RunRow, binding["run_id"])
        link = await session.get(RunInputAuthorizationRow, binding["run_id"])
        turn = await session.get(RunAuthenticatedInputRow, binding["authorization_turn_id"])
        evidence = _validated_persisted_input(present(row), link, turn)
    assert evidence is not None
    primary = Deployment(
        id="selected",
        logical_model="primary",
        provider_model="openai/acting",
        quota_scope_id="scope-selected",
        context_window_tokens=10000,
        max_output_tokens=2000,
        input_per_million_usd=Decimal(1),
        output_per_million_usd=Decimal(2),
    )
    reviewer = replace(
        primary,
        id="review-selected",
        logical_model="reviewer",
        quota_scope_id="scope-review-selected",
        provider_model="openai/reviewer",
        capabilities=frozenset({ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT}),
    )
    context = _RunBudgetContext(
        repository=budget,
        pool=pool,
        evidence=evidence,
        lease=binding["lease"],
        config=config,
        deadline=time.monotonic() + 220,
        trusted_contexts=tuple(
            TrustedContextBound(d.id, d.logical_model, d.provider_model, 10000)
            for d in (primary, reviewer)
        ),
    )
    try:
        yield Composition(factory, context, primary, reviewer)
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                delete(RunAdmissionPoolRow).where(RunAdmissionPoolRow.run_id == binding["run_id"])
            )


async def test_real_primary_review_shared_pool_and_cold_repeated_effect(composition: Composition) -> None:
    c = composition
    action = snapshot(c.context)
    wires: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wires.append(req)
        return httpx.Response(200, json=response(verdict(action)))

    gw, _ = gateway([c.primary, c.reviewer], handle)
    service = ModelApprovalReviewService(gw, ReviewRepository(c.factory))
    live = _LiveReviewContext(c.context, materials_trusted=True, contains_secrets=False)
    with _bind_run_budget(c.context):
        await gw.complete_with_context(logical_request())
        result = await service.review(action, live)
        repeated = await ModelApprovalReviewService(gw, ReviewRepository(c.factory)).review(
            action, live
        )
    assert result.status == "allow" and repeated.status == "needs_human" and len(wires) == 2
    pool = await RunAdmissionRepository(c.factory).get_pool(c.context.tenant_id, c.context.run_id)
    assert pool is not None
    assert pool.tokens_remaining == 14990 and pool.review_count_charged == 1
    async with c.factory() as session:
        rows = (
            await session.scalars(
                select(RunAdmissionAttemptRow).where(
                    RunAdmissionAttemptRow.run_id == c.context.run_id
                )
            )
        ).all()
    assert {row.purpose for row in rows} == {"primary", "review"}
    assert {row.state for row in rows} == {"completed"}
    assert len({row.physical_transport_id for row in rows}) == 2
    repository = ReviewRepository(c.factory)
    async with c.factory() as session:
        row = await session.get(RunReviewIntentRow, present(result.review_id))
        assert row is not None
        attempt = present(await session.get(RunAdmissionAttemptRow, row.attempt_id))
        receipt = json.loads(present(row.private_receipt))
        assert receipt["accounting_receipt_sha256"] == attempt.receipt_sha256
        assert receipt["evidence"]["usage"] == mapping(present(attempt.receipt))["usage"]
    current = _CurrentReviewBinding(action, c.context.config, c.context.lease)
    assert await repository.consume_permit(
        tenant_id=c.context.tenant_id,
        run_id=c.context.run_id,
        review_id=present(result.review_id),
        current_binding=current,
    )
    assert not await repository.consume_permit(
        tenant_id=c.context.tenant_id,
        run_id=c.context.run_id,
        review_id=present(result.review_id),
        current_binding=current,
    )


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("allow", "allow"),
        ("deny", "deny"),
        ("needs_human", "needs_human"),
        ("missing", "unknown"),
        ("malformed", "unknown"),
        ("deny_missing", "deny"),
        ("overbound", "unknown"),
        ("protocol", "unknown"),
    ],
)
async def test_real_review_receipt_accounting_and_cold_state(composition: Composition, kind: str, expected: str) -> None:
    c = composition
    action = snapshot(c.context)
    wires: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wires.append(req)
        if kind == "protocol":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Unsupported parameter: max_completion_tokens",
                        "type": "invalid_request_error",
                    }
                },
            )
        decision = (
            kind
            if kind in ("allow", "deny", "needs_human")
            else "deny"
            if kind == "deny_missing"
            else "allow"
        )
        body = response(verdict(action, decision), kind not in ("missing", "deny_missing"))
        if kind == "malformed":
            usage = body["usage"]
            assert isinstance(usage, dict)
            usage["total_tokens"] = 99
        if kind == "overbound":
            body["usage"] = {"prompt_tokens": 10001, "completion_tokens": 3, "total_tokens": 10004}
        return httpx.Response(200, json=body)

    gw, _ = gateway([c.primary, c.reviewer], handle)
    with _bind_run_budget(c.context):
        result = await ModelApprovalReviewService(gw, ReviewRepository(c.factory)).review(
            action, _LiveReviewContext(c.context, materials_trusted=True, contains_secrets=False)
        )
        if kind == "protocol":
            repeated = await ModelApprovalReviewService(gw, ReviewRepository(c.factory)).review(
                action,
                _LiveReviewContext(c.context, materials_trusted=True, contains_secrets=False),
            )
            assert repeated.status == "unknown" and len(wires) == 1
    assert result.status == expected and len(wires) == 1
    cold = await ReviewRepository(c.factory).get(
        c.context.tenant_id, c.context.run_id, present(result.review_id)
    )
    assert cold is not None
    assert cold.status == expected
    budget = RunAdmissionRepository(c.factory)
    pool = await budget.get_pool(c.context.tenant_id, c.context.run_id)
    assert pool is not None
    attempt = await budget.get_attempt(c.context.tenant_id, c.context.run_id, cold.attempt_id)
    assert attempt is not None
    assert pool.review_count_charged == 1
    assert pool.token_charged == (5 if kind in ("allow", "deny", "needs_human") else 10000)
    assert pool.closed == (kind == "overbound")
    if kind == "overbound":
        assert attempt.state == "invalid" and b"10004" in present(attempt.receipt_bytes)


async def test_primary_unknown_debit_leaves_insufficient_original_balance_for_review(composition: Composition) -> None:
    c = composition
    wires: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wires.append(req)
        return httpx.Response(200, json=response(usage=False))

    gw, _ = gateway([c.primary, c.reviewer], handle)
    with _bind_run_budget(c.context):
        await gw.complete_with_context(logical_request())
        result = await ModelApprovalReviewService(gw, ReviewRepository(c.factory)).review(
            snapshot(c.context),
            _LiveReviewContext(c.context, materials_trusted=True, contains_secrets=False),
        )
    assert result.status == "needs_human" and len(wires) == 1
    pool = await RunAdmissionRepository(c.factory).get_pool(c.context.tenant_id, c.context.run_id)
    assert pool is not None
    assert pool.tokens_remaining == 5000 and pool.review_count_charged == 0


async def test_cancelled_review_cold_unknown_cannot_pay_again(composition: Composition) -> None:
    c = composition
    wires: list[httpx.Request] = []
    entered = asyncio.Event()

    async def handle(req: httpx.Request) -> httpx.Response:
        wires.append(req)
        entered.set()
        return await asyncio.Future[httpx.Response]()

    gw, _ = gateway([c.primary, c.reviewer], handle)
    action = snapshot(c.context)
    live = _LiveReviewContext(c.context, materials_trusted=True, contains_secrets=False)
    with _bind_run_budget(c.context):
        task = asyncio.create_task(
            ModelApprovalReviewService(gw, ReviewRepository(c.factory)).review(action, live)
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = await ModelApprovalReviewService(gw, ReviewRepository(c.factory)).review(
            action, live
        )
    assert result.status == "unknown" and len(wires) == 1
    pool = await RunAdmissionRepository(c.factory).get_pool(c.context.tenant_id, c.context.run_id)
    assert pool is not None
    assert pool.token_charged == 10000 and pool.review_count_charged == 1


async def test_concurrent_primary_hold_blocks_review_before_extra_network(composition: Composition) -> None:
    c = composition
    entered, release = asyncio.Event(), asyncio.Event()
    wires: list[httpx.Request] = []

    async def handle(req: httpx.Request) -> httpx.Response:
        wires.append(req)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=response())

    gw, _ = gateway([c.primary, c.reviewer], handle)
    with _bind_run_budget(c.context):
        primary = asyncio.create_task(gw.complete_with_context(logical_request()))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            result = await ModelApprovalReviewService(gw, ReviewRepository(c.factory)).review(
                snapshot(c.context),
                _LiveReviewContext(c.context, materials_trusted=True, contains_secrets=False),
            )
            assert result.status == "needs_human" and len(wires) == 1
            pool = await RunAdmissionRepository(c.factory).get_pool(
                c.context.tenant_id, c.context.run_id
            )
            assert pool is not None
            assert pool.token_charged == 10000 and pool.tokens_remaining == 5000
        finally:
            release.set()
            await primary
    pool = await RunAdmissionRepository(c.factory).get_pool(c.context.tenant_id, c.context.run_id)
    assert pool is not None
    assert pool.token_charged == 5 and pool.review_count_charged == 0


@pytest.mark.parametrize(
    "invalid", ["lease_expired", "lease_replaced", "run_cancelled", "turn_replaced"]
)
async def test_post_issue_stale_fence_refuses_new_allow_after_known_accounting(
    composition: Composition, invalid: str
) -> None:
    """Commit real state changes while actual guarded HTTP is already issued."""
    c = composition
    action = snapshot(c.context)
    wires: list[httpx.Request] = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def handle(req: httpx.Request) -> httpx.Response:
        wires.append(req)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=response(verdict(action)))

    gw, _ = gateway([c.primary, c.reviewer], handle)
    live = _LiveReviewContext(c.context, materials_trusted=True, contains_secrets=False)
    with _bind_run_budget(c.context):
        task = asyncio.create_task(
            ModelApprovalReviewService(gw, ReviewRepository(c.factory)).review(action, live)
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            async with c.factory() as session, session.begin():
                row = await session.scalar(
                    select(RunRow).where(RunRow.id == c.context.run_id).with_for_update()
                )
                issued = (
                    await session.scalars(
                        select(RunAdmissionAttemptRow).where(
                            RunAdmissionAttemptRow.run_id == c.context.run_id
                        )
                    )
                ).one()
                assert issued.state == "issued" and issued.physical_transport_id is not None
                assert row is not None
                now = await session.scalar(select(func.clock_timestamp()))
                assert isinstance(now, datetime)
                if invalid == "lease_expired":
                    row.worker_lease_expires_at = now - timedelta(seconds=1)
                elif invalid == "lease_replaced":
                    row.worker_lease_token = uuid4()
                elif invalid == "run_cancelled":
                    row.status = "cancelled"
                else:
                    original = c.context.evidence
                    replacement = _capture_authenticated_input(
                        AuthenticatedPrincipal(
                            tenant_id=c.context.tenant_id,
                            user_id=original.actor_id,
                            role=original.actor_role,
                        ),
                        "A new authenticated instruction supersedes the original turn.",
                        json.loads(original.input_scope),
                    )
                    await _persist_authenticated_input(
                        session, row, replacement, submitted_message=replacement.message
                    )
            # Local continuity deliberately remains active: only the persisted
            # fence can reject this late result; heartbeat notifications lag.
            assert c.context._active
            c.context.check()
            release.set()
            result = await asyncio.wait_for(task, timeout=10)
            assert result.status == "needs_human" and result.reason_code == "review_unavailable"
            assert len(wires) == 1

            async with c.factory() as session:
                intent = (
                    await session.scalars(
                        select(RunReviewIntentRow).where(
                            RunReviewIntentRow.run_id == c.context.run_id
                        )
                    )
                ).one()
                attempt = present(await session.get(RunAdmissionAttemptRow, intent.attempt_id))
                # Late accounting remains committed; no stale fence is put
                # inside finish, and its known usage/receipt is not rolled back.
                assert intent.status == "allow" and attempt.state == "completed"
                receipt = json.loads(present(intent.private_receipt))
                assert receipt["accounting_receipt_sha256"] == attempt.receipt_sha256
                assert receipt["evidence"]["usage"] == mapping(present(attempt.receipt))["usage"]
                assert mapping(present(attempt.receipt))["usage"] == {
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "total_tokens": 5,
                    "cost_usd": {"decimal": "0.000008"},
                }
                original_receipts = (intent.receipt_sha256, attempt.receipt_sha256)

            repeated = await ModelApprovalReviewService(gw, ReviewRepository(c.factory)).review(
                action, live
            )
            assert repeated.status == "needs_human" and len(wires) == 1
            cold = RunAdmissionRepository(c.factory)
            pool = await cold.get_pool(c.context.tenant_id, c.context.run_id)
            assert pool is not None
            assert pool.token_charged == 5 and pool.review_count_charged == 1
            assert pool.original_deadline == c.context._pool.original_deadline
            async with c.factory() as session:
                intents = (
                    await session.scalars(
                        select(RunReviewIntentRow).where(
                            RunReviewIntentRow.run_id == c.context.run_id
                        )
                    )
                ).all()
                attempts = (
                    await session.scalars(
                        select(RunAdmissionAttemptRow).where(
                            RunAdmissionAttemptRow.run_id == c.context.run_id
                        )
                    )
                ).all()
                assert len(intents) == len(attempts) == 1
                assert (intents[0].receipt_sha256, attempts[0].receipt_sha256) == original_receipts
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
