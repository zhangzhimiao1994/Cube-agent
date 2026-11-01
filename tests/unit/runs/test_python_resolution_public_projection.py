"""Ordinary approval metadata retains exact resolution scope for native SQL UUIDs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.api.routers.runs import RunCapabilityApprovalsResponse
from agent_hub.capabilities.python_metadata_preparation import metadata_scope
from agent_hub.capabilities.python_resolution_authorization import (
    resolution_binding_matches,
    resolution_scope_resource,
)
from agent_hub.db.models import RunApprovalRow, RunRow
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.repository import RunConflict, RunRepository, _public_plugin_approval_events
from tests.unit.capabilities.capability_lifecycle_typed_fixture import native_asyncpg_uuid_class


@dataclass
class Pending:
    run: RunRow
    approval: RunApprovalRow
    scope: str
    resource: str


def pending(*, native: bool = True, mode: TaskMode = TaskMode.DIRECT) -> Pending:
    uuid_type = native_asyncpg_uuid_class() if native else UUID
    tenant, run_id, user = (uuid_type(str(uuid4())) for _ in range(3))
    now = datetime.now(UTC)
    resource = 'workspace/current/project-resolution/python/' + 'a' * 64
    approval_id = 'approval_resolution_public'
    scope = (f'capability-scope:v1:tenant={tenant}:user={user}:run={run_id}:'
             f'capability=command:operation=run:resource={resource}:agent=engineer')
    run = RunRow(id=run_id, tenant_id=tenant, actor_id=user, actor_role='operator',
        request='PRIVATE_USER_REQUEST', mode=mode.value, status='waiting_approval', version=4,
        created_at=now, routing_decision={
            'generated_project_contract_version': 1, 'python_dependency_preflight_version': 1,
            'python_dependency_preflight_submission_version': 1,
            'project_id': 'project', 'workspace_session_id': 'main',
            'sandbox_profile': 'workspace_write', 'execution_backend': 'systemd',
            'requested_permissions': ['workspace.read', 'workspace.write', 'command.run'],
            'runtime_timeout_seconds': 3600, 'approval_kind': 'capability_tool',
            'approval_id': approval_id, 'approval_scope': scope, 'approval_fingerprint': 'b' * 64,
        })
    bound = metadata_scope(RunRepository._record(run), actor='engineer')
    approval = RunApprovalRow(id=uuid4(), tenant_id=tenant, run_id=run_id,
        approval_id=approval_id, action='capability_tool', status='pending', created_at=now,
        payload={'approval_scope': scope, 'approval_fingerprint': 'b' * 64,
            'python_resolution_binding': {'schema_version': 1,
                'scope': bound.model_dump(mode='json'), 'resource': resource},
            'public_request': {'kind': 'approval.requested', 'sequence': 4,
                'run_id': str(run_id), 'approval_id': approval_id,
                'payload': {'run_id': str(run_id), 'version': 4},
                'arguments': 'PRIVATE_ENVELOPE_ARGUMENTS'},
            'arguments': {'requirements': ['PRIVATE_DEPENDENCY'], 'command': 'PRIVATE_COMMAND'},
            'source': 'PRIVATE_SOURCE', 'credential': 'PRIVATE_SECRET'})
    return Pending(run, approval, scope, resource)


def object_map(value: object) -> dict[str, object]:
    assert isinstance(value, dict) and all(isinstance(key, str) for key in value)
    return value


def expected_payload(case: Pending) -> dict[str, object]:
    return {'run_id': str(case.run.id), 'version': 4, 'capability': 'command',
            'operation': 'run', 'resource': case.resource, 'approval_scope': case.scope}


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
def test_exact_resolution_scope_is_public_for_real_driver_uuids(native: bool, mode: TaskMode) -> None:
    case = pending(native=native, mode=mode)
    raw_user = case.run.actor_id
    assert raw_user is not None
    record = RunRepository._record(case.run)
    assert type(record.actor_id) is UUID
    assert resolution_binding_matches(record, case.approval.payload['python_resolution_binding'], case.scope)
    if native:
        assert type(raw_user) is native_asyncpg_uuid_class() and type(raw_user) is not UUID
        # Demonstrate the raw SQL/strict runtime boundary, without weakening it.
        assert resolution_scope_resource(case.scope, tenant_id=case.run.tenant_id,
            user_id=raw_user, run_id=case.run.id) is None
    before = deepcopy((case.approval.payload, case.run.routing_decision, case.run.version))
    events = _public_plugin_approval_events([case.approval], case.run)
    assert len(events) == 1 and events[0]['payload'] == expected_payload(case)
    assert set(events[0]) == {'kind', 'sequence', 'run_id', 'approval_id', 'actor',
                              'action', 'reason', 'payload', 'created_at'}
    assert 'PRIVATE_' not in str(events)
    assert before == (case.approval.payload, case.run.routing_decision, case.run.version)


async def test_ordinary_dao_and_api_dto_keep_only_the_specific_public_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = pending()

    class Rows:
        def all(self) -> list[RunApprovalRow]:
            return [case.approval]

    monkeypatch.setattr(AsyncSession, 'scalar', AsyncMock(return_value=case.run))
    monkeypatch.setattr(AsyncSession, 'scalars', AsyncMock(return_value=Rows()))
    repository = RunRepository(async_sessionmaker(expire_on_commit=False))
    events = await repository.capability_approval_events(case.run.tenant_id, case.run.id,
                                                       actor_id=case.run.actor_id)
    assert events[0]['payload'] == expected_payload(case)
    # Presentation metadata is not evidence that an approval is ready or granted.
    response = RunCapabilityApprovalsResponse(items=events, ready=False).model_dump(mode='json')
    items = response['items']
    assert isinstance(items, list) and len(items) == 1
    assert object_map(items[0])['payload'] == expected_payload(case)
    assert response['ready'] is False and 'PRIVATE_' not in str(response)
    assert case.approval.status == 'pending' and case.run.version == 4


@pytest.mark.parametrize('defect', ['missing_binding', 'schema_bool', 'binding_extra',
    'actor', 'user', 'tenant', 'run', 'project', 'session', 'request', 'permissions',
    'resource', 'scope_agent', 'scope_suffix', 'route_session', 'route_permissions', 'run_actor'])
def test_unproven_resolution_binding_keeps_the_existing_minimal_view(defect: str) -> None:
    case = pending()
    payload = case.approval.payload
    binding = object_map(payload['python_resolution_binding'])
    scope = object_map(binding['scope'])
    if defect == 'missing_binding':
        payload.pop('python_resolution_binding')
    elif defect == 'schema_bool':
        binding['schema_version'] = True
    elif defect == 'binding_extra':
        binding['arguments'] = 'PRIVATE_SOURCE'
    elif defect in {'actor', 'user', 'tenant', 'run', 'project', 'session', 'request', 'permissions'}:
        key = {'actor': 'actor', 'user': 'user_id', 'tenant': 'tenant_id', 'run': 'run_id',
               'project': 'project_id', 'session': 'session_id', 'request': 'request_sha256',
               'permissions': 'permissions_sha256'}[defect]
        scope[key] = 'other_actor' if defect == 'actor' else ('e' * 64 if defect in {'request', 'permissions'} else str(uuid4()))
    elif defect == 'resource':
        binding['resource'] = 'workspace/current/project-resolution/python/' + 'e' * 64
    elif defect.startswith('scope_'):
        changed = case.scope.replace(':agent=engineer', ':agent=other_actor') if defect == 'scope_agent' else case.scope + ':grant=all'
        payload['approval_scope'] = changed
        assert case.run.routing_decision is not None
        case.run.routing_decision['approval_scope'] = changed
    elif defect == 'run_actor':
        case.run.actor_id = uuid4()
    else:
        assert case.run.routing_decision is not None
        case.run.routing_decision['workspace_session_id' if defect == 'route_session' else 'requested_permissions'] = (
            'other_session' if defect == 'route_session' else ['workspace.read'])
    before = deepcopy((payload, case.run.routing_decision))
    events = _public_plugin_approval_events([case.approval], case.run)
    assert events[0]['payload'] == {'run_id': str(case.run.id), 'version': 4}
    assert 'PRIVATE_' not in str(events)
    assert before == (payload, case.run.routing_decision)


@pytest.mark.parametrize('defect', ['tenant', 'run', 'version', 'version_bool', 'fingerprint',
                                  'approval_id', 'run_status', 'action', 'envelope_action'])
def test_pending_identity_and_version_guards_still_fail_closed(defect: str) -> None:
    case = pending()
    assert case.run.routing_decision is not None
    envelope = object_map(case.approval.payload['public_request'])
    if defect in {'tenant', 'run'}:
        setattr(case.approval, 'tenant_id' if defect == 'tenant' else 'run_id', uuid4())
    elif defect == 'version':
        case.run.version += 1
    elif defect == 'version_bool':
        object_map(envelope['payload'])['version'] = True
    elif defect == 'fingerprint':
        case.run.routing_decision['approval_fingerprint'] = 'e' * 64
    elif defect == 'approval_id':
        case.run.routing_decision['approval_id'] = 'other_approval'
    elif defect == 'run_status':
        case.run.status = 'running'
    elif defect == 'action':
        case.approval.action = 'unrelated_action'
    else:
        envelope['kind'] = 'approval.resolved'
    before = deepcopy((case.approval.payload, case.run.routing_decision, case.run.version))
    with pytest.raises(RunConflict, match='capability approval evidence|capability approval identity|pending capability approval evidence'):
        _public_plugin_approval_events([case.approval], case.run)
    assert before == (case.approval.payload, case.run.routing_decision, case.run.version)
