from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, TaskContext
from agent_hub.runtime.crew.adapter import (
    CrewAgentDefinition,
    CrewDispatchRuntime,
    CrewLLMBridge,
    CrewObjectFactory,
    CrewTaskDefinition,
    RuntimeExecutionError,
    _artifact_final_synthesis_payload,
    _artifact_prompt_payload,
    _artifact_review_packet_payload,
)
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
RUN_ID = UUID("00000000-0000-4000-8000-000000000002")


class UnusedGateway:
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        return GatewayCompletion(
            response=ModelResponse(text="unused", usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class LargeCandidateGateway:
    def __init__(self) -> None:
        self.large_text = "review candidate body " * 2_000

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        prompt = " ".join(cast(str, message.content) for message in request.messages)
        text = '{"verdict":"approve"}' if "REVIEWER" in prompt else self.large_text
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class ReviewAwareGateway:
    def __init__(self, reviewer_responses: tuple[str, ...] = ('{"verdict":"approve"}',)) -> None:
        self._reviewer_responses = list(reviewer_responses)

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        prompt = " ".join(cast(str, message.content) for message in request.messages)
        if "REVIEWER" in prompt:
            text = self._reviewer_responses.pop(0)
        else:
            text = "draft output " * 200
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class FailingGeneration:
    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, prompt, bridge, agent_id, storage_scope
        raise ValueError("agent identifier must be a safe identifier")


class FailingFactory(CrewObjectFactory):
    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> FailingGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        return FailingGeneration()


class TimeoutGeneration:
    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, prompt, bridge, agent_id, storage_scope
        raise TimeoutError


class TimeoutFactory(CrewObjectFactory):
    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> TimeoutGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        return TimeoutGeneration()


class StepTimeoutOnceGeneration:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, agent_id, storage_scope
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            raise TimeoutError
        return await bridge.complete([{"role": "user", "content": prompt}])


class StepTimeoutOnceFactory(CrewObjectFactory):
    def __init__(self) -> None:
        self.generation = StepTimeoutOnceGeneration()

    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> StepTimeoutOnceGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        return self.generation


class SlowStepGeneration:
    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, prompt, bridge, agent_id, storage_scope
        await asyncio.sleep(1)
        return "late"


class SlowStepFactory(CrewObjectFactory):
    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> SlowStepGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        return SlowStepGeneration()


class SlowThenFastStepGeneration:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, prompt, agent_id, storage_scope
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(1)
        return await bridge.complete([{"role": "user", "content": "recover"}])


class SlowThenFastStepFactory(CrewObjectFactory):
    def __init__(self) -> None:
        self.generation = SlowThenFastStepGeneration()

    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> SlowThenFastStepGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        return self.generation


class ReviewerTimeoutGeneration:
    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, storage_scope
        if agent_id == "critic":
            raise TimeoutError
        return await bridge.complete([{"role": "user", "content": prompt}])


class ReviewerTimeoutOnceGeneration:
    def __init__(self) -> None:
        self.review_calls = 0

    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, storage_scope
        if agent_id == "critic":
            self.review_calls += 1
            if self.review_calls == 1:
                raise TimeoutError
        return await bridge.complete([{"role": "user", "content": prompt}])


class ReviewerTimeoutFactory(CrewObjectFactory):
    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> ReviewerTimeoutGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        return ReviewerTimeoutGeneration()


class ReviewerTimeoutOnceFactory(CrewObjectFactory):
    def __init__(self) -> None:
        self.generation = ReviewerTimeoutOnceGeneration()

    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> ReviewerTimeoutOnceGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        return self.generation


class CapturingGeneration:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, agent_id, storage_scope
        self.prompts.append(prompt)
        return await bridge.complete([{"role": "user", "content": prompt}])


class CapturingFactory(CrewObjectFactory):
    def __init__(self) -> None:
        self.generation = CapturingGeneration()

    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> CapturingGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        return self.generation


def _one_step_plan(*, timeout_seconds: float = 60.0) -> DispatchPlan:
    return DispatchPlan(
        agents=(AgentSpec(id="writer", role="writer", goal="Write", logical_model="general"),),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=100,
                timeout_seconds=timeout_seconds,
            ),
        ),
        total_token_budget=100,
        total_timeout_seconds=max(60.0, timeout_seconds * 4),
    )


def _reviewed_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(id="writer", role="writer", goal="Write", logical_model="general"),
            AgentSpec(id="critic", role="critic", goal="Review", logical_model="general"),
        ),
        steps=(
            DispatchStep(
                id="draft",
                agent="writer",
                task="Draft",
                reviewer="critic",
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        total_token_budget=200,
    )


def _reviewed_plan_with_retry_budget(reviewer_retries: int = 1) -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(id="writer", role="writer", goal="Write", logical_model="general"),
            AgentSpec(id="critic", role="critic", goal="Review", logical_model="general"),
        ),
        steps=(
            DispatchStep(
                id="draft",
                agent="writer",
                task="Draft",
                reviewer="critic",
                reviewer_retries=reviewer_retries,
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        total_token_budget=200,
    )


def _context(*, artifacts: tuple[Artifact, ...] = (), timeout_seconds: float = 60.0) -> TaskContext:
    return TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="Write a short answer",
        artifacts=artifacts,
        timeout_seconds=timeout_seconds,
        token_budget=1000,
    )


def test_openai_tool_call_response_can_have_empty_text() -> None:
    completion = GatewayCompletion(
        response=ModelResponse(
            text="",
            tool_calls=(ToolCall(id="call_1", name="read_context", arguments={"query": "x"}),),
            usage=TokenUsage(10, 1, 11),
        ),
        deployment_id="primary",
        logical_model="general",
        provider_id="deepseek",
        provider_model="deepseek/deepseek-v4-flash",
        cost_usd=Decimal(0),
    )

    response = CrewDispatchRuntime._valid_response(completion)

    assert response.tool_calls[0].name == "read_context"


def test_text_only_empty_model_response_still_fails() -> None:
    completion = GatewayCompletion(
        response=ModelResponse(text="", usage=TokenUsage(10, 0, 10)),
        deployment_id="primary",
        logical_model="general",
        provider_id="deepseek",
        provider_model="deepseek/deepseek-v4-flash",
        cost_usd=Decimal(0),
    )

    with pytest.raises(RuntimeExecutionError, match="model response text is empty"):
        CrewDispatchRuntime._valid_response(completion)


async def _collect(runtime: CrewDispatchRuntime) -> list[RunEvent]:
    return [event async for event in runtime.run(_context())]


async def test_dispatch_framework_failure_records_safe_root_cause() -> None:
    runtime = CrewDispatchRuntime(
        UnusedGateway(),
        _one_step_plan(),
        crew_factory=FailingFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError) as caught:
        async for event in runtime.run(_context()):
            events.append(event)

    expected = "CrewAI step execution failed: agent identifier must be a safe identifier"
    assert str(caught.value) == expected
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == expected
    assert any(event.kind is EventKind.STEP_FAILED and event.reason == expected for event in events)


async def test_dispatch_framework_timeout_names_the_step_and_actor() -> None:
    runtime = CrewDispatchRuntime(
        UnusedGateway(),
        _one_step_plan(),
        crew_factory=TimeoutFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError) as caught:
        async for event in runtime.run(_context()):
            events.append(event)

    expected = "CrewAI step timed out: step=final actor=writer"
    assert str(caught.value) == expected
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == expected
    assert events[-1].payload["error_code"] == "crew.step_timeout"
    assert events[-1].payload["step_id"] == "final"
    assert events[-1].payload["actor"] == "writer"
    assert any(
        event.kind is EventKind.STEP_FAILED
        and event.reason == expected
        and event.payload["error_code"] == "crew.step_timeout"
        and event.payload["step_id"] == "final"
        and event.payload["actor"] == "writer"
        for event in events
    )


async def test_dispatch_step_timeout_retries_with_compact_recovery_prompt() -> None:
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="researcher",
        content={"text": "large source context " * 500},
    )
    factory = StepTimeoutOnceFactory()
    runtime = CrewDispatchRuntime(
        ReviewAwareGateway(),
        _one_step_plan(),
        crew_factory=factory,
    )

    events = [event async for event in runtime.run(_context(artifacts=(artifact,)))]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert factory.generation.calls == 2
    retry = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retry.actor == "writer"
    assert retry.reason == "step execution timed out; retrying with compact recovery"
    assert retry.payload["attempt"] == 2
    assert retry.payload["strategy"] == "compact_retry"
    assert retry.payload["fallback_policy"] == "fail_if_retry_exhausted"
    assert retry.payload["error_code"] == "crew.step_timeout"
    assert retry.payload["step_id"] == "final"
    assert retry.payload["actor"] == "writer"
    assert "compact_retry" in factory.generation.prompts[1]
    assert len(factory.generation.prompts[1].encode("utf-8")) < len(
        factory.generation.prompts[0].encode("utf-8")
    )


async def test_dispatch_step_timeout_recovery_keeps_each_attempt_on_step_deadline() -> None:
    factory = SlowThenFastStepFactory()
    runtime = CrewDispatchRuntime(
        ReviewAwareGateway(),
        _one_step_plan(timeout_seconds=0.05),
        crew_factory=factory,
    )

    events = [event async for event in runtime.run(_context(timeout_seconds=1.0))]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert factory.generation.calls == 2
    retry = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retry.payload["timeout_policy"] == "use_remaining_step_budget"


async def test_dispatch_step_retry_is_suppressed_when_runtime_budget_is_exhausted() -> None:
    runtime = CrewDispatchRuntime(
        UnusedGateway(),
        _one_step_plan(),
        crew_factory=SlowStepFactory(),
    )
    context = _context(timeout_seconds=0.05)
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError) as caught:
        async for event in runtime.run(context):
            events.append(event)

    assert str(caught.value) in {
        "dispatch deadline exhausted",
        "CrewAI step timed out: step=final actor=writer",
    }
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)
    assert events[-1].kind is EventKind.RUNTIME_FAILED


async def test_final_step_prompt_uses_review_packet_for_source_artifacts() -> None:
    original_text = "large upstream source " * 2_000
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="writer",
        content={"text": original_text},
    )
    factory = CapturingFactory()
    runtime = CrewDispatchRuntime(
        UnusedGateway(),
        _one_step_plan(),
        crew_factory=factory,
    )

    events = [
        event
        async for event in runtime.run(_context(artifacts=(artifact,)))
        if event.kind is EventKind.STEP_COMPLETED
    ]

    assert events
    prompt = factory.generation.prompts[0]
    assert "artifact_review_packet" in prompt
    assert "large upstream source large upstream source" in prompt
    assert original_text not in prompt
    assert '"content"' not in prompt


async def test_reviewer_prompt_uses_review_packet_for_candidate_artifact() -> None:
    gateway = LargeCandidateGateway()
    factory = CapturingFactory()
    runtime = CrewDispatchRuntime(
        gateway,
        _reviewed_plan(),
        crew_factory=factory,
    )

    events = [event async for event in runtime.run(_context())]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    reviewer_prompt = next(prompt for prompt in factory.generation.prompts if "REVIEWER" in prompt)
    assert "artifact_review_packet" in reviewer_prompt
    assert "review candidate body review candidate body" in reviewer_prompt
    assert gateway.large_text not in reviewer_prompt
    assert '"content"' not in reviewer_prompt


async def test_reviewer_timeout_is_recorded_and_dispatch_continues() -> None:
    runtime = CrewDispatchRuntime(
        UnusedGateway(),
        _reviewed_plan(),
        crew_factory=ReviewerTimeoutFactory(),
    )

    events = [event async for event in runtime.run(_context())]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    review = next(event for event in events if event.kind is EventKind.REVIEW_COMPLETED)
    assert review.payload["verdict"] == "approve"
    assert review.payload["review_status"] == "timeout_skipped"
    assert review.payload["error_code"] == "crew.step_timeout"
    assert review.payload["step_id"] == "draft.review"
    assert review.payload["actor"] == "critic"


async def test_reviewer_timeout_retries_before_skip() -> None:
    factory = ReviewerTimeoutOnceFactory()
    runtime = CrewDispatchRuntime(
        ReviewAwareGateway(),
        _reviewed_plan_with_retry_budget(),
        crew_factory=factory,
    )

    events = [event async for event in runtime.run(_context())]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert factory.generation.review_calls == 2
    retry = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retry.actor == "critic"
    assert retry.reason == "reviewer execution failed; retrying review"
    assert retry.payload["review_attempt"] == 2
    review = next(event for event in events if event.kind is EventKind.REVIEW_COMPLETED)
    assert review.payload["verdict"] == "approve"
    assert "review_status" not in review.payload


async def test_reviewer_invalid_json_retries_with_optimized_prompt_before_skip() -> None:
    factory = CapturingFactory()
    runtime = CrewDispatchRuntime(
        ReviewAwareGateway(("not json", '{"verdict":"approve"}')),
        _reviewed_plan_with_retry_budget(),
        crew_factory=factory,
    )

    events = [event async for event in runtime.run(_context())]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    reviewer_prompts = [prompt for prompt in factory.generation.prompts if "REVIEWER" in prompt]
    assert len(reviewer_prompts) == 2
    assert "Previous reviewer failure" in reviewer_prompts[1]
    assert "Return strict JSON only" in reviewer_prompts[1]
    assert len(reviewer_prompts[1].encode("utf-8")) < len(reviewer_prompts[0].encode("utf-8"))
    retry = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retry.actor == "critic"
    assert retry.payload["strategy"] == "optimized_retry"
    review = next(event for event in events if event.kind is EventKind.REVIEW_COMPLETED)
    assert review.payload["verdict"] == "approve"


def test_artifact_prompt_payload_truncates_large_text_without_mutating_artifact() -> None:
    original_text = "长文本" * 1_000
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="writer",
        content={"text": original_text},
    )

    payload = _artifact_prompt_payload(artifact, max_text_bytes=256)

    content = payload["content"]
    assert isinstance(content, dict)
    text = content["text"]
    assert isinstance(text, str)
    assert len(text.encode("utf-8")) <= 256
    assert "[truncated:" in text
    assert artifact.content["text"] == original_text


def test_final_synthesis_payload_uses_smaller_summary_without_mutating_artifact() -> None:
    original_text = "final synthesis source " * 2_000
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="planner",
        content={"text": original_text},
    )

    payload = _artifact_final_synthesis_payload(artifact)

    content = payload["content"]
    assert isinstance(content, dict)
    text = content["text"]
    assert isinstance(text, str)
    assert len(text.encode("utf-8")) <= 2_048
    assert "[truncated:" in text
    assert payload["synthesis_input"] == {
        "mode": "summary",
        "note": "Full artifact is stored separately; this final synthesis input is bounded to keep production model calls reliable.",
    }
    assert artifact.content["text"] == original_text


def test_artifact_review_packet_payload_uses_bounded_preview_without_full_text() -> None:
    original_text = "review source " * 2_000
    source_id = str(uuid4())
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="writer",
        content={"text": original_text, "risk": "low"},
        source_ids=(source_id,),
    )

    payload = _artifact_review_packet_payload(artifact)

    assert "content" not in payload
    packet = payload["artifact_review_packet"]
    assert isinstance(packet, dict)
    assert packet["producer"] == "writer"
    assert packet["type"] == "text"
    assert packet["source_ids"] == [source_id]
    assert packet["content_keys"] == ["risk", "text"]
    assert isinstance(packet["preview"], str)
    assert len(packet["preview"].encode("utf-8")) <= 1_200
    assert packet["preview"] != original_text
    assert "[truncated:" in packet["preview"]
    assert artifact.content["text"] == original_text
