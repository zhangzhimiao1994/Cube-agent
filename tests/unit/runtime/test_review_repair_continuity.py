"""Actual accept/claim/broker composition; local ORM seam, no PG/provider claims."""
__all__ = ["RepairDB", "checkpoint_row", "matches"]

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunAdmissionPoolRow,
    RunCheckpointRow,
    RunInputAuthorizationRow,
    RunOutboxRow,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.models.run_admission_repository import (
    AdmissionConflict,
    DeploymentIdentity,
    _ReserveArguments,
    _WorkerLease,
)
from agent_hub.runs.repository import RunConflict, RunRepository
from agent_hub.runtime.autogen import project_repair_phase as phase
from agent_hub.runtime.contracts import RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.review_assembly import ReviewAssemblyUnavailable
from agent_hub.runtime.worker_execution import worker_runtime_events
from tests.unit.runs.project_auto_repair_typed_fixture import (
    integer,
    json_map,
    mapping,
    mutable,
    present,
    sequence,
    string,
)
from tests.unit.runtime.repair_continuity_typed_fixture import (
    ContinuityRuntime,
    RepairDB,
    checkpoint_row,
    claim,
    matches,
    pool_values,
    productive_checkpoint,
    repair_fixture,
    service_runtime_context,
)


async def test_accepted_same_run_repair_uses_original_pool_and_new_lease(tmp_path: Path) -> None:
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
    assert repaired is not None
    assert repaired.budget.run_id == f.original_task.run_id
    assert repaired.budget.evidence == f.original.budget.evidence
    assert repaired.budget.lease == _WorkerLease("repair-worker", present(context.execution_token))
    assert context.execution_token != f.old_token
    assert pool_values(f.db) == f.before
    assert repaired.budget._pool.tokens_remaining == 22000
    assert repaired.budget._pool.original_deadline == f.original.budget._pool.original_deadline
    attempt = next(row for row in f.db.rows if type(row) is RunAdmissionAttemptRow)
    assert (attempt.state, attempt.token_hold, attempt.review_cost_hold) == (
        "unknown",
        18000,
        Decimal("0.01"),
    )
    assert f.db.run.request == f.original_task.request
    assert (
        context.routing_decision["approval_review_profile"]
        == f.original_task.routing_decision["approval_review_profile"]
    )
    assert not f.f.state.calls


@pytest.mark.parametrize(
    "defect",
    [
        "flags_only",
        "child",
        "proposal_child",
        "authority",
        "tokens",
        "timeout",
        "profile",
        "checkpoint",
        "missing_checkpoint",
        "missing_pool",
        "expired",
        "role",
        "old_owner",
        "lease_expired",
        "missing_authorization",
    ],
)
async def test_repair_cannot_mint_authority_budget_or_worker_ownership(tmp_path: Path, defect: str) -> None:
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    route = deepcopy(present(f.db.run.routing_decision))
    if defect == "flags_only":
        route.pop("repair_proposal")
    elif defect == "child":
        route["source_run_id"] = str(uuid4())
    elif defect == "proposal_child":
        mutable(route["repair_proposal"])["source_run_id"] = str(uuid4())
    elif defect == "authority":
        route["requested_permissions"] = ["*"]
    elif defect == "tokens":
        route["runtime_plan_token_budget"] = integer(route["runtime_plan_token_budget"]) + 1
    elif defect == "timeout":
        route["runtime_timeout_absolute_seconds"] = integer(route["runtime_timeout_absolute_seconds"]) + 1
    elif defect == "profile":
        profile = mutable(route["approval_review_profile"])
        profile["timeout_seconds"] = integer(profile["timeout_seconds"]) + 1
    elif defect == "checkpoint":
        context = context.model_copy(update={"checkpoint": None})
    elif defect == "missing_checkpoint":
        f.db.rows = [row for row in f.db.rows if type(row) is not RunCheckpointRow]
    elif defect == "missing_pool":
        f.db.rows = [row for row in f.db.rows if type(row) is not RunAdmissionPoolRow]
    elif defect == "expired":
        next(row for row in f.db.rows if type(row) is RunAdmissionPoolRow).original_deadline = (
            datetime.now(UTC) - timedelta(seconds=1)
        )
    elif defect == "role":
        f.db.run.actor_role = Role.ADMIN.value
    elif defect == "old_owner":
        context = context.model_copy(update={"execution_token": f.old_token})
    elif defect == "lease_expired":
        f.db.run.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif defect == "missing_authorization":
        f.db.rows = [row for row in f.db.rows if type(row) is not RunInputAuthorizationRow]
    f.db.run.routing_decision = route
    context = context.model_copy(update={"routing_decision": route})
    before = [pool_values(f.db)] if defect != "missing_pool" else []
    with pytest.raises((ReviewAssemblyUnavailable, AdmissionConflict)):
        await f.f.broker.prepare(context, worker_id="repair-worker")
    assert (
        [pool_values(f.db)] == before
        if before
        else not any(type(row) is RunAdmissionPoolRow for row in f.db.rows)
    )
    assert not f.f.state.calls


async def test_active_old_worker_cannot_be_accepted_as_repair(tmp_path: Path) -> None:
    f = await repair_fixture(tmp_path, accept=False)
    f.db.run.worker_id = "still-active"
    f.db.run.worker_lease_token = f.old_token
    with pytest.raises(RunConflict):
        await f.runs.accept_self_repair_and_enqueue(
            tenant_id=f.cp.tenant_id,
            run_id=f.cp.run_id,
            version=4,
            decision_token="token",
            automatic_checkpoint_sha256=f.cp.state_sha256,
        )
    assert not any(type(row) is RunOutboxRow for row in f.db.rows)
    assert pool_values(f.db) == f.before


@pytest.mark.parametrize("drop_profile", [False, True])
async def test_context_cannot_hide_persisted_repair_to_open_a_fresh_pool(tmp_path: Path, drop_profile: bool) -> None:
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    route = dict(context.routing_decision)
    route.pop("source")
    if drop_profile:
        route.pop("approval_review_profile")
    f.db.rows = [row for row in f.db.rows if type(row) is not RunAdmissionPoolRow]
    with pytest.raises(ReviewAssemblyUnavailable):
        await f.f.broker.prepare(
            context.model_copy(update={"routing_decision": route, "checkpoint": None}),
            worker_id="repair-worker",
        )
    assert not any(type(row) is RunAdmissionPoolRow for row in f.db.rows)


@pytest.mark.parametrize(
    "field,value", [("run_id", uuid4()), ("tenant_id", uuid4()), ("runtime_type", "other")]
)
async def test_checkpoint_state_hash_cannot_substitute_for_checkpoint_identity(
    tmp_path: Path, field: str, value: UUID | str
) -> None:
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    context = context.model_copy(
        update={"checkpoint": present(context.checkpoint).model_copy(update={field: value})}
    )
    with pytest.raises(ReviewAssemblyUnavailable):
        await f.f.broker.prepare(context, worker_id="repair-worker")




async def test_productive_episode_accepts_later_checkpoint_after_cold_new_worker(tmp_path: Path) -> None:
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    checkpoint = await productive_checkpoint(f, context)
    f.runs.clear_worker_lease(f.db.run)
    f.db.run.status = "failed"
    continued = await f.runs.continue_project_repair_and_enqueue(
        tenant_id=context.tenant_id,
        run_id=context.run_id,
        version=f.db.run.version,
        checkpoint_sha256=checkpoint.state_sha256,
    )
    assert (
        mapping(present(continued.routing_decision)["self_repair_episode_progress"])["checkpoint_sha256"]
        == checkpoint.state_sha256
    )
    context = await claim(f)
    assert present(context.checkpoint).id != f.cp.id
    f.f.broker._runs = RunRepository(f.db)  # no in-memory acceptance receipt
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
    assert repaired is not None
    assert repaired.budget.evidence.turn_id == f.original.budget.evidence.turn_id
    assert pool_values(f.db) == f.before


@pytest.mark.parametrize("defect", ["scope", "prefix", "unknown", "absolute_reset"])
async def test_later_phase_must_preserve_source_prefix_scope_and_budget(tmp_path: Path, defect: str) -> None:
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    checkpoint = await productive_checkpoint(f, context)
    state = mutable(checkpoint.to_payload()["state"])
    if defect == "scope":
        mutable(state[phase.PHASE_KEY])["source_scope_sha256"] = "f" * 64
    elif defect == "prefix":
        mutable(sequence(state["tool_ledger"])[0])["status"] = "succeeded"
    elif defect == "unknown":
        mutable(sequence(state["tool_ledger"])[-1])["status"] = "unknown"
    else:
        state["remaining_absolute_timeout_seconds"] = 60
        mutable(mutable(state[phase.PHASE_KEY])["source_checkpoint_metadata"] )[
            "remaining_absolute_timeout_seconds"
        ] = 50
    checkpoint = RuntimeCheckpoint(
        id=checkpoint.id,
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        mode=TaskMode.DISCUSS,
        runtime_type="autogen",
        runtime_version="2",
        state=json_map(state),
    )
    f.db.rows = [
        row for row in f.db.rows if not (type(row) is RunCheckpointRow and row.sequence == 3)
    ]
    f.db.add(checkpoint_row(checkpoint, 3))
    with pytest.raises(ReviewAssemblyUnavailable):
        await f.f.broker.prepare(
            context.model_copy(update={"checkpoint": checkpoint}), worker_id="repair-worker"
        )
    assert pool_values(f.db) == f.before


async def test_active_repair_can_admit_new_work_after_publishing_inflight_checkpoint(tmp_path: Path) -> None:
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
    assert repaired is not None
    state = mutable(f.cp.to_payload()["state"])
    model_ledger = state["model_ledger"]
    assert isinstance(model_ledger, list)
    model_ledger.append({"status": "running", "request_hash": "d" * 64})
    inflight = RuntimeCheckpoint(
        id=uuid4(),
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        mode=TaskMode.DISCUSS,
        runtime_type="autogen",
        runtime_version="2",
        state=json_map(state),
    )
    f.db.add(checkpoint_row(inflight, 3))
    # The stopped resume proof is immutable. New work has its own journal/UNKNOWN
    # fences; it must not be mistaken for another acceptance of a stopped run.
    await repaired.budget.require_current()
    assert pool_values(f.db) == f.before


@pytest.mark.parametrize("defect", ["authority", "config", "role", "owner", "deadline"])
async def test_live_repair_rechecks_authority_and_original_fences(tmp_path: Path, defect: str) -> None:
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
    assert repaired is not None
    if defect == "authority":
        f.db.run.routing_decision = {**present(f.db.run.routing_decision), "permission_mode": "full_access"}
    elif defect == "config":
        f.f.state.version += 1
    elif defect == "role":
        f.db.run.actor_role = Role.ADMIN.value
    elif defect == "owner":
        f.db.run.worker_lease_token = uuid4()
    else:
        next(row for row in f.db.rows if type(row) is RunAdmissionPoolRow).original_deadline = (
            datetime.now(UTC) - timedelta(seconds=1)
        )
    before = pool_values(f.db)
    with pytest.raises((ReviewAssemblyUnavailable, AdmissionConflict)):
        await repaired.budget.require_current()
    assert pool_values(f.db) == before
    assert not f.f.state.calls


async def test_new_repair_work_can_spend_only_the_original_remaining_balance(tmp_path: Path) -> None:
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
    assert repaired is not None
    reservation: _ReserveArguments = {
        "tenant_id": context.tenant_id,
        "run_id": context.run_id,
        "authorization_turn_id": repaired.budget.evidence.turn_id,
        "lease": repaired.budget.lease,
        "config_sha256": string(f.before["config_sha256"]),
        "attempt_id": uuid4(),
        "purpose": "primary",
        "request_sha256": "f" * 64,
        "deployment": DeploymentIdentity("fixed", "primary", "primary"),
        "token_hold": 22001,
        "review_cost_hold": None,
    }
    with pytest.raises(AdmissionConflict):
        await repaired.budget.repository.reserve(**reservation)
    assert pool_values(f.db) == f.before
    reservation["token_hold"] = 100
    await repaired.budget.repository.reserve(**reservation)
    attempts = [row for row in f.db.rows if type(row) is RunAdmissionAttemptRow]
    assert [(row.state, row.token_hold) for row in attempts] == [
        ("unknown", 18000),
        ("prepared", 100),
    ]
    assert attempts[1].worker_lease_token == context.execution_token
    after = pool_values(f.db)
    assert after.pop("token_charged") == 18100
    assert after == {key: value for key, value in f.before.items() if key != "token_charged"}
    assert not f.f.state.calls




@pytest.mark.parametrize("window,expected", [(10000, 10000), (20000000, 10000000)])
async def test_real_service_window_projection_preserves_repair_pool(tmp_path: Path, window: int, expected: int) -> None:
    f = await repair_fixture(tmp_path)
    context = await service_runtime_context(f, await claim(f), window=window)
    assert "main_agent_context_window_tokens" not in present(f.db.run.routing_decision)
    assert context.routing_decision["main_agent_context_window_tokens"] == expected
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
    assert repaired is not None
    await repaired.budget.require_current()
    assert pool_values(f.db) == f.before
    assert not f.f.state.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("main_agent_context_window_tokens", True),
        ("main_agent_context_window_tokens", -1),
        ("main_agent_context_window_tokens", 10000001),
        ("context_window_tokens", 10000),
        ("permission_mode", "full_access"),
        ("runtime_plan_token_budget", 40001),
        ("runtime_timeout_absolute_seconds", 61),
        ("source_run_id", "00000000-0000-4000-8000-000000000001"),
        ("unknown_authority", "new"),
    ],
)
async def test_service_projection_cannot_hide_runtime_authority_drift(tmp_path: Path, field: str, value: object) -> None:
    f = await repair_fixture(tmp_path)
    context = await service_runtime_context(f, await claim(f))
    context = context.model_copy(
        update={"routing_decision": {**context.routing_decision, field: value}}
    )
    with pytest.raises(ReviewAssemblyUnavailable):
        await f.f.broker.prepare(context, worker_id="repair-worker")
    assert pool_values(f.db) == f.before


async def test_service_projection_does_not_erase_persisted_window_drift(tmp_path: Path) -> None:
    f = await repair_fixture(tmp_path)
    context = await service_runtime_context(f, await claim(f))
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
    assert repaired is not None
    f.db.run.routing_decision = {
        **present(f.db.run.routing_decision),
        "main_agent_context_window_tokens": 10000,
    }
    with pytest.raises(ReviewAssemblyUnavailable):
        await repaired.budget.require_current()
    assert pool_values(f.db) == f.before


async def test_existing_persisted_window_cannot_be_overridden_by_runtime(tmp_path: Path) -> None:
    f = await repair_fixture(tmp_path, route_extras={"context_window_tokens": 8000})
    context = await service_runtime_context(f, await claim(f))
    assert "main_agent_context_window_tokens" not in context.routing_decision
    context = context.model_copy(
        update={
            "routing_decision": {
                **context.routing_decision,
                "main_agent_context_window_tokens": 10000,
            }
        }
    )
    with pytest.raises(ReviewAssemblyUnavailable):
        await f.f.broker.prepare(context, worker_id="repair-worker")


async def test_worker_private_projection_is_exact_and_persisted_locators_stay_bound(tmp_path: Path) -> None:
    private = {
        "project_verification_receipt": "fixture-private-receipt",
        "approval_checkpoint_worker_token": str(uuid4()),
    }
    f = await repair_fixture(tmp_path, route_extras=private)
    context = await service_runtime_context(f, await claim(f))
    seen: list[TaskContext] = []

    class Runtime(ContinuityRuntime):
        async def run(self, actual: TaskContext) -> AsyncIterator[RunEvent]:
            from agent_hub.models.physical_budget import _run_budget

            seen.append(actual)
            assert not private.keys() & actual.routing_decision.keys()
            await present(_run_budget.get()).require_current()
            f.db.run.routing_decision = {
                **present(f.db.run.routing_decision),
                "project_verification_receipt": "changed",
            }
            with pytest.raises(ReviewAssemblyUnavailable):
                await present(_run_budget.get()).require_current()
            if False:
                yield

    async with worker_runtime_events(
        Runtime(), context, worker_id="repair-worker", review_broker=f.f.broker
    ) as events:
        async for _ in events:
            pass
    assert len(seen) == 1
    assert pool_values(f.db) == f.before
