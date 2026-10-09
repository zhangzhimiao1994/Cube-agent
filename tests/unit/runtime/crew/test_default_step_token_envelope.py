"""Offline regressions for generated step envelopes and unchanged hard limits."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Mapping
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep, InvalidDispatchPlan
from agent_hub.runtime.defaults import _dispatch_plan, _runtime_plan_context
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose
from tests.unit.runtime.crew.test_adapter_failure_reason import FastFactory, RoleAwareGateway


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch, _function_scoped_runner: asyncio.Runner) -> None:
    _function_scoped_runner.get_loop()

    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("owned budget fixture cannot dispatch network traffic")

    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def context(
    tokens: int = 3_000_000,
    *,
    plan_tokens: int = 10_000_000,
    scale: str = "medium",
    checkpoint: RuntimeCheckpoint | None = None,
) -> TaskContext:
    return TaskContext(
        run_id=UUID(int=1),
        tenant_id=UUID(int=2),
        mode=TaskMode.DISPATCH,
        request="Summarize the owned fixture's verified results.",
        token_budget=tokens,
        routing_decision={
            "project_scale": scale,
            "flow": "multi_agent",
            "runtime_plan_token_budget": plan_tokens,
        },
        checkpoint=checkpoint,
    )


def generated_plan(task: TaskContext) -> DispatchPlan:
    roles = tuple(
        RoleAssignment(
            id=name,
            role=name,
            purpose=purpose,
            mission="Summarize owned evidence.",
            must_answer=("What is verified?",),
            allowed_tools=(),
            forbidden_actions=("Do not use external resources.",),
            skills=(),
            output_schema={"summary": "string"},
            model=name,
        )
        for name, purpose in (
            ("architect", RolePurpose.PLAN),
            ("implementer", RolePurpose.EXECUTE),
            ("tester", RolePurpose.VERIFY),
        )
    )
    return _dispatch_plan(roles, _runtime_plan_context(task))


def explicit_plan(
    *,
    step_tokens: int = 4096,
    total_tokens: int = 10_000_000,
    two_steps: bool = False,
    step_cost: Decimal = Decimal(0),
) -> DispatchPlan:
    first = DispatchStep(
        id="draft",
        agent="writer",
        task="Summarize owned evidence.",
        token_budget=step_tokens,
        cost_budget_usd=step_cost,
    )
    final = DispatchStep(
        id="final",
        agent="writer",
        task="Return the verified answer.",
        depends_on=("draft",) if two_steps else (),
        final_synthesizer=True,
        token_budget=step_tokens,
        cost_budget_usd=step_cost,
    )
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer", role="writer", goal="Summarize", logical_model="writer",
                output_schema={"summary": "string"},
            ),
        ),
        steps=(first, final) if two_steps else (final,),
        total_token_budget=total_tokens,
        total_cost_usd=step_cost * (2 if two_steps else 1),
    )


class UsageGateway(RoleAwareGateway):
    def __init__(self, tokens: tuple[int, ...], *, cost: Decimal = Decimal(0)) -> None:
        super().__init__()
        self.tokens = tokens
        self.cost = cost

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        index = len(self.requests)
        assert index < len(self.tokens), "unexpected additional model call"
        completed = await super().complete_with_context(request)
        total = self.tokens[index]
        return GatewayCompletion(
            response=ModelResponse(text=completed.response.text, usage=TokenUsage(total - 1, 1, total)),
            deployment_id="owned-deployment",
            logical_model=request.logical_model,
            provider_id="owned-provider",
            provider_model="owned-provider/owned-model",
            cost_usd=self.cost,
        )


@pytest.mark.parametrize("scale", ("small", "medium", "large", "ultra"))
@pytest.mark.parametrize("envelope", (3_000_000, 10_000_000))
def test_generated_steps_inherit_the_legal_plan_envelope(scale: str, envelope: int) -> None:
    task = context(plan_tokens=envelope, scale=scale)
    plan = generated_plan(task)

    assert plan.total_token_budget == envelope
    assert len(plan.steps) == 4
    assert {step.token_budget for step in plan.steps} == {envelope}
    assert sum(step.token_budget for step in plan.steps) > plan.total_token_budget
    assert {step.cost_budget_usd for step in plan.steps} == {Decimal(10)}
    assert plan.total_cost_usd == Decimal(40)
    assert task.token_budget == 3_000_000
    assert DispatchPlan.from_payload(plan.to_payload()) == plan
    assert DispatchPlan.revalidate(plan).digest == plan.digest


def test_generated_plan_without_anchor_uses_the_active_token_envelope() -> None:
    task = context().model_copy(update={"routing_decision": {"project_scale": "medium"}})
    plan = generated_plan(task)

    assert plan.total_token_budget == 3_000_000
    assert {step.token_budget for step in plan.steps} == {3_000_000}


async def test_generated_implementer_can_cross_the_old_one_million_cap_and_restore_without_calls() -> None:
    task = context()
    plan = generated_plan(task)
    gateway = UsageGateway((2, 1_007_665, 2, 2))
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )

    events = [event async for event in runtime.run(task)]
    checkpoint = await runtime.save_checkpoint()

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert checkpoint.state["usage"] == {"tokens": 1_007_671, "cost_usd": "0"}
    step_usage = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["step_usage"])
    assert step_usage["implementer_step"] == {
        "tokens": 1_007_665, "cost_usd": "0",
    }
    assert len(gateway.requests) == 4
    restored_gateway = UsageGateway(())
    restored = CrewDispatchRuntime(
        restored_gateway, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )
    await restored.restore_checkpoint(checkpoint)
    restored_events = [event async for event in restored.run(context(checkpoint=checkpoint))]
    assert [event.kind for event in restored_events] == [EventKind.RUNTIME_COMPLETED]
    assert restored_gateway.requests == []
    assert checkpoint.state["usage"] == {"tokens": 1_007_671, "cost_usd": "0"}


@pytest.mark.parametrize("budget", (1, 4096, 1_000_001, 3_000_000, 10_000_000))
def test_step_schema_accepts_only_bounded_integer_token_envelopes(budget: int) -> None:
    plan = explicit_plan(step_tokens=budget)

    assert plan.final_step.token_budget == budget
    assert DispatchPlan.from_payload(plan.to_payload()).final_step.token_budget == budget
    assert DispatchPlan.revalidate(plan).digest == plan.digest


@pytest.mark.parametrize("budget", (True, False, 0, -1, 10_000_001, 4096.0, "4096"))
def test_step_schema_denies_bool_invalid_range_and_coercion(budget: object) -> None:
    payload = explicit_plan().to_payload()
    steps = cast(list[dict[str, object]], payload["steps"])
    steps[0]["token_budget"] = budget

    with pytest.raises(InvalidDispatchPlan):
        DispatchPlan.from_payload(payload)
    with pytest.raises(ValidationError):
        DispatchStep.model_validate(
            {"id": "final", "agent": "writer", "task": "Answer", "token_budget": budget},
        )


def test_larger_step_schema_does_not_expand_single_response_output_limit() -> None:
    with pytest.raises(ValidationError):
        AgentSpec(
            id="writer", role="writer", goal="Summarize", logical_model="writer",
            max_output_tokens=1_000_001,
        )


def test_step_envelope_still_cannot_exceed_the_plan_total() -> None:
    with pytest.raises(ValidationError, match="total token budget is insufficient"):
        explicit_plan(step_tokens=3_000_000, total_tokens=2_000_000)


@pytest.mark.parametrize(
    "step_tokens,plan_tokens,active_tokens,two_steps,usage",
    (
        (4096, 10_000_000, 3_000_000, False, (4097,)),
        (10_000_000, 10_000_000, 4096, False, (4097,)),
        (4096, 4096, 10_000_000, True, (2048, 2049)),
    ),
    ids=("explicit-step-hard", "active-context-hard", "cumulative-plan-hard"),
)
async def test_ledger_limits_remain_hard_and_terminal_restore_never_rebills(
    step_tokens: int,
    plan_tokens: int,
    active_tokens: int,
    two_steps: bool,
    usage: tuple[int, ...],
) -> None:
    plan = explicit_plan(step_tokens=step_tokens, total_tokens=plan_tokens, two_steps=two_steps)
    gateway = UsageGateway(usage)
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )

    with pytest.raises(RuntimeExecutionError, match="dispatch budget exhausted"):
        async for _event in runtime.run(context(active_tokens, plan_tokens=plan_tokens)):
            pass
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["phase"] == "budget_exhausted"
    assert checkpoint.state["terminal"] is True
    assert checkpoint.state["usage"] == {"tokens": 4097, "cost_usd": "0"}
    assert len(gateway.requests) == len(usage)
    restored_gateway = UsageGateway(())
    restored = CrewDispatchRuntime(
        restored_gateway, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )
    await restored.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match="dispatch budget exhausted"):
        async for _event in restored.run(
            context(active_tokens, plan_tokens=plan_tokens, checkpoint=checkpoint),
        ):
            pass
    assert restored_gateway.requests == []
    assert checkpoint.state["usage"] == {"tokens": 4097, "cost_usd": "0"}


async def test_explicit_step_budget_at_equality_is_not_automatically_raised() -> None:
    plan = explicit_plan()
    gateway = UsageGateway((4096,))
    runtime = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())

    events = [event async for event in runtime.run(context())]
    checkpoint = await runtime.save_checkpoint()

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert plan.final_step.token_budget == 4096
    assert checkpoint.state["usage"] == {"tokens": 4096, "cost_usd": "0"}


async def test_cost_boundary_remains_hard_with_a_larger_token_envelope() -> None:
    plan = explicit_plan(step_tokens=10_000_000, step_cost=Decimal("0.1"))
    gateway = UsageGateway((2,), cost=Decimal("0.2"))
    runtime = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())

    with pytest.raises(RuntimeExecutionError, match="dispatch budget exhausted"):
        async for _event in runtime.run(context()):
            pass
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["phase"] == "budget_exhausted"
    assert checkpoint.state["usage"] == {"tokens": 2, "cost_usd": "0.2"}
    assert len(gateway.requests) == 1
