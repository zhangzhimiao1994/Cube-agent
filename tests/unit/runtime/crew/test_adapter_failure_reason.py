from __future__ import annotations

from decimal import Decimal
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
            provider_model="deepseek-v4-flash",
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


def _one_step_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(id="writer", role="writer", goal="Write", logical_model="general"),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        total_token_budget=100,
    )


def _context() -> TaskContext:
    return TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="Write a short answer",
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
    assert any(
        event.kind is EventKind.STEP_FAILED and event.reason == expected for event in events
    )


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
    assert any(
        event.kind is EventKind.STEP_FAILED and event.reason == expected for event in events
    )


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
