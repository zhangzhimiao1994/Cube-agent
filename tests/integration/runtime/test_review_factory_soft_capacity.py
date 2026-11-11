"""Real submit/claim -> worker/context -> broker/factory/Gateway/PG settlement.

Dedicated test PG and the existing owned Unix Redis are mandatory. Only provider
HTTP is MockTransport. No deadline helper, pool, factory, adapter or admission
repository is replaced. This tests one admitted call, not project delivery.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID

import pytest
from redis.asyncio import Redis
from sqlalchemy import select

from agent_hub.api.routers.runs import CreateRunRequest
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.models import RunAdmissionAttemptRow, RunAdmissionPoolRow, RunRow
from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import CapacityLease, CapacityPool
from agent_hub.models.physical_budget import _run_budget
from agent_hub.models.run_admission_repository import RunAdmissionRepository
from agent_hub.models.types import Deployment
from agent_hub.runs import service as service_module
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import RunEvent, TaskContext
from agent_hub.runtime.defaults import ConfigBackedDiscussionRuntime, _deployments
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.runtime.review_assembly import ReviewExecution
from tests.integration.capabilities.test_review_repository import (
    database_url as database_url,  # noqa: PLC0414
)
from tests.integration.models.run_admission_typed_fixture import Storage
from tests.integration.models.test_run_admission import storage as storage  # noqa: PLC0414
from tests.integration.runtime.test_model_review_modes import (
    own_redis as own_redis,  # noqa: PLC0414
)
from tests.integration.runtime.test_model_review_modes import (
    owned_crewai_storage as owned_crewai_storage,  # noqa: PLC0414
)
from tests.integration.runtime.test_model_review_modes_helper import (
    assert_wire_receipts,
    build_case,
    fixture_platform,
    submission_request,
    submitted_input_scope,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]
SOFT = 3.0
RELEASE_AFTER_SOFT = 4.0


class LocalQueue:
    """RunService's queue seam; no Celery/broker or second execution is launched."""

    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        assert isinstance(run_id, UUID) and idempotency_key


def remaining_context(context: TaskContext, *, forged: bool) -> TaskContext:
    """Round-trip public DTO validation, then restore only original private fields."""
    payload = context.to_payload()
    payload["timeout_seconds"] = min(SOFT, context.timeout_seconds)
    if forged:
        # Persisted claim is operator/explicit. A context-only promotion must
        # not turn that into an authenticated scale-soft policy.
        route = dict(payload["routing_decision"])
        route["runtime_timeout_source"] = "project_scale_soft_budget"
        route["engineering_soft_admission_version"] = 1
        payload["routing_decision"] = route
    public = TaskContext.from_payload(payload)
    values = {name: getattr(public, name) for name in TaskContext.model_fields}
    values["execution_token"] = context.execution_token
    values["instruction_context"] = context.instruction_context
    rebuilt = TaskContext.model_validate(values, strict=True)
    assert rebuilt.timeout_seconds <= context.timeout_seconds
    # DTO validation uses built-in JSON containers. Original worker processing
    # freezes routing only when it strips an actual private locator below.
    assert isinstance(rebuilt.routing_decision, Mapping)
    assert isinstance(rebuilt.routing_decision["approval_review_profile"], Mapping)
    assert rebuilt.routing_decision["approval_review_profile"] == context.routing_decision["approval_review_profile"]
    assert rebuilt.execution_token == context.execution_token
    return rebuilt


@pytest.mark.parametrize("boundary", ["trusted_soft", "explicit_short", "forged", "past_hard"])
async def test_claimed_factory_capacity_crosses_only_trusted_soft_boundary(
    storage: Storage,
    own_redis: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    body = CreateRunRequest.model_validate(
        {
            **submission_request(TaskMode.DISCUSS).model_dump(mode="json"),
            "message": "Build a small Python CLI project with source files, tests and README. project_scale=small.",
            "runtime_timeout_seconds": 0.3 if boundary in {"explicit_short", "forged"} else None,
        }
    )
    case = await build_case(storage, tmp_path, TaskMode.DISCUSS, own_redis, body=body)
    assert type(case.runtime) is ConfigBackedDiscussionRuntime
    actor = case.context.actor_id
    assert actor is not None
    principal = AuthenticatedPrincipal(actor, case.context.tenant_id, Role.OPERATOR)
    evidence = _capture_authenticated_input(principal, body.message, submitted_input_scope(body))
    service = RunService(
        case.repository,
        runtime_registry=RuntimeRegistry([case.runtime]),
        router=None,
        task_queue=LocalQueue(),
        review_broker=case.broker,
        worker_id="soft-capacity-native-fixture",
    )
    # The original submit creates the profile/scale route; original execute
    # below claims it and mints engineering/ack policy. No positive flag edits.
    submitted = await service.submit(
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        actor_role=principal.role,
        message=body.message,
        mode=body.mode,
        agent_ids=body.agent_ids,
        conversation_id=body.conversation_id,
        project_id=body.project_id,
        workspace_session_id=body.workspace_session_id,
        sandbox_profile=body.sandbox_profile,
        requested_permissions=body.requested_permissions,
        permission_mode=body.permission_mode,
        execution_backend=body.execution_backend,
        runtime_timeout_seconds=body.runtime_timeout_seconds,
        authenticated_input=evidence,
    )
    storage[1].append(submitted.id)
    record = await case.repository.get(principal.tenant_id, submitted.id)
    assert record.mode is TaskMode.DISCUSS
    route_before = record.routing_decision
    assert isinstance(route_before, Mapping)
    expected_source = "operator" if boundary in {"explicit_short", "forged"} else "project_scale_soft_budget"
    assert route_before["runtime_timeout_source"] == expected_source
    assert isinstance(route_before["approval_review_profile"], dict)

    deployments = _deployments(fixture_platform())
    holder = await case.runtime._capacity_factory(principal.tenant_id, deployments)
    assert type(holder) is CapacityPool
    await holder.initialize()
    primary = tuple(d for d in deployments if d.logical_model == "primary")
    assert len(primary) == 1
    limit = await holder.effective_limit(primary[0].quota_scope_id)
    assert type(limit) is int and limit >= 1
    held: list[CapacityLease] = []
    queued = asyncio.Event()
    entered_worker = asyncio.Event()
    observed: dict[str, Any] = {}
    real_try = CapacityPool._try_acquire

    async def observe_busy(
        pool: CapacityPool, deployment: Deployment, estimated_tokens: int, *, timeout: float,
    ) -> CapacityLease | None:
        result = await real_try(pool, deployment, estimated_tokens, timeout=timeout)
        budget = _run_budget.get()
        if (pool is not holder and deployment.logical_model == "primary" and result is None
                and budget is not None and budget.run_id == submitted.id and not queued.is_set()):
            observed["budget"] = budget
            observed["pool_before"] = budget._pool
            observed["queued_at"] = time.monotonic()
            queued.set()
        return result

    real_worker = service_module.worker_runtime_events
    real_prepare = case.broker.prepare

    async def observe_prepare(context: TaskContext, *, worker_id: str) -> ReviewExecution | None:
        # This observation is reached through original worker_runtime_events,
        # after its conditional private-locator removal and before real prepare.
        rebuilt = observed["rebuilt_context"]
        private = {"project_verification_receipt", "approval_checkpoint_worker_token"}
        expected_route = {key: value for key, value in rebuilt.routing_decision.items() if key not in private}
        assert context.run_id == submitted.id and context.execution_token == rebuilt.execution_token
        assert isinstance(context.routing_decision, Mapping)
        assert isinstance(context.routing_decision["approval_review_profile"], Mapping)
        assert dict(context.routing_decision) == expected_route
        assert context.routing_decision["approval_review_profile"] == rebuilt.routing_decision["approval_review_profile"]
        assert not private.intersection(context.routing_decision)
        if private.intersection(rebuilt.routing_decision):
            # Keep the real worker's deep-freeze assertions at its actual
            # boundary. Fresh claims need not carry either private locator.
            assert isinstance(context.routing_decision, MappingProxyType)
            assert isinstance(context.routing_decision["approval_review_profile"], MappingProxyType)
        observed["prepared_context"] = context
        return await real_prepare(context, worker_id=worker_id)

    @asynccontextmanager
    async def observe_worker(
        runtime: object, context: TaskContext, **kwargs: Any,
    ) -> AsyncIterator[AsyncIterator[RunEvent]]:
        assert runtime is case.runtime and context.run_id == submitted.id
        current = await case.repository.get(context.tenant_id, context.run_id)
        claimed = current.routing_decision
        assert isinstance(claimed, Mapping)
        assert context.routing_decision["runtime_timeout_source"] == claimed["runtime_timeout_source"] == expected_source
        assert isinstance(context.routing_decision["approval_review_profile"], Mapping)
        assert isinstance(claimed["approval_review_profile"], dict)
        assert dict(context.routing_decision["approval_review_profile"]) == claimed["approval_review_profile"]
        if boundary in {"trusted_soft", "past_hard"}:
            for key in ("engineering_soft_admission_version", "engineering_checkpoint_ack_version",
                        "engineering_progress_budget_version", "engineering_action_version"):
                assert type(claimed[key]) is int and claimed[key] == 1
        else:
            assert "engineering_soft_admission_version" not in claimed
        if boundary == "past_hard":
            # Age only the dedicated fixture run, before its pool is first
            # opened. Original hard route/profile values are never extended.
            # A prepare-time refusal is valid here: this negative does not
            # claim that the request necessarily reached the Redis queue.
            hard = claimed["runtime_timeout_absolute_seconds"]
            assert isinstance(hard, (int, float))
            async with case.factory() as session, session.begin():
                row = await session.get(RunRow, context.run_id)
                assert row is not None
                row.created_at = datetime.now(UTC) - timedelta(seconds=float(hard) - 1.0)
        rebuilt = remaining_context(context, forged=boundary == "forged")
        if boundary != "forged":
            assert rebuilt.routing_decision == context.routing_decision
        observed["claimed_context"] = context
        observed["rebuilt_context"] = rebuilt
        entered_worker.set()
        # Same original prepare/budget binding used by collect(case), while
        # actual RunService continues to persist/ack its genuine runtime events.
        async with real_worker(runtime, rebuilt, **kwargs) as stream:
            yield stream

    monkeypatch.setattr(CapacityPool, "_try_acquire", observe_busy)
    monkeypatch.setattr(service_module, "worker_runtime_events", observe_worker)
    monkeypatch.setattr(case.broker, "prepare", observe_prepare)
    release_task: asyncio.Task[None] | None = None
    execution: asyncio.Task[object] | None = None

    async def release_after_boundary() -> None:
        await queued.wait()
        await asyncio.sleep(1.04 if boundary == "past_hard" else RELEASE_AFTER_SOFT)
        observed["released_at"] = time.monotonic()
        assert not case.wire.calls, "provider entered before the real busy slot was released"
        while held:
            assert await holder.release(held.pop())

    try:
        for _ in range(limit):
            held.append(await holder.acquire(primary, wait_timeout=1, estimated_tokens=1))
        release_task = asyncio.create_task(release_after_boundary())
        execution = asyncio.create_task(service.execute(submitted.id))
        # This bounds the local fixture, not any production/request deadline.
        await asyncio.wait_for(execution, timeout=25)
        assert entered_worker.is_set(), "original submit/claim never reached the original worker entry"
        assert "prepared_context" in observed, "original worker never called the real broker prepare boundary"
        if boundary == "trusted_soft":
            assert queued.is_set(), "request did not exercise native Redis pretransport contention"
            await asyncio.wait_for(release_task, timeout=2)
            assert observed["released_at"] - observed["queued_at"] >= RELEASE_AFTER_SOFT
            assert case.wire.calls, "trusted admitted request died at remaining-soft instead of sending"
            budget = observed["budget"]
            assert type(budget).__name__ == "_RunBudgetContext"
            assert case.wire.budgets and all(b is budget for b in case.wire.budgets)
            assert all(body.closed for body in case.wire.bodies)
            pool = await RunAdmissionRepository(case.factory).get_pool(principal.tenant_id, submitted.id)
            assert pool is not None
            before = observed["pool_before"]
            for key in ("tenant_id", "run_id", "authorization_turn_id", "config_sha256",
                        "original_token_grant", "original_deadline"):
                assert getattr(pool, key) == getattr(before, key)
            async with case.factory() as session:
                attempts = list((await session.scalars(select(RunAdmissionAttemptRow).where(
                    RunAdmissionAttemptRow.run_id == submitted.id))).all())
                pools = list((await session.scalars(select(RunAdmissionPoolRow).where(
                    RunAdmissionPoolRow.run_id == submitted.id))).all())
            assert len(pools) == 1 and attempts
            assert all(row.state == "completed" for row in attempts), "known local responses must settle"
            assert_wire_receipts(attempts, case.wire.attempts, pool)
            assert pool.token_charged == len(case.wire.calls) * 5 > 0
            assert datetime.now(UTC) < pool.original_deadline
        else:
            # Only trusted_soft asserts queued. Explicit/forged/past-hard may
            # correctly refuse at prepare/admission before any capacity scan.
            assert not case.wire.calls, "explicit, forged or expired-hard policy admitted provider IO"
            async with case.factory() as session:
                attempts = list((await session.scalars(select(RunAdmissionAttemptRow).where(
                    RunAdmissionAttemptRow.run_id == submitted.id))).all())
            assert not attempts, "pretransport rejection must not fabricate an issued/UNKNOWN attempt"
    finally:
        for task in (execution, release_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (execution, release_task) if t is not None), return_exceptions=True)
        while held:
            await holder.release(held.pop())
