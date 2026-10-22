"""SDK approvals exercise the existing durable repository special cases."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from agent_hub.auth.models import Role
from agent_hub.db.models import UserRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, _public_plugin_approval_events
from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority
from agent_hub.runtime.contracts import TaskContext
from tests.unit.capabilities.test_toolchain_preparation import (
    KEY,
    PREFIX,
    free_changed,
    state_for,
)
from tests.unit.capabilities.test_toolchain_preparation import (
    case as case,  # noqa: PLC0414
)
from tests.unit.runs.test_python_project_approval_projection import dao as dao  # noqa: PLC0414


@pytest.fixture
def sdk(dao: Any, case: Any) -> Any:
    scope = case.plan.scope.model_copy(update={'tenant_id': dao.run.tenant_id,
        'run_id': dao.run.id, 'user_id': dao.run.actor_id})
    case.plan = case.plan.model_copy(update={'scope': scope})
    dao.run.routing_decision = deepcopy(case.row.routing_decision)
    dao.run.routing_decision[KEY] = state_for(case)
    dao.run.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    dao.plan = case.plan
    row = dao.run
    dao.authority = ToolchainSupplyAuthority.from_context(TaskContext(tenant_id=row.tenant_id,
        run_id=row.id, actor_id=row.actor_id, actor_role=Role(row.actor_role),
        execution_token=row.worker_lease_token, request=row.request, mode=TaskMode(row.mode),
        routing_decision=row.routing_decision), worker_id=row.worker_id)
    original_factory = dao.repository._session_factory

    class Session:
        def __init__(self) -> None:
            self.base = original_factory()

        def __getattr__(self, name: str) -> Any:
            return getattr(self.base, name)

        async def __aenter__(self) -> Any:
            await self.base.__aenter__()
            return self

        async def __aexit__(self, *args: object) -> None:
            await self.base.__aexit__(*args)

        async def scalar(self, statement: Any) -> Any:
            if {item.name for item in statement.get_final_froms()} == {'agent_hub_users'}:
                assert statement._for_update_arg is not None
                return UserRow(id=row.actor_id, tenant_id=row.tenant_id, username='operator',
                               role=row.actor_role, disabled=False)
            return await self.base.scalar(statement)

    dao.repository._session_factory = cast(Any, Session)
    return dao


def approval_scope(sdk: Any, prefix: str = PREFIX) -> str:
    row = sdk.run
    return (f'capability-scope:v1:tenant={row.tenant_id}:user={row.actor_id}:run={row.id}:'
            f'capability=command:operation=run:resource={prefix}{sdk.plan.sha256}')


async def begin(sdk: Any, *, scope: str | None = None) -> Any:
    return await sdk.repository.begin_capability_approval(sdk.run.tenant_id, sdk.run.id,
        actor_id=sdk.run.actor_id, approval_id='approval_sdk', approval_fingerprint='sdk-private',
        approval_scope=approval_scope(sdk) if scope is None else scope, toolchain_authority=sdk.authority)


async def test_sdk_approval_is_persisted_and_public_resource_is_exact(sdk: Any) -> None:
    result = await begin(sdk)
    assert result.routing_decision['approval_checkpoint_required'] is False
    payload = sdk.approvals[0].payload
    assert 'toolchain_input_binding' in payload
    assert 'python_input_binding' not in payload and 'python_binding' not in payload
    assert payload['toolchain_input_binding']['artifact_sha256'] == sdk.plan.artifact.artifact_sha256
    events = _public_plugin_approval_events(sdk.approvals, sdk.run)
    assert events[0]['payload'] == {'run_id': str(sdk.run.id), 'version': sdk.run.version,
        'capability': 'command', 'operation': 'run', 'resource': PREFIX + sdk.plan.sha256,
        'approval_scope': approval_scope(sdk)}
    assert 'catalog_sha256' not in str(events) and 'original_reference' not in str(events)


@pytest.mark.parametrize('defect', ['state', 'role', 'generated', 'scope', 'lease'])
async def test_invalid_sdk_binding_cannot_create_generic_approval(sdk: Any, defect: str) -> None:
    scope = approval_scope(sdk)
    if defect == 'state':
        sdk.run.routing_decision[KEY]['plan_sha256'] = '0' * 64
    elif defect == 'role':
        sdk.run.actor_role = 'viewer'
    elif defect == 'generated':
        sdk.run.routing_decision['generated_project_preparation'] = None
    elif defect == 'scope':
        scope += 'extra'
    else:
        sdk.run.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(RunConflict, match='toolchain'):
        await begin(sdk, scope=scope)
    assert sdk.approvals == []


@pytest.mark.parametrize('defect', ['none', 'arguments', 'role', 'source', 'python', 'missing_binding'])
async def test_approved_sdk_scope_and_fingerprint_require_current_exact_binding(sdk: Any, defect: str) -> None:
    await begin(sdk)
    row = sdk.approvals[0]
    row.status = 'approved'
    if defect == 'arguments':
        row.payload['toolchain_input_binding'] = {'schema_version': 1, 'plan_sha256': sdk.plan.sha256}
    elif defect == 'role':
        sdk.run.actor_role = 'admin'
    elif defect == 'source':
        sdk.run.routing_decision['project_input_preparation']['reference']['input_source_sha256'] = '0' * 64
    elif defect == 'python':
        row.payload['approval_scope'] = approval_scope(sdk, 'workspace/current/project-inputs/python/')
    elif defect == 'missing_binding':
        row.payload.pop('toolchain_input_binding', None)
    # Observed capacity changes do not change what was approved.
    if defect == 'none':
        sdk.run.routing_decision[KEY]['plan'] = free_changed(sdk.plan).to_payload()
    by_scope = await sdk.repository.is_capability_approval_scope_approved(
        sdk.run.tenant_id, sdk.run.id, approval_scope(sdk))
    by_fingerprint = await sdk.repository.is_capability_approval_approved(
        sdk.run.tenant_id, sdk.run.id, 'sdk-private')
    assert by_scope is (defect == 'none')
    assert by_fingerprint is (defect == 'none')


async def test_sdk_public_history_never_projects_stale_binding(sdk: Any) -> None:
    await begin(sdk)
    sdk.approvals[0].payload['toolchain_input_binding'] = {'plan_sha256': sdk.plan.sha256}
    events = _public_plugin_approval_events(sdk.approvals, sdk.run)
    assert events[0]['payload'] == {'run_id': str(sdk.run.id), 'version': sdk.run.version}


@pytest.mark.parametrize('phase', ['executing', 'supplied'])
async def test_receipt_or_execution_marker_cannot_create_sdk_approval(sdk: Any, case: Any, phase: str) -> None:
    case.plan = sdk.plan
    sdk.run.routing_decision[KEY] = state_for(case, phase)
    with pytest.raises(RunConflict, match='toolchain'):
        await begin(sdk)
    assert sdk.approvals == []


async def test_type_changed_binding_cannot_use_boolean_as_schema_integer(sdk: Any) -> None:
    await begin(sdk)
    sdk.approvals[0].status = 'approved'
    sdk.approvals[0].payload['toolchain_input_binding']['schema_version'] = True
    assert not await sdk.repository.is_capability_approval_scope_approved(
        sdk.run.tenant_id, sdk.run.id, approval_scope(sdk))


async def test_sdk_resolution_retains_exact_binding_and_public_history(sdk: Any, case: Any) -> None:
    await begin(sdk)
    private = deepcopy(sdk.approvals[0].payload['toolchain_input_binding'])
    result = await sdk.repository.resolve_capability_approval(sdk.run.tenant_id, sdk.run.id,
        RunStatus.RUNNING, approval_id='approval_sdk', approval_fingerprint='sdk-private')
    assert result.status is RunStatus.RUNNING
    assert sdk.approvals[0].payload['toolchain_input_binding'] == private
    assert await sdk.repository.is_capability_approval_scope_approved(
        sdk.run.tenant_id, sdk.run.id, approval_scope(sdk))
    case.plan = sdk.plan
    sdk.run.routing_decision[KEY] = state_for(case, 'supplied')
    events = _public_plugin_approval_events(sdk.approvals, sdk.run)
    assert [event['kind'] for event in events] == ['approval.requested', 'approval.resolved']
    payload = events[0]['payload']
    assert isinstance(payload, dict) and payload['resource'] == PREFIX + sdk.plan.sha256
