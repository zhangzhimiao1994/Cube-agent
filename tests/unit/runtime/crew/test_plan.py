from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_hub.runtime.crew.plan import (
    AgentSpec,
    DispatchPlan,
    DispatchStep,
    InvalidDispatchPlan,
)


def agent(
    agent_id: str,
    *,
    tools: tuple[str, ...] = (),
    output_schema: dict[str, str] | None = None,
) -> AgentSpec:
    return AgentSpec(
        id=agent_id,
        role=agent_id,
        goal=f"Goal for {agent_id}",
        logical_model="general",
        allowed_tools=tools,
        output_schema=output_schema or {},
    )


def test_agent_role_is_display_text_while_machine_fields_stay_safe_identifiers() -> None:
    spec = AgentSpec(
        id="director",
        role="导演",
        goal="负责拆解目标、镜头语言和最终质量把关。",
        logical_model="general",
    )

    assert spec.role == "导演"
    assert AgentSpec(
        id="final_synthesizer",
        role="Final Synthesizer",
        goal="Merge role outputs.",
        logical_model="general",
    ).role == "Final Synthesizer"
    with pytest.raises(ValidationError, match="agent identifier"):
        AgentSpec(
            id="导演",
            role="导演",
            goal="Invalid machine id",
            logical_model="general",
        )
    with pytest.raises(ValidationError, match="agent identifier"):
        AgentSpec(
            id="director",
            role="导演",
            goal="Invalid logical model",
            logical_model="main model",
        )


def test_valid_plan_has_deterministic_layers_and_round_trip() -> None:
    plan = DispatchPlan(
        agents=(
            agent(
                "researcher",
                tools=("web.search",),
                output_schema={"summary": "string"},
            ),
            agent("writer"),
        ),
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


@pytest.mark.parametrize(
    "budget,serialized",
    [
        (10, "10"),
        (100, "100"),
        (1000, "1000"),
        (1000000, "1000000"),
    ],
)
def test_integer_decimal_budgets_round_trip_without_exponent_text(
    budget: int,
    serialized: str,
) -> None:
    base_payload = DispatchPlan(
        agents=(agent("writer"),),
        steps=(DispatchStep(id="final", agent="writer", task="Write answer", final_synthesizer=True),),
    ).to_payload()
    steps = base_payload["steps"]
    assert isinstance(steps, list)
    payload = dict(base_payload)
    payload["steps"] = [dict(steps[0], cost_budget_usd=budget)]
    payload["total_cost_usd"] = budget

    plan = DispatchPlan.from_payload(payload)

    serialized_payload = plan.to_payload()
    round_tripped = DispatchPlan.from_payload(serialized_payload)
    revalidated = DispatchPlan.revalidate(plan)

    assert serialized_payload["total_cost_usd"] == serialized
    assert serialized_payload["steps"][0]["cost_budget_usd"] == serialized
    assert round_tripped == plan
    assert DispatchPlan.from_payload(round_tripped.to_payload()) == round_tripped
    assert revalidated == plan
    assert revalidated.to_payload() == serialized_payload
    assert round_tripped.digest == plan.digest


@pytest.mark.parametrize("value", ["1E+1", "1e1", "NaN", "Infinity", "-Infinity", True])
def test_payload_decimal_budgets_reject_exponents_non_finite_and_bool(value: object) -> None:
    valid = DispatchPlan(
        agents=(agent("writer"),),
        steps=(DispatchStep(id="final", agent="writer", task="Write answer", final_synthesizer=True),),
    ).to_payload()

    invalid_total = dict(valid)
    invalid_total["total_cost_usd"] = value
    with pytest.raises(InvalidDispatchPlan):
        DispatchPlan.from_payload(invalid_total)

    invalid_step = dict(valid)
    invalid_step["steps"] = [dict(valid["steps"][0], cost_budget_usd=value)]
    with pytest.raises(InvalidDispatchPlan):
        DispatchPlan.from_payload(invalid_step)


def test_handoff_source_steps_require_structured_output_schema() -> None:
    with pytest.raises((InvalidDispatchPlan, ValidationError), match="output_schema"):
        DispatchPlan(
            agents=(agent("researcher"), agent("writer")),
            steps=(
                DispatchStep(
                    id="research",
                    agent="researcher",
                    task="Research facts",
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
            total_token_budget=200,
        )


def test_revalidate_rejects_constructed_handoff_source_without_output_schema() -> None:
    unsafe = DispatchPlan.model_construct(
        agents=(agent("researcher"), agent("writer")),
        steps=(
            DispatchStep(
                id="research",
                agent="researcher",
                task="Research facts",
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
        allowed_tools=(),
        denied_tools=(),
        max_steps=64,
        max_parallelism=4,
        total_token_budget=200,
        total_timeout_seconds=3600.0,
        total_cost_usd=Decimal(0),
    )

    with pytest.raises(InvalidDispatchPlan):
        DispatchPlan.revalidate(unsafe)


def test_single_final_step_can_use_plain_text_without_output_schema() -> None:
    plan = DispatchPlan(
        agents=(agent("writer"),),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Write answer",
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        total_token_budget=100,
    )

    assert plan.final_step.id == "final"


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
    "source",
    (
        "agents: []\nagents: []\nsteps: []",
        "agents:\n  - id: writer\n    id: duplicate\nsteps: []",
        "agents: []\nsteps: []\npolicy:\n  safe: true\n  safe: false",
    ),
)
def test_yaml_rejects_duplicate_keys_at_every_depth(source: str) -> None:
    with pytest.raises(InvalidDispatchPlan):
        DispatchPlan.from_yaml(source)


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
def test_invalid_graphs_are_rejected(steps: tuple[DispatchStep, ...], message: str) -> None:
    with pytest.raises((InvalidDispatchPlan, ValidationError), match=message):
        DispatchPlan(
            agents=(agent("x", output_schema={"summary": "string"}),),
            steps=steps,
            total_token_budget=10_000,
        )


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
        "steps": (DispatchStep(id="final", agent="x", task="Final", final_synthesizer=True),),
        "total_token_budget": 4096,
        field: value,
    }
    with pytest.raises((ValidationError, InvalidDispatchPlan)):
        DispatchPlan(**data)  # type: ignore[arg-type]


def test_plan_rejects_unknown_agent_budget_overflow_and_construct_bypass() -> None:
    with pytest.raises((InvalidDispatchPlan, ValidationError), match="agent"):
        DispatchPlan(
            agents=(agent("x"),),
            steps=(DispatchStep(id="final", agent="y", task="Final", final_synthesizer=True),),
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


def test_plan_allows_shared_total_token_budget_across_sequential_steps() -> None:
    plan = DispatchPlan(
        agents=(agent("x", output_schema={"summary": "string"}),),
        steps=(
            DispatchStep(id="draft", agent="x", task="Draft", token_budget=100),
            DispatchStep(
                id="final",
                agent="x",
                task="Final",
                depends_on=("draft",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        total_token_budget=100,
    )

    assert plan.total_token_budget == 100


def test_plan_rejects_aggregate_oversized_text() -> None:
    with pytest.raises(ValidationError, match="size"):
        DispatchPlan(
            agents=(agent("x", output_schema={"summary": "string"}),),
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
