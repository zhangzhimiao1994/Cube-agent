"""Fixed Gateway/SDK transport proofs; no endpoint or real budget authority."""

import asyncio
import gc
import json
import time
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Unpack
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.capabilities.review_repository import review_config_sha256
from agent_hub.db.models import (
    RunAdmissionPoolRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunRow,
)
from agent_hub.models import physical_budget as b
from agent_hub.models.admission_bounds import (
    KnownPricing,
    TrustedContextBound,
    freeze_model_request,
)
from agent_hub.models.gateway import (
    DeploymentPricing,
    GatewayCompletion,
    ModelGateway,
    ModelTransport,
)
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.run_admission_repository import (
    AttemptSnapshot,
    AttemptState,
    DeploymentIdentity,
    PoolSnapshot,
    RunAdmissionRepository,
    _ExecutionArguments,
    _FinishArguments,
    _IssueArguments,
    _ReserveArguments,
    _WorkerLease,
)
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from agent_hub.runs.authorization import _capture_authenticated_input
from tests.unit.models.physical_budget_typed_fixture import (
    BudgetCall,
    CurrentFenceSession,
    FaultableExecutionBinding,
    FenceState,
    ModelRequestChanges,
    PhysicalBudgetModule,
    SelectedCapacity,
    WireJson,
    capture_void_return,
    wire_object,
)
from tests.unit.models.test_gateway import CapacityStub, SecretStub, lease


def budget_module() -> PhysicalBudgetModule:
    return b


class BudgetStore(RunAdmissionRepository, FenceState):
    """Transaction-boundary spy only; durable composition is tested with PG."""

    def __init__(self) -> None:
        FenceState.__init__(self)
        super().__init__(async_sessionmaker(
            expire_on_commit=False,
            sync_session_class=CurrentFenceSession,
            info={"physical_budget_fence": self},
        ))
        self.calls: list[BudgetCall] = []
        self.attempts: dict[UUID, AttemptSnapshot] = {}
        self.charged = 0
        self.grant = 40000
        self.closed = False

    async def reserve(self, **a: Unpack[_ReserveArguments]) -> AttemptSnapshot:
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

    async def mark_issued(self, **a: Unpack[_IssueArguments]) -> bool:
        self.calls.append(("issued", a))
        old = self.attempts[a["attempt_id"]]
        assert old.state == "prepared"
        self.attempts[old.attempt_id] = replace(
            old, state="issued", physical_transport_id=a["physical_transport_id"]
        )
        return True

    async def finish(self, **a: Unpack[_FinishArguments]) -> AttemptSnapshot:
        self.calls.append(("finish", a))
        old = self.attempts[a["attempt_id"]]
        usage = a.get("usage")
        known = usage is not None and usage.total_tokens is not None and usage.cost_usd is not None
        state: AttemptState = "completed" if a["outcome"] == "completed" and known else "unknown"
        if (
            usage is not None
            and usage.total_tokens is not None
            and usage.total_tokens > old.token_hold
        ):
            state, self.closed = "invalid", True
        elif state == "completed":
            assert usage is not None and usage.total_tokens is not None
            self.charged -= old.token_hold - usage.total_tokens
        item = replace(old, state=state)
        self.attempts[item.attempt_id] = item
        return item


def _install_current_fence(store: BudgetStore, context: b._RunBudgetContext) -> None:
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


def current_binding(context: b._RunBudgetContext) -> _ExecutionArguments:
    return {
        "tenant_id": context.tenant_id,
        "run_id": context.run_id,
        "authorization_turn_id": context.evidence.turn_id,
        "lease": context.lease,
        "config_sha256": review_config_sha256(context.config),
    }


def invalidate_current(store: BudgetStore, kind: str) -> None:
    if kind == "lease_expired":
        store.current_run.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif kind == "lease_replaced":
        store.current_run.worker_lease_token = uuid4()
    elif kind == "run_cancelled":
        store.current_run.status = "cancelled"
    elif kind == "turn_replaced":
        store.current_link.turn_id = store.current_turn.turn_id = uuid4()
    elif kind == "deadline":
        assert store.current_pool is not None
        store.current_pool.original_deadline = datetime.now(UTC) - timedelta(seconds=1)
    elif kind == "closed":
        assert store.current_pool is not None
        store.current_pool.closed = True
    elif kind == "config":
        assert store.current_pool is not None
        store.current_pool.config_sha256 = "0" * 64
    elif kind == "missing_pool":
        store.current_pool = None
    else:
        raise AssertionError("unknown test state")


def configured(
    store: BudgetStore | None = None,
    *,
    bounds: bool = True,
    config: ApprovalReviewConfig | None = None,
    acting: DeploymentIdentity | None = None,
) -> tuple[b._RunBudgetContext, BudgetStore, Deployment, Deployment]:
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


def logical_request(**changes: Unpack[ModelRequestChanges]) -> ModelRequest:
    return replace(
        ModelRequest(
            logical_model="primary",
            messages=(ModelMessage(role="user", content="private prompt"),),
            max_output_tokens=30000,
        ),
        **changes,
    )


def gateway(
    deployments: Sequence[Deployment],
    handler: (
        Callable[[httpx.Request], httpx.Response]
        | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]
        | None
    ),
    *,
    pricing: Mapping[str, DeploymentPricing] | None = None,
    transport: ModelTransport | None = None,
    fallbacks: Mapping[str, str] | None = None,
) -> tuple[ModelGateway, CapacityStub]:
    capacity = SelectedCapacity([lease(d.id) for d in deployments for _ in range(5)])
    if transport is None:
        assert handler is not None
        client: ModelTransport = LiteLLMClient(
            _network_delegate_factory=lambda: httpx.MockTransport(handler)
        )
    else:
        client = transport
    return ModelGateway(
        ModelRegistry(deployments),
        capacity,
        SecretStub(capacity.events),
        client,
        pricing=pricing,
        fallbacks=fallbacks,
    ), capacity


def response(text: str = "ok", usage: bool = True) -> dict[str, WireJson]:
    body: dict[str, WireJson] = {
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


def test_known_cost_uses_both_rates_without_decimal_round_down() -> None:
    with localcontext() as ctx:
        ctx.prec = 2
        observation = b._usage_observation(
            TokenUsage(2, 3, 5), KnownPricing(Decimal(1), Decimal(2))
        )
    assert observation is not None
    assert observation.cost_usd == Decimal("0.000008")
    assert b._usage_observation(None, KnownPricing(Decimal(0), Decimal(0))) is None


def test_default_complete_closed_before_first_await_does_not_leak_pending_coroutine() -> None:
    _, _, selected, _ = configured()
    gw, _ = gateway([selected], lambda r: pytest.fail("closed coroutine sent HTTP"))
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always", RuntimeWarning)
        pending = gw.complete(logical_request())
        pending.close()
        del pending
        gc.collect()
    assert not [warning for warning in observed if issubclass(warning.category, RuntimeWarning)]


def test_pool_deadline_conversion_never_adds_constructor_elapsed_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, store, _, _ = configured()
    pool = replace(context._pool, original_deadline=datetime.now(UTC) + timedelta(seconds=10))
    clock = iter((100.0, 110.0))
    with monkeypatch.context() as patch:
        patch.setattr(time, "monotonic", lambda: next(clock))
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


async def test_primary_gateway_selected_snapshot_override_and_reserve_issue_before_http() -> None:
    context, store, selected, _ = configured()
    wires: list[dict[str, WireJson]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        assert [c[0] for c in store.calls] == ["reserve", "issued"]
        wires.append(wire_object(req.content))
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
    messages = wires[0]["messages"]
    assert isinstance(messages, list)
    first_message = messages[0]
    assert isinstance(first_message, dict)
    assert first_message["content"] == "private prompt"
    assert wires[0]["max_completion_tokens"] == 2000
    reserve_call = store.calls[0]
    assert reserve_call[0] == "reserve"
    reserved = reserve_call[1]
    expected = replace(logical_request(), max_output_tokens=2000)
    assert reserved["request_sha256"] == freeze_model_request(expected).digest
    assert reserved["deployment"].deployment_id == selected.id
    assert reserved["purpose"] == "primary" and reserved["token_hold"] == 10000
    finish_call = store.calls[-1]
    assert finish_call[0] == "finish"
    finished_usage = finish_call[1]["usage"]
    assert finished_usage is not None
    assert finished_usage.cost_usd == Decimal("0.000018")
    assert store.charged == 5 and len(capacity.releases) == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_gateway_captures_scope_before_await_and_lazy_stream(stream: bool) -> None:
    context, store, selected, _ = configured()
    gw, _ = gateway([selected], lambda r: pytest.fail("revoked scope sent HTTP"))
    pending_stream = None
    pending_completion: Awaitable[GatewayCompletion] | None = None
    with b._bind_run_budget(context):
        if stream:
            pending_stream = gw.stream_openai_compatible_events(logical_request())
        else:
            pending_completion = gw.complete_with_context(logical_request())
    from agent_hub.models.physical_admission import _PhysicalAdmissionStopped

    with pytest.raises(_PhysicalAdmissionStopped):
        if stream:
            assert pending_stream is not None
            await anext(pending_stream)
        else:
            assert pending_completion is not None
            await pending_completion
    assert store.calls == []


async def test_missing_contract_and_custom_transport_fail_before_io() -> None:
    from agent_hub.models.physical_admission import _PhysicalAdmissionStopped

    context, store, selected, _ = configured(bounds=False)
    gw, _ = gateway([selected], lambda r: pytest.fail("unsupported bounds sent"))
    with b._bind_run_budget(context), pytest.raises((ValueError, _PhysicalAdmissionStopped)):
        await gw.complete_with_context(logical_request())
    assert store.calls == []

    class Unsupported:
        async def complete(
            self, deployment: Deployment, request: ModelRequest, api_key: str
        ) -> ModelResponse:
            pytest.fail("custom transport called")

    context, store, selected, _ = configured()
    gw, _ = gateway([selected], None, transport=Unsupported())
    with b._bind_run_budget(context), pytest.raises(_PhysicalAdmissionStopped):
        await gw.complete_with_context(logical_request())
    assert store.calls == []


async def test_primary_missing_usage_holds_and_authorized_fallback_uses_remaining() -> None:
    context, store, selected, _ = configured()
    fallback = replace(
        selected, id="fallback", logical_model="backup", quota_scope_id="scope-fallback"
    )
    context._trusted_contexts += (
        TrustedContextBound(fallback.id, "backup", fallback.provider_model, 10000),
    )
    wires: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
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


async def test_cancelled_delegate_preserves_full_hold() -> None:
    context, store, selected, _ = configured()
    entered = asyncio.Event()

    async def handle(req: httpx.Request) -> httpx.Response:
        entered.set()
        return await asyncio.Future[httpx.Response]()

    gw, _ = gateway([selected], handle)
    with b._bind_run_budget(context):
        task = asyncio.create_task(gw.complete_with_context(logical_request()))
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert store.charged == 10000
    assert all(a.state == "unknown" for a in store.attempts.values())


async def test_stream_actual_eof_settles_once_with_final_usage() -> None:
    context, store, selected, _ = configured()
    wires: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
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
async def test_invalid_usage_status_cannot_refund_retained_valid_observation(tokens: int) -> None:
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
    finish_call = store.calls[-1]
    assert finish_call[0] == "finish"
    observed = finish_call[1]["usage"]
    assert observed is not None
    assert observed.total_tokens == tokens and observed.cost_usd is None
    assert store.closed == (tokens > 10000)


async def test_stream_early_close_keeps_hold_and_closes_owned_body() -> None:
    context, store, selected, _ = configured()
    closed = asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            chunk = {
                "id": "chunk",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test",
                "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}],
            }
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            await asyncio.Future[None]()

        async def aclose(self) -> None:
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


async def test_root_scope_exit_while_dao_awaits_revokes_child_before_delegate() -> None:
    from agent_hub.models.physical_admission import _PhysicalAdmissionStopped

    waiting, release = asyncio.Event(), asyncio.Event()

    class WaitingStore(BudgetStore):
        async def mark_issued(self, **a: Unpack[_IssueArguments]) -> bool:
            waiting.set()
            await release.wait()
            return await super().mark_issued(**a)

    context, store, selected, _ = configured(WaitingStore())
    wires: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
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
async def test_require_current_uses_real_fence_without_writes_or_new_grant(kind: str) -> None:
    from agent_hub.models.run_admission_repository import AdmissionConflict

    context, store, _, _ = configured()
    require = store.require_current
    assert callable(require), "current durable fence query is missing"
    if kind != "current":
        invalidate_current(store, kind)
        with pytest.raises(AdmissionConflict, match="^admission state conflict$"):
            await require(**current_binding(context))
    else:
        assert (await capture_void_return(require(**current_binding(context))))[0] is None
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
async def test_require_current_rejects_invalid_original_binding_before_storage(
    change: dict[str, object],
) -> None:
    context, store, _, _ = configured()
    require = store.require_current
    assert callable(require), "current durable fence query is missing"
    fault = FaultableExecutionBinding(**current_binding(context))
    fault.inject(change)
    with pytest.raises(ValueError, match="^invalid admission binding$"):
        await require(**fault.arguments())
    assert store.current_reads == []


async def test_require_current_keeps_original_lease_across_lock_await() -> None:
    context, store, _, _ = configured()
    binding = current_binding(context)
    original = binding["lease"].token

    async def mutate_caller_lease() -> None:
        object.__setattr__(binding["lease"], "token", uuid4())

    store.before_current_read = mutate_caller_lease
    assert (await capture_void_return(store.require_current(**binding)))[0] is None
    assert binding["lease"].token != original and context.lease.token == original
    assert store.calls == [] and store.charged == 0
