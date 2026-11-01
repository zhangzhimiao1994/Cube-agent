"""Same-run supply admission uses fake Root IO, never native download/install."""

from __future__ import annotations

import importlib
import importlib.util
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
)
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.domain.runs import RunStatus
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyPlan,
    PythonInputSupplyReceipt,
)
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectProbeInvocation
from tests.unit.capabilities.test_python_project_input_store import interpreter, selected
from tests.unit.runs.test_python_project_approval_projection import (
    dao as dao,  # noqa: PLC0414 - intentional pytest fixture re-export.
)

PREFIX = 'workspace/current/project-inputs/python/'


def test_new_supply_scope_is_recognized_without_borrowing_verify() -> None:
    from agent_hub.capabilities.types import CapabilityRequest
    request = CapabilityRequest(tenant_id=UUID(int=1), user_id=UUID(int=2), run_id=UUID(int=3),
        agent_id='main_agent', capability='command', operation='run', resource=PREFIX + 'a' * 64,
        arguments={}, idempotency_key='supply')
    assert capability_approval_scope(request) is not None


def test_gateway_exposes_trusted_supply_admission() -> None:
    assert callable(getattr(HarnessToolGateway, 'authorize_project_inputs', None))


@pytest.fixture
def helper() -> Any:
    name = 'agent_hub.capabilities.python_input_preparation'
    assert importlib.util.find_spec(name) is not None, 'missing trusted input-supply preparation helper'
    return importlib.import_module(name)


def plan_for(row: Any) -> PythonInputSupplyPlan:
    state = row.routing_decision['project_input_preparation']
    return PythonInputSupplyPlan.model_validate_json(json.dumps({
        'scope': {'tenant_id': str(row.tenant_id), 'run_id': str(row.id),
            'project_id': row.routing_decision['project_id'],
            'session_id': row.routing_decision['workspace_session_id'],
            'original_reference': state['reference']},
        'declaration_sha256': '3' * 64, 'manifest_sha256': '4' * 64,
        'lock_sha256': '5' * 64, 'catalog_sha256': '6' * 64,
        'interpreter': interpreter().to_payload(),
        'artifacts': [{**wheel.to_payload(), 'size_bytes': 123} for wheel in selected()],
    }), strict=True)


def prepare_route(row: Any, plan: PythonInputSupplyPlan) -> None:
    state = row.routing_decision['project_input_preparation']
    state.pop('python_recipe', None)
    state['schema_version'] = 1
    row.routing_decision['python_input_supply'] = {
        'schema_version': 1, 'phase': 'planned', 'plan': plan.model_dump(mode='json'),
        'plan_sha256': plan.sha256,
    }
    row.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)


def test_supply_scope_is_exact_and_not_verification_scope(helper: Any, dao: Any) -> None:
    plan = plan_for(dao.run)
    req = helper.input_supply_request(plan, user_id=dao.run.actor_id, idempotency_key='supply')
    assert req.arguments['original_reference'] == plan.scope.original_reference.model_dump(mode='json')
    assert req.arguments['catalog_sha256'] == plan.catalog_sha256
    assert req.resource == PREFIX + plan.sha256
    assert capability_approval_scope(req) == (
        f'capability-scope:v1:tenant={dao.run.tenant_id}:user={dao.run.actor_id}:run={dao.run.id}:'
        f'capability=command:operation=run:resource={PREFIX}{plan.sha256}')


@pytest.mark.parametrize('phase', ['planned', 'executing', 'supplied'])
def test_strict_durable_supply_shape(helper: Any, dao: Any, phase: str) -> None:
    plan = plan_for(dao.run)
    prepare_route(dao.run, plan)
    state = dao.run.routing_decision['python_input_supply']
    if phase != 'planned':
        state.update(phase=phase, execution_id='input_supply_' + uuid4().hex)
    if phase == 'supplied':
        state['receipt'] = PythonInputSupplyReceipt(plan=plan, supply_execution_id=state['execution_id'],
            published=tuple(sorted(w.filename for w in plan.artifacts)), reused=()).model_dump(mode='json')
    assert helper.supply_state_plan(state) == plan
    for extra in ('url', 'installer_allowed', 'unknown'):
        with pytest.raises(ValueError, match='input_supply'):
            helper.supply_state_plan({**state, extra: True})
    with pytest.raises(ValueError, match='input_supply'):
        helper.supply_state_plan({**state, 'plan_sha256': '0' * 64})
    with pytest.raises(ValueError, match='input_supply'):
        helper.supply_state_plan({**state, 'schema_version': True})


async def test_trusted_gateway_is_authorization_only_and_not_model_tool(helper: Any, dao: Any) -> None:
    plan = plan_for(dao.run)
    seen: list[Any] = []

    class Policy:
        async def invoke(self, request: Any, *, role: Role) -> Any:
            from agent_hub.capabilities.gateway import CapabilityResult
            seen.append(request)
            assert role is Role.OPERATOR
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)

    class Backend:
        def is_available(self, *_: object) -> bool:
            raise AssertionError('trusted admission must not query/register a model tool')

    gateway = HarnessToolGateway(Backend(), policy_gateway=Policy())  # type: ignore[arg-type]
    request = HarnessToolCallRequest(run_id=dao.run.id, tool_name='project.prepare_inputs',
        actor='main_agent', arguments={}, sandbox='workspace_write', idempotency_key='supply',
        approval_required=True)
    assert hasattr(gateway, 'authorize_project_inputs'), 'missing trusted-only admission method'
    result = await gateway.authorize_project_inputs(dao.run.tenant_id, request,
        user_id=dao.run.actor_id, role=Role.OPERATOR, plan=plan)
    assert result.payload == {'authorized': True} and len(seen) == 1
    assert seen[0] == helper.input_supply_request(plan, user_id=dao.run.actor_id, idempotency_key='supply')
    assert (await gateway.invoke(dao.run.tenant_id, request,
        user_id=dao.run.actor_id, role=Role.OPERATOR)).status == 'failed'
    for update in ({'actor': 'implementer'}, {'arguments': {'plan': plan.sha256}}, {'sandbox': 'restricted'}):
        result = await gateway.authorize_project_inputs(dao.run.tenant_id, replace(request, **update),
            user_id=dao.run.actor_id, role=Role.OPERATOR, plan=plan)
        assert result.status == 'failed'
    assert len(seen) == 1


@pytest.mark.parametrize('defect', ['none', 'actor', 'tenant', 'role', 'permission', 'hash', 'catalog', 'ref',
    'extra_preparation', 'expired_lease'])
async def test_real_default_policy_requires_exact_supply_approval(helper: Any, dao: Any, defect: str) -> None:
    plan = plan_for(dao.run)
    prepare_route(dao.run, plan)
    request = helper.input_supply_request(plan, user_id=dao.run.actor_id, idempotency_key='supply')
    dao.run.routing_decision['requested_permissions'].append('network.read')
    if defect == 'actor':
        request = request.model_copy(update={'user_id': uuid4()})
    elif defect == 'tenant':
        request = request.model_copy(update={'tenant_id': uuid4()})
    elif defect == 'role':
        dao.run.actor_role = Role.VIEWER.value
    elif defect == 'permission':
        dao.run.routing_decision['requested_permissions'].remove('command.run')
    elif defect == 'hash':
        dao.run.routing_decision['python_input_supply']['plan_sha256'] = '0' * 64
    elif defect == 'catalog':
        request = request.model_copy(update={'arguments': {**request.arguments, 'catalog_sha256': '0' * 64}})
    elif defect == 'ref':
        dao.run.routing_decision['project_input_preparation']['reference']['file_count'] += 1
    elif defect == 'extra_preparation':
        dao.run.routing_decision['project_input_preparation']['installer_allowed'] = True
    elif defect == 'expired_lease':
        dao.run.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    class Repo:
        async def get(self, *_: object) -> Any:
            from agent_hub.runs.repository import RunRepository
            return RunRepository._record(dao.run)

        async def is_capability_approval_approved(self, *_: object) -> bool:
            return False

        async def is_capability_approval_scope_approved(self, *_: object) -> bool:
            return False

        async def begin_capability_approval(self, *args: Any, **kwargs: Any) -> Any:
            return await dao.repository.begin_capability_approval(*args, **kwargs)

    store = InMemoryApprovalStore()
    policy = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(store), Repo())
    result = await policy.invoke(request, role=Role.OPERATOR)
    assert result.status is (CapabilityStatus.WAITING_APPROVAL if defect == 'none' else CapabilityStatus.DENIED)
    if defect == 'none':
        events = await dao.repository.capability_approval_events(dao.run.tenant_id,
            dao.run.id, actor_id=dao.run.actor_id)
        assert set(events[0]['payload']) == {
            'run_id', 'version', 'capability', 'operation', 'resource', 'approval_scope'}
        assert events[0]['payload']['resource'] == request.resource
        assert 'arguments' not in str(events) and 'catalog_sha256' not in str(events)


async def test_supply_public_history_survives_recipe_observed(helper: Any, dao: Any) -> None:
    from agent_hub.runs.repository import _public_plugin_approval_events
    plan = plan_for(dao.run)
    prepare_route(dao.run, plan)
    req = helper.input_supply_request(plan, user_id=dao.run.actor_id, idempotency_key='supply')
    result = await dao.repository.begin_capability_approval(dao.run.tenant_id, dao.run.id,
        actor_id=dao.run.actor_id, approval_id='approval_supply', approval_fingerprint='private',
        approval_scope=capability_approval_scope(req))
    assert result.routing_decision['approval_checkpoint_required'] is False
    events = _public_plugin_approval_events(dao.approvals, dao.run)
    payload = events[0]['payload']
    assert isinstance(payload, dict) and payload['resource'] == req.resource
    snapshot = deepcopy(dao.approvals[0].payload)
    dao.approvals[0].status = 'approved'
    dao.approvals[0].payload['public_resolution'] = {'kind': 'approval.resolved', 'sequence': 100,
        'run_id': str(dao.run.id), 'approval_id': 'approval_supply', 'decision': 'approved'}
    state = dao.run.routing_decision['python_input_supply']
    execution = 'input_supply_' + 'a' * 32
    state.update(phase='supplied', execution_id=execution)
    state['receipt'] = PythonInputSupplyReceipt(plan=plan, supply_execution_id=execution,
        published=(), reused=tuple(sorted(w.filename for w in plan.artifacts))).model_dump(mode='json')
    dao.run.status = RunStatus.COMPLETED.value
    dao.run.routing_decision['project_input_preparation'].update(schema_version=2, phase='observed',
        python_recipe={'schema_version': 1, 'recipe_sha256': 'a' * 64, 'environment_input_sha256': 'b' * 64},
        execution_id='original_' + dao.run.id.hex, observation={}, observation_sha256='c' * 64)
    history = _public_plugin_approval_events(dao.approvals, dao.run)
    assert history[0] == events[0]
    history_payload = history[0]['payload']
    assert isinstance(history_payload, dict)
    assert set(history_payload) == {'run_id', 'version', 'capability', 'operation', 'resource', 'approval_scope'}
    assert snapshot['public_request'] == dao.approvals[0].payload['public_request']


async def test_real_postgres_uuid_type_keeps_supply_approval_details(helper: Any, dao: Any) -> None:
    from tests.unit.capabilities.capability_lifecycle_typed_fixture import native_asyncpg_uuid_class

    DatabaseUUID = native_asyncpg_uuid_class()

    from agent_hub.runs.repository import _public_plugin_approval_events

    plan = plan_for(dao.run)
    prepare_route(dao.run, plan)
    req = helper.input_supply_request(plan, user_id=dao.run.actor_id, idempotency_key='postgres-supply')
    for key in ('tenant_id', 'id', 'actor_id'):
        setattr(dao.run, key, DatabaseUUID(str(getattr(dao.run, key))))
    assert isinstance(dao.run.actor_id, UUID) and type(dao.run.actor_id) is not UUID
    await dao.repository.begin_capability_approval(dao.run.tenant_id, dao.run.id,
        actor_id=dao.run.actor_id, approval_id='approval_postgres_supply',
        approval_fingerprint='private', approval_scope=capability_approval_scope(req))
    assert 'python_input_binding' in dao.approvals[0].payload
    events = _public_plugin_approval_events(dao.approvals, dao.run)
    assert events[0]['payload'] == {
        'run_id': str(dao.run.id), 'version': dao.run.version,
        'capability': 'command', 'operation': 'run', 'resource': req.resource,
        'approval_scope': capability_approval_scope(req),
    }


def test_probe_is_no_install_data(helper: Any, dao: Any) -> None:
    plan = plan_for(dao.run)
    probe = PythonProjectProbeInvocation.model_validate({
        **plan.scope.model_dump(), 'execution_id': 'probe', 'install_allowed': False,
        'sandbox_profile': 'workspace_write', 'timeout_seconds': 30}, strict=True)
    plan.require_scope(probe)
    assert probe.install_allowed is False and type(probe.tenant_id) is UUID


async def test_gateway_default_policy_and_dao_require_and_reuse_only_exact_grant(helper: Any, dao: Any) -> None:
    from agent_hub.runs.repository import RunRepository

    plan = plan_for(dao.run)
    prepare_route(dao.run, plan)
    dao.run.routing_decision['requested_permissions'].append('network.read')

    class Repository:
        async def get(self, *_: object) -> Any:
            return RunRepository._record(dao.run)

        async def begin_capability_approval(self, *args: Any, **kwargs: Any) -> Any:
            return await dao.repository.begin_capability_approval(*args, **kwargs)

        async def is_capability_approval_approved(self, *_: object) -> bool:
            return False

        async def is_capability_approval_scope_approved(self, tenant: Any, run: Any, scope: str) -> bool:
            return any(a.tenant_id == tenant and a.run_id == run and a.status == 'approved'
                       and a.payload['approval_scope'] == scope for a in dao.approvals)

    class Backend:
        def is_available(self, *_: object) -> bool:
            raise AssertionError('authorization-only method must not check model tools')

    policy = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), Repository())
    gateway = HarnessToolGateway(Backend(), policy_gateway=policy)  # type: ignore[arg-type]
    request = HarnessToolCallRequest(run_id=dao.run.id, tool_name='project.prepare_inputs',
        actor='main_agent', arguments={}, sandbox='workspace_write', idempotency_key='admission',
        approval_required=True)
    waiting = await gateway.authorize_project_inputs(dao.run.tenant_id, request,
        user_id=dao.run.actor_id, role=Role.OPERATOR, plan=plan)
    assert waiting.status == 'failed' and waiting.failure_reason == 'capability requires approval'
    assert set(waiting.payload) == {'approval_id'}
    events = await dao.repository.capability_approval_events(dao.run.tenant_id, dao.run.id,
                                                            actor_id=dao.run.actor_id)
    assert set(events[0]['payload']) == {'run_id', 'version', 'capability', 'operation', 'resource', 'approval_scope'}
    await dao.repository.approve_capability_and_enqueue(tenant_id=dao.run.tenant_id, run_id=dao.run.id,
        actor_id=dao.run.actor_id, approval_id=waiting.payload['approval_id'], version=dao.run.version)
    dao.run.status = RunStatus.RUNNING.value
    dao.run.worker_id, dao.run.worker_lease_token = 'new-worker', uuid4()
    dao.run.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    admitted = await gateway.authorize_project_inputs(dao.run.tenant_id, request,
        user_id=dao.run.actor_id, role=Role.OPERATOR, plan=plan)
    assert admitted.status == 'succeeded' and admitted.payload == {'authorized': True}
    changed = plan.model_copy(update={'catalog_sha256': '0' * 64})
    denied = await gateway.authorize_project_inputs(dao.run.tenant_id, request,
        user_id=dao.run.actor_id, role=Role.OPERATOR, plan=changed)
    assert denied.status == 'failed'
