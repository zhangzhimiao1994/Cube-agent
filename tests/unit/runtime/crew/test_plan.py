from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_hub.runtime.crew.plan import (
    AgentSpec,
    DispatchPlan,
    DispatchStep,
    InvalidDispatchPlan,
)


def agent(agent_id: str, *, tools: tuple[str, ...] = ()) -> AgentSpec:
    return AgentSpec(
        id=agent_id,
        role=agent_id,
        goal=f"Goal for {agent_id}",
        logical_model="general",
        allowed_tools=tools,
    )


def test_valid_plan_has_deterministic_layers_and_round_trip() -> None:
    plan = DispatchPlan(
        agents=(agent("researcher", tools=("web.search",)), agent("writer")),
        steps=(
            DispatchStep(
                id="research",
                agent="researcher",
                task="Research facts",
                tools=("web.search",),
                token_budget=100,
            ),
            DispatchStep(
                id="write",
                agent="writer",
                task="Write answer",
                depends_on=("research",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=200,
    )

    assert plan.layers == (("research",), ("write",))
    assert plan.final_step.id == "write"
    assert DispatchPlan.from_payload(plan.to_payload()) == plan
    assert len(plan.digest) == 64


def test_fixed_yaml_plan_loads_and_yaml_aliases_are_rejected() -> None:
    loaded = DispatchPlan.from_yaml(
        """
agents:
  - id: writer
    role: writer
    goal: Write safely
    logical_model: general
steps:
  - id: final
    agent: writer
    task: Produce the final result
    final_synthesizer: true
    token_budget: 100
total_token_budget: 100
"""
    )
    assert loaded.final_step.id == "final"
    with pytest.raises(InvalidDispatchPlan):
        DispatchPlan.from_yaml("agents: &agents []\nsteps: *agents")


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        (
            (
                DispatchStep(id="a", agent="x", task="A", depends_on=("b",)),
                DispatchStep(
                    id="b",
                    agent="x",
                    task="B",
                    depends_on=("a",),
                    final_synthesizer=True,
                ),
            ),
            "cycle",
        ),
        (
            (
                DispatchStep(id="a", agent="x", task="A"),
                DispatchStep(
                    id="b",
                    agent="x",
                    task="B",
                    depends_on=("missing",),
                    final_synthesizer=True,
                ),
            ),
            "dependency",
        ),
        (
            (
                DispatchStep(id="a", agent="x", task="A"),
                DispatchStep(id="b", agent="x", task="B", final_synthesizer=True),
            ),
            "cover",
        ),
    ],
)
def test_invalid_graphs_are_rejected(
    steps: tuple[DispatchStep, ...], message: str
) -> None:
    with pytest.raises((InvalidDispatchPlan, ValidationError), match=message):
        DispatchPlan(agents=(agent("x"),), steps=steps, total_token_budget=10_000)


def test_tool_permission_is_intersection_and_dangerous_tools_are_rejected() -> None:
    with pytest.raises((InvalidDispatchPlan, ValidationError), match="tool"):
        DispatchPlan(
            agents=(agent("x", tools=("web.search", "shell.exec")),),
            steps=(
                DispatchStep(
                    id="final",
                    agent="x",
                    task="Unsafe",
                    tools=("shell.exec",),
                    final_synthesizer=True,
                ),
            ),
            allowed_tools=("web.search",),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_parallelism", True),
        ("total_token_budget", True),
        ("total_cost_usd", "NaN"),
        ("total_cost_usd", "0.0000001"),
    ],
)
def test_strict_bounded_numbers(field: str, value: object) -> None:
    data: dict[str, object] = {
        "agents": (agent("x"),),
        "steps": (
            DispatchStep(id="final", agent="x", task="Final", final_synthesizer=True),
        ),
        "total_token_budget": 4096,
        field: value,
    }
    with pytest.raises((ValidationError, InvalidDispatchPlan)):
        DispatchPlan(**data)  # type: ignore[arg-type]


def test_plan_rejects_unknown_agent_budget_overflow_and_construct_bypass() -> None:
    with pytest.raises((InvalidDispatchPlan, ValidationError), match="agent"):
        DispatchPlan(
            agents=(agent("x"),),
            steps=(
                DispatchStep(
                    id="final", agent="y", task="Final", final_synthesizer=True
                ),
            ),
        )
    with pytest.raises((InvalidDispatchPlan, ValidationError), match="budget"):
        DispatchPlan(
            agents=(agent("x"),),
            steps=(
                DispatchStep(
                    id="final",
                    agent="x",
                    task="Final",
                    final_synthesizer=True,
                    token_budget=101,
                ),
            ),
            total_token_budget=100,
        )

    unsafe = DispatchPlan.model_construct(
        agents=(agent("x"),),
        steps=(
            DispatchStep.model_construct(
                id="../secret",
                agent="x",
                task="bad",
                depends_on=(),
                tools=(),
                reviewer=None,
                reviewer_retries=0,
                final_synthesizer=True,
                token_budget=1,
                timeout_seconds=1.0,
                cost_budget_usd=Decimal(0),
            ),
        ),
        allowed_tools=(),
        denied_tools=(),
        max_steps=64,
        max_parallelism=4,
        total_token_budget=10,
        total_timeout_seconds=60.0,
        total_cost_usd=Decimal(0),
    )
    with pytest.raises(InvalidDispatchPlan):
        DispatchPlan.revalidate(unsafe)


def test_plan_rejects_aggregate_oversized_text() -> None:
    with pytest.raises(ValidationError, match="size"):
        DispatchPlan(
            agents=(agent("x"),),
            steps=tuple(
                DispatchStep(
                    id=f"step-{index}",
                    agent="x",
                    task="x" * 5000,
                    depends_on=(() if index == 0 else (f"step-{index - 1}",)),
                    final_synthesizer=index == 63,
                    token_budget=1,
                )
                for index in range(64)
            ),
            total_token_budget=64,
            total_timeout_seconds=4000,
        )
