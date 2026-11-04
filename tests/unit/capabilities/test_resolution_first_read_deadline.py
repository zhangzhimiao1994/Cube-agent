"""First late resolution reads retain the original run deadline and remaining cap."""
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.python_resolution_preparation import PythonResolutionPreparation
from agent_hub.runs.repository import RunRecord
from tests.unit.capabilities.test_python_resolution_preparation import Live
from tests.unit.capabilities.test_python_resolution_preparation import live as preparation_live

live = preparation_live


async def test_current_run_version_six_does_not_change_resolution_scope(live: Live) -> None:
    live.row = replace(live.row, version=6)

    async def current() -> RunRecord:
        return live.row

    client = PythonResolutionPreparation(current=current, approved=None)
    with bind_execution_authority(live.context, worker_id='worker', actor='implementer'):
        result = await client._claim(live.planning)
    assert result.planning.scope == live.planning.scope
    assert result.worker_id == 'worker' and result.lease_token == live.context.execution_token
    assert live.events == []


@pytest.mark.parametrize('has_retained_deadline', [False, True], ids=['first-read', 'retained-deadline'])
@pytest.mark.parametrize('remaining', [1600.0, 25.0], ids=['remaining-run-time', 'short-context-cap'])
async def test_elapsed_first_read_keeps_live_remaining_original_budget(
    live: Live, has_retained_deadline: bool, remaining: float,
) -> None:
    now = datetime.now(UTC)
    assert live.row.routing_decision is not None
    route = {**live.row.routing_decision, 'runtime_timeout_seconds': 3600}
    live.row = replace(live.row, version=6, created_at=now-timedelta(seconds=2000),
                       routing_decision=route, worker_lease_expires_at=now+timedelta(seconds=60))
    live.context = live.context.model_copy(update={'timeout_seconds': remaining, 'routing_decision': route})
    cutoff = now+timedelta(seconds=1599)

    async def current() -> RunRecord:
        return live.row

    client = PythonResolutionPreparation(current=current, approved=None,
        original_deadline=cutoff if has_retained_deadline else None)
    with bind_execution_authority(live.context, worker_id='worker', actor='implementer'):
        claim = await client._claim(live.planning)
    assert now < claim.original_deadline <= now+timedelta(seconds=1600)
    assert claim.original_deadline == (cutoff if has_retained_deadline else
                                       live.row.created_at + timedelta(seconds=3600))
    assert claim.monotonic_deadline <= time.monotonic() + remaining
    assert claim.task_deadline > 0 and claim.deadline <= claim.task_deadline
    assert claim.planning.scope == live.planning.scope
    assert claim.worker_id == 'worker' and claim.lease_token == live.context.execution_token
    assert live.events == []


@pytest.mark.parametrize('invalid', ['original-expired', 'retained-expired', 'retained-extended', 'naive'])
async def test_late_first_read_cannot_reset_expired_or_extend_retained_deadline(
    live: Live, invalid: str,
) -> None:
    now = datetime.now(UTC)
    assert live.row.routing_decision is not None
    route = {**live.row.routing_decision, 'runtime_timeout_seconds': 3600}
    elapsed = 4000 if invalid == 'original-expired' else 2000
    live.row = replace(live.row, created_at=now-timedelta(seconds=elapsed),
                       routing_decision=route, worker_lease_expires_at=now+timedelta(seconds=60))
    live.context = live.context.model_copy(update={'timeout_seconds': 1600, 'routing_decision': route})
    cutoff = {
        'original-expired': None,
        'retained-expired': now-timedelta(seconds=1),
        'retained-extended': live.row.created_at+timedelta(seconds=3601),
        'naive': (now+timedelta(seconds=100)).replace(tzinfo=None),
    }[invalid]

    async def current() -> RunRecord:
        return live.row

    client = PythonResolutionPreparation(current=current, approved=None, original_deadline=cutoff)
    with (bind_execution_authority(live.context, worker_id='worker', actor='implementer'),
          pytest.raises(ValueError, match='resolution_deadline')):
        await client._claim(live.planning)
    assert client._original is None
    assert live.events == []


async def test_repeated_claim_does_not_restore_a_tighter_remaining_cap(live: Live) -> None:
    now = datetime.now(UTC)
    assert live.row.routing_decision is not None
    route = {**live.row.routing_decision, 'runtime_timeout_seconds': 3600}
    live.row = replace(live.row, created_at=now-timedelta(seconds=2000),
                       routing_decision=route, worker_lease_expires_at=now+timedelta(seconds=60))
    live.context = live.context.model_copy(update={'timeout_seconds': 25, 'routing_decision': route})

    async def current() -> RunRecord:
        return live.row

    client = PythonResolutionPreparation(current=current, approved=None)
    with bind_execution_authority(live.context, worker_id='worker', actor='implementer'):
        first = await client._claim(live.planning)
    live.context = live.context.model_copy(update={'timeout_seconds': 1600})
    with bind_execution_authority(live.context, worker_id='worker', actor='implementer'):
        second = await client._claim(live.planning)
    assert second.original_deadline == first.original_deadline == now+timedelta(seconds=1600)
    assert second.monotonic_deadline <= first.monotonic_deadline
    assert second.worker_id == first.worker_id and second.lease_token == first.lease_token
    assert second.planning.scope == first.planning.scope
    assert live.events == []
