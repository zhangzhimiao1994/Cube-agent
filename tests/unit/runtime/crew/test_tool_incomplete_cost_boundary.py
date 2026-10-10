"""Recovery tool rounds must not purchase a call at an exhausted cost boundary."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Literal

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime
from agent_hub.runtime.crew.plan import DispatchPlan
from tests.unit.runtime.crew.test_adapter_failure_reason import RecordingHarnessToolGateway
from tests.unit.runtime.crew.test_known_incomplete_recovery import collect
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping
from tests.unit.runtime.crew.test_tool_incomplete_recovery import (
    ToolIncompleteGateway,
    draft_models,
    subject,
    tool_plan,
)

Scope = Literal["step", "global"]


def cost_plan(scope: Scope) -> DispatchPlan:
    base = tool_plan()
    # Valid plans reserve the sum of step costs; global equality also reaches the draft cap.
    return base.model_copy(update={
        "total_cost_usd": Decimal(1) if scope == "global" else Decimal(3),
        "steps": (
            base.steps[0].model_copy(update={"cost_budget_usd": Decimal(1)}),
            base.steps[1].model_copy(update={
                "cost_budget_usd": Decimal(0) if scope == "global" else Decimal(1),
            }),
        ),
    })


class CostGateway(ToolIncompleteGateway):
    def __init__(self, *outcomes: str, fresh_cost: Decimal = Decimal("0.99")) -> None:
        super().__init__(*(outcomes or ("incomplete", "tool", "success", "final")))
        self.fresh_cost = fresh_cost

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        outcome = self.outcomes[len(self.requests)]
        completion = await super().complete_with_context(request)
        cost = (self.fresh_cost if outcome == "tool" else
                Decimal(0) if outcome == "final" else Decimal("0.005"))
        return replace(completion, cost_usd=cost)


async def natural_boundaries(
    scope: Scope, fresh_cost: Decimal,
) -> tuple[DispatchPlan, InMemoryArtifactRepository, list[RunEvent]]:
    dispatch = cost_plan(scope)
    repository = InMemoryArtifactRepository()
    runtime = subject(CostGateway(fresh_cost=fresh_cost), dispatch, repository,
                      RecordingHarnessToolGateway())
    events = await collect(runtime, fail="budget exhausted" if fresh_cost == Decimal("0.99") else None)
    return dispatch, repository, events


def select_boundary(events: list[RunEvent], phase: str) -> RuntimeCheckpoint:
    def matches(checkpoint: RuntimeCheckpoint) -> bool:
        rows = draft_models(checkpoint)
        tools = [mapping(value) for value in mapping(checkpoint.state["tools"]).values()]
        if checkpoint.state["phase"] != "running" or len(rows) != 2:
            return False
        if phase == "model_running":
            return rows[-1]["status"] == "running" and not tools
        return rows[-1]["status"] == "succeeded" and len(tools) == 1 and tools[0]["status"] == phase

    return next(event.checkpoint for event in events
                if event.checkpoint is not None and matches(event.checkpoint))


@pytest.mark.parametrize("scope", ["step", "global"])
async def test_equal_cost_after_fresh_tool_receipt_never_purchases_third_call(scope: Scope) -> None:
    dispatch = cost_plan(scope)
    gateway = CostGateway()
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    await collect(runtime, fail="budget exhausted")
    assert len(gateway.requests) == 2, "cost 0.01 + 0.99 reached 1.00 before the third paid call"
    assert len(harness.calls) == 1
    checkpoint = await runtime.save_checkpoint()
    assert Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) == Decimal("1.00")
    assert mapping(checkpoint.state["usage"])["tokens"] == 8395
    assert len(draft_models(checkpoint)) == 2 and checkpoint.state["structured_repairs"] == {}


@pytest.mark.parametrize("scope", ["step", "global"])
@pytest.mark.parametrize("phase", ["prepared", "succeeded"])
async def test_equal_cost_cached_known_readback_never_rebills_or_reissues_model(
    scope: Scope, phase: str,
) -> None:
    dispatch, repository, events = await natural_boundaries(scope, Decimal("0.99"))
    checkpoint = select_boundary(events, phase)
    before = checkpoint.to_payload()
    assert Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) == Decimal("1.00")
    assert mapping(checkpoint.state["usage"])["tokens"] == 8395
    gateway = CostGateway("success", "final")
    harness = RecordingHarnessToolGateway()
    replay = subject(gateway, dispatch, repository, harness)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, fail="budget exhausted")
    assert gateway.requests == [], "known cached model receipt is readable, not a new paid call"
    assert len(harness.calls) == int(phase == "prepared")
    saved = await replay.save_checkpoint()
    for section in ("models", "rejected_outputs", "usage", "step_usage", "structured_repairs"):
        assert saved.state[section] == checkpoint.state[section]
    assert all(mapping(value)["status"] == "succeeded"
               for value in mapping(saved.state["tools"]).values())
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize("scope", ["step", "global"])
@pytest.mark.parametrize("phase", ["model_running", "running"])
async def test_unknown_model_or_tool_boundary_never_replays_despite_cost_gate(
    scope: Scope, phase: str,
) -> None:
    dispatch, repository, events = await natural_boundaries(scope, Decimal("0.99"))
    checkpoint = select_boundary(events, phase)
    before = checkpoint.to_payload()
    gateway = CostGateway("success", "final")
    harness = RecordingHarnessToolGateway()
    replay = subject(gateway, dispatch, repository, harness)
    await replay.restore_checkpoint(checkpoint)
    await collect(replay, checkpoint=checkpoint, fail=(
        "model outcome requires confirmation" if phase == "model_running"
        else "capability outcome requires confirmation"
    ))
    assert gateway.requests == [] and harness.calls == []
    assert (await replay.save_checkpoint()).to_payload() == before


@pytest.mark.parametrize("scope", ["step", "global"])
async def test_below_cost_boundary_cached_known_readback_preserves_receipt_and_continues(scope: Scope) -> None:
    dispatch, repository, events = await natural_boundaries(scope, Decimal("0.98"))
    checkpoint = select_boundary(events, "succeeded")
    before = checkpoint.to_payload()
    assert Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) == Decimal("0.99")
    gateway = CostGateway("success", "final")
    harness = RecordingHarnessToolGateway()
    replay: CrewDispatchRuntime = subject(gateway, dispatch, repository, harness)
    await replay.restore_checkpoint(checkpoint)
    replay_events = await collect(replay, checkpoint=checkpoint)
    assert replay_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 2 and harness.calls == []
    assert all(not request.allow_fallback for request in gateway.requests[:1])
    saved = await replay.save_checkpoint()
    assert Decimal(mapping(saved.state["usage"])["cost_usd"]) == Decimal("0.995")
    assert mapping(saved.state["usage"])["tokens"] == 8423
    for key, row in mapping(checkpoint.state["models"]).items():
        assert mapping(saved.state["models"])[key] == row
    assert saved.state["rejected_outputs"] == checkpoint.state["rejected_outputs"]
    assert saved.state["tools"] == checkpoint.state["tools"]
    assert saved.state["structured_repairs"] == {}
    assert checkpoint.to_payload() == before
