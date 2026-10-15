"""Checkpoint boundaries for saturated initial recovery chains, with no paid IO."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest

from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayRejectedOutput,
    GatewayResponseCancelled,
)
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import Deployment, ModelCapability, ModelRequest, TokenUsage
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RuntimeCheckpoint
from agent_hub.runtime.crew.adapter import RuntimeExecutionError
from tests.unit.runtime.crew.test_adapter_budgeted_incomplete_recovery import (
    TwiceSaturatedGateway as BaseChainGateway,
)
from tests.unit.runtime.crew.test_adapter_failure_reason import RecordingHarnessToolGateway
from tests.unit.runtime.crew.test_known_incomplete_recovery import collect
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping
from tests.unit.runtime.crew.test_tool_incomplete_recovery import (
    ToolIncompleteGateway,
    draft_models,
    subject,
    tool_plan,
)


class TwiceSaturatedGateway(BaseChainGateway):
    def __init__(self) -> None:
        super().__init__()
        self.deployment = replace(self.deployment, capabilities=frozenset({
            ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING,
        }))
        self._registry = ModelRegistry([self.deployment])


async def test_chain_keeps_business_attempt_and_tool_round_bound_to_success() -> None:
    gateway = TwiceSaturatedGateway()
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(100000), InMemoryArtifactRepository(), harness)
    events = await collect(runtime, tokens=100000)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runtime.save_checkpoint()
    rows = draft_models(checkpoint)
    assert [(row["attempt"], row["call_index"]) for row in rows] == [(0, 0), (1, 0), (1, 1), (1, 2)]
    assert rows[2]["reserved_max_output_tokens"] == rows[3]["reserved_max_output_tokens"] == 24576
    assert checkpoint.state["initial_incomplete_chain_growth"] is True
    tools = list(mapping(checkpoint.state["tools"]).values())
    assert len(tools) == 1 and tools[0]["round"] == 1 and len(harness.calls) == 1


@pytest.mark.parametrize("phase", ["prepared", "running", "rejected", "succeeded", "tool_succeeded", "completed"])
async def test_natural_chain_checkpoint_never_replays_received_models_or_tools(phase: str) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan(100000)
    gateway = TwiceSaturatedGateway()
    runtime = subject(gateway, dispatch, repository, RecordingHarnessToolGateway())
    events = await collect(runtime, tokens=100000)

    def matches(checkpoint: RuntimeCheckpoint) -> bool:
        rows = draft_models(checkpoint)
        tools = list(mapping(checkpoint.state["tools"]).values())
        if phase == "completed":
            return checkpoint.state["phase"] == "completed"
        if phase == "rejected":
            return len(rows) == 2 and rows[-1]["status"] == "rejected"
        if phase == "tool_succeeded":
            return len(rows) == 3 and len(tools) == 1 and tools[0]["status"] == "succeeded"
        return len(rows) == 3 and rows[-1]["status"] == phase and not tools

    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None and matches(event.checkpoint))
    before = copy.deepcopy(checkpoint.to_payload())
    outcomes: tuple[str, ...] = ("success", "final")
    if phase in {"prepared", "rejected"}:
        outcomes = ("tool", "success", "final")
    if phase == "completed":
        outcomes = ()
    resumed_gateway = TwiceSaturatedGateway()
    resumed_gateway.outcomes = outcomes
    harness = RecordingHarnessToolGateway()
    resumed = subject(resumed_gateway, dispatch, repository, harness)
    await resumed.restore_checkpoint(checkpoint)
    await collect(resumed, checkpoint=checkpoint, tokens=100000,
                  fail="outcome requires confirmation" if phase == "running" else None)
    if phase in {"running", "completed"}:
        assert resumed_gateway.requests == [] and harness.calls == []
    else:
        assert len(resumed_gateway.requests) == len(outcomes)
        assert len(harness.calls) == int(phase in {"prepared", "rejected", "succeeded"})
        assert mapping((await resumed.save_checkpoint()).state["usage"])["tokens"] == 23049
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize("change", ["flag_missing", "flag_false", "flag_type", "cap", "source_cap", "source_cap_type",
                                    "source_key", "source_request_sha256", "source_receipt_sha256",
                                    "request_hash", "actor", "index", "receipt", "followup_cap"])
async def test_chain_corruption_is_rejected_before_any_model_or_tool(change: str) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan(100000)
    runtime = subject(TwiceSaturatedGateway(), dispatch, repository, RecordingHarnessToolGateway())
    events = await collect(runtime, tokens=100000)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and len(draft_models(event.checkpoint)) == 4
                      and draft_models(event.checkpoint)[-1]["status"] == "prepared")
    payload: dict[str, Any] = checkpoint.to_payload()
    state = payload["state"]
    rows = sorted((row for row in state["models"].values() if row["step_id"] == "draft"),
                  key=lambda row: (row["attempt"], row["call_index"]))
    row = rows[2]
    if change.startswith("flag"):
        if change == "flag_missing":
            state.pop("initial_incomplete_chain_growth")
        else:
            state["initial_incomplete_chain_growth"] = False if change == "flag_false" else 1
    elif change == "cap":
        row["reserved_max_output_tokens"] = 12288
    elif change == "source_cap":
        row["initial_incomplete_chain"]["source_cap"] = 6144
    elif change == "source_cap_type":
        row["initial_incomplete_chain"]["source_cap"] = 12288.0
    elif change in {"source_key", "source_request_sha256", "source_receipt_sha256"}:
        row["initial_incomplete_chain"][change] = "a" * 64
    elif change == "request_hash":
        row["request_sha256"] = "a" * 64
    elif change == "actor":
        row["actor"] = "finalizer"
    elif change == "index":
        row["call_index"] = 2
    elif change == "followup_cap":
        rows[3]["reserved_max_output_tokens"] = 24577
    else:
        key = row["initial_incomplete_chain"]["source_key"]
        state["rejected_outputs"][key]["provenance"]["logical_model"] = "backup"
        state["models"][key]["provenance"]["logical_model"] = "backup"
        # Update the edge hash too: provenance must independently match its initial source.
        row["initial_incomplete_chain"]["source_receipt_sha256"] = hashlib.sha256(json.dumps(
            state["rejected_outputs"][key], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
    payload["state_sha256"] = ""
    corrupted = RuntimeCheckpoint.from_payload(payload)
    gateway = TwiceSaturatedGateway()
    harness = RecordingHarnessToolGateway()
    resumed = subject(gateway, dispatch, repository, harness)
    with pytest.raises(RuntimeExecutionError, match="checkpoint|request changed"):
        await resumed.restore_checkpoint(corrupted)
        await collect(resumed, checkpoint=corrupted, tokens=100000)
    assert gateway.requests == [] and harness.calls == []


async def test_legacy_missing_flag_still_stops_after_the_original_two_calls() -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan(100000)
    runtime = subject(TwiceSaturatedGateway(), dispatch, repository, RecordingHarnessToolGateway())
    events = await collect(runtime, tokens=100000)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and len(draft_models(event.checkpoint)) == 1
                      and draft_models(event.checkpoint)[0]["status"] == "rejected")
    payload = checkpoint.to_payload()
    state = dict(mapping(payload["state"]))
    state.pop("initial_incomplete_chain_growth")
    payload["state"] = state
    payload["state_sha256"] = ""
    legacy = RuntimeCheckpoint.from_payload(payload)
    gateway = TwiceSaturatedGateway()
    gateway.outcomes = ("incomplete",)
    harness = RecordingHarnessToolGateway()
    resumed = subject(gateway, dispatch, repository, harness)
    await resumed.restore_checkpoint(legacy)
    await collect(resumed, checkpoint=legacy, tokens=100000, fail="structured output invalid")
    assert len(gateway.requests) == 1 and gateway.requests[0].max_output_tokens == 12288
    assert harness.calls == []


async def test_low_cap_deployment_without_required_capabilities_does_not_constrain_growth() -> None:
    gateway = TwiceSaturatedGateway()
    gateway._registry = ModelRegistry([gateway.deployment, Deployment(
        id="text-only", logical_model="selected", max_output_tokens=12288,
        capabilities=frozenset({ModelCapability.TEXT}),
    )])
    runtime = subject(gateway, tool_plan(100000), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    await collect(runtime, tokens=100000)
    assert gateway.requests[2].max_output_tokens == 24576


async def test_context_ceiling_stops_without_an_effectively_equal_extra_call() -> None:
    class NarrowedContextGateway(TwiceSaturatedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput:
                if len(self.requests) == 2:
                    self._registry = ModelRegistry([replace(self.deployment, context_window_tokens=12288)])
                raise

    gateway = NarrowedContextGateway()
    runtime = subject(gateway, tool_plan(100000), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    await collect(runtime, tokens=100000, fail="budget exhausted")
    assert [request.max_output_tokens for request in gateway.requests] == [6144, 12288]


@pytest.mark.parametrize("cancel", [False, True])
async def test_third_saturated_receipt_grows_again_or_preserves_cancellation(cancel: bool) -> None:
    class ThreeSaturatedGateway(TwiceSaturatedGateway):
        def __init__(self) -> None:
            super().__init__()
            self.outcomes = ("incomplete", "incomplete", "incomplete", "tool", "success", "final")

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await ToolIncompleteGateway.complete_with_context(self, request)
            except GatewayRejectedOutput as error:
                assert error.evidence is not None
                usage = TokenUsage(10, request.max_output_tokens, 10 + request.max_output_tokens)
                error.evidence = replace(error.evidence, usage=usage)
                if cancel and len(self.requests) == 3:
                    raise GatewayResponseCancelled(receipt=error) from None
                raise

    repository = InMemoryArtifactRepository()
    dispatch = tool_plan(200000)
    gateway = ThreeSaturatedGateway()
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, repository, harness)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await collect(runtime, tokens=200000)
    else:
        await collect(runtime, tokens=200000)
        assert [request.max_output_tokens for request in gateway.requests[:4]] == [6144, 12288, 24576, 49152]
        assert len(harness.calls) == 1
    checkpoint = await runtime.save_checkpoint()
    replay_gateway = ThreeSaturatedGateway()
    replay_harness = RecordingHarnessToolGateway()
    resumed = subject(replay_gateway, dispatch, repository, replay_harness)
    await resumed.restore_checkpoint(checkpoint)
    await collect(resumed, checkpoint=checkpoint, tokens=200000,
                  fail="outcome requires confirmation" if cancel else None)
    assert replay_gateway.requests == [] and replay_harness.calls == []


async def test_recovery_checkpoint_preserves_previously_completed_step() -> None:
    class PredecessorGateway(TwiceSaturatedGateway):
        def __init__(self) -> None:
            super().__init__()
            self.outcomes = ("success", "incomplete", "incomplete", "tool", "success", "final")

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await ToolIncompleteGateway.complete_with_context(self, request)
            except GatewayRejectedOutput as error:
                assert error.evidence is not None
                error.evidence = replace(error.evidence, usage=TokenUsage(10, request.max_output_tokens, 10 + request.max_output_tokens))
                raise

    repository = InMemoryArtifactRepository()
    base = tool_plan(100000)
    prerequisite = base.steps[0].model_copy(update={"id": "prerequisite", "tools": (), "depends_on": ()})
    dispatch = base.model_copy(update={"steps": (prerequisite,
        base.steps[0].model_copy(update={"depends_on": ("prerequisite",)}), *base.steps[1:])})
    runtime = subject(PredecessorGateway(), dispatch, repository, RecordingHarnessToolGateway())
    events = await collect(runtime, tokens=100000)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and len(draft_models(event.checkpoint)) == 3
                      and draft_models(event.checkpoint)[-1]["status"] == "prepared")
    completed = checkpoint.state["completed"]
    assert isinstance(completed, tuple) and "prerequisite" in completed
    original = {key: row for key, row in mapping(checkpoint.state["models"]).items()
                if row["step_id"] == "prerequisite"}
    gateway = TwiceSaturatedGateway()
    gateway.outcomes = ("tool", "success", "final")
    harness = RecordingHarnessToolGateway()
    resumed = subject(gateway, dispatch, repository, harness)
    await resumed.restore_checkpoint(checkpoint)
    await collect(resumed, checkpoint=checkpoint, tokens=100000)
    assert len(gateway.requests) == 3 and len(harness.calls) == 1
    saved = await resumed.save_checkpoint()
    assert {key: row for key, row in mapping(saved.state["models"]).items()
            if row["step_id"] == "prerequisite"} == original


@pytest.mark.parametrize("initial_chain,corruption", [
    (False, None), (True, None), (True, "review_verdict"), (True, "future_tool_lineage"),
], ids=["once_recovery", "initial_chain", "forged_review_verdict", "forged_future_tool_lineage"])
async def test_closed_initial_chain_survives_reviewer_revision(
    initial_chain: bool, corruption: str | None,
) -> None:
    from decimal import Decimal

    from agent_hub.models.types import ToolCall

    class ReviewerRevisionGateway(TwiceSaturatedGateway):
        def __init__(self) -> None:
            super().__init__()
            prefix = ("incomplete", "incomplete") if initial_chain else ("incomplete",)
            self.outcomes = (*prefix, "tool", "success", "revise", "tool", "success", "approve", "final")

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            outcome = self.outcomes[len(self.requests)]
            result = await super().complete_with_context(request)
            if result.response.tool_calls:
                result = replace(result, response=replace(result.response, tool_calls=(
                    ToolCall(id=f"tool-{len(self.requests)}", name="web_search",
                             arguments={"q": f"query-{len(self.requests)}"}),
                )))
            if outcome in {"revise", "approve"}:
                result = replace(result, response=replace(result.response, text=json.dumps({
                    "verdict": outcome, "feedback": "check facts",
                })))
            return result

    base = tool_plan(200000)
    dispatch = base.model_copy(update={"total_cost_usd": Decimal(5), "steps": (
        base.steps[0].model_copy(update={"reviewer": "reviewer", "reviewer_retries": 1}),
        base.steps[1],
    )})
    repository = InMemoryArtifactRepository()
    gateway = ReviewerRevisionGateway()
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, repository, harness)
    events = await collect(runtime, tokens=200000)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == (9 if initial_chain else 8) and len(harness.calls) == 2
    assert [request.max_output_tokens for request in gateway.requests[:3 if initial_chain else 2]] == (
        [6144, 12288, 24576] if initial_chain else [6144, 12288]
    )
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["phase"] == "completed"
    tools = sorted((mapping(row) for row in mapping(checkpoint.state["tools"]).values()),
                   key=lambda row: row["attempt"])
    assert len(tools) == 2 and tools[0]["attempt"] == 1 and tools[1]["attempt"] > 1
    assert all(row["status"] == "succeeded" for row in tools)
    if corruption is not None:
        from uuid import UUID

        from agent_hub.runtime.contracts import Artifact

        payload: dict[str, Any] = copy.deepcopy(checkpoint.to_payload())
        state = payload["state"]
        if corruption == "review_verdict":
            row = min((row for row in state["models"].values() if row["purpose"] == "review"),
                      key=lambda row: (row["attempt"], row["call_index"]))
        else:
            row = max(state["tools"].values(), key=lambda row: row["attempt"])
        artifact_key = (checkpoint.tenant_id, checkpoint.run_id, UUID(row["artifact_id"]))
        artifact = repository._artifacts[artifact_key]
        fields: dict[str, Any] = artifact.to_payload()
        fields["id"] = artifact.id
        fields["content_sha256"] = ""
        if corruption == "review_verdict":
            fields["content"]["text"] = json.dumps({"verdict": "approve", "feedback": "check facts"})
        else:
            earlier = min(state["tools"].values(), key=lambda row: row["attempt"])
            fields["source_ids"] = [earlier["trigger_model_artifact_id"]]
        altered = Artifact.model_validate(fields)
        # Keep storage/digest bindings coherent so independent graph validation must reject.
        repository._artifacts[artifact_key] = altered
        row["sha256"] = altered.content_sha256
        state["artifact_registry"][str(altered.id)] = altered.content_sha256
        payload["state_sha256"] = ""
        checkpoint = RuntimeCheckpoint.from_payload(payload)
    before = copy.deepcopy(checkpoint.to_payload())
    artifacts_before = dict(repository._artifacts)
    replay_gateway = ReviewerRevisionGateway()
    replay_harness = RecordingHarnessToolGateway()
    resumed = subject(replay_gateway, dispatch, repository, replay_harness)
    await resumed.restore_checkpoint(checkpoint)
    if corruption is not None:
        with pytest.raises(RuntimeExecutionError, match="checkpoint.*(review|artifact|lineage)"):
            await collect(resumed, checkpoint=checkpoint, tokens=200000)
        assert replay_gateway.requests == [] and replay_harness.calls == []
        assert checkpoint.to_payload() == before and repository._artifacts == artifacts_before
        return
    resumed_events = await collect(resumed, checkpoint=checkpoint, tokens=200000)
    assert resumed_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert replay_gateway.requests == [] and replay_harness.calls == []
    assert (await resumed.save_checkpoint()).to_payload() == before
    assert checkpoint.to_payload() == before and repository._artifacts == artifacts_before
