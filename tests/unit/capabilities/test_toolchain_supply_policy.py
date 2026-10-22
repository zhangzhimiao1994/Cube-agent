"""SDK default policy uses persisted captures and the existing repository approval binding."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.toolchain_preparation import toolchain_supply_request
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority
from tests.unit.capabilities.test_toolchain_preparation import case as case  # noqa: PLC0414
from tests.unit.capabilities.test_toolchain_preparation import state_for
from tests.unit.runs.test_toolchain_authorization_fence import ApprovalDB


class PolicyDB(ApprovalDB):
    @property
    def run(self) -> Any:
        return self.row


@pytest.fixture
def dao(case: Any) -> PolicyDB:
    return PolicyDB(case)


class Repository:
    """Only the live read boundary is substituted; approval DAO methods stay real."""

    def __init__(self, dao: Any) -> None:
        self.dao = dao
        self.approval_checks: list[str] = []
        self.begins = 0
        self.authority = ToolchainSupplyAuthority.from_context(dao.context(), worker_id=dao.row.worker_id)

    async def get(self, tenant_id: UUID, run_id: UUID) -> object:
        assert (tenant_id, run_id) == (self.dao.run.tenant_id, self.dao.run.id)
        return RunRepository._record(self.dao.run)

    async def is_capability_approval_approved(self, tenant: UUID, run: UUID, fingerprint: str) -> bool:
        self.approval_checks.append('fingerprint')
        return bool(await self.dao.repository.is_capability_approval_approved(tenant, run, fingerprint))

    async def is_capability_approval_scope_approved(self, tenant: UUID, run: UUID, scope: str) -> bool:
        self.approval_checks.append('scope')
        return bool(await self.dao.repository.is_capability_approval_scope_approved(tenant, run, scope))

    async def begin_capability_approval(self, *args: Any, **kwargs: Any) -> object:
        self.begins += 1
        return await self.dao.repository.begin_capability_approval(*args, **kwargs)


def prepare(case: Any, dao: Any) -> CapabilityRequest:
    row = dao.run
    row.tenant_id, row.id, row.actor_id = UUID(int=1), UUID(int=3), UUID(int=2)
    row.actor_role = 'operator'
    row.routing_decision = deepcopy(case.row.routing_decision)
    row.routing_decision['toolchain_supply'] = state_for(case)
    row.routing_decision['requested_permissions'].append('network.read')
    row.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    return toolchain_supply_request(case.plan, original_reference=case.ref,
        user_id=UUID(int=2), idempotency_key='policy-sdk')


def policy_for(repository: object) -> DefaultRuntimeCapabilityPolicyGateway:
    return DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)


async def grant(case: Any, dao: Any) -> tuple[CapabilityRequest, Repository]:
    request = prepare(case, dao)
    repo = Repository(dao)
    result = await policy_for(repo).authorize_project_toolchain(request, role=Role.OPERATOR, authority=repo.authority)
    assert result.status is CapabilityStatus.WAITING_APPROVAL
    approval = dao.approvals[0]
    approval.status = 'approved'
    # Model a normal worker reacquisition after the durable approval was resolved.
    dao.run.status = 'running'
    dao.run.worker_id = 'resumed-worker'
    dao.run.worker_lease_token = uuid4()
    dao.run.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    repo.authority = ToolchainSupplyAuthority.from_context(dao.context(), worker_id=dao.row.worker_id)
    return request, repo


async def test_network_permission_still_requires_exact_durable_sdk_approval(case: Any, dao: Any) -> None:
    request = prepare(case, dao)
    repo = Repository(dao)
    result = await policy_for(repo).authorize_project_toolchain(request, role=Role.OPERATOR, authority=repo.authority)
    assert result.status is CapabilityStatus.WAITING_APPROVAL and result.approval_id is not None
    assert repo.begins == 1 and dao.run.status == 'waiting_approval'
    assert dao.run.routing_decision['approval_checkpoint_required'] is False
    approval = dao.approvals[0]
    assert approval.payload['toolchain_input_binding'] == request.arguments
    assert approval.payload['approval_scope'] == capability_approval_scope(request)
    assert approval.payload['approval_fingerprint'] == fingerprint_capability_request(request)
    assert dao.run.worker_lease_token is None


@pytest.mark.parametrize('retry', [False, True])
async def test_real_repository_reuses_only_exact_sdk_binding(case: Any, dao: Any, retry: bool) -> None:
    request, repo = await grant(case, dao)
    if retry:
        request = request.model_copy(update={'idempotency_key': 'sdk-resumed'})
    repo.approval_checks.clear()
    result = await policy_for(repo).authorize_project_toolchain(request, role=Role.OPERATOR, authority=repo.authority)
    assert result.status is CapabilityStatus.ALLOWED
    assert repo.approval_checks == ['scope']
    assert repo.begins == 1


@pytest.mark.parametrize('field,value', [
    ('schema_version', True), ('plan_sha256', '0' * 64), ('catalog_sha256', '0' * 64),
    ('declaration_sha256', '0' * 64), ('artifact_sha256', '0' * 64), ('archive_sha256', '0' * 64),
    ('source_sha256', '0' * 64), ('contract_sha256', '0' * 64), ('project_id', 'other'),
    ('workspace_session_id', 'other'), ('actor_id', 'implementer'), ('actor_role', 'admin'),
    ('approved', True), ('install_allowed', True), ('recipe', 'go-test'),
])
async def test_same_approved_scope_cannot_authorize_changed_arguments(
    case: Any, dao: Any, field: str, value: object,
) -> None:
    request, repo = await grant(case, dao)
    changed = request.model_copy(update={'arguments': {**request.arguments, field: value}})
    assert capability_approval_scope(changed) == capability_approval_scope(request)
    repo.approval_checks.clear()
    result = await policy_for(repo).authorize_project_toolchain(changed, role=Role.OPERATOR, authority=repo.authority)
    assert result.status is CapabilityStatus.DENIED and not repo.approval_checks
    assert repo.begins == 1


@pytest.mark.parametrize('defect', [
    'tenant', 'run', 'user', 'agent', 'role', 'capability', 'operation', 'resource',
    'normalized_resource', 'reference', 'reference_bool', 'missing_argument',
])
async def test_request_identity_and_capture_are_exact_even_with_prior_grant(
    case: Any, dao: Any, defect: str,
) -> None:
    request, repo = await grant(case, dao)
    patch: dict[str, Any] = {}
    role = Role.ADMIN if defect == 'role' else Role.OPERATOR
    if defect in {'tenant', 'run', 'user'}:
        patch[defect + '_id'] = uuid4()
    elif defect == 'agent':
        patch['agent_id'] = 'implementer'
    elif defect in {'capability', 'operation'}:
        patch[defect] = 'file' if defect == 'capability' else 'read'
    elif defect == 'resource':
        patch['resource'] = request.resource + '/nested'
    elif defect == 'normalized_resource':
        patch['resource'] = request.resource.replace('/toolchain/', '/toolchain/./')
    else:
        arguments = deepcopy(request.arguments)
        if defect == 'missing_argument':
            del arguments['archive_sha256']
        else:
            reference = dict(case.ref.to_payload())
            reference['file_count' if defect == 'reference' else 'schema_version'] = (
                99 if defect == 'reference' else True)
            arguments['original_reference'] = reference
        patch['arguments'] = arguments
    repo.approval_checks.clear()
    result = await policy_for(repo).authorize_project_toolchain(request.model_copy(update=patch),
        role=role, authority=repo.authority)
    assert result.status is CapabilityStatus.DENIED and not repo.approval_checks


@pytest.mark.parametrize('defect', [
    'missing_plan', 'hash', 'approved_bool', 'capture', 'capture_phase', 'capture_schema',
    'capture_extra', 'contract_version', 'permission', 'backend', 'sandbox',
    'python_supply', 'python_recipe', 'generated', 'status', 'worker', 'token', 'expiry',
    'no_expiry', 'naive_expiry', 'row_user', 'row_role', 'executing', 'supplied',
])
async def test_live_db_authority_is_revalidated_before_reusing_approval(
    case: Any, dao: Any, defect: str,
) -> None:
    request, repo = await grant(case, dao)
    row, route = dao.run, dao.run.routing_decision
    if defect == 'missing_plan':
        del route['toolchain_supply']
    elif defect == 'hash':
        route['toolchain_supply']['plan_sha256'] = '0' * 64
    elif defect == 'approved_bool':
        route['toolchain_supply']['approved'] = True
    elif defect.startswith('capture'):
        capture = route['project_input_preparation']
        if defect == 'capture':
            capture['reference']['file_count'] += 1
        else:
            capture[{'capture_phase': 'phase', 'capture_schema': 'schema_version',
                     'capture_extra': 'approved'}[defect]] = {
                'capture_phase': 'observed', 'capture_schema': True, 'capture_extra': True}[defect]
    elif defect == 'contract_version':
        route['project_input_contract_version'] = True
    elif defect == 'permission':
        route['requested_permissions'].remove('command.run')
    elif defect in {'backend', 'sandbox'}:
        route['execution_backend' if defect == 'backend' else 'sandbox_profile'] = 'local'
    elif defect in {'python_supply', 'generated'}:
        route['python_input_supply' if defect == 'python_supply' else 'generated_project_preparation'] = None
    elif defect == 'python_recipe':
        route['project_input_preparation']['python_recipe'] = {'approved': True}
    elif defect in {'executing', 'supplied'}:
        route['toolchain_supply'] = state_for(case, defect)
    else:
        field = {'status': 'status', 'worker': 'worker_id', 'token': 'worker_lease_token',
            'row_user': 'actor_id', 'row_role': 'actor_role'}.get(defect, 'worker_lease_expires_at')
        value = {'status': 'cancelled', 'worker': None, 'token': None, 'row_user': uuid4(),
            'row_role': 'admin', 'expiry': datetime.now(UTC) - timedelta(seconds=1),
            'no_expiry': None, 'naive_expiry': datetime.now(UTC).replace(tzinfo=None)}.get(defect)
        setattr(row, field, value)
    repo.approval_checks.clear()
    result = await policy_for(repo).authorize_project_toolchain(request, role=Role.OPERATOR, authority=repo.authority)
    assert result.status is CapabilityStatus.DENIED and not repo.approval_checks


async def test_boolean_only_approval_is_rejected_by_repository_binding(case: Any, dao: Any) -> None:
    request, repo = await grant(case, dao)
    dao.approvals[0].payload.pop('toolchain_input_binding')
    dao.approvals[0].payload['approved'] = True
    assert not await repo.is_capability_approval_approved(
        request.tenant_id, request.run_id, fingerprint_capability_request(request))
    scope = capability_approval_scope(request)
    assert scope is not None
    assert not await repo.is_capability_approval_scope_approved(request.tenant_id, request.run_id, scope)
    # The shared DAO write double supports one approval row. Remove the rejected
    # row only after both real repository lookups have rejected its boolean grant.
    dao.approvals.clear()
    result = await policy_for(repo).authorize_project_toolchain(request, role=Role.OPERATOR, authority=repo.authority)
    assert result.status is CapabilityStatus.WAITING_APPROVAL
    assert repo.begins == 2


@pytest.mark.parametrize('resource', [
    'workspace/current/project-inputs/python/' + 'a' * 64,
    'workspace/current/project-verification/go/' + 'a' * 64,
    'workspace/current/project-verification/toolchain/' + 'a' * 64,
])
async def test_sdk_approval_never_grants_python_or_go_recipe_execution(
    case: Any, dao: Any, resource: str,
) -> None:
    request, repo = await grant(case, dao)
    result = await policy_for(repo).authorize_project_toolchain(request.model_copy(update={'resource': resource}),
        role=Role.OPERATOR, authority=repo.authority)
    assert result.status is CapabilityStatus.DENIED
    assert repo.begins == 1


@pytest.mark.parametrize('failure', [object(), None])
async def test_missing_repository_authority_fails_closed(case: Any, dao: Any, failure: object) -> None:
    request = prepare(case, dao)
    result = await policy_for(failure).authorize_project_toolchain(request, role=Role.OPERATOR,
        authority=ToolchainSupplyAuthority.from_context(dao.context(), worker_id=dao.row.worker_id))
    assert result.status is CapabilityStatus.DENIED
