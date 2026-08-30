from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent, TaskContext
from agent_hub.runtime.crew.adapter import (
    CapabilityOutcomeUncertain,
    CrewAgentDefinition,
    CrewDispatchRuntime,
    CrewLLMBridge,
    CrewObjectFactory,
    CrewTaskDefinition,
    RuntimeExecutionError,
    _artifact_final_synthesis_payload,
    _artifact_prompt_payload,
    _tool_sandbox,
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


class ToolGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        response = (
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(id="provider-call", name="web_search", arguments={"q": "safe"}),
                ),
                usage=TokenUsage(1, 1, 2),
            )
            if len(self.requests) == 1
            else ModelResponse(text="tool-grounded answer", usage=TokenUsage(1, 1, 2))
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class FakeCapabilities:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        del tenant_id, run_id, arguments, idempotency_key
        self.calls.append((actor, name))
        return {"items": ("legacy result",)}

    def is_replay_safe(self, name: str) -> bool:
        return name == "web.search"


class RaisingCapabilities(FakeCapabilities):
    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        del tenant_id, run_id, actor, name, arguments, idempotency_key
        raise RuntimeError("commit status unavailable")

    def is_replay_safe(self, name: str) -> bool:
        del name
        return False


class RecordingHarnessToolGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, HarnessToolCallRequest]] = []

    async def invoke(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult:
        assert user_id is None
        assert role is None
        self.calls.append((tenant_id, request))
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="succeeded",
            payload={"items": ("harness result",)},
        )


class FailingHarnessToolGateway:
    def __init__(self) -> None:
        self.calls: list[HarnessToolCallRequest] = []

    async def invoke(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult:
        del tenant_id, user_id, role
        self.calls.append(request)
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="failed",
            payload={},
            failure_reason="tool unavailable",
        )


class IdentityRecordingHarnessToolGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, HarnessToolCallRequest, UUID | None, Role | None]] = []

    async def invoke(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult:
        self.calls.append((tenant_id, request, user_id, role))
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="succeeded",
            payload={"items": ("identity result",)},
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


class FastGeneration:
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
        return await bridge.complete([{"role": "system", "content": prompt}])


class FastFactory(CrewObjectFactory):
    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> FastGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        return FastGeneration()


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


def _tool_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
    )


def _context(**changes: object) -> TaskContext:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "tenant_id": TENANT_ID,
        "mode": TaskMode.DISPATCH,
        "request": "Write a short answer",
        "token_budget": 1000,
    }
    values.update(changes)
    return TaskContext.model_validate(values, strict=True)


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


async def test_tool_calls_cross_the_harness_tool_gateway_envelope() -> None:
    capabilities = FakeCapabilities()
    harness = RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=capabilities,
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = await _collect(runtime)

    assert capabilities.calls == []
    assert len(harness.calls) == 1
    tenant_id, request = harness.calls[0]
    assert tenant_id == TENANT_ID
    assert request.run_id == RUN_ID
    assert request.actor == "writer"
    assert request.tool_name == "web.search"
    assert request.arguments == {"q": "safe"}
    assert request.approval_required is False
    assert request.sandbox == "restricted"
    assert request.call_id == f"call-{request.idempotency_key[:32]}"
    tool_started = next(event for event in events if event.kind is EventKind.TOOL_STARTED)
    assert tool_started.payload["schema_version"] == 1
    assert tool_started.payload["status"] == "running"
    assert tool_started.payload["operation_kind"] == "generic"
    assert tool_started.payload["sandbox"] == "restricted"
    assert tool_started.payload["argument_keys"] == ("q",)
    assert tool_started.payload["argument_key_count"] == 1
    assert tool_started.payload["argument_bytes"] > 0
    assert "arguments" not in tool_started.payload
    assert '"safe"' not in json.dumps(dict(tool_started.payload))
    tool_artifact = next(
        event.artifact for event in events if event.artifact and event.artifact.type == "tool_result"
    )
    tool_completed = next(event for event in events if event.kind is EventKind.TOOL_COMPLETED)
    assert tool_completed.payload["schema_version"] == 1
    assert tool_completed.payload["status"] == "succeeded"
    assert tool_completed.payload["result_bytes"] > 0
    assert tool_completed.payload["artifact_id"] == str(tool_artifact.id)
    assert "result" not in tool_completed.payload
    assert "harness result" not in json.dumps(dict(tool_completed.payload))
    result = tool_artifact.content["result"]
    assert isinstance(result, Mapping)
    assert result["items"] == ("harness result",)


async def test_tool_calls_forward_trusted_actor_identity_to_harness_gateway() -> None:
    user_id = UUID("00000000-0000-4000-8000-000000000003")
    harness = IdentityRecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(
            _context(actor_id=user_id, actor_role=Role.OPERATOR)
        )
    ]

    assert len(harness.calls) == 1
    tenant_id, request, recorded_user_id, recorded_role = harness.calls[0]
    assert tenant_id == TENANT_ID
    assert request.actor == "writer"
    assert recorded_user_id == user_id
    assert recorded_role is Role.OPERATOR
    result = next(
        event.artifact.content["result"]
        for event in events
        if event.artifact and event.artifact.type == "tool_result"
    )
    assert result == {"items": ("identity result",)}


async def test_failed_harness_tool_result_records_failed_not_uncertain() -> None:
    capabilities = FakeCapabilities()
    harness = FailingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=capabilities,
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for event in runtime.run(_context()):
            events.append(event)

    assert capabilities.calls == []
    assert len(harness.calls) == 1
    assert [event.reason for event in events if event.kind is EventKind.TOOL_FAILED] == [
        "tool unavailable"
    ]
    failed_event = next(event for event in events if event.kind is EventKind.TOOL_FAILED)
    assert failed_event.payload["schema_version"] == 1
    assert failed_event.payload["status"] == "failed"
    assert failed_event.payload["failure_kind"] == "capability_failed"
    assert failed_event.payload["argument_keys"] == ("q",)
    assert "arguments" not in failed_event.payload
    assert '"safe"' not in json.dumps(dict(failed_event.payload))
    checkpoint = await runtime.save_checkpoint()
    tool_states = checkpoint.state["tools"]
    assert isinstance(tool_states, Mapping)
    state = next(iter(tool_states.values()))
    assert isinstance(state, Mapping)
    assert state["status"] == "failed"
    assert state["artifact_id"] is None
    assert state["sha256"] is None
    resumed = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=capabilities,
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )
    await resumed.restore_checkpoint(checkpoint)


async def test_default_harness_wrapper_fails_closed_without_actor_identity() -> None:
    capabilities = FakeCapabilities()
    runtime = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=capabilities,
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for event in runtime.run(_context()):
            events.append(event)

    assert capabilities.calls == []
    assert [event.reason for event in events if event.kind is EventKind.TOOL_FAILED] == [
        "capability identity unavailable"
    ]


async def test_default_harness_wrapper_preserves_backend_uncertainty() -> None:
    runtime = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=RaisingCapabilities(),
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(CapabilityOutcomeUncertain):
        async for event in runtime.run(
            _context(actor_id=uuid4(), actor_role=Role.OPERATOR)
        ):
            events.append(event)

    assert [event.reason for event in events if event.kind is EventKind.TOOL_FAILED] == [
        "capability execution failed"
    ]
    checkpoint = await runtime.save_checkpoint()
    tool_states = checkpoint.state["tools"]
    assert isinstance(tool_states, Mapping)
    state = next(iter(tool_states.values()))
    assert isinstance(state, Mapping)
    assert state["status"] == "uncertain"


def test_tool_sandbox_accepts_dot_and_underscore_builtin_names() -> None:
    assert _tool_sandbox("workspace.read") == "read_only"
    assert _tool_sandbox("workspace_read") == "read_only"
    assert _tool_sandbox("calculator.evaluate") == "none"
    assert _tool_sandbox("calculator_evaluate") == "none"
    assert _tool_sandbox("web.search") == "restricted"


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
