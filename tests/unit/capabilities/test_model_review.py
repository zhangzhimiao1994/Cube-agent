"""Tool-free service against the actual Gateway and guarded SDK MockTransport."""

import asyncio
import importlib
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.review_contracts import freeze_review_action
from agent_hub.capabilities.review_repository import (
    ReviewIntent,
    ReviewRepository,
    authorization_binding_sha256,
    review_config_sha256,
)
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.models.run_admission_repository import DeploymentIdentity
from tests.unit.models.test_physical_budget import (
    budget_module,
    configured,
    gateway,
    invalidate_current,
    logical_request,
    response,
)


def service_module():
    return importlib.import_module("agent_hub.capabilities.model_review")


def snapshot(context, **changes):
    values = {
        "role": Role.OPERATOR,
        "authorization_turn_id": context.evidence.turn_id,
        "authorization_sha256": authorization_binding_sha256(context.evidence),
        "policy_sha256": "c" * 64,
        "config_sha256": review_config_sha256(context.config),
        "source_binding": {"root": "synthetic", "authorization_complete": True},
        "normalized_effect": {"write": "file.txt", "text": "complete exact content"},
    }
    values.update(changes)
    return freeze_review_action(
        CapabilityRequest(
            tenant_id=context.tenant_id,
            user_id=context.evidence.actor_id,
            run_id=context.run_id,
            agent_id="test-agent",
            capability="workspace",
            operation="write",
            resource="file.txt",
            arguments={"text": "complete exact content"},
            idempotency_key="key",
        ),
        **values,
    )


class ReviewStore(ReviewRepository):
    """Service sequencing spy. Real ledger transactions are covered by PG composition."""

    def __init__(self, budget):
        self.budget = budget
        self.intent = None
        self.finishes = []
        self.begins = 0

    async def begin_intent(self, *, snapshot, config, reservation, review_id):
        self.begins += 1
        if self.intent is not None:
            return replace(self.intent, created=False)
        req, p = snapshot.request(), snapshot.to_payload()
        a = await self.budget.reserve(
            tenant_id=req.tenant_id,
            run_id=req.run_id,
            authorization_turn_id=p["authorization_turn_id"],
            purpose="review",
            attempt_id=uuid4(),
            request_sha256=reservation.request_sha256,
            deployment=reservation.deployment,
            token_hold=reservation.token_hold,
            review_cost_hold=reservation.cost_hold,
        )
        self.intent = ReviewIntent(
            review_id,
            req.tenant_id,
            req.run_id,
            a.attempt_id,
            snapshot.action_digest,
            snapshot.effect_digest,
            p["authorization_sha256"],
            p["config_sha256"],
            p["policy_sha256"],
            "d" * 64,
            reservation.request_sha256,
            reservation.deployment,
            None,
            "prepared",
            None,
            None,
            False,
            True,
        )
        return self.intent

    async def mark_issued(self, **a):
        ok = await self.budget.mark_issued(
            attempt_id=self.intent.attempt_id, physical_transport_id=a["physical_identity"]
        )
        self.intent = replace(
            self.intent, status="issued", physical_identity=a["physical_identity"]
        )
        return ok

    async def finish(self, **a):
        self.finishes.append(a)
        v, usage = a.get("verdict"), a.get("usage")
        settled = await self.budget.finish(
            attempt_id=a["attempt_id"],
            usage=usage,
            outcome="completed" if a["outcome"] in ("allow", "deny", "needs_human") else "unknown",
        )
        status = (
            "unknown" if a["outcome"] == "allow" and settled.state != "completed" else a["outcome"]
        )
        self.intent = replace(
            self.intent,
            status=status,
            risk=v.risk if v else "unknown",
            reason_code=v.reason_code if v else "review_unknown",
            created=False,
        )
        return self.intent


def verdict(action, decision="allow"):
    return json.dumps(
        {
            "schema_version": 1,
            "decision": decision,
            "risk": "low",
            "reason_code": "authorized_action"
            if decision == "allow"
            else "outside_authorized_scope",
            "rationale": "Private explanation.",
            "action_digest": action.action_digest,
        }
    )


def test_review_schema_is_exact_six_keys_and_strict():
    m = service_module()
    schema = m._review_schema().schema
    assert schema["additionalProperties"] is False
    assert (
        set(schema["required"])
        == set(schema["properties"])
        == {"schema_version", "decision", "risk", "reason_code", "rationale", "action_digest"}
    )


@pytest.mark.parametrize("decision", ["allow", "deny", "needs_human"])
async def test_strict_review_is_tool_free_and_finished_before_return(decision):
    b, m = budget_module(), service_module()
    context, budget, primary, reviewer = configured()
    action = snapshot(context)
    store, wires = ReviewStore(budget), []

    def handle(req):
        wires.append(json.loads(req.content))
        assert store.intent.status == "issued"
        return httpx.Response(200, json=response(verdict(action, decision)))

    gw, _ = gateway([primary, reviewer], handle)
    service = m.ModelApprovalReviewService(gw, store)
    live = m._LiveReviewContext(context, materials_trusted=True, contains_secrets=False)
    with b._bind_run_budget(context):
        m._request(action, live)
        result = await service.review(action, live)
    assert result.status == decision
    assert len(wires) == len(store.finishes) == 1
    assert budget.charged == 5
    assert not wires[0].get("tools") and "tool_choice" not in wires[0]
    assert len(wires[0]["messages"]) == 2
    text = wires[0]["messages"][1]["content"]
    assert context.evidence.message in text and "complete exact content" in text
    assert wires[0]["response_format"]["json_schema"]["strict"] is True
    public = json.dumps(result.public_summary())
    assert "Private explanation" not in public and context.evidence.message not in public
    assert str(context.lease.token) not in public


@pytest.mark.parametrize("case", ["secret", "untrusted", "oversize", "stale", "foreign_context"])
async def test_unreviewable_input_has_zero_review_io(case):
    b, m = budget_module(), service_module()
    context, budget, primary, reviewer = configured()
    action = snapshot(context)
    store = ReviewStore(budget)
    if case == "oversize":
        context, budget, primary, reviewer = configured(
            config=replace(context.config, max_input_tokens=1)
        )
        action, store = snapshot(context), ReviewStore(budget)
    if case == "stale":
        action = snapshot(context, config_sha256="0" * 64)
    gw, _ = gateway([primary, reviewer], lambda r: pytest.fail("invalid materials sent"))
    live = m._LiveReviewContext(
        context, materials_trusted=case != "untrusted", contains_secrets=case == "secret"
    )
    with b._bind_run_budget(context):
        result = await m.ModelApprovalReviewService(gw, store).review(
            action, {} if case == "foreign_context" else live
        )
    assert result.status == "needs_human"
    assert store.begins == 0 and budget.calls == []


@pytest.mark.parametrize("mode", ["missing", "malformed", "error", "protocol", "invalid_json"])
async def test_unknown_receipts_never_allow_refund_or_second_protocol_rpc(mode):
    b, m = budget_module(), service_module()
    context, budget, primary, reviewer = configured()
    action, store, wires = snapshot(context), ReviewStore(budget), []

    def handle(req):
        wires.append(req)
        if mode in ("error", "protocol"):
            return httpx.Response(
                503 if mode == "error" else 400,
                json={
                    "error": {
                        "message": "Unsupported parameter: max_completion_tokens",
                        "type": "invalid_request_error",
                    }
                },
            )
        body = response(
            "bad json" if mode == "invalid_json" else verdict(action), mode != "missing"
        )
        if mode == "malformed":
            body["usage"]["total_tokens"] = 99
        return httpx.Response(200, json=body)

    gw, _ = gateway([primary, reviewer], handle)
    with b._bind_run_budget(context):
        result = await m.ModelApprovalReviewService(gw, store).review(
            action, m._LiveReviewContext(context, materials_trusted=True, contains_secrets=False)
        )
    assert result.status != "allow" and len(wires) == 1
    assert budget.charged == 10000 and len(store.finishes) == 1


async def test_primary_and_review_share_balance_and_repeated_effect_never_sends():
    b, m = budget_module(), service_module()
    context, budget, primary, reviewer = configured()
    action, store, wires = snapshot(context), ReviewStore(budget), []

    def handle(req):
        wires.append(req)
        return httpx.Response(200, json=response(verdict(action)))

    gw, _ = gateway([primary, reviewer], handle)
    service = m.ModelApprovalReviewService(gw, store)
    live = m._LiveReviewContext(context, materials_trusted=True, contains_secrets=False)
    with b._bind_run_budget(context):
        await gw.complete_with_context(logical_request())
        await service.review(action, live)
        await service.review(action, live)
    assert len(wires) == 2 and budget.charged == 10
    assert store.begins == 2


async def test_distinct_compares_selected_actual_provider_model_not_alias_or_key():
    b, m = budget_module(), service_module()
    acting = DeploymentIdentity("openai", "another-key", "openai/reviewer")
    context, budget, primary, reviewer = configured(acting=acting)
    config = replace(context.config, require_distinct_model=True)
    # Construct a correctly matching context/config pair; test setup does not mint live authority.
    context, budget, primary, reviewer = configured(config=config, acting=acting)
    store, action = ReviewStore(budget), snapshot(context)
    gw, _ = gateway([primary, reviewer], lambda r: pytest.fail("same underlying model sent"))
    with b._bind_run_budget(context):
        result = await m.ModelApprovalReviewService(gw, store).review(
            action, m._LiveReviewContext(context, materials_trusted=True, contains_secrets=False)
        )
    assert result.status == "needs_human" and store.begins == 0


async def test_cancellation_is_propagated_and_issued_intent_is_unknown():
    b, m = budget_module(), service_module()
    context, budget, primary, reviewer = configured()
    action, store, entered = snapshot(context), ReviewStore(budget), asyncio.Event()

    async def handle(req):
        entered.set()
        await asyncio.Future()

    gw, _ = gateway([primary, reviewer], handle)
    with b._bind_run_budget(context):
        task = asyncio.create_task(
            m.ModelApprovalReviewService(gw, store).review(
                action,
                m._LiveReviewContext(context, materials_trusted=True, contains_secrets=False),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert store.intent.status == "unknown" and budget.charged == 10000


async def test_original_deadline_timeout_is_unknown_and_never_reissues():
    import time

    b, m = budget_module(), service_module()
    context, budget, primary, reviewer = configured()
    context._deadline = time.monotonic() + 0.15
    action, store, wires = snapshot(context), ReviewStore(budget), []

    async def handle(req):
        wires.append(req)
        await asyncio.Future()

    gw, _ = gateway([primary, reviewer], handle)
    with b._bind_run_budget(context):
        result = await m.ModelApprovalReviewService(gw, store).review(
            action, m._LiveReviewContext(context, materials_trusted=True, contains_secrets=False)
        )
    assert result.status != "allow" and len(wires) == 1 and budget.charged == 10000
    assert store.intent.status == "unknown"


async def test_revoke_during_review_never_returns_late_allow():
    b, m = budget_module(), service_module()
    context, budget, primary, reviewer = configured()
    action, store = snapshot(context), ReviewStore(budget)

    def handle(req):
        context.revoke()
        return httpx.Response(200, json=response(verdict(action)))

    gw, _ = gateway([primary, reviewer], handle)
    with b._bind_run_budget(context):
        result = await m.ModelApprovalReviewService(gw, store).review(
            action, m._LiveReviewContext(context, materials_trusted=True, contains_secrets=False)
        )
    assert result.status == "needs_human" and len(store.finishes) == 1


async def test_distinct_rejects_same_actual_request_model_behind_different_deployments():
    b, m = budget_module(), service_module()
    initial, _, _, _ = configured()
    context, budget, primary, reviewer = configured(
        config=replace(initial.config, require_distinct_model=True)
    )
    primary = replace(primary, request_model="same-underlying-model")
    reviewer = replace(reviewer, request_model="same-underlying-model")
    action, store, wires = snapshot(context), ReviewStore(budget), []

    def handle(req):
        wires.append(req)
        return httpx.Response(200, json=response(verdict(action)))

    gw, _ = gateway([primary, reviewer], handle)
    with b._bind_run_budget(context):
        await gw.complete_with_context(logical_request())
        result = await m.ModelApprovalReviewService(gw, store).review(
            action, m._LiveReviewContext(context, materials_trusted=True, contains_secrets=False)
        )
    assert result.status == "needs_human" and len(wires) == 1 and store.begins == 0


@pytest.mark.parametrize(
    "kind", ["lease_expired", "lease_replaced", "run_cancelled", "turn_replaced"]
)
async def test_post_issue_current_db_failure_cannot_return_new_allow(kind):
    b, m = budget_module(), service_module()
    context, budget, primary, reviewer = configured()
    action, store, wires = snapshot(context), ReviewStore(budget), []

    def handle(req):
        wires.append(req)
        assert store.intent.status == "issued"
        invalidate_current(budget, kind)
        return httpx.Response(200, json=response(verdict(action)))

    gw, _ = gateway([primary, reviewer], handle)
    live = m._LiveReviewContext(context, materials_trusted=True, contains_secrets=False)
    with b._bind_run_budget(context):
        result = await m.ModelApprovalReviewService(gw, store).review(action, live)
        repeated = await m.ModelApprovalReviewService(gw, store).review(action, live)
    assert result.status == repeated.status == "needs_human" and len(wires) == 1
    assert store.intent.status == "allow" and budget.charged == 5
    assert budget.current_reads and all(finishes == 1 for _, finishes in budget.current_reads)


async def test_current_db_read_error_redacts_and_preserves_committed_accounting():
    from sqlalchemy.exc import SQLAlchemyError

    b, m = budget_module(), service_module()
    context, budget, primary, reviewer = configured()
    action, store, wires = snapshot(context), ReviewStore(budget), []

    def handle(req):
        wires.append(req)
        budget.current_read_error = SQLAlchemyError("private lease database detail")
        return httpx.Response(200, json=response(verdict(action)))

    gw, _ = gateway([primary, reviewer], handle)
    with b._bind_run_budget(context):
        result = await m.ModelApprovalReviewService(gw, store).review(
            action, m._LiveReviewContext(context, materials_trusted=True, contains_secrets=False)
        )
    assert result.status == "needs_human" and result.reason_code == "review_unavailable"
    assert "private lease database detail" not in json.dumps(result.public_summary())
    assert store.intent.status == "allow" and budget.charged == 5 and len(wires) == 1


@pytest.mark.parametrize("interrupt", ["cancel", "revoke"])
async def test_current_db_read_preserves_final_cancel_and_revoke_checks(interrupt):
    b, m = budget_module(), service_module()
    context, budget, primary, reviewer = configured()
    action, store, wires = snapshot(context), ReviewStore(budget), []
    entered, release = asyncio.Event(), asyncio.Event()

    async def before_current_read():
        entered.set()
        await release.wait()

    budget.before_current_read = before_current_read

    def handle(req):
        wires.append(req)
        return httpx.Response(200, json=response(verdict(action)))

    gw, _ = gateway([primary, reviewer], handle)
    with b._bind_run_budget(context):
        task = asyncio.create_task(
            m.ModelApprovalReviewService(gw, store).review(
                action,
                m._LiveReviewContext(context, materials_trusted=True, contains_secrets=False),
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            assert budget.charged == 5 and store.intent.status == "allow"
            if interrupt == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                context.revoke()
                release.set()
                result = await task
                assert result.status == "needs_human"
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert len(wires) == 1 and len(store.finishes) == 1 and budget.charged == 5
