from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.models.gateway import GatewayCompletion, GatewayRejectedOutput
from agent_hub.models.types import (
    ModelRequest,
    ModelResponse,
    RejectedOutputEvidence,
    TokenUsage,
    ToolCall,
)
from agent_hub.runs.repository import _public_event_payload
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.crew.adapter import (
    CrewDispatchRuntime,
    ModelOutcomeUncertain,
    RuntimeExecutionError,
)
from agent_hub.runtime.crew.plan import DispatchPlan
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FakeCapabilities,
    FastFactory,
    RecordingHarnessToolGateway,
    _context,
    _reviewed_step_plan,
)

PRIVATE_ARGUMENT = "PRIVATE_FORBIDDEN_TOOL_ARGUMENT"


class KnownUsageToolGateway:
    def __init__(self, phase: str, *, allowed_first: bool = False, fallback: bool = False) -> None:
        self.phase = phase
        self.allowed_first = allowed_first
        self.fallback = fallback
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        forbidden = self.phase == "step" or len(self.requests) == 2
        if forbidden:
            allowed_calls = (ToolCall(
                id="provider-allowed-call", name="web.search", arguments={"query": "allowed query"},
            ),) if self.allowed_first else ()
            response = ModelResponse(
                text=None,
                usage=TokenUsage(5, 12, 17),
                tool_calls=(*allowed_calls, ToolCall(
                    id="provider-forbidden-call",
                    name="read_context",
                    arguments={"query": PRIVATE_ARGUMENT},
                ),),
            )
        else:
            response = ModelResponse(
                text="invalid JSON" if self.phase == "correction" else '{"summary":"candidate"}',
                usage=TokenUsage(1, 1, 2),
            )
        return GatewayCompletion(
            response=response,
            deployment_id="backup" if forbidden and self.fallback else "primary",
            logical_model="backup" if forbidden and self.fallback else request.logical_model,
            provider_id="provider",
            provider_model="provider/model",
            cost_usd=Decimal("0.03") if forbidden else Decimal("0.01"),
            fallback_used=forbidden and self.fallback,
            fallback_from_logical_model=request.logical_model if forbidden and self.fallback else None,
            fallback_reason="capacity_unavailable" if forbidden and self.fallback else None,
            attempted_logical_models=(request.logical_model, "backup")
            if forbidden and self.fallback else (),
        )


def _receipt_plan() -> DispatchPlan:
    plan = _reviewed_step_plan()
    return plan.model_copy(update={
        "allowed_tools": ("web.search",),
        "total_cost_usd": Decimal(1),
        "agents": tuple(agent.model_copy(update={
            "allowed_tools": ("web.search",) if agent.id == "writer" else (),
        }) for agent in plan.agents),
        "steps": tuple(step.model_copy(update={
            "tools": ("web.search",) if step.id == "draft" else (),
            "cost_budget_usd": Decimal("0.1"),
        }) for step in plan.steps),
    })


@pytest.mark.parametrize("phase,reason,tokens,cost", [
    ("step", "step requested a forbidden capability", 17, "0.03"),
    ("review", "reviewer returned tool calls instead of JSON", 19, "0.04"),
    ("correction", "structured output invalid", 19, "0.04"),
])
@pytest.mark.parametrize("allowed_first", [False, True], ids=["forbidden-only", "mixed-batch"])
@pytest.mark.parametrize("fallback", [False, True], ids=["primary", "fallback"])
async def test_forbidden_tool_paid_receipt_is_private_accounted_and_never_replayed(
    phase: str, reason: str, tokens: int, cost: str, allowed_first: bool, fallback: bool,
) -> None:
    plan = _receipt_plan()
    gateway = KnownUsageToolGateway(phase, allowed_first=allowed_first, fallback=fallback)
    capabilities, harness = FakeCapabilities(), RecordingHarnessToolGateway()
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
        capability_gateway=capabilities, harness_tool_gateway=harness,
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError) as failure:
        async for event in runtime.run(_context()):
            events.append(event)
    assert str(failure.value) == reason
    assert next(event for event in events if event.kind is EventKind.RUNTIME_FAILED).reason == reason
    assert len(gateway.requests) == (1 if phase == "step" else 2)
    assert capabilities.calls == []
    assert harness.calls == []
    assert not any(event.kind in {
        EventKind.STEP_COMPLETED, EventKind.REVIEW_COMPLETED, EventKind.RUNTIME_COMPLETED,
        EventKind.STEP_RETRYING,
    } or str(event.kind).startswith("tool.") for event in events)
    if phase != "step":
        assert gateway.requests[-1].tools == ()

    checkpoint = await runtime.save_checkpoint()
    before = checkpoint.to_payload()
    replay_gateway = KnownUsageToolGateway(phase, allowed_first=allowed_first, fallback=fallback)
    replay = CrewDispatchRuntime(
        replay_gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
        capability_gateway=capabilities, harness_tool_gateway=harness,
    )
    await replay.restore_checkpoint(checkpoint)
    replay_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError) as replay_failure:
        async for event in replay.run(_context(checkpoint=checkpoint)):
            replay_events.append(event)
    assert replay_gateway.requests == []
    assert capabilities.calls == []
    assert harness.calls == []
    assert not any(event.kind is EventKind.COST_RECORDED for event in replay_events)
    assert checkpoint.to_payload() == before
    assert checkpoint.state["usage"] == {"tokens": tokens, "cost_usd": cost}
    step_usage = checkpoint.state["step_usage"]
    assert isinstance(step_usage, Mapping)
    assert step_usage["draft"] == {"tokens": tokens, "cost_usd": cost}
    recorded = sum((event.cost_usd or Decimal(0)) for event in events
                   if event.kind is EventKind.COST_RECORDED)
    assert recorded == Decimal(cost)
    for event in (*events, *replay_events):
        assert PRIVATE_ARGUMENT not in str(_public_event_payload(event.to_payload()))

    private = checkpoint.state["rejected_outputs"]
    assert isinstance(private, Mapping)
    receipts = [value for value in private.values()
                if isinstance(value, Mapping) and value["reason"] == "invalid_tool"]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["disposition"] == "rejected"
    assert receipt["usage_status"] == "known"
    assert receipt["output_status"] == "completed"
    assert receipt["usage"] == {"prompt_tokens": 5, "completion_tokens": 12, "total_tokens": 17}
    assert receipt["cost_usd"] == "0.03"
    original_model = "review" if phase == "review" else "general"
    assert receipt["provenance"] == {
        "logical_model": "backup" if fallback else original_model,
        "deployment_id": "backup" if fallback else "primary",
        "provider_id": "provider", "provider_model": "provider/model",
    }
    assert receipt["fallback_used"] is fallback
    assert receipt["fallback_from_logical_model"] == (original_model if fallback else None)
    assert receipt["fallback_reason"] == ("capacity_unavailable" if fallback else None)
    assert receipt["attempted_logical_models"] == ((original_model, "backup") if fallback else ())
    assert receipt["final_text"] is None
    assert checkpoint.state["tools"] == {}
    models = checkpoint.state["models"]
    assert isinstance(models, Mapping)
    receipt_key = next(key for key, value in private.items() if value == receipt)
    model = models[receipt_key]
    assert isinstance(model, Mapping)
    assert model["status"] == "rejected"
    assert str(replay_failure.value) == reason


@pytest.mark.parametrize("phase", ["step", "review", "correction"])
@pytest.mark.parametrize("budget_scope", [
    "step_tokens", "plan_tokens", "context_tokens", "step_cost", "plan_cost",
])
async def test_forbidden_tool_receipt_budget_terminal_has_priority_on_replay(
    phase: str, budget_scope: str,
) -> None:
    tokens = 17 if phase == "step" else 19
    cost = Decimal("0.03") if phase == "step" else Decimal("0.04")
    bound = tokens - 1
    plan = _receipt_plan()
    plan = plan.model_copy(update={
        "total_token_budget": bound if budget_scope == "plan_tokens" else plan.total_token_budget,
        "total_cost_usd": cost - Decimal("0.005")
        if budget_scope == "plan_cost" else plan.total_cost_usd,
        "steps": tuple(step.model_copy(update={
            "token_budget": bound if budget_scope in {"step_tokens", "plan_tokens"}
            else step.token_budget,
            "cost_budget_usd": ((cost - Decimal("0.005")) / 2
                                if step.id == "draft" else Decimal(0))
            if budget_scope == "plan_cost" else cost - Decimal("0.005")
            if budget_scope == "step_cost" else step.cost_budget_usd,
        }) for step in plan.steps),
    })
    DispatchPlan.revalidate(plan)
    context = _context(
        token_budget=bound if budget_scope == "context_tokens" else 1000,
        routing_decision={"runtime_plan_token_budget": plan.total_token_budget},
    )
    gateway = KnownUsageToolGateway(phase, allowed_first=True)
    capabilities, harness = FakeCapabilities(), RecordingHarnessToolGateway()
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
        capability_gateway=capabilities, harness_tool_gateway=harness,
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="^dispatch budget exhausted$"):
        async for event in runtime.run(context):
            events.append(event)
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["phase"] == "budget_exhausted"
    assert checkpoint.state["terminal"] is True
    assert checkpoint.state["usage"] == {"tokens": tokens, "cost_usd": str(cost)}
    assert len(gateway.requests) == (1 if phase == "step" else 2)
    assert capabilities.calls == []
    assert harness.calls == []
    assert sum((event.cost_usd or Decimal(0)) for event in events
               if event.kind is EventKind.COST_RECORDED) == cost
    before = checkpoint.to_payload()
    replay_gateway = KnownUsageToolGateway(phase, allowed_first=True)
    replay = CrewDispatchRuntime(
        replay_gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
        capability_gateway=capabilities, harness_tool_gateway=harness,
    )
    await replay.restore_checkpoint(checkpoint)
    replay_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError) as replay_failure:
        async for event in replay.run(context.model_copy(update={"checkpoint": checkpoint})):
            replay_events.append(event)
    assert replay_gateway.requests == []
    assert capabilities.calls == []
    assert harness.calls == []
    assert not any(event.kind is EventKind.COST_RECORDED for event in replay_events)
    assert not any(event.kind in {
        EventKind.STEP_COMPLETED, EventKind.REVIEW_COMPLETED, EventKind.RUNTIME_COMPLETED,
        EventKind.STEP_RETRYING,
    } or str(event.kind).startswith("tool.") for event in (*events, *replay_events))
    for event in (*events, *replay_events):
        assert PRIVATE_ARGUMENT not in str(_public_event_payload(event.to_payload()))
    assert checkpoint.to_payload() == before
    assert (await replay.save_checkpoint()).to_payload() == before
    assert str(replay_failure.value) == "dispatch budget exhausted"
    assert next(event for event in replay_events
                if event.kind is EventKind.RUNTIME_FAILED).reason == "dispatch budget exhausted"


@pytest.mark.parametrize("tamper", [
    "missing_candidate", "model_as_candidate", "unknown_candidate",
    "candidate_producer", "candidate_version", "candidate_sources", "candidate_provenance",
])
async def test_forbidden_reviewer_receipt_checkpoint_rejects_tampered_graph(
    tamper: str,
) -> None:
    plan = _receipt_plan()
    gateway = KnownUsageToolGateway("review")
    repository = InMemoryArtifactRepository()
    capabilities, harness = FakeCapabilities(), RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
        capability_gateway=capabilities, harness_tool_gateway=harness,
    )
    with pytest.raises(RuntimeExecutionError, match="reviewer returned tool calls instead of JSON"):
        _ = [event async for event in runtime.run(_context())]
    checkpoint = await runtime.save_checkpoint()
    payload = cast(dict[str, Any], checkpoint.to_payload())
    state = payload["state"]
    receipt = next(value for value in state["rejected_outputs"].values()
                   if value["reason"] == "invalid_tool")
    candidate_id = receipt["source_ids"][0]
    registry = state["artifact_registry"]
    artifacts = await repository.get_many(
        checkpoint.tenant_id, checkpoint.run_id,
        tuple(ArtifactReference(id=UUID(artifact_id), sha256=digest)
              for artifact_id, digest in registry.items()),
    )
    if tamper == "missing_candidate":
        receipt["source_ids"] = []
    elif tamper == "model_as_candidate":
        receipt["source_ids"] = [str(next(artifact.id for artifact in artifacts
                                          if artifact.type == "model_response"))]
    elif tamper == "unknown_candidate":
        receipt["source_ids"] = ["00000000-0000-0000-0000-000000000099"]
    else:
        candidate = next(artifact for artifact in artifacts if str(artifact.id) == candidate_id)
        candidate_payload = candidate.to_payload()
        field, value = {
            "candidate_producer": ("producer", "reviewer"),
            "candidate_version": ("version", 2),
            "candidate_sources": ("source_ids", []),
            "candidate_provenance": ("provenance", None),
        }[tamper]
        candidate_payload[field] = value
        candidate_payload["id"] = candidate.id
        candidate_payload["content_sha256"] = ""
        damaged_candidate = Artifact.model_validate(candidate_payload)
        registry[candidate_id] = damaged_candidate.content_sha256
        damaged_repository = InMemoryArtifactRepository()
        for artifact in artifacts:
            await damaged_repository.put(
                checkpoint.tenant_id, checkpoint.run_id,
                damaged_candidate if artifact.id == candidate.id else artifact,
            )
        repository = damaged_repository
    # Recompute hashes so recovery must check the actual candidate relationships.
    payload["state_sha256"] = ""
    damaged = RuntimeCheckpoint.from_payload(payload)
    before = damaged.to_payload()
    replay_gateway = KnownUsageToolGateway("review")
    replay = CrewDispatchRuntime(
        replay_gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
        capability_gateway=capabilities, harness_tool_gateway=harness,
    )
    replay_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="checkpoint") as failure:
        await replay.restore_checkpoint(damaged)
        async for event in replay.run(_context(checkpoint=damaged)):
            replay_events.append(event)
    assert replay_gateway.requests == []
    assert capabilities.calls == []
    assert harness.calls == []
    assert not any(event.kind in {
        EventKind.COST_RECORDED, EventKind.STEP_COMPLETED, EventKind.REVIEW_COMPLETED,
        EventKind.RUNTIME_COMPLETED,
    } or str(event.kind).startswith("tool.") for event in replay_events)
    assert PRIVATE_ARGUMENT not in str(failure.value)
    for event in replay_events:
        assert PRIVATE_ARGUMENT not in str(_public_event_payload(event.to_payload()))
    assert damaged.to_payload() == before


class NativeRejectedToolGateway(KnownUsageToolGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        completion = await super().complete_with_context(request)
        if completion.response.tool_calls:
            raise GatewayRejectedOutput(
                evidence=RejectedOutputEvidence(
                    final_text=PRIVATE_ARGUMENT, usage=completion.response.usage,
                    usage_status="known", status="completed", reason="invalid_tool",
                ),
                deployment_id=completion.deployment_id, logical_model=completion.logical_model,
                provider_id=completion.provider_id, provider_model=completion.provider_model,
                cost_usd=completion.cost_usd, fallback_used=completion.fallback_used,
                fallback_from_logical_model=completion.fallback_from_logical_model,
                fallback_reason=completion.fallback_reason,
                attempted_logical_models=completion.attempted_logical_models,
            )
        return completion


@pytest.mark.parametrize("phase", ["step", "review", "correction"])
async def test_gateway_rejected_invalid_tool_retains_structured_failure_and_zero_paid_replay(
    phase: str,
) -> None:
    plan = _receipt_plan()
    gateway = NativeRejectedToolGateway(phase, fallback=True)
    repository = InMemoryArtifactRepository()
    capabilities, harness = FakeCapabilities(), RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
        capability_gateway=capabilities, harness_tool_gateway=harness,
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError) as failure:
        async for event in runtime.run(_context()):
            events.append(event)
    checkpoint = await runtime.save_checkpoint()
    tokens, cost = (17, "0.03") if phase == "step" else (19, "0.04")
    assert checkpoint.state["usage"] == {"tokens": tokens, "cost_usd": cost}
    assert len(gateway.requests) == (1 if phase == "step" else 2)
    assert capabilities.calls == []
    assert harness.calls == []
    private = cast(Mapping[str, Mapping[str, Any]], checkpoint.state["rejected_outputs"])
    receipt_key = next(key for key, receipt in private.items() if receipt["reason"] == "invalid_tool")
    receipt = private[receipt_key]
    assert receipt["final_text"] == PRIVATE_ARGUMENT
    assert receipt["fallback_used"] is True
    assert receipt["attempted_logical_models"] == (
        "review" if phase == "review" else "general", "backup",
    )
    models = cast(Mapping[str, Mapping[str, Any]], checkpoint.state["models"])
    assert models[receipt_key]["failure_reason"] == "structured output rejected"
    before = checkpoint.to_payload()
    replay_gateway = NativeRejectedToolGateway(phase, fallback=True)
    replay = CrewDispatchRuntime(
        replay_gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
        capability_gateway=capabilities, harness_tool_gateway=harness,
    )
    await replay.restore_checkpoint(checkpoint)
    replay_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError) as replay_failure:
        async for event in replay.run(_context(checkpoint=checkpoint)):
            replay_events.append(event)
    assert replay_gateway.requests == []
    assert capabilities.calls == []
    assert harness.calls == []
    assert not any(event.kind is EventKind.COST_RECORDED for event in replay_events)
    assert not any(event.kind in {
        EventKind.STEP_COMPLETED, EventKind.REVIEW_COMPLETED, EventKind.RUNTIME_COMPLETED,
        EventKind.STEP_RETRYING,
    } or str(event.kind).startswith("tool.") for event in (*events, *replay_events))
    for event in (*events, *replay_events):
        assert PRIVATE_ARGUMENT not in str(_public_event_payload(event.to_payload()))
    assert checkpoint.to_payload() == before
    assert str(failure.value) == "structured output invalid"
    assert str(replay_failure.value) == "structured output invalid"


@pytest.mark.parametrize("phase", ["step", "review", "correction"])
async def test_forged_context_budget_terminal_is_rejected_with_actual_run_budget(
    phase: str,
) -> None:
    plan = _receipt_plan()
    gateway = KnownUsageToolGateway(phase)
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
    )
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(_context())]
    checkpoint = await runtime.save_checkpoint()
    payload = cast(dict[str, Any], checkpoint.to_payload())
    payload["state"]["phase"] = "budget_exhausted"
    payload["state"]["terminal"] = True
    payload["state_sha256"] = ""
    damaged = RuntimeCheckpoint.from_payload(payload)
    before = damaged.to_payload()
    replay_gateway = KnownUsageToolGateway(phase)
    capabilities, harness = FakeCapabilities(), RecordingHarnessToolGateway()
    replay = CrewDispatchRuntime(
        replay_gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
        capability_gateway=capabilities, harness_tool_gateway=harness,
    )
    # Restore has no actual context budget; run must enforce it before trusting the phase.
    await replay.restore_checkpoint(damaged)
    replay_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="checkpoint") as failure:
        async for event in replay.run(_context(checkpoint=damaged)):
            replay_events.append(event)
    assert replay_gateway.requests == []
    assert capabilities.calls == []
    assert harness.calls == []
    assert not any(event.kind in {
        EventKind.COST_RECORDED, EventKind.STEP_COMPLETED, EventKind.REVIEW_COMPLETED,
        EventKind.RUNTIME_COMPLETED, EventKind.STEP_RETRYING,
    } or str(event.kind).startswith("tool.") for event in replay_events)
    assert PRIVATE_ARGUMENT not in str(failure.value)
    for event in replay_events:
        assert PRIVATE_ARGUMENT not in str(_public_event_payload(event.to_payload()))
    assert damaged.to_payload() == before


@pytest.mark.parametrize("budget_exceeded", [False, True])
async def test_parallel_uncertain_model_is_not_hidden_by_later_forbidden_receipt(
    budget_exceeded: bool,
) -> None:
    class ParallelGateway(KnownUsageToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            completion = await super().complete_with_context(request)
            if len(self.requests) == 1:
                await asyncio.Event().wait()
            return completion

    plan = _receipt_plan()
    parallel = plan.steps[0].model_copy(update={
        "id": "parallel", "reviewer": None, "tools": (),
    })
    plan = plan.model_copy(update={
        "steps": (
            parallel,
            plan.steps[0].model_copy(update={"token_budget": 16 if budget_exceeded else 1000}),
            plan.steps[1].model_copy(update={"depends_on": ("parallel", "draft")}),
        ),
    })
    DispatchPlan.revalidate(plan)
    gateway = ParallelGateway("step")
    repository = InMemoryArtifactRepository()
    capabilities, harness = FakeCapabilities(), RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
        capability_gateway=capabilities, harness_tool_gateway=harness,
    )
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(_context())]
    checkpoint = await runtime.save_checkpoint()
    models = cast(Mapping[str, Mapping[str, Any]], checkpoint.state["models"])
    assert [(model["step_id"], model["status"]) for model in models.values()] == [
        ("parallel", "running"), ("draft", "rejected"),
    ]
    assert checkpoint.state["usage"] == {"tokens": 17, "cost_usd": "0.03"}
    assert len(gateway.requests) == 2
    before = checkpoint.to_payload()
    replay_gateway = ParallelGateway("step")
    replay = CrewDispatchRuntime(
        replay_gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
        capability_gateway=capabilities, harness_tool_gateway=harness,
    )
    await replay.restore_checkpoint(checkpoint)
    replay_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError) as failure:
        async for event in replay.run(_context(checkpoint=checkpoint)):
            replay_events.append(event)
    assert replay_gateway.requests == []
    assert capabilities.calls == []
    assert harness.calls == []
    assert not any(event.kind is EventKind.COST_RECORDED for event in replay_events)
    for event in replay_events:
        assert PRIVATE_ARGUMENT not in str(_public_event_payload(event.to_payload()))
    assert checkpoint.to_payload() == before
    assert isinstance(failure.value, ModelOutcomeUncertain)
    assert str(failure.value) == "model outcome requires confirmation"
