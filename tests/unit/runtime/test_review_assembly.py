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
