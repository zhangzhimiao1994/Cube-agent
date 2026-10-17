"""Synthetic RAM refresh/round compatibility, not production-run causality."""
from __future__ import annotations

from typing import cast

import pytest

from agent_hub.runtime.contracts import EventKind, JsonValue, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import RuntimeExecutionError
from agent_hub.runtime.crew.plan import DispatchStep
from agent_hub.runtime.progress import workspace_progress_key
from tests.unit.runtime.crew.test_maintenance_initial_context_refresh import (
    PREFIX,
    TAIL,
    A,
    B,
    exercise,
    make_subject,
    partial,
    prove_lost_prewrite_context,
    rows,
)
from tests.unit.runtime.crew.test_meaningful_workspace_progress import (  # noqa: F401
    observe,
    offline_only,
)
from tests.unit.runtime.crew.test_workspace_context_refresh import (
    collect,
    prefix,
    tail,
    trace_deadline,
)

ROUND_LIMIT = "step capability round limit exceeded"
STALL = "workspace delivery stalled after unchanged observations"
FLAG = "workspace_refresh_round_continuation"


@pytest.fixture(autouse=True)
def bounded_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    def budget(context: TaskContext, step: DispatchStep) -> adapter._ToolRoundBudget:
        return adapter._ToolRoundBudget(initial_limit=4, extension_size=2, hard_limit=8)

    monkeypatch.setattr(adapter, "_tool_round_budget", budget)


async def test_lost_context_refresh_must_allow_pending_repair_within_original_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = (*PREFIX, observe("workspace.read", A), *TAIL)
    result = await exercise(monkeypatch, responses, version=1)
    prove_lost_prewrite_context(result)
    assert [call.tool_name for call in result.tools.calls[:3]] == ["workspace.read"] * 3
    # Before any repair, both first observations earned credit, not their refresh.
    refresh_boundaries = [event.checkpoint for event in result.events
                          if event.checkpoint is not None
                          and len(rows(event.checkpoint, "tools")) == 3
                          and all(row["status"] == "succeeded"
                                  for row in rows(event.checkpoint, "tools").values())]
    assert refresh_boundaries
    assert all(cp.state["timeout_progress_units"] == 2 for cp in refresh_boundaries)
    assert result.reason is None, "a real lost-context refresh must not strand the pending write at soft=4"
    assert result.events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in result.tools.calls] == ["workspace.read"] * 3 + [
        "workspace.write_text", "workspace.bundle",
    ]
    assert len(result.gateway.requests) == len(responses) == 7
    assert len(result.gateway.requests) - 1 <= 8
    assert len(result.deadline_credits) == 4, "refresh cannot extend the step deadline"
    # Completion adds the existing completed-step and final-text units.
    assert result.checkpoint.state["timeout_progress_units"] == 6
    assert result.checkpoint.state[FLAG] is True
    assert len(result.tools.files) == 30


@pytest.mark.parametrize("version", (None, 0))
async def test_legacy_feedback_does_not_open_prewrite_epoch(
    monkeypatch: pytest.MonkeyPatch, version: int | None,
) -> None:
    result = await exercise(monkeypatch, (*PREFIX, observe("workspace.read", A), *TAIL), version=version)
    prove_lost_prewrite_context(result)
    assert result.reason == STALL
    assert len(result.gateway.requests) == 4
    assert len(result.tools.calls) == 3
    assert len(result.deadline_credits) == result.checkpoint.state["timeout_progress_units"] == 2
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in result.events)


async def test_visible_duplicate_does_not_buy_round_extension_or_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await exercise(monkeypatch, (*PREFIX, observe("workspace.read", A), *TAIL),
                            version=1, pressure=False)
    key = workspace_progress_key("workspace.read", "", {"path": A, "text": "a", "truncated": False})
    visible = adapter.CrewDispatchRuntime._visible_workspace_read_keys(result.gateway.requests[3].messages)
    assert key is not None and visible is not None and key in visible
    assert result.reason == STALL
    assert len(result.gateway.requests) == 4 and len(result.tools.calls) == 3
    assert len(result.deadline_credits) == result.checkpoint.state["timeout_progress_units"] == 2
    assert all(call.tool_name == "workspace.read" for call in result.tools.calls)


async def test_spent_hidden_read_cannot_extend_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A and B each refresh once. Spent A must not buy the next soft extension.
    responses = (*PREFIX, *(observe("workspace.read", path) for path in (A, B) * 6), *TAIL)
    result = await exercise(monkeypatch, responses, version=1)
    prove_lost_prewrite_context(result)
    assert result.reason == ROUND_LIMIT
    assert len(result.gateway.requests) == 7 and len(result.tools.calls) == 5
    for index, path in enumerate((A, B, A, B), start=3):
        key = workspace_progress_key("workspace.read", "", {
            "path": path, "text": ("a" if path == A else "b") * 60_000, "truncated": False,
        })
        visible = adapter.CrewDispatchRuntime._visible_workspace_read_keys(result.gateway.requests[index].messages)
        assert key is not None and visible is not None and key not in visible
    assert len(result.deadline_credits) == result.checkpoint.state["timeout_progress_units"] == 2
    assert all(call.tool_name == "workspace.read" for call in result.tools.calls)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in result.events)


@pytest.mark.parametrize("with_refresh", (False, True))
async def test_genuine_new_observations_still_cannot_cross_original_hard_cap(
    monkeypatch: pytest.MonkeyPatch, with_refresh: bool,
) -> None:
    before = (*PREFIX, observe("workspace.read", A)) if with_refresh else (
        observe("workspace.read", A), observe("workspace.read", B),
    )
    paths = tuple(f"docs/retained-{index}.md" for index in range(8 - len(before)))
    result = await exercise(monkeypatch, (
        *before, *(observe("workspace.read", path) for path in paths), *TAIL,
    ), version=1)
    assert result.reason == ROUND_LIMIT
    assert len(result.gateway.requests) == 9
    assert len(result.tools.calls) == (7 if with_refresh else 8)
    assert len(result.deadline_credits) == result.checkpoint.state["timeout_progress_units"] == (
        6 if with_refresh else 8)
    assert all(call.tool_name == "workspace.read" for call in result.tools.calls)
    assert len(result.tools.files) == 30
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in result.events)


def checkpoint_flag(checkpoint: RuntimeCheckpoint, value: JsonValue) -> RuntimeCheckpoint:
    payload = checkpoint.to_payload()
    state = cast(dict[str, JsonValue], payload["state"])
    state.pop(FLAG, None)
    if value is not True:
        state.pop("workspace_productive_boundary", None)
    if value is not None:
        state[FLAG] = value
    payload["state_sha256"] = ""
    return RuntimeCheckpoint.from_payload(payload)


@pytest.mark.parametrize("stored", (None, False, True))
async def test_checkpoint_policy_wins_over_route_without_replaying_prefix(
    monkeypatch: pytest.MonkeyPatch, stored: bool | None,
) -> None:
    original = await exercise(monkeypatch, (*PREFIX, observe("workspace.read", A), *TAIL), version=1)
    checkpoint = checkpoint_flag(partial(original, models=3, tools=2), stored)
    before = checkpoint.to_payload()
    runtime, task, gateway, tools = make_subject(
        (observe("workspace.read", A), *TAIL), version=1, task=original.task,
        repository=original.repository,
    )
    assert runtime._plan.digest == original.runtime._plan.digest
    task = task.model_copy(update={"routing_decision": {
        **task.routing_decision, FLAG: stored is not True,
    }, "checkpoint": checkpoint})
    await runtime.restore_checkpoint(checkpoint)
    events = []
    if stored is True:
        events = [event async for event in runtime.run(task)]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(gateway.requests) == 4 and len(tools.calls) == 3
    else:
        with pytest.raises(RuntimeExecutionError, match=ROUND_LIMIT):
            async for event in runtime.run(task):
                events.append(event)
        assert len(gateway.requests) == 2 and len(tools.calls) == 1
    saved = await runtime.save_checkpoint()
    assert saved.state.get(FLAG, False) is (stored is True)
    if stored is not True:
        assert FLAG not in saved.state
    for key in ("models", "tools"):
        for ledger_key, row in rows(checkpoint, key).items():
            assert rows(saved, key)[ledger_key] == row
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize("value", (None, 0, 1, "true", [], {}))
async def test_invalid_persisted_flag_rejected_before_model_or_tool_io(
    monkeypatch: pytest.MonkeyPatch, value: JsonValue,
) -> None:
    original = await exercise(monkeypatch, (*PREFIX, observe("workspace.read", A), *TAIL), version=1)
    payload = partial(original, models=3, tools=2).to_payload()
    cast(dict[str, JsonValue], payload["state"])[FLAG] = value
    payload["state_sha256"] = ""
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    before = checkpoint.to_payload()
    runtime, _task, gateway, tools = make_subject((), version=1, task=original.task,
                                               repository=original.repository)
    with pytest.raises(RuntimeExecutionError, match="checkpoint"):
        await runtime.restore_checkpoint(checkpoint)
    assert not gateway.requests and not tools.calls and checkpoint.to_payload() == before


@pytest.mark.parametrize("dependency", (
    "workspace_context_refresh", "workspace_meaningful_progress", "workspace_delivery_continuation",
))
async def test_true_flag_requires_existing_refresh_policy(
    monkeypatch: pytest.MonkeyPatch, dependency: str,
) -> None:
    original = await exercise(monkeypatch, (*PREFIX, observe("workspace.read", A), *TAIL), version=1)
    payload = checkpoint_flag(partial(original, models=3, tools=2), True).to_payload()
    cast(dict[str, JsonValue], payload["state"])[dependency] = False
    payload["state_sha256"] = ""
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    runtime, _task, gateway, tools = make_subject((), version=1, task=original.task,
                                               repository=original.repository)
    with pytest.raises(RuntimeExecutionError, match="checkpoint"):
        await runtime.restore_checkpoint(checkpoint)
    assert not gateway.requests and not tools.calls


async def test_non_incremental_step_does_not_serialize_new_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await exercise(monkeypatch, PREFIX, version=1, native=False)
    assert result.reason is None and result.events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert FLAG not in result.checkpoint.state


async def test_ordinary_incremental_postwrite_refresh_continues_with_no_deadline_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credits = trace_deadline(monkeypatch)
    responses = (*prefix(), observe("workspace.read", A), observe("workspace.read", B), *tail())
    events, reason, checkpoint, gateway, tools = await collect(responses)
    for index, path in ((4, A), (5, B)):
        key = workspace_progress_key("workspace.read", "", {
            "path": path, "text": tools.files[path], "truncated": False,
        })
        visible = adapter.CrewDispatchRuntime._visible_workspace_read_keys(gateway.requests[index].messages)
        assert key is not None and visible is not None and key not in visible
    assert reason is None
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert checkpoint.state[FLAG] is True
    assert len(gateway.requests) == len(responses) == 9
    assert len(credits) == 5
    assert checkpoint.state["timeout_progress_units"] == 7


@pytest.mark.parametrize("completed", (False, True))
async def test_stored_true_replays_across_soft_boundary_without_reexecuting_prefix(
    monkeypatch: pytest.MonkeyPatch, completed: bool,
) -> None:
    original = await exercise(monkeypatch, (*PREFIX, observe("workspace.read", A), *TAIL), version=1)
    assert original.reason is None
    checkpoint = original.checkpoint if completed else partial(original, models=5, tools=4)
    assert checkpoint.state[FLAG] is True
    before = checkpoint.to_payload()
    runtime, task, gateway, tools = make_subject(
        () if completed else TAIL[1:], version=1, task=original.task, repository=original.repository,
    )
    tools.files = dict(original.tools.files)
    before_files = dict(tools.files)
    await runtime.restore_checkpoint(checkpoint)
    events = [event async for event in runtime.run(task.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == (0 if completed else 2)
    assert [call.tool_name for call in tools.calls] == ([] if completed else ["workspace.bundle"])
    assert tools.files == before_files and checkpoint.to_payload() == before
    saved = await runtime.save_checkpoint()
    assert saved.state[FLAG] is True
    for key in ("models", "tools"):
        for ledger_key, row in rows(checkpoint, key).items():
            assert rows(saved, key)[ledger_key] == row


@pytest.mark.parametrize("dependency", (
    "workspace_context_refresh", "workspace_meaningful_progress", "workspace_delivery_continuation",
))
async def test_fresh_disabled_refresh_policy_does_not_persist_true(
    dependency: str,
) -> None:
    runtime, task, _gateway, _tools = make_subject(TAIL, version=1)
    stream = cast(adapter.CrewRunStream, runtime.run(task))
    setattr(stream._state, dependency, False)
    events = [event async for event in stream]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runtime.save_checkpoint()
    assert FLAG not in checkpoint.state
