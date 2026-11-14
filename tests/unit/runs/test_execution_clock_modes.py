"""Ordinary and real AUTO routing preserve the original server-issued clock.

AUTO classification precedes run creation/claim: its elapsed time is currently
outside A, and H starts at run creation after routing. The controlled database
time advance records this limitation without changing any production policy.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.models import (
    RunAuthenticatedInputRow,
    RunExecutionClockRow,
    RunInputAuthorizationRow,
    RunOutboxRow,
    RunRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.routing.service import ModeRouter
from agent_hub.routing.types import (
    InMemoryDecisionTokenStore,
    RiskLevel,
    RouteAssessment,
    RouteSource,
)
from agent_hub.runs import execution_clock_store
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runs.execution_clock import _SUBMISSION, digest
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.runtime.review_assembly import ReviewAssemblyBroker
from tests.unit.runs.test_execution_clock_store import SQLClock, db  # noqa: F401
from tests.unit.runtime.test_review_assembly import fixture


class FixedClassification:
    """Only the model-classification boundary is deterministic; ModeRouter is real."""

    def __init__(self, mode: TaskMode, source: RouteSource, clock: SQLClock) -> None:
        self.mode, self.source, self.clock = mode, source, clock
        self.calls: list[str] = []
        self.original_policies: list[dict[str, object]] = []

    async def classify(self, task_text: str) -> RouteAssessment:
        self.calls.append(task_text)
        original = _SUBMISSION.get()
        assert original is not None, "server clock authorization must precede AUTO routing"
        self.original_policies.append(original.policy.payload())
        if self.source is RouteSource.CLASSIFIER:
            self.clock.now += timedelta(seconds=7)
        return RouteAssessment(
            mode=self.mode, confidence=0.99, reason="bounded mode fixture",
            estimated_seconds=50, estimated_cost_usd=Decimal(0),
            risk=RiskLevel.LOW, source=self.source, logical_model="router",
            deployment_id="router-" + self.source.value, provider_id="fixed",
        )


@pytest.mark.parametrize(
    ("requested_mode", "selected_mode"),
    [
        (TaskMode.DISPATCH, TaskMode.DISPATCH),
        (TaskMode.DISCUSS, TaskMode.DISCUSS),
        (TaskMode.HYBRID, TaskMode.HYBRID),
        (TaskMode.AUTO, TaskMode.DIRECT),
        (TaskMode.AUTO, TaskMode.DISPATCH),
        (TaskMode.AUTO, TaskMode.DISCUSS),
        (TaskMode.AUTO, TaskMode.HYBRID),
    ],
    ids=["ordinary-dispatch", "ordinary-discuss", "ordinary-hybrid",
         "auto-direct", "auto-dispatch", "auto-discuss", "auto-hybrid"],
)
async def test_submit_mode_routing_preserves_original_authorized_clock(
    db: SQLClock,  # noqa: F811 - imported existing SQL fixture
    tmp_path: Path, requested_mode: TaskMode, selected_mode: TaskMode,
) -> None:
    assembled = await fixture(tmp_path, mode=selected_mode)
    old = assembled.broker
    broker = ReviewAssemblyBroker(
        config_service=old._configs, config_getter=old._get_config,
        run_repository=db.repository, session_factory=db.factory,
        secret_service=old._secrets, capacity_factory=old._capacity,
        transport=old._transport, billing_contracts=old._contracts,
        verification=old._verification, runtime_authorization_timeout_seconds=7200.,
    )
    db.tenant = assembled.s.task.tenant_id
    primary = FixedClassification(selected_mode, RouteSource.CLASSIFIER, db)
    verifier = FixedClassification(selected_mode, RouteSource.VERIFIER, db)
    router = ModeRouter(primary, verifier, token_store=InMemoryDecisionTokenStore())
    service = RunService(
        db.repository, runtime_registry=RuntimeRegistry(tuple(
            UnavailableRuntime(mode) for mode in (
                TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID))),
        router=router, task_queue=cast(TaskQueue, object()),
        runtime_timeout_seconds=50., runtime_authorization_timeout_seconds=7200.,
        review_broker=broker,
    )
    message = "Consider the supplied notes."
    original = _capture_authenticated_input(
        AuthenticatedPrincipal(db.actor, db.tenant, Role.OPERATOR), message,
        {"mode": requested_mode.value},
    )
    before_route = db.now
    # Ordinary callers supply no clock version or implementation durations.
    submitted = await service.submit(
        tenant_id=db.tenant, actor_id=db.actor, actor_role=Role.OPERATOR,
        message=message, mode=requested_mode, authenticated_input=original,
        skip_evolution_proposal=True,
    )
    assert submitted.status is RunStatus.QUEUED and submitted.mode is selected_mode
    async with db.factory() as session:
        row = await session.get(RunRow, submitted.id)
        assert row is not None and row.mode == selected_mode.value
        route = row.routing_decision
        assert route is not None and route["requested_mode"] == requested_mode.value
        assert type(route["execution_clock_version"]) is int and route["execution_clock_version"] == 2
        assert route["runtime_timeout_absolute_seconds"] == 50.
        assert route["runtime_plan_timeout_seconds"] == 50.
        profile = route["approval_review_profile"]
        assert isinstance(profile, dict) and profile["timeout_seconds"] == 7200.
        clock = await execution_clock_store.locked(session, row)
        assert clock is not None
        saved = execution_clock_store.snapshot(clock)
        assert saved.policy.active_seconds == saved.policy.initial_soft_seconds == 50.
        assert saved.policy.authorization_seconds == 7200.
        assert saved.policy.policy_sha256 == digest(profile)
        assert saved.hard_deadline == row.created_at + timedelta(seconds=7200)
        assert row.execution_clock_sha256 == clock.binding_sha256
        assert saved.spent_seconds == 0. and saved.active_since is None
        assert saved.remaining(db.now).active_seconds == 50.
        link = await session.scalar(select(RunInputAuthorizationRow).where(
            RunInputAuthorizationRow.run_id == row.id))
        assert link is not None and clock.authorization_turn_id == link.turn_id
        authenticated = await session.get(RunAuthenticatedInputRow, link.turn_id)
        assert authenticated is not None and authenticated.message == message
        if requested_mode is TaskMode.AUTO:
            assert route["status"] == "ready"
            assert route["router_selected_mode"] == route["main_agent_selected_mode"] == selected_mode.value
            assert route["main_agent_adjusted"] is False
            assessments = route["assessments"]
            assert isinstance(assessments, list) and len(assessments) == 2
            assert primary.calls == verifier.calls == [message]
            assert primary.original_policies == verifier.original_policies == [clock.policy]
            assert row.created_at == before_route + timedelta(seconds=7)
        else:
            assert primary.calls == verifier.calls == []
            assert row.created_at == before_route
    assert await db.count(RunExecutionClockRow) == await db.count(RunOutboxRow) == 1
    assert _SUBMISSION.get() is None
    assert assembled.state.calls == []
