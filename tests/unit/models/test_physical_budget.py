"""Fixed Gateway/SDK transport proofs; no endpoint or real budget authority."""

import asyncio
import gc
import importlib
import json
import time
import warnings
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.capabilities.review_repository import review_config_sha256
from agent_hub.db.models import (
    RunAdmissionPoolRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunRow,
)
from agent_hub.models.admission_bounds import (
    KnownPricing,
    TrustedContextBound,
    freeze_model_request,
)
from agent_hub.models.gateway import DeploymentPricing, ModelGateway
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.run_admission_repository import (
    AttemptSnapshot,
    PoolSnapshot,
    RunAdmissionRepository,
    _WorkerLease,
)
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    TokenUsage,
)
from agent_hub.runs.authorization import _capture_authenticated_input
from tests.unit.models.test_gateway import CapacityStub, SecretStub, lease


def budget_module():
    return importlib.import_module("agent_hub.models.physical_budget")


class BudgetStore(RunAdmissionRepository):
    """Transaction-boundary spy only; durable composition is tested with PG."""

    def __init__(self):
        self.calls = []
        self.attempts = {}
        self.charged = 0
        self.grant = 40000
        self.closed = False

    async def reserve(self, **a):
        self.calls.append(("reserve", a))
        if self.closed or a["token_hold"] > self.grant - self.charged:
            raise ValueError("balance exhausted")
        self.charged += a["token_hold"]
        item = AttemptSnapshot(
            a["attempt_id"],
            a["purpose"],
            "prepared",
            a["request_sha256"],
            a["deployment"],
            a["token_hold"],
            a["review_cost_hold"],
            None,
            None,
            None,
        )
        self.attempts[item.attempt_id] = item
        return item

    async def mark_issued(self, **a):
        self.calls.append(("issued", a))
        old = self.attempts[a["attempt_id"]]
        assert old.state == "prepared"
        self.attempts[old.attempt_id] = replace(
            old, state="issued", physical_transport_id=a["physical_transport_id"]
        )
        return True

    async def finish(self, **a):
        self.calls.append(("finish", a))
        old = self.attempts[a["attempt_id"]]
        usage = a.get("usage")
        known = usage is not None and usage.total_tokens is not None and usage.cost_usd is not None
        state = "completed" if a["outcome"] == "completed" and known else "unknown"
        if (
            usage is not None
            and usage.total_tokens is not None
            and usage.total_tokens > old.token_hold
        ):
            state, self.closed = "invalid", True
        elif state == "completed":
            self.charged -= old.token_hold - usage.total_tokens
        item = replace(old, state=state)
        self.attempts[item.attempt_id] = item
        return item


class _CurrentFenceSession:
    """Read-only ORM/clock results for unchanged production _locked/_fence."""

    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def begin(self):
        return self

    def in_transaction(self):
        return True

    def _observed(self, kind):
        finishes = sum(name == "finish" for name, _ in self.store.calls)
        self.store.current_reads.append((kind, finishes))

    async def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is RunRow:
            self._observed("run")
            if self.store.before_current_read is not None:
                await self.store.before_current_read()
            if self.store.current_read_error is not None:
                raise self.store.current_read_error
            return self.store.current_run
        assert entity is RunAdmissionPoolRow
        self._observed("pool")
        return self.store.current_pool

    async def execute(self, statement):
        if statement.column_descriptions[0].get("entity") is RunInputAuthorizationRow:
            self._observed("authorization")
            return SimpleNamespace(
                one_or_none=lambda: (self.store.current_link, self.store.current_turn)
            )
        self._observed("clock")
        return SimpleNamespace(scalar_one=lambda: datetime.now(UTC))


def _install_current_fence(store, context):
    """Actual ORM facts plus a read-only session spy, shared by new unit regressions."""
    evidence = context.evidence
    store.current_reads = []
    store.before_current_read = None
    store.current_read_error = None
    store.current_run = RunRow(
        id=context.run_id,
        tenant_id=context.tenant_id,
        actor_id=evidence.actor_id,
        actor_role=evidence.actor_role.value,
        request=evidence.message,
        status="running",
        mode="direct",
        blocked_by_run_id=None,
        worker_id=context.lease.worker_id,
        worker_lease_token=context.lease.token,
        worker_lease_expires_at=context._pool.original_deadline,
        routing_decision={
            "runtime_token_absolute_tokens": 40000,
            "runtime_plan_token_budget": 40000,
            "runtime_timeout_absolute_seconds": 50,
        },
    )
    store.current_pool = RunAdmissionPoolRow(
        tenant_id=context.tenant_id,
        run_id=context.run_id,
        authorization_turn_id=evidence.turn_id,
        actor_id=evidence.actor_id,
        actor_role=evidence.actor_role.value,
        message_sha256=evidence.message_sha256,
        scope_sha256=evidence.scope_sha256,
        config_sha256=review_config_sha256(context.config),
        original_token_grant=context._pool.original_token_grant,
        original_deadline=context._pool.original_deadline,
        closed=False,
    )
    store.current_link = RunInputAuthorizationRow(
        run_id=context.run_id, tenant_id=context.tenant_id, turn_id=evidence.turn_id
    )
    store.current_turn = RunAuthenticatedInputRow(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        turn_id=evidence.turn_id,
        actor_id=evidence.actor_id,
        actor_role=evidence.actor_role.value,
        message=evidence.message,
        message_sha256=evidence.message_sha256,
        input_scope=evidence.input_scope,
        scope_sha256=evidence.scope_sha256,
    )
    store._session_factory = lambda: _CurrentFenceSession(store)


def current_binding(context):
    return {
        "tenant_id": context.tenant_id,
        "run_id": context.run_id,
        "authorization_turn_id": context.evidence.turn_id,
        "lease": context.lease,
        "config_sha256": review_config_sha256(context.config),
    }


def invalidate_current(store, kind):
    if kind == "lease_expired":
        store.current_run.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif kind == "lease_replaced":
        store.current_run.worker_lease_token = uuid4()
    elif kind == "run_cancelled":
        store.current_run.status = "cancelled"
    elif kind == "turn_replaced":
        store.current_link.turn_id = store.current_turn.turn_id = uuid4()
    elif kind == "deadline":
        store.current_pool.original_deadline = datetime.now(UTC) - timedelta(seconds=1)
    elif kind == "closed":
        store.current_pool.closed = True
    elif kind == "config":
        store.current_pool.config_sha256 = "0" * 64
    elif kind == "missing_pool":
        store.current_pool = None
    else:
        raise AssertionError("unknown test state")


def configured(store=None, *, bounds=True, config=None, acting=None):
    b = budget_module()
    config = config or ApprovalReviewConfig(
        engine="rules_then_model",
        reviewer_logical_model="reviewer",
        max_input_tokens=20000,
        max_output_tokens=512,
        max_cost_usd_per_run="10.00",
    )
    evidence = _capture_authenticated_input(
        AuthenticatedPrincipal(tenant_id=uuid4(), user_id=uuid4(), role=Role.OPERATOR),
        "Write the exact approved test file.",
        {"workspace": "synthetic"},
    )
    pool = PoolSnapshot(
        evidence.tenant_id,
        uuid4(),
        evidence.turn_id,
        review_config_sha256(config),
        40000,
        datetime.now(UTC) + timedelta(seconds=50),
        40000,
        8,
        Decimal(10),
        0,
        0,
        0,
        Decimal(0),
        False,
    )
    selected = Deployment(
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
        selected,
        id="review-selected",
        logical_model="reviewer",
        quota_scope_id="scope-review-selected",
        provider_model="openai/reviewer",
        capabilities=frozenset({ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT}),
    )
    store = store or BudgetStore()
    context = b._RunBudgetContext(
        repository=store,
        pool=pool,
        evidence=evidence,
        lease=_WorkerLease("test-worker", uuid4()),
        config=config,
        deadline=time.monotonic() + 45,
        trusted_contexts=tuple(
            TrustedContextBound(d.id, d.logical_model, d.provider_model, 10000)
            for d in (selected, reviewer)
        )
        if bounds
        else (),
        acting_model=acting,
    )
    _install_current_fence(store, context)
    return context, store, selected, reviewer


def logical_request(**changes):
    return replace(
        ModelRequest(
            logical_model="primary",
            messages=(ModelMessage(role="user", content="private prompt"),),
            max_output_tokens=30000,
        ),
        **changes,
    )


def gateway(deployments, handler, *, pricing=None, transport=None, fallbacks=None):
    capacity = CapacityStub([lease(d.id) for d in deployments for _ in range(5)])

    # Select only leases eligible in this acquisition; model scheduling is the real Gateway.
    async def acquire(candidates, wait_timeout, *, estimated_tokens):
        capacity.events.append(("acquire", tuple(d.id for d in candidates)))
        return lease(candidates[0].id)

    capacity.acquire = acquire
    client = transport or LiteLLMClient(
        _network_delegate_factory=lambda: httpx.MockTransport(handler)
    )
    return ModelGateway(
        ModelRegistry(deployments),
        capacity,
        SecretStub(capacity.events),
        client,
        pricing=pricing,
        fallbacks=fallbacks,
    ), capacity


def response(text="ok", usage=True):
    body = {
        "id": "fixed",
        "object": "chat.completion",
        "created": 1,
        "model": "test",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    return body


def test_known_cost_uses_both_rates_without_decimal_round_down():
    b = budget_module()
    with localcontext() as ctx:
        ctx.prec = 2
        observation = b._usage_observation(
            TokenUsage(2, 3, 5), KnownPricing(Decimal(1), Decimal(2))
        )
    assert observation.cost_usd == Decimal("0.000008")
    assert b._usage_observation(None, KnownPricing(Decimal(0), Decimal(0))) is None


def test_default_complete_closed_before_first_await_does_not_leak_pending_coroutine():
    _, _, selected, _ = configured()
    gw, _ = gateway([selected], lambda r: pytest.fail("closed coroutine sent HTTP"))
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always", RuntimeWarning)
        pending = gw.complete(logical_request())
        pending.close()
        del pending
        gc.collect()
    assert not [warning for warning in observed if issubclass(warning.category, RuntimeWarning)]


def test_pool_deadline_conversion_never_adds_constructor_elapsed_time(monkeypatch):
    b = budget_module()
    context, store, _, _ = configured()
    pool = replace(context._pool, original_deadline=datetime.now(UTC) + timedelta(seconds=10))
    clock = iter((100.0, 110.0))
    with monkeypatch.context() as patch:
        patch.setattr(b.time, "monotonic", lambda: next(clock))
        captured = b._RunBudgetContext(
            repository=store,
            pool=pool,
            evidence=context.evidence,
            lease=context.lease,
            config=context.config,
            deadline=115.0,
            trusted_contexts=context._trusted_contexts,
        )
    assert captured.deadline <= 110.0


async def test_primary_gateway_selected_snapshot_override_and_reserve_issue_before_http():
    b = budget_module()
    context, store, selected, _ = configured()
    wires = []

    def handle(req):
        assert [c[0] for c in store.calls] == ["reserve", "issued"]
        wires.append(json.loads(req.content))
        return httpx.Response(200, json=response())

    gw, capacity = gateway(
        [selected], handle, pricing={selected.id: DeploymentPricing(Decimal(3), Decimal(4))}
    )
    request = logical_request()
    with b._bind_run_budget(context):
        pending = gw.complete_with_context(request)
        object.__setattr__(request.messages[0], "content", "changed after capture")
        result = await pending
    assert result.response.text == "ok"
    assert wires[0]["messages"][0]["content"] == "private prompt"
    assert wires[0]["max_completion_tokens"] == 2000
    reserved = store.calls[0][1]
    expected = replace(logical_request(), max_output_tokens=2000)
    assert reserved["request_sha256"] == freeze_model_request(expected).digest
    assert reserved["deployment"].deployment_id == selected.id
    assert reserved["purpose"] == "primary" and reserved["token_hold"] == 10000
    assert store.calls[-1][1]["usage"].cost_usd == Decimal("0.000018")
    assert store.charged == 5 and len(capacity.releases) == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_gateway_captures_scope_before_await_and_lazy_stream(stream):
    b = budget_module()
    context, store, selected, _ = configured()
    gw, _ = gateway([selected], lambda r: pytest.fail("revoked scope sent HTTP"))
    with b._bind_run_budget(context):
        pending = (
            gw.stream_openai_compatible_events(logical_request())
            if stream
            else gw.complete_with_context(logical_request())
        )
    from agent_hub.models.physical_admission import _PhysicalAdmissionStopped

    with pytest.raises(_PhysicalAdmissionStopped):
        if stream:
            await anext(pending)
        else:
            await pending
    assert store.calls == []


async def test_missing_contract_and_custom_transport_fail_before_io():
    b = budget_module()
    from agent_hub.models.physical_admission import _PhysicalAdmissionStopped

    context, store, selected, _ = configured(bounds=False)
    gw, _ = gateway([selected], lambda r: pytest.fail("unsupported bounds sent"))
    with b._bind_run_budget(context), pytest.raises((ValueError, _PhysicalAdmissionStopped)):
        await gw.complete_with_context(logical_request())
    assert store.calls == []

    class Unsupported:
        async def complete(self, *a):
            pytest.fail("custom transport called")

    context, store, selected, _ = configured()
    gw, _ = gateway([selected], None, transport=Unsupported())
    with b._bind_run_budget(context), pytest.raises(_PhysicalAdmissionStopped):
        await gw.complete_with_context(logical_request())
    assert store.calls == []


async def test_primary_missing_usage_holds_and_authorized_fallback_uses_remaining():
    b = budget_module()
    context, store, selected, _ = configured()
    fallback = replace(
        selected, id="fallback", logical_model="backup", quota_scope_id="scope-fallback"
    )
    context._trusted_contexts += (
        TrustedContextBound(fallback.id, "backup", fallback.provider_model, 10000),
    )
    wires = []

    def handle(req):
        wires.append(req)
        return (
            httpx.Response(503, json={"error": {"message": "failed"}})
            if len(wires) == 1
            else httpx.Response(200, json=response())
        )

    gw, _ = gateway([selected, fallback], handle, fallbacks={"primary": "backup"})
    with b._bind_run_budget(context):
        result = await gw.complete_with_context(logical_request())
    assert result.deployment_id == "fallback" and len(wires) == 2
    assert sorted(a.state for a in store.attempts.values()) == ["completed", "unknown"]
    assert store.charged == 10005
    assert len({a.physical_transport_id for a in store.attempts.values()}) == 2


async def test_cancelled_delegate_preserves_full_hold():
    b = budget_module()
    context, store, selected, _ = configured()
    entered = asyncio.Event()

    async def handle(req):
        entered.set()
        await asyncio.Future()

    gw, _ = gateway([selected], handle)
    with b._bind_run_budget(context):
        task = asyncio.create_task(gw.complete_with_context(logical_request()))
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert store.charged == 10000
    assert all(a.state == "unknown" for a in store.attempts.values())


async def test_stream_actual_eof_settles_once_with_final_usage():
    b = budget_module()
    context, store, selected, _ = configured()
    wires = []

    def handle(req):
        wires.append(req)
        chunks = [
            {
                "id": "chunk",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "ok"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chunk",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        ]
        content = (
            "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, content=content.encode(), headers={"content-type": "text/event-stream"}
        )

    gw, _ = gateway([selected], handle)
    with b._bind_run_budget(context):
        events = [event async for event in gw.stream_openai_compatible_events(logical_request())]
    assert events and len(wires) == 1 and store.charged == 5
    assert [c[0] for c in store.calls] == ["reserve", "issued", "finish"]


@pytest.mark.parametrize("tokens", [5, 10001])
async def test_invalid_usage_status_cannot_refund_retained_valid_observation(tokens):
    b = budget_module()
    from agent_hub.models.physical_admission import _PhysicalAttemptMetadata, _PhysicalReceipt

    context, store, selected, _ = configured()
    admission = b._BudgetAdmission(context)
    selected, _req, selection = admission.select(
        selected, replace(logical_request(), max_output_tokens=2000), None
    )
    meta = _PhysicalAttemptMetadata(
        selected.id,
        selected.provider_model,
        selection.request_sha256,
        "primary",
        "chat_completions",
        True,
        1,
        selection,
    )
    permit = await admission.before_send(meta, deadline=context.deadline)
    await admission.after_receipt(
        permit,
        _PhysicalReceipt("completed", TokenUsage(tokens, 0, tokens), "invalid", 200, True),
        deadline=context.deadline,
    )
    assert store.charged == 10000
    observed = store.calls[-1][1]["usage"]
    assert observed.total_tokens == tokens and observed.cost_usd is None
    assert store.closed == (tokens > 10000)


async def test_stream_early_close_keeps_hold_and_closes_owned_body():
    b = budget_module()
    context, store, selected, _ = configured()
    closed = asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunk = {
                "id": "chunk",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test",
                "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}],
            }
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            await asyncio.Future()

        async def aclose(self):
            closed.set()

    gw, _ = gateway(
        [selected],
        lambda req: httpx.Response(
            200, stream=Body(), headers={"content-type": "text/event-stream"}
        ),
    )
    with b._bind_run_budget(context):
        events = gw.stream_openai_compatible_events(logical_request())
        await anext(events)
        await events.aclose()
    assert closed.is_set() and store.charged == 10000
    assert all(a.state == "unknown" for a in store.attempts.values())


async def test_root_scope_exit_while_dao_awaits_revokes_child_before_delegate():
    b = budget_module()
    from agent_hub.models.physical_admission import _PhysicalAdmissionStopped

    waiting, release = asyncio.Event(), asyncio.Event()

    class WaitingStore(BudgetStore):
        async def mark_issued(self, **a):
            waiting.set()
            await release.wait()
            return await super().mark_issued(**a)

    context, store, selected, _ = configured(WaitingStore())
    wires = []

    def handle(req):
        wires.append(req)
        return httpx.Response(200, json=response())

    gw, _ = gateway([selected], handle)
    with b._bind_run_budget(context):
        child = asyncio.create_task(gw.complete_with_context(logical_request()))
        await asyncio.wait_for(waiting.wait(), timeout=3)
    release.set()
    with pytest.raises(_PhysicalAdmissionStopped):
        await child
    assert wires == [] and store.charged == 10000


@pytest.mark.parametrize(
    "kind",
    [
        "current",
        "lease_expired",
        "lease_replaced",
        "run_cancelled",
        "turn_replaced",
        "deadline",
        "closed",
        "config",
        "missing_pool",
    ],
)
async def test_require_current_uses_real_fence_without_writes_or_new_grant(kind):
    from agent_hub.models.run_admission_repository import AdmissionConflict

    context, store, _, _ = configured()
    require = getattr(store, "require_current", None)
    assert callable(require), "current durable fence query is missing"
    if kind != "current":
        invalidate_current(store, kind)
        with pytest.raises(AdmissionConflict, match="^admission state conflict$"):
            await require(**current_binding(context))
    else:
        assert await require(**current_binding(context)) is None
        assert [kind for kind, _ in store.current_reads] == [
            "run",
            "pool",
            "authorization",
            "clock",
        ]
    assert store.calls == [] and store.charged == 0 and store.attempts == {}


@pytest.mark.parametrize(
    "change", [{"tenant_id": "bad"}, {"lease": None}, {"config_sha256": "bad"}]
)
async def test_require_current_rejects_invalid_original_binding_before_storage(change):
    context, store, _, _ = configured()
    require = getattr(store, "require_current", None)
    assert callable(require), "current durable fence query is missing"
    with pytest.raises(ValueError, match="^invalid admission binding$"):
        await require(**{**current_binding(context), **change})
    assert store.current_reads == []


async def test_require_current_keeps_original_lease_across_lock_await():
    context, store, _, _ = configured()
    binding = current_binding(context)
    original = binding["lease"].token

    async def mutate_caller_lease():
        object.__setattr__(binding["lease"], "token", uuid4())

    store.before_current_read = mutate_caller_lease
    assert await store.require_current(**binding) is None
    assert binding["lease"].token != original and context.lease.token == original
    assert store.calls == [] and store.charged == 0
