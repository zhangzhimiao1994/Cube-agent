"""Actual accept/claim/broker composition; local ORM seam, no PG/provider claims."""

import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList, Grouping, UnaryExpression

from agent_hub.auth.models import Role
from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunAdmissionPoolRow,
    RunAuthenticatedInputRow,
    RunCheckpointRow,
    RunEventRow,
    RunInputAuthorizationRow,
    RunOutboxRow,
    RunRow,
    RuntimeArtifactRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.run_admission_repository import (
    AdmissionConflict,
    DeploymentIdentity,
    RunAdmissionRepository,
    _WorkerLease,
)
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunConflict, RunRepository
from agent_hub.runs.self_repair import project_repair_scope_sha256
from agent_hub.runs.service import RunService, _with_main_agent_context_window
from agent_hub.runtime.autogen import project_repair_phase as phase
from agent_hub.runtime.contracts import Artifact, RuntimeCheckpoint
from agent_hub.runtime.review_assembly import ReviewAssemblyUnavailable
from agent_hub.runtime.worker_execution import worker_runtime_events
from tests.unit.runs.test_project_auto_repair import decision, evidence
from tests.unit.runtime.test_review_assembly import AssemblyDB, fixture


def matches(condition, row):
    """Evaluate the production queries' predicates at the existing ORM double seam."""
    if isinstance(condition, Grouping):
        return matches(condition.element, row)
    if isinstance(condition, BooleanClauseList):
        values = (matches(item, row) for item in condition.clauses)
        return all(values) if condition.operator is operators.and_ else any(values)
    if isinstance(condition, UnaryExpression):
        assert condition.operator is operators.inv
        return not matches(condition.element, row)
    assert isinstance(condition, BinaryExpression), condition
    left = getattr(row, condition.left.key)
    right = (
        condition.right.value
        if hasattr(condition.right, "value")
        else condition.right.__visit_name__ == "true"
    )
    if condition.operator is operators.is_:
        return left is right
    if condition.operator is operators.in_op:
        return left in right
    if condition.operator is operators.not_in_op:
        return left not in right
    return condition.operator(left, right)


class RepairDB(AssemblyDB):
    async def execute(self, statement):
        if statement.column_descriptions[0].get("entity") is RunRow:
            rows = self.select(statement)
            row = rows[0] if rows else None
            link = next(
                (
                    item
                    for item in self.rows
                    if type(item) is RunInputAuthorizationRow
                    and row is not None
                    and item.run_id == row.id
                ),
                None,
            )
            turn = next(
                (
                    item
                    for item in self.rows
                    if type(item) is RunAuthenticatedInputRow
                    and link is not None
                    and item.turn_id == link.turn_id
                ),
                None,
            )
            return SimpleNamespace(one_or_none=lambda: None if row is None else (row, link, turn))
        return await super().execute(statement)

    def select(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        rows = [row for row in self.rows if type(row) is entity]
        return [row for row in rows if all(matches(c, row) for c in statement._where_criteria)]

    async def scalar(self, statement):
        expr = statement.column_descriptions[0]["expr"]
        if getattr(expr, "name", None) == "max":
            column = next(iter(expr.clauses))
            entity = {
                RunEventRow.__tablename__: RunEventRow,
                RunCheckpointRow.__tablename__: RunCheckpointRow,
            }[column.table.name]
            values = [
                getattr(row, column.key)
                for row in self.rows
                if type(row) is entity and all(matches(c, row) for c in statement._where_criteria)
            ]
            return max(values, default=None)
        rows = self.select(statement)
        if statement._order_by_clauses and rows and hasattr(rows[0], "sequence"):
            rows.sort(key=lambda row: row.sequence, reverse=True)
        return rows[0] if rows else None


def checkpoint_row(cp, sequence):
    return RunCheckpointRow(
        id=cp.id,
        run_id=cp.run_id,
        tenant_id=cp.tenant_id,
        sequence=sequence,
        mode=cp.mode.value,
        runtime_type=cp.runtime_type,
        runtime_version=cp.runtime_version,
        state=cp.to_payload()["state"],
        state_sha256=cp.state_sha256,
        payload=cp.to_payload(),
    )


def pool_values(db):
    pool = next(row for row in db.rows if type(row) is RunAdmissionPoolRow)
    return {column.key: deepcopy(getattr(pool, column.key)) for column in pool.__table__.columns}


async def repair_fixture(tmp_path, *, accept=True, route_extras=None):
    f = await fixture(tmp_path, mode=TaskMode.DISCUSS)
    # Retain the real assembly configuration, replace only the SQL/session boundary.
    db = RepairDB(f.s)
    db.rows.remove(db.pool)
    db.run.created_at = f.s.runs.record.created_at
    db.run.version = 4
    route = {
        **f.s.task.routing_decision,
        "engineering_auto_repair_version": 1,
        "engineering_delivery_contract_version": 1,
        "engineering_checkpoint_ack_version": 1,
        **(route_extras or {}),
    }
    db.run.routing_decision = route
    runs = RunRepository(db)
    f.broker._runs = runs
    f.broker._sessions = db
    original_task = f.s.task.model_copy(update={"routing_decision": route})
    original = await f.broker.prepare(original_task, worker_id=db.run.worker_id)
    admission = RunAdmissionRepository(db)
    hold = {
        "tenant_id": original_task.tenant_id,
        "run_id": original_task.run_id,
        "authorization_turn_id": original.budget.evidence.turn_id,
        "lease": original.budget.lease,
        "config_sha256": original.budget._pool.config_sha256,
        "attempt_id": uuid4(),
        "purpose": "review",
        "request_sha256": "a" * 64,
        "deployment": DeploymentIdentity("fixed", "reviewer", "reviewer"),
        "token_hold": 18000,
        "review_cost_hold": Decimal("0.01"),
    }
    await admission.reserve(**hold)
    physical = uuid4()
    await admission.mark_issued(
        **{
            k: hold[k]
            for k in (
                "tenant_id",
                "run_id",
                "authorization_turn_id",
                "lease",
                "config_sha256",
                "attempt_id",
            )
        },
        physical_transport_id=physical,
    )
    await admission.finish(
        tenant_id=hold["tenant_id"],
        run_id=hold["run_id"],
        attempt_id=hold["attempt_id"],
        lease=hold["lease"],
        deployment=hold["deployment"],
        outcome="unknown",
        usage=None,
    )
    _, template = evidence()
    cp = RuntimeCheckpoint(
        id=uuid4(),
        run_id=original_task.run_id,
        tenant_id=original_task.tenant_id,
        mode=TaskMode.DISCUSS,
        runtime_type="autogen",
        runtime_version="2",
        state={
            **template.to_payload()["state"],
            "remaining_timeout_seconds": 30,
            "remaining_absolute_timeout_seconds": 60,
        },
    )
    proposal = replace(
        decision(cp),
        requires_approval=False,
        automatic_execution=True,
        automatic_checkpoint_sha256=cp.state_sha256,
        automatic_scope_sha256=project_repair_scope_sha256(route),
    ).to_proposal(run_id=cp.run_id)
    db.run.routing_decision = {
        **route,
        "approval_kind": "self_repair",
        "decision_token": "token",
        "repair_proposal": proposal,
    }
    db.run.status = "failed"
    runs.clear_worker_lease(db.run)
    db.add(checkpoint_row(cp, 1))
    db.add(
        RunEventRow(
            run_id=cp.run_id, tenant_id=cp.tenant_id, sequence=2, kind="runtime.failed", payload={}
        )
    )
    before = pool_values(db)
    old_token = original_task.execution_token
    if accept:
        accepted = await runs.accept_self_repair_and_enqueue(
            tenant_id=cp.tenant_id,
            run_id=cp.run_id,
            version=4,
            decision_token="token",
            automatic_checkpoint_sha256=cp.state_sha256,
        )
        assert accepted.status is RunStatus.QUEUED
        assert [row.run_id for row in db.rows if type(row) is RunOutboxRow] == [cp.run_id]
    return SimpleNamespace(**locals())


async def claim(f):
    token = uuid4()
    async with f.db.begin():
        result = await f.runs.claim_for_execution(
            f.db,
            f.cp.run_id,
            allow_running_recovery=False,
            worker_id="repair-worker",
            worker_lease_token=token,
            worker_lease_expires_at=datetime.now(UTC) + timedelta(seconds=90),
        )
    assert isinstance(result, tuple)
    row, checkpoint = result
    return f.original_task.model_copy(
        update={
            "execution_token": token,
            "checkpoint": checkpoint,
            "routing_decision": row.routing_decision,
        }
    )


async def test_accepted_same_run_repair_uses_original_pool_and_new_lease(tmp_path):
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
    assert repaired is not None
    assert repaired.budget.run_id == f.original_task.run_id
    assert repaired.budget.evidence == f.original.budget.evidence
    assert repaired.budget.lease == _WorkerLease("repair-worker", context.execution_token)
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
async def test_repair_cannot_mint_authority_budget_or_worker_ownership(tmp_path, defect):
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    route = deepcopy(f.db.run.routing_decision)
    if defect == "flags_only":
        route.pop("repair_proposal")
    elif defect == "child":
        route["source_run_id"] = str(uuid4())
    elif defect == "proposal_child":
        route["repair_proposal"]["source_run_id"] = str(uuid4())
    elif defect == "authority":
        route["requested_permissions"] = ["*"]
    elif defect == "tokens":
        route["runtime_plan_token_budget"] += 1
    elif defect == "timeout":
        route["runtime_timeout_absolute_seconds"] += 1
    elif defect == "profile":
        route["approval_review_profile"]["timeout_seconds"] += 1
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


async def test_active_old_worker_cannot_be_accepted_as_repair(tmp_path):
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
async def test_context_cannot_hide_persisted_repair_to_open_a_fresh_pool(tmp_path, drop_profile):
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
    tmp_path, field, value
):
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    context = context.model_copy(
        update={"checkpoint": context.checkpoint.model_copy(update={field: value})}
    )
    with pytest.raises(ReviewAssemblyUnavailable):
        await f.f.broker.prepare(context, worker_id="repair-worker")


async def productive_checkpoint(f, context):
    state = f.cp.to_payload()["state"]
    state[phase.PHASE_KEY] = phase.prepare(
        context, f.cp, f.cp.state["model_ledger"], f.cp.state["tool_ledger"]
    )
    artifact = Artifact(
        id=uuid4(),
        type="tool_result",
        producer="implementer",
        content={
            "tool_name": "workspace.write_text",
            "arguments_sha256": "b" * 64,
            "result": {
                "workspace_files": [{"path": "fixed.txt", "sha256": "c" * 64, "size_bytes": 7}]
            },
        },
    )
    state["tool_ledger"].append(
        {
            "name": "workspace.write_text",
            "actor": "implementer",
            "position": 1,
            "status": "succeeded",
            "artifact_id": str(artifact.id),
            "artifact_sha256": artifact.content_sha256,
            "arguments_hash": "b" * 64,
            "idempotency_key": hashlib.sha256(
                f"{context.run_id}:implementer:1:workspace.write_text:{'b' * 64}".encode()
            ).hexdigest(),
        }
    )
    state["artifact_registry"][str(artifact.id)] = artifact.content_sha256
    state["engineering_tool_prefix"] = 2
    state["remaining_timeout_seconds"] = 25
    state["remaining_absolute_timeout_seconds"] = 55
    state["usage"] = {"tokens": 120, "cost_usd": "0.2"}
    payload = PostgresArtifactRepository._encode_payload(artifact.to_payload())
    f.db.add(
        RuntimeArtifactRow(
            id=artifact.id,
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            content_sha256=artifact.content_sha256,
            payload=payload,
            byte_size=len(payload.encode()),
            permanent=True,
        )
    )
    checkpoint = RuntimeCheckpoint(
        id=uuid4(),
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        mode=TaskMode.DISCUSS,
        runtime_type="autogen",
        runtime_version="2",
        state=state,
    )
    f.db.add(checkpoint_row(checkpoint, 3))
    return checkpoint


async def test_productive_episode_accepts_later_checkpoint_after_cold_new_worker(tmp_path):
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
        continued.routing_decision["self_repair_episode_progress"]["checkpoint_sha256"]
        == checkpoint.state_sha256
    )
    context = await claim(f)
    assert context.checkpoint.id != f.cp.id
    f.f.broker._runs = RunRepository(f.db)  # no in-memory acceptance receipt
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
    assert repaired.budget.evidence.turn_id == f.original.budget.evidence.turn_id
    assert pool_values(f.db) == f.before


@pytest.mark.parametrize("defect", ["scope", "prefix", "unknown", "absolute_reset"])
async def test_later_phase_must_preserve_source_prefix_scope_and_budget(tmp_path, defect):
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    checkpoint = await productive_checkpoint(f, context)
    state = checkpoint.to_payload()["state"]
    if defect == "scope":
        state[phase.PHASE_KEY]["source_scope_sha256"] = "f" * 64
    elif defect == "prefix":
        state["tool_ledger"][0]["status"] = "succeeded"
    elif defect == "unknown":
        state["tool_ledger"][-1]["status"] = "unknown"
    else:
        state["remaining_absolute_timeout_seconds"] = 60
        state[phase.PHASE_KEY]["source_checkpoint_metadata"][
            "remaining_absolute_timeout_seconds"
        ] = 50
    checkpoint = RuntimeCheckpoint(
        id=checkpoint.id,
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        mode=TaskMode.DISCUSS,
        runtime_type="autogen",
        runtime_version="2",
        state=state,
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


async def test_active_repair_can_admit_new_work_after_publishing_inflight_checkpoint(tmp_path):
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
    state = f.cp.to_payload()["state"]
    state["model_ledger"].append({"status": "running", "request_hash": "d" * 64})
    inflight = RuntimeCheckpoint(
        id=uuid4(),
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        mode=TaskMode.DISCUSS,
        runtime_type="autogen",
        runtime_version="2",
        state=state,
    )
    f.db.add(checkpoint_row(inflight, 3))
    # The stopped resume proof is immutable. New work has its own journal/UNKNOWN
    # fences; it must not be mistaken for another acceptance of a stopped run.
    await repaired.budget.require_current()
    assert pool_values(f.db) == f.before


@pytest.mark.parametrize("defect", ["authority", "config", "role", "owner", "deadline"])
async def test_live_repair_rechecks_authority_and_original_fences(tmp_path, defect):
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
    if defect == "authority":
        f.db.run.routing_decision = {**f.db.run.routing_decision, "permission_mode": "full_access"}
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


async def test_new_repair_work_can_spend_only_the_original_remaining_balance(tmp_path):
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
    reservation = {
        "tenant_id": context.tenant_id,
        "run_id": context.run_id,
        "authorization_turn_id": repaired.budget.evidence.turn_id,
        "lease": repaired.budget.lease,
        "config_sha256": f.before["config_sha256"],
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


async def service_runtime_context(f, context, *, window=10000):
    async def trusted_window():
        return window

    service = RunService(
        f.runs,
        runtime_registry=SimpleNamespace(),
        router=None,
        task_queue=SimpleNamespace(),
        main_agent_context_window_getter=trusted_window,
    )
    selected = await service._main_agent_context_window_tokens(context.routing_decision)
    return context.model_copy(
        update={
            "routing_decision": _with_main_agent_context_window(context.routing_decision, selected)
        }
    )


@pytest.mark.parametrize("window,expected", [(10000, 10000), (20000000, 10000000)])
async def test_real_service_window_projection_preserves_repair_pool(tmp_path, window, expected):
    f = await repair_fixture(tmp_path)
    context = await service_runtime_context(f, await claim(f), window=window)
    assert "main_agent_context_window_tokens" not in f.db.run.routing_decision
    assert context.routing_decision["main_agent_context_window_tokens"] == expected
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
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
async def test_service_projection_cannot_hide_runtime_authority_drift(tmp_path, field, value):
    f = await repair_fixture(tmp_path)
    context = await service_runtime_context(f, await claim(f))
    context = context.model_copy(
        update={"routing_decision": {**context.routing_decision, field: value}}
    )
    with pytest.raises(ReviewAssemblyUnavailable):
        await f.f.broker.prepare(context, worker_id="repair-worker")
    assert pool_values(f.db) == f.before


async def test_service_projection_does_not_erase_persisted_window_drift(tmp_path):
    f = await repair_fixture(tmp_path)
    context = await service_runtime_context(f, await claim(f))
    repaired = await f.f.broker.prepare(context, worker_id="repair-worker")
    f.db.run.routing_decision = {
        **f.db.run.routing_decision,
        "main_agent_context_window_tokens": 10000,
    }
    with pytest.raises(ReviewAssemblyUnavailable):
        await repaired.budget.require_current()
    assert pool_values(f.db) == f.before


async def test_existing_persisted_window_cannot_be_overridden_by_runtime(tmp_path):
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


async def test_worker_private_projection_is_exact_and_persisted_locators_stay_bound(tmp_path):
    private = {
        "project_verification_receipt": "fixture-private-receipt",
        "approval_checkpoint_worker_token": str(uuid4()),
    }
    f = await repair_fixture(tmp_path, route_extras=private)
    context = await service_runtime_context(f, await claim(f))
    seen = []

    class Runtime:
        async def run(self, actual):
            from agent_hub.models.physical_budget import _run_budget

            seen.append(actual)
            assert not private.keys() & actual.routing_decision.keys()
            await _run_budget.get().require_current()
            f.db.run.routing_decision = {
                **f.db.run.routing_decision,
                "project_verification_receipt": "changed",
            }
            with pytest.raises(ReviewAssemblyUnavailable):
                await _run_budget.get().require_current()
            if False:
                yield

    async with worker_runtime_events(
        Runtime(), context, worker_id="repair-worker", review_broker=f.f.broker
    ) as events:
        async for _ in events:
            pass
    assert len(seen) == 1
    assert pool_values(f.db) == f.before
