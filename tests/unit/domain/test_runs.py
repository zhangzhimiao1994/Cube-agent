import pytest

from agent_hub.domain.runs import InvalidTransition, RunStatus, TaskMode, transition


def test_wait_for_user_mode_before_running() -> None:
    assert transition(RunStatus.PLANNING, RunStatus.WAITING_USER_MODE) is RunStatus.WAITING_USER_MODE
    assert transition(RunStatus.WAITING_USER_MODE, RunStatus.RUNNING) is RunStatus.RUNNING


def test_completed_run_cannot_restart() -> None:
    with pytest.raises(InvalidTransition):
        transition(RunStatus.COMPLETED, RunStatus.RUNNING)


def test_all_modes_are_stable_wire_values() -> None:
    assert [mode.value for mode in TaskMode] == ["auto", "direct", "dispatch", "discuss", "hybrid"]


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.QUEUED, RunStatus.PLANNING),
        (RunStatus.QUEUED, RunStatus.CANCELLED),
        (RunStatus.PLANNING, RunStatus.WAITING_USER_MODE),
        (RunStatus.PLANNING, RunStatus.RUNNING),
        (RunStatus.PLANNING, RunStatus.FAILED),
        (RunStatus.PLANNING, RunStatus.CANCELLED),
        (RunStatus.WAITING_USER_MODE, RunStatus.RUNNING),
        (RunStatus.WAITING_USER_MODE, RunStatus.CANCELLED),
        (RunStatus.RUNNING, RunStatus.WAITING_APPROVAL),
        (RunStatus.RUNNING, RunStatus.RETRYING),
        (RunStatus.RUNNING, RunStatus.PAUSED),
        (RunStatus.RUNNING, RunStatus.SYNTHESIZING),
        (RunStatus.RUNNING, RunStatus.FAILED),
        (RunStatus.RUNNING, RunStatus.CANCELLED),
        (RunStatus.WAITING_APPROVAL, RunStatus.RUNNING),
        (RunStatus.WAITING_APPROVAL, RunStatus.CANCELLED),
        (RunStatus.WAITING_APPROVAL, RunStatus.FAILED),
        (RunStatus.RETRYING, RunStatus.RUNNING),
        (RunStatus.RETRYING, RunStatus.FAILED),
        (RunStatus.RETRYING, RunStatus.CANCELLED),
        (RunStatus.PAUSED, RunStatus.RUNNING),
        (RunStatus.PAUSED, RunStatus.CANCELLED),
        (RunStatus.SYNTHESIZING, RunStatus.COMPLETED),
        (RunStatus.SYNTHESIZING, RunStatus.FAILED),
        (RunStatus.SYNTHESIZING, RunStatus.CANCELLED),
    ],
)
def test_all_declared_transitions_are_allowed(current: RunStatus, target: RunStatus) -> None:
    assert transition(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.QUEUED, RunStatus.RUNNING),
        (RunStatus.PLANNING, RunStatus.COMPLETED),
        (RunStatus.RUNNING, RunStatus.COMPLETED),
        (RunStatus.COMPLETED, RunStatus.RUNNING),
        (RunStatus.FAILED, RunStatus.RETRYING),
        (RunStatus.CANCELLED, RunStatus.PLANNING),
    ],
)
def test_invalid_transitions_have_a_stable_message(current: RunStatus, target: RunStatus) -> None:
    with pytest.raises(
        InvalidTransition, match=rf"^cannot transition {current.value} -> {target.value}$"
    ):
        transition(current, target)
