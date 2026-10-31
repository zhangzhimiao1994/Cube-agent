"""Typed local review fixture; real fences/dispatch/storage, fixed IO and verdict only."""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.model_review import ModelReviewOutcome, _LiveReviewContext
from agent_hub.capabilities.review_context import (
    ConcreteModelReviewGate,
    TrustedReviewContextLoader,
)
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig, FrozenReviewAction
from agent_hub.capabilities.review_repository import (
    ReviewIntent,
    _CurrentReviewBinding,
    review_config_sha256,
)
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.db.models import (
    RunAdmissionPoolRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.admission_bounds import TrustedContextBound
from agent_hub.models.physical_budget import _bind_run_budget, _RunBudgetContext
from agent_hub.models.run_admission_repository import (
    PoolSnapshot,
    RunAdmissionRepository,
    _WorkerLease,
)
from agent_hub.runs.authorization import _AuthenticatedInput, _capture_authenticated_input
from agent_hub.runs.repository import RunRecord
from agent_hub.runtime.contracts import JsonValue, TaskContext


class _FenceSession:
    """Only the SQL IO boundary is doubled; production _locked/_fence still execute."""

    def __init__(self, record: RunRecord, evidence: _AuthenticatedInput, pool: PoolSnapshot) -> None:
        self.run = RunRow(
            id=record.id, tenant_id=record.tenant_id, actor_id=record.actor_id,
            actor_role=evidence.actor_role.value, request=evidence.message, mode="direct",
            status="running", blocked_by_run_id=None, routing_decision=dict(record.routing_decision or {}),
            worker_id=record.worker_id, worker_lease_token=record.worker_lease_token,
            worker_lease_expires_at=record.worker_lease_expires_at,
        )
        self.pool = RunAdmissionPoolRow(**asdict(pool), actor_id=evidence.actor_id,
            actor_role=evidence.actor_role.value, message_sha256=evidence.message_sha256,
            scope_sha256=evidence.scope_sha256)
        self.link = RunInputAuthorizationRow(
            run_id=record.id, tenant_id=record.tenant_id, turn_id=evidence.turn_id)
        self.turn = RunAuthenticatedInputRow(
            run_id=record.id, tenant_id=evidence.tenant_id, actor_id=evidence.actor_id,
            actor_role=evidence.actor_role.value, turn_id=evidence.turn_id,
            message=evidence.message, message_sha256=evidence.message_sha256,
            input_scope=evidence.input_scope, scope_sha256=evidence.scope_sha256)

    def in_transaction(self) -> bool:
        return True

    async def scalar(self, statement: Any) -> RunRow | RunAdmissionPoolRow:
        entity = statement.column_descriptions[0]["entity"]
        assert entity in (RunRow, RunAdmissionPoolRow), "unexpected SQL fixture read"
        return self.run if entity is RunRow else self.pool

    async def execute(self, statement: Any) -> SimpleNamespace:
        if statement.column_descriptions[0].get("entity") is RunInputAuthorizationRow:
            return SimpleNamespace(one_or_none=lambda: (self.link, self.turn))
        assert "clock_timestamp" in str(statement), "unexpected SQL fixture read"
        return SimpleNamespace(scalar_one=lambda: datetime.now(UTC))


class _BudgetRepository(RunAdmissionRepository):
    def __init__(self, session: _FenceSession) -> None:
        self.session = session

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, self.session)


class _Runs:
    def __init__(self, record: RunRecord, evidence: _AuthenticatedInput) -> None:
        self.record, self.evidence = record, evidence

    async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
        assert (tenant_id, run_id) == (self.record.tenant_id, self.record.id)
        return self.record

    async def load_authenticated_input(self, tenant_id: UUID, run_id: UUID) -> _AuthenticatedInput:
        await self.get(tenant_id, run_id)
        return self.evidence

    async def is_capability_approval_approved(self, *args: object, **kwargs: object) -> bool:
        return False

    async def is_capability_approval_scope_approved(self, *args: object, **kwargs: object) -> bool:
        return False

    async def is_conversation_capability_approved(self, *args: object, **kwargs: object) -> bool:
        return False

    async def begin_capability_approval(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("reviewed empty text unexpectedly requested human approval")

    async def record_capability_approval_review(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("model review must not manufacture ordinary approval")


class _Permits:
    def __init__(self) -> None:
        self.allowed: dict[UUID, tuple[str, str]] = {}
        self.effects: set[tuple[UUID, UUID, UUID, str]] = set()

    async def lookup_effect(
        self, tenant_id: UUID, run_id: UUID, authorization_turn_id: UUID, effect_digest: str,
    ) -> ReviewIntent | None:
        assert (tenant_id, run_id, authorization_turn_id, effect_digest) not in self.effects
        return None

    async def consume_permit(
        self, *, tenant_id: UUID, run_id: UUID, review_id: UUID,
        current_binding: _CurrentReviewBinding,
    ) -> bool:
        snapshot = current_binding.snapshot
        request = snapshot.request()
        assert (tenant_id, run_id) == (request.tenant_id, request.run_id)
        expected = self.allowed.pop(review_id, None)
        return expected == (snapshot.action_digest, snapshot.effect_digest)


class _Reviewer:
    def __init__(self, permits: _Permits) -> None:
        self.permits = permits
        self.calls: list[tuple[FrozenReviewAction, _LiveReviewContext]] = []

    async def review(
        self, snapshot: FrozenReviewAction, live_context: _LiveReviewContext,
    ) -> ModelReviewOutcome:
        self.calls.append((snapshot, live_context))
        review_id = uuid4()
        self.permits.allowed[review_id] = (snapshot.action_digest, snapshot.effect_digest)
        request = snapshot.request()
        self.permits.effects.add((request.tenant_id, request.run_id,
            live_context.budget.evidence.turn_id, snapshot.effect_digest))
        return ModelReviewOutcome("allow", "low", "authorized_action",
            snapshot.action_digest, snapshot.effect_digest, review_id=review_id)


@dataclass
class ReviewFixture:
    task: TaskContext
    budget: _RunBudgetContext
    loader: TrustedReviewContextLoader
    policy: DefaultRuntimeCapabilityPolicyGateway
    reviewer: _Reviewer
    harness: HarnessToolGateway
    request: HarnessToolCallRequest


def setup(tmp_path: Path) -> ReviewFixture:
    config = ApprovalReviewConfig(engine="rules_then_model", reviewer_logical_model="reviewer",
        max_input_tokens=20000, max_output_tokens=512, max_cost_usd_per_run="10.00")
    evidence = _capture_authenticated_input(
        AuthenticatedPrincipal(tenant_id=uuid4(), user_id=uuid4(), role=Role.OPERATOR),
        "Write the exact approved test file.", {"workspace": "synthetic"})
    run_id, token = uuid4(), uuid4()
    route: dict[str, JsonValue] = {
        "project_id": "demo", "workspace_session_id": "session",
        "sandbox_profile": "workspace_write", "permission_mode": "auto_review",
        "requested_permissions": ("workspace.read", "workspace.write", "command.run"),
        "execution_backend": "systemd", "runtime_timeout_seconds": 60,
        "runtime_timeout_absolute_seconds": 60, "runtime_token_absolute_tokens": 40000,
        "runtime_plan_token_budget": 40000,
    }
    task = TaskContext(tenant_id=evidence.tenant_id, run_id=run_id, actor_id=evidence.actor_id,
        actor_role=Role.OPERATOR, mode=TaskMode.DIRECT, execution_token=token,
        request=evidence.message, routing_decision=route)
    record = RunRecord(run_id, task.tenant_id, evidence.actor_id, task.request, task.mode,
        RunStatus.RUNNING, 1, datetime.now(UTC), dict[str, object](route), actor_role=Role.OPERATOR,
        worker_id="test-worker", worker_lease_token=token,
        worker_lease_expires_at=datetime.now(UTC)+timedelta(seconds=90))
    pool = PoolSnapshot(task.tenant_id, run_id, evidence.turn_id, review_config_sha256(config),
        40000, datetime.now(UTC)+timedelta(seconds=50), 40000, 8, Decimal(10),
        0, 0, 0, Decimal(0), False)
    budget = _RunBudgetContext(repository=_BudgetRepository(_FenceSession(record, evidence, pool)),
        pool=pool, evidence=evidence, lease=_WorkerLease("test-worker", token),
        config=config, deadline=time.monotonic()+45,
        trusted_contexts=(TrustedContextBound("selected", "primary", "openai/acting", 10000),
            TrustedContextBound("review-selected", "reviewer", "openai/reviewer", 10000)))
    runs = _Runs(record, evidence)
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path/"skills", tenant_id=task.tenant_id,
        workspace_root=tmp_path, generated_artifact_dir=tmp_path/"generated",
        project_workspace_dir=tmp_path, run_repository=runs, skill_sandboxes={})

    async def current_config(tenant_id: UUID) -> ApprovalReviewConfig:
        assert tenant_id == task.tenant_id
        return config

    async def no_human_grant(request: object) -> bool:
        return False

    policy = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()),
        runs, exact_human_approval_getter=no_human_grant)
    permits = _Permits()
    reviewer = _Reviewer(permits)
    loader = TrustedReviewContextLoader(
        runs, preparation_reader=backend.action_preparation_snapshot, config_getter=current_config)
    harness = HarnessToolGateway(backend, policy_gateway=policy,
        review_gate=ConcreteModelReviewGate(loader, reviewer, permits))
    request = HarnessToolCallRequest(run_id, "main_agent", "workspace.write_text",
        {"path": "hello.txt", "content": "original content"}, True, "workspace_write", "first")
    return ReviewFixture(task, budget, loader, policy, reviewer, harness, request)


async def invoke(state: ReviewFixture) -> HarnessToolCallResult:
    assert state.task.actor_id is not None and state.task.actor_role is not None
    with bind_execution_authority(state.task, worker_id=state.budget.lease.worker_id), _bind_run_budget(state.budget):
        return await state.harness.invoke(state.task.tenant_id, state.request,
            user_id=state.task.actor_id, role=state.task.actor_role)
