"""Worker assembly proofs with real repository code and fixed HTTP only."""

import asyncio
import hashlib
import importlib
import json
import time
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.config.schema import PlatformConfig
from agent_hub.db.models import RunInputAuthorizationRow, RunRow
from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import ModelGatewayError
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.physical_admission import _PhysicalAdmissionStopped
from agent_hub.models.physical_budget import _run_budget
from agent_hub.models.registry import NoCapableDeployment
from agent_hub.models.run_admission_repository import AdmissionConflict, RunAdmissionRepository
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.contracts import RunEvent
from agent_hub.runtime.worker_execution import worker_runtime_events
from tests.unit.capabilities.test_concrete_model_review import FocusedDB
from tests.unit.harness.test_review_dispatch import setup
from tests.unit.models.test_gateway import CapacityStub, lease
from tests.unit.models.test_physical_budget import logical_request, response


def assembly():
    return importlib.import_module("agent_hub.runtime.review_assembly")


class AssemblyDB(FocusedDB):
    async def execute(self, statement):
        if statement.column_descriptions[0].get("entity") is RunRow:
            link = next(r for r in self.rows if type(r) is RunInputAuthorizationRow)
            turn = next(
                r
                for r in self.rows
                if getattr(r, "turn_id", None) == link.turn_id and hasattr(r, "message")
            )
            return SimpleNamespace(one_or_none=lambda: (self.run, link, turn))
        return await super().execute(statement)


async def fixture(tmp_path, *, mode=TaskMode.DIRECT, contracts=True, verified=True):
    s = setup(tmp_path)
    a = assembly()
    db = AssemblyDB(s)
    # A first execution starts without a pool; the DAO, not the test, opens it.
    db.rows.remove(db.pool)
    db.run.created_at = datetime.now(UTC)
    db.run.mode = mode.value
    s.runs.record = replace(s.runs.record, created_at=db.run.created_at, mode=mode)
    s.task = s.task.model_copy(update={"mode": mode, "token_budget": 123})
    actual_runs = RunRepository(db)
    s.runs.load_authenticated_input = actual_runs.load_authenticated_input
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
    state = SimpleNamespace(config=s.settings.config, platform=platform, version=7, calls=[])

    class Configs:
        async def get_current(self, tenant):
            assert tenant == s.task.tenant_id
            return SimpleNamespace(
                version=state.version, document=state.platform.model_dump(mode="json")
            )

    class Secrets:
        async def resolve(self, tenant, ref):
            return "fixed-test-secret"

        async def fingerprint(self, tenant, ref):
            return "fixed-test-fingerprint"

    async def config(tenant):
        return state.config

    async def capacity(tenant, deployments):
        result = CapacityStub([])

        async def acquire(candidates, wait_timeout, *, estimated_tokens):
            return lease(candidates[0].id, candidates[0].quota_scope_id)

        result.acquire = acquire
        return result

    def handler(request):
        payload = json.loads(request.content)
        state.calls.append(payload)
        assert _run_budget.get() is not None
        assert any(getattr(row, "original_token_grant", None) == 40000 for row in db.rows)
        if getattr(state, "review", False):
            action = json.loads(payload["messages"][-1]["content"])
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
        config_service=Configs(),
        config_getter=config,
        run_repository=s.runs,
        session_factory=db,
        secret_service=Secrets(),
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
    return SimpleNamespace(
        s=s,
        db=db,
        broker=broker,
        state=state,
        config_service=Configs(),
        secrets=Secrets(),
        capacity=capacity,
    )


def test_broker_is_a_real_server_assembly():
    assert callable(assembly().ReviewAssemblyBroker)


@pytest.mark.parametrize(
    "mode", [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID]
)
async def test_worker_builds_pool_before_lazy_runtime_and_retains_until_close(tmp_path, mode):
    a = assembly()
    f = await fixture(tmp_path, mode=mode)
    seen, child_scopes = [], []

    class Runtime:
        def run(self, context):
            seen.append(_run_budget.get())  # synchronous/lazy creation boundary
            assert a.current_review_platform(context.tenant_id) is not None

            async def events():
                from agent_hub.runtime.defaults import _gateway_for_config

                budget = _run_budget.get()
                assert budget is seen[0]
                assert budget.lease.token == context.execution_token
                assert budget._pool.original_token_grant == 40000  # soft 123 is not a hard grant
                gateway, _, _ = await _gateway_for_config(
                    a.current_review_platform(context.tenant_id),
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

                    async def child():
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
                    seen.append(_run_budget.get())
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
    assert pool.token_charged == 15
    assert pool.original_deadline == f.s.runs.record.created_at + timedelta(seconds=60)


async def test_same_run_reentry_and_unknown_never_refill_or_extend(tmp_path):
    f = await fixture(tmp_path)
    first = await f.broker.prepare(f.s.task, worker_id=f.s.budget.lease.worker_id)
    pool_row = next(row for row in f.db.rows if hasattr(row, "original_deadline"))
    pool_row.token_charged = 18000  # retained UNKNOWN debit in the actual stored pool
    deadline = first.budget._pool.original_deadline
    second = await f.broker.prepare(
        f.s.task.model_copy(update={"token_budget": 999999}), worker_id=f.s.budget.lease.worker_id
    )
    assert second.budget._pool.tokens_remaining == 22000
    assert second.budget._pool.original_deadline == deadline
    assert second.budget.deadline <= time.monotonic() + 60


async def test_old_profile_is_never_upgraded_even_when_configuration_ready(tmp_path):
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
async def test_declared_context_is_not_provider_contract(tmp_path, contracts, verified, reason):
    f = await fixture(tmp_path, contracts=contracts, verified=verified)
    readiness = await f.broker.readiness(f.s.task.tenant_id)
    assert readiness.status == "not_ready" and readiness.reason_code == reason
    assert not readiness.supported_scope
    assert not f.state.calls


async def test_rules_default_produces_no_profile_or_pool(tmp_path):
    f = await fixture(tmp_path)
    f.state.config = ApprovalReviewConfig()
    assert await f.broker.submission_profile(f.s.task.tenant_id, timeout_seconds=60) == {}
    assert (await f.broker.readiness(f.s.task.tenant_id)).reason_code == "rules_only"
    assert not f.state.calls


@pytest.mark.parametrize("change", ["worker", "role", "config", "catalog", "derived"])
async def test_changed_worker_config_role_and_derived_run_fail_closed(tmp_path, change):
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
    with pytest.raises(assembly().ReviewAssemblyUnavailable):
        await f.broker.prepare(context, worker_id=f.s.budget.lease.worker_id)
    assert not f.state.calls


async def test_runtime_local_context_token_survives_yields_without_leaking(tmp_path):
    f = await fixture(tmp_path)
    local = ContextVar("runtime_local", default="outside")

    class Runtime:
        async def run(self, context):
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
async def test_actual_adapter_stream_retains_one_consumer_task(tmp_path, mode, advances):
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
            owner = owner or actual._owner
            assert actual._owner is owner and not owner.done()
            assert owner is not asyncio.current_task()
        assert _run_budget.get() is None
    assert owner is None or owner.done()
    assert actual._closed and runtime._active_stream is None
    assert not (asyncio.all_tasks() - before)


async def test_worker_owner_cancellation_drains_same_task_finalizer_without_prefetch(tmp_path):
    f = await fixture(tmp_path)
    entered, finalizing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed, counts = [], []
    local = ContextVar("owner_local", default="outside")
    before = asyncio.all_tasks()

    class Runtime:
        async def run(self, context):
            token = local.set("inside")
            owner, budget = asyncio.current_task(), _run_budget.get()
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
                await asyncio.Event().wait()
            finally:
                assert asyncio.current_task() is owner and local.get() == "inside"
                finalizing.set()
                await release.wait()
                assert _run_budget.get() is budget and budget._active
                local.reset(token)

    async def driver():
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
async def test_worker_scope_failure_closes_and_revokes_without_background_task(tmp_path, phase):
    f = await fixture(tmp_path)
    seen = []
    before = asyncio.all_tasks()

    class Runtime:
        def run(self, context):
            seen.append(_run_budget.get())
            if phase == "construct":
                raise ValueError("construct")

            async def events():
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


def evidence_document(f):
    a = assembly()
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


async def test_installable_server_evidence_loader_is_used_by_real_builder(tmp_path, monkeypatch):
    a = assembly()
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
async def test_server_evidence_rejects_stale_incomplete_or_modified_records(tmp_path, invalid):
    a = assembly()
    f = await fixture(tmp_path)
    document = evidence_document(f)
    if invalid == "expired":
        document["valid_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif invalid == "source":
        document["worker_verification"]["source_sha256"] = "0" * 64
    elif invalid == "checks":
        document["worker_verification"]["checks"] = ["original_pool"]
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


async def test_unused_untrusted_catalog_deployment_does_not_block_trusted_subset(tmp_path):
    a = assembly()
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

    class Runtime:
        async def run(self, context):
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


async def test_actual_stack_broker_reviewer_repository_and_backend_execute_once(tmp_path):
    from agent_hub.capabilities.defaults import build_runtime_capability_stack
    from agent_hub.db.models import RunReviewEventRow
    from agent_hub.files.workspace import ProjectWorkspaceStore

    a = assembly()
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
    fresh = profiled._fresh
    gate = fresh._review_gate
    assert profiled._backend is fresh._backend is stack.runtime_gateway
    assert gate.loader._preparation_reader.__self__ is stack.runtime_gateway
    assert profiled._review_gate is None  # default/old profile takes the real None path
    assert gate.repository is f.broker.review_repository
    results = []

    class Runtime:
        async def run(self, context):
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
async def test_live_revocation_after_yield_prevents_next_physical_call(tmp_path, change):
    from agent_hub.runtime.defaults import _gateway_for_config

    f = await fixture(tmp_path)
    budgets = []

    class Runtime:
        async def run(self, context):
            budgets.append(_run_budget.get())
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
        pool = next(row for row in f.db.rows if hasattr(row, "original_deadline"))
        if change == "config":
            f.state.config = ApprovalReviewConfig()
        elif change == "lease":
            f.db.run.worker_lease_token = uuid4()
        elif change == "role":
            f.db.run.actor_role = Role.VIEWER.value
        elif change == "closed":
            pool.closed = True
        else:
            pool.original_deadline = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(
            (assembly().ReviewAssemblyUnavailable, AdmissionConflict, _PhysicalAdmissionStopped)
        ):
            await anext(events)
    assert not f.state.calls
    assert not budgets[0]._active


async def test_inherited_child_cannot_send_after_owner_stream_exits(tmp_path):
    from agent_hub.runtime.defaults import _gateway_for_config

    f = await fixture(tmp_path)
    release = asyncio.Event()
    pending = []

    class Runtime:
        async def run(self, context):
            gateway, _, _ = await _gateway_for_config(
                f.state.platform,
                tenant_id=context.tenant_id,
                secret_service=f.secrets,
                capacity_factory=f.capacity,
                transport=f.broker._transport,
            )

            async def child():
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


async def test_request_and_app_state_dictionary_cannot_forge_readiness(tmp_path):
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
        SystemSettingsRequest(approval_review_readiness={"status": "ready", "reason_code": "ready"})
    app = FastAPI()
    app.state.review_assembly_broker = {"status": "ready"}
    request = Request({"type": "http", "app": app})
    settings = SystemSettingsResponse(approval_review=f.state.config)
    projected = await _settings_with_review_runtime_status(settings, request, f.s.task.tenant_id)
    assert projected.approval_review_readiness.status == "not_ready"
    app.state.review_assembly_broker = f.broker
    projected = await _settings_with_review_runtime_status(settings, request, f.s.task.tenant_id)
    assert projected.approval_review_readiness.status == "ready"
    assert "_runtime_review_readiness" not in projected.model_dump()


@pytest.mark.parametrize(
    "mode", [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID]
)
async def test_actual_configbacked_factories_use_original_scope_for_all_leaves(
    tmp_path, monkeypatch, mode
):
    """Real factories/driver/Gateway; native engine scheduling is a bounded seam."""
    import agent_hub.runtime.autogen.adapter as autogen
    from agent_hub.runtime import defaults
    from agent_hub.runtime.contracts import EventKind

    f = await fixture(tmp_path, mode=mode)
    route = {**f.s.task.routing_decision, "requested_permissions": []}
    f.s.task = f.s.task.model_copy(update={"routing_decision": route})
    f.s.runs.record = replace(f.s.runs.record, routing_decision=route)
    f.db.run.routing_decision = route
    seen = []

    class Leaf:
        def __init__(self, gateway, *args, **options):
            self.mode = mode
            self.gateway = gateway
            self.logical_model = options.get("logical_model", "primary")

        async def run(self, context):
            seen.append(_run_budget.get())
            await self.gateway.complete(logical_request(logical_model=self.logical_model))
            yield RunEvent(
                kind=EventKind.RUNTIME_COMPLETED,
                sequence=1,
                run_id=context.run_id,
                reason="fixed-leaf",
            )

        async def cancel(self):
            pass

    class Composite:
        def __init__(self, *leaves, **options):
            self.mode = mode
            self.leaves = leaves

        async def run(self, context):
            for child in self.leaves:
                async for event in child.run(context):
                    yield event

        async def cancel(self):
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
    assert pool.token_charged == 5 * len(f.state.calls)
    assert pool.original_token_grant == 40000
    assert not seen[0]._active


async def test_nested_child_exit_revokes_escaped_work_without_revoking_original_run(tmp_path):
    from agent_hub.runtime.defaults import _gateway_for_config

    f = await fixture(tmp_path)
    release = asyncio.Event()
    pending = []

    class Parent:
        async def run(self, context):
            gateway, _, _ = await _gateway_for_config(
                f.state.platform,
                tenant_id=context.tenant_id,
                secret_service=f.secrets,
                capacity_factory=f.capacity,
                transport=f.broker._transport,
            )

            class Child:
                async def run(self, child_context):
                    async def escaped():
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
            assert _run_budget.get()._active
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


async def test_internal_derived_run_cannot_drop_parent_profile_to_escape_budget(tmp_path):
    f = await fixture(tmp_path)
    assert assembly()._execution.get() is None  # independent worker, no inherited frame
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
        actor_role=derived.actor_role.value,
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
    f.s.runs.get = RunRepository(f.db).get  # actual persisted current and parent reads

    class MustNotDelegate:
        def run(self, context):
            pytest.fail("derived worker must never enter an unbudgeted runtime")

    async def rejected():
        with pytest.raises(
            assembly().ReviewAssemblyUnavailable, match="derived_run_pool_continuity_unsupported"
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
        actor_role=derived.actor_role.value,
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
        for key, value in f.db.run.routing_decision.items()
        if key != "approval_review_profile"
    }
    assert await f.broker.prepare(derived, worker_id="independent-worker") is None
    assert f.db.run.worker_lease_token == original_lease != derived.execution_token
    assert await RunAdmissionRepository(f.db).get_pool(derived.tenant_id, derived.run_id) is None
    assert not f.state.calls


async def test_readiness_requires_review_budget_for_actual_conservative_hold(tmp_path):
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
    tmp_path, monkeypatch, phase, revocation
):
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

    a = assembly()
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
    gate = stack.harness_tool_gateway._fresh._review_gate
    assert gate.dispatch_check.__self__ is f.broker
    executions = []
    execute = stack.runtime_gateway.execute

    async def actual_execute(**kwargs):
        executions.append(kwargs)
        return await execute(**kwargs)

    monkeypatch.setattr(stack.runtime_gateway, "execute", actual_execute)
    paused, resume = asyncio.Event(), asyncio.Event()
    original_receipt = []
    outcomes = []

    async def pause():
        row = next(row for row in f.db.rows if type(row) is RunReviewIntentRow)
        assert row.status == "allow" and row.private_receipt is not None
        attempt = next(row for row in f.db.rows if type(row) is RunAdmissionAttemptRow)
        assert attempt.state == "completed" and attempt.receipt_sha256
        pool = next(row for row in f.db.rows if type(row) is RunAdmissionPoolRow)
        original_receipt.append(
            (row.private_receipt, attempt.receipt_sha256, pool.original_deadline)
        )
        paused.set()
        await resume.wait()

    if phase == "before_consume":
        consume = f.broker.review_repository.consume_permit

        async def before(**kwargs):
            await pause()
            return await consume(**kwargs)

        monkeypatch.setattr(f.broker.review_repository, "consume_permit", before)
    elif phase == "inside_consume":
        consume = ReviewRepository.consume_permit

        async def inside(self, **kwargs):
            result = await consume(self, **kwargs)
            assert result is True
            await pause()
            return result

        monkeypatch.setattr(ReviewRepository, "consume_permit", inside)
    elif phase == "during_dispatch":
        callback = gate.dispatch_check

        async def during_dispatch():
            # All original budget/compile/DAO checks pass. Revoke while awaiting
            # the callback's LAST catalog read; its sync evidence check must see it.
            budget = a._execution.get().budget
            require = budget.require_current
            get_current = f.broker._configs.get_current
            armed = False

            async def checked():
                nonlocal armed
                await require()
                armed = True

            async def current(tenant):
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

        async def before_dispatch(self):
            await pause()
            return await dispatch(self)

        monkeypatch.setattr(_ConcreteReviewCall, "before_dispatch", before_dispatch)

    class Runtime:
        async def run(self, context):
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

    async def drive():
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
            expiry = datetime.fromisoformat(document["valid_until"])

            class Expired(datetime):
                @classmethod
                def now(cls, tz=None):
                    return expiry + timedelta(seconds=1)

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
        pool.original_deadline,
    ) == original_receipt[0]
    assert attempt.state == "completed" and pool.token_charged == 5
    assert pool.review_count_charged == 1 and pool.original_token_grant == 40000
    consumed = sum(type(row) is RunReviewEventRow and row.kind == "consumed" for row in f.db.rows)
    assert consumed == (0 if phase == "before_consume" else 1)


@pytest.mark.parametrize(
    "relative", ["capabilities/action_preparation.py", "files/workspace.py", "runtime/streams.py"]
)
async def test_actual_proof_dependency_change_invalidates_installed_evidence(
    tmp_path, monkeypatch, relative
):
    from pathlib import Path

    a = assembly()
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

    def changed(self):
        data = read(self)
        return data + b"\n# virtual dependency change\n" if self.resolve() == target else data

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert a.runtime_source_fingerprint() != original
    with pytest.raises(ValueError, match="invalid server review evidence"):
        a.load_server_review_evidence(path, expected_sha256=digest)
