from itertools import product

import pytest

from agent_hub.domain.runs import (
    _ALLOWED_TRANSITIONS,
    InvalidTransition,
    RunStatus,
    TaskMode,
    transition,
)

EXPECTED_ALLOWED_TARGETS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.PLANNING, RunStatus.CANCELLED}),
    RunStatus.PLANNING: frozenset(
        {RunStatus.WAITING_USER_MODE, RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.WAITING_USER_MODE: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.RETRYING,
            RunStatus.PAUSED,
            RunStatus.SYNTHESIZING,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED}
    ),
    RunStatus.RETRYING: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.PAUSED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.SYNTHESIZING: frozenset(
        {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def test_wait_for_user_mode_before_running() -> None:
    assert transition(RunStatus.PLANNING, RunStatus.WAITING_USER_MODE) is RunStatus.WAITING_USER_MODE
    assert transition(RunStatus.WAITING_USER_MODE, RunStatus.RUNNING) is RunStatus.RUNNING


def test_completed_run_cannot_restart() -> None:
    with pytest.raises(InvalidTransition):
        transition(RunStatus.COMPLETED, RunStatus.RUNNING)


def test_all_modes_are_stable_wire_values() -> None:
    assert [mode.value for mode in TaskMode] == ["auto", "direct", "dispatch", "discuss", "hybrid"]


def test_all_run_statuses_have_stable_wire_values() -> None:
    assert [status.value for status in RunStatus] == [
        "queued",
        "planning",
        "waiting_user_mode",
        "running",
        "waiting_approval",
        "retrying",
        "paused",
        "synthesizing",
        "completed",
        "failed",
        "cancelled",
    ]


def test_transition_graph_is_immutable() -> None:
    with pytest.raises(TypeError):
        _ALLOWED_TRANSITIONS[RunStatus.COMPLETED] = frozenset({RunStatus.RUNNING})  # type: ignore[index]


@pytest.mark.parametrize(("current", "target"), product(RunStatus, RunStatus))
def test_transition_matches_the_declared_graph(current: RunStatus, target: RunStatus) -> None:
    if target in EXPECTED_ALLOWED_TARGETS[current]:
        assert transition(current, target) is target
    else:
        with pytest.raises(
            InvalidTransition, match=rf"^cannot transition {current.value} -> {target.value}$"
        ):
            transition(current, target)
