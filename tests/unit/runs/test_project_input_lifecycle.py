from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_terminal_hooks import ExecutableFakeRepository, RuntimeCompletes


class ObservingRuntime(RuntimeCompletes):
    def __init__(self, mode: TaskMode, calls: list[str]) -> None:
        self.mode = mode
        self.calls = calls

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.calls.append('model')
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=context.run_id)


def route() -> dict[str, object]:
    return {'project_id': 'project-a', 'workspace_session_id': 'session-a',
            'sandbox_profile': 'workspace_write',
            'requested_permissions': ['workspace.read', 'workspace.write', 'command.run'],
            'project_input_contract_version': 1,
            'project_input': {'schema_version': 1, 'kind': 'managed_workspace', 'editable_paths': ['src']}}


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_shared_original_preparation_runs_before_any_mode_model(mode: TaskMode) -> None:
    repository = ExecutableFakeRepository(routing_decision=route())
    repository.row.mode = mode.value
    calls: list[str] = []

    async def prepare(context: TaskContext) -> TaskContext:
        calls.append('original-baseline')
        state: JsonValue = {'schema_version': 1, 'phase': 'observed',
                            'checks': ({'phase': 'test', 'status': 'failed'},)}
        repository.row.routing_decision = {**(repository.row.routing_decision or {}),
            'project_input_preparation': {'schema_version': 1, 'phase': 'observed',
                                          'checks': [{'phase': 'test', 'status': 'failed'}]}}
        return context.model_copy(update={'routing_decision': {
            **context.routing_decision, 'project_input_preparation': state,
        }})

    service = RunService(
        cast(RunRepository, repository), runtime_registry=RuntimeRegistry((ObservingRuntime(mode, calls),)),
        router=None, task_queue=cast(TaskQueue, object()), project_input_preparer=prepare,
    )
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.COMPLETED
    assert calls == ['original-baseline', 'model']


@pytest.mark.parametrize('change', ['missing', 'unobserved', 'tenant', 'permissions', 'tokens', 'cancelled',
    'scope_changed_in_db', 'state_not_persisted', 'lease_lost', 'actor', 'checkpoint', 'request', 'timeout'])
async def test_missing_invalid_or_revoked_preparation_never_reaches_model(change: str) -> None:
    repository = ExecutableFakeRepository(routing_decision=route())
    calls: list[str] = []

    async def prepare(context: TaskContext) -> TaskContext:
        calls.append('original-baseline')
        if change == 'cancelled':
            repository.row.status = RunStatus.CANCELLED.value
        new_route: dict[str, JsonValue] = {
            **context.routing_decision, 'project_input_preparation': {'schema_version': 1, 'phase': 'observed'},
        }
        if change == 'unobserved':
            new_route['project_input_preparation'] = {'schema_version': 1, 'phase': 'executing'}
        if change == 'permissions':
            new_route['requested_permissions'] = ('workspace.read', 'workspace.write', 'network.read', 'command.run')
        updates: dict[str, object] = {'routing_decision': new_route}
        if change == 'tokens':
            updates['token_budget'] = context.token_budget + 1
        if change == 'tenant':
            from uuid import uuid4
            updates['tenant_id'] = uuid4()
        if change == 'actor':
            from uuid import uuid4
            updates['actor_id'] = uuid4()
        if change == 'request':
            updates['request'] = 'different original requirement'
        if change == 'checkpoint':
            updates['checkpoint'] = 'unvalidated checkpoint'
        if change == 'timeout':
            updates['timeout_seconds'] = context.timeout_seconds + 1
        repository.row.routing_decision = cast(dict[str, object], new_route)
        if change == 'scope_changed_in_db':
            repository.row.routing_decision = {**repository.row.routing_decision,
                                                'workspace_session_id': 'other-session'}
        if change == 'state_not_persisted':
            repository.row.routing_decision = route()
        if change == 'lease_lost':
            repository.row.worker_lease_token = None
        return context.model_copy(update=updates)

    service = RunService(
        cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((ObservingRuntime(TaskMode.DISPATCH, calls),)),
        router=None, task_queue=cast(TaskQueue, object()),
        project_input_preparer=None if change == 'missing' else prepare,
    )
    result = await service.execute(repository.run_id)
    assert result.status in ({RunStatus.RUNNING} if change == 'lease_lost' else
                            {RunStatus.FAILED, RunStatus.CANCELLED})
    assert 'model' not in calls


async def test_user_cancellation_interrupts_original_preparation_before_any_model() -> None:
    repository = ExecutableFakeRepository(routing_decision=route())
    calls: list[str] = []
    started, stopped, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def prepare(context: TaskContext) -> TaskContext:
        started.set()
        try:
            await release.wait()
        finally:
            stopped.set()
        return context

    service = RunService(cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((ObservingRuntime(TaskMode.DISPATCH, calls),)),
        router=None, task_queue=cast(TaskQueue, object()), project_input_preparer=prepare,
        run_worker_lease_seconds=1)
    task = asyncio.create_task(service.execute(repository.run_id))
    try:
        await asyncio.wait_for(started.wait(), 1)
        repository.row.status = RunStatus.CANCELLED.value
        await asyncio.wait_for(stopped.wait(), 1.5)
        assert (await asyncio.wait_for(task, 1)).status is RunStatus.CANCELLED
        assert 'model' not in calls
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('cleanup', ['error', 'blocked'])
async def test_preparation_cancel_cleanup_failure_keeps_execution_nonquiescent(cleanup: str) -> None:
    repository = ExecutableFakeRepository(routing_decision=route())
    calls: list[str] = []
    started, cleanup_failed = asyncio.Event(), asyncio.Event()

    async def prepare(context: TaskContext) -> TaskContext:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_failed.set()
            if cleanup == 'blocked':
                await asyncio.Event().wait()
            else:
                raise RuntimeError('original execution cleanup is not confirmed')
        return context

    service = RunService(cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((ObservingRuntime(TaskMode.DISPATCH, calls),)),
        router=None, task_queue=cast(TaskQueue, object()), project_input_preparer=prepare,
        run_worker_lease_seconds=1)
    task = asyncio.create_task(service.execute(repository.run_id))
    try:
        await asyncio.wait_for(started.wait(), 1)
        repository.row.status = RunStatus.CANCELLED.value
        result = await asyncio.wait_for(task, 8)
        assert result.status is RunStatus.CANCELLED
        assert cleanup_failed.is_set() and 'model' not in calls
        assert repository.row.worker_id is not None
        assert repository.row.worker_lease_token is not None
        assert repository.row.worker_lease_expires_at is not None
        assert repository.row.worker_heartbeat_at is None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
