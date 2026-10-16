"""Approved repair guidance must not rewrite pre-approval model requests."""

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion, GatewayRejectedOutput
from agent_hub.models.types import ModelRequest, ModelResponse, RejectedOutputEvidence, TokenUsage
from agent_hub.runs.repository import _retained_self_repair_contexts
from agent_hub.runs.self_repair import repair_context_from_proposal
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    _workspace_delivery_bundle_response,
    _workspace_delivery_sequence_context,
    _workspace_delivery_sequence_runtime,
)
from tests.unit.runtime.crew.test_meaningful_workspace_progress import FINAL, TOOLS, observe, write
from tests.unit.runtime.crew.test_workspace_read_tool_contract import (
    MissingPathHarness,
    wrong_read,
)


def runtime(gateway: WorkspaceDeliverySequenceGateway, harness: MissingPathHarness,
            repository: InMemoryArtifactRepository, *,
            factory: adapter.CrewObjectFactory | None = None) -> adapter.CrewDispatchRuntime:
    result = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository, tools=TOOLS)
    planners = tuple(AgentSpec(id=f"planner{i}", role=f"Planner {i}", goal="Plan one component",
                              logical_model="general", output_schema={"summary": "string"}) for i in range(6))
    steps = tuple(DispatchStep(id=f"plan{i}", agent=agent.id, task=f"Plan component {i}", token_budget=5_000)
                  for i, agent in enumerate(planners))
    base = result._plan
    result._plan = DispatchPlan(agents=(*planners, *base.agents),
        steps=(*steps, base.steps[0].model_copy(update={"depends_on": tuple(step.id for step in steps),
                                                      "token_budget": 50_000})),
        allowed_tools=base.allowed_tools, total_token_budget=100_000)
    if factory is not None:
        result._factory = factory
    return result


async def saved_failure(*, factory: adapter.CrewObjectFactory | None = None,
                        already_accepted: bool = False, minimal_prefix: bool = False) -> tuple[
                            RuntimeCheckpoint, TaskContext, InMemoryArtifactRepository, MissingPathHarness]:
    repository = InMemoryArtifactRepository()
    harness = MissingPathHarness("exception")
    responses = (ModelResponse(text='{"summary":"Component plan"}', tool_calls=(), usage=TokenUsage(1, 1, 2)),) * 6
    implementation = ((wrong_read(with_list=True),) if minimal_prefix else
                      (write(), observe("workspace.read"), wrong_read(with_list=True)))
    gateway = WorkspaceDeliverySequenceGateway((*responses, *implementation))
    old = runtime(gateway, harness, repository, factory=factory)
    context = _workspace_delivery_sequence_context().model_copy(update={"token_budget": 100_000})
    if already_accepted:
        context = accepted_context(context, 1)
    stream = old.run(context)
    assert isinstance(stream, adapter.CrewRunStream)
    stream._state.builtin_tool_input_contract = False
    correction = adapter._is_correctable_tool_argument_failure
    with (patch.object(adapter, "_is_correctable_tool_argument_failure", side_effect=lambda name, reason:
                       False if reason == "workspace reader requires path" else correction(name, reason)),
          pytest.raises(adapter.RuntimeExecutionError, match="capability execution failed")):
        async for _ in stream:
            pass
    checkpoint = await old.save_checkpoint()
    assert len(checkpoint.state["completed"]) == 6  # type: ignore[arg-type]
    assert len(gateway.requests) == (7 if minimal_prefix else 9)
    sequence = checkpoint.state["next_sequence"]
    assert isinstance(sequence, int)
    context = context.model_copy(update={"checkpoint": checkpoint})
    if not already_accepted:
        context = accepted_context(context, sequence + 1)
    return checkpoint, context, repository, harness


def accepted_context(context: TaskContext, sequence: int) -> TaskContext:
    return context.model_copy(update={"routing_decision": {
        **context.routing_decision, "source": "self_repair", "self_repair_accepted": True,
        "self_repair_source_run_id": str(context.run_id), "self_repair_source_event_sequence": sequence,
        "self_repair_recovery_baseline_sequence": sequence + 1,
        "self_repair_attempt": 1, "self_repair_max_attempts": 1,
        "self_repair_context": repair_context_from_proposal({"schema_version": 1, "source": "self_repair", "source_run_id": str(context.run_id),
            "source_event_sequence": sequence, "failure_kind": "step_failure", "attempt": 1, "max_attempts": 1,
            "repair_action": "draft_repair_proposal", "instruction": "Correct the builtin read path, then finish once.",
            "recovery_strategy": "repair_plugin_endpoint_or_adapter_and_retry", "requires_approval": True,
            "automatic_execution": False}),
    }})


@pytest.mark.asyncio
@pytest.mark.parametrize("framework", ("fixed", "real", "already_accepted"))
async def test_accepted_repair_reuses_multiple_cached_rounds_then_guides_new_request(
    tmp_path: Path, framework: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("OTEL_SDK_DISABLED", "CREWAI_DISABLE_TELEMETRY", "CREWAI_DISABLE_TRACKING", "CREWAI_TESTING"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "false")
    factory = adapter.CrewAIObjectFactory(storage_dir=tmp_path) if framework == "real" else None
    checkpoint, context, repository, harness = await saved_failure(
        factory=factory, already_accepted=framework == "already_accepted")
    before = checkpoint.to_payload()
    success_calls = tuple(call for call in harness.calls if call.tool_name != "workspace.read" or "path" in call.arguments)
    gateway = WorkspaceDeliverySequenceGateway((observe("workspace.read"), _workspace_delivery_bundle_response(), FINAL))
    restored = runtime(gateway, harness, repository, factory=factory)
    await restored.restore_checkpoint(checkpoint)
    events = [event async for event in restored.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 3
    assert all("SELF_REPAIR_CONTEXT" in "\n".join(str(message.content) for message in request.messages)
               and "orchestration_repair" in "\n".join(str(message.content) for message in request.messages)
               for request in gateway.requests)
    assert "workspace_read_path_required" in "\n".join(str(message.content) for message in gateway.requests[0].messages)
    assert sum(call.tool_name == "workspace.list" for call in harness.calls) == 1
    assert harness.calls[:len(success_calls)] == list(success_calls)
    assert all(sum(call.call_id == old.call_id for call in harness.calls) == 1 for old in success_calls)
    assert checkpoint.to_payload() == before
    retained = await restored.save_checkpoint()
    old_models = checkpoint.state["models"]
    new_models = retained.state["models"]
    assert isinstance(old_models, Mapping) and isinstance(new_models, Mapping)
    assert all(new_models[key] == value for key, value in old_models.items())
    old_tools, new_tools = checkpoint.state["tools"], retained.state["tools"]
    assert isinstance(old_tools, Mapping) and isinstance(new_tools, Mapping)
    assert all(new_tools[key] == value for key, value in old_tools.items()
               if isinstance(value, Mapping) and value["status"] == "succeeded")
    assert retained.state["usage"] == {"tokens": 24, "cost_usd": "0"}
    assert [event.step_id for event in events if event.kind is EventKind.STEP_STARTED] == ["implementer_step"]
    cached = runtime(WorkspaceDeliverySequenceGateway(()), harness, repository, factory=factory)
    await cached.restore_checkpoint(retained)
    done = [event async for event in cached.run(context.model_copy(update={"checkpoint": retained}))]
    assert done[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("running", "prepared"))
async def test_accepted_repair_never_resends_an_unknown_running_model(status: str) -> None:
    paused, ready = asyncio.Event(), asyncio.Event()
    responses = (ModelResponse(text='{"summary":"Component plan"}', tool_calls=(), usage=TokenUsage(1, 1, 2)),) * 6

    class PausingGateway(WorkspaceDeliverySequenceGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) == len(self.responses):
                paused.set()
                await asyncio.Event().wait()
                raise AssertionError("unconfirmed request must not complete")
            return await super().complete_with_context(request)

    repository, harness = InMemoryArtifactRepository(), MissingPathHarness("exception")
    gateway = PausingGateway((*responses, write(), observe("workspace.read")))
    old = runtime(gateway, harness, repository)
    context = _workspace_delivery_sequence_context().model_copy(update={"token_budget": 100_000})
    stream = old.run(context)
    assert isinstance(stream, adapter.CrewRunStream)
    stream._state.builtin_tool_input_contract = False
    snapshots: list[RuntimeCheckpoint] = []

    async def consume() -> None:
        async for event in stream:
            if event.checkpoint is not None:
                models = event.checkpoint.state.get("models")
                if isinstance(models, Mapping) and len(models) == 9 and any(
                    isinstance(row, Mapping) and row.get("status") == status for row in models.values()
                ):
                    snapshots.append(event.checkpoint)
                    ready.set()

    consumer = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(5):
            await paused.wait()
            await ready.wait()
    finally:
        await old.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    checkpoint = snapshots[-1]
    before, calls = checkpoint.to_payload(), len(harness.calls)
    sequence = checkpoint.state["next_sequence"]
    assert isinstance(sequence, int)
    context = accepted_context(context, sequence + 1).model_copy(update={"checkpoint": checkpoint})
    fresh = WorkspaceDeliverySequenceGateway(())
    restored = runtime(fresh, harness, repository)
    await restored.restore_checkpoint(checkpoint)
    expected = "model outcome requires confirmation" if status == "running" else "model request changed after checkpoint"
    with pytest.raises(adapter.RuntimeExecutionError, match=expected):
        async for _ in restored.run(context):
            pass
    assert not fresh.requests and len(harness.calls) == calls
    assert checkpoint.to_payload() == before
    if status == "running":
        retained = await restored.save_checkpoint()
        assert retained.state["usage"] == checkpoint.state["usage"]


@pytest.mark.asyncio
@pytest.mark.parametrize("framework", ("fixed", "real", "foreign"))
async def test_second_approved_repair_reconstructs_prior_context_only_from_bound_history(
    tmp_path: Path, framework: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("OTEL_SDK_DISABLED", "CREWAI_DISABLE_TELEMETRY", "CREWAI_DISABLE_TRACKING", "CREWAI_TESTING"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "false")
    factory = adapter.CrewAIObjectFactory(storage_dir=tmp_path) if framework == "real" else None
    checkpoint, first_context, repository, harness = await saved_failure(factory=factory, already_accepted=True)
    before = checkpoint.to_payload()
    sequence = checkpoint.state["next_sequence"]
    assert isinstance(sequence, int)
    first_decision: dict[str, object] = {key: adapter._mutable_json(value)
                                       for key, value in first_context.routing_decision.items()}
    history = cast(list[dict[str, JsonValue]], _retained_self_repair_contexts(
        first_decision, run_id=first_context.run_id, next_attempt=2, next_sequence=sequence + 1))
    if framework == "foreign":
        history[0]["source_run_id"] = str(uuid4())
    second = accepted_context(first_context, sequence + 1)
    decision = dict(second.routing_decision)
    scope = decision["self_repair_context"]
    assert isinstance(scope, Mapping)
    decision.update({"self_repair_attempt": 2, "self_repair_max_attempts": 3,
        "self_repair_context_history": tuple(history),
        "self_repair_context": {**scope, "attempt": 2, "max_attempts": 3,
            "instruction": "Second approval: inspect the required path and finish once."}})
    second = second.model_copy(update={"routing_decision": decision})
    old_calls = len(harness.calls)
    gateway = WorkspaceDeliverySequenceGateway((observe("workspace.read"), _workspace_delivery_bundle_response(), FINAL))
    restored = runtime(gateway, harness, repository, factory=factory)
    await restored.restore_checkpoint(checkpoint)
    if framework == "foreign":
        with pytest.raises(adapter.RuntimeExecutionError, match="self repair context history is invalid"):
            async for _ in restored.run(second):
                pass
        assert not gateway.requests and len(harness.calls) == old_calls
    else:
        events = [event async for event in restored.run(second)]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED and len(gateway.requests) == 3
        assert all("Second approval" in "\n".join(str(message.content) for message in request.messages)
                   for request in gateway.requests)
        assert [event.step_id for event in events if event.kind is EventKind.STEP_STARTED] == ["implementer_step"]
        assert sum(call.tool_name == "workspace.write_text" for call in harness.calls) == 1
        assert sum(call.tool_name == "workspace.list" for call in harness.calls) == 1
        retained = await restored.save_checkpoint()
        old_models, new_models = checkpoint.state["models"], retained.state["models"]
        assert isinstance(old_models, Mapping) and isinstance(new_models, Mapping)
        assert all(new_models[key] == value for key, value in old_models.items())
        assert retained.state["usage"] == {"tokens": 24, "cost_usd": "0"}
    assert checkpoint.to_payload() == before


@pytest.mark.asyncio
async def test_known_rejection_cannot_reenter_internal_correction_with_old_repair_guidance() -> None:
    responses = (ModelResponse(text='{"summary":"Component plan"}', tool_calls=(), usage=TokenUsage(1, 1, 2)),) * 6

    class RejectingGateway(WorkspaceDeliverySequenceGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) == 8:
                self.requests.append(request)
                raise GatewayRejectedOutput(evidence=RejectedOutputEvidence(
                    final_text="{invalid", usage=TokenUsage(1, 1, 2), usage_status="known",
                    status="completed", reason="invalid_json"), deployment_id="primary",
                    logical_model=request.logical_model, provider_id="deepseek", provider_model="deepseek/chat")
            return await super().complete_with_context(request)

    repository, harness = InMemoryArtifactRepository(), MissingPathHarness("exception")
    gateway = RejectingGateway((*responses, write(), observe("workspace.read")))
    old = runtime(gateway, harness, repository)
    context = _workspace_delivery_sequence_context().model_copy(update={"token_budget": 100_000})
    with pytest.raises(adapter.RuntimeExecutionError, match="structured output invalid"):
        async for _ in old.run(context):
            pass
    checkpoint = await old.save_checkpoint()
    models = checkpoint.state["models"]
    assert isinstance(models, Mapping)
    assert any(isinstance(row, Mapping) and row.get("status") == "rejected"
               and row.get("failure_reason") == "structured output rejected" for row in models.values())
    sequence = checkpoint.state["next_sequence"]
    assert isinstance(sequence, int)
    context = accepted_context(context, sequence + 1).model_copy(update={"checkpoint": checkpoint})
    before, calls = checkpoint.to_payload(), len(harness.calls)
    fresh = WorkspaceDeliverySequenceGateway(())
    restored = runtime(fresh, harness, repository)
    await restored.restore_checkpoint(checkpoint)
    with pytest.raises(adapter.RuntimeExecutionError, match="model request changed after checkpoint"):
        async for _ in restored.run(context):
            pass
    assert not fresh.requests and len(harness.calls) == calls
    assert checkpoint.to_payload() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("framework", ("fixed", "real"))
async def test_orchestration_noop_reuses_successful_list_before_failed_read_correction(
    tmp_path: Path, framework: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("OTEL_SDK_DISABLED", "CREWAI_DISABLE_TELEMETRY", "CREWAI_DISABLE_TRACKING", "CREWAI_TESTING"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "false")
    factory = adapter.CrewAIObjectFactory(storage_dir=tmp_path) if framework == "real" else None
    checkpoint, context, repository, harness = await saved_failure(factory=factory, minimal_prefix=True)
    scope = context.routing_decision["self_repair_context"]
    assert isinstance(scope, Mapping)
    context = context.model_copy(update={"routing_decision": {**context.routing_decision,
        "self_repair_context": {**scope, "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
            "orchestration_recovery_hint": "retry_blocked_contract_chain",
            "blocked_contract_ids": ["plan0-to-implementer_step"]}}})
    before = checkpoint.to_payload()
    reached = asyncio.Event()
    denied: list[str] = []
    requests: list[ModelRequest] = []
    original_invoke = harness.invoke

    async def guard(tenant_id: UUID, request: HarnessToolCallRequest, *,
                    user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        if request.tool_name != "workspace.read" or "path" in request.arguments:
            denied.append(request.tool_name)
            reached.set()
            raise RuntimeCapabilityError("successful capability must not execute again")
        return await original_invoke(tenant_id, request, user_id=user_id, role=role)

    monkeypatch.setattr(harness, "invoke", guard)

    class BoundaryGateway(WorkspaceDeliverySequenceGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            requests.append(request)
            reached.set()
            await asyncio.Event().wait()
            raise AssertionError("new model boundary must not complete")

    restored = runtime(BoundaryGateway(()), harness, repository, factory=factory)
    await restored.restore_checkpoint(checkpoint)
    events: list[RunEvent] = []
    step_started = asyncio.Event()

    async def consume() -> None:
        async for event in restored.run(context):
            events.append(event)
            if event.kind is EventKind.STEP_STARTED:
                step_started.set()

    consumer = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(10):
            await reached.wait()
        assert not denied, f"successful tools replayed: {denied}"
        async with asyncio.timeout(10):
            await step_started.wait()
        assert len(requests) == 1
        text = "\n".join(str(message.content) for message in requests[0].messages)
        assert "CAPABILITY_ARGUMENT_CORRECTION" in text and "workspace_read_path_required" in text
        assert sum(call.tool_name == "workspace.list" for call in harness.calls) == 1
        assert sum(call.tool_name == "workspace.read" for call in harness.calls) == 2
        retained = await restored.save_checkpoint()
        models, retained_models = checkpoint.state["models"], retained.state["models"]
        assert isinstance(models, Mapping) and isinstance(retained_models, Mapping)
        assert all(retained_models[key] == value for key, value in models.items())
        tools = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["tools"])
        retained_tools = cast(Mapping[str, Mapping[str, JsonValue]], retained.state["tools"])
        assert all(retained_tools[key] == row for key, row in tools.items() if row["status"] == "succeeded")
        assert retained.state["completed"] == checkpoint.state["completed"]
        assert retained.state["usage"] == checkpoint.state["usage"]
        assert retained.state["repair_reopened_contract_ids"] == ()
        assert [event.step_id for event in events if event.kind is EventKind.STEP_STARTED] == ["implementer_step"]
        assert checkpoint.to_payload() == before
    finally:
        await restored.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("unapproved", "foreign_run", "changed_request", "foreign_context", "source_event"))
async def test_unbound_repair_or_other_request_change_cannot_reuse_cached_sha(change: str) -> None:
    checkpoint, context, repository, harness = await saved_failure()
    before = checkpoint.to_payload()
    calls = len(harness.calls)
    decision: dict[str, JsonValue] = dict(context.routing_decision)
    if change == "unapproved":
        decision["self_repair_accepted"] = False
    elif change == "foreign_run":
        decision["self_repair_source_run_id"] = str(uuid4())
    elif change in {"foreign_context", "source_event"}:
        repair = decision["self_repair_context"]
        assert isinstance(repair, Mapping)
        decision["self_repair_context"] = {**repair,
            **({"source_run_id": str(uuid4())} if change == "foreign_context" else {"source_event_sequence": 1})}
    else:
        context = context.model_copy(update={"request": "A different task must not match the old receipt"})
    context = context.model_copy(update={"routing_decision": decision})
    gateway = WorkspaceDeliverySequenceGateway(())
    restored = runtime(gateway, harness, repository)
    await restored.restore_checkpoint(checkpoint)
    with pytest.raises(adapter.RuntimeExecutionError, match="model request changed after checkpoint"):
        async for _ in restored.run(context):
            pass
    assert not gateway.requests and len(harness.calls) == calls
    assert checkpoint.to_payload() == before
