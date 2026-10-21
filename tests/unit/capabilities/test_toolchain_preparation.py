"""Original SDK preparation with real contracts and simulated privileged transport."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.project_baselines import (
    ProjectBaselineReference,
    ProjectBaselineStore,
    StoredProjectBaseline,
)
from agent_hub.capabilities.project_contracts import (
    ManagedProjectInput,
    build_project_check_contract,
)
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.project import OriginalProjectReference, ProjectInvocation
from agent_hub.skills.sandbox.toolchain_catalog import (
    ToolchainCatalog,
    ToolchainPlanScope,
    ToolchainSupplyPlan,
    plan_toolchain_supply,
)
from agent_hub.skills.sandbox.toolchain_protocol import (
    ToolchainInvocation,
    ToolchainReference,
    ToolchainResponse,
    ToolchainSupplyReceipt,
    ToolchainSupplyStatus,
)
from tests.unit.skills.sandbox.test_toolchain_catalog import catalog, declaration, spaces

PREFIX = 'workspace/current/project-inputs/toolchain/'
KEY = 'toolchain_supply'


@pytest.fixture
def api() -> Any:
    name = 'agent_hub.capabilities.toolchain_preparation'
    assert importlib.util.find_spec(name) is not None, 'missing original SDK preparation'
    return importlib.import_module(name)


@pytest.fixture
def case(tmp_path: Path) -> Any:
    files = {'go.mod': b'module example.org/demo\ngo 1.27\n', 'main.go': b'package main\n'}
    hashes = {name: hashlib.sha256(b'f\0' + data).hexdigest() for name, data in files.items()}
    source = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    contract = build_project_check_contract(UUID(int=1), UUID(int=3), 'demo', 'session',
                                            source, ('main.go',), hashes)
    envelope = {'schema_version': 1, 'contract': contract.to_payload(), 'source_files': hashes}
    raw = json.dumps(envelope, sort_keys=True, separators=(',', ':'),
                     ensure_ascii=True, allow_nan=False).encode('ascii')
    ref = ProjectBaselineReference(hashlib.sha256(raw).hexdigest(), source, len(files))
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    stored = StoredProjectBaseline(ref, contract, tmp_path, hashes)
    scope = ToolchainPlanScope(tenant_id=UUID(int=1), user_id=UUID(int=2), run_id=UUID(int=3),
        project_id='demo', session_id='session', actor_id='main_agent', actor_role='operator',
        source_sha256=source, contract_sha256=ref.contract_sha256)
    call = ToolchainInvocation(execution_id='toolchain_base', tenant_id=scope.tenant_id,
        run_id=scope.run_id, project_id='demo', session_id='session', sandbox_profile='workspace_write',
        original_reference=OriginalProjectReference.model_validate(ref.to_payload(), strict=True),
        scope=scope, timeout_seconds=600)
    plan = plan_toolchain_supply(ToolchainCatalog.from_payload(catalog()), declaration(), scope,
        spaces(importlib.import_module('agent_hub.skills.sandbox.toolchain_catalog')),
        execution_bytes=call.workspace_limit_bytes + call.source_limit_bytes,
        source_limit_bytes=call.source_limit_bytes, source_entry_limit=10 * call.file_limit)
    environment = ToolchainReference(artifact_sha256=plan.artifact.artifact_sha256,
        archive_sha256=plan.artifact.sha256, tree_sha256='1' * 64, binary_sha256='2' * 64,
        gofmt_sha256='3' * 64, total_bytes=100, file_count=3,
        version=plan.artifact.version, platform='linux_amd64')
    route = {'project_id': 'demo', 'workspace_session_id': 'session',
        'sandbox_profile': 'workspace_write', 'execution_backend': 'systemd',
        'requested_permissions': ['workspace.read', 'workspace.write', 'command.run'],
        'project_input_contract_version': 1,
        'project_input': {'schema_version': 1, 'kind': 'managed_workspace', 'editable_paths': ['main.go']},
        'project_input_preparation': {'schema_version': 1, 'phase': 'captured', 'reference': ref.to_payload()}}
    row = SimpleNamespace(tenant_id=scope.tenant_id, id=scope.run_id, actor_id=scope.user_id,
                          actor_role='operator', routing_decision=route)
    return SimpleNamespace(call=call, plan=plan, ref=ref, stored=stored,
                           environment=environment, row=row)


def state_for(case: Any, phase: str = 'planned') -> dict[str, Any]:
    state: dict[str, Any] = {'schema_version': 1, 'phase': phase,
        'plan': case.plan.to_payload(), 'plan_sha256': case.plan.sha256,
        'original_reference': case.ref.to_payload()}
    if phase != 'planned':
        state['execution_id'] = 'toolchain_supply_' + 'a' * 32
    if phase == 'supplied':
        state['receipt'] = receipt_for(case, state['execution_id']).model_dump(mode='json')
    return state


def receipt_for(case: Any, execution: str, *, plan: ToolchainSupplyPlan | None = None) -> ToolchainSupplyReceipt:
    return ToolchainSupplyReceipt(plan=case.plan if plan is None else plan,
        supply_execution_id=execution, environment=case.environment, reused=False)


def free_changed(plan: ToolchainSupplyPlan) -> ToolchainSupplyPlan:
    payload = plan.to_payload()
    for peak in cast(list[dict[str, Any]], payload['filesystems']):
        peak['available_bytes'] -= 1
    return ToolchainSupplyPlan.from_payload(payload)


class StateIO:
    def __init__(self, value: Mapping[str, JsonValue] | None = None) -> None:
        self.value = None if value is None else _freeze_object(value, name='test state')
        self.writes: list[str] = []
        self.fail_cas = False

    async def __call__(self, *, expected: Mapping[str, JsonValue] | None = None,
                       replace: Mapping[str, JsonValue] | None = None) -> Mapping[str, JsonValue] | None:
        if replace is not None:
            if self.fail_cas or self.value != expected:
                raise ValueError('toolchain_scope_changed')
            self.value = _freeze_object(replace, name='test state')
            self.writes.append(str(replace['phase']))
        return self.value


class Root:
    def __init__(self, case: Any, state: StateIO) -> None:
        self.case, self.state = case, state
        self.calls: list[str] = []
        self.response_plan = case.plan
        self.result_plan = case.plan
        self.status: Literal['absent', 'in_progress', 'supplied', 'unknown'] = 'supplied'
        self.supply_error: BaseException | None = None
        self.profile_error: BaseException | None = None
        self.profile_result: ToolchainReference = case.environment
        self.change_state: str | None = None

    def event(self, name: str) -> None:
        self.calls.append(name)
        if self.change_state == name:
            self.state.value = _freeze_object({'changed': True}, name='concurrent mutation')

    async def plan(self, invocation: ToolchainInvocation) -> ToolchainResponse:
        self.event('plan')
        return ToolchainResponse(action='plan_project_toolchain', execution_id=invocation.execution_id,
            scope=invocation.scope, ok=True, plan=self.response_plan, missing=True)

    async def supply(self, invocation: ToolchainInvocation, *, plan: ToolchainSupplyPlan) -> ToolchainSupplyReceipt:
        assert self.state.value is not None and self.state.value['phase'] == 'executing'
        assert self.state.value['execution_id'] == invocation.execution_id
        assert plan.sha256 == self.case.plan.sha256 and invocation.install_allowed is False
        self.event('supply')
        if self.supply_error is not None:
            raise self.supply_error
        return receipt_for(self.case, invocation.execution_id, plan=self.result_plan)

    async def inspect(self, invocation: ToolchainInvocation, *, plan: ToolchainSupplyPlan,
                      supply_execution_id: str) -> ToolchainSupplyStatus:
        assert invocation.execution_id != supply_execution_id and plan.sha256 == self.case.plan.sha256
        self.event('inspect')
        receipt = receipt_for(self.case, supply_execution_id) if self.status == 'supplied' else None
        return ToolchainSupplyStatus(state=self.status, plan_sha256=plan.sha256,
            supply_execution_id=supply_execution_id, receipt=receipt)

    async def profile(self, invocation: ToolchainInvocation, *, plan: ToolchainSupplyPlan,
                      expected_environment: ToolchainReference) -> ToolchainReference:
        assert self.state.value is not None and self.state.value['phase'] == 'supplied'
        assert plan.sha256 == self.case.plan.sha256 and expected_environment == self.case.environment
        self.event('profile')
        if self.profile_error is not None:
            raise self.profile_error
        return self.profile_result


async def run(api: Any, case: Any, state: StateIO, root: Root, *, authorize: Any = None) -> Any:
    async def approved(plan: ToolchainSupplyPlan, binding: Mapping[str, JsonValue]) -> None:
        assert binding['phase'] == 'planned' and binding['plan_sha256'] == plan.sha256
        root.event('authorize')
    return await api.supply_project_toolchain(client=root, invocation=case.call, stored=case.stored,
        state_io=state, authorize=approved if authorize is None else authorize,
        deadline=asyncio.get_running_loop().time() + 30)


def test_request_separates_sdk_supply_from_python_and_execution(api: Any, case: Any) -> None:
    request = api.toolchain_supply_request(case.plan, original_reference=case.ref,
        user_id=UUID(int=2), idempotency_key='sdk')
    assert request.resource == PREFIX + case.plan.sha256
    assert capability_approval_scope(request) == (
        'capability-scope:v1:tenant=00000000-0000-0000-0000-000000000001:'
        'user=00000000-0000-0000-0000-000000000002:run=00000000-0000-0000-0000-000000000003:'
        'capability=command:operation=run:resource=' + PREFIX + case.plan.sha256)
    for resource in ('workspace/current/project-verification/toolchain/' + case.plan.sha256,
                     PREFIX, PREFIX + 'bad'):
        assert capability_approval_scope(request.model_copy(update={'resource': resource})) is None
    assert (request.agent_id, request.capability, request.operation) == ('main_agent', 'command', 'run')
    assert request.arguments == {'schema_version': 1, 'plan_sha256': case.plan.sha256,
        'original_reference': case.ref.to_payload(), 'project_id': 'demo', 'workspace_session_id': 'session',
        'actor_id': 'main_agent', 'actor_role': 'operator', 'source_sha256': case.ref.input_source_sha256,
        'contract_sha256': case.ref.contract_sha256, 'catalog_sha256': case.plan.catalog_sha256,
        'declaration_sha256': case.plan.declaration_sha256,
        'artifact_sha256': case.plan.artifact.artifact_sha256, 'archive_sha256': case.plan.artifact.sha256}
    changed = free_changed(case.plan)
    assert api.toolchain_supply_request(changed, original_reference=case.ref,
        user_id=UUID(int=2), idempotency_key='sdk') == request
    with pytest.raises(ValueError, match='toolchain_scope_changed'):
        api.toolchain_supply_request(case.plan, original_reference=case.ref,
            user_id=uuid4(), idempotency_key='sdk')


@pytest.mark.parametrize('phase', ['planned', 'executing', 'supplied'])
def test_durable_shapes_and_receipts_are_exact(api: Any, case: Any, phase: str) -> None:
    state = state_for(case, phase)
    assert api.toolchain_state_plan(state) == case.plan
    for patch in ({'schema_version': True}, {'grant': True}, {'plan_sha256': '0' * 64},
                  {'execution_id': 'input_supply_' + 'a' * 32}):
        with pytest.raises(ValueError, match='toolchain_request_invalid'):
            api.toolchain_state_plan(state | patch)
    if phase == 'supplied':
        state['receipt']['plan'] = free_changed(case.plan).to_payload()
        assert api.toolchain_state_plan(state).sha256 == case.plan.sha256
        state['receipt']['supply_execution_id'] = 'toolchain_supply_' + 'b' * 32
        with pytest.raises(ValueError, match='toolchain_request_invalid'):
            api.toolchain_state_plan(state)


@pytest.mark.parametrize('defect', ['user', 'tenant', 'run', 'project', 'session', 'actor', 'role',
    'reference', 'permission', 'backend', 'profile', 'contract_version', 'extra_capture',
    'python_recipe', 'python_supply', 'generated_version', 'generated_state'])
def test_db_binding_rejects_other_identity_or_authority(api: Any, case: Any, defect: str) -> None:
    row = case.row
    route = row.routing_decision
    route[KEY] = state_for(case)
    if defect in {'user', 'tenant', 'run'}:
        setattr(row, {'user': 'actor_id', 'tenant': 'tenant_id', 'run': 'id'}[defect], uuid4())
    elif defect in {'project', 'session'}:
        route['project_id' if defect == 'project' else 'workspace_session_id'] = 'other'
    elif defect == 'actor':
        scope = case.plan.scope.model_copy(update={'actor_id': 'implementer'})
        changed = case.plan.model_copy(update={'scope': scope})
        route[KEY].update(plan=changed.to_payload(), plan_sha256=changed.sha256)
    elif defect == 'role':
        row.actor_role = 'viewer'
    elif defect == 'reference':
        route['project_input_preparation']['reference']['file_count'] += 1
    elif defect == 'permission':
        route['requested_permissions'].remove('command.run')
    elif defect == 'backend':
        route['execution_backend'] = 'local'
    elif defect == 'profile':
        route['sandbox_profile'] = 'restricted'
    elif defect == 'contract_version':
        route['project_input_contract_version'] = True
    elif defect == 'extra_capture':
        route['project_input_preparation']['allow'] = True
    elif defect == 'python_recipe':
        route['project_input_preparation']['python_recipe'] = {}
    elif defect == 'python_supply':
        route['python_input_supply'] = None
    else:
        route['generated_project_contract_version' if defect == 'generated_version'
              else 'generated_project_preparation'] = None
    with pytest.raises((ValueError, TypeError, RuntimeError), match='toolchain'):
        api.toolchain_supply_binding(row, planned=True)


def test_binding_normalizes_database_uuid_subclass(api: Any, case: Any) -> None:
    class DatabaseUUID(UUID):
        pass
    row = case.row
    row.routing_decision[KEY] = state_for(case)
    for name in ('tenant_id', 'id', 'actor_id'):
        setattr(row, name, DatabaseUUID(str(getattr(row, name))))
    bound = api.toolchain_supply_binding(row, planned=True)
    assert all(type(getattr(bound, name)) is UUID for name in ('tenant_id', 'run_id', 'user_id'))


@pytest.mark.parametrize('role', ['viewer', 'engineer', 'unknown'])
def test_request_rejects_unprivileged_or_nonuser_roles_with_finite_error(api: Any, role: str) -> None:
    fixtures = importlib.import_module('tests.unit.skills.sandbox.test_toolchain_protocol')
    protocol = importlib.import_module('agent_hub.skills.sandbox.toolchain_protocol')
    original = fixtures.invocation(protocol).original_reference
    ref = ProjectBaselineReference.from_payload(original.model_dump(mode='json'))
    selected = fixtures.plan(protocol)
    scope = selected.scope.model_copy(update={'actor_role': role})
    plan = selected.model_copy(update={'scope': scope})
    with pytest.raises(ValueError, match='^toolchain_scope_changed$'):
        api.toolchain_supply_request(plan, original_reference=ref,
            user_id=UUID(int=2), idempotency_key='sdk')


async def test_first_supply_persists_intent_then_fresh_profile(api: Any, case: Any) -> None:
    assert case.ref.contract_sha256 != case.stored.contract.protected_sha256
    state = StateIO()
    root = Root(case, state)
    result = await run(api, case, state, root)
    assert result['phase'] == 'supplied'
    assert state.writes == ['planned', 'executing', 'supplied']
    assert root.calls == ['plan', 'authorize', 'supply', 'profile']
    assert 'install_allowed' not in result and 'execution_allowed' not in result


async def test_protected_contract_digest_cannot_impersonate_baseline_envelope(api: Any, case: Any) -> None:
    from dataclasses import replace
    forged = ProjectBaselineReference(case.stored.contract.protected_sha256,
                                     case.ref.input_source_sha256, case.ref.file_count)
    case.stored = replace(case.stored, reference=forged)
    scope = case.call.scope.model_copy(update={'contract_sha256': forged.contract_sha256})
    case.call = case.call.model_copy(update={'scope': scope,
        'original_reference': OriginalProjectReference.model_validate(forged.to_payload(), strict=True)})
    case.plan = case.plan.model_copy(update={'scope': scope})
    state = StateIO()
    root = Root(case, state)
    with pytest.raises(ValueError, match='toolchain_scope_changed'):
        await run(api, case, state, root)
    assert root.calls == [] and state.writes == []


async def test_actual_baseline_store_capture_and_load_can_prepare_sdk(api: Any, case: Any, tmp_path: Path) -> None:
    workspace = tmp_path / 'workspaces'
    if os.name == 'nt':
        from agent_hub.plugins.handover_snapshots import _windows_security
        _windows_security(workspace, private=True, create=True)
    else:
        workspace.mkdir(mode=0o700)
    source = workspace / str(UUID(int=1)) / 'projects/demo/sessions/session'
    source.mkdir(parents=True)
    (source / 'go.mod').write_bytes(b'module example.org/demo\ngo 1.27\n')
    (source / 'main.go').write_bytes(b'package main\n')
    invocation = ProjectInvocation.model_validate({name: getattr(case.call, name)
        for name in ProjectInvocation.model_fields}, strict=True)
    store = ProjectBaselineStore(workspace)
    reference = await store.capture_once(invocation, ManagedProjectInput(('main.go',)))
    assert reference == case.ref
    case.stored = store.load(invocation, reference)
    assert reference.contract_sha256 != case.stored.contract.protected_sha256
    state = StateIO()
    root = Root(case, state)
    assert (await run(api, case, state, root))['phase'] == 'supplied'


async def test_approval_pause_retains_plan_without_supply(api: Any, case: Any) -> None:
    state = StateIO()
    root = Root(case, state)
    async def pending(*_: object) -> None:
        raise ValueError('toolchain_approval_required')
    with pytest.raises(ValueError, match='toolchain_approval_required'):
        await run(api, case, state, root, authorize=pending)
    assert state.writes == ['planned'] and root.calls == ['plan']


async def test_free_space_drift_keeps_the_same_exact_approval(api: Any, case: Any) -> None:
    state = StateIO(state_for(case))
    root = Root(case, state)
    root.response_plan = root.result_plan = free_changed(case.plan)
    assert (await run(api, case, state, root))['phase'] == 'supplied'
    assert root.calls == ['plan', 'authorize', 'supply', 'profile']


@pytest.mark.parametrize('field,value', [('catalog_sha256', 'f' * 64),
    ('declaration_sha256', 'f' * 64), ('runtime_root', '/var/lib/other-sdk')])
async def test_semantic_plan_change_never_supplies(api: Any, case: Any, field: str, value: str) -> None:
    state = StateIO(state_for(case))
    root = Root(case, state)
    root.response_plan = case.plan.model_copy(update={field: value})
    with pytest.raises(ValueError, match='toolchain_plan_changed'):
        await run(api, case, state, root)
    assert root.calls == ['plan'] and not state.writes


@pytest.mark.parametrize('failure', [TimeoutError(), asyncio.CancelledError()])
async def test_uncertain_supply_recovers_by_inspection_only(api: Any, case: Any, failure: BaseException) -> None:
    state = StateIO()
    root = Root(case, state)
    root.supply_error = failure
    with pytest.raises((ValueError, asyncio.CancelledError)):
        await run(api, case, state, root)
    assert state.value is not None and state.value['phase'] == 'executing'
    old = state.value['execution_id']
    root.calls.clear()
    assert (await run(api, case, state, root))['execution_id'] == old
    assert root.calls == ['inspect', 'profile']


@pytest.mark.parametrize('status', ['absent', 'unknown', 'in_progress'])
async def test_nonfinal_recovery_never_retries_supply(api: Any, case: Any, status: str) -> None:
    state = StateIO(state_for(case, 'executing'))
    root = Root(case, state)
    root.status = cast(Literal['absent', 'in_progress', 'unknown'], status)
    with pytest.raises(ValueError, match='toolchain_unknown'):
        await run(api, case, state, root)
    assert root.calls == ['inspect'] and not state.writes


async def test_receipt_alone_cannot_pass_without_fresh_profile(api: Any, case: Any) -> None:
    state = StateIO(state_for(case, 'supplied'))
    root = Root(case, state)
    root.profile_error = ValueError('toolchain_not_ready')
    with pytest.raises(ValueError, match='toolchain_not_ready'):
        await run(api, case, state, root)
    root.profile_error = None
    root.profile_result = case.environment.model_copy(update={'tree_sha256': 'e' * 64})
    with pytest.raises(ValueError, match='toolchain_plan_changed'):
        await run(api, case, state, root)
    assert root.calls == ['profile', 'profile'] and not state.writes


@pytest.mark.parametrize('event', ['plan', 'authorize', 'supply', 'inspect', 'profile'])
async def test_db_cas_fences_concurrent_changes(api: Any, case: Any, event: str) -> None:
    phase = 'executing' if event == 'inspect' else 'supplied' if event == 'profile' else 'planned'
    state = StateIO(state_for(case, phase))
    root = Root(case, state)
    root.change_state = event
    with pytest.raises(ValueError, match='toolchain_scope_changed'):
        await run(api, case, state, root)
    assert root.calls[-1] == event


async def test_cas_failure_prevents_privileged_supply(api: Any, case: Any) -> None:
    state = StateIO(state_for(case))
    state.fail_cas = True
    root = Root(case, state)
    with pytest.raises(ValueError, match='toolchain_scope_changed'):
        await run(api, case, state, root)
    assert root.calls == ['plan', 'authorize']


async def test_new_receipt_profile_failure_keeps_supplied_for_profile_only_retry(api: Any, case: Any) -> None:
    state = StateIO()
    root = Root(case, state)
    root.profile_error = ValueError('toolchain_not_ready')
    with pytest.raises(ValueError, match='toolchain_not_ready'):
        await run(api, case, state, root)
    assert state.value is not None and state.value['phase'] == 'supplied'
    root.calls.clear()
    root.profile_error = None
    await run(api, case, state, root)
    assert root.calls == ['profile']


@pytest.mark.parametrize('defect', ['execution', 'scope', 'catalog', 'artifact'])
async def test_wrong_receipt_cannot_complete_execution(api: Any, case: Any, defect: str) -> None:
    state = StateIO(state_for(case))
    root = Root(case, state)
    if defect == 'execution':
        async def wrong(invocation: ToolchainInvocation, *, plan: ToolchainSupplyPlan) -> ToolchainSupplyReceipt:
            return receipt_for(case, 'toolchain_supply_' + 'b' * 32)
        root.supply = wrong  # type: ignore[method-assign]
    elif defect == 'scope':
        root.result_plan = case.plan.model_copy(update={
            'scope': case.plan.scope.model_copy(update={'user_id': uuid4()})})
    elif defect == 'catalog':
        root.result_plan = case.plan.model_copy(update={'catalog_sha256': 'e' * 64})
    else:
        root.result_plan = case.plan.model_copy(update={
            'artifact': case.plan.artifact.model_copy(update={'sha256': 'e' * 64})})
    with pytest.raises(ValueError, match='toolchain'):
        await run(api, case, state, root)
    assert state.value is not None and state.value['phase'] == 'executing'
    assert 'profile' not in root.calls


@pytest.mark.parametrize('deadline', [0.0, float('nan'), float('inf'), True])
async def test_invalid_or_expired_deadline_has_no_root_effect(api: Any, case: Any, deadline: float) -> None:
    state = StateIO()
    root = Root(case, state)
    with pytest.raises(ValueError, match='toolchain_timeout'):
        await api.supply_project_toolchain(client=root, invocation=case.call, stored=case.stored,
            state_io=state, authorize=None, deadline=deadline)
    assert root.calls == [] and state.writes == []


@pytest.mark.parametrize('defect', ['controls', 'unprotected', 'generated', 'reference', 'mixed'])
async def test_capture_must_be_original_exact_and_protected(api: Any, case: Any, defect: str) -> None:
    from dataclasses import replace
    if defect == 'controls':
        (case.stored.snapshot / 'go.mod').write_bytes(b'module evil.org/changed\ngo 1.27\n')
    elif defect == 'unprotected':
        contract = build_project_check_contract(UUID(int=1), UUID(int=3), 'demo', 'session',
            case.ref.input_source_sha256, ('go.mod',), case.stored.source_files)
        case.stored = replace(case.stored, contract=contract)
    elif defect == 'generated':
        case.stored = replace(case.stored, generated_metadata={'schema_version': 1,
            'purpose': 'generated_acceptance', 'capture_phase': 'post_model', 'user_id': str(UUID(int=2)),
            'request_sha256': 'a' * 64, 'manifest_sha256': 'b' * 64})
    elif defect == 'reference':
        object.__setattr__(case.stored.reference, 'file_count', 3)
    else:
        object.__setattr__(case.stored, 'source_files', dict(case.stored.source_files) | {'package.json': 'a' * 64})
    state = StateIO()
    root = Root(case, state)
    with pytest.raises((ValueError, RuntimeError), match='toolchain'):
        await run(api, case, state, root)
    assert 'supply' not in root.calls and 'authorize' not in root.calls
