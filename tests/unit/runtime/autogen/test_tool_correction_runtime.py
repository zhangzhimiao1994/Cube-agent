"""Real AutoGen correction reaches delivery without weakening cold replay fences."""

from collections.abc import AsyncGenerator, Mapping
from dataclasses import replace
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest

from agent_hub.models.capacity import CapacityWaitTimeout
from agent_hub.models.gateway import GatewayCompletion, GatewayRejectedOutput
from agent_hub.models.types import ModelRequest, ModelResponse, RejectedOutputEvidence, TokenUsage
from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionPlan,
    _DiscussionDurability,
)
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent, TaskContext
from tests.unit.runtime.autogen.test_engineering_actions import action_context, inspection
from tests.unit.runtime.autogen.test_engineering_completion import Capabilities, plan
from tests.unit.runtime.autogen.test_final_delivery_runtime import REPORT
from tests.unit.runtime.autogen.test_native_action_runtime import ScopedModel

POLICY = "engineering_tool_correction_version"


class CorrectingModel(ScopedModel):
    def __init__(self, *, forbidden_inspection: bool = False) -> None:
        super().__init__()
        self.rejected = False
        self.observed_tokens = 0
        self.forbidden_inspection = forbidden_inspection

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if request.required_tool == "project_inspect":
            assert not self.forbidden_inspection, "known inspection model attempt must be reused"
            if not self.rejected:
                self.rejected = True
                self.requests.append(request)
                self.observed_tokens += 10
                raise GatewayRejectedOutput(evidence=RejectedOutputEvidence(final_text="PRIVATE_REJECTED_BODY",
                    usage=TokenUsage(7, 3, 10), usage_status="known", status="completed", reason="invalid_tool"),
                    logical_model=request.logical_model, deployment_id="shared", provider_id="test",
                    provider_model="test/model", cost_usd=None)
        if any("Main Agent final delivery" in str(message.content) for message in request.messages
               if message.role == "system"):
            self.requests.append(request)
            self.observed_tokens += 2
            return GatewayCompletion(response=ModelResponse(text=REPORT, usage=TokenUsage(1, 1, 2)),
                logical_model=request.logical_model, deployment_id="shared", provider_id="test",
                provider_model="test/model", cost_usd=Decimal(0))
        result = await super().complete_with_context(request)
        assert result.response.usage is not None
        self.observed_tokens += result.response.usage.total_tokens
        return result


def correction_context() -> TaskContext:
    ctx = action_context()
    return ctx.model_copy(update={"routing_decision": {**ctx.routing_decision,
        POLICY: 1, "engineering_checkpoint_ack_version": 1, "engineering_final_report_version": 1}})


def configuration() -> DiscussionPlan:
    configured = plan()
    return replace(configured, max_turns=16, participants=(
        replace(configured.participants[0], allowed_tools=(
            *configured.participants[0].allowed_tools, "project.inspect")), configured.participants[1],
    ))


def backend(ctx: TaskContext) -> Capabilities:
    result = Capabilities()
    result.results["project.inspect"] = inspection(ctx)
    return result


async def test_known_tool_rejection_is_corrected_and_delivered_with_actual_final_report() -> None:
    ctx, model = correction_context(), CorrectingModel()
    tools = backend(ctx)
    runtime = AutoGenDiscussionRuntime(model, configuration(), capability_gateway=tools)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    inspected = [request for request in model.requests if request.required_tool == "project_inspect"]
    assert len(inspected) == 2 and inspected[0].tools == inspected[1].tools
    assert inspected[1].max_output_tokens < inspected[0].max_output_tokens
    assert tools.calls == ["project.inspect", "project.verify", "workspace.bundle"]
    final = [event.artifact for event in events if event.artifact is not None
             and event.artifact.content.get("presentation") == "final_answer"]
    assert len(final) == 1 and final[0].content["text"] == REPORT
    saved = await runtime.save_checkpoint()
    assert saved.state[POLICY] == 1 and "PRIVATE_REJECTED_BODY" not in str(saved.to_payload())


async def test_correction_capacity_failure_reports_current_fault_not_first_rejection() -> None:
    class CapacityFailure(CorrectingModel):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if request.required_tool == "project_inspect" and self.rejected:
                self.requests.append(request)
                raise CapacityWaitTimeout("model capacity wait timeout")
            return await super().complete_with_context(request)

    ctx, model = correction_context(), CapacityFailure()
    tools = backend(ctx)
    runtime = AutoGenDiscussionRuntime(model, configuration(), capability_gateway=tools)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason is not None and "capacity" in events[-1].reason
    assert "invalid_tool" not in events[-1].reason
    assert not tools.calls


@pytest.mark.parametrize("boundary", ["rejection", "correction_intent", "accepted_correction"])
async def test_cold_boundaries_keep_unknown_stopped_and_reuse_known_corrected_output(boundary: str) -> None:
    ctx, model = correction_context(), CorrectingModel()
    tools = backend(ctx)
    runtime = AutoGenDiscussionRuntime(model, configuration(), capability_gateway=tools)
    stream = cast(AsyncGenerator[RunEvent, None], runtime.run(ctx))
    saved = None
    try:
        async for event in stream:
            checkpoint = event.checkpoint
            if checkpoint is None:
                continue
            rows = cast(tuple[Mapping[str, JsonValue], ...], checkpoint.state["model_ledger"])
            if not rows:
                continue
            last = rows[-1]
            hit = (boundary == "rejection" and last.get("status") == "rejected"
                or boundary == "correction_intent" and "correction_for" in last and last["status"] == "running"
                or boundary == "accepted_correction" and "correction_for" in last and last["status"] == "succeeded")
            if hit:
                saved = checkpoint
                if boundary == "accepted_correction":
                    # RunRepository's checkpoint transaction makes referenced
                    # private artifacts permanent before acknowledging the stream.
                    registry = cast(Mapping[str, str], checkpoint.state["artifact_registry"])
                    references = tuple(ArtifactReference(id=UUID(key), sha256=value)
                                       for key, value in registry.items())
                    stored = await runtime._repository.get_many(ctx.tenant_id, ctx.run_id, references)
                    for artifact in stored:
                        await runtime._repository.put(ctx.tenant_id, ctx.run_id, artifact)
                break
    finally:
        await stream.aclose()
    assert saved is not None, boundary
    assert tools.calls == []
    other = CorrectingModel(forbidden_inspection=boundary != "rejection")
    if boundary == "rejection":
        other.rejected = True
    resumed = AutoGenDiscussionRuntime(other, configuration(), capability_gateway=tools,
                                        artifact_repository=runtime._repository)
    await resumed.restore_checkpoint(saved)
    events = [event async for event in resumed.run(ctx.model_copy(update={"checkpoint": saved}))]
    if boundary in {"rejection", "accepted_correction"}:
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
        assert tools.calls == ["project.inspect", "project.verify", "workspace.bundle"]
        inspections = [request for request in other.requests if request.required_tool == "project_inspect"]
        if boundary == "rejection":
            assert len(inspections) == 1
            rejected = cast(tuple[Mapping[str, JsonValue], ...], saved.state["model_ledger"])[-1]
            aliases = {"project.inspect": "project.inspect", "project_inspect": "project.inspect"}
            original = next(request for request in model.requests if request.required_tool == "project_inspect")
            assert _DiscussionDurability.request_hash(original, tool_names=aliases) == rejected["request_hash"]
            assert _DiscussionDurability.request_hash(inspections[0], tool_names=aliases) != rejected["request_hash"]
        else:
            assert not inspections
        previous_usage = cast(Mapping[str, JsonValue], saved.state["usage"])
        final_usage = cast(Mapping[str, JsonValue], (await resumed.save_checkpoint()).state["usage"])
        assert final_usage["tokens"] == cast(int, previous_usage["tokens"]) + other.observed_tokens
    else:
        assert events[-1].kind is EventKind.RUNTIME_FAILED
        assert not other.requests and not tools.calls
        assert (await resumed.save_checkpoint()).state["usage"] == saved.state["usage"]


async def test_correction_policy_cannot_be_added_or_removed_during_cold_recovery() -> None:
    ctx, model = correction_context(), CorrectingModel()
    tools = backend(ctx)
    runtime = AutoGenDiscussionRuntime(model, configuration(), capability_gateway=tools)
    assert [event async for event in runtime.run(ctx)][-1].kind is EventKind.RUNTIME_COMPLETED
    saved = await runtime.save_checkpoint()
    for version in (None, True, 2):
        other = CorrectingModel(forbidden_inspection=True)
        resumed = AutoGenDiscussionRuntime(other, configuration(), capability_gateway=tools,
                                            artifact_repository=runtime._repository)
        await resumed.restore_checkpoint(saved)
        route = {key: value for key, value in ctx.routing_decision.items() if key != POLICY}
        if version is not None:
            route[POLICY] = version
        events = [event async for event in resumed.run(ctx.model_copy(update={
            "checkpoint": saved, "routing_decision": route,
        }))]
        assert events[-1].kind is EventKind.RUNTIME_FAILED and not other.requests
