"""Real engineering work renews the soft runway, never the absolute fuse."""

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Mapping
from dataclasses import replace
from typing import cast

import pytest
from autogen_agentchat.messages import TextMessage

from agent_hub.runtime.adaptive_budget import AdaptiveDeadline
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    CapabilityGateway,
    DiscussionParticipant,
    DiscussionPlan,
    RuntimeExecutionError,
    _AdaptiveDiscussionTermination,
    _DiscussionControl,
)
from agent_hub.runtime.autogen.termination import CompositeDiscussionTermination, DiscussionUsage
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
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


def turn_runtime(*, policy: JsonValue = 1, ack: bool = True) -> tuple[AutoGenDiscussionRuntime, TaskContext]:
    original, ctx = configured(policy=policy)
    ctx = ctx.model_copy(update={"token_budget": 120, "routing_decision": {
        **ctx.routing_decision, **({"engineering_final_report_version": 1} if ack else {}),
    }})
    plan = replace(original._plan, participants=(original._plan.participants[0], *(
        DiscussionParticipant(id=f"reviewer_{index}", role="Reviewer", goal="Review", logical_model="shared")
        for index in range(7)
    )))
    runtime = AutoGenDiscussionRuntime(Gateway(), plan, capability_gateway=cast(CapabilityGateway, object()))
    runtime._discussion_control = _DiscussionControl.create(plan, ctx, DiscussionUsage())
    runtime._wall_budget, runtime._wall_deadline = original._wall_budget, original._wall_deadline
    return runtime, ctx


def termination(runtime: AutoGenDiscussionRuntime, ctx: TaskContext) -> _AdaptiveDiscussionTermination:
    control = runtime._discussion_control
    assert control is not None
    return _AdaptiveDiscussionTermination(base=CompositeDiscussionTermination(
        usage=DiscussionUsage(), max_turns=control.hard_limit,
        token_budget=min(runtime._plan.token_budget, ctx.token_budget),
        cost_budget_usd=runtime._plan.cost_budget_usd, wall_time_seconds=runtime._plan.wall_time_seconds,
        consensus_votes=runtime._plan.consensus_votes, cancelled=lambda: False,
        adaptive_deadline=runtime._wall_budget, monotonic=asyncio.get_running_loop().time,
        delivery_ready=lambda: False,
    ), control=control, participants=frozenset(runtime.participant_ids), context=ctx,
        plan_token_budget=runtime._plan.token_budget)


def write_result(index: int) -> Mapping[str, JsonValue]:
    return {"workspace_files": ({"path": f"src/file_{index}.py",
        "sha256": hashlib.sha256(f"content {index}".encode()).hexdigest(), "size_bytes": 10},)}


@pytest.mark.parametrize("ample_runway", [False, True])
async def test_semantic_writes_cross_multiple_turn_windows_then_duplicate_work_stops(
    ample_runway: bool,
) -> None:
    runtime, ctx = turn_runtime()
    assert runtime._wall_budget is not None
    if ample_runway:
        runtime._wall_budget.deadline = asyncio.get_running_loop().time() + 450
        runtime._wall_deadline = runtime._wall_budget.deadline
    initial_deadline = runtime._wall_budget.deadline
    stop = termination(runtime, ctx)
    rows: list[dict[str, JsonValue]] = []
    artifacts: list[Artifact] = []
    for index in range(40):
        append_result(rows, artifacts, "workspace.write_text", write_result(index))
        saved = checkpoint(runtime, ctx, rows, artifacts)
        runtime._validate_checkpoint(saved, ctx)
        result = await stop((TextMessage(source="engineer", content="Working"),))
        assert result is None, f"new semantic write stopped at turn {index + 1}: {result}"
    assert stop.control.soft_limit == 48
    assert stop.control.hard_limit == 60
    assert runtime._wall_budget is not None and runtime._wall_budget.progress_units == 40
    if ample_runway:
        assert runtime._wall_budget.deadline == initial_deadline
    deadline = runtime._wall_budget.deadline
    absolute = runtime._wall_budget.absolute_deadline
    for turn in range(41, 49):
        # Fresh artifact UUIDs and read text do not constitute new semantic work.
        append_result(rows, artifacts, "workspace.read", {"text": "[EVIDENCE] [COMPLETE]"})
        append_result(rows, artifacts, "workspace.write_text", write_result(39))
        saved = checkpoint(runtime, ctx, rows, artifacts)
        runtime._validate_checkpoint(saved, ctx)
        result = await stop((TextMessage(source="engineer", content="Working"),))
        if turn < 48:
            assert result is None
        else:
            assert result is not None and result.content == "no_progress"
    assert runtime._wall_budget.progress_units == 40
    assert runtime._wall_budget.deadline == deadline
    assert runtime._wall_budget.absolute_deadline == absolute


async def test_fresh_semantic_work_still_stops_at_existing_hard_turn_cap() -> None:
    runtime, ctx = turn_runtime()
    stop = termination(runtime, ctx)
    rows: list[dict[str, JsonValue]] = []
    artifacts: list[Artifact] = []
    for index in range(60):
        append_result(rows, artifacts, "workspace.write_text", write_result(index))
        checkpoint(runtime, ctx, rows, artifacts)
        result = await stop((TextMessage(source="engineer", content="Working"),))
        if index < 59:
            assert result is None, f"new semantic write stopped at turn {index + 1}: {result}"
        else:
            assert result is not None and result.content == "resource_limit"
    assert stop.control.hard_limit == 60


@pytest.mark.parametrize("budget", ["tokens", "cost", "absolute"])
async def test_semantic_turn_progress_cannot_bypass_resource_fuses(budget: str) -> None:
    runtime, ctx = turn_runtime()
    stop = termination(runtime, ctx)
    rows: list[dict[str, JsonValue]] = []
    artifacts: list[Artifact] = []
    append_result(rows, artifacts, "workspace.write_text", write_result(0))
    checkpoint(runtime, ctx, rows, artifacts)
    if budget == "tokens":
        stop.base.usage.tokens = ctx.token_budget
    elif budget == "cost":
        stop.base.usage.cost_usd = runtime._plan.cost_budget_usd
    else:
        assert runtime._wall_budget is not None
        expired_at = runtime._wall_budget.absolute_deadline
        stop.base.monotonic = lambda: expired_at
        append_result(rows, artifacts, "workspace.write_text", write_result(1))
        checkpoint(runtime, ctx, rows, artifacts)
        assert runtime._wall_budget.absolute_deadline == expired_at
    result = await stop((TextMessage(source="engineer", content="Working"),))
    assert result is not None
    assert result.content == ("wall_time" if budget == "absolute" else "budget_exhausted")


@pytest.mark.parametrize("defect", ["UNKNOWN", "running", "hash", "orphan"])
async def test_uncertain_or_unbound_work_cannot_recredit_a_consumed_window(defect: str) -> None:
    runtime, ctx = turn_runtime()
    stop = termination(runtime, ctx)
    rows, artifacts = fixture()
    rows, artifacts = rows[:1], artifacts[:1]
    for _ in range(16):
        checkpoint(runtime, ctx, rows, artifacts)
        assert await stop((TextMessage(source="engineer", content="Working"),)) is None
    assert not stop.control.progress_since_extension
    assert runtime._wall_budget is not None
    deadline = runtime._wall_budget.deadline
    append_result(rows, artifacts, "workspace.write_text", write_result(1))
    if defect in {"UNKNOWN", "running"}:
        rows[-1]["status"] = defect
    elif defect == "hash":
        rows[-1]["artifact_sha256"] = "0" * 64
    else:
        rows.pop()
    if defect != "orphan":
        append_result(rows, artifacts, "workspace.write_text", write_result(2))
    for turn in range(17, 25):
        checkpoint(runtime, ctx, rows, artifacts)
        assert not stop.control.progress_since_extension
        result = await stop((TextMessage(source="engineer", content="Working"),))
        if turn < 24:
            assert result is None
        else:
            assert result is not None and result.content == "no_progress"
    assert runtime._wall_budget.progress_units == 1
    assert runtime._wall_budget.deadline == deadline


async def test_cold_runtime_old_work_does_not_recredit_consumed_turn_progress() -> None:
    runtime, ctx = turn_runtime()
    stop = termination(runtime, ctx)
    rows, artifacts = fixture()
    rows, artifacts = rows[:1], artifacts[:1]
    checkpoint(runtime, ctx, rows, artifacts)
    for _ in range(16):
        assert await stop((TextMessage(source="engineer", content="Working"),)) is None
    saved = checkpoint(runtime, ctx, rows, artifacts)
    runtime._validate_checkpoint(saved, ctx)
    for artifact in artifacts:
        await runtime._repository.put(ctx.tenant_id, ctx.run_id, artifact)
    gateway = Gateway()
    fresh = AutoGenDiscussionRuntime(gateway, runtime._plan,
        capability_gateway=cast(CapabilityGateway, object()), artifact_repository=runtime._repository)
    await fresh.restore_checkpoint(RuntimeCheckpoint.from_payload(saved.to_payload()))
    stream = cast(AsyncGenerator[RunEvent, None], fresh.run(ctx.model_copy(update={"checkpoint": saved})))
    try:
        async with asyncio.timeout(5):
            started = await anext(stream)
            assert started.kind is EventKind.DISCUSSION_STARTED, started
            assert fresh._wall_budget is not None and fresh._wall_budget.progress_units == 1
            deadline = fresh._wall_budget.deadline
            resumed_stop = termination(fresh, ctx)
            for turn in range(17, 25):
                checkpoint(fresh, ctx, rows, artifacts)
                assert not resumed_stop.control.progress_since_extension
                result = await resumed_stop((TextMessage(source="engineer", content="Working"),))
                if turn < 24:
                    assert result is None
                else:
                    assert result is not None and result.content == "no_progress"
            assert fresh._wall_budget.deadline == deadline
            assert fresh._wall_budget.progress_units == 1
    finally:
        await stream.aclose()
    assert gateway.requests == []


@pytest.mark.parametrize("policy,ack", [(None, True), (True, True), ("1", True), (2, True), (1, False)])
async def test_legacy_or_non_ack_turn_policy_keeps_existing_window(policy: JsonValue, ack: bool) -> None:
    runtime, ctx = turn_runtime(policy=policy, ack=ack)
    stop = termination(runtime, ctx)
    rows: list[dict[str, JsonValue]] = []
    artifacts: list[Artifact] = []
    for turn in range(1, 25):
        append_result(rows, artifacts, "workspace.write_text", write_result(turn))
        checkpoint(runtime, ctx, rows, artifacts)
        result = await stop((TextMessage(source="engineer", content="Working"),))
        if turn < 24:
            assert result is None
        else:
            assert result is not None and result.content == "no_progress"


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
