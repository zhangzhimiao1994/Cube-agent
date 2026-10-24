from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from agent_hub.capabilities.approvals import PluginApprovalIdentity
from agent_hub.db.models import RunApprovalRow, RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import (
    RunConflict,
    RunNotFound,
    RunRepository,
    _pre_model_original_capture,
    _public_plugin_approval_events,
)


def route() -> dict[str, Any]:
    return {
        'project_id': 'python-original', 'workspace_session_id': 'session-original',
        'sandbox_profile': 'workspace_write', 'execution_backend': 'systemd',
        'requested_permissions': ['workspace.read', 'workspace.write', 'command.run'],
        'project_input_contract_version': 1,
        'project_input': {'schema_version': 1, 'kind': 'managed_workspace',
                          'editable_paths': ['src/release_gate/__init__.py']},
        'project_input_preparation': {
            'schema_version': 2, 'phase': 'captured',
            'reference': {'schema_version': 1, 'contract_sha256': 'a' * 64,
                          'input_source_sha256': 'b' * 64, 'file_count': 6},
            'python_recipe': {'schema_version': 1, 'recipe_sha256': 'c' * 64,
                              'environment_input_sha256': 'd' * 64},
        },
    }


def arguments() -> dict[str, object]:
    return {'schema_version': 1, 'recipe_sha256': 'c' * 64,
            'environment_input_sha256': 'd' * 64,
            'project_id': 'python-original', 'workspace_session_id': 'session-original'}


def scope(run: RunRow) -> str:
    return (f'capability-scope:v1:tenant={run.tenant_id}:user={run.actor_id}:run={run.id}:'
            'capability=command:operation=run:resource=workspace/current/project-verification/python/'
            + 'c' * 64)


@pytest.fixture
def dao() -> Any:
    """Real DAO entry points/SQL recovery query; in-memory PostgreSQL-write boundary."""
    run = RunRow(id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role='operator',
        request='repair original Python project', mode=TaskMode.DISPATCH.value,
        status=RunStatus.RUNNING.value, version=4, created_at=datetime.now(UTC),
        routing_decision=route(), worker_id='worker-original', worker_lease_token=uuid4(),
        worker_lease_expires_at=datetime.now(UTC), worker_heartbeat_at=datetime.now(UTC))
    engine = create_engine('sqlite:///:memory:')
    connection = engine.connect()
    connection.execute(text('CREATE TABLE agent_hub_run_checkpoints ('
        'run_id CHAR(32), sequence INTEGER, id CHAR(32), tenant_id CHAR(32), '
        'runtime_type TEXT, runtime_version TEXT, mode TEXT, state TEXT, '
        'state_sha256 TEXT, payload TEXT, created_at TEXT)'))
    connection.execute(text('CREATE TABLE agent_hub_run_events '
                            '(run_id CHAR(32), sequence INTEGER, kind TEXT)'))
    connection.execute(text('INSERT INTO agent_hub_run_events VALUES (:run,1,\'context.loaded\')'),
                       {'run': run.id.hex})
    approvals: list[RunApprovalRow] = []
    outbox: list[object] = []

    class Session:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        def begin(self) -> Any:
            return self

        async def scalar(self, statement: Any) -> Any:
            tables = {item.name for item in statement.get_final_froms()}
            return run if tables == {'agent_hub_runs'} else connection.scalar(statement)

        async def scalars(self, statement: Any) -> Any:
            assert {item.name for item in statement.get_final_froms()} == {'agent_hub_run_approvals'}
            params = statement.compile().params
            assert run.tenant_id in params.values() and run.id in params.values()
            return SimpleNamespace(all=lambda: approvals)

        async def execute(self, statement: Any) -> Any:
            params = statement.compile().params
            if statement.is_insert:
                assert not approvals
                approvals.append(RunApprovalRow(**{key: params[key] for key in (
                    'id', 'tenant_id', 'run_id', 'approval_id', 'action', 'status', 'payload')},
                    created_at=datetime.now(UTC)))
            else:
                assert statement.is_update and len(approvals) == 1
                resolution = next(value for value in params.values()
                    if isinstance(value, dict) and 'public_resolution' in value)
                approvals[0].status = resolution['status']
                approvals[0].payload = {**approvals[0].payload, **resolution}
            return SimpleNamespace(scalar_one_or_none=lambda: approvals[0].id)

        async def flush(self) -> None:
            pass

        def add(self, item: object) -> None:
            outbox.append(item)

    repository = RunRepository(cast(Any, lambda: Session()))
    yield SimpleNamespace(repository=repository, run=run, approvals=approvals,
                          outbox=outbox, connection=connection)
    connection.close()
    engine.dispose()


async def begin(dao: Any, *, plugin: bool = False) -> Any:
    run = dao.run
    return await dao.repository.begin_capability_approval(run.tenant_id, run.id,
        actor_id=run.actor_id, approval_id='approval_python_original',
        approval_fingerprint='PRIVATE-FINGERPRINT', approval_scope=scope(run),
        plugin_identity=PluginApprovalIdentity('calendar', 'calendar.create_event') if plugin else None)


async def test_python_captured_dao_workflow_matches_normal_approval_api(dao: Any) -> None:
    from agent_hub.api.routers.runs import (
        CapabilityApprovalRequest,
        RunCapabilityApprovalsResponse,
        RunSummaryResponse,
    )
    from agent_hub.runs.service import RunSummary

    record = await begin(dao)
    assert record.status is RunStatus.WAITING_APPROVAL
    assert record.routing_decision['approval_checkpoint_required'] is False
    assert all(getattr(dao.run, key) is None for key in (
        'worker_id', 'worker_lease_token', 'worker_lease_expires_at', 'worker_heartbeat_at'))
    events = await dao.repository.capability_approval_events(
        dao.run.tenant_id, dao.run.id, actor_id=dao.run.actor_id)
    expected_scope = scope(dao.run)
    assert events[0]['payload'] == {'run_id': str(dao.run.id), 'version': record.version,
        'capability': 'command', 'operation': 'run',
        'resource': 'workspace/current/project-verification/python/' + 'c' * 64,
        'approval_scope': expected_scope}
    assert dao.approvals[0].payload['python_binding'] == arguments()
    assert 'arguments' not in events[0]
    assert events[0]['actor'] == 'capability_gateway'
    summary = RunSummaryResponse.from_summary(RunSummary(
        id=dao.run.id, tenant_id=dao.run.tenant_id, status=RunStatus.WAITING_APPROVAL,
        mode=TaskMode.DISPATCH, version=record.version, request='repair',
        completed_step_ids=(), artifact_ids=(), usage_cost_usd=Decimal(0),
        approval_id='approval_python_original', approval_kind='capability_tool'))
    assert summary.model_dump(mode='json')['status'] == 'waiting_approval'
    ready = await dao.repository.capability_approval_ready(
        dao.run.tenant_id, dao.run.id, actor_id=dao.run.actor_id)
    metadata = RunCapabilityApprovalsResponse(items=events, ready=ready).model_dump(mode='json')
    assert metadata['ready'] is True and 'arguments' not in metadata['items'][0]
    approval_request = CapabilityApprovalRequest.model_validate({
        'approval_id': summary.approval_id, 'version': summary.version})
    result = await dao.repository.approve_capability_and_enqueue(
        tenant_id=dao.run.tenant_id, run_id=dao.run.id, actor_id=dao.run.actor_id,
        **approval_request.model_dump())
    assert result.status is RunStatus.QUEUED and len(dao.outbox) == 1
    history = await dao.repository.capability_approval_events(
        dao.run.tenant_id, dao.run.id, actor_id=dao.run.actor_id)
    assert history[0] == events[0]
    assert history[1]['decision'] == 'approved'
    assert history[1]['payload'] == {'decision_scope': 'once'}
    assert not await dao.repository.capability_approval_ready(
        dao.run.tenant_id, dao.run.id, actor_id=dao.run.actor_id)


@pytest.mark.parametrize('status', ['pending', 'approved', 'rejected'])
async def test_python_projection_keeps_recipe_binding_private_and_public_shape_unchanged(dao: Any, status: str) -> None:
    await begin(dao)
    approval = dao.approvals[0]
    approval.payload.update({'source': 'PRIVATE-SOURCE', 'arguments': {'grant': True, 'path': '/PRIVATE'}})
    if status != 'pending':
        approval.status = status
        approval.payload['public_resolution'] = {'kind': 'approval.resolved', 'sequence': 6,
            'run_id': str(dao.run.id), 'approval_id': approval.approval_id, 'decision': status}
        dao.run.status, dao.run.version = RunStatus.COMPLETED.value, 8
        dao.run.routing_decision = route()
        state = dao.run.routing_decision['project_input_preparation']
        state.update({'phase': 'observed', 'execution_id': 'original_' + dao.run.id.hex,
                      'observation': {'private_log': 'PRIVATE-LOG'}, 'observation_sha256': 'f' * 64})
    before = deepcopy(approval.payload)
    events = _public_plugin_approval_events([approval], dao.run)
    assert approval.payload['python_binding'] == arguments()
    assert set(events[0]) == {'kind', 'sequence', 'run_id', 'approval_id', 'actor',
                              'action', 'reason', 'payload', 'created_at'}
    payload = events[0]['payload']
    assert isinstance(payload, dict)
    assert payload['resource'] == 'workspace/current/project-verification/python/' + 'c' * 64
    assert set(payload) == {'run_id', 'version', 'capability', 'operation', 'resource', 'approval_scope'}
    assert 'PRIVATE' not in str(events) and 'grant' not in str(events)
    assert approval.payload == before


@pytest.mark.parametrize('defect', [
    'executing', 'observed', 'unknown', 'version_bool', 'marker_version_bool',
    'marker_missing', 'marker_extra', 'marker_recipe', 'marker_environment',
    'reference', 'selection', 'extra_execution', 'phase_list', 'phase_missing',
])
async def test_only_immutable_python_capture_has_no_checkpoint_exemption(dao: Any, defect: str) -> None:
    preparation = dao.run.routing_decision['project_input_preparation']
    if defect in {'executing', 'observed', 'unknown'}:
        preparation['phase'] = defect
    elif defect == 'version_bool':
        preparation['schema_version'] = True
    elif defect == 'marker_version_bool':
        preparation['python_recipe']['schema_version'] = True
    elif defect == 'marker_missing':
        preparation.pop('python_recipe')
    elif defect == 'marker_extra':
        preparation['python_recipe']['grant'] = True
    elif defect.startswith('marker_'):
        preparation['python_recipe'][{'marker_recipe': 'recipe_sha256',
                                     'marker_environment': 'environment_input_sha256'}[defect]] = 'INVALID'
    elif defect == 'reference':
        preparation['reference']['file_count'] = True
    elif defect == 'selection':
        dao.run.routing_decision['project_input']['editable_paths'] = ['../private']
    elif defect == 'phase_list':
        preparation['phase'] = ['captured']
    elif defect == 'phase_missing':
        preparation.pop('phase')
    else:
        preparation['execution_id'] = 'already-consumed'
    assert not _pre_model_original_capture(dao.run.routing_decision)
    record = await begin(dao)
    assert record.routing_decision['approval_checkpoint_required'] is True
    assert not await dao.repository.capability_approval_ready(
        dao.run.tenant_id, dao.run.id, actor_id=dao.run.actor_id)


@pytest.mark.parametrize('extra', ['model.started', 'tool.started', 'tool.completed',
                                 'artifact.created', 'context.injected', 'runtime.failed'])
async def test_python_capture_cannot_ignore_actual_runtime_effects(dao: Any, extra: str) -> None:
    record = await begin(dao)
    dao.connection.execute(text('INSERT INTO agent_hub_run_events VALUES (:run,2,:kind)'),
                           {'run': dao.run.id.hex, 'kind': extra})
    before = deepcopy((dao.run.status, dao.run.version, dao.run.routing_decision))
    assert not await dao.repository.capability_approval_ready(
        dao.run.tenant_id, dao.run.id, actor_id=dao.run.actor_id)
    with pytest.raises(RunConflict):
        await dao.repository.approve_capability_and_enqueue(
            tenant_id=dao.run.tenant_id, run_id=dao.run.id, actor_id=dao.run.actor_id,
            approval_id='approval_python_original', version=record.version)
    assert (dao.run.status, dao.run.version, dao.run.routing_decision) == before
    assert not dao.outbox


@pytest.mark.parametrize('defect', ['missing_binding', 'binding_extra', 'project', 'session',
    'recipe', 'environment', 'reference', 'selection', 'actor', 'tenant_scope', 'run_scope',
    'scope_recipe', 'scope_suffix', 'scope_generic', 'backend'])
async def test_python_projection_refuses_unproven_binding_and_never_leaks_private_data(
    dao: Any, defect: str,
) -> None:
    await begin(dao)
    approval = dao.approvals[0]
    approval.payload.update({'source': 'PRIVATE-SOURCE', 'logs': 'PRIVATE-LOG',
                             'arguments': {'host_path': '/PRIVATE-PATH', 'grant': True}})
    state = dao.run.routing_decision['project_input_preparation']
    if defect == 'missing_binding':
        approval.payload.pop('python_binding', None)
    elif defect == 'binding_extra':
        approval.payload['python_binding']['raw_source'] = 'PRIVATE-SOURCE'
    elif defect in {'project', 'session'}:
        dao.run.routing_decision[{'project': 'project_id', 'session': 'workspace_session_id'}[defect]] = 'other-valid-id'
    elif defect in {'recipe', 'environment'}:
        state['python_recipe'][{'recipe': 'recipe_sha256', 'environment': 'environment_input_sha256'}[defect]] = 'e' * 64
    elif defect == 'reference':
        state['reference']['file_count'] = True
    elif defect == 'selection':
        dao.run.routing_decision['project_input'] = {'editable_paths': ['../secret']}
    elif defect == 'actor':
        dao.run.actor_id = uuid4()
    elif defect == 'backend':
        dao.run.routing_decision['execution_backend'] = 'host'
    else:
        old = approval.payload['approval_scope']
        if defect in {'tenant_scope', 'run_scope'}:
            key = defect.removesuffix('_scope')
            old = old.replace(f'{key}={getattr(dao.run, "tenant_id" if key == "tenant" else "id")}',
                              f'{key}={uuid4()}')
        elif defect == 'scope_recipe':
            old = old.replace('c' * 64, 'f' * 64)
        elif defect == 'scope_suffix':
            old += ':grant=true'
        else:
            old = old.replace('/python/' + 'c' * 64, '')
        approval.payload['approval_scope'] = old
        dao.run.routing_decision['approval_scope'] = old
    original = deepcopy(approval.payload)
    events = _public_plugin_approval_events([approval], dao.run)
    assert events[0]['payload'] == {'run_id': str(dao.run.id), 'version': dao.run.version}
    assert 'arguments' not in events[0] and 'PRIVATE' not in str(events)
    assert approval.payload == original


async def test_python_capture_does_not_exempt_plugin_and_dao_keeps_actor_boundary(dao: Any) -> None:
    record = await begin(dao, plugin=True)
    assert record.routing_decision['approval_checkpoint_required'] is True
    assert 'python_binding' not in dao.approvals[0].payload
    events = await dao.repository.capability_approval_events(
        dao.run.tenant_id, dao.run.id, actor_id=dao.run.actor_id)
    assert events[0]['payload']['plugin_id'] == 'calendar' and 'arguments' not in events[0]
    with pytest.raises(RunNotFound):
        await dao.repository.capability_approval_events(dao.run.tenant_id, dao.run.id, actor_id=uuid4())
