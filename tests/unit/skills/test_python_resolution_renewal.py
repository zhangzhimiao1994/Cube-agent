"""Long reads keep the same execution and need live authority beyond lease rollovers."""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.python_metadata_preparation import metadata_scope
from agent_hub.capabilities.python_resolution_preparation import PythonResolutionPreparation
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRecord
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.skills.sandbox import python_resolution_worker as worker
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    PythonMetadataDraft,
    PythonMetadataPlanningRequest,
    PythonMetadataScope,
)
from agent_hub.skills.sandbox.python_project_backend import RootConfig
from agent_hub.skills.sandbox.python_registry_protocol import digest
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionClaim,
    ResolutionRequest,
    ResolutionStatus,
)


def request(*, lease: float = 0.15, task: float = 1.0) -> ResolutionRequest:
    now = datetime.now(UTC)
    claim = ResolutionClaim(
        planning=PythonMetadataPlanningRequest(
            invocation=ProjectInvocation(execution_id='resolution_renewal_test', tenant_id=UUID(int=1),
                run_id=UUID(int=2), project_id='project', session_id='session',
                sandbox_profile='restricted', timeout_seconds=30),
            scope=PythonMetadataScope(tenant_id=UUID(int=1), run_id=UUID(int=2), user_id=UUID(int=3),
                project_id='project', session_id='session', actor='implementer', actor_role='operator',
                request_sha256='1' * 64, permissions_sha256='4' * 64),
            draft=PythonMetadataDraft(source_sha256='2' * 64, acceptance_sha256='5' * 64,
                manifest_sha256='6' * 64, lock_sha256='7' * 64),
            expected_root_config_sha256='8' * 64, expected_catalog_sha256='3' * 64),
        worker_id='original-worker', lease_token=UUID(int=4), lease_expires_at=now + timedelta(seconds=lease),
        original_deadline=now + timedelta(seconds=task), monotonic_deadline=time.monotonic() + task)
    return ResolutionRequest(action='resolve_python_inputs', claim=claim,
        requirements=(('click==8.1.7', 'a' * 64),), environment={
            'implementation_name': 'cpython', 'implementation_version': '3.12.0',
            'os_name': 'posix', 'platform_machine': 'x86_64', 'platform_release': '6.8.0',
            'platform_system': 'Linux', 'platform_version': 'test',
            'platform_python_implementation': 'CPython', 'python_full_version': '3.12.0',
            'python_version': '3.12', 'sys_platform': 'linux'})


def test_short_lease_and_original_task_deadline_are_separate_bounds() -> None:
    claim = request(lease=0.15, task=2).claim
    bound = claim.task_deadline
    assert isinstance(bound, float), 'protocol needs an immutable task bound separate from its renewable lease'
    assert bound > time.monotonic() + 1.5
    assert claim.deadline < time.monotonic() + 0.2


def test_task_budget_never_authorizes_an_expired_lease() -> None:
    claim = request(lease=-1, task=2).claim
    bound = claim.task_deadline
    assert isinstance(bound, float) and bound > time.monotonic()
    with pytest.raises(ValueError, match='resolution_deadline'):
        _ = claim.deadline


async def test_same_operation_survives_multiple_valid_lease_rollovers() -> None:
    initial = request(lease=0.09)
    acknowledged: list[ResolutionClaim] = []
    entries = 0

    async def operation() -> ResolutionStatus:
        nonlocal entries
        entries += 1
        await asyncio.sleep(0.22)
        return ResolutionStatus(state='unknown', key=initial.key)

    async def refresh() -> ResolutionClaim:
        return initial.claim.model_copy(update={'lease_expires_at': datetime.now(UTC) + timedelta(seconds=0.09)})

    async def renew(value: ResolutionRequest) -> ResolutionStatus:
        assert value.action == 'renew_python_resolution'
        assert value.invocation.execution_id == initial.invocation.execution_id
        acknowledged.append(value.claim)
        return ResolutionStatus(state='lease_renewed', key=initial.key)

    run = worker._await_resolution_with_renewal
    assert callable(run), 'waiting for Root must actively propagate verified lease renewals'
    result = await run(initial, operation=operation(), refresh=refresh, renew=renew)
    assert result.key == initial.key and entries == 1
    assert len(acknowledged) >= 2
    assert all(c.worker_id == initial.claim.worker_id and c.lease_token == initial.claim.lease_token
        and c.original_deadline == initial.claim.original_deadline for c in acknowledged)


async def test_renewal_checks_stay_in_the_authority_owning_task() -> None:
    initial = request(lease=0.09)
    owner = asyncio.current_task()

    async def operation() -> ResolutionStatus:
        await asyncio.sleep(0.14)
        return ResolutionStatus(state='unknown', key=initial.key)

    async def refresh() -> ResolutionClaim:
        assert asyncio.current_task() is owner, 'authority cannot be borrowed by a detached task'
        return initial.claim.model_copy(update={'lease_expires_at': datetime.now(UTC) + timedelta(seconds=0.09)})

    async def renew(value: ResolutionRequest) -> ResolutionStatus:
        assert asyncio.current_task() is owner
        return ResolutionStatus(state='lease_renewed', key=value.key)

    run = worker._await_resolution_with_renewal
    assert callable(run)
    await run(initial, operation=operation(), refresh=refresh, renew=renew)


@pytest.mark.parametrize('failure', ['revoked', 'new-token', 'new-worker', 'extended-task'])
async def test_authority_loss_cancels_and_drains_the_original_operation(failure: str) -> None:
    initial = request(lease=0.12)
    drained = asyncio.Event()

    async def operation() -> ResolutionStatus:
        try:
            await asyncio.Event().wait()
            raise AssertionError('unreachable')
        finally:
            drained.set()

    async def refresh() -> ResolutionClaim:
        if failure == 'revoked':
            raise ValueError('resolution_authority_unavailable')
        changes: dict[str, object] = {'lease_expires_at': datetime.now(UTC) + timedelta(seconds=0.12)}
        if failure == 'new-token':
            changes['lease_token'] = UUID(int=99)
        elif failure == 'new-worker':
            changes['worker_id'] = 'replacement-worker'
        else:
            changes['original_deadline'] = initial.claim.original_deadline + timedelta(seconds=1)
        return initial.claim.model_copy(update=changes)

    async def renew(value: ResolutionRequest) -> ResolutionStatus:
        raise AssertionError('changed authority must never reach Root renewal')

    run = worker._await_resolution_with_renewal
    assert callable(run), 'the SDK needs a continuous authority monitor'
    with pytest.raises(ValueError):
        await run(initial, operation=operation(), refresh=refresh, renew=renew)
    assert drained.is_set()


async def test_renewal_cannot_extend_the_original_task_budget() -> None:
    initial = request(lease=0.09, task=0.16)
    drained = asyncio.Event()

    async def operation() -> ResolutionStatus:
        try:
            await asyncio.Event().wait()
            raise AssertionError('unreachable')
        finally:
            drained.set()

    async def refresh() -> ResolutionClaim:
        return initial.claim.model_copy(update={'lease_expires_at': datetime.now(UTC) + timedelta(seconds=0.09)})

    async def renew(value: ResolutionRequest) -> ResolutionStatus:
        return ResolutionStatus(state='lease_renewed', key=value.key)

    run = worker._await_resolution_with_renewal
    assert callable(run), 'task deadline must stay independently enforced'
    with pytest.raises((TimeoutError, ValueError)):
        await run(initial, operation=operation(), refresh=refresh, renew=renew)
    assert drained.is_set()


async def test_preparer_propagates_current_authority_during_one_transport_call() -> None:
    first = request().claim
    route: dict[str, JsonValue] = {
        'generated_project_contract_version': 1, 'python_dependency_preflight_version': 1,
        'python_dependency_preflight_submission_version': 1,
        'project_id': 'project', 'workspace_session_id': 'session',
        'sandbox_profile': 'workspace_write', 'execution_backend': 'systemd',
        'requested_permissions': ('workspace.read', 'workspace.write', 'command.run'),
        'runtime_timeout_seconds': 30}
    row_route: dict[str, object] = {key: value for key, value in route.items()}
    row = RunRecord(id=first.planning.scope.run_id, tenant_id=first.planning.scope.tenant_id,
        actor_id=first.planning.scope.user_id, actor_role=Role.OPERATOR,
        request='build the approved Python project', mode=TaskMode.DIRECT,
        status=RunStatus.RUNNING, version=1, created_at=datetime.now(UTC), routing_decision=row_route,
        worker_id='original-worker', worker_lease_token=first.lease_token,
        worker_lease_expires_at=datetime.now(UTC) + timedelta(seconds=0.12))
    context = TaskContext(run_id=row.id, tenant_id=row.tenant_id, actor_id=row.actor_id,
        actor_role=row.actor_role, execution_token=row.worker_lease_token,
        mode=TaskMode.DIRECT, request=row.request, routing_decision=route, timeout_seconds=30)
    planning = first.planning.model_copy(update={'scope': metadata_scope(row, actor='implementer', live=True)})
    actions: list[str] = []
    owner = asyncio.current_task()

    async def current() -> RunRecord:
        assert asyncio.current_task() is owner
        return replace(row, worker_lease_expires_at=datetime.now(UTC) + timedelta(seconds=0.12))

    async def approved(value: CapabilityRequest) -> bool:
        assert asyncio.current_task() is owner
        return True

    async def transport(value: ResolutionRequest) -> ResolutionStatus:
        actions.append(value.action)
        if value.action == 'renew_python_resolution':
            return ResolutionStatus(state='lease_renewed', key=value.key)
        await asyncio.sleep(0.28)
        return ResolutionStatus(state='unknown', key=value.key)

    preparation = PythonResolutionPreparation(current=current, approved=approved, transport=transport,
        original_deadline=row.created_at + timedelta(seconds=1))
    with bind_execution_authority(context, worker_id='original-worker', actor='implementer'):
        response = await preparation.resolve(needs=request().needs, environment=request().environment, planning=planning)
    assert response.state == 'unknown'  # This is transport evidence, never a successful build receipt.
    assert actions.count('resolve_python_inputs') == 1
    assert actions.count('renew_python_resolution') >= 2


async def test_root_wait_uses_the_latest_trusted_witness_until_task_completion() -> None:
    initial = request(lease=0.09)
    live = initial.claim
    calls = 0

    def current() -> ResolutionClaim:
        return live

    async def heartbeat() -> None:
        nonlocal live
        while True:
            await asyncio.sleep(0.02)
            live = live.model_copy(update={'lease_expires_at': datetime.now(UTC) + timedelta(seconds=0.09)})

    async def operation() -> ResolutionStatus:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)
        return ResolutionStatus(state='unknown', key=initial.key)

    run = worker._await_resolution_with_root_lease
    assert callable(run), 'Root must independently watch the latest private lease witness'
    pulse = asyncio.create_task(heartbeat())
    try:
        result = await run(initial, operation=operation(), current=current)
        assert result.key == initial.key and calls == 1
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)


async def test_root_witness_loss_drains_the_original_operation() -> None:
    initial = request(lease=0.09)
    drained = asyncio.Event()

    def current() -> ResolutionClaim:
        return initial.claim

    async def operation() -> ResolutionStatus:
        try:
            await asyncio.Event().wait()
            raise AssertionError('unreachable')
        finally:
            drained.set()

    run = worker._await_resolution_with_root_lease
    assert callable(run), 'Root cannot rely solely on the SDK to enforce lease expiry'
    with pytest.raises((ValueError, TimeoutError)):
        await run(initial, operation=operation(), current=current)
    assert drained.is_set()


async def test_root_watch_failure_cannot_be_hidden_by_simultaneous_completion() -> None:
    initial = request(lease=0.5)
    failed = asyncio.Event()
    calls = 0
    drained = asyncio.Event()

    def current() -> ResolutionClaim:
        nonlocal calls
        calls += 1
        if calls == 2:
            failed.set()
            raise ValueError('resolution_scope_changed')
        return initial.claim

    async def operation() -> ResolutionStatus:
        try:
            await failed.wait()
            return ResolutionStatus(state='unknown', key=initial.key)
        finally:
            drained.set()

    with pytest.raises(ValueError, match='resolution_scope_changed'):
        await worker._await_resolution_with_root_lease(initial,
            operation=operation(), current=current)
    assert drained.is_set()


@pytest.mark.parametrize('closed', [False, True], ids=['slot-finished', 'renewal-connection-closed'])
async def test_completed_original_response_wins_a_late_renewal_unknown(closed: bool) -> None:
    initial = request(lease=0.15)
    entered = asyncio.Event()

    async def operation() -> ResolutionStatus:
        await entered.wait()
        return ResolutionStatus(state='unknown', key=initial.key)

    async def refresh() -> ResolutionClaim:
        return initial.claim.model_copy(update={'lease_expires_at': datetime.now(UTC) + timedelta(seconds=0.15)})

    async def renew(value: ResolutionRequest) -> ResolutionStatus:
        entered.set()
        await asyncio.sleep(0.02)
        if closed:
            raise OSError('late renewal connection closed')
        # The Root operation has finished and its live broker slot is gone.
        return ResolutionStatus(state='unknown', key=value.key)

    run = worker._await_resolution_with_renewal
    assert callable(run)
    result = await run(initial, operation=operation(), refresh=refresh, renew=renew)
    assert result.key == initial.key


@pytest.mark.parametrize('change', ['renewed', 'revoked', 'scope-changed'])
async def test_context_rechecks_live_authority_after_async_source_validation(
    monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    root = RootConfig.from_payload({
        'schema_version': 1, 'runtime_root': '/private/runtime', 'wheel_root': '/private/wheels',
        'environment_root': '/private/environments', 'stage_root': '/private/stage',
        'interpreter': {'implementation': 'cpython', 'version': '3.12.0', 'abi': 'cp312',
            'platform': 'linux_x86_64', 'binary_sha256': 'a' * 64, 'base_runtime_sha256': 'b' * 64},
        'cache_policy': {'max_bytes': 2**30, 'max_entries': 8, 'disk_reserve_bytes': 0,
            'retention_seconds': 60}, 'runtime_max_bytes': 2**28, 'wheel_max_bytes': 2**28,
        'max_environment_bytes': 2**28})
    wheel = WheelDownload('click-8.1.7-py3-none-any.whl', 'click', '8.1.7', 'a' * 64, 10,
        'https://files.pythonhosted.org/packages/aa/bb/click-8.1.7-py3-none-any.whl')
    supply = RootSupplyConfig(Path('/private/state'), (wheel,), 64 * 1024**2, 0, 30)
    original = request(lease=0.05)
    planning = original.claim.planning.model_copy(update={
        'expected_root_config_sha256': digest(root.to_payload()),
        'expected_catalog_sha256': supply.digest})
    original = original.model_copy(update={'claim': original.claim.model_copy(update={'planning': planning})})
    live = original.claim

    async def source(value: PythonMetadataPlanningRequest, policy: BrokerPolicy, deadline: float) -> None:
        nonlocal live
        await asyncio.sleep(0.08)  # Snapshot validation crosses the first lease.
        updates: dict[str, object] = {'lease_expires_at': datetime.now(UTC) + timedelta(seconds=0.2)}
        if change == 'revoked':
            updates['lease_token'] = UUID(int=99)
        elif change == 'scope-changed':
            updates['planning'] = planning.model_copy(update={
                'draft': planning.draft.model_copy(update={'source_sha256': 'f' * 64})})
        live = live.model_copy(update=updates)

    def controls(value: PythonMetadataPlanningRequest, policy: BrokerPolicy, deadline: float) -> None:
        pass

    monkeypatch.setattr(worker, 'read_root_configuration', lambda: root)
    monkeypatch.setattr(worker, 'read_supply_configuration', lambda: supply)
    monkeypatch.setattr(worker, 'validate_configuration', lambda *args: None)
    monkeypatch.setattr(worker, '_source', source)
    monkeypatch.setattr(worker, '_controls', controls)
    if change == 'renewed':
        await worker._context(original, BrokerPolicy(), root, supply,
            original.claim.task_deadline, current_claim=lambda: live)
    else:
        with pytest.raises(ValueError, match='resolution_scope_changed'):
            await worker._context(original, BrokerPolicy(), root, supply,
                original.claim.task_deadline, current_claim=lambda: live)
