"""Fresh-run RED for budgeted growth beyond one known-incomplete recovery.

No policy/checkpoint implementation is supplied here. All model/tool boundaries
are existing RAM fixtures; the two rejected usages mirror the reported case.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from agent_hub.models.gateway import GatewayCompletion, GatewayRejectedOutput, ModelGateway
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelRequest,
    RejectedUsageStatus,
    TokenUsage,
)
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.crew.adapter import RuntimeExecutionError
from tests.unit.runtime.crew.test_adapter_failure_reason import RecordingHarnessToolGateway
from tests.unit.runtime.crew.test_known_incomplete_output_budget import SaturatedGateway
from tests.unit.runtime.crew.test_known_incomplete_recovery import (
    collect,
)
from tests.unit.runtime.crew.test_known_incomplete_recovery import (
    plan as ordinary_plan,
)
from tests.unit.runtime.crew.test_known_incomplete_recovery import (
    runtime as ordinary_runtime,
)
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping
from tests.unit.runtime.crew.test_tool_incomplete_recovery import (
    ToolIncompleteGateway,
    draft_models,
    subject,
    tool_plan,
)


class TwiceSaturatedGateway(ToolIncompleteGateway):
    def __init__(
        self, *, usage_status: RejectedUsageStatus = "known", hard_limit: int | None = None
    ) -> None:
        super().__init__("incomplete", "incomplete", "tool", "success", "final")
        self.usage_status = usage_status
        self.deployment = Deployment(
            id="selected", logical_model="selected", max_output_tokens=hard_limit,
            capabilities=frozenset({ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT,
                                    ModelCapability.TOOL_CALLING}),
        )
        self._registry = ModelRegistry([self.deployment])

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        request = ModelGateway._request_for_deployment(request, self.deployment, None)
        try:
            return await super().complete_with_context(request)
        except GatewayRejectedOutput as error:
            assert error.evidence is not None
            if error.evidence.usage_status == "known":
                usage = (TokenUsage(2226, 6144, 8370), TokenUsage(2346, 12288, 14634))[
                    len(self.requests) - 1
                ]
                error.evidence = replace(error.evidence, usage=usage)
            raise


class TwiceSaturatedStructuredGateway(SaturatedGateway):
    def __init__(self) -> None:
        super().__init__("incomplete", "incomplete", "success", "final")

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        try:
            return await super().complete_with_context(request)
        except GatewayRejectedOutput as error:
            assert error.evidence is not None
            usage = (TokenUsage(2226, 6144, 8370), TokenUsage(2346, 12288, 14634))[
                len(self.requests) - 1
            ]
            error.evidence = replace(error.evidence, usage=usage)
            raise


async def test_fresh_run_with_budget_recovers_twice_without_tools() -> None:
    gateway = TwiceSaturatedStructuredGateway()
    runtime = ordinary_runtime(gateway, ordinary_plan(tokens=100000))
    events = []
    failure = None
    try:
        events = await collect(runtime, tokens=100000)
    except RuntimeExecutionError as error:
        failure = error
    saved = await runtime.save_checkpoint()
    rows = draft_models(saved)
    assert [request.max_output_tokens for request in gateway.requests[:2]] == [6144, 12288]
    assert [row["status"] for row in rows[:2]] == ["rejected", "rejected"]
    assert [row["failure_reason"] for row in rows[:2]] == [
        "model response incomplete",
        "model response incomplete",
    ]
    assert all(row["actor"] == "writer" for row in rows)
    assert saved.state["incomplete_recovery_output_growth"] is True
    assert saved.state["tools"] == {} and saved.state["structured_repairs"] == {}
    if failure is not None:
        assert str(failure) == "model response incomplete"
        assert mapping(saved.state["usage"])["tokens"] == 23004
        assert mapping(saved.state["usage"])["cost_usd"] == "0.02"
    assert [request.max_output_tokens for request in gateway.requests[:3]] == [
        6144,
        12288,
        24576,
    ], f"tool-free known-incomplete chain stopped after {len(gateway.requests)} calls: {failure}"
    assert failure is None and events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 4
    assert all(request.tools == () for request in gateway.requests)
    original, *recoveries = gateway.requests[:3]
    assert all(request.response_schema == original.response_schema for request in recoveries)
    assert all(
        request.logical_model == "selected" and not request.allow_fallback for request in recoveries
    )
    assert all(request.timeout_seconds <= original.timeout_seconds for request in recoveries)
    assert mapping(saved.state["usage"])["tokens"] == 23032
    assert mapping(saved.state["usage"])["cost_usd"] == "0.04"


async def test_fresh_run_with_budget_recovers_twice_before_one_tool_execution() -> None:
    gateway = TwiceSaturatedGateway()
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(100000), InMemoryArtifactRepository(), harness)
    events = []
    failure = None
    try:
        events = await collect(runtime, tokens=100000)
    except RuntimeExecutionError as error:
        failure = error
    saved = await runtime.save_checkpoint()
    rows = draft_models(saved)
    assert [request.max_output_tokens for request in gateway.requests[:2]] == [6144, 12288]
    assert [row["status"] for row in rows[:2]] == ["rejected", "rejected"]
    assert all(row["actor"] == "writer" for row in rows)
    assert saved.state["incomplete_recovery_output_growth"] is True
    assert saved.state["first_tool_incomplete_recovery_output_growth"] is True
    assert saved.state["incomplete_continuation_output_growth"] is True
    if failure is not None:
        assert str(failure) == "structured output invalid"
        assert mapping(saved.state["usage"])["tokens"] == 23004
        assert mapping(saved.state["usage"])["cost_usd"] == "0.02"
        assert saved.state["tools"] == {} and harness.calls == []
    assert [request.max_output_tokens for request in gateway.requests[:3]] == [
        6144,
        12288,
        24576,
    ], (
        f"known usage 23004 leaves ample budget, but stopped after {len(gateway.requests)} calls: "
        f"{failure}"
    )
    assert failure is None and events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 5 and len(harness.calls) == 1
    assert gateway.requests[3].max_output_tokens == 24576
    original, *recoveries = gateway.requests[:4]
    assert all(request.response_schema == original.response_schema for request in recoveries)
    assert all(request.tools == original.tools for request in recoveries)
    assert all(
        request.logical_model == "selected" and not request.allow_fallback for request in recoveries
    )
    assert all(request.timeout_seconds <= original.timeout_seconds for request in recoveries)
    assert mapping(saved.state["usage"])["tokens"] == 23049
    assert mapping(saved.state["usage"])["cost_usd"] == "0.05"
    assert saved.state["structured_repairs"] == {}


@pytest.mark.parametrize("usage_status", ["missing", "invalid"])
async def test_unknown_usage_stops_before_any_recovery(usage_status: RejectedUsageStatus) -> None:
    gateway = TwiceSaturatedGateway(usage_status=usage_status)
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(100000), InMemoryArtifactRepository(), harness)
    await collect(runtime, tokens=100000, fail="usage unaccounted")
    saved = await runtime.save_checkpoint()
    assert len(gateway.requests) == 1 and harness.calls == []
    assert saved.state["phase"] == "unaccounted"
    assert saved.state["tools"] == {} and saved.state["structured_repairs"] == {}


@pytest.mark.parametrize("scope", ["run", "step", "cost"])
async def test_insufficient_remaining_budget_never_buys_a_third_call(scope: str) -> None:
    dispatch = tool_plan(29004 if scope == "run" else 100000)
    if scope in {"step", "cost"}:
        dispatch = dispatch.model_copy(
            update={
                "steps": (
                    dispatch.steps[0].model_copy(
                        update={
                            "token_budget": 29004 if scope == "step" else 100000,
                            "cost_budget_usd": Decimal("0.02") if scope == "cost" else Decimal(1),
                        }
                    ),
                    *dispatch.steps[1:],
                )
            }
        )
    gateway = TwiceSaturatedGateway()
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    await collect(
        runtime,
        tokens=dispatch.total_token_budget,
        fail="structured output invalid|budget exhausted",
    )
    saved = await runtime.save_checkpoint()
    assert [request.max_output_tokens for request in gateway.requests] == [6144, 12288]
    assert mapping(saved.state["usage"])["tokens"] == 23004
    assert mapping(saved.state["usage"])["cost_usd"] == "0.02"
    assert harness.calls == [] and saved.state["tools"] == {}
    assert saved.state["structured_repairs"] == {}


async def test_short_original_deadline_never_buys_recovery() -> None:
    base = tool_plan(100000)
    dispatch = base.model_copy(
        update={
            "steps": (
                base.steps[0].model_copy(update={"timeout_seconds": 1}),
                *base.steps[1:],
            )
        }
    )
    gateway = TwiceSaturatedGateway()
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    await collect(runtime, tokens=100000, fail="model response incomplete")
    saved = await runtime.save_checkpoint()
    assert len(gateway.requests) == 1 and harness.calls == []
    assert mapping(saved.state["usage"])["tokens"] == 8370
    assert saved.state["tools"] == {} and saved.state["structured_repairs"] == {}


async def test_explicit_physical_limit_cannot_purchase_another_effectively_equal_cap() -> None:
    gateway = TwiceSaturatedGateway(hard_limit=12288)
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(100000), InMemoryArtifactRepository(), harness)
    await collect(runtime, tokens=100000, fail="structured output invalid|incomplete")
    saved = await runtime.save_checkpoint()
    assert [request.max_output_tokens for request in gateway.requests] == [6144, 12288]
    # A doubled logical request would still hit the same explicit physical cap.
    proposed = replace(gateway.requests[-1], max_output_tokens=24576)
    effective = ModelGateway._request_for_deployment(proposed, gateway.deployment, None)
    assert effective.max_output_tokens == 12288
    assert mapping(saved.state["usage"])["tokens"] == 23004
    assert harness.calls == [] and saved.state["tools"] == {}
