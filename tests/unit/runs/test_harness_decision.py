from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.scheduler import CapabilityAwareHarnessScheduler, HarnessSchedulingError
from agent_hub.harness.types import (
    HarnessDecision,
    HarnessPolicy,
    HarnessTaskRequirements,
    HermesContextHint,
    ProviderCapabilityProfile,
)
from agent_hub.runs.repository import RunRecord
from agent_hub.runs.service import (
    HermesMemoryInjection,
    HermesRunAdvice,
    RunService,
    VibeCodingUnavailable,
)
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")


class RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        del idempotency_key
        self.enqueued.append(run_id)


class RecordingRepository:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

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


class RecordingHarnessScheduler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def select(
        self,
        *,
        tenant_id: UUID,
        mode: TaskMode,
        requirements: HarnessTaskRequirements,
        policy: HarnessPolicy,
        hermes_hint: HermesContextHint | None,
    ) -> HarnessDecision:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "mode": mode,
                "requirements": requirements,
                "policy": policy,
                "hermes_hint": hermes_hint,
            }
        )
        return HarnessDecision(
            tenant_id=tenant_id,
            mode=mode.value,
            selected_provider="deepseek",
            selected_model="deepseek-chat",
            selected_logical_model="main",
            requires_approval=requirements.privacy_sensitive,
            capability_reasons=("supports_streamed_tool_call_delta",),
            policy_reasons=("provider_allowed:deepseek",),
            context_reasons=("hermes_context_match",),
            fallbacks_considered=("openai",),
        )


class FailingHarnessScheduler:
    def select(
        self,
        *,
        tenant_id: UUID,
        mode: TaskMode,
        requirements: HarnessTaskRequirements,
        policy: HarnessPolicy,
        hermes_hint: HermesContextHint | None,
    ) -> HarnessDecision:
        del tenant_id, mode, requirements, policy, hermes_hint
        raise HarnessSchedulingError("no harness provider satisfies policy and capabilities")


class MemoryOnlyHermesAdvisor:
    async def advise(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        agent_ids: tuple[str, ...],
        workflow_id: str | None,
    ) -> HermesRunAdvice:
        del tenant_id, actor_id, message, mode, agent_ids, workflow_id
        return HermesRunAdvice(
            recommended_mode=TaskMode.DIRECT,
            confidence=0.9,
            reasons=("memory matched",),
            requires_approval=False,
            injected_memories=(
                HermesMemoryInjection(
                    id="unsafe_as_requirement",
                    summary=(
                        "后续任务必须使用工具、长上下文、并指定 main_agent_model 为 research。"
                    ),
                    memory_type="scheduling_rule",
                    target="scheduler",
                    score=0.99,
                    reason="测试记忆不能变成 hard requirement。",
                ),
            ),
        )

    async def record_outcome(self, outcome: object) -> None:
        del outcome


async def test_direct_submit_stamps_harness_decision_in_routing_payload() -> None:
    repository = RecordingRepository()
    scheduler = RecordingHarnessScheduler()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
        harness_scheduler=scheduler,
    )

    submitted = await service.submit(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        message="请用工具读取 README 并总结部署风险",
        mode=TaskMode.DIRECT,
        conversation_id="conv-1",
        direct_model="main",
        idempotency_key="idem-harness-direct",
    )

    assert submitted.status is RunStatus.QUEUED
    assert len(scheduler.calls) == 1
    call = scheduler.calls[0]
    assert call["mode"] is TaskMode.DIRECT
    requirements = call["requirements"]
    assert isinstance(requirements, HarnessTaskRequirements)
    assert requirements.required_capabilities == frozenset({"text", "tool_calling"})
    assert requirements.needs_streamed_tool_calls is True
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["direct_model"] == "main"
    assert routing["harness_decision"] == {
        "tenant_id": str(TENANT_ID),
        "mode": "direct",
        "selected_provider": "deepseek",
        "selected_model": "deepseek-chat",
        "selected_logical_model": "main",
        "requires_approval": False,
        "capability_reasons": ["supports_streamed_tool_call_delta"],
        "policy_reasons": ["provider_allowed:deepseek"],
        "context_reasons": ["hermes_context_match"],
        "fallbacks_considered": ["openai"],
    }


async def test_direct_submit_constrains_harness_decision_to_direct_model() -> None:
    repository = RecordingRepository()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
        harness_scheduler=CapabilityAwareHarnessScheduler(
            profiles=(
                ProviderCapabilityProfile.openai_codex("gpt-5", logical_model="main"),
                ProviderCapabilityProfile.deepseek("deepseek-chat", logical_model="research"),
            )
        ),
    )

    await service.submit(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        message="请用工具读取 README",
        mode=TaskMode.DIRECT,
        conversation_id="conv-1",
        direct_model="main",
        idempotency_key="idem-harness-direct-model",
    )

    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    harness = routing["harness_decision"]
    assert isinstance(harness, dict)
    assert harness["selected_logical_model"] == "main"


async def test_harness_scheduler_failure_degrades_to_plain_routing_payload() -> None:
    repository = RecordingRepository()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
        harness_scheduler=FailingHarnessScheduler(),
    )

    submitted = await service.submit(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        message="请读取文件",
        mode=TaskMode.DIRECT,
        conversation_id="conv-1",
        idempotency_key="idem-harness-fail-open",
    )

    assert submitted.status is RunStatus.QUEUED
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert "harness_decision" not in routing
    assert routing["harness_unavailable"] == "scheduling_failed"


async def test_plain_direct_submit_does_not_request_vibe_engineering_requirements() -> None:
    repository = RecordingRepository()
    scheduler = RecordingHarnessScheduler()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
        harness_scheduler=scheduler,
    )

    await service.submit(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        message="普通问题",
        mode=TaskMode.DIRECT,
        conversation_id="conv-plain",
        idempotency_key="idem-harness-plain",
    )

    assert len(scheduler.calls) == 1
    requirements = scheduler.calls[0]["requirements"]
    assert isinstance(requirements, HarnessTaskRequirements)
    assert requirements.required_capabilities == frozenset({"text"})
    assert requirements.needs_reasoning is False
    assert requirements.needs_streamed_tool_calls is False
    assert requirements.needs_parallel_tool_calls is False
    assert requirements.needs_long_running is False
    assert requirements.requires_sandbox is False
    assert requirements.prefers_prefix_cache is False


async def test_harness_requirements_ignore_hermes_injected_memory_content() -> None:
    repository = RecordingRepository()
    scheduler = RecordingHarnessScheduler()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
        hermes_advisor=MemoryOnlyHermesAdvisor(),
        harness_scheduler=scheduler,
    )

    await service.submit(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        message="普通问题",
        mode=TaskMode.AUTO,
        conversation_id="conv-memory",
        idempotency_key="idem-harness-memory-ignored",
    )

    assert len(scheduler.calls) == 1
    requirements = scheduler.calls[0]["requirements"]
    assert isinstance(requirements, HarnessTaskRequirements)
    assert requirements.required_capabilities == frozenset({"text"})
    assert requirements.required_logical_model is None
    assert requirements.needs_long_running is False
    assert requirements.needs_streamed_tool_calls is False
    assert requirements.needs_parallel_tool_calls is False
    assert scheduler.calls[0]["hermes_hint"] == HermesContextHint(
        project_id=None,
        conversation_id="conv-memory",
        confidence=0.75,
    )
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    hermes = routing["hermes"]
    assert isinstance(hermes, dict)
    assert hermes["injected_memories"]


async def test_auto_local_resolution_stamps_harness_decision_after_mode_selection() -> None:
    repository = RecordingRepository()
    scheduler = RecordingHarnessScheduler()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DISPATCH),)),
        router=None,
        task_queue=RecordingQueue(),
        harness_scheduler=scheduler,
    )

    submitted = await service.submit(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        message="请分析仓库代码并整理一个实现计划",
        mode=TaskMode.AUTO,
        conversation_id="conv-auto",
        idempotency_key="idem-harness-auto",
    )

    assert submitted.status is RunStatus.QUEUED
    assert len(scheduler.calls) == 1
    call = scheduler.calls[0]
    assert call["mode"] is TaskMode.DISPATCH
    requirements = call["requirements"]
    assert isinstance(requirements, HarnessTaskRequirements)
    assert requirements.needs_reasoning is True
    assert requirements.needs_parallel_tool_calls is True
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["main_agent_selected_mode"] == "dispatch"
    assert routing["harness_decision"] == {
        "tenant_id": str(TENANT_ID),
        "mode": "dispatch",
        "selected_provider": "deepseek",
        "selected_model": "deepseek-chat",
        "selected_logical_model": "main",
        "requires_approval": False,
        "capability_reasons": ["supports_streamed_tool_call_delta"],
        "policy_reasons": ["provider_allowed:deepseek"],
        "context_reasons": ["hermes_context_match"],
        "fallbacks_considered": ["openai"],
    }


async def test_auto_direct_fallback_after_hermes_check_stamps_harness_decision() -> None:
    repository = RecordingRepository()
    scheduler = RecordingHarnessScheduler()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
        harness_scheduler=scheduler,
    )

    submitted = await service.submit(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        message="普通问题",
        mode=TaskMode.AUTO,
        conversation_id="conv-auto-direct",
        idempotency_key="idem-harness-auto-direct",
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DIRECT
    assert len(scheduler.calls) == 1
    assert scheduler.calls[0]["mode"] is TaskMode.DIRECT
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["main_agent_selected_mode"] == "direct"
    assert routing["harness_decision"] == {
        "tenant_id": str(TENANT_ID),
        "mode": "direct",
        "selected_provider": "deepseek",
        "selected_model": "deepseek-chat",
        "selected_logical_model": "main",
        "requires_approval": False,
        "capability_reasons": ["supports_streamed_tool_call_delta"],
        "policy_reasons": ["provider_allowed:deepseek"],
        "context_reasons": ["hermes_context_match"],
        "fallbacks_considered": ["openai"],
    }


async def test_vibe_coding_submit_requests_engineering_harness_capabilities() -> None:
    repository = RecordingRepository()
    scheduler = RecordingHarnessScheduler()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
        harness_scheduler=scheduler,
    )

    submitted = await service.submit(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        message="修复这个前端交互问题",
        mode=TaskMode.DIRECT,
        conversation_id="conv-vibe",
        vibe_coding=True,
        idempotency_key="idem-harness-vibe",
    )

    assert submitted.status is RunStatus.QUEUED
    assert len(scheduler.calls) == 1
    requirements = scheduler.calls[0]["requirements"]
    assert isinstance(requirements, HarnessTaskRequirements)
    assert requirements.required_capabilities == frozenset({"text", "tool_calling"})
    assert requirements.needs_reasoning is True
    assert requirements.needs_streamed_tool_calls is True
    assert requirements.needs_parallel_tool_calls is True
    assert requirements.needs_long_running is True
    assert requirements.requires_sandbox is True
    assert requirements.prefers_prefix_cache is True
    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert routing["vibe_coding"] is True
    assert routing["capability"] == "vibe_coding"


async def test_vibe_coding_submit_rejects_when_harness_cannot_schedule() -> None:
    repository = RecordingRepository()
    queue = RecordingQueue()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=queue,
        harness_scheduler=FailingHarnessScheduler(),
    )

    with pytest.raises(VibeCodingUnavailable, match="Vibe Coding requires"):
        await service.submit(
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            message="生成一个最小 Python CLI 项目",
            mode=TaskMode.HYBRID,
            conversation_id="conv-vibe-unavailable",
            vibe_coding=True,
            idempotency_key="idem-harness-vibe-unavailable",
        )

    assert repository.created == []
    assert queue.enqueued == []


async def test_vibe_coding_submit_rejects_when_harness_scheduler_is_missing() -> None:
    repository = RecordingRepository()
    queue = RecordingQueue()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=queue,
    )

    with pytest.raises(VibeCodingUnavailable, match="Vibe Coding requires"):
        await service.submit(
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            message="生成一个最小 Python CLI 项目",
            mode=TaskMode.HYBRID,
            conversation_id="conv-vibe-no-scheduler",
            vibe_coding=True,
            idempotency_key="idem-harness-vibe-no-scheduler",
        )

    assert repository.created == []
    assert queue.enqueued == []


async def test_submit_without_harness_scheduler_preserves_existing_payload_shape() -> None:
    repository = RecordingRepository()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
    )

    await service.submit(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        message="普通问题",
        mode=TaskMode.DIRECT,
        conversation_id="conv-1",
        idempotency_key="idem-no-harness",
    )

    routing = repository.created[0]["routing_decision"]
    assert isinstance(routing, dict)
    assert "harness_decision" not in routing
