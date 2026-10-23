"""Driver-only regression: published inspection must survive RunService resume.

Repository/runtime transports are doubles; context construction and execute are
the real RunService. No project source, model calls or native execution are used.
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.action_context_artifacts import action_context_artifacts
from agent_hub.runs.repository import RunRecord
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_direct_action_approval_checkpoint import action_checkpoint
from tests.unit.runs.test_terminal_hooks import (
    TENANT_ID,
    ExecutableFakeRepository,
    FakeRunRow,
    FakeTransaction,
)


class ResumingRepository(ExecutableFakeRepository):
    def __init__(self, *, conversation: bool) -> None:
        super().__init__(routing_decision={
            'project_id':'inspection-project', 'workspace_session_id':'inspection-session',
            **({'conversation_id':'inspection-review'} if conversation else {}),
        })
        self.row.mode = TaskMode.DIRECT.value
        self.row.actor_role = Role.OPERATOR.value
        self.inspection = Artifact(id=uuid4(), type='tool_result', producer='main_agent',
            content={'agent_id':'main_agent', 'tool_name':'project.inspect',
                     'call_id':'inspect_one', 'arguments_sha256':hashlib.sha256(b'{}').hexdigest(),
                     'result':{'schema_version':1, 'summary':'Synthetic inspection metadata only',
                         'scope':{'tenant_id':str(TENANT_ID), 'run_id':str(self.run_id),
                                  'project_id':'inspection-project',
                                  'workspace_session_id':'inspection-session'}}})
        self.checkpoint = action_checkpoint(self.run_id)
        self.artifacts.append(self.inspection)
        self.event_log.extend((
            RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=1, run_id=self.run_id,
                     actor='main_agent', artifact=self.inspection,
                     payload={'tool_name':'project.inspect'}),
            RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=2, run_id=self.run_id,
                     checkpoint=self.checkpoint),
        ))

    async def claim_for_execution(self, session: FakeTransaction, run_id: UUID, *,
        allow_running_recovery: bool, worker_id: str | None = None,
        worker_lease_token: UUID | None = None, worker_lease_expires_at: datetime | None = None,
    ) -> tuple[FakeRunRow, RuntimeCheckpoint | None] | RunRecord:
        claimed = await super().claim_for_execution(session, run_id,
            allow_running_recovery=allow_running_recovery, worker_id=worker_id,
            worker_lease_token=worker_lease_token, worker_lease_expires_at=worker_lease_expires_at)
        assert isinstance(claimed, tuple)
        return claimed[0], self.checkpoint

    async def raw_artifacts(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
        assert tenant_id == TENANT_ID and run_id == self.run_id
        return tuple(artifact.to_payload() for artifact in self.artifacts)


class ContextProbe:
    mode = TaskMode.DIRECT

    def __init__(self) -> None:
        self.seen: TaskContext | None = None

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        assert checkpoint.runtime_type == 'direct_action'

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.seen = context
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=context.run_id)

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError('not used')

    async def cancel(self) -> None:
        pass


@pytest.mark.parametrize('conversation', [False, True])
async def test_full_service_resume_rehydrates_published_current_run_inspection(conversation: bool) -> None:
    repository = ResumingRepository(conversation=conversation)
    runtime = ContextProbe()
    service = RunService(cast(Any, repository), runtime_registry=RuntimeRegistry((runtime,)),
        router=None, task_queue=cast(Any, object()), worker_id='inspection-review-fresh-worker')
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.COMPLETED
    assert runtime.seen is not None and runtime.seen.checkpoint is not None
    restored = {artifact.id:artifact for artifact in runtime.seen.artifacts}
    assert repository.inspection.id in restored, 'RunService omitted its own published inspection on resume'
    assert restored[repository.inspection.id].to_payload() == repository.inspection.to_payload()


async def observations(repository: ResumingRepository) -> tuple[Artifact, ...]:
    assert repository.row.routing_decision is not None
    return await action_context_artifacts(repository, tenant_id=TENANT_ID,
        run_id=repository.run_id, checkpoint=repository.checkpoint,
        routing_decision=repository.row.routing_decision)


def replace_inspection(repository: ResumingRepository, **changes: Any) -> Artifact:
    payload = repository.inspection.to_payload()
    payload.update(changes)
    payload['content_sha256'] = ''
    replacement = Artifact.from_payload(payload)
    repository.event_log[0] = repository.event_log[0].model_copy(update={'artifact':replacement})
    return replacement


@pytest.mark.parametrize('mutation', [
    'unpublished', 'after_checkpoint', 'not_artifact_created', 'other_type', 'other_producer', 'other_tool',
])
async def test_unpublished_suffix_and_unrelated_outputs_are_not_context(mutation: str) -> None:
    repository = ResumingRepository(conversation=True)
    artifact_event, boundary = repository.event_log
    if mutation == 'unpublished':
        repository.event_log = [boundary]
    elif mutation == 'after_checkpoint':
        repository.event_log = [boundary.model_copy(update={'sequence':1}),
            artifact_event.model_copy(update={'sequence':2})]
    elif mutation == 'not_artifact_created':
        repository.event_log[0] = artifact_event.model_copy(update={'kind':EventKind.TOOL_COMPLETED})
    elif mutation == 'other_type':
        replace_inspection(repository, type='model_attempt')
    elif mutation == 'other_producer':
        replace_inspection(repository, producer='other_agent')
    else:
        content = cast(dict[str, Any], repository.inspection.to_payload()['content'])
        content['tool_name'] = 'project.verify'
        replace_inspection(repository, content=content)
    assert await observations(repository) == ()


@pytest.mark.parametrize('mutation', [
    'artifact_hash', 'agent_id', 'arguments_sha256', 'scope_tenant', 'scope_run',
    'scope_project', 'scope_session', 'scope_extra', 'scope_missing_session', 'scope_wrong_session_key',
])
async def test_inspection_identity_or_scope_corruption_is_rejected(mutation: str) -> None:
    repository = ResumingRepository(conversation=True)
    if mutation == 'artifact_hash':
        bad = repository.inspection.model_copy(update={'content_sha256':'0'*64})
        repository.event_log[0] = repository.event_log[0].model_copy(update={'artifact':bad})
    else:
        content = cast(dict[str, Any], repository.inspection.to_payload()['content'])
        if mutation == 'agent_id':
            content['agent_id'] = 'other_agent'
        elif mutation == 'arguments_sha256':
            content['arguments_sha256'] = hashlib.sha256(b'{"unexpected":true}').hexdigest()
        else:
            scope = content['result']['scope']
            if mutation == 'scope_tenant':
                scope['tenant_id'] = str(uuid4())
            elif mutation == 'scope_run':
                scope['run_id'] = str(uuid4())
            elif mutation == 'scope_project':
                scope['project_id'] = 'other-project'
            elif mutation == 'scope_session':
                scope['workspace_session_id'] = 'other-session'
            elif mutation == 'scope_extra':
                scope['unexpected'] = 'extra'
            elif mutation == 'scope_missing_session':
                scope.pop('workspace_session_id')
            else:
                scope['session_id'] = scope.pop('workspace_session_id')
        replace_inspection(repository, content=content)
    with pytest.raises(ValueError):
        await observations(repository)


@pytest.mark.parametrize('mutation', [
    'cross_run_event', 'duplicate_sequence', 'missing_checkpoint', 'other_checkpoint_id',
    'same_id_changed_payload', 'same_id_wrong_event_kind',
])
async def test_only_exact_same_scope_checkpoint_prefix_is_accepted(mutation: str) -> None:
    repository = ResumingRepository(conversation=False)
    artifact_event, boundary = repository.event_log
    if mutation == 'cross_run_event':
        repository.event_log[0] = artifact_event.model_copy(update={'run_id':uuid4()})
    elif mutation == 'duplicate_sequence':
        repository.event_log[1] = boundary.model_copy(update={'sequence':artifact_event.sequence})
    elif mutation == 'missing_checkpoint':
        repository.event_log = [artifact_event]
    elif mutation == 'other_checkpoint_id':
        different = repository.checkpoint.model_copy(update={'id':uuid4()})
        repository.event_log[1] = boundary.model_copy(update={'checkpoint':different})
    elif mutation == 'same_id_changed_payload':
        payload = repository.checkpoint.to_payload()
        cast(dict[str, Any], payload['state'])['context_sha256'] = 'f'*64
        payload['state_sha256'] = ''
        repository.event_log[1] = boundary.model_copy(update={'checkpoint':RuntimeCheckpoint.from_payload(payload)})
    else:
        repository.event_log[1] = boundary.model_copy(update={'kind':EventKind.TOOL_COMPLETED})
    with pytest.raises(ValueError):
        await observations(repository)


@pytest.mark.parametrize('conflict', [False, True])
async def test_repeated_inspection_id_is_deduplicated_or_rejected(conflict: bool) -> None:
    repository = ResumingRepository(conversation=True)
    original_event, boundary = repository.event_log
    repeated = repository.inspection
    if conflict:
        content = cast(dict[str, Any], repeated.to_payload()['content'])
        content['result']['summary'] = 'Different metadata with the same artifact ID'
        repeated = replace_inspection(repository, content=content)
    repository.event_log = [original_event, original_event.model_copy(update={'sequence':2,'artifact':repeated}),
        boundary.model_copy(update={'sequence':3})]
    if conflict:
        with pytest.raises(ValueError, match='identity conflicts'):
            await observations(repository)
    else:
        assert await observations(repository) == (repository.inspection,)


@pytest.mark.parametrize('other_output', ['model', 'verify'])
async def test_conflicting_id_cannot_be_hidden_by_an_unrelated_artifact_type(other_output: str) -> None:
    repository = ResumingRepository(conversation=True)
    original_event, boundary = repository.event_log
    if other_output == 'model':
        different = Artifact(id=repository.inspection.id, type='text', producer='main',
                             content={'text':'Synthetic model output, not an inspection'})
    else:
        different = Artifact(id=repository.inspection.id, type='tool_result', producer='main_agent',
                             content={'agent_id':'main_agent','tool_name':'project.verify','status':'succeeded'})
    repository.event_log = [original_event, original_event.model_copy(update={'sequence':2,'artifact':different}),
        boundary.model_copy(update={'sequence':3})]
    with pytest.raises(ValueError):
        await observations(repository)
