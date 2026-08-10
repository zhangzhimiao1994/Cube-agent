from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunNotFound, RunRecord
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
            suggested_skills=("frontend",),
            permanentizable=True,
        )


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
