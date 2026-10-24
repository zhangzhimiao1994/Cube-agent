"""Real DAO component tests; transaction doubles do not prove PostgreSQL locking."""

from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self, cast
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.toolchain_preparation import toolchain_supply_binding
from agent_hub.db.models import RunApprovalRow, RunRow, UserRow
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.repository import RunConflict, RunNotFound, RunRepository
from agent_hub.runtime.contracts import JsonValue, TaskContext
from tests.unit.capabilities.test_toolchain_preparation import case as case  # noqa: PLC0414
from tests.unit.capabilities.test_toolchain_preparation import (
    free_changed,
    state_for,
)


def api() -> Any:
    name = 'agent_hub.runs.toolchain_supply'
    assert importlib.util.find_spec(name) is not None, 'missing production toolchain supply adapter'
    return importlib.import_module(name)


def object_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


class SupplyDB:
    def __init__(self, case: Any) -> None:
        self.case = case
        self.row = RunRow(id=case.call.run_id, tenant_id=case.call.tenant_id,
            actor_id=case.plan.scope.user_id, actor_role='operator', request='repair Go original',
            mode='dispatch', status='running', version=4, created_at=datetime.now(UTC),
            routing_decision=deepcopy(case.row.routing_decision), worker_id='worker-go',
            worker_lease_token=uuid4(), worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5))
        self.route['unrelated'] = {'keep': [1, False, 'value']}
        self.approvals: list[RunApprovalRow] = []
        self.user = UserRow(id=self.row.actor_id, tenant_id=self.row.tenant_id,
                            username='operator_go', role='operator', disabled=False)
        self.user_missing = False
        self.commits: list[str | None] = []
        self.fail_phase: str | None = None
        self.before_approval: Any = None
        self.repository = RunRepository(cast(Any, lambda: Session(self)))
        self.bound = api().ToolchainSupplyAuthority.from_context(self.context(), worker_id='worker-go')

    @property
    def route(self) -> dict[str, object]:
        assert self.row.routing_decision is not None
        return self.row.routing_decision

    def context(self) -> TaskContext:
        row = self.row
        assert row.actor_role is not None and row.mode is not None
        return TaskContext(tenant_id=row.tenant_id, run_id=row.id, actor_id=row.actor_id,
            actor_role=Role(row.actor_role), execution_token=row.worker_lease_token,
            request=row.request, mode=TaskMode(row.mode),
            routing_decision=cast(Mapping[str, JsonValue], self.route))

    def approve(self) -> None:
        record = RunRepository._record(self.row)
        assert record.routing_decision is not None
        record.routing_decision['toolchain_supply'] = state_for(self.case)
        request = toolchain_supply_binding(record, planned=True)
        self.approvals.append(RunApprovalRow(id=uuid4(), tenant_id=self.row.tenant_id,
            run_id=self.row.id, approval_id='approval_go', action='capability_tool',
            status='approved', created_at=datetime.now(UTC), payload={
                'approval_scope': capability_approval_scope(request),
                'toolchain_input_binding': deepcopy(request.arguments)}))

    async def read(self) -> Any:
        return await self.repository.get_toolchain_supply_state(
            self.row.tenant_id, self.row.id, authority=self.bound)

    async def write(self, expected: Any, replacement: Any) -> Any:
        return await self.repository.compare_and_set_toolchain_supply(
            self.row.tenant_id, self.row.id, authority=self.bound,
            expected=expected, replacement=replacement)


class Transaction:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def __aenter__(self) -> None:
        self.session.row = deepcopy(self.session.db.row)
        self.session.active = True

    async def __aexit__(self, error: object, *_: object) -> None:
        session = self.session
        session.active = False
        if error is None:
            assert session.row.routing_decision is not None
            state = session.row.routing_decision.get('toolchain_supply')
            phase = None if state is None else object_dict(state)['phase']
            assert phase is None or isinstance(phase, str)
            if session.db.fail_phase is not None and phase == session.db.fail_phase:
                raise OSError('simulated commit failure')
            session.db.row = deepcopy(session.row)
            session.db.commits.append(phase)


class Session:
    def __init__(self, db: SupplyDB) -> None:
        self.db, self.active = db, False
        self.row = db.row

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        assert not self.active

    def begin(self) -> Transaction:
        return Transaction(self)

    async def scalar(self, statement: Any) -> Any:
        assert self.active
        compiled = statement.compile(dialect=cast(Callable[[], Dialect], postgresql.dialect)())
        assert 'FOR UPDATE' in str(compiled)
        tables = {item.name for item in statement.get_final_froms()}
        params = compiled.params
        assert len(params) == 2
        if tables == {'agent_hub_users'}:
            return self.db.user if not self.db.user_missing and set(params.values()) == {
                self.db.user.id, self.db.user.tenant_id} else None
        assert tables == {'agent_hub_runs'}
        return self.row if set(params.values()) == {self.row.id, self.row.tenant_id} else None

    async def scalars(self, statement: Any) -> Any:
        assert self.active
        assert {item.name for item in statement.get_final_froms()} == {'agent_hub_run_approvals'}
        values = statement.compile().params.values()
        assert self.row.id in values and self.row.tenant_id in values and 'approved' in values
        if self.db.before_approval is not None:
            self.db.before_approval(self.row)
        return SimpleNamespace(all=lambda: [item for item in self.db.approvals
            if item.tenant_id == self.row.tenant_id and item.run_id == self.row.id
            and item.status == 'approved'])

    async def flush(self) -> None:
        assert self.active


@pytest.fixture
def db(case: Any) -> SupplyDB:
    return SupplyDB(case)


async def test_planned_access_needs_no_grant_and_preserves_other_routing(case: Any) -> None:
    db = SupplyDB(case)
    before = deepcopy(db.row.routing_decision)
    assert before is not None
    assert await db.read() is None
    planned = state_for(db.case)
    result = await db.write(None, planned)
    assert result.version == 5
    assert result.routing_decision == {**before, 'toolchain_supply': planned}
    assert db.row.routing_decision == result.routing_decision
    assert json.loads(json.dumps(await db.read(), default=dict)) == planned


async def test_exact_grant_allows_only_forward_transitions(db: SupplyDB) -> None:
    await db.write(None, state_for(db.case))
    db.approve()
    for previous, phase in [('planned', 'executing'), ('executing', 'supplied')]:
        result = await db.write(state_for(db.case, previous), state_for(db.case, phase))
        assert result.routing_decision['toolchain_supply']['phase'] == phase
        assert json.loads(json.dumps(await db.read(), default=dict)) == state_for(db.case, phase)
    assert db.row.version == 7


@pytest.mark.parametrize('operation', ['read', 'write'])
@pytest.mark.parametrize('defect', ['actor', 'role', 'worker', 'token', 'expiry', 'missing_expiry',
    'status', 'request', 'mode', 'project', 'session', 'permissions', 'permission_added', 'profile',
    'backend', 'capture', 'capture_phase', 'capture_version', 'contract_version', 'input'])
async def test_every_access_rechecks_bound_authority(db: SupplyDB, operation: str, defect: str) -> None:
    row, route = db.row, db.route
    if defect in {'actor', 'token'}:
        setattr(row, 'actor_id' if defect == 'actor' else 'worker_lease_token', uuid4())
    elif defect in {'role', 'worker', 'status', 'request', 'mode'}:
        field = {'role': 'actor_role', 'worker': 'worker_id'}.get(defect, defect)
        setattr(row, field, {'role': 'admin', 'worker': 'other-worker', 'status': 'cancelled',
                            'request': 'changed request', 'mode': 'direct'}[defect])
    elif defect in {'expiry', 'missing_expiry'}:
        row.worker_lease_expires_at = None if defect == 'missing_expiry' else datetime.now(UTC)
    elif defect in {'project', 'session', 'profile', 'backend'}:
        route[{'project': 'project_id', 'session': 'workspace_session_id',
               'profile': 'sandbox_profile', 'backend': 'execution_backend'}[defect]] = 'other'
    elif defect in {'permissions', 'permission_added'}:
        permissions = route['requested_permissions']
        assert isinstance(permissions, list)
        route['requested_permissions'] = ['workspace.read'] if defect == 'permissions' else [
            *permissions, 'network.read']
    elif defect == 'input':
        object_dict(route['project_input'])['editable_paths'] = ['other.go']
    elif defect == 'contract_version':
        route['project_input_contract_version'] = True
    else:
        capture = object_dict(route['project_input_preparation'])
        if defect == 'capture':
            reference = object_dict(capture['reference'])
            count = reference['file_count']
            assert isinstance(count, int)
            reference['file_count'] = count + 1
        elif defect == 'capture_phase':
            capture['phase'] = 'observed'
        else:
            capture['schema_version'] = 2
    before = deepcopy(route)
    with pytest.raises(RunConflict):
        await db.read() if operation == 'read' else await db.write(None, state_for(db.case))
    assert db.row.routing_decision == before and db.row.version == 4


@pytest.mark.parametrize('key', ['python_input_supply', 'generated_project_contract_version',
                               'generated_project_preparation'])
@pytest.mark.parametrize('value', [None, False, {}])
async def test_mixed_authority_is_rejected_by_key_presence(db: SupplyDB, key: str, value: Any) -> None:
    db.route[key] = value
    with pytest.raises(RunConflict):
        await db.read()
    with pytest.raises(RunConflict):
        await db.write(None, state_for(db.case))


@pytest.mark.parametrize('phase', ['planned', 'executing', 'supplied'])
@pytest.mark.parametrize('defect', ['missing', 'pending', 'denied', 'binding', 'scope', 'boolean',
                                  'python', 'tenant', 'run'])
async def test_privileged_access_requires_live_exact_db_grant(
    db: SupplyDB, phase: str, defect: str,
) -> None:
    db.route['toolchain_supply'] = state_for(db.case, phase)
    db.approve()
    approval = db.approvals[0]
    if defect == 'missing':
        db.approvals.clear()
    elif defect in {'pending', 'denied'}:
        approval.status = defect
    elif defect == 'binding':
        object_dict(approval.payload['toolchain_input_binding'])['artifact_sha256'] = '0' * 64
    elif defect == 'boolean':
        object_dict(approval.payload['toolchain_input_binding'])['schema_version'] = True
    elif defect == 'scope':
        scope = approval.payload['approval_scope']
        assert isinstance(scope, str)
        approval.payload['approval_scope'] = scope + '/extra'
    elif defect == 'python':
        approval.payload['python_input_binding'] = None
    else:
        setattr(approval, 'tenant_id' if defect == 'tenant' else 'run_id', uuid4())
    with pytest.raises(RunConflict):
        if phase == 'planned':
            await db.write(state_for(db.case), state_for(db.case, 'executing'))
        else:
            await db.read()
    assert db.route['toolchain_supply'] == state_for(db.case, phase)


@pytest.mark.parametrize('before,after', [
    (None, 'executing'), (None, 'supplied'), ('planned', 'planned'), ('planned', 'supplied'),
    ('executing', 'planned'), ('executing', 'executing'), ('supplied', 'planned'),
    ('supplied', 'executing'), ('supplied', 'supplied'), ('executing', None)])
async def test_no_deletion_skipping_rollback_or_noop(db: SupplyDB, before: Any, after: Any) -> None:
    previous = None if before is None else state_for(db.case, before)
    if previous is not None:
        db.route['toolchain_supply'] = deepcopy(previous)
    db.approve()
    with pytest.raises((RunConflict, TypeError)):
        await db.write(previous, None if after is None else state_for(db.case, after))
    assert db.row.version == 4


@pytest.mark.parametrize('defect', ['partial', 'boolean', 'capacity', 'stale'])
async def test_expected_is_full_and_type_sensitive(db: SupplyDB, defect: str) -> None:
    db.route['toolchain_supply'] = state_for(db.case)
    db.approve()
    expected: dict[str, object] | None = state_for(db.case)
    assert expected is not None
    if defect == 'partial':
        expected.pop('original_reference')
    elif defect == 'boolean':
        expected['schema_version'] = True
    elif defect == 'capacity':
        expected['plan'] = free_changed(db.case.plan).to_payload()
    else:
        expected = None
    with pytest.raises(RunConflict):
        await db.write(expected, state_for(db.case, 'executing'))


@pytest.mark.parametrize('defect', ['plan', 'capacity', 'execution', 'receipt', 'reference', 'extra'])
async def test_forward_cas_cannot_replace_fixed_binding(db: SupplyDB, defect: str) -> None:
    db.route['toolchain_supply'] = state_for(db.case, 'executing')
    db.approve()
    after = state_for(db.case, 'supplied')
    if defect == 'plan':
        changed = db.case.plan.model_copy(update={'catalog_sha256': '0' * 64})
        after['plan'], after['plan_sha256'] = changed.to_payload(), changed.sha256
        after['receipt']['plan'] = changed.to_payload()
    elif defect == 'capacity':
        after['plan'] = free_changed(db.case.plan).to_payload()
    elif defect == 'execution':
        after['execution_id'] = 'toolchain_supply_' + 'b' * 32
        after['receipt']['supply_execution_id'] = after['execution_id']
    elif defect == 'receipt':
        after['receipt']['supply_execution_id'] = 'toolchain_supply_' + 'b' * 32
    elif defect == 'reference':
        after['original_reference']['file_count'] += 1
    else:
        after['approved'] = True
    with pytest.raises(RunConflict):
        await db.write(state_for(db.case, 'executing'), after)
    assert db.row.version == 4


async def test_lease_expiring_during_approval_query_cannot_write(db: SupplyDB) -> None:
    await db.write(None, state_for(db.case))
    db.approve()
    db.before_approval = lambda row: setattr(row, 'worker_lease_expires_at', datetime.now(UTC))
    with pytest.raises(RunConflict):
        await db.write(state_for(db.case), state_for(db.case, 'executing'))
    assert object_dict(db.route['toolchain_supply'])['phase'] == 'planned'


async def test_stale_expected_loses_after_another_commit(db: SupplyDB) -> None:
    await db.write(None, state_for(db.case))
    db.approve()
    await db.write(state_for(db.case), state_for(db.case, 'executing'))
    with pytest.raises(RunConflict):
        await db.write(state_for(db.case), state_for(db.case, 'executing'))
    assert db.row.version == 6


@pytest.mark.parametrize('identity', ['tenant', 'run'])
async def test_scoped_query_does_not_return_foreign_run(db: SupplyDB, identity: str) -> None:
    tenant, run = db.row.tenant_id, db.row.id
    if identity == 'tenant':
        tenant = uuid4()
    else:
        run = uuid4()
    with pytest.raises((RunNotFound, RunConflict)):
        await db.repository.get_toolchain_supply_state(tenant, run, authority=db.bound)


async def test_commit_failure_never_returns_success_or_changes_state(db: SupplyDB) -> None:
    db.fail_phase = 'planned'
    with pytest.raises(OSError, match='commit failure'):
        await db.write(None, state_for(db.case))
    assert 'toolchain_supply' not in db.route and db.row.version == 4


@pytest.mark.parametrize('operation', ['read', 'write'])
@pytest.mark.parametrize('phase', [None, 'planned', 'executing', 'supplied'])
@pytest.mark.parametrize('defect', ['disabled', 'downgraded'])
async def test_live_user_disabled_or_downgraded_cannot_use_old_run_or_grant(
    db: SupplyDB, operation: str, phase: Any, defect: str,
) -> None:
    if phase is not None:
        db.route['toolchain_supply'] = state_for(db.case, phase)
    db.approve()
    if defect == 'disabled':
        db.user.disabled = True
    else:
        db.user.role = 'viewer'
    with pytest.raises(RunConflict):
        if operation == 'read':
            await db.read()
        else:
            next_phase = {None: 'planned', 'planned': 'executing',
                          'executing': 'supplied', 'supplied': 'supplied'}[phase]
            await db.write(None if phase is None else state_for(db.case, phase),
                           state_for(db.case, next_phase))
    assert db.row.actor_role == 'operator' and db.row.version == 4


async def test_absent_backend_uses_existing_systemd_default(case: Any) -> None:
    case.row.routing_decision.pop('execution_backend', None)
    db = SupplyDB(case)
    await db.write(None, state_for(db.case))
    assert 'execution_backend' not in db.route


@pytest.mark.parametrize('defect', ['missing', 'tenant', 'id', 'invalid_role', 'disabled_null'])
async def test_missing_or_invalid_current_user_is_rejected(db: SupplyDB, defect: str) -> None:
    if defect == 'missing':
        db.user_missing = True
    elif defect in {'tenant', 'id'}:
        setattr(db.user, 'tenant_id' if defect == 'tenant' else 'id', uuid4())
    elif defect == 'invalid_role':
        db.user.role = 'engineer'
    else:
        db.user.disabled = cast(bool, None)
    with pytest.raises(RunConflict):
        await db.read()


@pytest.mark.parametrize('defect', ['role', 'mode', 'status'])
async def test_malformed_run_identity_is_a_scoped_conflict(db: SupplyDB, defect: str) -> None:
    setattr(db.row, 'actor_role' if defect == 'role' else defect, 'invalid')
    with pytest.raises(RunConflict):
        await db.read()


@pytest.mark.parametrize('value', [None, False, {}, {'schema_version': True}])
async def test_existing_invalid_supply_is_never_treated_as_absent(db: SupplyDB, value: Any) -> None:
    db.route['toolchain_supply'] = value
    with pytest.raises(RunConflict):
        await db.read()
    with pytest.raises(RunConflict):
        await db.write(None, state_for(db.case))


@pytest.mark.parametrize('defect', ['schema_version', 'kind'])
async def test_managed_input_requires_explicit_schema_and_kind(case: Any, defect: str) -> None:
    case.row.routing_decision['project_input'].pop(defect)
    with pytest.raises(RunConflict):
        SupplyDB(case)


async def test_authority_and_returned_state_are_detached_immutable_snapshots(db: SupplyDB) -> None:
    await db.write(None, state_for(db.case))
    read = await db.read()
    with pytest.raises(TypeError):
        read['plan']['scope']['actor_role'] = 'admin'
    with pytest.raises(TypeError):
        cast(dict[str, list[str]], db.bound.binding['project_input'])['editable_paths'][0] = 'other.go'
    plan = object_dict(object_dict(db.route['toolchain_supply'])['plan'])
    assert object_dict(plan['scope'])['actor_role'] == 'operator'
