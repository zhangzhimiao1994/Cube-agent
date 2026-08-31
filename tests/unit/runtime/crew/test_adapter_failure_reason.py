from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent, TaskContext
from agent_hub.runtime.crew.adapter import (
    CapabilityOutcomeUncertain,
    CrewAgentDefinition,
    CrewDispatchRuntime,
    CrewLLMBridge,
    CrewObjectFactory,
    CrewTaskDefinition,
    ModelOutcomeUncertain,
    RuntimeExecutionError,
    _artifact_final_synthesis_payload,
    _artifact_prompt_payload,
    _artifact_review_packet_payload,
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


class RecordingGeneration:
    def __init__(self, *, reviewer_timeouts: int = 0, agent_timeouts: int = 0) -> None:
        self.reviewer_timeouts = reviewer_timeouts
        self.agent_timeouts = agent_timeouts
        self.prompts: list[tuple[str, str | None, str]] = []

    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del storage_scope
        self.prompts.append((step_id, agent_id, prompt))
        if self.reviewer_timeouts > 0 and agent_id == "reviewer":
            self.reviewer_timeouts -= 1
            raise TimeoutError
        if self.agent_timeouts > 0 and agent_id != "reviewer":
            self.agent_timeouts -= 1
            raise TimeoutError
        return await bridge.complete([{"role": "system", "content": prompt}])


class RecordingFactory(CrewObjectFactory):
    def __init__(self, generation: RecordingGeneration) -> None:
        self.generation = generation

    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> RecordingGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        return self.generation


class RoleAwareGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        text = (
            '{"verdict":"approve"}'
            if request.logical_model == "review"
            else "role output " + ("内容" * 600)
        )
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class EmptyThenRoleAwareGateway(RoleAwareGateway):
    def __init__(self, *, empty_logical_model: str) -> None:
        super().__init__()
        self.empty_logical_model = empty_logical_model
        self._empty_returned = False

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if request.logical_model == self.empty_logical_model and not self._empty_returned:
            self._empty_returned = True
            text = ""
        else:
            text = (
                '{"verdict":"approve"}'
                if request.logical_model == "review"
                else "role output " + ("内容" * 600)
            )
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class EmptyThenReviewingGateway(EmptyThenRoleAwareGateway):
    def __init__(self, *, empty_logical_model: str, reviews: tuple[str, ...]) -> None:
        super().__init__(empty_logical_model=empty_logical_model)
        self.reviews = list(reviews)

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if request.logical_model != "review":
            return await super().complete_with_context(request)
        self.requests.append(request)
        if request.logical_model == self.empty_logical_model and not self._empty_returned:
            self._empty_returned = True
            text = ""
        else:
            text = self.reviews.pop(0) if self.reviews else '{"verdict":"approve"}'
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class FailingModelGateway(RoleAwareGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        raise RuntimeError("provider detail must be redacted")


class FailingAfterToolGateway(ToolGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if self.requests:
            raise RuntimeError("provider detail must be redacted")
        self.requests.append(request)
        return GatewayCompletion(
            response=ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(id="provider-call", name="web.search", arguments={"q": "safe"}),
                ),
                usage=TokenUsage(1, 1, 2),
            ),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


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


def _reviewed_step_plan(*, reviewer_retries: int = 0) -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(id="writer", role="writer", goal="Write", logical_model="general"),
            AgentSpec(id="reviewer", role="reviewer", goal="Review", logical_model="review"),
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Synthesize",
                logical_model="general",
            ),
        ),
        steps=(
            DispatchStep(
                id="draft",
                agent="writer",
                task="Draft",
                reviewer="reviewer",
                reviewer_retries=reviewer_retries,
                token_budget=1000,
            ),
            DispatchStep(
                id="final_response",
                agent="final_synthesizer",
                task="Synthesize",
                depends_on=("draft",),
                final_synthesizer=True,
                token_budget=1000,
            ),
        ),
        total_token_budget=1000,
    )


def _dependent_final_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(id="writer", role="writer", goal="Write", logical_model="general"),
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Synthesize",
                logical_model="general",
            ),
        ),
        steps=(
            DispatchStep(id="draft", agent="writer", task="Draft", token_budget=1000),
            DispatchStep(
                id="final_response",
                agent="final_synthesizer",
                task="Synthesize",
                depends_on=("draft",),
                final_synthesizer=True,
                token_budget=1000,
            ),
        ),
        total_token_budget=1000,
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
    argument_bytes = tool_started.payload["argument_bytes"]
    assert isinstance(argument_bytes, int)
    assert argument_bytes > 0
    assert "arguments" not in tool_started.payload
    assert '"safe"' not in json.dumps(dict(tool_started.payload))
    tool_artifact = next(
        event.artifact for event in events if event.artifact and event.artifact.type == "tool_result"
    )
    tool_completed = next(event for event in events if event.kind is EventKind.TOOL_COMPLETED)
    assert tool_completed.payload["schema_version"] == 1
    assert tool_completed.payload["status"] == "succeeded"
    result_bytes = tool_completed.payload["result_bytes"]
    assert isinstance(result_bytes, int)
    assert result_bytes > 0
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
    step_failed = next(event for event in events if event.kind is EventKind.STEP_FAILED)
    assert step_failed.payload["error_code"] == "crew.step_timeout"
    assert step_failed.payload["step_id"] == "final"
    assert step_failed.payload["actor"] == "writer"


async def test_agent_timeout_compact_retries_before_failing_step() -> None:
    generation = RecordingGeneration(agent_timeouts=1)
    plan = _one_step_plan()
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    writer_prompts = [prompt for step_id, agent_id, prompt in generation.prompts if step_id == "final"]
    assert len(writer_prompts) == 2
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.actor == "writer"
    assert retrying.payload["recovery_strategy"] == "compact_retry"
    assert retrying.payload["recovery_attempt"] == 1
    assert retrying.payload["recovery_layers"] == (
        "input_compression",
        "prompt_decomposition",
        "model_fallback_marked",
        "failure_closure",
    )
    assert retrying.payload["model_fallback"] == "not_available_in_crewai_bridge"
    assert "Keep the retry concise" in writer_prompts[1]
    assert any(event.kind is EventKind.STEP_COMPLETED for event in events)
    checkpoint = await runtime.save_checkpoint()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(checkpoint)


async def test_agent_timeout_reports_recovery_closure_after_retry_exhausted() -> None:
    generation = RecordingGeneration(agent_timeouts=2)
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        _one_step_plan(),
        crew_factory=RecordingFactory(generation),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError) as caught:
        async for event in runtime.run(_context()):
            events.append(event)

    assert str(caught.value) == "CrewAI step timed out: step=final actor=writer"
    writer_prompts = [prompt for step_id, agent_id, prompt in generation.prompts if step_id == "final"]
    assert len(writer_prompts) == 2
    retry_events = [event for event in events if event.kind is EventKind.STEP_RETRYING]
    assert len(retry_events) == 1
    step_failed = next(event for event in events if event.kind is EventKind.STEP_FAILED)
    assert step_failed.payload["error_code"] == "crew.step_timeout"
    assert step_failed.payload["recovery_status"] == "failed_after_compact_retry"
    assert step_failed.payload["recovery_attempts"] == 1
    assert step_failed.payload["recovery_layers"] == (
        "input_compression",
        "prompt_decomposition",
        "model_fallback_marked",
        "failure_closure",
    )


async def test_agent_empty_model_response_compact_retries_before_completing() -> None:
    gateway = EmptyThenRoleAwareGateway(empty_logical_model="general")
    generation = RecordingGeneration()
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway,
        _one_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    assert len(gateway.requests) == 2
    writer_prompts = [prompt for step_id, agent_id, prompt in generation.prompts if step_id == "final"]
    assert len(writer_prompts) == 2
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.actor == "writer"
    assert retrying.payload["error_code"] == "model.empty_response"
    assert retrying.payload["recovery_strategy"] == "compact_retry"
    assert retrying.payload["recovery_attempt"] == 1
    assert retrying.payload["recovery_layers"] == (
        "input_compression",
        "prompt_decomposition",
        "model_fallback_marked",
        "failure_closure",
    )
    assert "Keep the retry concise" in writer_prompts[1]
    assert any(event.kind is EventKind.STEP_COMPLETED for event in events)
    checkpoint = await runtime.save_checkpoint()
    model_states = checkpoint.state["models"]
    assert isinstance(model_states, Mapping)
    assert {state["status"] for state in model_states.values() if isinstance(state, Mapping)} == {
        "failed",
        "succeeded",
    }
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        _one_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(checkpoint)
    resumed_events = [event async for event in restored.run(_context(checkpoint=checkpoint))]
    assert [event.kind for event in resumed_events] == [EventKind.RUNTIME_COMPLETED]


async def test_failed_model_checkpoint_resumes_through_generic_compact_retry() -> None:
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        EmptyThenRoleAwareGateway(empty_logical_model="general"),
        _one_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )

    checkpoints = [
        event.checkpoint
        async for event in runtime.run(_context())
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    ]
    failed_checkpoint = None
    for checkpoint in checkpoints:
        model_states = checkpoint.state["models"]
        assert isinstance(model_states, Mapping)
        if {
            state["status"] for state in model_states.values() if isinstance(state, Mapping)
        } == {"failed"}:
            failed_checkpoint = checkpoint
            break
    assert failed_checkpoint is not None
    restored_generation = RecordingGeneration()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        _one_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(restored_generation),
    )
    await restored.restore_checkpoint(failed_checkpoint)

    events = [event async for event in restored.run(_context(checkpoint=failed_checkpoint))]

    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.actor == "writer"
    assert retrying.payload["error_code"] == "model.empty_response"
    assert retrying.payload["recovery_strategy"] == "compact_retry"
    assert len(restored_generation.prompts) == 2
    assert "Keep the retry concise" in restored_generation.prompts[1][2]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_non_retryable_failed_model_checkpoint_requires_confirmation() -> None:
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        FailingModelGateway(),
        _one_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context(actor_id=uuid4(), actor_role=Role.OPERATOR)):
            events.append(event)

    def has_failed_model(event: RunEvent) -> bool:
        checkpoint = event.checkpoint
        if checkpoint is None:
            return False
        model_states = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["models"])
        return any(state["status"] == "failed" for state in model_states.values())

    failed_checkpoint = next(
        event.checkpoint
        for event in reversed(events)
        if event.kind is EventKind.CHECKPOINT_SAVED
        and has_failed_model(event)
    )
    assert failed_checkpoint is not None
    restored_gateway = RoleAwareGateway()
    restored = CrewDispatchRuntime(
        restored_gateway,
        _one_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(failed_checkpoint)

    with pytest.raises(ModelOutcomeUncertain, match="model outcome requires confirmation"):
        [event async for event in restored.run(_context(checkpoint=failed_checkpoint))]

    assert restored_gateway.requests == []


async def test_succeeded_tool_survives_non_retryable_followup_model_failure() -> None:
    repository = InMemoryArtifactRepository()
    tool_plan = _tool_plan()
    first_capabilities = FakeCapabilities()
    runtime = CrewDispatchRuntime(
        FailingAfterToolGateway(),
        tool_plan,
        capability_gateway=first_capabilities,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context(actor_id=uuid4(), actor_role=Role.OPERATOR)):
            events.append(event)

    checkpoint = await runtime.save_checkpoint()
    persisted_artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    tool_artifacts = tuple(
        artifact for artifact in persisted_artifacts if artifact.type == "tool_result"
    )
    assert len(tool_artifacts) == 1
    assert first_capabilities.calls == [("writer", "web.search")]

    second_capabilities = FakeCapabilities()
    restored_gateway = ToolGateway()
    restored = CrewDispatchRuntime(
        restored_gateway,
        tool_plan,
        capability_gateway=second_capabilities,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(checkpoint)

    with pytest.raises(ModelOutcomeUncertain, match="model outcome requires confirmation"):
        [
            event
            async for event in restored.run(
                _context(checkpoint=checkpoint, artifacts=persisted_artifacts)
            )
        ]

    assert restored_gateway.requests == []
    assert second_capabilities.calls == []


async def test_reviewer_timeout_uses_generic_recovery_before_soft_skip() -> None:
    generation = RecordingGeneration(reviewer_timeouts=1)
    plan = _reviewed_step_plan()
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    reviewer_prompts = [item for item in generation.prompts if item[1] == "reviewer"]
    assert len(reviewer_prompts) == 2
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.actor == "reviewer"
    assert retrying.payload["recovery_strategy"] == "compact_retry"
    assert retrying.payload["recovery_attempt"] == 1
    assert retrying.payload["recovery_layers"] == (
        "input_compression",
        "prompt_decomposition",
        "model_fallback_marked",
        "failure_closure",
    )
    assert retrying.payload["model_fallback"] == "not_available_in_crewai_bridge"
    assert "Keep the retry concise" in reviewer_prompts[1][2]
    review_completed = next(event for event in events if event.kind is EventKind.REVIEW_COMPLETED)
    assert review_completed.payload["verdict"] == "approve"
    assert "review_status" not in review_completed.payload
    assert "error_code" not in review_completed.payload
    assert any(event.kind is EventKind.STEP_COMPLETED for event in events)
    checkpoint = await runtime.save_checkpoint()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(checkpoint)


async def test_reviewer_empty_model_response_uses_generic_recovery_before_soft_skip() -> None:
    gateway = EmptyThenRoleAwareGateway(empty_logical_model="review")
    generation = RecordingGeneration()
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway,
        _reviewed_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    reviewer_prompts = [item for item in generation.prompts if item[1] == "reviewer"]
    assert len(reviewer_prompts) == 2
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.actor == "reviewer"
    assert retrying.payload["error_code"] == "model.empty_response"
    assert retrying.payload["recovery_strategy"] == "compact_retry"
    assert retrying.payload["recovery_attempt"] == 1
    assert "Keep the retry concise" in reviewer_prompts[1][2]
    review_completed = next(event for event in events if event.kind is EventKind.REVIEW_COMPLETED)
    assert review_completed.payload["verdict"] == "approve"
    assert any(event.kind is EventKind.STEP_COMPLETED for event in events)
    checkpoint = await runtime.save_checkpoint()
    model_states = checkpoint.state["models"]
    assert isinstance(model_states, Mapping)
    assert {state["status"] for state in model_states.values() if isinstance(state, Mapping)} == {
        "failed",
        "succeeded",
    }
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        _reviewed_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(checkpoint)
    resumed_events = [event async for event in restored.run(_context(checkpoint=checkpoint))]
    assert [event.kind for event in resumed_events] == [EventKind.RUNTIME_COMPLETED]


async def test_reviewer_revise_checkpoint_after_compact_recovery_resumes() -> None:
    gateway = EmptyThenReviewingGateway(
        empty_logical_model="review",
        reviews=('{"verdict":"revise","feedback":"tighten"}', '{"verdict":"approve"}'),
    )
    generation = RecordingGeneration()
    repository = InMemoryArtifactRepository()
    plan = _reviewed_step_plan(reviewer_retries=1)
    runtime = CrewDispatchRuntime(
        gateway,
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(generation),
    )

    checkpoints = [
        event.checkpoint
        async for event in runtime.run(_context())
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    ]
    revise_checkpoint = next(
        checkpoint for checkpoint in checkpoints if checkpoint.state["review_refs"]
    )
    restored_generation = RecordingGeneration()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(restored_generation),
    )
    await restored.restore_checkpoint(revise_checkpoint)

    events = [event async for event in restored.run(_context(checkpoint=revise_checkpoint))]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert any(item[0] == "draft" and item[1] == "writer" for item in restored_generation.prompts)


async def test_recovered_agent_attempt_can_still_survive_reviewer_revision_resume() -> None:
    gateway = EmptyThenReviewingGateway(
        empty_logical_model="general",
        reviews=('{"verdict":"revise","feedback":"tighten"}', '{"verdict":"approve"}'),
    )
    generation = RecordingGeneration()
    repository = InMemoryArtifactRepository()
    plan = _reviewed_step_plan(reviewer_retries=1)
    runtime = CrewDispatchRuntime(
        gateway,
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    assert len([event for event in events if event.kind is EventKind.STEP_RETRYING]) == 2
    checkpoint = await runtime.save_checkpoint()
    model_states = checkpoint.state["models"]
    assert isinstance(model_states, Mapping)
    draft_step_attempts = {
        state["attempt"]
        for state in model_states.values()
        if isinstance(state, Mapping)
        and state["step_id"] == "draft"
        and state["purpose"] == "step"
    }
    assert draft_step_attempts == {0, 1, 3}
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(checkpoint)
    resumed_events = [event async for event in restored.run(_context(checkpoint=checkpoint))]
    assert [event.kind for event in resumed_events] == [EventKind.RUNTIME_COMPLETED]


async def test_reviewer_timeout_retries_before_soft_skip() -> None:
    generation = RecordingGeneration(reviewer_timeouts=1)
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        _reviewed_step_plan(reviewer_retries=1),
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    reviewer_prompts = [item for item in generation.prompts if item[1] == "reviewer"]
    assert len(reviewer_prompts) == 2
    review_completed = next(event for event in events if event.kind is EventKind.REVIEW_COMPLETED)
    assert review_completed.payload["verdict"] == "approve"
    assert "review_status" not in review_completed.payload
    assert "error_code" not in review_completed.payload


async def test_reviewer_timeout_skips_after_retry_budget_is_exhausted() -> None:
    generation = RecordingGeneration(reviewer_timeouts=2)
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        _reviewed_step_plan(),
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    reviewer_prompts = [item for item in generation.prompts if item[1] == "reviewer"]
    assert len(reviewer_prompts) == 2
    review_completed = next(event for event in events if event.kind is EventKind.REVIEW_COMPLETED)
    assert review_completed.payload["review_status"] == "timeout_skipped"
    assert review_completed.payload["error_code"] == "crew.step_timeout"
    assert review_completed.payload["recovery_status"] == "failed_after_compact_retry"
    assert review_completed.payload["recovery_attempts"] == 1
    assert review_completed.payload["recovery_layers"] == (
        "input_compression",
        "prompt_decomposition",
        "model_fallback_marked",
        "failure_closure",
    )
    assert review_completed.payload["model_fallback"] == "not_available_in_crewai_bridge"
    assert any(event.kind is EventKind.STEP_COMPLETED for event in events)


async def test_dependent_final_step_receives_bounded_review_packets() -> None:
    generation = RecordingGeneration()
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        _dependent_final_plan(),
        crew_factory=RecordingFactory(generation),
    )

    await _collect(runtime)

    final_prompt = next(prompt for step_id, _, prompt in generation.prompts if step_id == "final_response")
    assert '"artifact_review_packet"' in final_prompt
    assert '"content_keys"' in final_prompt
    assert '"content":{"text"' not in final_prompt


def test_artifact_review_packet_payload_exposes_bounded_preview_without_full_content() -> None:
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="writer",
        content={"text": "long text " * 1_000, "metadata": {"unsafe": "kept out of prompt"}},
        source_ids=(str(uuid4()),),
    )

    payload = _artifact_review_packet_payload(artifact)

    packet = payload["artifact_review_packet"]
    assert isinstance(packet, Mapping)
    assert packet["id"] == str(artifact.id)
    assert packet["type"] == "text"
    assert packet["producer"] == "writer"
    assert isinstance(packet["source_ids"], tuple)
    assert len(packet["source_ids"]) == 1
    assert packet["content_sha256"] == artifact.content_sha256
    assert packet["content_keys"] == ("metadata", "text")
    preview = packet["preview"]
    assert isinstance(preview, str)
    assert len(preview.encode("utf-8")) <= 1200
    assert "metadata" not in packet


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
