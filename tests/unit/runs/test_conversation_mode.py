from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.routing.types import EXECUTABLE_MODES, RiskLevel, RouteDecision
from agent_hub.runs.repository import RunRecord
from agent_hub.runs.service import RunService
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry


class RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        del idempotency_key
        self.enqueued.append(run_id)


class WaitingRouter:
    def __init__(self) -> None:
        self.calls = 0

    async def route(self, task_text: object) -> RouteDecision:
        del task_text
        self.calls += 1
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


class ConversationModeRepository:
    def __init__(self, previous_mode: TaskMode | None) -> None:
        self.previous_mode = previous_mode
        self.created: list[dict[str, object]] = []

    async def latest_resolved_mode_for_conversation(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        conversation_id: str,
    ) -> TaskMode | None:
        del tenant_id, actor_id, conversation_id
        return self.previous_mode

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
        del idempotency_key, enqueue
        self.created.append(
            {
                "request": request,
                "mode": mode,
                "status": status,
                "routing_decision": routing_decision,
            }
        )
        return RunRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            actor_id=actor_id,
            request=request,
            mode=mode,
            status=status,
            version=1,
            created_at=datetime.now(UTC),
            routing_decision=routing_decision,
        )


async def test_auto_submission_reuses_previous_mode_for_same_conversation_without_reasking() -> None:
    repository = ConversationModeRepository(TaskMode.HYBRID)
    router = WaitingRouter()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)),
        router=router,
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="预算是多少",
        mode=TaskMode.AUTO,
        conversation_id="conv-1",
        idempotency_key="idem-continuation",
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.HYBRID
    assert submitted.clarification_reason is None
    assert router.calls == 0
    assert repository.created[0]["routing_decision"] == {
        "reason": "conversation_mode_continuation",
        "main_agent_selected_mode": "hybrid",
        "mode_source": "previous_conversation_run",
        "selected_agent_ids": [],
        "workflow_id": None,
        "allow_workflow_adjustment": False,
        "workflow_adjustment_policy": "strict_preset",
        "conversation_id": "conv-1",
        "reference_conversation_id": None,
        "attachment_ids": [],
    }


async def test_auto_reuses_previous_mode_when_discussion_is_context() -> None:
    repository = ConversationModeRepository(TaskMode.HYBRID)
    router = WaitingRouter()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)),
        router=router,
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="继续刚刚的方案，用上一轮讨论结论补充执行细节",
        mode=TaskMode.AUTO,
        conversation_id="conv-1",
        idempotency_key="idem-continuation-discussion-word",
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.HYBRID
    assert router.calls == 0
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["reason"] == "conversation_mode_continuation"


async def test_auto_reuses_previous_mode_when_mixed_model_is_context() -> None:
    repository = ConversationModeRepository(TaskMode.HYBRID)
    router = WaitingRouter()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)),
        router=router,
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="继续解释刚刚说的混合模型为什么会被识别错",
        mode=TaskMode.AUTO,
        conversation_id="conv-1",
        idempotency_key="idem-continuation-mixed-model-word",
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.HYBRID
    assert router.calls == 0
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["reason"] == "conversation_mode_continuation"


async def test_auto_submission_switches_mode_when_user_explicitly_requests_it() -> None:
    repository = ConversationModeRepository(TaskMode.HYBRID)
    router = WaitingRouter()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DISCUSS),)),
        router=router,
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="这轮切换到讨论模式",
        mode=TaskMode.AUTO,
        conversation_id="conv-1",
        idempotency_key="idem-mode-switch",
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DISCUSS
    assert router.calls == 0
    assert repository.created[0]["routing_decision"] == {
        "reason": "conversation_mode_switch",
        "main_agent_selected_mode": "discuss",
        "mode_source": "explicit_user_request",
        "selected_agent_ids": [],
        "workflow_id": None,
        "allow_workflow_adjustment": False,
        "workflow_adjustment_policy": "strict_preset",
        "conversation_id": "conv-1",
        "reference_conversation_id": None,
        "attachment_ids": [],
    }


async def test_auto_submission_does_not_reuse_mode_when_user_requests_new_conversation() -> None:
    repository = ConversationModeRepository(TaskMode.HYBRID)
    router = WaitingRouter()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)),
        router=router,
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="换个话题，帮我看一个新问题",
        mode=TaskMode.AUTO,
        conversation_id="conv-1",
        idempotency_key="idem-new-conversation",
    )

    assert submitted.status is RunStatus.WAITING_USER_MODE
    assert submitted.mode is None
    assert submitted.clarification_reason == "classification_unavailable"
    assert router.calls == 1
