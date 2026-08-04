from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class TaskMode(StrEnum):
    AUTO = "auto"
    DIRECT = "direct"
    DISPATCH = "dispatch"
    DISCUSS = "discuss"
    HYBRID = "hybrid"


class RunStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    WAITING_USER_MODE = "waiting_user_mode"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RETRYING = "retrying"
    PAUSED = "paused"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidTransition(ValueError):
    pass


_ALLOWED_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = MappingProxyType({
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
})


def transition(current: RunStatus, target: RunStatus) -> RunStatus:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"cannot transition {current.value} -> {target.value}")
    return target
