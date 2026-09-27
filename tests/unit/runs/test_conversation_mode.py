from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.memory.persistent import RuntimeMemoryItem
from agent_hub.routing.types import EXECUTABLE_MODES, RiskLevel, RouteDecision
from agent_hub.runs.conversations import ConversationArchived, ConversationRecord
from agent_hub.runs.repository import RunRecord
from agent_hub.runs.service import (
    HermesMemoryInjection,
    HermesRunAdvice,
    HermesRunOutcome,
    HermesSkippedMemory,
    RunService,
)
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
        actor_role: Role | None = None,
        routing_decision: dict[str, object] | None = None,
        enqueue: bool,
    ) -> RunRecord:
        del idempotency_key, actor_role, enqueue
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


class ConversationMetadataRepository:
    def __init__(self, record: ConversationRecord) -> None:
        self.record = record
        self.lookups: list[tuple[UUID, str]] = []

    async def find(self, tenant_id: UUID, conversation_id: str) -> ConversationRecord | None:
        self.lookups.append((tenant_id, conversation_id))
        if tenant_id != self.record.tenant_id or conversation_id != self.record.conversation_id:
            return None
        return self.record


class PreviewMutationRepository:
    def __init__(self, *, tenant_id: UUID, actor_id: UUID, run_id: UUID) -> None:
        self.sequence: list[str] = []
        self.record = RunRecord(
            id=run_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            request="continue",
            mode=TaskMode.DIRECT,
            status=RunStatus.QUEUED,
            version=1,
            created_at=datetime.now(UTC),
            routing_decision={"conversation_id": "conv-preview"},
        )

    async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
        assert tenant_id == self.record.tenant_id
        assert run_id == self.record.id
        return self.record

    async def approve_temporary_agent_and_enqueue(self, **kwargs: object) -> RunRecord:
        del kwargs
        self.sequence.append("mutation")
        return self.record

    async def revise_temporary_agent_and_enqueue(self, **kwargs: object) -> RunRecord:
        del kwargs
        self.sequence.append("mutation")
        return self.record

    async def accept_self_repair_and_enqueue(self, **kwargs: object) -> RunRecord:
        del kwargs
        self.sequence.append("mutation")
        return self.record

    async def approve_project_preflight_and_enqueue(self, **kwargs: object) -> RunRecord:
        del kwargs
        self.sequence.append("mutation")
        return self.record

    async def approve_capability_and_enqueue(self, **kwargs: object) -> RunRecord:
        del kwargs
        self.sequence.append("mutation")
        return self.record

    async def choose_mode_and_enqueue(self, **kwargs: object) -> RunRecord:
        del kwargs
        self.sequence.append("mutation")
        return self.record

    async def enqueue_existing_run(self, **kwargs: object) -> RunRecord:
        del kwargs
        self.sequence.append("mutation")
        return self.record

    async def completed_step_ids(self, tenant_id: UUID, run_id: UUID) -> tuple[str, ...]:
        del tenant_id, run_id
        return ()

    async def artifact_ids(self, tenant_id: UUID, run_id: UUID) -> tuple[str, ...]:
        del tenant_id, run_id
        return ()

    async def usage_cost(self, tenant_id: UUID, run_id: UUID) -> float:
        del tenant_id, run_id
        return 0.0


class RecordingHermesAdvisor:
    def __init__(self, advice: HermesRunAdvice | None) -> None:
        self.advice = advice
        self.calls: list[dict[str, object]] = []

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
        self.calls.append(
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
        del outcome


class SlowHermesAdvisor(RecordingHermesAdvisor):
    async def advise(self, **kwargs: object) -> HermesRunAdvice | None:
        await asyncio.sleep(2)
        return None


class RecordingRuntimeMemoryRecall:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def recall(self, **kwargs: object) -> tuple[RuntimeMemoryItem, ...]:
        self.calls.append(kwargs)
        return (
            RuntimeMemoryItem(
                id="project-test-policy",
                summary="Use pytest for backend verification.",
                layer="episodic",
                category="fact",
                score=0.91,
                reason="当前项目记忆",
            ),
        )


async def test_submit_stops_conversation_preview_before_creating_run() -> None:
    repository = ConversationModeRepository(None)
    stopped: list[tuple[UUID, str]] = []

    async def stop_preview(tenant_id: UUID, conversation_id: str) -> None:
        stopped.append((tenant_id, conversation_id))

    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
        conversation_preview_stopper=stop_preview,
    )
    tenant_id = uuid4()

    await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="continue the project",
        mode=TaskMode.DIRECT,
        conversation_id="conv-preview",
    )

    assert stopped == [(tenant_id, "conv-preview")]
    assert len(repository.created) == 1


async def test_submit_preview_stop_failure_prevents_run_creation() -> None:
    repository = ConversationModeRepository(None)

    async def stop_preview(_tenant_id: UUID, _conversation_id: str) -> None:
        raise RuntimeError("preview stop failed")

    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
        conversation_preview_stopper=stop_preview,
    )

    with pytest.raises(RuntimeError, match="preview stop failed"):
        await service.submit(
            tenant_id=uuid4(),
            actor_id=uuid4(),
            message="continue the project",
            mode=TaskMode.DIRECT,
            conversation_id="conv-preview",
        )

    assert repository.created == []


@pytest.mark.parametrize(
    ("method_name", "method_kwargs"),
    [
        (
            "approve_temporary_agent",
            {"actor_id": uuid4(), "decision_token": "decision", "version": 1},
        ),
        (
            "revise_temporary_agent",
            {
                "actor_id": uuid4(),
                "decision_token": "decision",
                "version": 1,
                "feedback": "revise this",
            },
        ),
        (
            "accept_self_repair",
            {"actor_id": uuid4(), "decision_token": "decision", "version": 1},
        ),
        (
            "approve_project_preflight",
            {"actor_id": uuid4(), "decision_token": "decision", "version": 1},
        ),
        (
            "approve_capability",
            {"actor_id": uuid4(), "approval_id": "approval", "version": 1},
        ),
        (
            "choose_mode",
            {
                "actor_id": uuid4(),
                "mode": TaskMode.DIRECT,
                "decision_token": "decision",
                "version": 1,
            },
        ),
        ("resume", {}),
    ],
)
async def test_run_state_entry_stops_preview_before_mutation(
    method_name: str,
    method_kwargs: dict[str, object],
) -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    run_id = uuid4()
    repository = PreviewMutationRepository(
        tenant_id=tenant_id,
        actor_id=actor_id,
        run_id=run_id,
    )

    async def stop_preview(actual_tenant_id: UUID, conversation_id: str) -> None:
        assert actual_tenant_id == tenant_id
        assert conversation_id == "conv-preview"
        repository.sequence.append("stop")

    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
        conversation_preview_stopper=stop_preview,
    )
    method = getattr(service, method_name)

    await method(tenant_id=tenant_id, run_id=run_id, **method_kwargs)

    assert repository.sequence == ["stop", "mutation"]


@pytest.mark.parametrize(
    "mode",
    [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID],
)
async def test_explicit_modes_receive_persistent_memory_without_changing_mode(
    mode: TaskMode,
) -> None:
    repository = ConversationModeRepository(None)
    recall = RecordingRuntimeMemoryRecall()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(mode),)),
        router=None,
        task_queue=RecordingQueue(),
        runtime_memory_recall=recall,
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="verify the backend",
        mode=mode,
        project_id="cube-agent",
        conversation_id="conv-memory",
    )

    assert submitted.mode is mode
    routing = repository.created[-1]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["memory"] == {
        "items": [
            {
                "id": "project-test-policy",
                "summary": "Use pytest for backend verification.",
                "layer": "episodic",
                "category": "fact",
                "score": 0.91,
                "reason": "当前项目记忆",
            }
        ]
    }
    assert recall.calls[0]["project_id"] == "cube-agent"
    assert recall.calls[0]["conversation_id"] == "conv-memory"


async def test_auto_conversation_continuation_receives_persistent_memory() -> None:
    repository = ConversationModeRepository(TaskMode.HYBRID)
    recall = RecordingRuntimeMemoryRecall()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)),
        router=WaitingRouter(),
        task_queue=RecordingQueue(),
        runtime_memory_recall=recall,
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="continue backend verification",
        mode=TaskMode.AUTO,
        project_id="cube-agent",
        conversation_id="conv-memory",
    )

    assert submitted.mode is TaskMode.HYBRID
    routing = repository.created[-1]["routing_decision"]
    assert isinstance(routing, dict)
    assert isinstance(routing.get("memory"), dict)


async def test_auto_natural_large_website_persists_workspace_first_delivery_contract() -> None:
    repository = ConversationModeRepository(None)
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)),
        router=None,
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="编写一个网盘网站",
        mode=TaskMode.AUTO,
        conversation_id="conv-natural-large-project",
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.HYBRID
    routing = repository.created[-1]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing.items() >= {
        "project_scale": "large",
        "project_delivery": "workspace",
        "artifact_strategy": "workspace_bundle",
        "website_preview_required": True,
        "runtime_timeout_seconds": 1200.0,
        "main_agent_selected_mode": "hybrid",
    }.items()


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
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing.items() >= {
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
            "project_id": "default",
            "workspace_session_id": "conv-1",
            "sandbox_profile": "workspace_write",
            "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
    }.items()


async def test_auto_large_project_upgrades_previous_direct_conversation_mode() -> None:
    repository = ConversationModeRepository(TaskMode.DIRECT)
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
        message="编写一个带登录、上传、分享、搜索和权限管理的网盘网站",
        mode=TaskMode.AUTO,
        conversation_id="conv-previous-direct",
    )

    assert submitted.mode is TaskMode.HYBRID
    assert router.calls == 0
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing.items() >= {
        "reason": "project_scale_mode_upgrade",
        "main_agent_selected_mode": "hybrid",
        "mode_source": "project_scale_assessment",
        "project_scale": "large",
        "project_delivery": "workspace",
    }.items()


@pytest.mark.parametrize(
    ("message", "expected_scale"),
    (
        ("开发一个带登录、上传、分享、搜索和权限管理的大型网盘网站", "large"),
        ("构建一个超大型企业项目管理平台", "ultra"),
    ),
)
async def test_explicit_direct_large_project_is_upgraded_before_runtime(
    message: str,
    expected_scale: str,
) -> None:
    repository = ConversationModeRepository(None)
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)),
        router=None,
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message=message,
        mode=TaskMode.DIRECT,
        conversation_id=f"conv-explicit-direct-{expected_scale}",
    )

    assert submitted.mode is TaskMode.HYBRID
    routing = repository.created[-1]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing.items() >= {
        "reason": "project_scale_mode_upgrade",
        "requested_mode": "direct",
        "main_agent_selected_mode": "hybrid",
        "mode_source": "project_scale_assessment",
        "project_scale": expected_scale,
        "project_delivery": "workspace",
        "artifact_strategy": "workspace_bundle",
    }.items()


async def test_reference_workflow_is_advisory_and_persisted_without_selecting_workflow() -> None:
    repository = ConversationModeRepository(TaskMode.HYBRID)
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)),
        router=WaitingRouter(),
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="参考短视频方案继续执行",
        mode=TaskMode.AUTO,
        conversation_id="conv-reference-workflow",
        reference_workflow_id="short-video-dispatch",
    )

    assert submitted.mode is TaskMode.HYBRID
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["reference_workflow_id"] == "short-video-dispatch"
    assert routing["workflow_id"] is None


async def test_existing_conversation_metadata_overrides_temporary_run_workspace_values() -> None:
    tenant_id = uuid4()
    repository = ConversationModeRepository(TaskMode.HYBRID)
    metadata_repository = ConversationMetadataRepository(
        ConversationRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            conversation_id="conv-persisted",
            title="持久化会话",
            project_id="persisted-project",
            project_label="持久化项目",
            workspace_path="persisted-workspace",
            archived_at=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)),
        router=WaitingRouter(),
        task_queue=RecordingQueue(),
        conversation_repository=metadata_repository,
    )

    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="继续处理",
        mode=TaskMode.AUTO,
        conversation_id="conv-persisted",
        project_id="temporary-project",
        project_label="临时项目",
        workspace_session_id="temporary-workspace",
    )

    assert submitted.project_id == "persisted-project"
    assert submitted.project_label == "持久化项目"
    assert submitted.workspace_session_id == "persisted-workspace"
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["project_id"] == "persisted-project"
    assert routing["project_label"] == "持久化项目"
    assert routing["workspace_session_id"] == "persisted-workspace"
    assert metadata_repository.lookups == [(tenant_id, "conv-persisted")]


async def test_archived_conversation_cannot_accept_a_new_run() -> None:
    tenant_id = uuid4()
    repository = ConversationModeRepository(TaskMode.HYBRID)
    metadata_repository = ConversationMetadataRepository(
        ConversationRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            conversation_id="conv-archived",
            title="已归档",
            project_id="default",
            project_label="默认项目",
            workspace_path="conv-archived",
            archived_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.HYBRID),)),
        router=WaitingRouter(),
        task_queue=RecordingQueue(),
        conversation_repository=metadata_repository,
    )

    with pytest.raises(ConversationArchived, match="archived"):
        await service.submit(
            tenant_id=tenant_id,
            actor_id=uuid4(),
            message="继续处理",
            mode=TaskMode.AUTO,
            conversation_id="conv-archived",
        )

    assert repository.created == []


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
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing.items() >= {
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
            "project_id": "default",
            "workspace_session_id": "conv-1",
            "sandbox_profile": "workspace_write",
            "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
    }.items()


async def test_auto_submission_does_not_reuse_previous_mode_when_user_requests_new_conversation() -> None:
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

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DIRECT
    assert submitted.clarification_reason is None
    assert router.calls == 1


async def test_auto_submission_queues_local_direct_when_router_cannot_classify() -> None:
    repository = ConversationModeRepository(None)
    router = WaitingRouter()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=router,
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="为什么刚才任务停住了",
        mode=TaskMode.AUTO,
        conversation_id="conv-1",
        idempotency_key="idem-auto-direct-fallback",
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DIRECT
    assert submitted.clarification_reason is None
    assert router.calls == 1
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["reason"] == "main_agent_local_resolution"
    assert routing["main_agent_selected_mode"] == "direct"
    assert routing["router_clarification_reason"] == "classification_unavailable"


async def test_auto_submission_waits_when_router_requires_user_choice() -> None:
    repository = ConversationModeRepository(None)
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=UserChoiceRouter(),
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="ambiguous workflow",
        mode=TaskMode.AUTO,
        conversation_id="conv-1",
        idempotency_key="idem-router-user-choice",
    )

    assert submitted.status is RunStatus.WAITING_USER_MODE
    assert submitted.mode is None
    assert submitted.decision_token == "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234"
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["reason"] == "routing_requires_user_choice"
    assert "main_agent_selected_mode" not in routing


async def test_get_preserves_actionable_mode_decision_for_public_recovery() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    run_id = uuid4()
    repository = PreviewMutationRepository(
        tenant_id=tenant_id,
        actor_id=actor_id,
        run_id=run_id,
    )
    repository.record = RunRecord(
        id=run_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        request="ambiguous workflow",
        mode=None,
        status=RunStatus.WAITING_USER_MODE,
        version=3,
        created_at=datetime.now(UTC),
        routing_decision={
            "reason": "routing_requires_user_choice",
            "decision_token": "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
            "conversation_id": "conv-recovery",
        },
    )
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
    )

    summary = await service.get(tenant_id, run_id)

    assert summary.decision_token == "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234"
    assert summary.clarification_reason == "routing_requires_user_choice"

    repository.record = RunRecord(
        id=run_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        request="ambiguous workflow",
        mode=TaskMode.HYBRID,
        status=RunStatus.COMPLETED,
        version=4,
        created_at=datetime.now(UTC),
        routing_decision={
            "reason": "routing_requires_user_choice",
            "decision_token": "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
        },
    )

    completed = await service.get(tenant_id, run_id)

    assert completed.decision_token is None
    assert completed.clarification_reason is None


async def test_get_exposes_only_current_waiting_capability_approval_id() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    run_id = uuid4()
    repository = PreviewMutationRepository(
        tenant_id=tenant_id,
        actor_id=actor_id,
        run_id=run_id,
    )
    repository.record = RunRecord(
        id=run_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        request="run an approved capability",
        mode=TaskMode.DISPATCH,
        status=RunStatus.WAITING_APPROVAL,
        version=3,
        created_at=datetime.now(UTC),
        routing_decision={
            "approval_kind": "capability_tool",
            "approval_id": "capability_approval_public_1",
            "approval_fingerprint": "fingerprint-1",
            "reason": "capability requires approval",
        },
    )
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
    )

    waiting = await service.get(tenant_id, run_id)

    assert waiting.approval_id == "capability_approval_public_1"

    repository.record = RunRecord(
        id=run_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        request="run an approved capability",
        mode=TaskMode.DISPATCH,
        status=RunStatus.COMPLETED,
        version=4,
        created_at=datetime.now(UTC),
        routing_decision={
            "approval_kind": "capability_tool",
            "approval_id": "capability_approval_public_1",
            "approval_fingerprint": "fingerprint-1",
            "reason": "capability requires approval",
        },
    )

    completed = await service.get(tenant_id, run_id)

    assert completed.approval_id is None

    repository.record = RunRecord(
        id=run_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        request="approve a project plan",
        mode=TaskMode.HYBRID,
        status=RunStatus.WAITING_APPROVAL,
        version=5,
        created_at=datetime.now(UTC),
        routing_decision={
            "approval_kind": "project_preflight",
            "approval_id": "non_capability_approval",
            "reason": "project preflight requires approval",
        },
    )

    non_capability = await service.get(tenant_id, run_id)

    assert non_capability.approval_id is None


async def test_auto_submission_uses_hermes_before_local_direct_router_fallback() -> None:
    repository = ConversationModeRepository(None)
    advisor = RecordingHermesAdvisor(
        HermesRunAdvice(
            recommended_mode=TaskMode.DISPATCH,
            confidence=0.86,
            reasons=("matched previous execution pattern",),
            recommended_skills=("script-review",),
            requires_approval=False,
        )
    )
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DISPATCH),)),
        router=None,
        task_queue=RecordingQueue(),
        hermes_advisor=advisor,
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="short video script",
        mode=TaskMode.AUTO,
        conversation_id="conv-1",
        idempotency_key="idem-hermes-before-direct-fallback",
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DISPATCH
    assert len(advisor.calls) == 1
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["reason"] == "hermes_recommendation"


async def test_auto_submission_records_hermes_injected_memory_payload() -> None:
    repository = ConversationModeRepository(None)
    advisor = RecordingHermesAdvisor(
        HermesRunAdvice(
            recommended_mode=TaskMode.DISPATCH,
            confidence=0.86,
            reasons=("matched previous execution pattern",),
            recommended_skills=("script-review",),
            requires_approval=False,
            injected_memories=(
                HermesMemoryInjection(
                    id="hermes_confirmed_review",
                    summary="reviewer 超时时先压缩上下文再分块审查。",
                    memory_type="error_handling",
                    target="reviewer",
                    score=0.91,
                    reason="命中 reviewer 超时处理经验",
                ),
            ),
            skipped_memories=(
                HermesSkippedMemory(
                    id="hermes_old_direct",
                    summary="旧 direct 模式观察。",
                    reason="当前任务相关性不足",
                    score=0.42,
                ),
            ),
        )
    )
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DISPATCH),)),
        router=None,
        task_queue=RecordingQueue(),
        hermes_advisor=advisor,
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="short video script",
        mode=TaskMode.AUTO,
        conversation_id="conv-1",
        idempotency_key="idem-hermes-memory-payload",
    )

    assert submitted.status is RunStatus.QUEUED
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    hermes = routing["hermes"]
    assert isinstance(hermes, dict)
    assert hermes["injected_memories"] == [
        {
            "id": "hermes_confirmed_review",
            "summary": "reviewer 超时时先压缩上下文再分块审查。",
            "memory_type": "error_handling",
            "target": "reviewer",
            "score": 0.91,
            "reason": "命中 reviewer 超时处理经验",
        }
    ]
    assert hermes["skipped_memories"][0]["reason"] == "当前任务相关性不足"


async def test_hermes_advice_timeout_does_not_block_auto_submission() -> None:
    repository = ConversationModeRepository(None)
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
        hermes_advisor=SlowHermesAdvisor(None),
    )

    started = time.monotonic()
    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="hello",
        mode=TaskMode.AUTO,
        conversation_id="conv-1",
        idempotency_key="idem-hermes-timeout",
    )
    elapsed = time.monotonic() - started

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DIRECT
    assert elapsed < 1.5


async def test_declined_evolution_proposal_can_continue_through_auto_mode() -> None:
    repository = ConversationModeRepository(None)
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="请进化 darwin-skill，做多轮迭代",
        mode=TaskMode.AUTO,
        conversation_id="conv-1",
        skip_evolution_proposal=True,
        idempotency_key="idem-declined-evolution-continue",
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DIRECT
    assert submitted.evolution_proposal is None
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["reason"] == "main_agent_local_resolution"
    assert routing["main_agent_selected_mode"] == "direct"
    assert routing["skip_evolution_proposal"] is True
