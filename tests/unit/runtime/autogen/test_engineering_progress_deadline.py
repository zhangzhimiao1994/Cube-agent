"""Real engineering work renews the soft runway, never the absolute fuse."""

import asyncio
from collections.abc import Mapping
from typing import cast

import pytest

from agent_hub.runtime.adaptive_budget import AdaptiveDeadline
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    CapabilityGateway,
    DiscussionParticipant,
    DiscussionPlan,
    RuntimeExecutionError,
    _DiscussionControl,
)
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import Artifact, JsonValue, RuntimeCheckpoint, TaskContext
from tests.unit.runtime.autogen.test_engineering_actions import action_context
from tests.unit.runtime.autogen.test_engineering_delivery import TOOLS, append_result, fixture
from tests.unit.runtime.autogen.test_model_milestones import Gateway

POLICY = "engineering_progress_budget_version"


@pytest.mark.parametrize("now,units,expected", [(400, 1, 700), (400, 20, 700),
    (449, 1, 749), (450, 1, 450), (3599, 1, 450)])
def test_work_renewal_is_a_window_not_a_per_file_multiplier(
    now: float, units: int, expected: float,
) -> None:
    budget = AdaptiveDeadline.create(now=0, initial_seconds=450, soft_seconds=300,
        absolute_seconds=3600, complexity_units=3)
    renew = getattr(budget, "renew_for_progress", None)
    assert callable(renew), "semantic progress runway must be distinct from role credits"
    renew(progress_units=units, now=now, runway_seconds=300)
    assert budget.deadline == expected
    assert budget.absolute_deadline == 3600


def test_work_renewal_never_moves_absolute_fuse_or_recredits_restored_work() -> None:
    budget = AdaptiveDeadline.create(now=3000, initial_seconds=450, soft_seconds=300,
        absolute_seconds=3600, restored_absolute_seconds=500, complexity_units=3, progress_units=3)
    renew = getattr(budget, "renew_for_progress", None)
    assert callable(renew)
    renew(progress_units=3, now=3400, runway_seconds=300)
    assert budget.deadline == 3450
    renew(progress_units=4, now=3400, runway_seconds=300)
    assert budget.deadline == budget.absolute_deadline == 3500
    renew(progress_units=5, now=3500, runway_seconds=300)
    assert budget.deadline == 3500


def configured(*, policy: JsonValue = 1, adaptive: bool = True) -> tuple[AutoGenDiscussionRuntime, TaskContext]:
    ctx = action_context()
    ctx = ctx.model_copy(update={"routing_decision": {**ctx.routing_decision, POLICY: policy,
        "runtime_timeout_source": "project_scale_soft_budget" if adaptive else "explicit",
        "runtime_timeout_soft_seconds": 300.0, "runtime_timeout_absolute_seconds": 3600.0}})
    plan = DiscussionPlan(participants=(DiscussionParticipant(
        id="engineer", role="Engineer", goal="Build", logical_model="shared",
        allowed_tools=TOOLS["engineer"],
    ), DiscussionParticipant(id="reviewer", role="Reviewer", goal="Review", logical_model="shared")),
        selector_model="shared", max_turns=16, token_budget=1000, wall_time_seconds=450)
    runtime = AutoGenDiscussionRuntime(Gateway(), plan, capability_gateway=cast(CapabilityGateway, object()))
    runtime._discussion_control = _DiscussionControl.create(plan, ctx, DiscussionUsage())
    now = asyncio.get_running_loop().time()
    runtime._wall_budget = AdaptiveDeadline.create(now=now - 400, initial_seconds=450,
        soft_seconds=300, absolute_seconds=3600 if adaptive else 450, complexity_units=3)
    runtime._wall_deadline = runtime._wall_budget.deadline
    return runtime, ctx


def checkpoint(runtime: AutoGenDiscussionRuntime, ctx: TaskContext,
               rows: list[dict[str, JsonValue]], artifacts: list[Artifact]) -> RuntimeCheckpoint:
    return runtime._checkpoint(ctx, next_sequence=1, terminal=False, reason=None,
        artifacts=tuple(artifacts), usage=DiscussionUsage(), tool_ledger=tuple(rows))


async def test_actual_write_renews_existing_soft_window_not_increment_per_role() -> None:
    runtime, ctx = configured()
    rows, artifacts = fixture()
    saved = checkpoint(runtime, ctx, rows[:1], artifacts[:1])
    remaining = saved.state["remaining_timeout_seconds"]
    assert isinstance(remaining, float) and 299 <= remaining <= 300
    assert saved.state[POLICY] == 1
    assert runtime._wall_budget is not None and runtime._wall_budget.progress_units == 1
    assert 3199 <= cast(float, saved.state["remaining_absolute_timeout_seconds"]) <= 3200


async def test_repeated_write_or_read_cannot_renew_countdown_again() -> None:
    runtime, ctx = configured()
    rows, artifacts = fixture()
    rows, artifacts = rows[:1], artifacts[:1]
    checkpoint(runtime, ctx, rows, artifacts)
    assert runtime._wall_budget is not None
    deadline = runtime._wall_budget.deadline
    result = artifacts[0].content["result"]
    assert isinstance(result, Mapping)
    append_result(rows, artifacts, "workspace.write_text", result)
    append_result(rows, artifacts, "workspace.read", {"text": "[COMPLETE] [EVIDENCE]"})
    checkpoint(runtime, ctx, rows, artifacts)
    assert runtime._wall_budget.deadline == deadline
    assert runtime._wall_budget.progress_units == 1


async def test_marker_text_does_not_buy_engineering_work_time() -> None:
    runtime, ctx = configured()
    assert runtime._discussion_control is not None and runtime._wall_budget is not None
    runtime._discussion_control.observe("engineer", "[EVIDENCE] claims of work")
    deadline = runtime._wall_budget.deadline
    saved = checkpoint(runtime, ctx, [], [])
    assert runtime._wall_budget.deadline == deadline
    assert runtime._wall_budget.progress_units == 0
    assert 49 <= cast(float, saved.state["remaining_timeout_seconds"]) <= 50


@pytest.mark.parametrize("policy", [None, True, "1", 2])
async def test_legacy_policy_keeps_existing_role_progress_accounting(policy: JsonValue) -> None:
    runtime, ctx = configured(policy=policy)
    assert runtime._discussion_control is not None and runtime._wall_budget is not None
    runtime._discussion_control.observe("engineer", "[EVIDENCE] legacy behavior")
    saved = checkpoint(runtime, ctx, [], [])
    assert POLICY not in saved.state
    assert 149 <= cast(float, saved.state["remaining_timeout_seconds"]) <= 150


async def test_explicit_user_timeout_cannot_be_extended_by_work() -> None:
    runtime, ctx = configured(adaptive=False)
    rows, artifacts = fixture()
    assert runtime._wall_budget is not None
    deadline = runtime._wall_budget.deadline
    saved = checkpoint(runtime, ctx, rows, artifacts)
    assert runtime._wall_budget.deadline == deadline == runtime._wall_budget.absolute_deadline
    assert 49 <= cast(float, saved.state["remaining_timeout_seconds"]) <= 50


@pytest.mark.parametrize("mutate", ["remove_route", "remove_state", "bool_state", "bool_route"])
async def test_checkpoint_policy_is_exact_and_cannot_be_retrofitted(mutate: str) -> None:
    runtime, ctx = configured()
    saved = checkpoint(runtime, ctx, [], [])
    route, state = dict(ctx.routing_decision), dict(saved.state)
    if mutate == "remove_route":
        route.pop(POLICY)
    elif mutate == "remove_state":
        state.pop(POLICY, None)
    elif mutate == "bool_state":
        state[POLICY] = True
    else:
        route[POLICY] = True
    changed = RuntimeCheckpoint(id=saved.id, runtime_type=saved.runtime_type,
        runtime_version=saved.runtime_version, run_id=saved.run_id, tenant_id=saved.tenant_id,
        mode=saved.mode, state=state)
    with pytest.raises(RuntimeExecutionError, match="incompatible"):
        runtime._validate_checkpoint(changed, ctx.model_copy(update={"routing_decision": route}))
