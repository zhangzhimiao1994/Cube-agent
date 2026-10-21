"""Cancellation provenance only; a marker is not permission to release an env."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunRepository
from tests.unit.runs.test_recovery_guard import (
    _CapabilityApprovalSession,
    _CapabilityApprovalSessionFactory,
    _FakeRunRow,
)

MARKER = 'python_project_cancel_cleanup_version'


def configured(status: RunStatus, route: dict[str, object] | None = None,
) -> tuple[RunRepository, _FakeRunRow, _CapabilityApprovalSession]:
    row = _FakeRunRow(id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role=None,
        request='cancel cleanup provenance', mode=TaskMode.DIRECT.value, status=status.value,
        version=8, created_at=datetime.now(UTC), routing_decision=route)
    session = _CapabilityApprovalSession(row, approved=False)
    repository = RunRepository(cast(Any, _CapabilityApprovalSessionFactory(session)))
    return repository, row, session


@pytest.mark.parametrize('status', [RunStatus.QUEUED, RunStatus.PLANNING, RunStatus.WAITING_USER_MODE,
    RunStatus.RUNNING, RunStatus.WAITING_APPROVAL, RunStatus.RETRYING, RunStatus.PAUSED, RunStatus.SYNTHESIZING])
async def test_only_actual_nonterminal_cancel_transition_receives_server_marker(status: RunStatus) -> None:
    original: dict[str, object] = {'source': 'manual', 'approval_kind': 'self_repair',
        'python_input_supply': {'phase': 'executing'}, 'outcome': 'UNKNOWN'}
    repository, row, session = configured(status, original)
    record = await repository.update_control_status(row.tenant_id, row.id, RunStatus.CANCELLED)
    assert record.status is RunStatus.CANCELLED and record.version == 9
    assert row.routing_decision == {**original, MARKER: 1}
    assert row.routing_decision is not original and MARKER not in original
    assert record.routing_decision is not None
    assert type(record.routing_decision[MARKER]) is int and record.routing_decision[MARKER] == 1
    assert session.scalar_calls == 1 and session.added == []
    statement = cast(Any, session.statements[0])
    compiled = statement.compile()
    assert 'FOR UPDATE' in str(compiled)
    assert set(compiled.params.values()) == {row.tenant_id, row.id}


@pytest.mark.parametrize('previous', [True, False, '1', 2, None])
async def test_new_cancel_writes_exact_integer_one_not_existing_untrusted_value(previous: object) -> None:
    repository, row, _ = configured(RunStatus.RUNNING, {MARKER: previous, 'keep': 'value'})
    record = await repository.update_control_status(row.tenant_id, row.id, RunStatus.CANCELLED)
    assert record.routing_decision is not None
    assert type(record.routing_decision[MARKER]) is int and record.routing_decision[MARKER] == 1
    assert record.routing_decision['keep'] == 'value'


@pytest.mark.parametrize('route', [{}, {MARKER: True}, {MARKER: 2}, None])
async def test_already_cancelled_returns_early_without_retrofitting_old_records(route: dict[str, object] | None) -> None:
    repository, row, session = configured(RunStatus.CANCELLED, route)
    record = await repository.update_control_status(row.tenant_id, row.id, RunStatus.CANCELLED)
    assert record.status is RunStatus.CANCELLED and record.version == 8
    assert row.routing_decision is route and record.routing_decision == route
    assert session.added == []


@pytest.mark.parametrize('status', [RunStatus.FAILED, RunStatus.COMPLETED])
async def test_other_terminal_states_are_immutable_without_stamp(status: RunStatus) -> None:
    route: dict[str, object] = {'source': 'manual'}
    repository, row, _ = configured(status, route)
    with pytest.raises(RunConflict, match='terminal run state is immutable'):
        await repository.update_control_status(row.tenant_id, row.id, RunStatus.CANCELLED)
    assert row.status == status.value and row.version == 8
    assert row.routing_decision is route and MARKER not in route


@pytest.mark.parametrize('status', [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PAUSED])
async def test_pause_and_repeated_pause_do_not_create_marker(status: RunStatus) -> None:
    route: dict[str, object] = {'source': 'manual'}
    repository, row, _ = configured(status, route)
    record = await repository.update_control_status(row.tenant_id, row.id, RunStatus.PAUSED)
    assert record.status is RunStatus.PAUSED
    assert record.version == (8 if status is RunStatus.PAUSED else 9)
    assert row.routing_decision is route and MARKER not in route


@pytest.mark.parametrize('route', [None, [], [('source', 'manual')], MappingProxyType({'source': 'manual'})])
async def test_nondict_route_is_not_rebuilt_to_issue_marker(route: object) -> None:
    repository, row, _ = configured(RunStatus.RUNNING)
    cast(Any, row).routing_decision = route
    record = await repository.update_control_status(row.tenant_id, row.id, RunStatus.CANCELLED)
    assert record.status is RunStatus.CANCELLED and record.version == 9
    assert cast(Any, row).routing_decision is route
    assert record.routing_decision is None or MARKER not in record.routing_decision


async def test_malformed_route_keeps_original_record_conversion_failure() -> None:
    repository, row, _ = configured(RunStatus.RUNNING)
    cast(Any, row).routing_decision = 'not a dict'
    with pytest.raises((ValueError, TypeError)):
        await repository.update_control_status(row.tenant_id, row.id, RunStatus.CANCELLED)
    assert cast(Any, row).routing_decision == 'not a dict'


async def test_blocked_successor_cannot_be_cancelled_or_stamped() -> None:
    route: dict[str, object] = {'source': 'manual'}
    repository, row, _ = configured(RunStatus.QUEUED, route)
    row.blocked_by_run_id = uuid4()
    with pytest.raises(RunConflict, match='blocked by an active conversation run'):
        await repository.update_control_status(row.tenant_id, row.id, RunStatus.CANCELLED)
    assert row.status == RunStatus.QUEUED.value and row.version == 8
    assert row.routing_decision is route and MARKER not in route


async def test_cancel_marker_does_not_clear_worker_or_approval_or_unknown_metadata() -> None:
    route: dict[str, object] = {'approval_kind': 'capability_tool', 'approval_id': 'pending', 'phase': 'UNKNOWN'}
    repository, row, _ = configured(RunStatus.RUNNING, route)
    token = uuid4()
    expiry = datetime.now(UTC) + timedelta(seconds=60)
    heartbeat = datetime.now(UTC)
    row.worker_id, row.worker_lease_token = 'worker-1', token
    row.worker_lease_expires_at, row.worker_heartbeat_at = expiry, heartbeat
    record = await repository.update_control_status(row.tenant_id, row.id, RunStatus.CANCELLED)
    assert (record.worker_id, record.worker_lease_token, record.worker_lease_expires_at) == ('worker-1', token, expiry)
    assert row.worker_heartbeat_at == heartbeat
    assert row.routing_decision == {**route, MARKER: 1}
