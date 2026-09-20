from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.instruction_context import InstructionContext, InstructionContextLoader
from agent_hub.runtime.registry import RuntimeRegistry
from tests.contracts.test_runtime_contract import FakeGateway
from tests.integration.runtime.test_crew_adapter import FastFactory, one_step_plan
from tests.unit.capabilities.test_scoped_read import project_path, write_file
from tests.unit.runs.test_terminal_hooks import (
    TENANT_ID,
    ExecutableFakeRepository,
    RuntimeCompletes,
)


@pytest.mark.parametrize('available', [True, False])
@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH])
async def test_service_loads_real_files_and_persists_metadata_before_gateway(
    tmp_path: Path, available: bool, mode: TaskMode,
) -> None:
    repository = ExecutableFakeRepository(routing_decision={
        'source': 'manual', 'project_id': 'project-a', 'workspace_session_id': 'session-a',
        'sandbox_profile': 'read_only', 'requested_permissions': ['workspace.read'],
        'instruction_context': {'text': 'FORGED', 'loaded': True},
    })
    repository.row.mode = mode.value
    if available:
        write_file(tmp_path, f'{project_path(TENANT_ID)}/AGENTS.md', 'PRIVATE-RULES')
    gateway = FakeGateway()
    service = RunService(
        cast(RunRepository, repository), runtime_registry=RuntimeRegistry((
            DirectRuntime(gateway, logical_model='main') if mode is TaskMode.DIRECT
            else CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory()),
        )),
        router=None, task_queue=cast(TaskQueue, object()),
        instruction_context_loader=InstructionContextLoader(repository=repository, project_root=tmp_path),
    )
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.COMPLETED
    loaded = [event for event in repository.event_log if event.kind == 'context.loaded']
    injected = [event for event in repository.event_log if event.kind == 'context.injected']
    assert len(loaded) == 1
    assert len(injected) == int(available)
    assert loaded[0].sequence == 1
    if available:
        assert loaded[0].payload['load_id'] == injected[0].payload['load_id']
        assert 'PRIVATE-RULES' in str(gateway.requests[0].messages[-1].content)
    assert 'FORGED' not in str(gateway.requests[0].messages[-1].content)
    assert all('PRIVATE-RULES' not in json.dumps(event.to_payload()) for event in repository.event_log)
    assert [event.sequence for event in repository.event_log] == list(range(1, len(repository.event_log) + 1))


async def test_service_does_not_activate_loader_for_discuss(tmp_path: Path) -> None:
    class DiscussCompletes(RuntimeCompletes):
        mode = TaskMode.DISCUSS

    repository = ExecutableFakeRepository(routing_decision={})
    repository.row.mode = TaskMode.DISCUSS.value
    service = RunService(
        cast(RunRepository, repository), runtime_registry=RuntimeRegistry((DiscussCompletes(),)),
        router=None, task_queue=cast(TaskQueue, object()),
        instruction_context_loader=InstructionContextLoader(repository=repository, project_root=tmp_path),
    )
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.COMPLETED
    assert not any(event.kind in ('context.loaded', 'context.injected') for event in repository.event_log)


@pytest.mark.parametrize('change', [
    'cancelled', 'paused', 'waiting_approval', 'completed',
    'lease_token', 'worker_id', 'expired_lease', 'missing_lease',
])
@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH])
async def test_control_change_during_load_prevents_evidence_and_model(
    tmp_path: Path, change: str, mode: TaskMode,
) -> None:
    loaded = asyncio.Event()
    release = asyncio.Event()

    class PausingLoader(InstructionContextLoader):
        async def load(self, *, tenant_id: UUID, run_id: UUID) -> InstructionContext:
            result = await super().load(tenant_id=tenant_id, run_id=run_id)
            loaded.set()
            await release.wait()
            return result

    repository = ExecutableFakeRepository(routing_decision={
        'project_id': 'project-a', 'workspace_session_id': 'session-a',
        'sandbox_profile': 'read_only', 'requested_permissions': ['workspace.read'],
    })
    repository.row.mode = mode.value
    write_file(tmp_path, f'{project_path(TENANT_ID)}/AGENTS.md', 'PRIVATE-RULES')
    gateway = FakeGateway()
    service = RunService(
        cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((
            DirectRuntime(gateway, logical_model='main') if mode is TaskMode.DIRECT
            else CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory()),
        )),
        router=None, task_queue=cast(TaskQueue, object()),
        instruction_context_loader=PausingLoader(repository=repository, project_root=tmp_path),
    )
    execution = asyncio.create_task(service.execute(repository.run_id))
    try:
        await asyncio.wait_for(loaded.wait(), timeout=3)
        if change == 'lease_token':
            repository.row.worker_lease_token = uuid4()
        elif change == 'worker_id':
            repository.row.worker_id = 'replacement-worker'
        elif change == 'expired_lease':
            repository.row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == 'missing_lease':
            repository.row.worker_lease_expires_at = None
        else:
            repository.row.status = change
        expected_status = RunStatus(repository.row.status)
        expected_token = repository.row.worker_lease_token
        release.set()
        result = await asyncio.wait_for(execution, timeout=3)
        assert gateway.requests == []
        assert repository.event_log == []
        assert result.status is expected_status
        assert repository.row.worker_lease_token == expected_token
    finally:
        release.set()
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.parametrize('change', ['permissions', 'sandbox', 'project', 'session'])
@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH])
async def test_authorization_change_during_load_discards_content_but_completes_chat(
    tmp_path: Path, change: str, mode: TaskMode,
) -> None:
    loaded = asyncio.Event()
    release = asyncio.Event()

    class PausingLoader(InstructionContextLoader):
        async def load(self, *, tenant_id: UUID, run_id: UUID) -> InstructionContext:
            result = await super().load(tenant_id=tenant_id, run_id=run_id)
            assert result.sources[0].status == 'loaded'
            loaded.set()
            await release.wait()
            return result

    repository = ExecutableFakeRepository(routing_decision={
        'project_id': 'project-a', 'workspace_session_id': 'session-a',
        'sandbox_profile': 'read_only', 'requested_permissions': ['workspace.read'],
    })
    repository.row.mode = mode.value
    write_file(tmp_path, f'{project_path(TENANT_ID)}/AGENTS.md', 'REVOKED-PRIVATE-RULES')
    gateway = FakeGateway()
    service = RunService(
        cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((
            DirectRuntime(gateway, logical_model='main') if mode is TaskMode.DIRECT
            else CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory()),
        )),
        router=None, task_queue=cast(TaskQueue, object()),
        instruction_context_loader=PausingLoader(repository=repository, project_root=tmp_path),
    )
    execution = asyncio.create_task(service.execute(repository.run_id))
    try:
        await asyncio.wait_for(loaded.wait(), timeout=3)
        routing = dict(repository.row.routing_decision or {})
        updates: dict[str, dict[str, object]] = {
            'permissions': {'requested_permissions': []},
            'sandbox': {'sandbox_profile': 'none'},
            'project': {'project_id': 'project-b'},
            'session': {'workspace_session_id': 'session-b'},
        }
        routing.update(updates[change])
        repository.row.routing_decision = routing
        release.set()
        result = await asyncio.wait_for(execution, timeout=3)
        assert result.status is RunStatus.COMPLETED
        assert len(gateway.requests) == 1
        assert all('REVOKED-PRIVATE-RULES' not in str(message.content)
                   for message in gateway.requests[0].messages)
        assert not any(event.kind == 'context.injected' for event in repository.event_log)
        evidence = [event for event in repository.event_log if event.kind == 'context.loaded']
        assert len(evidence) == 1
        assert 'loaded' in str(evidence[0].payload['sources'])
    finally:
        release.set()
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.parametrize('change', ['permissions', 'missing_session'])
@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH])
async def test_authorization_change_between_files_discards_first_loaded_source(
    tmp_path: Path, change: str, mode: TaskMode,
) -> None:
    first_read = asyncio.Event()
    release = asyncio.Event()

    class InterleavingRepository(ExecutableFakeRepository):
        reads = 0

        async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
            self.reads += 1
            if self.reads == 2:
                first_read.set()
                await release.wait()
            return await super().get(tenant_id, run_id)

    repository = InterleavingRepository(routing_decision={
        'project_id': 'project-a', 'workspace_session_id': 'session-a',
        'sandbox_profile': 'read_only', 'requested_permissions': ['workspace.read'],
    })
    repository.row.mode = mode.value
    write_file(tmp_path, f'{project_path(TENANT_ID)}/AGENTS.md', 'FIRST-PRIVATE-RULES')
    write_file(tmp_path, f'{project_path(TENANT_ID)}/SKILL.md', 'SECOND-PRIVATE-RULES')
    gateway = FakeGateway()
    service = RunService(
        cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((
            DirectRuntime(gateway, logical_model='main') if mode is TaskMode.DIRECT
            else CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory()),
        )),
        router=None, task_queue=cast(TaskQueue, object()),
        instruction_context_loader=InstructionContextLoader(repository=repository, project_root=tmp_path),
    )
    execution = asyncio.create_task(service.execute(repository.run_id))
    try:
        await asyncio.wait_for(first_read.wait(), timeout=3)
        routing = dict(repository.row.routing_decision or {})
        if change == 'permissions':
            routing['requested_permissions'] = []
        else:
            routing['workspace_session_id'] = 'missing-session'
        repository.row.routing_decision = routing
        release.set()
        result = await asyncio.wait_for(execution, timeout=3)
        assert result.status is RunStatus.COMPLETED
        assert len(gateway.requests) == 1
        assert all('PRIVATE-RULES' not in str(message.content)
                   for message in gateway.requests[0].messages)
        assert not any(event.kind == 'context.injected' for event in repository.event_log)
        evidence = [event for event in repository.event_log if event.kind == 'context.loaded']
        assert len(evidence) == 1
        sources = cast(tuple[dict[str, object], ...], evidence[0].payload['sources'])
        assert sources[0]['status'] == 'loaded'
        assert sources[1]['status'] == 'unavailable'
    finally:
        release.set()
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
