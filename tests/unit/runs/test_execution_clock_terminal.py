"""Confirmed quiet interval settlement and actual bounded consumers; no Root."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runs.execution_clock import (
    ClockPolicy,
    ClockSnapshot,
    activate,
    execution_boundary,
)
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.adaptive_budget import AdaptiveDeadline
from agent_hub.runtime.contracts import ExecutionRuntime, RunEvent, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.harness.review_typed_fixture import StreamRuntimeStub
from tests.unit.runs.test_execution_clock_store import SQLClock, db  # noqa: F401
from tests.unit.runtime.test_review_assembly import fixture


@pytest.mark.parametrize("uncertain", [False, True])
async def test_confirmed_failure_closes_once_before_controlled_same_run_repair_wait(
    db: SQLClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncertain: bool,  # noqa: F811
) -> None:
    from agent_hub.runtime.review_assembly import ReviewExecution
    from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

    assembled = await fixture(tmp_path)
    assembled.broker._runtime_authorization_timeout_seconds = 1000.0
    db.tenant = assembled.s.task.tenant_id
    text = "inspect notes"
    original = _capture_authenticated_input(AuthenticatedPrincipal(db.actor, db.tenant, Role.OPERATOR),
                                           text, {"selection": {"mode": "direct"}})
    closed = []

    class Runtime(StreamRuntimeStub):
        mode = TaskMode.DIRECT

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            try:
                db.now += timedelta(seconds=5)
                raise RuntimeError("fixture execution failed")
                yield RunEvent(kind="runtime.completed", sequence=1, run_id=context.run_id)
            finally:
                db.now += timedelta(seconds=5)
                closed.append(not uncertain)
                if uncertain:
                    raise ProcessCleanupIncomplete("fixture external cleanup is unknown")

    service = RunService(db.repository, runtime_registry=RuntimeRegistry((Runtime(),)), router=None,
        task_queue=cast(TaskQueue, object()), runtime_timeout_seconds=50.,
        runtime_authorization_timeout_seconds=1000., review_broker=assembled.broker)
    submitted = await service.submit(tenant_id=db.tenant, actor_id=db.actor, actor_role=Role.OPERATOR,
        message=text, mode=TaskMode.DIRECT, authenticated_input=original, skip_evolution_proposal=True,
        sandbox_profile="none", requested_permissions=())

    async def no_model_operation(context: TaskContext, *, worker_id: str) -> ReviewExecution | None:
        assert context.execution_token is not None and worker_id
        return None

    monkeypatch.setattr(assembled.broker, "prepare", no_model_operation)
    result = await service.execute(submitted.id)
    assert result.status is RunStatus.FAILED and closed == [not uncertain]
    state = await db.clock(submitted.id)
    hard = state.hard_deadline
    if uncertain:
        assert state.active_since is not None
        db.now += timedelta(seconds=500)
        assert state.remaining(db.now).active_seconds == 0.
        return
    assert state.active_since is None and state.spent_seconds == 10.
    db.now += timedelta(seconds=500)
    assert (await db.clock(submitted.id)).remaining(db.now).active_seconds == 40.
    # The separately guarded repair-policy decision is outside this accounting
    # regression. Its same-run QUEUED transition is represented under the real
    # row lock; claim/snapshot remain actual repository SQL and cannot mint a pool.
    async with db.factory.begin() as session:
        row = await db.repository.get_for_update(session, submitted.id)
        row.status = RunStatus.QUEUED.value
    db.token = uuid4()
    await db.claim(submitted.id)
    resumed, _ = await db.repository.execution_clock_snapshot(db.tenant, submitted.id,
        worker_id=db.worker, worker_lease_token=db.token, checkpoint=None, progress_units=0)
    assert resumed.hard_deadline == hard and resumed.spent_seconds == 10.
    assert resumed.remaining(db.now).active_seconds == 40.
    assert assembled.state.calls == []


@execution_boundary
async def test_actual_adaptive_renew_never_exceeds_sealed_soft_remaining(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from agent_hub.runs import execution_clock as clock_module

    now = datetime.now(UTC)
    token = uuid4()
    policy = ClockPolicy(100., 1000., 10., 5., "a"*64)
    state = ClockSnapshot(policy, now+timedelta(seconds=1000), 9., now, "worker", token, 0, 1)
    activate(state, now, run_id=uuid4(), worker_token=token)
    current = time.monotonic()
    budget = AdaptiveDeadline.create(now=current, initial_seconds=10., soft_seconds=100.,
                                     absolute_seconds=100., complexity_units=1)
    assert budget.extension_per_unit == 5.
    budget.renew_for_progress(progress_units=1, now=time.monotonic(), runway_seconds=100.)
    actual = clock_module.live_remaining(progress_units=1)
    assert actual is not None
    assert 0 < budget.remaining(now=time.monotonic()) <= actual.soft_seconds + 1e-6
    assert actual.soft_seconds <= 6.


@execution_boundary
async def test_actual_action_continuation_uses_current_remaining_without_historical_double_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.capabilities.execution_authority import bind_execution_authority
    from agent_hub.models.physical_budget import _bind_run_budget
    from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
    from agent_hub.runtime.streams import closing_runtime_events
    from tests.unit.runtime.test_plain_file_action import note_case

    case = note_case(tmp_path, mode="default")
    children: list[ActionApprovedDirectRuntime] = []
    observed_remaining: list[float] = []
    original_factory = case.runtime._runtime_for

    async def captured_child(context: TaskContext) -> ExecutionRuntime:
        child = await original_factory(context)
        assert isinstance(child, ActionApprovedDirectRuntime)
        children.append(child)
        original_child_run = child.run

        async def observe_deadline(current: TaskContext) -> AsyncIterator[RunEvent]:
            async with closing_runtime_events(original_child_run(current)) as events:
                async for event in events:
                    if child._action_deadline > 0:
                        observed_remaining.append(child._action_deadline - asyncio.get_running_loop().time())
                    yield event

        monkeypatch.setattr(child, "run", observe_deadline)
        return child

    monkeypatch.setattr(case.runtime, "_runtime_for", captured_child)
    total = min(40., case.task.timeout_seconds)
    charged = total / 4.
    remaining = total-charged
    token = case.task.execution_token
    assert token is not None
    task = case.task.model_copy(update={"timeout_seconds": total, "routing_decision": {
        **case.task.routing_decision, "execution_clock_version": 2,
        "runtime_timeout_seconds": total, "runtime_timeout_absolute_seconds": total}})
    case.task = task
    route: dict[str, object] = dict(task.routing_decision)
    case.runs.record = replace(case.runs.record, routing_decision=route)
    now = datetime.now(UTC)
    policy = ClockPolicy(total, 1000., total, total, "a"*64)
    activate(ClockSnapshot(policy, now+timedelta(seconds=1000), 0., now,
        case.budget.lease.worker_id, token, 0, 1), now, run_id=task.run_id, worker_token=token)
    with bind_execution_authority(task, worker_id=case.budget.lease.worker_id), _bind_run_budget(case.budget):
        async with closing_runtime_events(case.runtime.run(task)) as stream:
            waiting = [event async for event in stream]
        saved = waiting[-1].checkpoint
        assert saved is not None and saved.runtime_type == "direct_action", [
            (str(event.kind), event.reason) for event in waiting
        ]
        saved = saved.model_copy(update={"state": {**saved.state, "elapsed_seconds": charged,
                                                  "timeout_seconds": total}})
        # Recompute the contract hash rather than altering original identity fields.
        from agent_hub.runtime.contracts import RuntimeCheckpoint
        saved = RuntimeCheckpoint.from_payload({**saved.to_payload(), "state_sha256": saved.recompute_state_sha256()})
        case.runs.exact = False
        await case.runtime.restore_checkpoint(saved)
        current = task.model_copy(update={"checkpoint": saved, "timeout_seconds": remaining})
        observed = datetime.now(UTC)
        activate(ClockSnapshot(policy, now+timedelta(seconds=1000), charged, observed,
            case.budget.lease.worker_id, token, 0, 2), observed, run_id=task.run_id, worker_token=token)
        async with closing_runtime_events(case.runtime.run(current)) as stream:
            again = [event async for event in stream]
        next_saved = again[-1].checkpoint
        assert next_saved is not None and next_saved.state['timeout_seconds'] == total
        charged += total / 8.
        remaining = total-charged
        next_saved = next_saved.model_copy(update={"state": {**next_saved.state,
                                                            "elapsed_seconds": charged}})
        next_saved = RuntimeCheckpoint.from_payload({**next_saved.to_payload(),
                                                    "state_sha256": next_saved.recompute_state_sha256()})
        case.runs.exact = True
        await case.runtime.restore_checkpoint(next_saved)
        current = task.model_copy(update={"checkpoint": next_saved, "timeout_seconds": remaining})
        observed = datetime.now(UTC)
        activate(ClockSnapshot(policy, now+timedelta(seconds=1000), charged, observed,
            case.budget.lease.worker_id, token, 0, 3), observed, run_id=task.run_id, worker_token=token)
        async with closing_runtime_events(case.runtime.run(current)) as stream:
            resumed = [event async for event in stream]
        assert resumed[-1].kind == "runtime.completed"
        assert children and children[-1]._action_deadline == 0.0
        assert observed_remaining
        left = observed_remaining[-1]
        assert remaining-charged/2 < left <= remaining
    assert len(case.calls) == 1 and case.fixed.calls == 0
