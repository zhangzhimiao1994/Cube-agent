"""RunService may release a silent runtime, but must not invent completion."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunRecord, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_terminal_hooks import ExecutableFakeRepository, RuntimeCompletes


class ScriptedRuntime(RuntimeCompletes):
    def __init__(self, mode: TaskMode, *, tail: str = 'empty', repository: ExecutableFakeRepository) -> None:
        self.mode, self.tail, self.repository = mode, tail, repository

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        if self.tail == 'artifact':
            yield RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=1, run_id=context.run_id,
                artifact=Artifact(id=uuid4(), type='text', producer='main', content={'text': 'Draft only.'}))
        elif self.tail == 'message':
            yield RunEvent(kind=EventKind.MESSAGE_CREATED, sequence=1, run_id=context.run_id,
                           actor='main_agent', message='A natural language reply, not a terminal event.')
        elif self.tail in {'completed', 'cancelled', 'unknown_model', 'unknown_tool'}:
            yield RunEvent(kind=EventKind.MESSAGE_CREATED, sequence=1, run_id=context.run_id,
                           actor='main_agent', message='A natural language reply without an attachment.')
            yield RunEvent(kind={'completed': EventKind.RUNTIME_COMPLETED,
                'cancelled': EventKind.RUNTIME_CANCELLED}.get(self.tail, EventKind.RUNTIME_FAILED),
                sequence=2, run_id=context.run_id, reason={'unknown_model': 'model_outcome_uncertain',
                    'unknown_tool': 'tool_outcome_uncertain'}.get(self.tail))
        elif self.tail == 'transferred':
            self.repository.row.worker_id = 'new-worker'
            self.repository.row.worker_lease_token = uuid4()
        elif self.tail.startswith('external_'):
            self.repository.row.status = self.tail.removeprefix('external_')


class FencedRepository(ExecutableFakeRepository):
    async def fail_run(self, run_id: UUID, *, reason: str, worker_id: str | None = None,
                       worker_lease_token: UUID | None = None) -> RunRecord:
        if (worker_id, worker_lease_token) != (self.row.worker_id, self.row.worker_lease_token):
            raise RunConflict('run worker execution ownership changed')
        return await super().fail_run(run_id, reason=reason, worker_id=worker_id,
                                      worker_lease_token=worker_lease_token)

    async def release_worker_execution(self, tenant_id: UUID, run_id: UUID, *, worker_id: str,
                                       worker_lease_token: UUID, complete_if_running: bool = False) -> RunRecord:
        if (worker_id, worker_lease_token) != (self.row.worker_id, self.row.worker_lease_token):
            raise RunConflict('run worker execution ownership changed')
        return await super().release_worker_execution(tenant_id, run_id, worker_id=worker_id,
            worker_lease_token=worker_lease_token, complete_if_running=complete_if_running)


def setup(mode: TaskMode, *, tail: str, engineering: bool = False) -> tuple[RunService, ExecutableFakeRepository]:
    route: dict[str, object] = {'source': 'manual'}
    if engineering:
        route.update(project_delivery='workspace', artifact_strategy='workspace_bundle',
                     generated_project_contract_version=1)
    repository = FencedRepository(routing_decision=route)
    repository.row.mode = mode.value
    runtime = ScriptedRuntime(mode, tail=tail, repository=repository)
    service = RunService(cast(RunRepository, repository), runtime_registry=RuntimeRegistry((runtime,)),
                         router=None, task_queue=object())  # type: ignore[arg-type]
    return service, repository


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
@pytest.mark.parametrize('tail', ['empty', 'artifact', 'message'])
@pytest.mark.parametrize('engineering', [False, True])
async def test_eof_without_explicit_terminal_never_completes(mode: TaskMode, tail: str, engineering: bool) -> None:
    service, repository = setup(mode, tail=tail, engineering=engineering)
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.FAILED
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in repository.event_log)
    assert any(event.kind is EventKind.RUNTIME_FAILED for event in repository.event_log)
    assert repository.row.worker_id is None and repository.row.worker_lease_token is None


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_explicit_completion_allows_natural_language_reply_without_artifacts(mode: TaskMode) -> None:
    service, repository = setup(mode, tail='completed')
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.COMPLETED
    assert repository.artifacts == []
    assert not any(event.kind is EventKind.RUNTIME_FAILED for event in repository.event_log)


@pytest.mark.parametrize('tail,status', [('cancelled', RunStatus.CANCELLED),
    ('unknown_model', RunStatus.FAILED), ('unknown_tool', RunStatus.FAILED),
    ('external_cancelled', RunStatus.CANCELLED), ('external_paused', RunStatus.PAUSED),
    ('external_waiting_approval', RunStatus.WAITING_APPROVAL)])
async def test_existing_terminal_and_external_control_identity_is_preserved(tail: str, status: RunStatus) -> None:
    service, repository = setup(TaskMode.DISCUSS, tail=tail)
    result = await service.execute(repository.run_id)
    assert result.status is status
    failures = [event for event in repository.event_log if event.kind is EventKind.RUNTIME_FAILED]
    if tail.startswith('unknown_'):
        assert [event.reason for event in failures] == [
            'model_outcome_uncertain' if tail == 'unknown_model' else 'tool_outcome_uncertain']
    else:
        assert failures == []


async def test_silent_old_worker_cannot_fail_or_release_a_new_owner() -> None:
    service, repository = setup(TaskMode.DISPATCH, tail='transferred')
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.RUNNING
    assert repository.row.worker_id == 'new-worker'
    assert repository.row.worker_lease_token is not None
    assert not any(event.kind is EventKind.RUNTIME_FAILED for event in repository.event_log)
