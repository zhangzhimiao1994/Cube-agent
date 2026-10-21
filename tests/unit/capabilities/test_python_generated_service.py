"""Generated Python control tests: real local stores, fake Root transport, no native proof."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectService,
    validate_generated_preparation,
    validate_generated_transition,
)
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.capabilities.python_project_input_store import _capture
from agent_hub.capabilities.python_project_service import python_recipe_marker
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyPlan,
    PythonInputSupplyReceipt,
    PythonInputSupplyResponse,
    PythonInputSupplyStatus,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.capabilities.test_generated_project_service import USER, Repository
from tests.unit.capabilities.test_python_project_input_store import interpreter, selected
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST
from tests.unit.capabilities.test_python_project_service import PythonExecutor
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file


@pytest.fixture
def api() -> Any:
    return importlib.import_module('agent_hub.capabilities.python_generated_service')


class Approvals(Repository):
    def __init__(self) -> None:
        super().__init__()
        self.approvals: set[str] = set()

    async def compare_and_set_generated_preparation(self, tenant_id: UUID, run_id: UUID, **kw: Any) -> object:
        assert (tenant_id, run_id) == (TENANT, RUN)
        assert kw['actor_id'] == self.row.actor_id and kw['lease_token'] == self.row.worker_lease_token
        previous = self.row.routing_decision.get('generated_project_preparation')
        normalized = None if previous is None else validate_generated_preparation(previous)
        assert normalized == kw['expected']
        after = validate_generated_transition(previous, kw['replacement'])
        self.row.routing_decision = {**self.row.routing_decision,
            'generated_project_preparation': json.loads(json.dumps(after, default=dict))}
        self.updates += 1
        return self.row

    async def is_capability_approval_scope_approved(self, tenant: UUID, run: UUID, scope: str) -> bool:
        assert (tenant, run) == (TENANT, RUN)
        return scope in self.approvals


class Executor(PythonExecutor):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.probe_error: str | None = None
        self.failure_index = None
        self.proof_defect: str | None = None
        self.run_error: BaseException | None = None

    async def probe(self, invocation: Any) -> Any:
        if self.probe_error:
            self.probes.append(invocation)
            raise OSError(self.probe_error)
        return await super().probe(invocation)

    async def run(self, invocation: Any) -> Any:
        if self.run_error:
            raise self.run_error
        response = await super().run(invocation)
        assert response.result is not None
        raw = json.loads(response.result.stdout)
        if raw['status'] != 'passed':
            return response
        core = {'schema_version': 1,
            'manifest_sha256': hashlib.sha256((self.root / project_path() / 'ACCEPTANCE.json').read_bytes()).hexdigest(),
            'count': 1, 'names': ['test_billing.BillingTests.test_total']}
        if self.proof_defect == 'wrong-name':
            core['names'] = ['test_billing.BillingTests.test_something_else']
        if self.proof_defect == 'wrong-manifest':
            core['manifest_sha256'] = 'f' * 64
        proof = {**core, 'test_evidence_sha256': hashlib.sha256(json.dumps(core,
            sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()}
        if self.proof_defect == 'bad-digest':
            proof['test_evidence_sha256'] = 'f' * 64
        raw['test_count'] = 1
        detail = json.loads(raw['checks'][-1]['stdout'])
        detail['test_count'] = 1
        if self.proof_defect != 'absent':
            detail['generated_test_execution'] = proof
            raw['generated_test_execution'] = proof
        if self.proof_defect == 'mismatch-outer':
            raw['generated_test_execution'] = {**proof, 'test_evidence_sha256': 'f' * 64}
        raw['checks'][-1]['stdout'] = json.dumps(detail)
        raw['checks'][-1]['stdout_observed_bytes'] = len(json.dumps(detail).encode())
        return response.model_copy(update={'result': SkillResult(exit_code=0, timed_out=False,
            stdout=json.dumps(raw), stderr='')})


class Supply:
    def __init__(self, inputs: Any, executor: Executor) -> None:
        self.inputs, self.executor = inputs, executor
        self.plans: list[Any] = []
        self.supplies: list[Any] = []
        self.inspects: list[Any] = []
        self.unknown = False
        self.change_catalog = False

    def plan_value(self, invocation: Any) -> PythonInputSupplyPlan:
        return PythonInputSupplyPlan.model_validate({
            'scope': {name: getattr(invocation, name) for name in
                ('tenant_id', 'run_id', 'project_id', 'session_id', 'original_reference')},
            'declaration_sha256': self.inputs.declaration.sha256,
            'manifest_sha256': self.inputs.declaration.manifest_sha256,
            'lock_sha256': self.inputs.declaration.lock_sha256,
            'catalog_sha256': ('9' if self.change_catalog else 'd') * 64,
            'interpreter': self.inputs.interpreter.to_payload(),
            'artifacts': tuple(w.to_payload() | {'size_bytes': 100} for w in self.inputs.wheels),
        }, strict=True)

    async def plan(self, invocation: Any) -> PythonInputSupplyResponse:
        assert not invocation.install_allowed
        assert invocation.execution_id not in {p.execution_id for p in self.plans}
        self.plans.append(invocation)
        return PythonInputSupplyResponse(action='plan_python_project_inputs',
            execution_id=invocation.execution_id, ok=True, plan=self.plan_value(invocation),
            missing_wheels=tuple(w.filename for w in self.inputs.wheels))

    async def supply(self, invocation: Any, *, expected_plan_sha256: str) -> PythonInputSupplyReceipt:
        assert not invocation.install_allowed
        self.supplies.append(invocation)
        if self.unknown:
            raise ValueError('input_supply_unknown')
        self.executor.probe_error = None
        plan = self.plan_value(invocation)
        assert plan.sha256 == expected_plan_sha256
        return PythonInputSupplyReceipt(plan=plan, supply_execution_id=invocation.execution_id,
            published=tuple(w.filename for w in self.inputs.wheels), reused=())

    async def inspect(self, invocation: Any, **kw: Any) -> PythonInputSupplyStatus:
        self.inspects.append(invocation)
        return PythonInputSupplyStatus(plan_sha256=kw['expected_plan_sha256'],
            supply_execution_id=kw['supply_execution_id'], state='unknown')


async def configured(api: Any, tmp_path: Path) -> Any:
    root = tmp_path / 'projects'
    if os.name == 'nt':
        from agent_hub.plugins.handover_snapshots import _windows_security
        _windows_security(root, private=True, create=True)
    else:
        root.mkdir(mode=0o700)
    manifest = {'schema_version': 1, 'stack': 'python', 'editable_paths': ['src'],
        'test_files': ['tests/test_billing.py'], 'criteria': [{'id': 'AC-001',
            'description': 'Returns the requested totals',
            'tests': ['test_billing.BillingTests.test_total']}]}
    for name, body in {'ACCEPTANCE.json': json.dumps(manifest), 'pyproject.toml': MANIFEST.decode(),
        'requirements.lock': LOCK.decode(), 'src/billing/__init__.py': 'def total(): return 12\n',
        'tests/test_billing.py': 'import unittest\n'}.items():
        write_file(root, f'{project_path()}/{name}', body)
    repo = Approvals()
    generated = GeneratedProjectService(root=root, repository=repo)
    context = await generated.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None
    executor = Executor(root)
    owner = ProjectVerificationService(root=root, repository=repo, executor=None, python_executor=executor)
    service = api.PythonGeneratedProjectService(owner, generated, executor)
    inputs = _capture(context.stored, interpreter=interpreter(), wheels=selected(),
        max_control_bytes=256 * 1024, max_requirements=128)
    supply = Supply(inputs, executor)
    service.supply_client = supply
    return service, generated, repo, executor, supply, context


def request(*, actor: str = 'engineer') -> CapabilityRequest:
    return CapabilityRequest(tenant_id=TENANT, run_id=RUN, user_id=USER, agent_id=actor,
        capability='command', operation='run', resource='workspace/current/project-verification',
        arguments={}, idempotency_key='generated-verify')


def approve(repo: Approvals, value: CapabilityRequest) -> None:
    scope = capability_approval_scope(value)
    assert scope is not None
    repo.approvals.add(scope)


async def test_authorization_prepares_real_recipe_in_generated_state_only(api: Any, tmp_path: Path) -> None:
    service, generated, repo, executor, _, context = await configured(api, tmp_path)
    approval = await service.authorization_request(request(), context)
    current = await generated.load(TENANT, RUN, 'engineer')
    assert current is not None and current.state['phase'] == 'python_ready'
    assert current.state['python_recipe'] == python_recipe_marker(service.owner._python.executor.inputs(executor.probes[0]))
    assert approval.resource.startswith('workspace/current/project-verification/python/')
    assert approval.agent_id == 'engineer'
    assert not executor.calls and not service.owner._original_receipts
    assert not {'project_input', 'project_input_preparation', 'python_input_supply'} & repo.row.routing_decision.keys()
    assert all(not probe.install_allowed for probe in executor.probes)
    assert api.generated_python_approval_request(repo.row, agent_id='engineer', idempotency_key='projection').resource == approval.resource


async def test_recipe_approval_is_required_before_run_and_prepared_tester_can_verify(api: Any, tmp_path: Path) -> None:
    service, _, _, executor, _, context = await configured(api, tmp_path)
    approval = await service.authorization_request(request(), context)
    with pytest.raises(ProjectVerificationError, match='python_project_install_approval_required'):
        await service.verify(context, 'engineer')
    assert not executor.calls
    tester = await service.authorization_request(request(actor='tester'), context)
    assert tester.agent_id == 'tester'
    assert capability_approval_scope(tester) == capability_approval_scope(approval)
    assert not executor.calls


@pytest.mark.parametrize('code', ['python_project_runtime_missing', 'python_project_wheel_cache_unsafe',
    'python_project_wheel_cache_missing', 'python_project_source_changed'])
async def test_non_wheel_missing_never_falls_back_or_plans_supply(api: Any, tmp_path: Path, code: str) -> None:
    service, _, _, executor, supply, context = await configured(api, tmp_path)
    executor.probe_error = code
    with pytest.raises(ProjectVerificationError):
        await service.authorization_request(request(), context)
    assert not supply.plans and not supply.supplies and not executor.calls


async def test_missing_wheels_plan_only_and_proposer_bound(api: Any, tmp_path: Path) -> None:
    service, generated, repo, executor, supply, context = await configured(api, tmp_path)
    executor.probe_error = 'python_project_wheel_missing'
    approval = await service.authorization_request(request(), context)
    assert approval.resource.startswith('workspace/current/project-inputs/python/')
    assert approval.agent_id == context.envelope.proposer_actor
    assert len(supply.plans) == 1 and not supply.supplies and not executor.calls
    current = await generated.load(TENANT, RUN, 'engineer')
    assert current is not None and current.state['python_input_supply']['phase'] == 'planned'
    assert api.generated_python_approval_request(repo.row, agent_id='engineer', idempotency_key='projection').resource == approval.resource
    with pytest.raises(ProjectVerificationError):
        await service.authorization_request(request(actor='tester'), context)
    with pytest.raises(ProjectVerificationError, match='input_supply_approval_required'):
        await service.verify(context, 'engineer')
    assert not supply.supplies and not executor.calls


async def test_supply_grant_does_not_authorize_recipe_install(api: Any, tmp_path: Path) -> None:
    service, generated, repo, executor, supply, context = await configured(api, tmp_path)
    executor.probe_error = 'python_project_wheel_missing'
    approval = await service.authorization_request(request(), context)
    approve(repo, approval)
    with pytest.raises(api.PythonGeneratedRecipeApprovalRequired) as error:
        await service.verify(context, 'engineer')
    assert error.value.approval_request.resource.startswith('workspace/current/project-verification/python/')
    assert len(supply.supplies) == 1 and not executor.calls
    current = await generated.load(TENANT, RUN, 'engineer')
    assert current is not None and current.state['phase'] == 'python_ready'
    assert current.state['python_input_supply']['phase'] == 'supplied'
    assert len({p.execution_id for p in (*supply.plans, *supply.supplies, *executor.probes)}) == len(supply.plans) + len(supply.supplies) + len(executor.probes)


async def test_unknown_supply_retries_inspection_never_supply_replay(api: Any, tmp_path: Path) -> None:
    service, _, repo, executor, supply, context = await configured(api, tmp_path)
    executor.probe_error = 'python_project_wheel_missing'
    approval = await service.authorization_request(request(), context)
    approve(repo, approval)
    supply.unknown = True
    for _ in range(2):
        with pytest.raises(ProjectVerificationError, match='input_supply_unknown'):
            await service.verify(context, 'engineer')
    assert len(supply.supplies) == 1 and len(supply.inspects) == 1 and not executor.calls


async def test_invalid_request_and_stale_authority_fail_closed(api: Any, tmp_path: Path) -> None:
    service, _, repo, executor, supply, context = await configured(api, tmp_path)
    for changed in ({'user_id': UUID(int=1)}, {'arguments': {'install_allowed': True}},
                    {'resource': 'workspace/current/project-inputs/python/' + 'f' * 64}):
        with pytest.raises(ProjectVerificationError):
            await service.authorization_request(request().model_copy(update=changed), context)
    repo.row.request = 'a different task'
    with pytest.raises(ProjectVerificationError):
        await service.verify(context, 'engineer')
    assert not supply.supplies and not executor.calls


async def test_pure_projection_rejects_unbound_recipe_or_supply_and_original_route(api: Any, tmp_path: Path) -> None:
    service, _, repo, _, _, context = await configured(api, tmp_path)
    assert api.generated_python_approval_request(repo.row, agent_id='engineer', idempotency_key='x') is None
    await service.authorization_request(request(), context)
    repo.row.routing_decision['project_input_contract_version'] = 1
    with pytest.raises((ValueError, RuntimeError)):
        api.generated_python_approval_request(repo.row, agent_id='engineer', idempotency_key='x')


async def test_no_receipt_cannot_deliver_or_inspect(api: Any, tmp_path: Path) -> None:
    service, _, _, executor, _, _ = await configured(api, tmp_path)
    with pytest.raises(ProjectVerificationError, match='verification required'):
        await service.require_current(TENANT, RUN)
    assert not executor.inspections


async def test_real_decoder_named_receipt_durable_reference_and_fresh_inspect(api: Any, tmp_path: Path) -> None:
    service, generated, repo, executor, _, context = await configured(api, tmp_path)
    approval = await service.authorization_request(request(), context)
    approve(repo, approval)
    result = await service.verify(context, 'tester')
    assert result['verification']['generated_acceptance_sha256'] == context.envelope.sha256
    assert result['generated_project_acceptance']['acceptance_sha256'] == context.envelope.sha256
    assert 'original_check_contract' not in result and not service.owner._original_receipts
    assert len(executor.calls) == 1 and executor.calls[0].source_kind == 'candidate'
    assert executor.calls[0].install_allowed
    assert len(await service.environment_references(TENANT, RUN)) == 1
    for _ in range(2):
        assert await service.require_current(TENANT, RUN) == result['verification']['source_sha256']
    ids = [p.execution_id for p in (*executor.probes, *executor.calls, *executor.inspections)]
    assert len(set(ids)) == len(ids)
    assert all(not item.install_allowed for item in executor.inspections)
    assert all(item.original_reference == executor.calls[0].original_reference for item in executor.inspections)
    # Revocation and altered tests invalidate delivery even after a valid receipt.
    repo.approvals.clear()
    with pytest.raises(ProjectVerificationError, match='verification required'):
        await service.require_current(TENANT, RUN)
    assert not service.receipts
    assert await generated.load(TENANT, RUN, 'tester') is not None


@pytest.mark.parametrize('defect', ['wrong-name', 'wrong-manifest', 'bad-digest', 'absent', 'mismatch-outer'])
async def test_count_or_forged_or_unbound_names_never_mint_generated_receipt(api: Any, tmp_path: Path, defect: str) -> None:
    service, _, repo, executor, _, context = await configured(api, tmp_path)
    approve(repo, await service.authorization_request(request(), context))
    executor.proof_defect = defect
    with pytest.raises(ProjectVerificationError):
        await service.verify(context, 'engineer')
    assert not service.receipts and not service.owner._original_receipts


async def test_project_failure_retains_typed_diagnostic_and_durable_environment_only(api: Any, tmp_path: Path) -> None:
    service, _, repo, executor, _, context = await configured(api, tmp_path)
    approve(repo, await service.authorization_request(request(), context))
    executor.failure_index = 2
    with pytest.raises(ProjectVerificationError, match='python_project_checks_failed') as failure:
        await service.verify(context, 'engineer')
    assert failure.value.python_project_failure is not None
    assert not service.receipts and not service.owner._original_receipts
    assert len(await service.environment_references(TENANT, RUN)) == 1


async def test_catalog_change_while_approval_pending_prevents_supply(api: Any, tmp_path: Path) -> None:
    service, _, repo, executor, supply, context = await configured(api, tmp_path)
    executor.probe_error = 'python_project_wheel_missing'
    approve(repo, await service.authorization_request(request(), context))
    supply.change_catalog = True
    with pytest.raises(ProjectVerificationError, match='input_supply_plan_changed'):
        await service.verify(context, 'engineer')
    assert not supply.supplies and not executor.calls


@pytest.mark.parametrize('change', ['protected-test', 'lease', 'environment'])
async def test_late_changes_invalidate_receipt_without_touching_original_history(api: Any, tmp_path: Path, change: str) -> None:
    service, _, repo, executor, _, context = await configured(api, tmp_path)
    approve(repo, await service.authorization_request(request(), context))
    await service.verify(context, 'engineer')
    if change == 'protected-test':
        (service.owner.root / project_path() / 'tests/test_billing.py').write_bytes(b'changed checks')
    elif change == 'lease':
        repo.row.worker_lease_token = UUID(int=99)
    else:
        executor.changed_environment = True
    with pytest.raises(ProjectVerificationError, match='verification required'):
        await service.require_current(TENANT, RUN)
    assert not service.receipts and not service.owner._original_receipts


async def test_cleanup_uncertain_is_not_converted_to_failed_test_or_receipt(api: Any, tmp_path: Path) -> None:
    service, _, repo, executor, _, context = await configured(api, tmp_path)
    approve(repo, await service.authorization_request(request(), context))
    executor.run_error = ProcessCleanupIncomplete('python_project_cleanup_incomplete')
    with pytest.raises(ProcessCleanupIncomplete):
        await service.verify(context, 'engineer')
    assert not service.receipts and not service.owner._original_receipts


async def test_lease_revoked_during_approval_check_prevents_project_dispatch(
    api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, repo, executor, _, context = await configured(api, tmp_path)
    await service.authorization_request(request(), context)

    async def revoke(*args: Any) -> bool:
        repo.row.worker_lease_token = UUID(int=99)
        return True

    monkeypatch.setattr(repo, 'is_capability_approval_scope_approved', revoke)
    with pytest.raises(ProjectVerificationError):
        await service.verify(context, 'engineer')
    assert not executor.calls


async def test_restarted_generated_environment_reader_is_readonly_without_original_context(
    api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.domain.runs import RunStatus

    service, generated, repo, executor, _, context = await configured(api, tmp_path)
    approve(repo, await service.authorization_request(request(), context))
    await service.verify(context, 'engineer')
    repo.approvals.clear()
    repo.row.status = RunStatus.COMPLETED
    repo.row.worker_id = None
    repo.row.worker_lease_token = None
    repo.row.worker_lease_expires_at = None
    repo.row.worker_heartbeat_at = None
    before = (len(executor.calls), len(executor.probes), len(executor.inspections), repo.updates)

    async def original_forbidden(*args: Any) -> Any:
        raise AssertionError('readonly generated refs are never original history')

    monkeypatch.setattr(service.owner, 'python_original_context', original_forbidden)
    restarted = api.PythonGeneratedProjectService(service.owner, generated, executor)
    recovered = await restarted.environment_context(TENANT, RUN)
    assert recovered is not None and len(recovered.environment_references) == 1
    assert recovered.invocation.source_kind == 'candidate'
    assert recovered.invocation.install_allowed is False
    assert recovered.invocation.original_reference.model_dump() == context.stored.reference.to_payload()
    assert await restarted.environment_references(TENANT, RUN) == recovered.environment_references
    assert before == (len(executor.calls), len(executor.probes), len(executor.inspections), repo.updates)
    assert not restarted.receipts and not service.owner._original_receipts
