from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent_hub.runs.execution_clock import ClockPolicy, ClockSnapshot


def test_split_clock_wait_and_dynamic_soft_never_change_hard_authority() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    policy = ClockPolicy(active_seconds=100., authorization_seconds=1000., initial_soft_seconds=30.,
                         extension_per_unit=10., policy_sha256="a"*64)
    state = ClockSnapshot(policy=policy, hard_deadline=start+timedelta(seconds=1000),
                          spent_seconds=10., active_since=None, worker_id=None, worker_token=None,
                          progress_units=0, version=2)
    remaining = state.remaining(start+timedelta(seconds=510))
    assert remaining.soft_seconds == 20. and remaining.active_seconds == 90.
    assert remaining.authorization_seconds == 490. and remaining.effective_seconds == 20.
    assert state.remaining(start+timedelta(seconds=510), progress_units=1).effective_seconds == 30.
    assert state.hard_deadline == start+timedelta(seconds=1000)
    assert state.remaining(start+timedelta(seconds=1000), progress_units=100).effective_seconds == 0.


def test_unclosed_execution_and_policy_validation_fail_closed() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    policy = ClockPolicy(active_seconds=100., authorization_seconds=1000., initial_soft_seconds=30.,
                         extension_per_unit=10., policy_sha256="a"*64)
    state = ClockSnapshot(policy=policy, hard_deadline=now+timedelta(seconds=1000),
                          spent_seconds=10., active_since=now, worker_id="old", worker_token=uuid4(),
                          progress_units=0, version=2)
    assert state.remaining(now+timedelta(seconds=25)).soft_seconds == 0.
    assert state.remaining(now+timedelta(seconds=25)).active_seconds == 65.
    with pytest.raises(ValueError):
        state.remaining(now-timedelta(seconds=1))
    with pytest.raises(ValueError):
        ClockPolicy(active_seconds=101., authorization_seconds=100., initial_soft_seconds=30.,
                    extension_per_unit=10., policy_sha256="a"*64)
