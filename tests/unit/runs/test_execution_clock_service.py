"""Actual ordinary submit + durable SQL, plus original same-pool worker assembly."""
from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.models import (
    RunAdmissionPoolRow,
    RunExecutionClockRow,
    RunInputAuthorizationRow,
    RunRow,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runs.execution_clock import (
    ClockPolicy,
    activate,
    digest,
    execution_boundary,
)
from agent_hub.runs.execution_clock_store import binding_seal, snapshot
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.contracts import RunEvent, TaskContext
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.runtime.worker_execution import worker_runtime_events
from tests.unit.harness.review_typed_fixture import (
    StreamRuntimeStub,
    logical_request,
    present,
)
from tests.unit.runs.test_execution_clock_store import SQLClock, db  # noqa: F401
from tests.unit.runtime.test_review_assembly import fixture


async def test_ordinary_submit_auto_selects_split_policy_with_actual_profile_and_sql(
    db: SQLClock, tmp_path: Path,  # noqa: F811 - imported pytest fixture
) -> None:
    assembled = await fixture(tmp_path)
    assembled.broker._runtime_authorization_timeout_seconds = 1000.0
    db.tenant = assembled.s.task.tenant_id
    actor = uuid4()
    text = "inspect notes"
    principal = AuthenticatedPrincipal(actor, db.tenant, Role.OPERATOR)
    original = _capture_authenticated_input(principal, text, {"selection": {"mode": "direct"}})
    service = RunService(db.repository, runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None, task_queue=cast(TaskQueue, object()), runtime_timeout_seconds=50.,
        runtime_authorization_timeout_seconds=1000., review_broker=assembled.broker)
    # This is the ordinary submit signature: no clock version or implementation durations.
    submitted = await service.submit(tenant_id=db.tenant, actor_id=actor, actor_role=Role.OPERATOR,
        message=text, mode=TaskMode.DIRECT, authenticated_input=original, skip_evolution_proposal=True)
    async with db.factory() as session:
        clock = await session.scalar(select(RunExecutionClockRow).where(RunExecutionClockRow.run_id == submitted.id))
        assert clock is not None
        assert clock.policy["active_seconds"] == 50. and clock.policy["authorization_seconds"] == 1000.
        assert snapshot(clock).hard_deadline == db.now + timedelta(seconds=1000)
        run = await session.get(RunRow, submitted.id)
        assert run is not None and run.routing_decision is not None
        assert run.routing_decision["execution_clock_version"] == 2
    assert assembled.state.calls == []


async def test_server_policy_is_required_and_public_selection_cannot_enable_clock(
    db: SQLClock, tmp_path: Path,  # noqa: F811 - imported pytest fixture
) -> None:
    assembled = await fixture(tmp_path)
    db.tenant = assembled.s.task.tenant_id
    text = "inspect notes"
    principal = AuthenticatedPrincipal(db.actor, db.tenant, Role.OPERATOR)
    original = _capture_authenticated_input(principal, text, {"execution_clock_version": 2})
    service = RunService(db.repository, runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None, task_queue=cast(TaskQueue, object()), review_broker=assembled.broker)
    with pytest.raises(ValueError, match="server-approved"):
        await service.submit(tenant_id=db.tenant, actor_id=db.actor, actor_role=Role.OPERATOR,
            message=text, mode=TaskMode.DIRECT, authenticated_input=original,
            execution_clock_version=2, runtime_timeout_seconds=100., runtime_authorization_seconds=1000.,
            skip_evolution_proposal=True)
    assert await db.count(RunExecutionClockRow) == 0


@execution_boundary
async def test_same_pool_new_worker_uses_active_remainder_with_unchanged_h_and_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
    from agent_hub.models.physical_budget import _run_budget
    from agent_hub.runtime import review_assembly
    from agent_hub.runtime.defaults import _gateway_for_config

    assembled = await fixture(tmp_path)
    monotonic = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: monotonic[0])
    original_get_config = assembled.broker._get_config
    first_prepare = True

    async def advancing_get_config(tenant_id: UUID) -> ApprovalReviewConfig:
        nonlocal first_prepare
        config = await original_get_config(tenant_id)
        if first_prepare:
            monotonic[0] += 5.0
            first_prepare = False
        return config

    monkeypatch.setattr(assembled.broker, "_get_config", advancing_get_config)
    task = assembled.s.task
    route: dict[str, object] = dict(task.routing_decision)
    profile = task.routing_decision["approval_review_profile"]
    assert isinstance(profile, Mapping)
    policy = ClockPolicy(20., 60., 10., 5., digest(dict(profile)))
    execution_token = present(task.execution_token)
    now = datetime.now(UTC)
    route.update(execution_clock_version=2, runtime_timeout_seconds=10., runtime_timeout_soft_seconds=10.,
                 runtime_timeout_absolute_seconds=20., runtime_plan_timeout_seconds=20.,
                 runtime_timeout_source="project_scale_soft_budget", critical_path_complexity_units=2)
    turn = next(row for row in assembled.db.rows if isinstance(row, RunInputAuthorizationRow))
    assembled.db.run.created_at = now
    marker = binding_seal(assembled.db.run, policy, turn.turn_id, now+timedelta(seconds=60))
    clock = RunExecutionClockRow(run_id=task.run_id, tenant_id=task.tenant_id,
        authorization_turn_id=turn.turn_id, actor_id=task.actor_id,
        request_sha256=__import__("hashlib").sha256(assembled.db.run.request.encode()).hexdigest(),
        binding_sha256=marker, policy=policy.payload(), hard_deadline=now+timedelta(seconds=60),
        spent_seconds=0., active_since=now, worker_id=assembled.db.run.worker_id,
        worker_token=execution_token, progress_units=0, checkpoint_id=None, checkpoint_sha256=None, version=1)
    assembled.db.rows.append(clock)
    assembled.db.run.execution_clock_sha256 = marker
    assembled.db.run.created_at = now
    assembled.db.run.routing_decision = route
    assembled.s.runs.record = replace(assembled.s.runs.record, created_at=now,
        routing_decision=route, execution_clock_sha256=marker)
    task = task.model_copy(update={"routing_decision": route, "timeout_seconds": 10.})
    budgets = []

    class Runtime(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            budget = present(_run_budget.get())
            budgets.append(budget)
            remaining = budget.deadline - time.monotonic()
            assert remaining == 15.0, "prepare time is charged once against the current active remainder"
            gateway, _, _ = await _gateway_for_config(present(review_assembly.current_review_platform(context.tenant_id)),
                tenant_id=context.tenant_id, secret_service=assembled.secrets,
                capacity_factory=assembled.capacity, transport=assembled.broker._transport)
            await gateway.complete(logical_request())
            yield RunEvent(kind="runtime.completed", sequence=1, run_id=context.run_id)

    activate(snapshot(clock), now, run_id=task.run_id, worker_token=execution_token)
    async with worker_runtime_events(Runtime(), task, worker_id=clock.worker_id, review_broker=assembled.broker) as events:
        await anext(events)
    pool = next(row for row in assembled.db.rows if isinstance(row, RunAdmissionPoolRow))
    original_h, grant, charged = pool.original_deadline, pool.original_token_grant, pool.token_charged
    assert grant == 40000 and charged > 0
    # SQL transaction tests prove the real park/resume transition; this exercises
    # the real assembly/DAO/gateway consumers of its resulting persisted snapshot.
    token = uuid4()
    assembled.db.run.worker_lease_token = token
    assembled.s.runs.record = replace(assembled.s.runs.record, worker_lease_token=token)
    clock.worker_token, clock.spent_seconds, clock.active_since = token, 5., datetime.now(UTC)
    clock.version += 1
    task = task.model_copy(update={"execution_token": token, "timeout_seconds": 5.})
    activate(snapshot(clock), clock.active_since, run_id=task.run_id, worker_token=token)
    async with worker_runtime_events(Runtime(), task, worker_id=clock.worker_id, review_broker=assembled.broker) as events:
        await anext(events)
    pools = [row for row in assembled.db.rows if isinstance(row, RunAdmissionPoolRow)]
    assert pools == [pool] and pool.original_deadline == original_h == clock.hard_deadline
    assert pool.original_token_grant == grant and pool.token_charged > charged
    assert len(assembled.state.calls) == 2 and len(budgets) == 2


async def test_actual_service_execute_parks_only_after_preparation_and_heartbeat_cleanup(
    db: SQLClock, tmp_path: Path,  # noqa: F811
) -> None:
    from agent_hub.capabilities.project_contracts import ManagedProjectInput
    from agent_hub.domain.runs import RunStatus
    from agent_hub.runs.project_inputs import OriginalProjectApprovalPending
    from agent_hub.runtime.contracts import _freeze_object

    assembled = await fixture(tmp_path)
    assembled.broker._runtime_authorization_timeout_seconds = 1000.0
    db.tenant = assembled.s.task.tenant_id
    text = "inspect existing project"
    actor = uuid4()
    original = _capture_authenticated_input(AuthenticatedPrincipal(actor, db.tenant, Role.OPERATOR), text,
                                           {"selection": {"mode": "direct"}})
    seen = []
    preparation = {"schema_version": 1, "phase": "captured",
        "reference": {"schema_version": 1, "contract_sha256": "a"*64,
                      "input_source_sha256": "b"*64, "file_count": 6}}
    approval = "approval_"+"e"*32

    async def prepare(context: TaskContext) -> TaskContext:
        # Only original project IO is fake. The service, authority scope,
        # approval/checkpoint guards, SQL transaction and worker cleanup are real.
        from agent_hub.runs.execution_clock import live_remaining

        remaining = live_remaining()
        assert remaining is not None and 0 < remaining.effective_seconds <= 50.
        seen.append(context.timeout_seconds)
        db.now += timedelta(seconds=5)
        async with db.factory.begin() as session:
            row = await db.repository.get_for_update(session, context.run_id)
            assert row.routing_decision is not None
            row.routing_decision = {**row.routing_decision, "project_input_preparation": preparation}
        scope = (f"capability-scope:v1:tenant={db.tenant}:user={actor}:run={context.run_id}:"
                 "capability=command:operation=run:resource=workspace/current/project-verification")
        await db.repository.begin_capability_approval(db.tenant, context.run_id,
            approval_id=approval, approval_fingerprint="e"*64, actor_id=actor,
            approval_scope=scope)
        raise OriginalProjectApprovalPending(approval, _freeze_object(preparation, name="fixture capture"))

    service = RunService(db.repository, runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None, task_queue=cast(TaskQueue, object()), runtime_timeout_seconds=50.,
        runtime_authorization_timeout_seconds=1000., review_broker=assembled.broker,
        project_input_preparer=prepare)
    submitted = await service.submit(tenant_id=db.tenant, actor_id=actor, actor_role=Role.OPERATOR,
        message=text, mode=TaskMode.DIRECT, authenticated_input=original, skip_evolution_proposal=True,
        project_id="project", workspace_session_id="session", sandbox_profile="workspace_write",
        requested_permissions=("workspace.read", "workspace.write", "command.run"),
        project_input=ManagedProjectInput.from_payload({"schema_version": 1, "kind": "managed_workspace",
                                                       "editable_paths": ["src"]}))
    result = await service.execute(submitted.id)
    assert result.status is RunStatus.WAITING_APPROVAL and len(seen) == 1
    clock = await db.clock(submitted.id)
    assert clock.spent_seconds == 5. and clock.active_since is None
    before = clock.hard_deadline
    db.now += timedelta(seconds=500)
    after = await db.clock(submitted.id)
    assert after.hard_deadline == before and after.spent_seconds == 5.
    assert after.remaining(db.now).active_seconds == 45.
    events = await db.repository.raw_events(db.tenant, submitted.id)
    assert not any(event.kind in {"runtime.failed", "model.started"} for event in events)
    assert assembled.state.calls == []


async def test_native_qualification_hashes_all_clock_authority_and_rejects_stale_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ast
    import shutil

    from agent_hub.runtime import review_assembly

    assembled = await fixture(tmp_path)
    original_file = Path(review_assembly.__file__).resolve()
    root = original_file.parents[1]
    tree = ast.parse(original_file.read_bytes())
    function = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                    and item.name == "runtime_source_fingerprint")
    assignment = next(item for item in function.body if isinstance(item, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "files" for target in item.targets))
    paths = ast.literal_eval(assignment.value)
    assert len(paths) == 97
    required = {"runs/execution_clock.py", "runs/execution_clock_store.py", "runs/execution_clock_schema.py",
                "db/models.py", "settings.py", "runtime/adaptive_budget.py"}
    assert required <= set(paths)
    copied_root = tmp_path/"qualified-source"/"agent_hub"
    for name in paths:
        target = copied_root/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root/name, target)
    with monkeypatch.context() as scoped:
        scoped.setattr(review_assembly, "__file__", str(copied_root/"runtime/review_assembly.py"))
        original_sha = review_assembly.runtime_source_fingerprint()
        current, reason = await assembled.broker._compile(assembled.s.task.tenant_id)
        assert current is not None and reason == "ready"
        for name in ("runs/execution_clock.py", "runs/execution_clock_store.py", "runs/execution_clock_schema.py"):
            file = copied_root/name
            original = file.read_bytes()
            file.write_bytes(original+b"\n# altered authority source\n")
            assert review_assembly.runtime_source_fingerprint() != original_sha
            rejected, _ = await assembled.broker._compile(assembled.s.task.tenant_id)
            assert rejected is None
            file.write_bytes(original)
    assert assembled.state.calls == []


@pytest.mark.parametrize("cleanup_confirmed", [True, False])
async def test_actual_runtime_approval_parks_after_generator_cleanup_only(
    db: SQLClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    caplog: pytest.LogCaptureFixture, cleanup_confirmed: bool,
) -> None:
    from agent_hub.domain.runs import RunStatus
    from agent_hub.runtime.contracts import EventKind
    from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

    assembled = await fixture(tmp_path, mode=TaskMode.DISCUSS)
    assembled.broker._runtime_authorization_timeout_seconds = 1000.0
    db.tenant = assembled.s.task.tenant_id
    text = "inspect notes"
    original = _capture_authenticated_input(AuthenticatedPrincipal(db.actor, db.tenant, Role.OPERATOR),
                                           text, {"selection": {"mode": "discuss"}})
    closed = []
    parked = []

    class Runtime(StreamRuntimeStub):
        mode = TaskMode.DISCUSS

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            try:
                db.now += timedelta(seconds=5)
                await db.waiting(context.run_id)
                yield RunEvent(kind=EventKind.APPROVAL_REQUESTED, sequence=1, run_id=context.run_id,
                    approval_id="approval_clock_transaction", actor="implementer", action="capability_tool",
                    reason="fixture capability requires approval",
                    payload={"approval_id": "approval_clock_transaction"})
                value = db.checkpoint(context.run_id)
                if context.routing_decision.get("engineering_diagnostic_allowance_version") == 1:
                    from agent_hub.runtime.contracts import (
                        RuntimeCheckpoint,
                        _freeze_object,
                    )

                    value = RuntimeCheckpoint(id=value.id, tenant_id=value.tenant_id,
                        run_id=value.run_id, mode=value.mode, runtime_type=value.runtime_type,
                        runtime_version=value.runtime_version, state=_freeze_object({
                            **value.state, "engineering_diagnostic_allowance_version": 1,
                            "engineering_diagnostic_consumed_through": 0,
                            "remaining_timeout_seconds": context.timeout_seconds,
                            "remaining_absolute_timeout_seconds": context.timeout_seconds,
                            "usage": {"tokens": 0, "cost_usd": "0.00"},
                            "model_ledger": (), "artifact_registry": {},
                        }, name="actual runtime approval diagnostic checkpoint"))
                yield RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=2, run_id=context.run_id,
                               checkpoint=value)
            finally:
                db.now += timedelta(seconds=5)
                closed.append(cleanup_confirmed)
                if not cleanup_confirmed:
                    raise ProcessCleanupIncomplete("fixture cleanup was not confirmed")

    service = RunService(db.repository, runtime_registry=RuntimeRegistry((Runtime(),)),
        router=None, task_queue=cast(TaskQueue, object()), runtime_timeout_seconds=50.,
        runtime_authorization_timeout_seconds=1000., review_broker=assembled.broker)
    submitted = await service.submit(tenant_id=db.tenant, actor_id=db.actor, actor_role=Role.OPERATOR,
        message=text, mode=TaskMode.DISCUSS, authenticated_input=original, skip_evolution_proposal=True,
        sandbox_profile="none", requested_permissions=())
    # The deterministic native runtime has no model operation. The actual model
    # assembly/gateway is verified separately by the same-pool case above.
    from agent_hub.runs.execution_clock import bound_clock
    from agent_hub.runtime.review_assembly import ReviewExecution

    async def prepare_boundary(context: TaskContext, *, worker_id: str) -> ReviewExecution | None:
        live = bound_clock(context.run_id, context.execution_token)
        assert live.remaining().effective_seconds > 0
        record = await db.repository.get(context.tenant_id, context.run_id)
        assert record.routing_decision is not None
        assert record.routing_decision["execution_clock_version"] == 2
        assert record.worker_id == worker_id and record.worker_lease_token == context.execution_token
        return None

    monkeypatch.setattr(assembled.broker, "prepare", prepare_boundary)
    original_park = db.repository.park_execution_clock

    async def park(tenant_id: UUID, run_id: UUID, *, worker_id: str,
                   worker_lease_token: UUID, quiescent: bool) -> None:
        assert closed == [True]
        parked.append(True)
        await original_park(tenant_id, run_id, worker_id=worker_id,
                            worker_lease_token=worker_lease_token, quiescent=quiescent)

    monkeypatch.setattr(db.repository, "park_execution_clock", park)
    result = await service.execute(submitted.id)
    clock = await db.clock(submitted.id)
    assert closed == [cleanup_confirmed]
    if cleanup_confirmed:
        assert result.status is RunStatus.WAITING_APPROVAL
        assert parked == [True] and clock.active_since is None and clock.spent_seconds == 10.
    else:
        # The original fail_run deliberately preserves an already committed
        # WAITING_APPROVAL after the checkpoint drain completed. Uncertain
        # cleanup must still never park/refund this active execution interval.
        assert result.status is RunStatus.WAITING_APPROVAL
        assert parked == [] and clock.active_since is not None
        assert clock.remaining(db.now).active_seconds <= 40.
        assert clock.remaining(db.now + timedelta(seconds=500)).active_seconds == 0.
        assert any("error_type=ProcessCleanupIncomplete" in record.getMessage()
                   for record in caplog.records)
    assert assembled.state.calls == []
