"""Worker assembly proofs with real repository code and fixed HTTP only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from types import MethodType, ModuleType
from typing import TypedDict, Unpack
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.engine.result import IteratorResult
from sqlalchemy.sql import Select

from agent_hub.auth.models import Role
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.capabilities.review_repository import _CurrentReviewBinding
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.config.schema import PlatformConfig
from agent_hub.config.service import ConfigService
from agent_hub.db.models import (
    RunAdmissionPoolRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunRow,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import CapacityPool
from agent_hub.models.gateway import ModelGateway, ModelGatewayError
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.physical_admission import _PhysicalAdmissionStopped
from agent_hub.models.physical_budget import _run_budget, _RunBudgetContext
from agent_hub.models.registry import NoCapableDeployment
from agent_hub.models.run_admission_repository import AdmissionConflict, RunAdmissionRepository
from agent_hub.models.types import Deployment
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime import review_assembly
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
from agent_hub.runtime.contracts import ExecutionRuntime, JsonValue, RunEvent, TaskContext
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.worker_execution import worker_runtime_events
from agent_hub.security.secrets import SecretService
from tests.unit.capabilities.test_concrete_model_review import FocusedDB
from tests.unit.harness.review_typed_fixture import (
    FixedCapacityPool,
    StreamRuntimeStub,
    UnusedConfigs,
    UnusedGateway,
    UnusedSecrets,
    json_objects,
    logical_request,
    mutable,
    present,
    response,
    result_rows,
    unused_capacity,
)
from tests.unit.harness.test_review_dispatch import ExecuteCall, ReviewSetup, setup


def assembly() -> ModuleType:
    return review_assembly


class ConsumeKeywords(TypedDict):
    tenant_id: UUID
    run_id: UUID
    review_id: UUID
    current_binding: _CurrentReviewBinding


async def await_cancel(callback: Callable[[], Awaitable[None]]) -> None:
    await callback()


class AssemblyDB(FocusedDB):
    def execute_rows(self, statement: Select[tuple[object, ...]]) -> IteratorResult[tuple[object, ...]]:
        if (statement.column_descriptions[0].get("entity") is RunRow
                and len(statement.column_descriptions) == 3):
            link = next(r for r in self.rows if isinstance(r, RunInputAuthorizationRow))
            turn = next(r for r in self.rows if isinstance(r, RunAuthenticatedInputRow) and r.turn_id == link.turn_id)
            return result_rows([(self.run, link, turn)])
        return super().execute_rows(statement)


@dataclass
class AssemblyState:
    config: ApprovalReviewConfig
    platform: PlatformConfig
    version: int = 7
    calls: list[dict[str, object]] = field(default_factory=list)
    review: bool = False


class FixtureConfigs(ConfigService):
    def __init__(self, state: AssemblyState, tenant: UUID) -> None:
        self.state, self.tenant = state, tenant

    async def get_current(self, tenant_id: UUID) -> ConfigRevision:
        assert tenant_id == self.tenant
        return ConfigRevision(uuid4(), tenant_id, self.state.version, ConfigStatus.PUBLISHED,
            self.state.platform.model_dump(mode='json'), None, datetime.now(UTC))


class FixtureSecrets(SecretService):
    def __init__(self) -> None:
        pass

    async def resolve(self, tenant_id: UUID, reference: object) -> str:
        return 'fixed-test-secret'

    async def fingerprint(self, tenant_id: UUID, reference: object) -> str:
        return 'fixed-test-fingerprint'


@dataclass
class AssemblyFixture:
    s: ReviewSetup
    db: AssemblyDB
    broker: review_assembly.ReviewAssemblyBroker
    state: AssemblyState
    config_service: FixtureConfigs
    secrets: FixtureSecrets
    capacity: Callable[[UUID, Sequence[Deployment]], Awaitable[CapacityPool]]


@dataclass
class CancelState:
    f: AssemblyFixture
    actual: AutoGenDiscussionRuntime
    started: asyncio.Event
    release: asyncio.Event
    completed: asyncio.Event
    seen: list[tuple[asyncio.Task[object], _RunBudgetContext]]
    key: tuple[UUID, UUID]


async def fixture(tmp_path: Path, *, mode: TaskMode = TaskMode.DIRECT, contracts: bool = True, verified: bool = True) -> AssemblyFixture:
    s = setup(tmp_path)
    a = review_assembly
    db = AssemblyDB(s)
    s.runs._session_factory = db
    # A first execution starts without a pool; the DAO, not the test, opens it.
    db.rows.remove(db.pool)
    db.run.created_at = datetime.now(UTC)
    db.run.mode = mode.value
    s.runs.record = replace(s.runs.record, created_at=db.run.created_at, mode=mode)
    s.task = s.task.model_copy(update={"mode": mode, "token_budget": 123})
    actual_runs = RunRepository(db)
    pytest.MonkeyPatch().setattr(s.runs, "load_authenticated_input", actual_runs.load_authenticated_input)
    definition = {
        "provider": "openai",
        "model": "fixed",
        "credential_ref": "test-secret",
        "quota_scope_id": "fixed",
        "context_window_tokens": 10000,
        "max_output_tokens": 2000,
        "input_per_million_usd": "1",
        "output_per_million_usd": "2",
        "capabilities": ["text", "structured_output", "tool_calling"],
    }
    platform = PlatformConfig.model_validate(
        {
            "models": {
                "primary": {"deployments": [definition]},
                "reviewer": {"deployments": [{**definition, "model": "reviewer"}]},
            },
            "agents": [],
        }
    )
    state = AssemblyState(s.settings.config, platform)

    async def config(tenant: UUID) -> ApprovalReviewConfig:
        return state.config

    async def capacity(tenant: UUID, deployments: Sequence[Deployment]) -> CapacityPool:
        return FixedCapacityPool(deployments)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = mutable(json.loads(request.content))
        state.calls.append(payload)
        assert _run_budget.get() is not None
        assert any(getattr(row, "original_token_grant", None) == 40000 for row in db.rows)
        if getattr(state, "review", False):
            messages = json_objects(payload["messages"])
            action = mutable(json.loads(str(messages[-1]["content"])))
            verdict = {
                "schema_version": 1,
                "decision": "allow",
                "risk": "low",
                "reason_code": "authorized_action",
                "rationale": "Matches the exact authorized write.",
                "action_digest": action["action_digest"],
            }
            return httpx.Response(200, json=response(json.dumps(verdict)))
        return httpx.Response(200, json=response())

    broker = a.ReviewAssemblyBroker(
        config_service=FixtureConfigs(state, s.task.tenant_id),
        config_getter=config,
        run_repository=s.runs,
        session_factory=db,
        secret_service=FixtureSecrets(),
        capacity_factory=capacity,
        transport=LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(handler)),
        billing_contracts=tuple(
            a.ProviderBillingContract(
                s.task.tenant_id,
                a.catalog_fingerprint(platform),
                name,
                "test://mock-all-billed-tokens-contract",
            )
            for name in ("primary_1", "reviewer_1")
        )
        if contracts
        else (),
        verification=a.WorkerReviewVerification(
            frozenset({TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID}),
            "test://fixed-driver-verification",
            source_sha256=a.runtime_source_fingerprint(),
            checks=a.REQUIRED_VERIFICATION_CHECKS,
        )
        if verified
        else None,
    )
    profile = await broker.submission_profile(s.task.tenant_id, timeout_seconds=60)
    route = {
        **s.task.routing_decision,
        "runtime_token_absolute_tokens": 40000,
        "runtime_plan_token_budget": 40000,
        **profile,
    }
    s.task = s.task.model_copy(update={"routing_decision": route})
    s.runs.record = replace(s.runs.record, routing_decision=route)
    db.run.routing_decision = route
    return AssemblyFixture(s, db, broker, state, FixtureConfigs(state, s.task.tenant_id), FixtureSecrets(), capacity)


def test_broker_is_a_real_server_assembly() -> None:
    assert callable(review_assembly.ReviewAssemblyBroker)


@pytest.mark.parametrize(
    "mode", [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID]
)
async def test_worker_builds_pool_before_lazy_runtime_and_retains_until_close(tmp_path: Path, mode: TaskMode) -> None:
    a = review_assembly
    f = await fixture(tmp_path, mode=mode)
    seen, child_scopes = [], []

    class Runtime(StreamRuntimeStub):
        def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            seen.append(present(_run_budget.get()))  # synchronous/lazy creation boundary
            assert a.current_review_platform(context.tenant_id) is not None

            async def events() -> AsyncIterator[RunEvent]:
                from agent_hub.runtime.defaults import _gateway_for_config

                budget = present(_run_budget.get())
                assert budget is seen[0]
                assert budget.lease.token == context.execution_token
                assert budget._pool.original_token_grant == 40000  # soft 123 is not a hard grant
                gateway, _, _ = await _gateway_for_config(
                    present(a.current_review_platform(context.tenant_id)),
                    tenant_id=context.tenant_id,
                    secret_service=f.secrets,
                    capacity_factory=f.capacity,
                    transport=f.broker._transport,
                )
                try:
                    await gateway.complete(logical_request())
                    yield RunEvent(
                        kind="runtime.started",
                        sequence=1,
                        run_id=context.run_id,
                        payload={"phase": "started"},
                    )
                    assert _run_budget.get() is budget

                    async def child() -> None:
                        child_scopes.append(_run_budget.get())
                        await gateway.complete(logical_request())

                    await asyncio.create_task(child())
                    yield RunEvent(
                        kind="runtime.completed",
                        sequence=2,
                        run_id=context.run_id,
                        payload={"phase": "completed"},
                    )
                finally:
                    seen.append(present(_run_budget.get()))
                    await gateway.complete(logical_request())  # finalization is also charged

            return events()

    assert _run_budget.get() is None
    async with worker_runtime_events(
        Runtime(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
    ) as events:
        await anext(events)
        assert _run_budget.get() is None
        await anext(events)
        assert seen[0]._active
        assert child_scopes == [seen[0]]
    assert seen == [seen[0], seen[0]]
    assert not seen[0]._active
    assert len(f.state.calls) == 3
    pool = await RunAdmissionRepository(f.db).get_pool(f.s.task.tenant_id, f.s.task.run_id)
    assert present(pool).token_charged == 15
    assert present(pool).original_deadline == f.s.runs.record.created_at + timedelta(seconds=60)


async def test_same_run_reentry_and_unknown_never_refill_or_extend(tmp_path: Path) -> None:
    f = await fixture(tmp_path)
    first = present(await f.broker.prepare(f.s.task, worker_id=f.s.budget.lease.worker_id))
    pool_row = next(row for row in f.db.rows if isinstance(row, RunAdmissionPoolRow))
    pool_row.token_charged = 18000  # retained UNKNOWN debit in the actual stored pool
    deadline = present(first).budget._pool.original_deadline
    second = present(await f.broker.prepare(
        f.s.task.model_copy(update={"token_budget": 999999}), worker_id=f.s.budget.lease.worker_id
    ))
    assert present(second).budget._pool.tokens_remaining == 22000
    assert present(second).budget._pool.original_deadline == deadline
    assert present(second).budget.deadline <= time.monotonic() + 60


async def test_old_profile_is_never_upgraded_even_when_configuration_ready(tmp_path: Path) -> None:
    f = await fixture(tmp_path)
    route = {
        key: value
        for key, value in f.s.task.routing_decision.items()
        if key != "approval_review_profile"
    }
    assert (
        await f.broker.prepare(
            f.s.task.model_copy(update={"routing_decision": route}),
            worker_id=f.s.budget.lease.worker_id,
        )
        is None
    )
    assert not f.state.calls
    assert await RunAdmissionRepository(f.db).get_pool(f.s.task.tenant_id, f.s.task.run_id) is None


@pytest.mark.parametrize(
    "contracts,verified,reason",
    [(False, True, "provider_contract_missing"), (True, False, "current_mode_fence_unverified")],
)
async def test_declared_context_is_not_provider_contract(tmp_path: Path, contracts: bool, verified: bool, reason: object) -> None:
    f = await fixture(tmp_path, contracts=contracts, verified=verified)
    readiness = await f.broker.readiness(f.s.task.tenant_id)
    assert readiness.status == "not_ready" and readiness.reason_code == reason
    assert not readiness.supported_scope
    assert not f.state.calls


async def test_rules_default_produces_no_profile_or_pool(tmp_path: Path) -> None:
    f = await fixture(tmp_path)
    f.state.config = ApprovalReviewConfig()
    assert await f.broker.submission_profile(f.s.task.tenant_id, timeout_seconds=60) == {}
    assert (await f.broker.readiness(f.s.task.tenant_id)).reason_code == "rules_only"
    assert not f.state.calls


@pytest.mark.parametrize("change", ["worker", "role", "config", "catalog", "derived"])
async def test_changed_worker_config_role_and_derived_run_fail_closed(tmp_path: Path, change: str) -> None:
    f = await fixture(tmp_path)
    context = f.s.task
    if change == "worker":
        f.s.runs.record = replace(f.s.runs.record, worker_lease_token=uuid4())
    elif change == "role":
        f.s.runs.record = replace(f.s.runs.record, actor_role=Role.VIEWER)
    elif change == "config":
        f.state.config = ApprovalReviewConfig()
    elif change == "catalog":
        f.state.version += 1
    else:
        context = context.model_copy(
            update={
                "routing_decision": {
                    **context.routing_decision,
                    "source": "self_repair",
                    "source_run_id": str(uuid4()),
                }
            }
        )
        f.s.runs.record = replace(f.s.runs.record, routing_decision=dict(context.routing_decision))
    with pytest.raises(review_assembly.ReviewAssemblyUnavailable):
        await f.broker.prepare(context, worker_id=f.s.budget.lease.worker_id)
    assert not f.state.calls


async def test_runtime_local_context_token_survives_yields_without_leaking(tmp_path: Path) -> None:
    f = await fixture(tmp_path)
    local = ContextVar("runtime_local", default="outside")

    class Runtime(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            token = local.set("inside")
            try:
                yield RunEvent(
                    kind="runtime.step", sequence=1, run_id=context.run_id, payload={"step": 1}
                )
                assert local.get() == "inside"
                yield RunEvent(
                    kind="runtime.step", sequence=2, run_id=context.run_id, payload={"step": 2}
                )
            finally:
                local.reset(token)

    async with worker_runtime_events(
        Runtime(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
    ) as events:
        await anext(events)
        assert local.get() == "outside"
        await anext(events)
    assert local.get() == "outside"


@pytest.mark.parametrize("mode", [TaskMode.DIRECT, TaskMode.DISPATCH])
@pytest.mark.parametrize("advances", [0, 2])
async def test_actual_adapter_stream_retains_one_consumer_task(tmp_path: Path, mode: TaskMode, advances: int) -> None:
    from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, CrewRunStream
    from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
    from agent_hub.runtime.defaults import _gateway_for_config
    from agent_hub.runtime.direct import DirectRunStream, DirectRuntime

    f = await fixture(tmp_path, mode=mode)
    context = f.s.task.model_copy(update={"token_budget": 20000})
    gateway, _, _ = await _gateway_for_config(
        f.state.platform,
        tenant_id=f.s.task.tenant_id,
        secret_service=f.secrets,
        capacity_factory=f.capacity,
        transport=f.broker._transport,
    )
    runtime = (
        DirectRuntime(gateway, logical_model="primary")
        if mode is TaskMode.DIRECT
        else CrewDispatchRuntime(
            gateway,
            DispatchPlan(
                agents=(
                    AgentSpec(id="writer", role="Writer", goal="Reply", logical_model="primary"),
                ),
                steps=(
                    DispatchStep(
                        id="reply", agent="writer", task="Reply briefly", final_synthesizer=True
                    ),
                ),
                total_token_budget=20000,
                total_timeout_seconds=60,
            ),
        )
    )
    before = asyncio.all_tasks()
    async with worker_runtime_events(
        runtime, context, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
    ) as events:
        actual = runtime._active_stream
        assert isinstance(actual, DirectRunStream if mode is TaskMode.DIRECT else CrewRunStream)
        owner = None
        for _ in range(advances):
            await anext(events)
            owner = present(owner or actual._owner)
            assert actual._owner is owner and not owner.done()
            assert owner is not asyncio.current_task()
        assert _run_budget.get() is None
    assert owner is None or owner.done()
    assert actual._closed and runtime._active_stream is None
    assert not (asyncio.all_tasks() - before)


async def test_worker_owner_cancellation_drains_same_task_finalizer_without_prefetch(tmp_path: Path) -> None:
    f = await fixture(tmp_path)
    entered, finalizing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancel_work = asyncio.Event()
    observed, counts = [], []
    local = ContextVar("owner_local", default="outside")
    before = asyncio.all_tasks()

    class Runtime(StreamRuntimeStub):
        async def cancel(self) -> None:
            # Match ExecutionRuntime's work-cancellation protocol, not owner.cancel().
            cancel_work.set()

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            token = local.set("inside")
            owner, budget = present(asyncio.current_task()), present(_run_budget.get())
            observed.append((owner, budget))
            try:
                counts.append(1)
                yield RunEvent(
                    kind="runtime.first",
                    sequence=1,
                    run_id=context.run_id,
                    payload={"phase": "first"},
                )
                counts.append(2)
                entered.set()
                await cancel_work.wait()
            finally:
                assert asyncio.current_task() is owner and local.get() == "inside"
                finalizing.set()
                await release.wait()
                assert _run_budget.get() is budget and budget._active
                local.reset(token)

    async def driver() -> None:
        async with worker_runtime_events(
            Runtime(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
        ) as events:
            await anext(events)
            await asyncio.sleep(0)
            assert counts == [1] and local.get() == "outside" and _run_budget.get() is None
            await anext(events)

    task = asyncio.create_task(driver())
    try:
        try:
            await asyncio.wait_for(entered.wait(), 3)
        except TimeoutError:
            if task.done():
                await task
            raise
        task.cancel()
        await asyncio.wait_for(finalizing.wait(), 3)
        task.cancel()  # Must not re-cancel/abandon the owner's awaited finalizer.
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        owner, budget = observed[0]
        assert owner.done() and not budget._active and local.get() == "outside"
        assert not (asyncio.all_tasks() - before)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("phase", ["construct", "advance", "finalize"])
async def test_worker_scope_failure_closes_and_revokes_without_background_task(tmp_path: Path, phase: str) -> None:
    f = await fixture(tmp_path)
    seen = []
    before = asyncio.all_tasks()

    class Runtime(StreamRuntimeStub):
        def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            seen.append(present(_run_budget.get()))
            if phase == "construct":
                raise ValueError("construct")

            async def events() -> AsyncIterator[RunEvent]:
                try:
                    if phase == "advance":
                        raise ValueError("advance")
                    yield RunEvent(
                        kind="runtime.first",
                        sequence=1,
                        run_id=context.run_id,
                        payload={"phase": "first"},
                    )
                finally:
                    assert _run_budget.get() is seen[0] and seen[0]._active
                    if phase == "finalize":
                        raise ValueError("finalize")

            return events()

    with pytest.raises(ValueError, match=phase):
        async with worker_runtime_events(
            Runtime(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
        ) as events:
            await anext(events)
    assert not seen[0]._active and _run_budget.get() is None
    assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize("terminal", ["eof", "error"])
async def test_natural_finalizer_survives_first_caller_cancel(tmp_path: Path, terminal: object) -> None:
    """Unmodified AsyncIterator contract: cancellation cannot infer its phase."""
    f = await fixture(tmp_path)
    started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []
    before = asyncio.all_tasks()

    class Runtime(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            owner, budget = present(asyncio.current_task()), present(_run_budget.get())
            observed.append((owner, budget))
            try:
                yield RunEvent(
                    kind="runtime.first", sequence=1, run_id=context.run_id, payload={"first": True}
                )
                if terminal == "error":
                    raise ValueError("terminal")
            finally:
                started.set()
                await release.wait()
                assert asyncio.current_task() is owner and _run_budget.get() is budget
                assert budget._active
                completed.set()

    async def driver() -> None:
        async with worker_runtime_events(
            Runtime(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
        ) as events:
            await anext(events)
            await anext(events)

    task = asyncio.create_task(driver())
    try:
        await asyncio.wait_for(started.wait(), 3)
        owner, budget = observed[0]
        task.cancel()
        for _ in range(4):
            await asyncio.sleep(0)
        assert not owner.done(), "first caller cancel truncated a finalizer already in progress"
        assert not completed.is_set() and not task.done() and budget._active
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set() and owner.done() and not budget._active
        assert _run_budget.get() is None and not (asyncio.all_tasks() - before)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("mode", [TaskMode.DIRECT, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_actual_direct_cancel_stops_http_but_drains_network_cleanup(tmp_path: Path, mode: TaskMode) -> None:
    from agent_hub.db.models import RunAdmissionAttemptRow
    from agent_hub.runtime.autogen.adapter import (
        AutoGenDiscussionRuntime,
        DiscussionParticipant,
        DiscussionPlan,
    )
    from agent_hub.runtime.crew.adapter import CrewAIObjectFactory, CrewDispatchRuntime
    from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
    from agent_hub.runtime.defaults import _gateway_for_config
    from agent_hub.runtime.direct import DirectRuntime
    from agent_hub.runtime.hybrid import HybridRuntime

    crew_factory = CrewAIObjectFactory(storage_dir=tmp_path / "crewai")
    if mode is TaskMode.HYBRID:
        # Initialize the real optional SDK before creating the admitted run.
        # The original 3s HTTP barrier and 10s runtime budget stay unchanged.
        crew_factory.build((), (), share_crew=False, telemetry_disabled=True)
    f = await fixture(tmp_path, mode=mode)
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    budgets, cancel_calls = [], []

    async def handler(request: httpx.Request) -> httpx.Response:
        budgets.append(present(_run_budget.get()))
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()
            assert _run_budget.get() is budgets[0] and budgets[0]._active
        raise AssertionError("cancelled HTTP must not return a response")

    gateway, _, _ = await _gateway_for_config(
        f.state.platform,
        tenant_id=f.s.task.tenant_id,
        secret_service=f.secrets,
        capacity_factory=f.capacity,
        transport=LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(handler)),
    )
    direct = DirectRuntime(gateway, logical_model="primary")
    discussion = AutoGenDiscussionRuntime(
        gateway,
        DiscussionPlan(
            participants=tuple(
                DiscussionParticipant(name, name, "Reply", "primary")
                for name in ("analyst", "critic")
            ),
            selector_model="primary",
            token_budget=20000,
            wall_time_seconds=10,
        ),
    )
    dispatch = CrewDispatchRuntime(
        gateway,
        DispatchPlan(
            agents=(AgentSpec(id="writer", role="Writer", goal="Reply", logical_model="primary"),),
            steps=(
                DispatchStep(
                    id="reply",
                    agent="writer",
                    task="Reply",
                    final_synthesizer=True,
                    timeout_seconds=10,
                ),
            ),
            total_token_budget=20000,
            total_timeout_seconds=10,
        ),
        crew_factory=crew_factory,
    )
    runtimes: dict[TaskMode, ExecutionRuntime] = {
        TaskMode.DIRECT: direct,
        TaskMode.DISCUSS: discussion,
        TaskMode.HYBRID: HybridRuntime(dispatch, discussion, direct),
    }
    runtime = runtimes[mode]
    original_cancel = runtime.cancel

    async def cancel() -> None:
        cancel_calls.append(_run_budget.get())
        await original_cancel()

    pytest.MonkeyPatch().setattr(runtime, "cancel", cancel)  # Delegating spy; actual runtime/stream/cancel implementation remain.
    context = f.s.task.model_copy(update={"token_budget": 20000, "timeout_seconds": 10.0})
    before = asyncio.all_tasks()

    async def driver() -> None:
        async with worker_runtime_events(
            runtime, context, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
        ) as events:
            async for _ in events:
                pass

    task = asyncio.create_task(driver())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        owner = next(
            t for t in asyncio.all_tasks() - before if t.get_name() == "worker-runtime-owner"
        )
        task.cancel()
        await asyncio.wait_for(cleanup.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not owner.done() and owner.cancelling() == 0
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Hybrid's coordinator can start HTTP while the owner is between event
        # demands; that path closes in the owner without a cancel callback.
        assert len(cancel_calls) <= 1 and all(b is budgets[0] for b in cancel_calls)
        if mode is not TaskMode.HYBRID:
            assert cancel_calls == [budgets[0]]
        assert not budgets[0]._active
        assert (
            getattr(runtime, "_active_stream", None) is None
            and getattr(runtime, "_active_task", None) is None
            and owner.done()
        )
        attempts = [r for r in f.db.rows if type(r) is RunAdmissionAttemptRow]
        assert len(attempts) == 1 and attempts[0].state == "unknown"
        pool = await RunAdmissionRepository(f.db).get_pool(context.tenant_id, context.run_id)
        assert present(pool).token_charged == 10000
        assert present(pool).original_deadline == budgets[0]._pool.original_deadline
        assert not (asyncio.all_tasks() - before)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("mode", ["discuss", "hybrid"])
async def test_actual_cancel_protocol_does_not_cancel_finalizing_owner(tmp_path: Path, mode: str) -> None:
    """Actual cancel methods around a barrier probe, not a native adapter run."""
    from autogen_core import CancellationToken

    from agent_hub.runtime.autogen.adapter import (
        AutoGenDiscussionRuntime,
        DiscussionParticipant,
        DiscussionPlan,
    )
    from agent_hub.runtime.crew.adapter import CrewDispatchRuntime
    from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
    from agent_hub.runtime.direct import DirectRuntime
    from agent_hub.runtime.hybrid import HybridRuntime

    f = await fixture(tmp_path)
    discussion = AutoGenDiscussionRuntime(
        UnusedGateway(),
        DiscussionPlan(
            participants=tuple(
                DiscussionParticipant(name, name, "Reply", "primary")
                for name in ("analyst", "critic")
            ),
            selector_model="primary",
        ),
    )
    discussion._cancel_token, discussion._cancel_event = CancellationToken(), asyncio.Event()
    direct = DirectRuntime(UnusedGateway(), logical_model="primary")
    dispatch = CrewDispatchRuntime(
        UnusedGateway(),
        DispatchPlan(
            agents=(AgentSpec(id="writer", role="Writer", goal="Reply", logical_model="primary"),),
            steps=(DispatchStep(id="reply", agent="writer", task="Reply", final_synthesizer=True),),
        ),
    )
    actual = discussion if mode == "discuss" else HybridRuntime(dispatch, discussion, direct)
    started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen = []
    before = asyncio.all_tasks()

    class Probe(StreamRuntimeStub):
        async def cancel(self) -> None:
            await actual.cancel()

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            owner = present(asyncio.current_task())
            actual._active_task = discussion._active_task = owner
            if mode == "hybrid":
                assert isinstance(actual, HybridRuntime)
                actual._active_child = discussion
            seen.append(owner)
            try:
                yield RunEvent(
                    kind="runtime.probe", sequence=1, run_id=context.run_id, payload={"probe": True}
                )
            finally:
                started.set()
                await release.wait()
                assert asyncio.current_task() is owner
                completed.set()

    async def driver() -> None:
        async with worker_runtime_events(
            Probe(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
        ) as events:
            await anext(events)
            await anext(events)

    task = asyncio.create_task(driver())
    try:
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        await asyncio.wait_for(discussion._cancel_event.wait(), 3)
        assert not task.done() and not seen[0].done() and seen[0].cancelling() == 0
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set() and seen[0].done()
        assert not (asyncio.all_tasks() - before)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _finalizer_cancel_runtime(mode: str) -> tuple[AutoGenDiscussionRuntime | HybridRuntime, AutoGenDiscussionRuntime]:
    from autogen_core import CancellationToken

    from agent_hub.runtime.autogen.adapter import (
        AutoGenDiscussionRuntime,
        DiscussionParticipant,
        DiscussionPlan,
    )
    from agent_hub.runtime.crew.adapter import CrewDispatchRuntime
    from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
    from agent_hub.runtime.direct import DirectRuntime
    from agent_hub.runtime.hybrid import HybridRuntime

    discussion = AutoGenDiscussionRuntime(
        UnusedGateway(),
        DiscussionPlan(
            participants=tuple(
                DiscussionParticipant(name, name, "Reply", "primary")
                for name in ("analyst", "critic")
            ),
            selector_model="primary",
        ),
    )
    discussion._cancel_token, discussion._cancel_event = CancellationToken(), asyncio.Event()
    if mode == "discuss":
        return discussion, discussion
    dispatch = CrewDispatchRuntime(
        UnusedGateway(),
        DispatchPlan(
            agents=(AgentSpec(id="writer", role="Writer", goal="Reply", logical_model="primary"),),
            steps=(DispatchStep(id="reply", agent="writer", task="Reply", final_synthesizer=True),),
        ),
    )
    runtime = HybridRuntime(dispatch, discussion, DirectRuntime(UnusedGateway(), logical_model="primary"))
    runtime._active_child = discussion
    return runtime, discussion


@pytest.mark.parametrize("mode", ["discuss", "hybrid"])
@pytest.mark.parametrize("terminal", ["eof", "error"])
@pytest.mark.parametrize("entry", ["heartbeat", "owned", "run", "all", "adapter"])
async def test_fresh_external_cancel_drains_exact_owner(tmp_path: Path, mode: str, terminal: str, entry: str) -> None:
    """Independent cancellation must not inject into an already finalizing owner."""
    from contextvars import Context

    from agent_hub.runs.service import RunService, _runtime_cancel_for_run
    from agent_hub.runtime.defaults import ConfigBackedDiscussionRuntime, ConfigBackedHybridRuntime
    from agent_hub.runtime.streams import _is_protected_stream_owner

    f = await fixture(tmp_path)
    actual, discussion = _finalizer_cancel_runtime(mode)
    factory_type = ConfigBackedDiscussionRuntime if mode == "discuss" else ConfigBackedHybridRuntime
    factory = factory_type(
        config_service=UnusedConfigs(), secret_service=UnusedSecrets(), capacity_factory=unused_capacity
    )
    key = (f.s.task.run_id, present(f.s.task.execution_token))
    started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    model_entered, model_stopped = asyncio.Event(), asyncio.Event()
    observed, renewals, models = [], [], []
    before = asyncio.all_tasks()

    async def model_work() -> None:
        model_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            model_stopped.set()

    class Probe(StreamRuntimeStub):
        # Actual factory lookup and actual adapter cancel; only the run body is a seam.
        async def cancel_run_owned(self, run_id: UUID, execution_token: UUID) -> None:
            await factory.cancel_run_owned(run_id, execution_token)

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            owner, budget = present(asyncio.current_task()), present(_run_budget.get())
            actual._active_task = discussion._active_task = owner
            factory._active[key] = actual
            model = asyncio.create_task(model_work())
            models.append(model)
            present(discussion._cancel_token).link_future(model)
            observed.append((owner, budget))
            try:
                await model_entered.wait()
                yield RunEvent(
                    kind="runtime.probe", sequence=1, run_id=context.run_id, payload={"probe": True}
                )
                if terminal == "error":
                    raise ValueError("terminal")
            finally:
                try:
                    started.set()
                    await release.wait()
                    assert asyncio.current_task() is owner and _run_budget.get() is budget
                    assert budget._active
                    completed.set()
                finally:
                    factory._active.pop(key, None)

    async def driver() -> None:
        async with worker_runtime_events(
            Probe(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
        ) as events:
            async for _ in events:
                assert _run_budget.get() is None

    class Repository(RunRepository):
        def __init__(self) -> None:
            pass

        async def renew_active_worker_lease(self, *, tenant_id: UUID, run_id: UUID,
            worker_id: str, worker_lease_token: UUID, worker_lease_expires_at: datetime) -> bool:
            renewals.append({"tenant_id": tenant_id, "run_id": run_id, "worker_id": worker_id,
                "worker_lease_token": worker_lease_token, "worker_lease_expires_at": worker_lease_expires_at})
            return False

    class HeartbeatService(RunService):
        def __init__(self) -> None:
            self._repository = Repository()
            self._worker_id = f.s.budget.lease.worker_id

        def _worker_lease_heartbeat_interval_seconds(self) -> float:
            return 0.001

        def _worker_lease_expires_at(self) -> datetime:
            return datetime.now(UTC)

    service = HeartbeatService()

    async def external_cancel() -> None:
        assert _run_budget.get() is None
        if entry == "heartbeat":
            await RunService._heartbeat_worker_lease(
                service,
                tenant_id=f.s.task.tenant_id,
                run_id=key[0],
                worker_lease_token=key[1],
                stop=asyncio.Event(),
                runtime_cancel=_runtime_cancel_for_run(factory, *key),
            )
        elif entry == "owned":
            await factory.cancel_run_owned(*key)
        elif entry == "run":
            await factory.cancel_run(key[0])
        elif entry == "all":
            await factory.cancel()
        else:
            await actual.cancel()

    task = asyncio.create_task(driver())
    try:
        await asyncio.wait_for(started.wait(), 3)
        owner, budget = observed[0]
        assert not _is_protected_stream_owner(models[0])
        await asyncio.wait_for(asyncio.create_task(external_cancel(), context=Context()), 3)
        await asyncio.wait_for(model_stopped.wait(), 3)
        assert present(discussion._cancel_event).is_set() and present(discussion._cancel_token).is_cancelled()
        assert owner.cancelling() == 0, "external cancel injected into finalizing fresh owner"
        assert _is_protected_stream_owner(owner)
        assert not task.done() and not owner.done() and not completed.is_set() and budget._active
        if entry == "heartbeat":
            assert len(renewals) == 1
            assert renewals[0]["run_id"] == key[0]
            assert renewals[0]["worker_lease_token"] == key[1]
            assert renewals[0]["worker_id"] == f.s.budget.lease.worker_id
        release.set()
        if terminal == "error":
            with pytest.raises(ValueError, match="terminal"):
                await asyncio.wait_for(task, 3)
        else:
            await asyncio.wait_for(task, 3)
        await asyncio.gather(*models, return_exceptions=True)
        assert completed.is_set() and owner.done() and owner.cancelling() == 0
        assert not budget._active and not _is_protected_stream_owner(owner)
        assert not factory._active and not (asyncio.all_tasks() - before)
    finally:
        release.set()
        for model in models:
            if not model.done():
                model.cancel()
        await asyncio.gather(task, *models, return_exceptions=True)


@pytest.mark.parametrize("close_error", [False, True])
async def test_fresh_owner_registration_lasts_through_aclose(tmp_path: Path, close_error: object) -> None:
    from contextvars import Context

    from agent_hub.runtime.streams import _is_protected_stream_owner

    f = await fixture(tmp_path)
    actual, _ = _finalizer_cancel_runtime("discuss")
    started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen = []
    before = asyncio.all_tasks()

    class Stream:
        def __aiter__(self) -> Stream:
            return self

        async def __anext__(self) -> RunEvent:
            actual._active_task = asyncio.current_task()
            seen.append((present(asyncio.current_task()), present(_run_budget.get())))
            return RunEvent(
                kind="runtime.probe", sequence=1, run_id=f.s.task.run_id, payload={"probe": True}
            )

        async def aclose(self) -> None:
            owner, budget = seen[0]
            started.set()
            await release.wait()
            assert (
                asyncio.current_task() is owner and _run_budget.get() is budget and budget._active
            )
            completed.set()
            if close_error:
                raise ValueError("close")

    class Probe(StreamRuntimeStub):
        def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            return Stream()

    async def driver() -> None:
        async with worker_runtime_events(
            Probe(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
        ) as events:
            await anext(events)

    task = asyncio.create_task(driver())
    try:
        await asyncio.wait_for(started.wait(), 3)
        owner, budget = seen[0]
        await asyncio.wait_for(asyncio.create_task(actual.cancel(), context=Context()), 3)
        assert owner.cancelling() == 0 and not owner.done() and not task.done()
        assert _is_protected_stream_owner(owner) and budget._active
        release.set()
        if close_error:
            with pytest.raises(ValueError, match="close"):
                await asyncio.wait_for(task, 3)
        else:
            await asyncio.wait_for(task, 3)
        assert completed.is_set() and owner.done() and owner.cancelling() == 0
        assert not _is_protected_stream_owner(owner) and not budget._active
        assert not (asyncio.all_tasks() - before)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_owned_external_cancel_does_not_cross_run_or_token(tmp_path: Path) -> None:
    from contextvars import Context

    from agent_hub.runs.service import _runtime_cancel_for_run
    from agent_hub.runtime.defaults import ConfigBackedDiscussionRuntime
    from agent_hub.runtime.streams import _is_protected_stream_owner

    factory = ConfigBackedDiscussionRuntime(
        config_service=UnusedConfigs(), secret_service=UnusedSecrets(), capacity_factory=unused_capacity
    )
    states: list[CancelState] = []
    before = asyncio.all_tasks()
    for number in range(2):
        directory = tmp_path / str(number)
        directory.mkdir()
        f = await fixture(directory)
        actual, _ = _finalizer_cancel_runtime("discuss")
        assert isinstance(actual, AutoGenDiscussionRuntime)
        states.append(
            CancelState(
                f=f,
                actual=actual,
                started=asyncio.Event(),
                release=asyncio.Event(),
                completed=asyncio.Event(),
                seen=[],
                key=(f.s.task.run_id, present(f.s.task.execution_token)),
            )
        )

    class Probe(StreamRuntimeStub):
        def __init__(self, state: CancelState) -> None:
            self.state = state

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            state = self.state
            owner, budget = present(asyncio.current_task()), present(_run_budget.get())
            state.actual._active_task = owner
            factory._active[state.key] = state.actual
            state.seen.append((owner, budget))
            try:
                yield RunEvent(
                    kind="runtime.probe", sequence=1, run_id=context.run_id, payload={"probe": True}
                )
            finally:
                try:
                    state.started.set()
                    await state.release.wait()
                    assert budget._active
                    state.completed.set()
                finally:
                    factory._active.pop(state.key, None)

    async def driver(state: CancelState) -> None:
        async with worker_runtime_events(
            Probe(state),
            state.f.s.task,
            worker_id=state.f.s.budget.lease.worker_id,
            review_broker=state.f.broker,
        ) as events:
            async for _ in events:
                pass

    tasks = [asyncio.create_task(driver(state)) for state in states]
    first, second = states
    try:
        await asyncio.wait_for(asyncio.gather(*(s.started.wait() for s in states)), 3)
        for wrong_key in (
            (first.key[0], second.key[1]),
            (second.key[0], first.key[1]),
            (uuid4(), first.key[1]),
        ):
            await asyncio.wait_for(
                asyncio.create_task(
                    await_cancel(_runtime_cancel_for_run(factory, *wrong_key)), context=Context()
                ),
                3,
            )
        assert all(not present(s.actual._cancel_event).is_set() for s in states)
        assert all(s.seen[0][0].cancelling() == 0 and not s.seen[0][0].done() for s in states)
        await asyncio.wait_for(
            asyncio.create_task(await_cancel(_runtime_cancel_for_run(factory, *first.key)), context=Context()),
            3,
        )
        assert present(first.actual._cancel_event).is_set() and not present(second.actual._cancel_event).is_set()
        assert all(s.seen[0][0].cancelling() == 0 and s.seen[0][1]._active for s in states)
        first.release.set()
        await asyncio.wait_for(tasks[0], 3)
        assert first.completed.is_set() and not _is_protected_stream_owner(first.seen[0][0])
        assert not first.seen[0][1]._active
        assert not tasks[1].done() and _is_protected_stream_owner(second.seen[0][0])
        assert second.seen[0][1]._active and not present(second.actual._cancel_event).is_set()
        await _runtime_cancel_for_run(
            factory, *first.key
        )()  # Closed key cannot target the other run.
        assert not present(second.actual._cancel_event).is_set()
        second.release.set()
        await asyncio.wait_for(tasks[1], 3)
        assert second.completed.is_set() and not _is_protected_stream_owner(second.seen[0][0])
        assert not second.seen[0][1]._active and not factory._active
        assert not (asyncio.all_tasks() - before)
    finally:
        for state in states:
            state.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_fresh_owner_registration_does_not_protect_model_task(tmp_path: Path) -> None:
    from contextvars import Context

    from agent_hub.runtime.streams import _is_protected_stream_owner

    f = await fixture(tmp_path)
    actual, discussion = _finalizer_cancel_runtime("hybrid")
    started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    model_entered, model_stopped = asyncio.Event(), asyncio.Event()
    seen, models = [], []
    before = asyncio.all_tasks()

    async def model_work() -> None:
        model_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            model_stopped.set()

    class Probe(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            owner = present(asyncio.current_task())
            actual._active_task = owner
            # No linked token: actual AutoGen.cancel must cancel this distinct Task.
            discussion._active_task = asyncio.create_task(model_work())
            models.append(discussion._active_task)
            seen.append(owner)
            try:
                await model_entered.wait()
                yield RunEvent(
                    kind="runtime.probe", sequence=1, run_id=context.run_id, payload={"probe": True}
                )
            finally:
                started.set()
                await release.wait()
                completed.set()

    async def driver() -> None:
        async with worker_runtime_events(
            Probe(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
        ) as events:
            async for _ in events:
                pass

    task = asyncio.create_task(driver())
    try:
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.wait_for(asyncio.create_task(actual.cancel(), context=Context()), 3)
        await asyncio.wait_for(model_stopped.wait(), 3)
        assert models[0].cancelled() and not _is_protected_stream_owner(models[0])
        assert _is_protected_stream_owner(seen[0]) and seen[0].cancelling() == 0
        assert not task.done() and not completed.is_set()
        release.set()
        await asyncio.wait_for(task, 3)
        assert completed.is_set() and not _is_protected_stream_owner(seen[0])
        assert not (asyncio.all_tasks() - before)
    finally:
        release.set()
        for model in models:
            if not model.done():
                model.cancel()
        await asyncio.gather(task, *models, return_exceptions=True)


async def test_worker_cancel_targets_original_owned_run_only(tmp_path: Path) -> None:
    f = await fixture(tmp_path)
    entered, stop = asyncio.Event(), asyncio.Event()
    seen = []

    class Runtime(StreamRuntimeStub):
        async def cancel(self) -> None:
            raise AssertionError("shared factory cancel-all must not be called")

        async def cancel_run_owned(self, run_id: UUID, execution_token: UUID) -> None:
            seen.append((run_id, execution_token, _run_budget.get()))
            stop.set()

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            entered.set()
            await stop.wait()
            yield RunEvent(kind="runtime.cancelled", sequence=1, run_id=context.run_id)

    async def driver() -> None:
        async with worker_runtime_events(
            Runtime(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
        ) as events:
            await anext(events)

    task = asyncio.create_task(driver())
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(seen) == 1 and seen[0][:2] == (f.s.task.run_id, present(f.s.task.execution_token))
    assert not present(seen[0][2])._active and _run_budget.get() is None


@pytest.mark.parametrize("mode", ["discuss", "hybrid"])
async def test_legacy_cancel_keeps_unguarded_task_cancellation(mode: str) -> None:
    from agent_hub.runtime.autogen.adapter import (
        AutoGenDiscussionRuntime,
        DiscussionParticipant,
        DiscussionPlan,
    )
    from agent_hub.runtime.crew.adapter import CrewDispatchRuntime
    from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
    from agent_hub.runtime.direct import DirectRuntime
    from agent_hub.runtime.hybrid import HybridRuntime
    from agent_hub.runtime.streams import _is_protected_stream_owner

    discussion = AutoGenDiscussionRuntime(
        UnusedGateway(),
        DiscussionPlan(
            participants=tuple(
                DiscussionParticipant(name, name, "Reply", "primary")
                for name in ("analyst", "critic")
            ),
            selector_model="primary",
        ),
    )
    actual: AutoGenDiscussionRuntime | HybridRuntime
    if mode == "hybrid":
        dispatch = CrewDispatchRuntime(
            UnusedGateway(),
            DispatchPlan(
                agents=(
                    AgentSpec(id="writer", role="Writer", goal="Reply", logical_model="primary"),
                ),
                steps=(
                    DispatchStep(id="reply", agent="writer", task="Reply", final_synthesizer=True),
                ),
            ),
        )
        actual = HybridRuntime(
            dispatch, discussion, DirectRuntime(UnusedGateway(), logical_model="primary")
        )
    else:
        actual = discussion
    entered = asyncio.Event()

    async def waiting() -> None:
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(waiting())
    actual._active_task = task
    try:
        await entered.wait()
        assert not _is_protected_stream_owner(task)
        await actual.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_legacy_none_worker_path_does_not_install_cancel_owner_or_budget(tmp_path: Path) -> None:
    f = await fixture(tmp_path)
    context = f.s.task.model_copy(
        update={
            "routing_decision": {
                key: value
                for key, value in f.s.task.routing_decision.items()
                if key != "approval_review_profile"
            }
        }
    )
    observed = []
    caller = asyncio.current_task()
    before = asyncio.all_tasks()

    class Runtime(StreamRuntimeStub):
        async def cancel(self) -> None:
            raise AssertionError("legacy stream must keep its existing close path")

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            try:
                observed.append((asyncio.current_task(), _run_budget.get()))
                yield RunEvent(
                    kind="runtime.legacy",
                    sequence=1,
                    run_id=context.run_id,
                    payload={"legacy": True},
                )
            finally:
                observed.append((asyncio.current_task(), _run_budget.get()))

    async with worker_runtime_events(
        Runtime(), context, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
    ) as events:
        await anext(events)
    assert observed == [(caller, None), (caller, None)]
    assert not (asyncio.all_tasks() - before)
    assert await RunAdmissionRepository(f.db).get_pool(context.tenant_id, context.run_id) is None


def evidence_document(f: AssemblyFixture) -> dict[str, object]:
    a = review_assembly
    return {
        "schema_version": 1,
        "valid_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "billing_contracts": [
            {
                "tenant_id": str(f.s.task.tenant_id),
                "catalog_sha256": a.catalog_fingerprint(f.state.platform),
                "deployment_id": name,
                "evidence_reference": "test://fixed-contract-report",
                "contract": "single_physical_all_billed_tokens_v1",
            }
            for name in ("primary_1", "reviewer_1")
        ],
        "worker_verification": {
            "profile_version": 1,
            "source_sha256": a.runtime_source_fingerprint(),
            "modes": ["direct", "dispatch", "discuss", "hybrid"],
            "checks": sorted(a.REQUIRED_VERIFICATION_CHECKS),
            "evidence_reference": "test://fixed-native-report",
        },
    }


async def test_installable_server_evidence_loader_is_used_by_real_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = review_assembly
    f = await fixture(tmp_path, contracts=False)
    document = evidence_document(f)
    path = tmp_path / "installed-server-evidence.json"
    body = json.dumps(document).encode()
    path.write_bytes(body)
    monkeypatch.setenv("AGENT_HUB_REVIEW_EVIDENCE_FILE", str(path))
    monkeypatch.setenv("AGENT_HUB_REVIEW_EVIDENCE_SHA256", hashlib.sha256(body).hexdigest())
    broker = a.build_review_broker(
        config_service=f.config_service,
        config_getter=f.broker.current_config,
        run_repository=f.s.runs,
        session_factory=f.db,
        secret_service=f.secrets,
        redis_client=object(),
    )
    assert (await broker.readiness(f.s.task.tenant_id)).status == "ready"
    path.write_text("{}", encoding="utf-8")
    readiness = await broker.readiness(f.s.task.tenant_id)
    assert readiness.status == "not_ready" and readiness.reason_code == "evidence_invalid"


@pytest.mark.parametrize(
    "invalid", ["digest", "expired", "source", "checks", "duplicate", "unknown", "bool"]
)
async def test_server_evidence_rejects_stale_incomplete_or_modified_records(tmp_path: Path, invalid: object) -> None:
    a = review_assembly
    f = await fixture(tmp_path)
    document = evidence_document(f)
    if invalid == "expired":
        document["valid_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif invalid == "source":
        mutable(document["worker_verification"])["source_sha256"] = "0" * 64
    elif invalid == "checks":
        mutable(document["worker_verification"])["checks"] = ["original_pool"]
    elif invalid == "unknown":
        document["ready"] = True
    elif invalid == "bool":
        document["schema_version"] = True
    body = json.dumps(document).encode()
    if invalid == "duplicate":
        body = body[:-1] + b',"schema_version":1}'
    path = tmp_path / "evidence.json"
    path.write_bytes(body)
    digest = "0" * 64 if invalid == "digest" else hashlib.sha256(body).hexdigest()
    with pytest.raises(ValueError, match="invalid server review evidence"):
        a.load_server_review_evidence(path, expected_sha256=digest)


async def test_unused_untrusted_catalog_deployment_does_not_block_trusted_subset(tmp_path: Path) -> None:
    a = review_assembly
    f = await fixture(tmp_path)
    document = f.state.platform.model_dump(mode="json")
    document["models"]["untrusted"] = {
        "deployments": [
            {
                "provider": "openai",
                "model": "unknown",
                "credential_ref": "unknown",
                "quota_scope_id": "unknown",
            }
        ]
    }
    f.state.platform = PlatformConfig.model_validate(document)
    fingerprint = a.catalog_fingerprint(f.state.platform)
    f.broker._contracts = tuple(
        replace(contract, catalog_sha256=fingerprint) for contract in f.broker._contracts
    )
    assert (await f.broker.readiness(f.s.task.tenant_id)).status == "ready"
    profile = await f.broker.submission_profile(f.s.task.tenant_id, timeout_seconds=60)
    route = {**f.s.task.routing_decision, **profile}
    f.s.task = f.s.task.model_copy(update={"routing_decision": route})
    f.s.runs.record = replace(f.s.runs.record, routing_decision=route)
    f.db.run.routing_decision = route

    class Runtime(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            from agent_hub.runtime.defaults import _deployments, _gateway_for_config

            actual = a.review_deployments(_deployments(f.state.platform))
            assert {deployment.id for deployment in actual} == {"primary_1", "reviewer_1"}
            gateway, _, _ = await _gateway_for_config(
                f.state.platform,
                tenant_id=context.tenant_id,
                secret_service=f.secrets,
                capacity_factory=f.capacity,
                transport=f.broker._transport,
            )
            with pytest.raises((NoCapableDeployment, ModelGatewayError)):
                await gateway.complete(
                    logical_request(logical_model="untrusted", allow_fallback=False)
                )
            yield RunEvent(
                kind="runtime.filtered",
                sequence=1,
                run_id=context.run_id,
                payload={"filtered": True},
            )

    async with worker_runtime_events(
        Runtime(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
    ) as events:
        await anext(events)
    assert not f.state.calls


async def test_actual_stack_broker_reviewer_repository_and_backend_execute_once(tmp_path: Path) -> None:
    from agent_hub.capabilities.defaults import build_runtime_capability_stack
    from agent_hub.db.models import RunReviewEventRow
    from agent_hub.files.workspace import ProjectWorkspaceStore

    a = review_assembly
    f = await fixture(tmp_path)
    f.state.review = True
    stack = a.assemble_review_capability_stack(
        f.broker,
        build_runtime_capability_stack,
        tenant_id=f.s.task.tenant_id,
        run_repository=f.s.runs,
        skill_store_dir=tmp_path / "skills",
        workspace_root=tmp_path,
        generated_artifact_dir=tmp_path / "generated",
        project_workspace_dir=tmp_path,
    )
    profiled = stack.harness_tool_gateway
    assert isinstance(profiled, a._ProfiledHarness)
    fresh = profiled._fresh
    gate = present(fresh._review_gate)
    assert profiled._backend is fresh._backend is stack.runtime_gateway
    reader = gate.loader._preparation_reader
    assert isinstance(reader, MethodType)
    assert reader.__self__ is stack.runtime_gateway
    assert profiled._review_gate is None  # default/old profile takes the real None path
    assert id(gate.repository) == id(f.broker.review_repository)
    results = []

    class Runtime(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            results.append(
                await profiled.invoke(
                    context.tenant_id,
                    f.s.request,
                    user_id=context.actor_id,
                    role=context.actor_role,
                )
            )
            results.append(
                await profiled.invoke(
                    context.tenant_id,
                    replace(f.s.request, actor="repair_agent", idempotency_key="repair"),
                    user_id=context.actor_id,
                    role=context.actor_role,
                )
            )
            yield RunEvent(
                kind="runtime.proof",
                sequence=1,
                run_id=context.run_id,
                payload={"proof": "actual-write"},
            )

    async with worker_runtime_events(
        Runtime(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
    ) as events:
        await anext(events)
    assert results[0].status == "succeeded", results[0]
    assert results[1].status == "failed"
    assert len(f.state.calls) == 1
    assert not f.state.calls[0].get("tools")
    assert [
        item.path
        for item in ProjectWorkspaceStore(tmp_path).list_files(
            f.s.task.tenant_id, "demo", "session"
        )
    ] == ["hello.txt"]
    stored_events = [row for row in f.db.rows if type(row) is RunReviewEventRow]
    assert sum(row.kind == "consumed" for row in stored_events) == 1


@pytest.mark.parametrize("change", ["config", "lease", "role", "closed", "deadline"])
async def test_live_revocation_after_yield_prevents_next_physical_call(tmp_path: Path, change: str) -> None:
    from agent_hub.runtime.defaults import _gateway_for_config

    f = await fixture(tmp_path)
    budgets = []

    class Runtime(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            budgets.append(present(_run_budget.get()))
            gateway, _, _ = await _gateway_for_config(
                f.state.platform,
                tenant_id=context.tenant_id,
                secret_service=f.secrets,
                capacity_factory=f.capacity,
                transport=f.broker._transport,
            )
            yield RunEvent(
                kind="runtime.wait", sequence=1, run_id=context.run_id, payload={"waiting": True}
            )
            await gateway.complete(logical_request())

    async with worker_runtime_events(
        Runtime(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
    ) as events:
        await anext(events)
        pool = next(row for row in f.db.rows if isinstance(row, RunAdmissionPoolRow))
        if change == "config":
            f.state.config = ApprovalReviewConfig()
        elif change == "lease":
            f.db.run.worker_lease_token = uuid4()
        elif change == "role":
            f.db.run.actor_role = Role.VIEWER.value
        elif change == "closed":
            pool.closed = True
        else:
            present(pool).original_deadline = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(
            (review_assembly.ReviewAssemblyUnavailable, AdmissionConflict, _PhysicalAdmissionStopped)
        ):
            await anext(events)
    assert not f.state.calls
    assert not budgets[0]._active


async def test_inherited_child_cannot_send_after_owner_stream_exits(tmp_path: Path) -> None:
    from agent_hub.runtime.defaults import _gateway_for_config

    f = await fixture(tmp_path)
    release = asyncio.Event()
    pending = []

    class Runtime(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            gateway, _, _ = await _gateway_for_config(
                f.state.platform,
                tenant_id=context.tenant_id,
                secret_service=f.secrets,
                capacity_factory=f.capacity,
                transport=f.broker._transport,
            )

            async def child() -> None:
                await release.wait()
                await gateway.complete(logical_request())

            pending.append(asyncio.create_task(child()))
            yield RunEvent(
                kind="runtime.wait", sequence=1, run_id=context.run_id, payload={"waiting": True}
            )

    async with worker_runtime_events(
        Runtime(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
    ) as events:
        await anext(events)
    release.set()
    with pytest.raises(_PhysicalAdmissionStopped):
        await pending[0]
    assert not f.state.calls


async def test_request_and_app_state_dictionary_cannot_forge_readiness(tmp_path: Path) -> None:
    from fastapi import FastAPI
    from pydantic import ValidationError
    from starlette.requests import Request

    from agent_hub.api.routers.admin import (
        SystemSettingsRequest,
        SystemSettingsResponse,
        _settings_with_review_runtime_status,
    )

    f = await fixture(tmp_path)
    with pytest.raises(ValidationError):
        SystemSettingsRequest.model_validate({"approval_review_readiness": {"status": "ready", "reason_code": "ready"}})
    app = FastAPI()
    app.state.review_assembly_broker = {"status": "ready"}
    request = Request({"type": "http", "app": app})
    settings = SystemSettingsResponse(approval_review=f.state.config)
    projected = await _settings_with_review_runtime_status(settings, request, f.s.task.tenant_id)
    assert mutable(projected.model_dump()["approval_review_readiness"])["status"] == "not_ready"
    app.state.review_assembly_broker = f.broker
    projected = await _settings_with_review_runtime_status(settings, request, f.s.task.tenant_id)
    assert mutable(projected.model_dump()["approval_review_readiness"])["status"] == "ready"
    assert "_runtime_review_readiness" not in projected.model_dump()


@pytest.mark.parametrize(
    "mode", [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID]
)
async def test_actual_configbacked_factories_use_original_scope_for_all_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: TaskMode
) -> None:
    """Real factories/driver/Gateway; native engine scheduling is a bounded seam."""
    import agent_hub.runtime.autogen.adapter as autogen
    from agent_hub.runtime import defaults
    from agent_hub.runtime.contracts import EventKind

    f = await fixture(tmp_path, mode=mode)
    route: dict[str, object] = {**f.s.task.routing_decision, "requested_permissions": []}
    f.s.task = f.s.task.model_copy(update={"routing_decision": route})
    f.s.runs.record = replace(f.s.runs.record, routing_decision=route)
    f.db.run.routing_decision = route
    seen = []

    class Leaf(StreamRuntimeStub):
        def __init__(self, gateway: ModelGateway, *args: object, **options: object) -> None:
            self.mode = mode
            self.gateway = gateway
            logical_model = options.get("logical_model", "primary")
            assert isinstance(logical_model, str)
            self.logical_model = logical_model

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            seen.append(present(_run_budget.get()))
            await self.gateway.complete(logical_request(logical_model=self.logical_model))
            yield RunEvent(
                kind=EventKind.RUNTIME_COMPLETED,
                sequence=1,
                run_id=context.run_id,
                reason="fixed-leaf",
            )

        async def cancel(self) -> None:
            pass

    class Composite(StreamRuntimeStub):
        def __init__(self, *leaves: ExecutionRuntime, **options: object) -> None:
            self.mode = mode
            self.leaves = leaves

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            for child in self.leaves:
                async for event in child.run(context):
                    yield event

        async def cancel(self) -> None:
            pass

    monkeypatch.setattr(defaults, "DirectRuntime", Leaf)
    monkeypatch.setattr(defaults, "CrewDispatchRuntime", Leaf)
    monkeypatch.setattr(autogen, "AutoGenDiscussionRuntime", Leaf)
    monkeypatch.setattr(defaults, "HybridRuntime", Composite)
    cls = {
        TaskMode.DIRECT: defaults.ConfigBackedDirectRuntime,
        TaskMode.DISPATCH: defaults.ConfigBackedDispatchRuntime,
        TaskMode.DISCUSS: defaults.ConfigBackedDiscussionRuntime,
        TaskMode.HYBRID: defaults.ConfigBackedHybridRuntime,
    }[mode]
    runtime = cls(
        config_service=f.config_service,
        secret_service=f.secrets,
        capacity_factory=f.capacity,
        transport=f.broker._transport,
    )

    async with worker_runtime_events(
        runtime, f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
    ) as events:
        collected = [event async for event in events]
    assert collected and seen and all(context is seen[0] for context in seen)
    assert len(f.state.calls) == (3 if mode is TaskMode.HYBRID else 1)
    pool = await RunAdmissionRepository(f.db).get_pool(f.s.task.tenant_id, f.s.task.run_id)
    assert present(pool).token_charged == 5 * len(f.state.calls)
    assert present(pool).original_token_grant == 40000
    assert not seen[0]._active


async def test_nested_child_exit_revokes_escaped_work_without_revoking_original_run(tmp_path: Path) -> None:
    from agent_hub.runtime.defaults import _gateway_for_config

    f = await fixture(tmp_path)
    release = asyncio.Event()
    pending = []

    class Parent(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            gateway, _, _ = await _gateway_for_config(
                f.state.platform,
                tenant_id=context.tenant_id,
                secret_service=f.secrets,
                capacity_factory=f.capacity,
                transport=f.broker._transport,
            )

            class Child(StreamRuntimeStub):
                async def run(self, child_context: TaskContext) -> AsyncIterator[RunEvent]:
                    async def escaped() -> None:
                        await release.wait()
                        await gateway.complete(logical_request())

                    pending.append(asyncio.create_task(escaped()))
                    yield RunEvent(
                        kind="runtime.child",
                        sequence=1,
                        run_id=context.run_id,
                        payload={"child": True},
                    )

            async with worker_runtime_events(
                Child(), context, worker_id=f.s.budget.lease.worker_id
            ) as child:
                await anext(child)
            assert present(_run_budget.get())._active
            release.set()
            with pytest.raises(_PhysicalAdmissionStopped):
                await pending[0]
            await gateway.complete(logical_request())  # original owner remains usable
            yield RunEvent(
                kind="runtime.parent", sequence=1, run_id=context.run_id, payload={"parent": True}
            )

    async with worker_runtime_events(
        Parent(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
    ) as events:
        await anext(events)
    assert len(f.state.calls) == 1


async def test_internal_derived_run_cannot_drop_parent_profile_to_escape_budget(tmp_path: Path) -> None:
    f = await fixture(tmp_path)
    assert review_assembly._execution.get() is None  # independent worker, no inherited frame
    parent = f.s.runs.record
    route = {
        key: value
        for key, value in f.s.task.routing_decision.items()
        if key != "approval_review_profile"
    }
    route.update(source="self_repair", source_run_id=str(parent.id))
    derived = f.s.task.model_copy(
        update={"run_id": uuid4(), "execution_token": uuid4(), "routing_decision": route}
    )
    original_lease = f.db.run.worker_lease_token
    f.db.run.version = 1
    row = RunRow(
        id=derived.run_id,
        tenant_id=derived.tenant_id,
        actor_id=derived.actor_id,
        actor_role=present(derived.actor_role).value,
        request=derived.request,
        mode=derived.mode.value,
        status="running",
        version=1,
        created_at=datetime.now(UTC),
        routing_decision=route,
        worker_id="independent-worker",
        worker_lease_token=derived.execution_token,
        worker_lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    f.db.add(row)
    pytest.MonkeyPatch().setattr(f.s.runs, "get", RunRepository(f.db).get)  # actual persisted current and parent reads

    class MustNotDelegate(StreamRuntimeStub):
        def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            pytest.fail("derived worker must never enter an unbudgeted runtime")

    async def rejected() -> None:
        with pytest.raises(
            review_assembly.ReviewAssemblyUnavailable, match="derived_run_pool_continuity_unsupported"
        ):
            async with worker_runtime_events(
                MustNotDelegate(), derived, worker_id="independent-worker", review_broker=f.broker
            ):
                pytest.fail("profileless derived worker was admitted")

    await rejected()
    # Another repair generation must not hide the original enrolled ancestor.
    intermediate_id = uuid4()
    middle = RunRow(
        id=intermediate_id,
        tenant_id=derived.tenant_id,
        actor_id=derived.actor_id,
        actor_role=present(derived.actor_role).value,
        request=derived.request,
        mode=derived.mode.value,
        status="failed",
        version=1,
        created_at=datetime.now(UTC),
        routing_decision=route,
    )
    f.db.add(middle)
    row.routing_decision = {**route, "source_run_id": str(intermediate_id)}
    derived = derived.model_copy(update={"routing_decision": row.routing_decision})
    await rejected()
    # Existing non-review parent chains retain the legacy None path.
    f.db.run.routing_decision = {
        key: value
        for key, value in present(f.db.run.routing_decision).items()
        if key != "approval_review_profile"
    }
    assert await f.broker.prepare(derived, worker_id="independent-worker") is None
    assert f.db.run.worker_lease_token == original_lease != derived.execution_token
    assert await RunAdmissionRepository(f.db).get_pool(derived.tenant_id, derived.run_id) is None
    assert not f.state.calls


async def test_readiness_requires_review_budget_for_actual_conservative_hold(tmp_path: Path) -> None:
    f = await fixture(tmp_path)
    f.state.config = replace(f.state.config, max_cost_usd_per_run="0.000001")
    readiness = await f.broker.readiness(f.s.task.tenant_id)
    assert readiness.status == "not_ready"
    assert readiness.reason_code == "review_budget_insufficient"


@pytest.mark.parametrize(
    ("phase", "revocation"),
    [
        (phase, revocation)
        for phase in ("before_consume", "inside_consume", "before_dispatch")
        for revocation in ("removed", "expired", "catalog")
    ]
    + [
        ("before_dispatch", "unchanged"),
        ("during_dispatch", "removed"),
        ("during_dispatch", "expired"),
    ],
)
async def test_assembled_allow_revocation_fences_consume_and_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, revocation: object
) -> None:
    """Actual worker/gate/SDK/ledger; only the awaited race is controlled."""
    from agent_hub.capabilities.defaults import build_runtime_capability_stack
    from agent_hub.capabilities.review_context import _ConcreteReviewCall
    from agent_hub.capabilities.review_repository import ReviewRepository
    from agent_hub.db.models import (
        RunAdmissionAttemptRow,
        RunAdmissionPoolRow,
        RunReviewEventRow,
        RunReviewIntentRow,
    )
    from agent_hub.files.workspace import ProjectWorkspaceStore

    a = review_assembly
    f = await fixture(tmp_path)
    f.state.review = True
    document = evidence_document(f)
    path = tmp_path / "activation.json"
    body = json.dumps(document).encode()
    path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    f.broker._evidence_loader = lambda: a.load_server_review_evidence(path, expected_sha256=digest)
    stack = a.assemble_review_capability_stack(
        f.broker,
        build_runtime_capability_stack,
        tenant_id=f.s.task.tenant_id,
        run_repository=f.s.runs,
        skill_store_dir=tmp_path / "skills",
        workspace_root=tmp_path,
        generated_artifact_dir=tmp_path / "generated",
        project_workspace_dir=tmp_path,
    )
    assert f.s.gate.dispatch_check is None  # unchanged default for old/non-assembled gates
    assert isinstance(stack.harness_tool_gateway, a._ProfiledHarness)
    gate = stack.harness_tool_gateway._fresh._review_gate
    assert gate is not None
    bound_check = gate.dispatch_check
    assert isinstance(bound_check, MethodType)
    assert bound_check.__self__ is f.broker
    executions = []
    execute = stack.runtime_gateway.execute

    async def actual_execute(**kwargs: Unpack[ExecuteCall]) -> Mapping[str, JsonValue]:
        executions.append(kwargs)
        return await execute(**kwargs)

    monkeypatch.setattr(stack.runtime_gateway, "execute", actual_execute)
    paused, resume = asyncio.Event(), asyncio.Event()
    original_receipt = []
    outcomes = []

    async def pause() -> None:
        row = next(row for row in f.db.rows if type(row) is RunReviewIntentRow)
        assert row.status == "allow" and row.private_receipt is not None
        attempt = next(row for row in f.db.rows if type(row) is RunAdmissionAttemptRow)
        assert attempt.state == "completed" and attempt.receipt_sha256
        pool = next(row for row in f.db.rows if type(row) is RunAdmissionPoolRow)
        original_receipt.append(
            (row.private_receipt, attempt.receipt_sha256, present(pool).original_deadline)
        )
        paused.set()
        await resume.wait()

    if phase == "before_consume":
        consume = f.broker.review_repository.consume_permit

        async def before(**kwargs: Unpack[ConsumeKeywords]) -> bool:
            await pause()
            result = await consume(**kwargs)
            assert isinstance(result, bool)
            return result

        monkeypatch.setattr(f.broker.review_repository, "consume_permit", before)
    elif phase == "inside_consume":
        base_consume = ReviewRepository.consume_permit

        async def inside(self: ReviewRepository, **kwargs: Unpack[ConsumeKeywords]) -> bool:
            result = await base_consume(self, **kwargs)
            assert result is True
            await pause()
            return result

        monkeypatch.setattr(ReviewRepository, "consume_permit", inside)
    elif phase == "during_dispatch":
        callback = present(gate.dispatch_check)

        async def during_dispatch() -> None:
            # All original budget/compile/DAO checks pass. Revoke while awaiting
            # the callback's LAST catalog read; its sync evidence check must see it.
            budget = present(a._execution.get()).budget
            require = budget.require_current
            get_current = f.broker._configs.get_current
            armed = False

            async def checked() -> None:
                nonlocal armed
                await require()
                armed = True

            async def current(tenant: UUID) -> ConfigRevision | None:
                value = await get_current(tenant)
                if armed:
                    await pause()
                return value

            monkeypatch.setattr(budget, "require_current", checked)
            monkeypatch.setattr(f.broker._configs, "get_current", current)
            try:
                await callback()
            finally:
                monkeypatch.setattr(budget, "require_current", require)
                monkeypatch.setattr(f.broker._configs, "get_current", get_current)

        monkeypatch.setattr(gate, "dispatch_check", during_dispatch)
    else:
        dispatch = _ConcreteReviewCall.before_dispatch

        async def before_dispatch(self: _ConcreteReviewCall) -> bool:
            await pause()
            return await dispatch(self)

        monkeypatch.setattr(_ConcreteReviewCall, "before_dispatch", before_dispatch)

    class Runtime(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            for request in (
                f.s.request,
                replace(f.s.request, actor="repair_agent", idempotency_key="repeat"),
            ):
                outcomes.append(
                    await stack.harness_tool_gateway.invoke(
                        context.tenant_id,
                        request,
                        user_id=context.actor_id,
                        role=context.actor_role,
                    )
                )
            yield RunEvent(
                kind="runtime.checked", sequence=1, run_id=context.run_id, payload={"checked": True}
            )

    async def drive() -> list[RunEvent]:
        async with worker_runtime_events(
            Runtime(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
        ) as events:
            return [event async for event in events]

    task = asyncio.create_task(drive())
    try:
        await asyncio.wait_for(paused.wait(), timeout=10)
        assert len(f.state.calls) == 1
        if revocation == "removed":
            path.unlink()
        elif revocation == "expired":
            expiry_text = document["valid_until"]
            assert isinstance(expiry_text, str)
            expiry = datetime.fromisoformat(expiry_text)

            class Expired(datetime):
                @classmethod
                def now(cls, tz: tzinfo | None = None) -> Expired:
                    return cls.fromisoformat((expiry + timedelta(seconds=1)).isoformat())

            monkeypatch.setattr(a, "datetime", Expired)
        elif revocation == "catalog":
            f.state.version += 1  # approval config deliberately unchanged
        resume.set()
        await asyncio.wait_for(task, timeout=10)
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(outcomes) == 2
    files = ProjectWorkspaceStore(tmp_path).list_files(f.s.task.tenant_id, "demo", "session")
    if revocation == "unchanged":
        assert [result.status for result in outcomes] == ["succeeded", "failed"]
        assert [file.path for file in files] == ["hello.txt"]
        assert len(executions) == 1
    else:
        assert all(result.status == "failed" for result in outcomes) and not files
        assert not executions
    assert len(f.state.calls) == 1  # no second reviewer, fallback or repair RPC
    row = next(row for row in f.db.rows if type(row) is RunReviewIntentRow)
    attempt = next(row for row in f.db.rows if type(row) is RunAdmissionAttemptRow)
    pool = next(row for row in f.db.rows if type(row) is RunAdmissionPoolRow)
    assert (
        row.private_receipt,
        attempt.receipt_sha256,
        present(pool).original_deadline,
    ) == original_receipt[0]
    assert attempt.state == "completed" and present(pool).token_charged == 5
    assert pool.review_count_charged == 1 and present(pool).original_token_grant == 40000
    consumed = sum(type(row) is RunReviewEventRow and row.kind == "consumed" for row in f.db.rows)
    assert consumed == (0 if phase == "before_consume" else 1)


@pytest.mark.parametrize(
    "relative",
    [
        "capabilities/action_preparation.py",
        "files/workspace.py",
        "runtime/streams.py",
        "capabilities/python_resolution_preparation.py",
        "skills/sandbox/python_resolution_protocol.py",
        "skills/sandbox/python_resolution_store.py",
        "skills/sandbox/python_resolution_worker.py",
        "capabilities/python_dependency_resolution.py",
        "skills/sandbox/python_registry_protocol.py",
        "skills/sandbox/python_registry_read.py",
        "skills/sandbox/python_input_fetch.py",
        "skills/sandbox/python_dependency_metadata.py",
        "skills/sandbox/broker.py",
        "skills/sandbox/python_input_supply_catalog.py",
        "skills/sandbox/python_input_supply_worker.py",
        "skills/sandbox/python_input_publish.py",
        "skills/sandbox/python_dependency_index_store.py",
        "skills/sandbox/python_metadata_plan_protocol.py",
        "skills/sandbox/python_metadata_planning.py",
        "skills/sandbox/python_metadata_preparation_worker.py",
        "skills/sandbox/python_metadata_preparation_client.py",
        "skills/sandbox/project_contract_broker.py",
        "skills/sandbox/python_project_backend.py",
        "skills/sandbox/project.py",
        "capabilities/generated_project_service.py",
        "capabilities/python_metadata_preparation.py",
        "security/network.py",
        "capabilities/tools/http_read.py",
        "skills/sandbox/python_registry_fetch.py",
        "skills/sandbox/python_metadata_preparation_broker.py",
        "skills/sandbox/python_input_supply_broker.py",
        "skills/sandbox/python_project_broker.py",
        "skills/sandbox/base.py",
        "subprocess_cleanup.py",
        "skills/sandbox/python_resolution_overlay.py",
        "capabilities/python_generated_service.py",
        "capabilities/python_input_preparation.py",
        "runtime/generated_project_guidance.py",
        "skills/sandbox/python_project_profile.py",
        "skills/sandbox/python_dependency_inspection.py",
        "runtime/artifacts.py",
        "runs/artifacts.py",
        "runs/automatic_project_inputs.py",
        "capabilities/python_project_service.py",
        "capabilities/python_project_lifecycle.py",
        "runtime/request_admission.py",
        "runtime/crew/context_window.py",
        "runtime/autogen/context_budget.py",
    ],
)
async def test_actual_proof_dependency_change_invalidates_installed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    from pathlib import Path

    a = review_assembly
    f = await fixture(tmp_path)
    original = a.runtime_source_fingerprint()
    document = evidence_document(f)
    path = tmp_path / "activation.json"
    body = json.dumps(document).encode()
    path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    a.load_server_review_evidence(path, expected_sha256=digest)
    target = Path(a.__file__).resolve().parents[1] / relative
    read = Path.read_bytes

    def changed(self: Path) -> bytes:
        data = read(self)
        return data + b"\n# virtual dependency change\n" if self.resolve() == target else data

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert a.runtime_source_fingerprint() != original
    with pytest.raises(ValueError, match="invalid server review evidence"):
        a.load_server_review_evidence(path, expected_sha256=digest)
