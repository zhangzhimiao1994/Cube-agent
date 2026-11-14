"""Independent finite server authorization, actual broker and persisted lifecycle."""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.models import RunAdmissionPoolRow
from agent_hub.domain.runs import TaskMode
from agent_hub.models.physical_budget import _run_budget
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runs.execution_clock import activate, execution_boundary
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.contracts import RunEvent, TaskContext
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.runtime.worker_execution import worker_runtime_events
from tests.unit.runs.test_execution_clock_store import SQLClock, db  # noqa: F401
from tests.unit.runtime.test_review_assembly import fixture


@execution_boundary
async def test_actual_server_h7200_a3600_survives_59min_approval_without_new_pool(
    db: SQLClock, tmp_path: Path,  # noqa: F811
) -> None:
    assembled = await fixture(tmp_path, mode=TaskMode.DISCUSS)
    # Constructor configuration represents the trusted server setting, not routing.
    from agent_hub.runtime.review_assembly import ReviewAssemblyBroker

    old = assembled.broker
    with pytest.raises(ValueError, match="invalid original review timeout"):
        await old.submission_profile(assembled.s.task.tenant_id, timeout_seconds=7200.)
    with pytest.raises(ValueError, match="server-approved"):
        await old.clock_submission_profile(assembled.s.task.tenant_id, timeout_seconds=7200.)
    broker = ReviewAssemblyBroker(
        config_service=old._configs, config_getter=old._get_config,
        run_repository=db.repository, session_factory=db.factory,
        secret_service=old._secrets, capacity_factory=old._capacity,
        transport=old._transport, billing_contracts=old._contracts,
        verification=old._verification,
        runtime_authorization_timeout_seconds=7200.,
    )
    db.tenant = assembled.s.task.tenant_id
    text = "inspect existing project with human approval"
    principal = AuthenticatedPrincipal(db.actor, db.tenant, Role.OPERATOR)
    original = _capture_authenticated_input(principal, text, {"mode": "discuss"})
    service = RunService(db.repository,
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DISCUSS),)),
        router=None, task_queue=cast(TaskQueue, object()),
        runtime_timeout_seconds=3600., runtime_token_budget=40000,
        runtime_authorization_timeout_seconds=7200., review_broker=broker)
    submitted = await service.submit(tenant_id=db.tenant, actor_id=db.actor,
        actor_role=Role.OPERATOR, message=text, mode=TaskMode.DISCUSS,
        authenticated_input=original, skip_evolution_proposal=True)
    original_clock = await db.clock(submitted.id)
    submitted_record = await db.repository.get(db.tenant, submitted.id)
    assert submitted_record.routing_decision is not None
    absolute_tokens = submitted_record.routing_decision['runtime_token_absolute_tokens']
    plan_tokens = submitted_record.routing_decision['runtime_plan_token_budget']
    assert type(absolute_tokens) is int and type(plan_tokens) is int
    original_tokens = min(absolute_tokens, plan_tokens)
    assert original_clock.policy.active_seconds == 3600.
    assert original_clock.policy.authorization_seconds == 7200.
    assert original_clock.hard_deadline == db.now + timedelta(seconds=7200)
    await db.claim(submitted.id)
    pools = []

    class Runtime(UnavailableRuntime):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            budget = _run_budget.get()
            assert budget is not None
            assert 0 < context.timeout_seconds <= 3600.
            pools.append(budget._pool)
            yield RunEvent(kind="runtime.completed", sequence=1, run_id=context.run_id)

    async def observe(*, prove_missing_private_witness: bool = False) -> None:
        record = await db.repository.get(db.tenant, submitted.id)
        clock = await db.clock(submitted.id)
        task = assembled.s.task.model_copy(update={
            "run_id": submitted.id, "actor_id": db.actor, "actor_role": Role.OPERATOR,
            "routing_decision": record.routing_decision, "execution_token": db.token,
            "checkpoint": None, "timeout_seconds": clock.remaining(db.now).effective_seconds,
        })
        if prove_missing_private_witness:
            with pytest.raises(ValueError, match="private worker binding"):
                async with worker_runtime_events(Runtime(TaskMode.DISCUSS), task,
                    worker_id=db.worker, review_broker=broker):
                    pytest.fail("public routing must never authorize a longer pool")
            assert await db.count(RunAdmissionPoolRow) == 0
        activate(clock, db.now, run_id=submitted.id, worker_token=db.token)
        async with worker_runtime_events(Runtime(TaskMode.DISCUSS), task,
            worker_id=db.worker, review_broker=broker) as events:
            await anext(events)

    await observe(prove_missing_private_witness=True)
    db.now += timedelta(seconds=5)
    checkpoint = db.checkpoint(submitted.id).model_copy(update={"mode": TaskMode.DISCUSS})
    await db.waiting(submitted.id)
    await db.save(checkpoint)
    await db.park(submitted.id)
    db.now += timedelta(minutes=59)
    await db.approve(submitted.id)
    await db.claim(submitted.id)
    resumed = await db.clock(submitted.id)
    assert resumed.remaining(db.now).active_seconds == 3595.
    assert resumed.hard_deadline == original_clock.hard_deadline
    await observe()
    db.now += timedelta(seconds=79)
    after = (await db.clock(submitted.id)).remaining(db.now)
    assert after.active_seconds == 3516. and after.authorization_seconds == 3576.
    async with db.factory() as session:
        rows = list((await session.scalars(select(RunAdmissionPoolRow))).all())
        assert len(rows) == 1
        assert rows[0].original_token_grant == original_tokens
        assert rows[0].original_deadline == original_clock.hard_deadline
    assert len(pools) == 2 and pools[0].run_id == pools[1].run_id
    assert pools[0].original_token_grant == pools[1].original_token_grant == original_tokens
    assert assembled.state.calls == []
