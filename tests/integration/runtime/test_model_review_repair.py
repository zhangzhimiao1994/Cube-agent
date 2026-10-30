"""A2 selectors: real PG/service/DISCUSS/Root, deterministic SDK HTTP only.

Uses an already migrated, explicitly bound dedicated PG and the existing owned
Redis fixture when native selectors are deliberately executed. Offline selectors
start neither; they are not PG/native acceptance. No SSH/migrations/real provider.
"""

import asyncio
import hashlib
import importlib.util
import json
import shutil
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunOutboxRow,
    RunReviewEventRow,
    RunReviewIntentRow,
    RuntimeArtifactRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.models.run_admission_repository import RunAdmissionRepository
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.self_repair import project_repair_checkpoint_is_safe
from agent_hub.runtime import review_assembly
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.autogen import project_repair_phase
from agent_hub.runtime.contracts import EventKind, RunEvent, _mutable_json
from agent_hub.runtime.review_assembly import ReviewAssemblyUnavailable
from agent_hub.runtime.worker_execution import worker_runtime_events
from tests.integration.capabilities.test_review_repository import (
    database_url as database_url,  # noqa: PLC0414 - explicit dedicated PG fixture
)
from tests.integration.models.test_run_admission import storage as storage  # noqa: PLC0414
from tests.integration.runtime._model_review_repair_fixture import (
    ORIGINAL,
    PATCHED,
    TARGET,
    RepairWire,
    capacity_clean,
    components,
    pg_case,
    projected_worker,
    projection_case,
)
from tests.integration.runtime.test_model_review_modes import (
    own_redis as own_redis,  # noqa: PLC0414
)
from tests.unit.runtime.test_review_assembly import evidence_document
from tests.unit.runtime.test_review_assembly import fixture as offline_assembly
from tests.unit.runtime.test_review_repair_continuity import (
    claim,
    pool_values,
    productive_checkpoint,
    repair_fixture,
    service_runtime_context,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(scope="session")
def migrated_database():
    """Override integration's auto-migration for this module only.

    The PG selectors use storage's explicit identity/URL guards and require the
    parent's existing schema; offline execution must not connect to any database.
    """


async def rows(factory, model, run):
    async with factory() as session:
        return list((await session.scalars(select(model).where(model.run_id == run))).all())


async def accepted_case(storage, tmp_path, redis_client, *, unknown_review=False):
    f = await pg_case(storage, tmp_path, redis_client, unknown_review=unknown_review)
    initial = await f.service("a2-original-worker")
    result = await asyncio.wait_for(initial.service.execute(f.record.id), timeout=75)
    assert result.status is RunStatus.QUEUED, (
        "known stopped failure must really classify/auto-accept"
    )
    accepted = await f.repository.get(f.user.tenant_id, f.record.id)
    assert accepted.routing_decision["source"] == "self_repair"
    assert accepted.routing_decision["self_repair_source_run_id"] == str(f.record.id)
    outbox = await rows(f.factory, RunOutboxRow, f.record.id)
    assert any(":self-repair:" in item.idempotency_key for item in outbox)
    assert len(f.wire.unknown_ids) == 1
    assert initial.root_entries == initial.root_effects == []
    attempts = await rows(f.factory, RunAdmissionAttemptRow, f.record.id)
    unknown = next(item for item in attempts if item.attempt_id in f.wire.unknown_ids)
    assert unknown.state == "unknown" and unknown.token_hold > 0
    f.before = await RunAdmissionRepository(f.factory).get_pool(f.user.tenant_id, f.record.id)
    f.unknown = (unknown.attempt_id, unknown.token_hold, unknown.receipt_sha256)
    f.initial = initial
    return f


async def test_discuss_service_repair_new_diagnosis_review_and_single_root_effect(
    storage, own_redis, tmp_path
):
    f = await accepted_case(storage, tmp_path, own_redis)
    assert (f.root / TARGET).read_text(encoding="utf-8") == ORIGINAL
    repair = await f.service("a2-repair-worker")  # cold actual adapter, same durable DAO facts
    await asyncio.wait_for(repair.service.execute(f.record.id), timeout=75)
    assert f.wire.diagnosed and len(f.wire.reviews) == 1
    assert (f.root / TARGET).read_text(encoding="utf-8") == PATCHED
    assert repair.root_entries == [(f.record.id, {"path": TARGET, "content": PATCHED})]
    assert len(repair.root_effects) == 1
    contexts = f.wire.contexts
    assert len(contexts) == 2 and all(context.run_id == f.record.id for context in contexts)
    assert contexts[0].execution_token != contexts[1].execution_token
    assert contexts[1].routing_decision["source"] == "self_repair"
    assert [name for phase, name, _ in f.wire.tool_calls if phase == "repair"] == [
        "workspace.read",
        "workspace.write_text",
    ]
    intents = await rows(f.factory, RunReviewIntentRow, f.record.id)
    assert len(intents) == 1 and intents[0].status == "allow" and intents[0].consumed
    events = await rows(f.factory, RunReviewEventRow, f.record.id)
    assert sum(item.kind == "consumed" for item in events) == 1
    attempts = await rows(f.factory, RunAdmissionAttemptRow, f.record.id)
    unknown = next(item for item in attempts if item.attempt_id == f.unknown[0])
    assert (unknown.token_hold, unknown.receipt_sha256, unknown.state) == (
        *f.unknown[1:],
        "unknown",
    )
    review = next(item for item in attempts if item.purpose == "review")
    assert review.state == "completed" and review.deployment["deployment_id"] == "reviewer_1"
    assert all(
        sent["deployment_id"] == "primary_1"
        for sent in f.wire.sent.values()
        if sent["purpose"] == "primary"
    )
    assert {item.attempt_id for item in attempts} == set(f.wire.sent)
    assert len({item.physical_transport_id for item in attempts}) == len(attempts)
    for item in attempts:
        assert item.request_sha256 == f.wire.sent[item.attempt_id]["request_sha256"]
        assert item.physical_transport_id == f.wire.sent[item.attempt_id]["physical_id"]
    after = await RunAdmissionRepository(f.factory).get_pool(f.user.tenant_id, f.record.id)
    assert (after.authorization_turn_id, after.original_token_grant, after.original_deadline) == (
        f.before.authorization_turn_id,
        f.before.original_token_grant,
        f.before.original_deadline,
    )
    assert after.token_charged == sum(
        item.token_hold if item.state == "unknown" else item.receipt["usage"]["total_tokens"]
        for item in attempts
    )
    assert f.wire.bodies and all(body.closed for body in f.wire.bodies)
    await capacity_clean(f)


async def test_discuss_repair_unknown_review_never_executes_or_replays(
    storage, own_redis, tmp_path
):
    f = await accepted_case(storage, tmp_path, own_redis, unknown_review=True)
    repair = await f.service("a2-repair-worker")
    result = await asyncio.wait_for(repair.service.execute(f.record.id), timeout=75)
    assert result.status is RunStatus.WAITING_APPROVAL
    assert f.wire.diagnosed and len(f.wire.reviews) == 1
    assert (f.root / TARGET).read_text(encoding="utf-8") == ORIGINAL
    assert repair.root_entries == repair.root_effects == []
    intents = await rows(f.factory, RunReviewIntentRow, f.record.id)
    assert len(intents) == 1 and intents[0].status == "unknown" and not intents[0].consumed
    before = len(f.wire.calls)
    retry = await f.service("a2-retry-worker")
    await asyncio.wait_for(retry.service.execute(f.record.id), timeout=75)
    assert len(f.wire.calls) == before and len(f.wire.reviews) == 1
    assert (f.root / TARGET).read_text(encoding="utf-8") == ORIGINAL
    attempts = await rows(f.factory, RunAdmissionAttemptRow, f.record.id)
    assert len([item for item in attempts if item.purpose == "review"]) == 1
    assert len([item for item in attempts if item.state == "unknown"]) == 2
    assert retry.root_entries == retry.root_effects == []
    await capacity_clean(f)


async def test_discuss_sdk_stopped_checkpoint_protocol_without_pg(tmp_path):
    """Actual adapter/SDK/Root diagnostic only; no claim of PG/service coverage."""
    f = await offline_assembly(tmp_path, mode=TaskMode.DISCUSS)
    route = {
        **f.s.task.routing_decision,
        "engineering_auto_repair_version": 1,
        "engineering_delivery_contract_version": 1,
        "engineering_checkpoint_ack_version": 1,
        "engineering_final_report_version": 1,
    }
    f.s.task = f.s.task.model_copy(update={"routing_decision": route})
    f.s.runs.record = replace(f.s.runs.record, routing_decision=route)
    f.db.run.routing_decision = route
    root = ProjectWorkspaceStore(tmp_path).session_root(f.s.task.tenant_id, "demo", "session")
    root.mkdir(parents=True, exist_ok=True)
    (root / TARGET).write_text(ORIGINAL, encoding="utf-8")
    f.broker._a2_tenant = f.s.task.tenant_id
    case = await components(
        tmp_path,
        f.s.runs,
        InMemoryArtifactRepository(),
        f.broker,
        f.state.platform,
        f.config_service,
        f.secrets,
        f.capacity,
        RepairWire(),
    )
    events = []
    async with worker_runtime_events(
        case.runtime, f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
    ) as stream:
        async for event in stream:
            events.append(event)
    assert any(event.kind is EventKind.RUNTIME_FAILED for event in events)
    checkpoint = case.runtime.last_checkpoint
    assert checkpoint is not None, (
        "runtime fault must occur after a real ACKed observation boundary; "
        f"events={[(event.kind.value, event.reason) for event in events]}; "
        f"calls={len(case.wire.calls)}; tools={case.wire.tool_calls}"
    )
    assert project_repair_checkpoint_is_safe(
        checkpoint,
        routing_decision=route,
        run_id=f.s.task.run_id,
        tenant_id=f.s.task.tenant_id,
        mode=TaskMode.DISCUSS,
    )
    assert len(case.wire.calls) >= 2
    assert case.wire.tool_calls == [("initial", "workspace.read", {"path": TARGET})]
    assert not case.wire.reviews
    assert len(case.wire.unknown_ids) == 1
    unknown = next(
        row
        for row in f.db.rows
        if type(row) is RunAdmissionAttemptRow and row.attempt_id in case.wire.unknown_ids
    )
    assert unknown.state == "unknown" and unknown.token_hold > 0
    assert case.root_entries == case.root_effects == []
    assert (root / TARGET).read_text(encoding="utf-8") == ORIGINAL


async def test_service_window_projection_preserves_actual_repair_phase_without_pg(tmp_path):
    """A2's next production gate, after A1 has admitted the actual service projection."""
    f = await repair_fixture(tmp_path)
    original = await claim(f)
    context = await service_runtime_context(f, original, window=10000)
    assert await f.f.broker.prepare(context, worker_id="repair-worker") is not None
    checkpoint = context.checkpoint
    assert project_repair_phase.prepare(
        original, checkpoint, checkpoint.state["model_ledger"], checkpoint.state["tool_ledger"]
    )

    async def check(actual):
        return prepare_projection_phase(actual, checkpoint)

    assert await projected_worker(f, context, check)


async def test_projected_phase_keeps_original_scope_through_productive_and_cold_resume_without_pg(
    tmp_path,
):
    f = await repair_fixture(tmp_path)
    original = await claim(f)
    runtime = await service_runtime_context(f, original, window=10000)
    expected = original.routing_decision["repair_proposal"]["automatic_scope_sha256"]
    assert project_repair_phase.authority_digest(original.routing_decision) == expected
    assert project_repair_phase.authority_digest(runtime.routing_decision) != expected

    async def work(actual):
        checkpoint = await productive_checkpoint(f, actual)
        artifacts = tuple(
            PostgresArtifactRepository._hydrate(row)
            for row in f.db.rows
            if type(row) is RuntimeArtifactRow
        )
        return checkpoint, project_repair_phase.productive_boundary(actual, checkpoint, artifacts)

    checkpoint, before = await projected_worker(f, runtime, work)
    phase = checkpoint.state[project_repair_phase.PHASE_KEY]
    assert phase["source_scope_sha256"] == expected
    artifacts = tuple(
        PostgresArtifactRepository._hydrate(row)
        for row in f.db.rows
        if type(row) is RuntimeArtifactRow
    )
    persisted = original.model_copy(update={"execution_token": None})
    assert project_repair_phase.productive_boundary(persisted, checkpoint, artifacts) == before
    f.runs.clear_worker_lease(f.db.run)
    f.db.run.status = "failed"
    continued = await f.runs.continue_project_repair_and_enqueue(
        tenant_id=original.tenant_id,
        run_id=original.run_id,
        version=f.db.run.version,
        checkpoint_sha256=checkpoint.state_sha256,
    )
    assert "main_agent_context_window_tokens" not in continued.routing_decision
    cold = await service_runtime_context(f, await claim(f), window=20000)
    assert cold.execution_token != original.execution_token
    assert await f.f.broker.prepare(cold, worker_id="repair-worker") is not None

    async def restored(actual):
        return project_repair_phase.validate(
            phase,
            context=actual,
            models=checkpoint.state["model_ledger"],
            tools=checkpoint.state["tool_ledger"],
            tool_prefix=checkpoint.state["engineering_tool_prefix"],
            state=checkpoint.state,
            artifacts=artifacts,
        )["source_scope_sha256"]

    assert await projected_worker(f, cold, restored) == expected
    assert pool_values(f.db) == f.before


@pytest.mark.parametrize("value", [None, True, 0, -1, "10000", 10000.0, 10000001])
async def test_invalid_runtime_window_cannot_match_original_phase_scope_without_pg(tmp_path, value):
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    context = context.model_copy(
        update={
            "routing_decision": {
                **context.routing_decision,
                "main_agent_context_window_tokens": value,
            }
        }
    )
    with pytest.raises(ValueError, match="project repair phase is invalid"):
        project_repair_phase.prepare(
            context, f.cp, f.cp.state["model_ledger"], f.cp.state["tool_ledger"]
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("permission_mode", "full_access"),
        ("requested_permissions", ["*"]),
        ("project_id", "other"),
        ("runtime_plan_token_budget", 40001),
        ("runtime_token_absolute_tokens", 40001),
        ("runtime_timeout_absolute_seconds", 61),
        ("context_window_tokens", 8000),
        ("source_run_id", "00000000-0000-4000-8000-000000000001"),
        ("unknown_authority", "added"),
    ],
)
async def test_window_candidate_does_not_hide_any_other_phase_authority_without_pg(
    tmp_path, key, value
):
    f = await repair_fixture(tmp_path)
    context = await service_runtime_context(f, await claim(f))
    context = context.model_copy(
        update={"routing_decision": {**context.routing_decision, key: value}}
    )
    with pytest.raises(ValueError, match="project repair phase is invalid"):
        project_repair_phase.prepare(
            context, f.cp, f.cp.state["model_ledger"], f.cp.state["tool_ledger"]
        )


@pytest.mark.parametrize("key", ["main_agent_context_window_tokens", "context_window_tokens"])
@pytest.mark.parametrize("change", ["unchanged", "changed", "removed"])
async def test_original_persisted_window_scope_is_never_dropped_without_pg(tmp_path, key, change):
    f = await repair_fixture(tmp_path, route_extras={key: 8000})
    context = await service_runtime_context(f, await claim(f))
    route = dict(context.routing_decision)
    if change == "changed":
        route[key] = 10000
    elif change == "removed":
        route.pop(key)
    context = context.model_copy(update={"routing_decision": route})
    if change == "unchanged":
        assert project_repair_phase.prepare(
            context, f.cp, f.cp.state["model_ledger"], f.cp.state["tool_ledger"]
        )
    else:
        with pytest.raises(ValueError, match="project repair phase is invalid"):
            project_repair_phase.prepare(
                context, f.cp, f.cp.state["model_ledger"], f.cp.state["tool_ledger"]
            )


async def test_persisted_reader_cannot_adopt_runtime_only_window_candidate_without_pg(tmp_path):
    f = await repair_fixture(tmp_path)
    runtime = await service_runtime_context(f, await claim(f))
    assert await f.f.broker.prepare(runtime, worker_id="repair-worker") is not None
    # The read-only DAO deliberately has no worker token in its persisted context.
    f.db.run.routing_decision = {
        **f.db.run.routing_decision,
        "main_agent_context_window_tokens": 10000,
    }
    assert await f.runs.load_same_run_repair_continuity(runtime.tenant_id, runtime.run_id) is None
    with pytest.raises(ReviewAssemblyUnavailable):
        await f.f.broker.prepare(runtime, worker_id="repair-worker")


@pytest.mark.parametrize("defect", ["profile", "role", "source", "phase_scope", "prefix"])
async def test_projected_phase_retains_profile_role_source_and_prefix_fences_without_pg(
    tmp_path, defect
):
    from agent_hub.auth.models import Role

    f = await repair_fixture(tmp_path)
    context = await service_runtime_context(f, await claim(f))
    if defect == "role":
        with pytest.raises(ReviewAssemblyUnavailable):
            await f.f.broker.prepare(
                context.model_copy(update={"actor_role": Role.ADMIN}), worker_id="repair-worker"
            )
        return
    if defect in {"profile", "source"}:
        route = _mutable_json(context.routing_decision)
        if defect == "profile":
            route["approval_review_profile"]["timeout_seconds"] += 1
        else:
            route["source"] = "other"
        with pytest.raises(ValueError, match="project repair phase is invalid"):
            project_repair_phase.prepare(
                context.model_copy(update={"routing_decision": route}),
                f.cp,
                f.cp.state["model_ledger"],
                f.cp.state["tool_ledger"],
            )
        return

    async def check(actual):
        phase = prepare_projection_phase(actual, f.cp)
        if defect == "phase_scope":
            phase["source_scope_sha256"] = project_repair_phase.authority_digest(
                actual.routing_decision
            )
        else:
            phase["source_tool_sha256"] = "f" * 64
        with pytest.raises(ValueError, match="project repair phase is invalid"):
            project_repair_phase.validate(
                phase,
                context=actual,
                models=f.cp.state["model_ledger"],
                tools=f.cp.state["tool_ledger"],
                tool_prefix=len(f.cp.state["tool_ledger"]),
                state=f.cp.state,
            )

    await projected_worker(f, context, check)


@pytest.mark.parametrize(
    "relative", ["runtime/autogen/project_repair_phase.py", "runtime/repair_projection.py"]
)
async def test_phase_copy_source_change_invalidates_runtime_evidence_without_pg(
    tmp_path, monkeypatch, relative
):
    """Mutate actual copied bytes, then exercise the real fingerprint/evidence fences."""
    source = Path(review_assembly.__file__).resolve().parents[1]
    original_phase = (source / relative).read_bytes()
    copied = tmp_path / "copied-package" / "agent_hub"
    shutil.copytree(source, copied, ignore=shutil.ignore_patterns("__pycache__"))
    name = "agent_hub.runtime._a2_fingerprint_copy"
    spec = importlib.util.spec_from_file_location(name, copied / "runtime/review_assembly.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    before = module.runtime_source_fingerprint()
    assert before == review_assembly.runtime_source_fingerprint()
    f = await offline_assembly(tmp_path / "fixture")
    document = evidence_document(f)
    document["worker_verification"]["source_sha256"] = before
    evidence = tmp_path / "installed-evidence.json"
    body = json.dumps(document).encode()
    evidence.write_bytes(body)
    pin = hashlib.sha256(body).hexdigest()
    contracts, verification = module.load_server_review_evidence(evidence, expected_sha256=pin)
    broker = module.ReviewAssemblyBroker(
        config_service=f.config_service,
        config_getter=f.broker.current_config,
        run_repository=f.s.runs,
        session_factory=f.db,
        secret_service=f.secrets,
        capacity_factory=f.capacity,
        transport=f.broker._transport,
        billing_contracts=contracts,
        verification=verification,
    )
    snapshot, reason = await broker._compile(f.s.task.tenant_id)
    assert snapshot is not None and reason == "ready"
    broker._require_activation_current(snapshot)
    phase = copied / relative
    phase.write_bytes(phase.read_bytes() + b"\n# A2 copied scope gate change\n")
    assert (source / relative).read_bytes() == original_phase
    assert module.runtime_source_fingerprint() != before
    with pytest.raises(ValueError, match="invalid server review evidence"):
        module.load_server_review_evidence(evidence, expected_sha256=pin)
    current, reason = await broker._compile(f.s.task.tenant_id)
    assert current is None and reason == "current_mode_fence_unverified"
    with pytest.raises(module.ReviewAssemblyUnavailable, match="review_activation_changed"):
        broker._require_activation_current(snapshot)
    assert not f.state.calls


@pytest.mark.parametrize("legacy", [False, True], ids=["model_profile", "legacy"])
async def test_private_projection_worker_strip_window_and_cold_resume_without_pg(tmp_path, legacy):
    from agent_hub.models.physical_budget import _run_budget

    f = await projection_case(tmp_path, legacy=legacy)
    contexts, checkpoints = [], []

    class Runtime:
        async def run(self, actual):
            contexts.append(actual)
            assert not f.private.keys() & actual.routing_decision.keys()
            assert actual.routing_decision["main_agent_context_window_tokens"] == 10000
            budget = _run_budget.get()
            if legacy:
                assert budget is None and "approval_review_profile" not in actual.routing_decision
            else:
                await budget.require_current()
            checkpoint = await productive_checkpoint(f, actual)
            checkpoints.append(checkpoint)
            phase = checkpoint.state[project_repair_phase.PHASE_KEY]
            expected = f.db.run.routing_decision["repair_proposal"]["automatic_scope_sha256"]
            assert phase["source_scope_sha256"] == expected
            artifacts = tuple(
                PostgresArtifactRepository._hydrate(row)
                for row in f.db.rows
                if type(row) is RuntimeArtifactRow
            )
            project_repair_phase.productive_boundary(actual, checkpoint, artifacts)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    kwargs = {"worker_id": "repair-worker"}
    if legacy:
        kwargs["run_repository"] = f.runs
    else:
        kwargs["review_broker"] = f.f.broker
    async with worker_runtime_events(Runtime(), f.context, **kwargs) as events:
        async for _ in events:
            pass
    checkpoint = checkpoints[0]
    f.runs.clear_worker_lease(f.db.run)
    f.db.run.status = "failed"
    await f.runs.continue_project_repair_and_enqueue(
        tenant_id=f.context.tenant_id,
        run_id=f.context.run_id,
        version=f.db.run.version,
        checkpoint_sha256=checkpoint.state_sha256,
    )
    cold = await service_runtime_context(f, await claim(f), window=10000)

    class Cold:
        async def run(self, actual):
            assert actual.execution_token != contexts[0].execution_token
            artifacts = tuple(
                PostgresArtifactRepository._hydrate(row)
                for row in f.db.rows
                if type(row) is RuntimeArtifactRow
            )
            project_repair_phase.validate(
                checkpoint.state[project_repair_phase.PHASE_KEY],
                context=actual,
                models=checkpoint.state["model_ledger"],
                tools=checkpoint.state["tool_ledger"],
                tool_prefix=2,
                state=checkpoint.state,
                artifacts=artifacts,
            )
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    async with worker_runtime_events(Cold(), cold, **kwargs) as events:
        async for _ in events:
            pass
    assert pool_values(f.db) == f.before
    assert not f.f.state.calls


def prepare_projection_phase(context, checkpoint):
    return project_repair_phase.prepare(
        context, checkpoint, checkpoint.state["model_ledger"], checkpoint.state["tool_ledger"]
    )


@pytest.mark.parametrize(
    "defect", ["lease", "role", "child", "permission", "profile", "token", "time", "unknown"]
)
async def test_private_projection_exact_task_binding_rejects_mutations_without_pg(tmp_path, defect):
    from agent_hub.auth.models import Role

    f = await projection_case(tmp_path)

    class Runtime:
        async def run(self, actual):
            assert prepare_projection_phase(actual, f.cp)
            updates = {}
            route = _mutable_json(actual.routing_decision)
            if defect == "lease":
                updates["execution_token"] = uuid4()
            elif defect == "role":
                updates["actor_role"] = Role.ADMIN
            elif defect == "child":
                updates.update(run_id=uuid4(), checkpoint=None)
            elif defect == "permission":
                route["requested_permissions"] = ["*"]
            elif defect == "profile":
                route["approval_review_profile"]["timeout_seconds"] += 1
            elif defect == "token":
                route["runtime_token_absolute_tokens"] += 1
            elif defect == "time":
                route["runtime_timeout_absolute_seconds"] += 1
            else:
                route["unknown_authority"] = "new"
            updates["routing_decision"] = route
            with pytest.raises(ValueError, match="project repair phase is invalid"):
                prepare_projection_phase(actual.model_copy(update=updates), f.cp)
            public = json.dumps(actual.to_payload())
            assert not any(key in public or value in public for key, value in f.private.items())
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    async with worker_runtime_events(
        Runtime(), f.context, worker_id="repair-worker", review_broker=f.f.broker
    ) as stream:
        async for _ in stream:
            pass
    assert pool_values(f.db) == f.before


@pytest.mark.parametrize("legacy", [False, True], ids=["model_profile", "legacy"])
@pytest.mark.parametrize("defect", ["receipt", "locator", "authority", "owner", "expiry", "actor"])
async def test_private_projection_refresh_rejects_persisted_drift_without_pg(
    tmp_path, legacy, defect
):
    f = await projection_case(tmp_path, legacy=legacy)
    reached = []

    class Runtime:
        async def run(self, actual):
            assert prepare_projection_phase(actual, f.cp)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )
            reached.append("stale work")
            prepare_projection_phase(actual, f.cp)

    kwargs = {"worker_id": "repair-worker", "run_repository": f.runs}
    if not legacy:
        kwargs["review_broker"] = f.f.broker
    async with worker_runtime_events(Runtime(), f.context, **kwargs) as stream:
        await anext(stream)
        if defect in {"receipt", "locator", "authority"}:
            key = {
                "receipt": "project_verification_receipt",
                "locator": "approval_checkpoint_worker_token",
                "authority": "unknown_authority",
            }[defect]
            f.db.run.routing_decision = {**f.db.run.routing_decision, key: "changed"}
        elif defect == "owner":
            f.db.run.worker_lease_token = uuid4()
        elif defect == "expiry":
            f.db.run.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        else:
            f.db.run.actor_id = uuid4()
        with pytest.raises(ValueError, match="repair_projection_unavailable"):
            await anext(stream)
    assert not reached
    assert pool_values(f.db) == f.before


@pytest.mark.parametrize("legacy", [False, True], ids=["model_profile", "legacy"])
async def test_private_projection_cannot_escape_owner_or_child_task_binding_without_pg(
    tmp_path, legacy
):
    from agent_hub.capabilities.execution_authority import bind_execution_authority

    f = await projection_case(tmp_path, legacy=legacy)
    release, escaped = asyncio.Event(), []

    class Runtime:
        async def run(self, actual):
            assert prepare_projection_phase(actual, f.cp)
            child = actual.model_copy(
                update={"run_id": uuid4(), "checkpoint": None, "execution_token": uuid4()}
            )
            with (
                bind_execution_authority(child, worker_id="child-worker"),
                pytest.raises(ValueError, match="project repair phase is invalid"),
            ):
                prepare_projection_phase(actual, f.cp)
            with (
                bind_execution_authority(actual, worker_id="repair-worker", actor="other_agent"),
                pytest.raises(ValueError, match="project repair phase is invalid"),
            ):
                prepare_projection_phase(actual, f.cp)

            async def late():
                await release.wait()
                with pytest.raises(ValueError, match="project repair phase is invalid"):
                    prepare_projection_phase(actual, f.cp)

            escaped.append(asyncio.create_task(late()))
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    kwargs = {"worker_id": "repair-worker", "run_repository": f.runs}
    if not legacy:
        kwargs["review_broker"] = f.f.broker
    try:
        async with worker_runtime_events(Runtime(), f.context, **kwargs) as stream:
            await anext(stream)
    finally:
        release.set()
        await asyncio.gather(*escaped)
    assert pool_values(f.db) == f.before


async def test_private_projection_json_and_unsealed_objects_cannot_forge_proof_without_pg(tmp_path):
    from agent_hub.runtime import repair_projection
    from agent_hub.runtime.contracts import TaskContext

    f = await projection_case(tmp_path)

    class Runtime:
        async def run(self, actual):
            public = TaskContext.from_payload(actual.to_payload())
            assert public.execution_token is None
            with pytest.raises(ValueError, match="project repair phase is invalid"):
                prepare_projection_phase(public, f.cp)
            route = {**actual.routing_decision, "repair_projection_proof": {"approved": True}}
            with pytest.raises(ValueError, match="project repair phase is invalid"):
                prepare_projection_phase(
                    actual.model_copy(update={"routing_decision": route}), f.cp
                )
            current = repair_projection._CURRENT.get()
            for fake in (
                {"scope": current.proof.scope, "approved": True},
                replace(current, proof=replace(current.proof, seal=object())),
            ):
                token = repair_projection._CURRENT.set(fake)
                try:
                    with pytest.raises(ValueError, match="project repair phase is invalid"):
                        prepare_projection_phase(actual, f.cp)
                finally:
                    repair_projection._CURRENT.reset(token)
            assert prepare_projection_phase(actual, f.cp)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    async with worker_runtime_events(
        Runtime(), f.context, worker_id="repair-worker", review_broker=f.f.broker
    ) as stream:
        async for _ in stream:
            pass


async def test_private_projection_cached_lease_expiry_rejects_without_refresh_without_pg(
    tmp_path, monkeypatch
):
    from agent_hub.runtime import repair_projection

    f = await projection_case(tmp_path)

    class Runtime:
        async def run(self, actual):
            assert prepare_projection_phase(actual, f.cp)

            class Expired(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(UTC) + timedelta(hours=1)

            with monkeypatch.context() as patch:
                patch.setattr(repair_projection, "datetime", Expired)
                with pytest.raises(ValueError, match="project repair phase is invalid"):
                    prepare_projection_phase(actual, f.cp)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    async with worker_runtime_events(
        Runtime(), f.context, worker_id="repair-worker", review_broker=f.f.broker
    ) as stream:
        async for _ in stream:
            pass


@pytest.mark.parametrize("legacy", [False, True], ids=["model_profile", "legacy"])
async def test_private_projection_refreshes_same_lease_but_not_original_pool_without_pg(
    tmp_path, legacy
):
    f = await projection_case(tmp_path, legacy=legacy)
    reached = []

    class Runtime:
        async def run(self, actual):
            assert prepare_projection_phase(actual, f.cp)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )
            assert prepare_projection_phase(actual, f.cp)
            reached.append(True)
            yield RunEvent(
                kind=EventKind.RUNTIME_COMPLETED,
                sequence=2,
                run_id=actual.run_id,
                reason="bounded probe",
            )
            with pytest.raises(ValueError, match="project repair phase is invalid"):
                prepare_projection_phase(actual, f.cp)

    kwargs = {"worker_id": "repair-worker", "run_repository": f.runs}
    if not legacy:
        kwargs["review_broker"] = f.f.broker
    async with worker_runtime_events(Runtime(), f.context, **kwargs) as stream:
        await anext(stream)
        f.db.run.worker_lease_expires_at += timedelta(seconds=60)
        await anext(stream)
        f.db.run.status = "completed"  # terminal consumer update must permit natural cleanup
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    assert reached and pool_values(f.db) == f.before


async def test_private_projection_does_not_downgrade_configuration_or_unknown_fences_without_pg(
    tmp_path,
):
    from agent_hub.models.physical_budget import _run_budget

    f = await projection_case(tmp_path)
    reached = []

    class Runtime:
        async def run(self, actual):
            checkpoint = await productive_checkpoint(f, actual)
            state = checkpoint.to_payload()["state"]
            state["tool_ledger"][-1]["status"] = "unknown"
            with pytest.raises(ValueError, match="project repair phase is invalid"):
                project_repair_phase.validate(
                    state[project_repair_phase.PHASE_KEY],
                    context=actual,
                    models=state["model_ledger"],
                    tools=state["tool_ledger"],
                    tool_prefix=2,
                    state=state,
                )
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )
            await _run_budget.get().require_current()
            reached.append("downgraded work")

    async with worker_runtime_events(
        Runtime(), f.context, worker_id="repair-worker", review_broker=f.f.broker
    ) as stream:
        await anext(stream)
        f.f.state.version += 1
        with pytest.raises(ReviewAssemblyUnavailable, match="review_configuration_changed"):
            await anext(stream)
    assert not reached and pool_values(f.db) == f.before
    unknown = next(row for row in f.db.rows if type(row) is RunAdmissionAttemptRow)
    assert (unknown.state, unknown.token_hold) == ("unknown", 18000)


async def test_private_projection_legacy_service_adapter_passes_repository_without_pg(tmp_path):
    from agent_hub.runs.service import _adaptive_runtime_events

    f = await projection_case(tmp_path, legacy=True)
    reached = []

    class Runtime:
        async def run(self, actual):
            assert prepare_projection_phase(actual, f.cp)
            reached.append(True)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    async for _ in _adaptive_runtime_events(
        Runtime(),
        f.context,
        configured_tokens=40000,
        routing_decision=f.context.routing_decision,
        initial_progress_units=0,
        worker_id="repair-worker",
        run_repository=f.runs,
    ):
        pass
    assert reached and pool_values(f.db) == f.before


@pytest.mark.parametrize("change", ["lease", "role", "owner_closed"])
async def test_private_projection_window_only_cannot_fallback_after_failure_without_pg(
    tmp_path, change
):
    from agent_hub.auth.models import Role

    f = await repair_fixture(tmp_path)
    context = await service_runtime_context(f, await claim(f))
    saved = []

    class Runtime:
        async def run(self, actual):
            assert prepare_projection_phase(actual, f.cp)
            saved.append(actual)
            if change != "owner_closed":
                updates = (
                    {"execution_token": uuid4()}
                    if change == "lease"
                    else {"actor_role": Role.ADMIN}
                )
                with pytest.raises(ValueError, match="project repair phase is invalid"):
                    prepare_projection_phase(actual.model_copy(update=updates), f.cp)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    async with worker_runtime_events(
        Runtime(), context, worker_id="repair-worker", review_broker=f.f.broker
    ) as stream:
        async for _ in stream:
            pass
    if change == "owner_closed":
        with pytest.raises(ValueError, match="project repair phase is invalid"):
            prepare_projection_phase(saved[0], f.cp)


async def test_private_projection_legacy_without_trusted_repository_fails_closed_without_pg(
    tmp_path,
):
    f = await projection_case(tmp_path, legacy=True)

    class Runtime:
        async def run(self, actual):
            prepare_projection_phase(actual, f.cp)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    with pytest.raises(ValueError, match="project repair phase is invalid"):
        async with worker_runtime_events(Runtime(), f.context, worker_id="repair-worker") as stream:
            await anext(stream)


@pytest.mark.parametrize("legacy", [False, True], ids=["model_profile", "legacy"])
async def test_private_projection_nested_worker_cannot_borrow_parent_without_pg(tmp_path, legacy):
    f = await projection_case(tmp_path, legacy=legacy)
    child_seen = []

    class Parent:
        async def run(self, actual):
            assert prepare_projection_phase(actual, f.cp)
            child = actual.model_copy(
                update={"run_id": uuid4(), "checkpoint": None, "execution_token": uuid4()}
            )

            class Child:
                async def run(self, own):
                    child_seen.append(True)
                    with pytest.raises(ValueError, match="project repair phase is invalid"):
                        prepare_projection_phase(actual, f.cp)
                    yield RunEvent(
                        kind="runtime.started",
                        sequence=1,
                        run_id=own.run_id,
                        payload={"phase": "child-probe"},
                    )

            if legacy:
                async with worker_runtime_events(
                    Child(), child, worker_id="child-worker"
                ) as stream:
                    await anext(stream)
            else:
                with pytest.raises(
                    ReviewAssemblyUnavailable, match="derived_run_pool_continuity_unsupported"
                ):
                    async with worker_runtime_events(
                        Child(), child, worker_id="child-worker"
                    ) as stream:
                        await anext(stream)
            assert prepare_projection_phase(actual, f.cp)
            yield RunEvent(
                kind="runtime.started",
                sequence=1,
                run_id=actual.run_id,
                payload={"phase": "parent-probe"},
            )

    kwargs = {"worker_id": "repair-worker", "run_repository": f.runs}
    if not legacy:
        kwargs["review_broker"] = f.f.broker
    async with worker_runtime_events(Parent(), f.context, **kwargs) as stream:
        async for _ in stream:
            pass
    assert bool(child_seen) is legacy
    assert pool_values(f.db) == f.before
