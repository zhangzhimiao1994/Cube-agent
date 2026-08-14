from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.routing.types import (
    EXECUTABLE_MODES,
    RiskLevel,
    RouteAssessment,
    RouteDecision,
    RouteSource,
)
from agent_hub.runs.repository import (
    RunConflict,
    RunNotFound,
    RunRecord,
    _safe_temporary_agent_model,
)
from agent_hub.runs.service import RunService, TemporaryAgentProposal
from agent_hub.runtime.contracts import RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry


class RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[UUID, str]] = []

    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        self.enqueued.append((run_id, idempotency_key))


class UnusedRuntime:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext):  # type: ignore[no-untyped-def]
        del context
        if False:
            yield RunEvent  # pragma: no cover

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        return None


class FakeRepository:
    def __init__(self) -> None:
        self.records: dict[UUID, RunRecord] = {}
        self.outbox: list[tuple[UUID, str]] = []

    async def create_run(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        request: str,
        mode: TaskMode | None,
        status: RunStatus,
        idempotency_key: str | None,
        routing_decision: dict[str, object] | None = None,
        enqueue: bool,
    ) -> RunRecord:
        del idempotency_key
        run_id = uuid4()
        record = RunRecord(
            id=run_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            request=request,
            mode=mode,
            status=status,
            version=1,
            created_at=datetime.now(UTC),
            routing_decision=routing_decision,
        )
        self.records[run_id] = record
        if enqueue:
            self.outbox.append((run_id, f"{tenant_id}:{run_id}"))
        return record

    async def approve_temporary_agent_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        decision_token: str,
        version: int,
    ) -> RunRecord:
        record = self.records.get(run_id)
        if record is None or record.tenant_id != tenant_id:
            raise RunNotFound("run was not found")
        if record.status is not RunStatus.WAITING_APPROVAL:
            raise RunConflict("run is not waiting for temporary agent approval")
        decision = dict(record.routing_decision or {})
        if decision.get("decision_token") != decision_token:
            raise RunConflict("temporary agent approval token is invalid")
        if record.version != version:
            raise RunConflict("run version is stale")
        proposal = decision["temporary_agent_proposal"]
        assert isinstance(proposal, dict)
        proposal = {**proposal, "model": _safe_temporary_agent_model(proposal)}
        raw_selected = decision.get("selected_agent_ids", [])
        selected = list(raw_selected) if isinstance(raw_selected, list) else []
        selected.append(str(proposal["id"]))
        updated = RunRecord(
            id=record.id,
            tenant_id=record.tenant_id,
            actor_id=record.actor_id,
            request=record.request,
            mode=record.mode,
            status=RunStatus.QUEUED,
            version=record.version + 1,
            created_at=record.created_at,
            routing_decision={
                **decision,
                "selected_agent_ids": selected,
                "temporary_agents": [proposal],
                "temporary_agent_approved": True,
            },
        )
        self.records[run_id] = updated
        self.outbox.append((run_id, f"{tenant_id}:{run_id}:temporary-agent:{version}"))
        return updated

    async def revise_temporary_agent_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        decision_token: str,
        version: int,
        feedback: str,
    ) -> RunRecord:
        record = self.records.get(run_id)
        if record is None or record.tenant_id != tenant_id:
            raise RunNotFound("run was not found")
        if record.status is not RunStatus.WAITING_APPROVAL:
            raise RunConflict("run is not waiting for temporary agent approval")
        decision = dict(record.routing_decision or {})
        if decision.get("decision_token") != decision_token:
            raise RunConflict("temporary agent revision token is invalid")
        if record.version != version:
            raise RunConflict("run version is stale")
        updated = RunRecord(
            id=record.id,
            tenant_id=record.tenant_id,
            actor_id=record.actor_id,
            request=f"{record.request}\n\nUser feedback for temporary agent proposal: {feedback}",
            mode=record.mode,
            status=RunStatus.QUEUED,
            version=record.version + 1,
            created_at=record.created_at,
            routing_decision={
                **decision,
                "temporary_agent_rejected": True,
                "temporary_agent_feedback": feedback,
                "temporary_agents": [],
                "workflow_adjustment_policy": "ask_before_apply",
            },
        )
        self.records[run_id] = updated
        self.outbox.append((run_id, f"{tenant_id}:{run_id}:temporary-agent-revision:{version}"))
        return updated


class FakeTemporaryAgentPolicy:
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
        del tenant_id, actor_id, message, mode, agent_ids, workflow_id
        if not allow_workflow_adjustment:
            return None
        return TemporaryAgentProposal(
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


class WaitingRouter:
    def __init__(self, decision: RouteDecision) -> None:
        self.decision = decision

    async def route(self, task_text: object) -> RouteDecision:
        del task_text
        return self.decision


class FailingRouter:
    async def route(self, task_text: object) -> RouteDecision:
        del task_text
        raise TimeoutError


def assessment(
    mode: TaskMode,
    *,
    confidence: float,
    risk: RiskLevel = RiskLevel.LOW,
    cost: Decimal = Decimal("0.01"),
) -> RouteAssessment:
    return RouteAssessment(
        mode=mode,
        confidence=confidence,
        reason=f"{mode.value}_assessment",
        roles=(f"{mode.value}_agent",),
        estimated_seconds=30,
        estimated_cost_usd=cost,
        risk=risk,
        source=RouteSource.CLASSIFIER,
        logical_model="test_model",
        deployment_id="test_deployment",
        provider_id="test_provider",
    )


def waiting_decision(
    *assessments: RouteAssessment,
    risk: RiskLevel = RiskLevel.LOW,
    requires_approval: bool = False,
) -> RouteDecision:
    return RouteDecision(
        mode=None,
        needs_user_choice=True,
        status="waiting_user_mode",
        assessments=assessments,
        clarification_reason="routing_requires_user_choice",
        options=EXECUTABLE_MODES,
        decision_token="safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
        version=1,
        risk=risk,
        requires_approval=requires_approval,
        permissions_still_apply=True,
    )


def waiting_unavailable_decision(reason: str = "classification_unavailable") -> RouteDecision:
    return RouteDecision(
        mode=None,
        needs_user_choice=True,
        status="waiting_user_mode",
        assessments=(),
        clarification_reason=reason,
        options=EXECUTABLE_MODES,
        decision_token="safe-unavailable-token-abcdefghijklmnopqrstuvwxyz12",
        version=1,
        risk=RiskLevel.LOW,
        requires_approval=False,
        permissions_still_apply=True,
    )


def ready_decision(mode: TaskMode) -> RouteDecision:
    return RouteDecision(
        mode=mode,
        needs_user_choice=False,
        status="ready",
        assessments=(
            assessment(mode, confidence=0.95),
        ),
        clarification_reason=None,
        options=(),
        decision_token=None,
        version=1,
        risk=RiskLevel.LOW,
        requires_approval=False,
        permissions_still_apply=True,
    )


@pytest.mark.asyncio
async def test_auto_ready_direct_is_promoted_for_generation_work() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    repository = FakeRepository()
    queue = RecordingQueue()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
        router=WaitingRouter(ready_decision(TaskMode.DIRECT)),
        task_queue=queue,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="写一个国庆活动方案",
        mode=TaskMode.AUTO,
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DISPATCH
    routing = repository.records[submitted.id].routing_decision
    assert routing is not None
    assert routing["router_selected_mode"] == "direct"
    assert routing["main_agent_selected_mode"] == "dispatch"
    assert routing["main_agent_adjusted"] is True


@pytest.mark.asyncio
async def test_submit_persists_vibe_coding_capability_metadata() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    repository = FakeRepository()
    queue = RecordingQueue()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
        router=None,
        task_queue=queue,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="review this code archive",
        mode=TaskMode.HYBRID,
        conversation_id="conv-vibe-coding",
        attachment_ids=("att_11111111111111111111111111111111",),
        vibe_coding=True,
    )

    routing = repository.records[submitted.id].routing_decision

    assert routing is not None
    assert routing["vibe_coding"] is True
    assert routing["capability"] == "vibe_coding"
    assert routing["conversation_id"] == "conv-vibe-coding"
    assert routing["attachment_ids"] == ["att_11111111111111111111111111111111"]


@pytest.mark.asyncio
async def test_schedule_intent_returns_confirmation_proposal_without_enqueue() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    repository = FakeRepository()
    queue = RecordingQueue()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
        router=None,
        task_queue=queue,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="每天9点提醒我填写日报",
        mode=TaskMode.AUTO,
    )

    assert submitted.status is RunStatus.WAITING_APPROVAL
    assert submitted.mode is TaskMode.DISPATCH
    assert submitted.clarification_reason == "schedule_requires_user_confirmation"
    assert submitted.decision_token is not None
    assert submitted.schedule_proposal is not None
    assert submitted.schedule_proposal["kind"] == "cron"
    assert submitted.schedule_proposal["cron"] == "0 9 * * *"
    assert submitted.schedule_proposal["timezone"] == "Asia/Shanghai"
    assert submitted.schedule_proposal["workflow_id"] == "scheduled_task"
    assert repository.outbox == []
    assert queue.enqueued == []
    routing = repository.records[submitted.id].routing_decision
    assert routing is not None
    assert routing["approval_kind"] == "schedule_creation"
    assert routing["schedule_proposal"] == submitted.schedule_proposal

@pytest.mark.asyncio
async def test_evolution_intent_returns_confirmation_proposal_without_enqueue() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    repository = FakeRepository()
    queue = RecordingQueue()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
        router=None,
        task_queue=queue,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="帮我进化 darwin-skill，做多轮迭代并用基准 agent 判断是否继续",
        mode=TaskMode.AUTO,
        conversation_id="conv-evolution-chat",
    )

    assert submitted.status is RunStatus.WAITING_APPROVAL
    assert submitted.mode is TaskMode.HYBRID
    assert submitted.clarification_reason == "evolution_requires_user_confirmation"
    assert submitted.decision_token is not None
    assert submitted.evolution_proposal is not None
    assert submitted.evolution_proposal["kind"] == "skill_optimization"
    assert submitted.evolution_proposal["title"] == "Skill 进化任务"
    assert submitted.evolution_proposal["source_skill_ids"] == ["darwin-skill"]
    assert submitted.evolution_proposal["baseline_agent_id"] == "main-agent"
    assert submitted.evolution_proposal["evaluator_agent_id"] == "evaluator-agent"
    assert submitted.evolution_proposal["approval_policy"] == "ask"
    assert submitted.evolution_proposal["memory_policy"] == "summarize_between_rounds"
    assert submitted.evolution_proposal["source_conversation_id"] == "conv-evolution-chat"
    assert repository.outbox == []
    assert queue.enqueued == []
    routing = repository.records[submitted.id].routing_decision
    assert routing is not None
    assert routing["approval_kind"] == "evolution_creation"
    assert routing["evolution_proposal"] == submitted.evolution_proposal


@pytest.mark.asyncio
async def test_normal_research_or_plan_request_does_not_become_evolution_task() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    repository = FakeRepository()
    queue = RecordingQueue()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
        router=FailingRouter(),
        task_queue=queue,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="这个 AI 科研项目需要长期迭代，先帮我给出研究方案和资料检索计划",
        mode=TaskMode.AUTO,
        conversation_id="conv-research-plan",
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.evolution_proposal is None
    routing = repository.records[submitted.id].routing_decision
    assert routing is not None
    assert "evolution_proposal" not in routing


@pytest.mark.asyncio
async def test_explicit_skill_creation_request_returns_grounded_evolution_proposal() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    repository = FakeRepository()
    queue = RecordingQueue()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
        router=None,
        task_queue=queue,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="我想进行 AI 方面的科研，给我生成一个相关的 skill，并用真实资料和测试任务验收",
        mode=TaskMode.AUTO,
        conversation_id="conv-research-skill",
    )

    assert submitted.status is RunStatus.WAITING_APPROVAL
    assert submitted.evolution_proposal is not None
    assert submitted.evolution_proposal["kind"] == "skill_distillation"
    assert submitted.evolution_proposal["target_artifact_type"] == "skill"
    assert submitted.evolution_proposal["title"] == "Skill 创建任务"
    assert submitted.evolution_proposal["source_conversation_id"] == "conv-research-skill"
    summary = submitted.evolution_proposal["summary"]
    assert isinstance(summary, str)
    assert "真实任务验收" in summary


@pytest.mark.asyncio
async def test_auto_router_timeout_uses_local_main_agent_for_generation_work() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    repository = FakeRepository()
    queue = RecordingQueue()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
        router=FailingRouter(),
        task_queue=queue,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="请生成一个中秋活动方案",
        mode=TaskMode.AUTO,
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DISPATCH
    routing = repository.records[submitted.id].routing_decision
    assert routing is not None
    assert routing["reason"] == "main_agent_local_resolution"
    assert routing["main_agent_selected_mode"] == "dispatch"


@pytest.mark.asyncio
async def test_auto_router_classification_unavailable_uses_local_main_agent() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    repository = FakeRepository()
    queue = RecordingQueue()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
        router=WaitingRouter(waiting_unavailable_decision()),
        task_queue=queue,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="给我写一个新年活动策划方案",
        mode=TaskMode.AUTO,
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DISPATCH
    assert repository.outbox == [(submitted.id, f"{tenant_id}:{submitted.id}")]
    routing = repository.records[submitted.id].routing_decision
    assert routing is not None
    assert routing["reason"] == "main_agent_local_resolution"
    assert routing["main_agent_selected_mode"] == "dispatch"
    assert routing["router_clarification_reason"] == "classification_unavailable"


@pytest.mark.asyncio
async def test_auto_mode_resolves_low_risk_router_uncertainty_through_main_agent() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    repository = FakeRepository()
    queue = RecordingQueue()
    decision = waiting_decision(
        assessment(TaskMode.DISPATCH, confidence=0.62),
        assessment(TaskMode.DISCUSS, confidence=0.91),
    )
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
        router=WaitingRouter(decision),
        task_queue=queue,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="分析一下方案，然后给出建议",
        mode=TaskMode.AUTO,
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DISCUSS
    assert submitted.decision_token is None
    assert repository.outbox == [(submitted.id, f"{tenant_id}:{submitted.id}")]
    routing = repository.records[submitted.id].routing_decision
    assert routing is not None
    assert routing["reason"] == "main_agent_auto_resolved"
    assert routing["auto_resolution_reason"] == "routing_requires_user_choice"
    assert routing["auto_resolution_source_modes"] == ["dispatch", "discuss"]


@pytest.mark.asyncio
async def test_auto_mode_waits_for_user_choice_when_router_is_unavailable() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    repository = FakeRepository()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
        router=None,
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="用一句中文回答：烟测 auto 快速模式。",
        mode=TaskMode.AUTO,
    )

    assert submitted.status is RunStatus.WAITING_USER_MODE
    assert submitted.mode is None
    assert submitted.decision_token is not None
    assert repository.outbox == []
    routing = repository.records[submitted.id].routing_decision
    assert routing is not None
    assert routing["reason"] == "router_unavailable"
    assert routing["decision_token"] == submitted.decision_token


@pytest.mark.asyncio
async def test_auto_mode_still_requires_user_choice_for_high_risk_router_decision() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    repository = FakeRepository()
    decision = waiting_decision(
        assessment(TaskMode.DISPATCH, confidence=0.92, risk=RiskLevel.HIGH),
        risk=RiskLevel.HIGH,
        requires_approval=True,
    )
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
        router=WaitingRouter(decision),
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="执行高风险操作",
        mode=TaskMode.AUTO,
    )

    assert submitted.status is RunStatus.WAITING_USER_MODE
    assert submitted.mode is None
    assert submitted.decision_token is not None
    assert repository.outbox == []


@pytest.mark.asyncio
async def test_dispatch_requires_user_approval_before_temporary_agent_is_queued() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    repository = FakeRepository()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
        router=None,
        task_queue=RecordingQueue(),
        temporary_agent_policy=FakeTemporaryAgentPolicy(),
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="把艺术设计工作流临时扩展成网页落地任务",
        mode=TaskMode.DISPATCH,
        agent_ids=("director",),
        workflow_id="creative-design-discuss",
        allow_workflow_adjustment=True,
    )

    assert submitted.status is RunStatus.WAITING_APPROVAL
    assert submitted.clarification_reason == "temporary_agent_requires_user_approval"
    assert submitted.decision_token is not None
    assert submitted.temporary_agent_proposal is not None
    assert submitted.temporary_agent_proposal["id"] == "temp-web-engineer"
    assert submitted.temporary_agent_proposal["recommended_model"] == "deepseek"
    assert repository.outbox == []

    approved = await service.approve_temporary_agent(
        tenant_id=tenant_id,
        actor_id=actor_id,
        run_id=submitted.id,
        decision_token=submitted.decision_token,
        version=submitted.version,
    )

    assert approved.status is RunStatus.QUEUED
    assert repository.outbox == [
        (submitted.id, f"{tenant_id}:{submitted.id}:temporary-agent:1")
    ]
    decision = repository.records[submitted.id].routing_decision
    assert decision is not None
    assert decision["temporary_agent_approved"] is True
    assert decision["selected_agent_ids"] == ["director", "temp-web-engineer"]
    assert decision["temporary_agents"] == [
        {**submitted.temporary_agent_proposal, "model": "deepseek"}
    ]


def test_temporary_agent_model_never_falls_back_to_agent_id() -> None:
    with pytest.raises(RunConflict, match="no safe model"):
        _safe_temporary_agent_model({"id": "temp-web-engineer"})


@pytest.mark.asyncio
async def test_user_can_reject_temporary_agent_with_feedback_and_continue() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    repository = FakeRepository()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnusedRuntime(),)),
        router=None,
        task_queue=RecordingQueue(),
        temporary_agent_policy=FakeTemporaryAgentPolicy(),
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message="把艺术设计工作流临时扩展成网页落地任务",
        mode=TaskMode.DISPATCH,
        agent_ids=("director",),
        workflow_id="creative-design-discuss",
        allow_workflow_adjustment=True,
    )

    revised = await service.revise_temporary_agent(
        tenant_id=tenant_id,
        actor_id=actor_id,
        run_id=submitted.id,
        decision_token=submitted.decision_token or "",
        version=submitted.version,
        feedback="不要加工程师，先让产品经理重新拆需求。",
    )

    assert revised.status is RunStatus.QUEUED
    record = repository.records[submitted.id]
    assert "不要加工程师" in record.request
    assert record.routing_decision is not None
    assert record.routing_decision["temporary_agent_rejected"] is True
    assert record.routing_decision["temporary_agent_feedback"] == "不要加工程师，先让产品经理重新拆需求。"
    assert repository.outbox == [
        (submitted.id, f"{tenant_id}:{submitted.id}:temporary-agent-revision:1")
    ]
