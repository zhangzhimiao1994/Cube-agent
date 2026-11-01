"""Focused component regressions: private history, real continuation storage, no runtime."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Unpack
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from agent_hub.auth.models import Role
from agent_hub.db.models import RunRow
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.conversation_inputs import ConversationInputs
from agent_hub.runs.repository import RunConflict
from agent_hub.runtime.continuation_artifacts import _encode, load_continuation, save_continuation
from agent_hub.runtime.contracts import Artifact, RuntimeCheckpoint
from tests.unit.runs.conversation_authenticated_input_typed_fixture import (
    Case,
    InputArgs,
    InputChanges,
    MemoryRowRepository,
    NoDatabaseRepository,
    RevocableArtifacts,
    invoke_builder,
    json_map,
    mutable,
    present,
)

VERSION = 'conversation_input_snapshot_version'
STATE = 'conversation_input_snapshot'


class MemoryRuns(MemoryRowRepository):
    """Only the row-lock boundary is substituted; _record remains production code."""
    def __init__(self) -> None:
        row = RunRow(
            id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role='operator',
            mode='dispatch', request='如何使用当前项目？', status='running', version=1,
            created_at=datetime.now(UTC), worker_id='worker-a', worker_lease_token=uuid4(),
            worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            worker_heartbeat_at=datetime.now(UTC),
            routing_decision={VERSION: 1, 'conversation_id': 'conversation-a',
                'project_id': 'project-a', 'workspace_session_id': 'session-a',
                'sandbox_profile': 'read_only', 'permission_mode': 'read_only',
                'execution_backend': 'systemd', 'requested_permissions': ['workspace.read']},
        )
        super().__init__(row)

@pytest.fixture
def case() -> Case:
    runs = MemoryRuns()
    row = runs.row
    args: InputArgs = {'tenant_id': row.tenant_id, 'run_id': row.id, 'actor_id': row.actor_id,
        'actor_role': Role.OPERATOR, 'mode': TaskMode.DISPATCH, 'worker_id': present(row.worker_id),
        'execution_token': row.worker_lease_token, 'current_request': row.request,
        'routing_decision': json.loads(json.dumps(mutable(row.routing_decision))), 'checkpoint': None}
    return runs, RevocableArtifacts(), args


def history(text: str) -> Artifact:
    return Artifact(id=uuid5(NAMESPACE_URL, text), type='text', producer='conversation_history',
                    content={'text': text})


def checkpoint(args: InputArgs) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(id=uuid4(), tenant_id=args['tenant_id'], run_id=args['run_id'],
        mode=args['mode'], runtime_type='test_action', runtime_version='1', state={'resumed': True})


async def invoke(
    case: tuple[MemoryRowRepository | NoDatabaseRepository, RevocableArtifacts, InputArgs],
    builder: Callable[[], Awaitable[object]], **changes: Unpack[InputChanges],
) -> ConversationInputs:
    runs, artifacts, args = case
    merged: InputArgs = {**args, **changes}
    return await invoke_builder(runs, artifacts, merged, builder)


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_readonly_history_stays_frozen_across_resume_and_next_run_is_fresh(
    case: Case, tmp_path: Path, mode: TaskMode,
) -> None:
    runs, artifacts, args = case
    runs.row.mode = mode.value
    args['mode'] = mode
    readme = tmp_path / 'README.md'
    readme.write_text('original README', encoding='utf-8')
    calls = []

    async def build() -> tuple[Artifact, ...]:
        calls.append('read')
        return (history(readme.read_text(encoding='utf-8')),)

    original_route = json.loads(json.dumps(args['routing_decision']))
    first = await invoke(case, build)
    assert first.artifacts[0].content['text'] == 'original README'
    assert first.snapshot == mutable(runs.row.routing_decision)[STATE]
    assert args['routing_decision'] == original_route
    assert set(present(first.snapshot)) == {'schema_version', 'scope_sha256', 'reference'}
    assert 'artifacts' not in present(first.snapshot) and 'original README' not in str(first.snapshot)
    readme.write_text('changed README', encoding='utf-8')
    args['routing_decision'] = {**original_route, STATE: first.snapshot}
    runs.row.worker_id = 'worker-b'
    runs.row.worker_lease_token = uuid4()
    args.update({'worker_id': present(runs.row.worker_id), 'execution_token': runs.row.worker_lease_token,
                 'checkpoint': checkpoint(args)})
    restored = await invoke(case, build)
    assert [a.to_payload() for a in restored.artifacts] == [a.to_payload() for a in first.artifacts]
    assert restored.snapshot == first.snapshot and calls == ['read']
    assert readme.read_text(encoding='utf-8') == 'changed README'
    assert mutable(runs.row.routing_decision)['requested_permissions'] == ['workspace.read']
    next_run = MemoryRuns()
    next_run.row.tenant_id, next_run.row.actor_id = args['tenant_id'], args['actor_id']
    next_run.row.mode = mode.value
    next_args: InputArgs = {**args, 'run_id': next_run.row.id, 'checkpoint': None,
                 'worker_id': present(next_run.row.worker_id), 'execution_token': next_run.row.worker_lease_token,
                 'routing_decision': json.loads(json.dumps(next_run.row.routing_decision))}
    fresh = await invoke((next_run, artifacts, next_args), build)
    assert fresh.artifacts[0].content['text'] == 'changed README'
    assert fresh.artifacts[0].id != first.artifacts[0].id and calls == ['read', 'read']


@pytest.mark.parametrize('route', [{}, {'requested_permissions': []}, {'requested_permissions': None}])
async def test_unmarked_legacy_uses_builder_without_database_access(case: Case, route: dict[str, object]) -> None:
    _, artifacts, args = case
    expected = (history('legacy'),)

    async def build() -> tuple[Artifact, ...]:
        return expected

    result = await invoke((NoDatabaseRepository(), artifacts, args), build, routing_decision=route)
    assert result.artifacts is expected and result.snapshot is None


@pytest.mark.parametrize('permissions', [None, [], ['workspace.write'], ['command.run']])
async def test_marker_without_existing_read_permission_cannot_capture_history(
    case: Case, permissions: object,
) -> None:
    runs, artifacts, args = case
    mutable(runs.row.routing_decision)['requested_permissions'] = permissions
    args['routing_decision']['requested_permissions'] = permissions
    calls = []

    async def build() -> tuple[Artifact, ...]:
        calls.append('read')
        return ()

    with pytest.raises(RunConflict):
        await invoke(case, build)
    assert calls == [] and not artifacts._artifacts
    assert STATE not in mutable(runs.row.routing_decision)


@pytest.mark.parametrize('defect', [
    'tenant', 'run', 'actor', 'role', 'mode', 'request', 'worker', 'token', 'expired', 'status',
    'conversation_id', 'project_id', 'workspace_session_id', 'sandbox_profile',
    'permission_mode', 'execution_backend', 'requested_permissions', 'marker_bool',
])
async def test_invalid_current_scope_or_lease_never_reads_history(case: Case, defect: str) -> None:
    runs, artifacts, _ = case
    row = runs.row
    if defect in {'tenant', 'run', 'actor'}:
        setattr(row, {'tenant': 'tenant_id', 'run': 'id', 'actor': 'actor_id'}[defect], uuid4())
    elif defect == 'role': row.actor_role = 'admin'
    elif defect == 'mode': row.mode = 'direct'
    elif defect == 'request': row.request = 'changed request'
    elif defect == 'worker': row.worker_id = 'other-worker'
    elif defect == 'token': row.worker_lease_token = uuid4()
    elif defect == 'expired': row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif defect == 'status': row.status = 'waiting_approval'
    elif defect == 'marker_bool': mutable(row.routing_decision)[VERSION] = True
    else: mutable(row.routing_decision)[defect] = ['workspace.read', 'workspace.write'] if defect == 'requested_permissions' else 'changed'

    async def forbidden() -> tuple[Artifact, ...]:
        pytest.fail('invalid identity/scope must not call history builder')

    with pytest.raises(RunConflict):
        await invoke(case, forbidden)
    assert not artifacts._artifacts and STATE not in mutable(row.routing_decision)


async def test_checkpoint_without_snapshot_cannot_rebuild_history(case: Case) -> None:
    async def forbidden() -> tuple[Artifact, ...]:
        pytest.fail('a checkpoint cannot acquire a new historical input set')

    with pytest.raises(RunConflict):
        await invoke(case, forbidden, checkpoint=checkpoint(case[2]))


@pytest.mark.parametrize('defect', ['duplicate_id', 'wrong_type', 'bad_hash', 'list'])
async def test_invalid_builder_artifacts_are_not_captured(case: Case, defect: str) -> None:
    item = history('one')
    values: dict[str, object] = {'duplicate_id': (item, item), 'wrong_type': (object(),),
              'bad_hash': (item.model_copy(update={'content_sha256': '0' * 64}),), 'list': [item]}

    async def build() -> object:
        return values[defect]

    with pytest.raises(RunConflict):
        await invoke(case, build)
    assert STATE not in mutable(case[0].row.routing_decision) and not case[1]._artifacts


async def captured(case: Case) -> ConversationInputs:
    async def build() -> tuple[Artifact, ...]:
        return (history('PRIVATE_INPUT_CONTENT'),)

    return await invoke(case, build)


@pytest.mark.parametrize('defect', [
    'scope_hash', 'extra_state', 'state_bool', 'reference_hash', 'root_scope', 'root_kind',
    'root_extra', 'duplicate', 'artifact_hash', 'artifact_missing_hash', 'artifact_extra', 'foreign_run',
])
async def test_corrupted_metadata_or_private_body_never_falls_back_to_builder(case: Case, defect: str) -> None:
    runs, artifacts, args = case
    await captured(case)
    state = mutable(mutable(runs.row.routing_decision)[STATE])
    if defect == 'scope_hash': state['scope_sha256'] = '0' * 64
    elif defect == 'extra_state': state['extra'] = True
    elif defect == 'state_bool': state['schema_version'] = True
    elif defect == 'reference_hash': mutable(state['reference'])['content_sha256'] = '0' * 64
    else:
        body = json.loads(_encode(await load_continuation(artifacts,
            tenant_id=args['tenant_id'], run_id=args['run_id'], reference=json_map(state['reference']))))
        if defect == 'root_scope': body['scope']['actor_id'] = str(uuid4())
        elif defect == 'root_kind': body['kind'] = 'another_kind'
        elif defect == 'root_extra': body['extra'] = True
        elif defect == 'duplicate': body['artifacts'] *= 2
        elif defect == 'artifact_hash': body['artifacts'][0]['content_sha256'] = '0' * 64
        elif defect == 'artifact_missing_hash': del body['artifacts'][0]['content_sha256']
        elif defect == 'artifact_extra': body['artifacts'][0]['extra'] = True
        state['reference'] = await save_continuation(artifacts,
            tenant_id=args['tenant_id'], run_id=uuid4() if defect == 'foreign_run' else args['run_id'], value=body)

    async def forbidden() -> tuple[Artifact, ...]:
        pytest.fail('captured state must never trigger live historical rebuilding')

    with pytest.raises(RunConflict) as failure:
        await invoke(case, forbidden)
    assert 'PRIVATE_INPUT_CONTENT' not in str(failure.value)
    assert failure.value.__context__ is None


@pytest.mark.parametrize('defect', ['actor', 'role', 'mode', 'request', 'project', 'permissions'])
async def test_captured_scope_cannot_change_even_when_caller_matches_new_record(case: Case, defect: str) -> None:
    runs, _, args = case
    await captured(case)
    if defect == 'actor':
        runs.row.actor_id = args['actor_id'] = uuid4()
    elif defect == 'role':
        runs.row.actor_role, args['actor_role'] = 'admin', Role.ADMIN
    elif defect == 'mode':
        runs.row.mode, args['mode'] = 'direct', TaskMode.DIRECT
    elif defect == 'request':
        runs.row.request = args['current_request'] = 'a new request'
    else:
        key = 'project_id' if defect == 'project' else 'requested_permissions'
        value = 'project-b' if defect == 'project' else ['workspace.read', 'workspace.write']
        mutable(runs.row.routing_decision)[key] = args['routing_decision'][key] = value

    async def forbidden() -> tuple[Artifact, ...]:
        pytest.fail('a captured scope cannot be rebound by rewriting caller and row')

    with pytest.raises(RunConflict):
        await invoke(case, forbidden)


async def test_valid_snapshot_load_handles_mappingproxy_codec_and_rechecks_lease(case: Case) -> None:
    runs, artifacts, args = case
    result = await captured(case)
    body = await load_continuation(artifacts, tenant_id=args['tenant_id'], run_id=args['run_id'],
                                   reference=json_map(present(result.snapshot)['reference']))
    assert isinstance(body, Mapping) and not isinstance(body, dict)
    def revoked() -> None:
        runs.row.worker_lease_token = uuid4()

    artifacts.after_get = revoked

    async def forbidden() -> tuple[Artifact, ...]:
        pytest.fail('restore cannot build history')

    with pytest.raises(RunConflict):
        await invoke(case, forbidden)


@pytest.mark.parametrize('when', ['builder', 'save'])
async def test_lease_loss_before_cas_cannot_publish_snapshot(case: Case, when: str) -> None:
    runs, artifacts, _ = case
    def revoked_put() -> None:
        runs.row.worker_lease_token = uuid4()

    if when == 'save': artifacts.after_put = revoked_put

    async def build() -> tuple[Artifact, ...]:
        if when == 'builder': runs.row.worker_lease_token = uuid4()
        return (history('one'),)

    with pytest.raises(RunConflict):
        await invoke(case, build)
    assert STATE not in mutable(runs.row.routing_decision)


async def test_capture_race_cannot_replace_first_committed_history(case: Case) -> None:
    async def winner() -> tuple[Artifact, ...]:
        return (history('winner'),)

    async def losing_reader() -> tuple[Artifact, ...]:
        await invoke(case, winner)
        return (history('loser'),)

    with pytest.raises(RunConflict):
        await invoke(case, losing_reader)

    async def forbidden() -> tuple[Artifact, ...]:
        pytest.fail('winning snapshot cannot be recaptured')

    restored = await invoke(case, forbidden)
    assert restored.artifacts[0].content['text'] == 'winner'


async def test_failed_read_leaves_no_reserved_state_and_retry_can_capture_empty_history(case: Case) -> None:
    async def failed() -> tuple[Artifact, ...]:
        raise ValueError('PRIVATE_INPUT_CONTENT')

    with pytest.raises(RunConflict) as failure:
        await invoke(case, failed)
    assert failure.value.__context__ is None and 'PRIVATE_INPUT_CONTENT' not in str(failure.value)
    assert STATE not in mutable(case[0].row.routing_decision)

    async def empty() -> tuple[Artifact, ...]:
        return ()

    result = await invoke(case, empty)
    assert result.artifacts == () and result.snapshot is not None
