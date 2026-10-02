"""Output allowance regressions through real dispatch accounting and checkpoints."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any, cast

import pytest

from agent_hub.models.gateway import GatewayCompletion, GatewayRejectedOutput
from agent_hub.models.types import (
    ModelRequest,
    ModelResponse,
    RejectedOutputReason,
    RejectedUsageStatus,
    ToolCall,
)
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from agent_hub.runtime.crew.plan import DispatchPlan
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FastFactory,
    RecordingHarnessToolGateway,
    _context,
    _reviewed_step_plan,
)
from tests.unit.runtime.crew.test_structured_handoff_repair import (
    RepairCaptureGateway,
    assert_private_rejection,
)

TRUNCATED = '{"summary":"PRIVATE_TRUNCATED_STRING'
SCHEMA_MISMATCH = '{"summary":123,"private":"PRIVATE_SCHEMA_MISMATCH"}'
CORRECTED = '{"summary":"actual corrected candidate"}'
PRICE = Decimal("0.01")


def mapping(value: object) -> Mapping[str, Any]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, Any], value)


def budget_plan(*, output_limit: int = 4096, step_tokens: int = 20_000,
                total_tokens: int = 20_000) -> DispatchPlan:
    plan = _reviewed_step_plan()
    return plan.model_copy(update={
        "agents": tuple(agent.model_copy(update={"max_output_tokens": output_limit})
                        for agent in plan.agents),
        "steps": (
            plan.steps[0].model_copy(update={
                "token_budget": min(step_tokens, total_tokens), "cost_budget_usd": Decimal(1),
            }),
            plan.steps[1].model_copy(update={"cost_budget_usd": Decimal(1)}),
        ),
        "total_token_budget": total_tokens,
        "total_cost_usd": Decimal(3),
    })


class BudgetGateway(RepairCaptureGateway):
    def __init__(self, completion_tokens: int | None, *,
                 reason: RejectedOutputReason = "invalid_json",
                 usage_status: RejectedUsageStatus = "known",
                 native: bool = True, correction: str = CORRECTED,
                 source_cost: Decimal = PRICE) -> None:
        total = None if completion_tokens is None else completion_tokens + 1
        text = SCHEMA_MISMATCH if reason == "schema_mismatch" else TRUNCATED
        super().__init__(
            (text, total, native), (correction, 17, False),
            ('{"verdict":"approve"}', 8, False), ("actual final response", 11, False),
        )
        self.reason = reason
        self.usage_status = usage_status
        self.source_cost = source_cost

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        try:
            completion = await super().complete_with_context(request)
        except GatewayRejectedOutput as error:
            assert error.evidence is not None
            evidence = replace(
                error.evidence, reason=self.reason, usage_status=self.usage_status,
                usage=error.evidence.usage if self.usage_status == "known" else None,
            )
            raise GatewayRejectedOutput(
                evidence=evidence, deployment_id=error.deployment_id,
                logical_model=error.logical_model, provider_id=error.provider_id,
                provider_model=error.provider_model, cost_usd=self.source_cost,
            ) from None
        return replace(completion, cost_usd=self.source_cost if len(self.requests) == 1 else PRICE)


@pytest.mark.parametrize("native", [False, True], ids=["chat", "native"])
@pytest.mark.parametrize(
    "output_limit,completion_tokens,reason,expected",
    [
        (4096, 4096, "invalid_json", 8192),
        (4096, 4094, "invalid_json", 8192),
        (4096, 4056, "invalid_json", 8192),
        (4096, 4055, "invalid_json", 4096),
        (4096, 16, "invalid_json", 4096),
        (4096, 4096, "schema_mismatch", 4096),
        (128, 127, "invalid_json", 256),
        (128, 126, "invalid_json", 128),
    ],
    ids=["full", "production-4094", "threshold", "below-threshold", "low-usage",
         "schema-mismatch", "small-threshold", "below-small-threshold"],
)
async def test_only_known_invalid_json_near_output_limit_expands_existing_correction(
    native: bool, output_limit: int, completion_tokens: int,
    reason: RejectedOutputReason, expected: int,
) -> None:
    gateway = BudgetGateway(completion_tokens, reason=reason, native=native)
    runtime = CrewDispatchRuntime(
        gateway, budget_plan(output_limit=output_limit),
        artifact_repository=InMemoryArtifactRepository(), crew_factory=FastFactory(),
    )
    events = [event async for event in runtime.run(_context(token_budget=20_000))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 4
    original, correction = gateway.requests[:2]
    assert original.max_output_tokens == output_limit
    assert correction.max_output_tokens == expected
    assert correction.response_schema == original.response_schema
    assert correction.logical_model == original.logical_model
    assert correction.tools == () and correction.allow_fallback is False
    assert any("Correct only the JSON format" in str(message.content)
               and "Do not invent" in str(message.content) for message in correction.messages)
    checkpoint = await runtime.save_checkpoint()
    link = mapping(mapping(checkpoint.state["structured_repairs"])["draft"])
    assert link["max_output_tokens"] == expected
    assert mapping(checkpoint.state["usage"])["tokens"] == completion_tokens + 1 + 17 + 8 + 11
    assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.04"
    assert_private_rejection(events, TRUNCATED if reason == "invalid_json" else SCHEMA_MISMATCH)


@pytest.mark.parametrize("usage_status", ["missing", "invalid"])
async def test_unknown_usage_retains_receipt_without_paid_correction(
    usage_status: RejectedUsageStatus,
) -> None:
    gateway = BudgetGateway(None, usage_status=usage_status)
    runtime = CrewDispatchRuntime(gateway, budget_plan(), crew_factory=FastFactory())
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(_context(token_budget=20_000))]
    assert len(gateway.requests) == 1
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["phase"] == "unaccounted"
    assert checkpoint.state["structured_repairs"] == {}
    assert len(mapping(checkpoint.state["rejected_outputs"])) == 1


@pytest.mark.parametrize("binding_budget", ["step", "plan", "context"])
@pytest.mark.parametrize("remaining,expected", [(6000, 6000), (3000, 3000)])
async def test_expansion_clamps_to_remaining_budget_after_source_accounting(
    binding_budget: str, remaining: int, expected: int,
) -> None:
    bound = 4097 + remaining
    gateway = BudgetGateway(4096)
    plan = budget_plan(step_tokens=bound if binding_budget == "step" else 20_000,
                       total_tokens=bound if binding_budget == "plan" else 20_000)
    runtime = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())
    events = [event async for event in runtime.run(
        _context(
            token_budget=bound if binding_budget == "context" else 20_000,
            routing_decision={"runtime_plan_token_budget": plan.total_token_budget},
        ),
    )]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 4
    assert gateway.requests[0].max_output_tokens == 4096
    assert gateway.requests[1].max_output_tokens == expected
    checkpoint = await runtime.save_checkpoint()
    assert mapping(checkpoint.state["usage"])["tokens"] == 4133
    assert mapping(mapping(checkpoint.state["structured_repairs"])["draft"])[
        "max_output_tokens"
    ] == expected


@pytest.mark.parametrize("phase", ["prepared", "running", "succeeded"])
async def test_legacy_cached_repair_preserves_output_limit_hash_and_accounting(phase: str) -> None:
    repository = InMemoryArtifactRepository()
    captured = BudgetGateway(4096)
    plan = budget_plan()
    runtime = CrewDispatchRuntime(
        captured, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )
    events = [event async for event in runtime.run(_context(token_budget=20_000))]
    checkpoint = next(
        event.checkpoint for event in events
        if event.checkpoint is not None
        and "draft" in mapping(event.checkpoint.state["structured_repairs"])
        and mapping(mapping(event.checkpoint.state["models"])[
            mapping(mapping(event.checkpoint.state["structured_repairs"])["draft"])[
                "correction_key"
            ]
        ])["status"] == phase
    )
    # Build a coherent legacy reservation, made before adaptive expansion existed.
    payload = cast(dict[str, Any], checkpoint.to_payload())
    link = payload["state"]["structured_repairs"]["draft"]
    legacy_request = replace(captured.requests[1], max_output_tokens=4096)
    legacy_sha = CrewDispatchRuntime._model_request_sha256(legacy_request)
    link["max_output_tokens"] = 4096
    link["correction_request_sha256"] = legacy_sha
    payload["state"]["models"][link["correction_key"]]["request_sha256"] = legacy_sha
    payload["state_sha256"] = ""
    legacy = RuntimeCheckpoint.from_payload(payload)
    before = legacy.to_payload()
    replies: tuple[tuple[str, int, bool], ...] = (
        ((CORRECTED, 17, False),) if phase == "prepared" else ()
    )
    if phase != "running":
        replies += (('{"verdict":"approve"}', 8, False), ("actual final response", 11, False))
    gateway = RepairCaptureGateway(*replies)
    replay = CrewDispatchRuntime(
        gateway, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )
    await replay.restore_checkpoint(legacy)
    if phase == "running":
        with pytest.raises(RuntimeExecutionError, match="model outcome requires confirmation"):
            _ = [event async for event in replay.run(_context(token_budget=20_000, checkpoint=legacy))]
        assert gateway.requests == []
    else:
        replay_events = [event async for event in replay.run(
            _context(token_budget=20_000, checkpoint=legacy),
        )]
        assert replay_events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(gateway.requests) == (3 if phase == "prepared" else 2)
        if phase == "prepared":
            assert gateway.requests[0].max_output_tokens == 4096
            assert CrewDispatchRuntime._model_request_sha256(gateway.requests[0]) == legacy_sha
        restored = await replay.save_checkpoint()
        restored_link = mapping(mapping(restored.state["structured_repairs"])["draft"])
        assert restored_link["max_output_tokens"] == 4096
        assert restored_link["correction_request_sha256"] == legacy_sha
        assert mapping(restored.state["models"])[link["source_key"]] == mapping(
            legacy.state["models"]
        )[link["source_key"]]
        assert restored.state["rejected_outputs"] == legacy.state["rejected_outputs"]
        assert mapping(restored.state["usage"])["tokens"] == 4133
    assert legacy.to_payload() == before


@pytest.mark.parametrize("tool_response", [False, True], ids=["invalid-json", "forbidden-tool"])
async def test_expanded_correction_failure_never_adds_paid_or_tool_calls(tool_response: bool) -> None:
    class FailedCorrectionGateway(BudgetGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            completion = await super().complete_with_context(request)
            if tool_response and len(self.requests) == 2:
                return replace(completion, response=ModelResponse(
                    text=None, usage=completion.response.usage,
                    tool_calls=(ToolCall(id="forbidden", name="read_context", arguments={}),),
                ))
            return completion

    gateway = FailedCorrectionGateway(4096, correction=TRUNCATED)
    harness = RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway, budget_plan(), crew_factory=FastFactory(), harness_tool_gateway=harness,
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="structured output invalid"):
        async for event in runtime.run(_context(token_budget=20_000)):
            events.append(event)
    assert len(gateway.requests) == 2
    assert gateway.requests[1].tools == () and gateway.requests[1].allow_fallback is False
    assert harness.calls == []
    assert not any(event.kind in {
        EventKind.STEP_COMPLETED, EventKind.REVIEW_COMPLETED, EventKind.STEP_RETRYING,
        EventKind.RUNTIME_COMPLETED,
    } or str(event.kind).startswith("tool.") for event in events)
    checkpoint = await runtime.save_checkpoint()
    assert len(mapping(checkpoint.state["models"])) == 2
    assert mapping(checkpoint.state["usage"])["tokens"] == 4114
    assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.02"
    assert len(mapping(checkpoint.state["structured_repairs"])) == 1
    assert_private_rejection(events, TRUNCATED)
    assert gateway.requests[1].max_output_tokens == 8192


@pytest.mark.parametrize("budget_scope", ["step", "plan"])
async def test_saturated_receipt_cannot_expand_when_cost_budget_is_exhausted(
    budget_scope: str,
) -> None:
    plan = budget_plan()
    plan = plan.model_copy(update={
        "total_cost_usd": Decimal("0.1") if budget_scope == "plan" else Decimal(2),
        "steps": tuple(step.model_copy(update={
            "cost_budget_usd": Decimal("0.1") if budget_scope == "step" and index == 0
            else Decimal(0),
        }) for index, step in enumerate(plan.steps)),
    })
    gateway = BudgetGateway(4096, source_cost=Decimal("0.1"))
    runtime = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())
    with pytest.raises(RuntimeExecutionError, match="budget exhausted"):
        _ = [event async for event in runtime.run(_context(token_budget=20_000))]
    assert len(gateway.requests) == 1
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["structured_repairs"] == {}
    assert mapping(checkpoint.state["usage"])["tokens"] == 4097
    assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.1"
