from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import AdminResourceRow, RunArtifactRow, RunEventRow, RunOutboxRow, RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.hermes import PersistentHermesRunAdvisor
from agent_hub.routing.types import EXECUTABLE_MODES, RiskLevel, RouteDecision
from agent_hub.runs.repository import RunConflict, RunNotFound, RunRepository
from agent_hub.runs.service import (
    HermesRunAdvice,
    HermesRunOutcome,
    RunService,
    TemporaryAgentProposal,
)
from agent_hub.runs.tasks import CeleryRunQueue
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.registry import RuntimeRegistry


class FakeRuntime:
    mode = TaskMode.DISPATCH

    def __init__(self) -> None:
        self.calls = 0
        self.restored: list[RuntimeCheckpoint] = []
        self.artifact_id = uuid4()

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.calls += 1
        if context.checkpoint is not None:
            yield RunEvent(
                kind=EventKind.RUNTIME_COMPLETED,
                sequence=3,
                run_id=context.run_id,
            )
            return
        yield RunEvent(
            kind=EventKind.STEP_COMPLETED,
            sequence=1,
            run_id=context.run_id,
            step_id="research",
            actor="researcher",
            payload={"completed": True},
        )
        artifact = Artifact(
            id=self.artifact_id,
            type="text",
            producer="researcher",
            content={"text": "research complete"},
        )
        checkpoint = RuntimeCheckpoint(
            id=uuid4(),
            runtime_type="fake-dispatch",
            runtime_version="1",
            run_id=context.run_id,
            tenant_id=context.tenant_id,
            mode=TaskMode.DISPATCH,
            state={
                "completed_step_ids": ("research",),
                "artifact_refs": ({"id": str(artifact.id), "sha256": artifact.content_sha256}),
            },
        )
        yield RunEvent(
            kind=EventKind.ARTIFACT_CREATED,
            sequence=2,
            run_id=context.run_id,
            artifact=artifact,
        )
        yield RunEvent(
            kind=EventKind.CHECKPOINT_SAVED,
            sequence=3,
            run_id=context.run_id,
            checkpoint=checkpoint,
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("service persists checkpoint events directly")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        self.restored.append(checkpoint)

    async def cancel(self) -> None:
        raise AssertionError("not used")


class WaitingModeRouter:
    async def route(self, task_text: object) -> RouteDecision:
        del task_text
        return RouteDecision(
            mode=None,
            needs_user_choice=True,
            status="waiting_user_mode",
            assessments=(),
            clarification_reason="classification_unavailable",
            options=EXECUTABLE_MODES,
            decision_token="safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
            version=1,
            risk=RiskLevel.LOW,
            requires_approval=False,
            permissions_still_apply=True,
        )


class UserChoiceRouter:
    async def route(self, task_text: object) -> RouteDecision:
        del task_text
        return RouteDecision(
            mode=None,
            needs_user_choice=True,
            status="waiting_user_mode",
            assessments=(),
            clarification_reason="routing_requires_user_choice",
            options=EXECUTABLE_MODES,
            decision_token="safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
            version=1,
            risk=RiskLevel.LOW,
            requires_approval=True,
            permissions_still_apply=True,
        )


class CountingUserChoiceRouter:
    def __init__(self) -> None:
        self.calls = 0

    async def route(self, task_text: object) -> RouteDecision:
        self.calls += 1
        del task_text
        return RouteDecision(
            mode=None,
            needs_user_choice=True,
            status="waiting_user_mode",
            assessments=(),
            clarification_reason="routing_requires_user_choice",
            options=EXECUTABLE_MODES,
            decision_token="safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
            version=1,
            risk=RiskLevel.LOW,
            requires_approval=True,
            permissions_still_apply=True,
        )

class RecordingTemporaryAgentPolicy:
    def __init__(self, proposal: TemporaryAgentProposal | None) -> None:
        self.proposal = proposal
        self.calls: list[dict[str, object]] = []

    async def propose(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        agent_ids: tuple[str, ...],
        workflow_id: str | None,
        allow_workflow_adjustment: bool,
    ) -> TemporaryAgentProposal | None:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "message": message,
                "mode": mode,
                "agent_ids": agent_ids,
                "workflow_id": workflow_id,
                "allow_workflow_adjustment": allow_workflow_adjustment,
            }
        )
        return self.proposal


class AccountingRuntime:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.APPROVAL_REQUESTED,
            sequence=1,
            run_id=context.run_id,
            actor="operator",
            approval_id="approval_1",
            action="tool_execute",
            reason="requires_user_approval",
        )
        yield RunEvent(
            kind=EventKind.APPROVAL_RESOLVED,
            sequence=2,
            run_id=context.run_id,
            actor="operator",
            approval_id="approval_1",
            decision="approved",
        )
        yield RunEvent(
            kind=EventKind.COST_RECORDED,
            sequence=3,
            run_id=context.run_id,
            actor="researcher",
            provider_id="deepseek",
            cost_usd=Decimal("0.25"),
            currency="USD",
        )
        yield RunEvent(
            kind=EventKind.RUNTIME_COMPLETED,
            sequence=4,
            run_id=context.run_id,
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError("not used")

    async def cancel(self) -> None:
        raise AssertionError("not used")


class ExplodingRuntime:
    mode = TaskMode.DISPATCH

    def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        del context
        return self._run()

    async def _run(self) -> AsyncIterator[RunEvent]:
        raise RuntimeError("provider details must not leak")
        yield  # pragma: no cover

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError("not used")

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RestoreExplodingRuntime(FakeRuntime):
    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint
        raise RuntimeError("restore failed")


@dataclass(slots=True)
class RecordingQueue:
    enqueued: list[UUID]
    idempotency_keys: list[str] | None = None
    fail_once: bool = False

    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("queue unavailable")
        self.enqueued.append(run_id)
        if self.idempotency_keys is not None:
            self.idempotency_keys.append(idempotency_key)


@dataclass(slots=True)
class RecordingHermesAdvisor:
    advice: HermesRunAdvice | None = None
    advise_calls: list[dict[str, object]] | None = None
    outcomes: list[HermesRunOutcome] | None = None

    async def advise(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        agent_ids: tuple[str, ...],
        workflow_id: str | None,
    ) -> HermesRunAdvice | None:
        if self.advise_calls is not None:
            self.advise_calls.append(
                {
                    "tenant_id": tenant_id,
                    "actor_id": actor_id,
                    "message": message,
                    "mode": mode,
                    "agent_ids": agent_ids,
                    "workflow_id": workflow_id,
                }
            )
        return self.advice

    async def record_outcome(self, outcome: HermesRunOutcome) -> None:
        if self.outcomes is not None:
            self.outcomes.append(outcome)


class Router(Protocol):
    async def route(
        self, task_text: object, *, confirmation_subject: object | None = None
    ) -> object: ...


@pytest.fixture
async def run_session_factory(
    database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from agent_hub.db.session import build_database
    from tests.integration.conftest import _clean_database

    database = build_database(database_url)
    try:
        await _clean_database(database.session_factory)
        yield database.session_factory
    finally:
        await _clean_database(database.session_factory)
        await database.dispose()


async def test_worker_resumes_from_latest_safe_checkpoint_without_duplicate_artifact_work(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    queue = RecordingQueue([])
    runtime = FakeRuntime()
    repository = RunRepository(run_session_factory)
    service = RunService(
        repository,
        runtime_registry=RuntimeRegistry((runtime,)),
        router=None,
        task_queue=queue,
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=user_id,
        message="research the topic",
        mode=TaskMode.DISPATCH,
        idempotency_key="tenant-safe-key",
    )

    first = await service.execute(submitted.id, crash_after_event_kind=EventKind.CHECKPOINT_SAVED)
    assert first.status is RunStatus.RUNNING
    duplicate_worker = await service.execute(submitted.id)
    duplicate_events = await service.events(tenant_id, submitted.id)

    recovered = await service.recover(submitted.id)
    run = await service.get(tenant_id, submitted.id)
    events = await service.events(tenant_id, submitted.id)

    assert duplicate_worker.status is RunStatus.RUNNING
    assert sum(event["kind"] == "artifact.created" for event in duplicate_events) == 1
    assert recovered.status is RunStatus.COMPLETED
    assert run.status is RunStatus.COMPLETED
    assert run.completed_step_ids == ("research",)
    assert sum(event["kind"] == "artifact.created" for event in events) == 1
    assert runtime.calls == 2
    assert len(runtime.restored) == 1


async def test_conversation_context_keeps_origin_anchor_when_history_exceeds_window(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    conversation_id = "conv-long-memory-anchor"
    repository = RunRepository(run_session_factory)

    first = await repository.create_run(
        tenant_id=tenant_id,
        actor_id=user_id,
        request="初始目标：完成 Agent Hub，并且所有高风险操作都必须审批。",
        mode=TaskMode.DISPATCH,
        status=RunStatus.COMPLETED,
        idempotency_key=None,
        routing_decision={"conversation_id": conversation_id},
        enqueue=False,
    )
    for index in range(8):
        await repository.create_run(
            tenant_id=tenant_id,
            actor_id=user_id,
            request=f"中间讨论 {index}",
            mode=TaskMode.DISPATCH,
            status=RunStatus.COMPLETED,
            idempotency_key=None,
            routing_decision={"conversation_id": conversation_id},
            enqueue=False,
        )
    latest = await repository.create_run(
        tenant_id=tenant_id,
        actor_id=user_id,
        request="最新结论：上下文压缩属于对话框架，不属于进化模块。",
        mode=TaskMode.DISPATCH,
        status=RunStatus.COMPLETED,
        idempotency_key=None,
        routing_decision={"conversation_id": conversation_id},
        enqueue=False,
    )
    current = await repository.create_run(
        tenant_id=tenant_id,
        actor_id=user_id,
        request="继续当前任务",
        mode=TaskMode.DISPATCH,
        status=RunStatus.QUEUED,
        idempotency_key=None,
        routing_decision={"conversation_id": conversation_id},
        enqueue=False,
    )

    items = await repository.conversation_context(
        tenant_id,
        conversation_id,
        before_run_id=current.id,
    )

    assert len(items) == 6
    assert items[0].run_id == first.id
    assert items[-1].run_id == latest.id
    assert all(item.run_id != current.id for item in items)


async def test_dispatch_waits_for_user_before_creating_temporary_agent(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    queue = RecordingQueue([])
    policy = RecordingTemporaryAgentPolicy(
        TemporaryAgentProposal(
            id="temp-web-engineer",
            name="Temporary Web Engineer",
            role="Web Engineer",
            prompt="Implement the landing page requested by the user.",
            reason="selected workflow has no engineering-capable agent",
            missing_capability="software_engineering",
            recommended_model="deepseek",
            suggested_skills=("frontend",),
            permanentizable=True,
        )
    )
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((FakeRuntime(),)),
        router=None,
        task_queue=queue,
        temporary_agent_policy=policy,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=user_id,
        message="把这个艺术设计方案落成一个网页",
        mode=TaskMode.DISPATCH,
        agent_ids=("director",),
        workflow_id="creative-design-discuss",
        allow_workflow_adjustment=True,
        idempotency_key="needs-temp-agent",
    )

    assert submitted.status is RunStatus.WAITING_APPROVAL
    assert submitted.mode is TaskMode.DISPATCH
    assert submitted.decision_token is not None
    assert submitted.clarification_reason == "temporary_agent_requires_user_approval"
    assert submitted.temporary_agent_proposal == {
        "id": "temp-web-engineer",
        "name": "Temporary Web Engineer",
        "role": "Web Engineer",
        "prompt": "Implement the landing page requested by the user.",
        "reason": "selected workflow has no engineering-capable agent",
        "missing_capability": "software_engineering",
        "recommended_model": "deepseek",
        "suggested_skills": ["frontend"],
        "permanentizable": True,
    }
    assert queue.enqueued == []
    record = await RunRepository(run_session_factory).get(tenant_id, submitted.id)
    assert record.routing_decision is not None
    assert record.routing_decision["approval_kind"] == "temporary_agent_creation"

    approved = await service.approve_temporary_agent(
        tenant_id=tenant_id,
        actor_id=user_id,
        run_id=submitted.id,
        decision_token=submitted.decision_token,
        version=submitted.version,
    )

    assert approved.status is RunStatus.QUEUED
    assert queue.enqueued == []
    approved_record = await RunRepository(run_session_factory).get(tenant_id, submitted.id)
    assert approved_record.routing_decision is not None
    assert approved_record.routing_decision["selected_agent_ids"] == [
        "director",
        "temp-web-engineer",
    ]
    assert approved_record.routing_decision["temporary_agent_approved"] is True
    assert approved_record.routing_decision["temporary_agents"] == [
        {**submitted.temporary_agent_proposal, "model": "deepseek"}
    ]


async def test_hermes_high_confidence_auto_advice_queues_recommended_mode(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    queue = RecordingQueue([])
    advisor = RecordingHermesAdvisor(
        advice=HermesRunAdvice(
            recommended_mode=TaskMode.DISPATCH,
            confidence=0.86,
            reasons=("matched previous short-video workflow",),
            recommended_skills=("script-review",),
            requires_approval=False,
        ),
        advise_calls=[],
    )
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((FakeRuntime(),)),
        router=None,
        task_queue=queue,
        hermes_advisor=advisor,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=user_id,
        message="short video script",
        mode=TaskMode.AUTO,
        agent_ids=("director",),
        workflow_id="short-video-dispatch",
    )
    record = await RunRepository(run_session_factory).get(tenant_id, submitted.id)

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DISPATCH
    assert queue.enqueued == []
    assert advisor.advise_calls == [
        {
            "tenant_id": tenant_id,
            "actor_id": user_id,
            "message": "short video script",
            "mode": TaskMode.AUTO,
            "agent_ids": ("director",),
            "workflow_id": "short-video-dispatch",
        }
    ]
    assert record.routing_decision is not None
    assert record.routing_decision["reason"] == "hermes_recommendation"
    hermes_payload = cast(dict[str, object], record.routing_decision["hermes"])
    assert hermes_payload["confidence"] == 0.86


async def test_completed_run_records_bounded_hermes_outcome(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    outcomes: list[HermesRunOutcome] = []
    advisor = RecordingHermesAdvisor(outcomes=outcomes)
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((FakeRuntime(),)),
        router=None,
        task_queue=RecordingQueue([]),
        hermes_advisor=advisor,
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=user_id,
        message="secret-free request must not be copied into Hermes",
        mode=TaskMode.DISPATCH,
        agent_ids=("director", "copywriter"),
        workflow_id="short-video-dispatch",
    )

    completed = await service.execute(submitted.id)

    assert completed.status is RunStatus.COMPLETED
    assert outcomes == [
        HermesRunOutcome(
            tenant_id=tenant_id,
            actor_id=user_id,
            run_id=submitted.id,
            status=RunStatus.COMPLETED,
            mode=TaskMode.DISPATCH,
            workflow_id="short-video-dispatch",
            conversation_id=submitted.conversation_id,
            agent_ids=("director", "copywriter"),
        )
    ]


async def test_persistent_hermes_runtime_outcome_is_scheduler_observation(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    run_id = uuid4()
    advisor = PersistentHermesRunAdvisor(run_session_factory)

    await advisor.record_outcome(
        HermesRunOutcome(
            tenant_id=tenant_id,
            actor_id=actor_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            mode=TaskMode.HYBRID,
            workflow_id="quality-review",
            conversation_id="conv-scheduler-observe",
            agent_ids=("planner", "reviewer"),
        )
    )

    async with run_session_factory() as session:
        row = (
            await session.execute(
                select(AdminResourceRow)
                .where(AdminResourceRow.tenant_id == tenant_id)
                .where(AdminResourceRow.kind == "hermes")
            )
        ).scalar_one()

    payload = dict(row.payload)
    assert payload["category"] == "scheduler"
    assert payload["conversation_id"] == "conv-scheduler-observe"
    assert payload["run_id"] == str(run_id)

async def test_persistent_hermes_runtime_advice_uses_only_confirmed_lessons(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    advisor = PersistentHermesRunAdvisor(run_session_factory)
    lesson_payload = {
        "id": "hermes_unconfirmed_dispatch",
        "outcome": "success",
        "lesson": "Use dispatch for smoke approval planning tasks.",
        "summary": "Learned success pattern: dispatch for planning.",
        "tags": ["planning", "dispatch"],
        "weight": 10,
        "created_at": datetime.now(UTC).isoformat(),
        "run_id": None,
        "conversation_id": "conv-hermes-runtime",
        "confirmed_at": None,
    }
    async with run_session_factory() as session, session.begin():
        session.add(
            AdminResourceRow(
                tenant_id=tenant_id,
                kind="hermes",
                resource_id="hermes_unconfirmed_dispatch",
                payload=lesson_payload,
            )
        )
        session.add(
            AdminResourceRow(
                tenant_id=tenant_id,
                kind="main_agent",
                resource_id="default",
                payload={"hermes_policy": "suggest"},
            )
        )

    unconfirmed = await advisor.advise(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="planning task should not use unconfirmed lesson",
        mode=TaskMode.AUTO,
        agent_ids=(),
        workflow_id=None,
    )
    assert unconfirmed is None

    confirmed_at = datetime.now(UTC).isoformat()
    async with run_session_factory() as session, session.begin():
        row = (
            await session.execute(
                select(AdminResourceRow)
                .where(AdminResourceRow.tenant_id == tenant_id)
                .where(AdminResourceRow.kind == "hermes")
                .where(AdminResourceRow.resource_id == "hermes_unconfirmed_dispatch")
            )
        ).scalar_one()
        row.payload = {**lesson_payload, "confirmed_at": confirmed_at}

    confirmed = await advisor.advise(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="planning task should use confirmed lesson",
        mode=TaskMode.AUTO,
        agent_ids=(),
        workflow_id=None,
    )

    assert confirmed is not None
    assert confirmed.recommended_mode is TaskMode.DISPATCH


async def test_persistent_hermes_runtime_advice_respects_main_agent_policy(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    advisor = PersistentHermesRunAdvisor(run_session_factory)
    confirmed_lesson = {
        "id": "hermes_confirmed_policy",
        "outcome": "success",
        "lesson": "Use dispatch for planning tasks.",
        "summary": "Learned success pattern: dispatch for planning.",
        "tags": ["planning", "dispatch"],
        "weight": 10,
        "created_at": datetime.now(UTC).isoformat(),
        "run_id": None,
        "conversation_id": "conv-hermes-policy",
        "confirmed_at": datetime.now(UTC).isoformat(),
    }
    async with run_session_factory() as session, session.begin():
        session.add(
            AdminResourceRow(
                tenant_id=tenant_id,
                kind="hermes",
                resource_id="hermes_confirmed_policy",
                payload=confirmed_lesson,
            )
        )
        session.add(
            AdminResourceRow(
                tenant_id=tenant_id,
                kind="main_agent",
                resource_id="default",
                payload={"hermes_policy": "observe"},
            )
        )

    observe = await advisor.advise(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="planning task",
        mode=TaskMode.AUTO,
        agent_ids=(),
        workflow_id=None,
    )
    assert observe is None

    async with run_session_factory() as session, session.begin():
        row = (
            await session.execute(
                select(AdminResourceRow)
                .where(AdminResourceRow.tenant_id == tenant_id)
                .where(AdminResourceRow.kind == "main_agent")
                .where(AdminResourceRow.resource_id == "default")
            )
        ).scalar_one()
        row.payload = {"hermes_policy": "suggest"}

    suggest = await advisor.advise(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="planning task",
        mode=TaskMode.AUTO,
        agent_ids=(),
        workflow_id=None,
    )

    assert suggest is not None
    assert suggest.recommended_mode is TaskMode.DISPATCH
    assert suggest.requires_approval is True


async def test_recovery_fails_safe_when_side_effect_event_has_no_checkpoint(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    runtime = FakeRuntime()
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((runtime,)),
        router=None,
        task_queue=RecordingQueue([]),
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="research the topic",
        mode=TaskMode.DISPATCH,
        idempotency_key="tenant-safe-key-no-checkpoint",
    )

    first = await service.execute(submitted.id, crash_after_event_kind=EventKind.ARTIFACT_CREATED)
    recovered = await service.recover(submitted.id)
    events = await service.events(tenant_id, submitted.id)

    assert first.status is RunStatus.RUNNING
    assert recovered.status is RunStatus.FAILED
    assert runtime.calls == 1
    assert sum(event["kind"] == "artifact.created" for event in events) == 1


async def test_submission_writes_run_and_outbox_atomically_then_publisher_delivers_once(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    queue = RecordingQueue([], [])
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((FakeRuntime(),)),
        router=WaitingModeRouter(),
        task_queue=queue,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="run fixed workflow",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-1",
    )
    duplicate = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="run fixed workflow",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-1",
    )

    assert duplicate.id == submitted.id
    assert queue.enqueued == []
    assert await service.publish_pending() == 1
    assert await service.publish_pending() == 0
    assert queue.enqueued == [submitted.id]
    assert queue.idempotency_keys == [f"{tenant_id}:client-request-1"]


async def test_choose_mode_persists_choice_and_enqueues_waiting_run(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    queue = RecordingQueue([])
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((FakeRuntime(),)),
        router=WaitingModeRouter(),
        task_queue=queue,
    )

    waiting = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="ambiguous workflow",
        mode=TaskMode.AUTO,
        idempotency_key="client-request-2",
    )
    assert waiting.status is RunStatus.WAITING_USER_MODE
    assert waiting.decision_token is not None

    chosen = await service.choose_mode(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        run_id=waiting.id,
        mode=TaskMode.DISPATCH,
        decision_token=waiting.decision_token,
        version=waiting.version,
    )

    assert chosen.status is RunStatus.QUEUED
    assert chosen.mode is TaskMode.DISPATCH
    assert queue.enqueued == []
    assert await service.publish_pending() == 1
    assert await service.publish_pending() == 0
    assert queue.enqueued == [waiting.id]


async def test_auto_submission_reuses_recent_conversation_mode_for_continuation(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    conversation_id = "conv-channel-continuation"
    queue = RecordingQueue([])
    router = CountingUserChoiceRouter()
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((FakeRuntime(),)),
        router=router,
        task_queue=queue,
    )

    selected = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="用户已选择混合模式的起始任务",
        mode=TaskMode.HYBRID,
        conversation_id=conversation_id,
        idempotency_key="client-request-conversation-mode-1",
    )
    assert selected.status is RunStatus.QUEUED

    continued = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="继续把刚刚这个方案展开",
        mode=TaskMode.AUTO,
        conversation_id=conversation_id,
        idempotency_key="client-request-conversation-mode-2",
    )

    assert continued.status is RunStatus.QUEUED
    assert continued.mode is TaskMode.HYBRID
    assert continued.clarification_reason is None
    assert router.calls == 0


async def test_outbox_is_not_marked_delivered_when_queue_enqueue_fails(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    queue = RecordingQueue([], fail_once=True)
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((FakeRuntime(),)),
        router=None,
        task_queue=queue,
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="run fixed workflow",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-3",
    )

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await service.publish_pending()

    assert queue.enqueued == []
    assert await service.publish_pending() == 1
    assert await service.publish_pending() == 0
    assert queue.enqueued == [submitted.id]


async def test_resume_paused_run_enqueues_recovery_work(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    queue = RecordingQueue([])
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((FakeRuntime(),)),
        router=None,
        task_queue=queue,
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="run fixed workflow",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-4",
    )
    await service.pause(tenant_id, submitted.id)

    resumed = await service.resume(tenant_id, submitted.id)

    assert resumed.status is RunStatus.QUEUED
    assert await service.publish_pending() == 2
    assert await service.publish_pending() == 0
    assert queue.enqueued == [submitted.id, submitted.id]


async def test_worker_persists_approval_and_usage_events(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((AccountingRuntime(),)),
        router=None,
        task_queue=RecordingQueue([]),
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="run accounting workflow",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-5",
    )

    completed = await service.execute(submitted.id)
    summary = await service.get(tenant_id, submitted.id)

    assert completed.status is RunStatus.COMPLETED
    assert summary.usage_cost_usd == Decimal("0.25")


async def test_runtime_exception_is_persisted_as_terminal_failure(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((ExplodingRuntime(),)),
        router=None,
        task_queue=RecordingQueue([]),
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="explode safely",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-8",
    )

    failed = await service.execute(submitted.id)
    duplicate_retry = await service.execute(submitted.id)
    events = await service.events(tenant_id, submitted.id)

    assert failed.status is RunStatus.FAILED
    assert duplicate_retry.status is RunStatus.FAILED
    assert [event["kind"] for event in events] == ["runtime.failed"]


async def test_restore_exception_is_persisted_as_terminal_failure(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    runtime = RestoreExplodingRuntime()
    service = RunService(
        RunRepository(run_session_factory),
        runtime_registry=RuntimeRegistry((runtime,)),
        router=None,
        task_queue=RecordingQueue([]),
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="restore safely",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-9",
    )
    await service.execute(submitted.id, crash_after_event_kind=EventKind.CHECKPOINT_SAVED)

    failed = await service.recover(submitted.id)
    duplicate_retry = await service.execute(submitted.id)
    events = await service.events(tenant_id, submitted.id)

    assert failed.status is RunStatus.FAILED
    assert duplicate_retry.status is RunStatus.FAILED
    assert events[-1]["kind"] == "runtime.failed"


async def test_celery_run_queue_uses_outbox_key_as_task_id() -> None:
    class CeleryApp:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        def send_task(self, name: str, **kwargs: object) -> None:
            self.sent.append({"name": name, **kwargs})

    app = CeleryApp()
    queue = CeleryRunQueue(app)
    run_id = uuid4()

    await queue.enqueue_run(run_id, idempotency_key="tenant:request")

    assert app.sent == [
        {
            "name": "agent_hub.runs.execute",
            "args": [str(run_id)],
            "task_id": "tenant:request",
        }
    ]


async def test_usage_events_are_idempotent_by_run_sequence(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    repository = RunRepository(run_session_factory)
    service = RunService(
        repository,
        runtime_registry=RuntimeRegistry((AccountingRuntime(),)),
        router=None,
        task_queue=RecordingQueue([]),
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="run accounting workflow",
        mode=TaskMode.DISPATCH,
        idempotency_key="client-request-6",
    )
    duplicate_cost = RunEvent(
        kind=EventKind.COST_RECORDED,
        sequence=1,
        run_id=submitted.id,
        actor="researcher",
        provider_id="deepseek",
        cost_usd=Decimal("0.25"),
        currency="USD",
    )

    async with await repository.run_transaction() as session, session.begin():
        await repository.persist_event(
            session,
            tenant_id=tenant_id,
            run_id=submitted.id,
            event=duplicate_cost,
        )
        await repository.persist_event(
            session,
            tenant_id=tenant_id,
            run_id=submitted.id,
            event=duplicate_cost,
        )

    summary = await service.get(tenant_id, submitted.id)
    assert summary.usage_cost_usd == Decimal("0.25")


async def test_duplicate_artifact_content_for_same_run_is_idempotent(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    repository = RunRepository(run_session_factory)
    submitted = await repository.create_run(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        request="persist duplicate artifacts",
        mode=TaskMode.DISPATCH,
        status=RunStatus.RUNNING,
        idempotency_key="client-request-duplicate-artifact",
        enqueue=False,
    )
    first = Artifact(
        id=uuid4(),
        type="text",
        producer="planner",
        content={"text": "same content"},
    )
    duplicate_content = Artifact(
        id=uuid4(),
        type="text",
        producer="planner",
        content={"text": "same content"},
    )

    async with await repository.run_transaction() as session, session.begin():
        await repository.persist_event(
            session,
            tenant_id=tenant_id,
            run_id=submitted.id,
            event=RunEvent(
                kind=EventKind.ARTIFACT_CREATED,
                sequence=1,
                run_id=submitted.id,
                artifact=first,
            ),
        )
        await repository.persist_event(
            session,
            tenant_id=tenant_id,
            run_id=submitted.id,
            event=RunEvent(
                kind=EventKind.ARTIFACT_CREATED,
                sequence=2,
                run_id=submitted.id,
                artifact=duplicate_content,
            ),
        )

    artifacts = await repository.artifacts(tenant_id, submitted.id)
    assert len(artifacts) == 1


async def test_public_events_sanitize_sensitive_persisted_payload_keys(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    repository = RunRepository(run_session_factory)
    submitted = await repository.create_run(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        request="inspect event",
        mode=TaskMode.DISPATCH,
        status=RunStatus.RUNNING,
        idempotency_key="client-request-7",
        enqueue=False,
    )
    async with run_session_factory() as session, session.begin():
        session.add(
            RunEventRow(
                id=uuid4(),
                tenant_id=tenant_id,
                run_id=submitted.id,
                sequence=1,
                kind="custom.progress",
                payload={
                    "kind": "custom.progress",
                    "sequence": 1,
                    "run_id": str(submitted.id),
                    "payload": {
                        "safe": True,
                        "api_key": "sk-should-not-leak",
                        "nested": {"hidden_reasoning": "private", "visible": "ok"},
                    },
                },
            )
        )

    events = await repository.events(tenant_id, submitted.id)

    serialized = str(events).lower()
    assert "safe" in serialized
    assert "visible" in serialized
    assert "api_key" not in serialized
    assert "hidden_reasoning" not in serialized
    assert "sk-should-not-leak" not in serialized


async def test_delete_run_removes_terminal_run_and_persisted_children(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    repository = RunRepository(run_session_factory)
    submitted = await repository.create_run(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        request="delete finished conversation",
        mode=TaskMode.DIRECT,
        status=RunStatus.CANCELLED,
        idempotency_key="client-request-delete",
        enqueue=True,
    )
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="main",
        content={"text": "safe terminal response"},
    )
    async with run_session_factory() as session, session.begin():
        await repository.persist_event(
            session,
            tenant_id=tenant_id,
            run_id=submitted.id,
            event=RunEvent(
                kind=EventKind.ARTIFACT_CREATED,
                sequence=1,
                run_id=submitted.id,
                artifact=artifact,
            ),
        )

    await repository.delete_run(tenant_id, submitted.id)

    async with run_session_factory() as session:
        for table in (RunRow, RunOutboxRow, RunEventRow, RunArtifactRow):
            count = await session.scalar(
                select(func.count()).select_from(table).where(table.tenant_id == tenant_id)
            )
            assert count == 0
    with pytest.raises(RunNotFound):
        await repository.get(tenant_id, submitted.id)


async def test_delete_run_rejects_active_run(
    run_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    repository = RunRepository(run_session_factory)
    submitted = await repository.create_run(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        request="active run",
        mode=TaskMode.DIRECT,
        status=RunStatus.RUNNING,
        idempotency_key="client-request-active-delete",
        enqueue=False,
    )

    with pytest.raises(RunConflict):
        await repository.delete_run(tenant_id, submitted.id)
