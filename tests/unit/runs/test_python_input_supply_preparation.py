"""Actual preparer/store/service, fake Root v4/v5 IO. No native execution."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.project_baselines import ProjectBaselineReference, ProjectBaselineStore
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.capabilities.python_project_input_store import _capture
from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
from agent_hub.capabilities.python_project_recipe import parse_python_project_declaration
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.domain.runs import RunStatus
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runs.project_inputs import OriginalProjectApprovalPending
from agent_hub.runs.repository import RunConflict, RunRepository
from agent_hub.runtime.contracts import TaskContext
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyPlan,
    PythonInputSupplyReceipt,
    PythonInputSupplyResponse,
    PythonInputSupplyStatus,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.capabilities.test_python_project_input_store import (
    interpreter,
    python_controls,
    selected,
)
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST
from tests.unit.capabilities.test_python_project_service import PythonExecutor
from tests.unit.runs.test_original_project_preparation import setup


class Root(PythonExecutor):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.available = False
        self.probe_ids: list[str] = []
        self.supply_ids: list[str] = []
        self.plan_ids: list[str] = []
        self.inspect_ids: list[str] = []
        self.last: PythonInputSupplyReceipt | None = None
        self.inspect_state: Any = 'supplied'
        self.interrupt: BaseException | None = None
        self.on_io: Any = None
        self.error = 'python_project_wheel_missing'
        self.corrupt = False
        self.runtime_changed = False

    async def probe(self, invocation: Any) -> Any:
        self.probe_ids.append(invocation.execution_id)
        if not self.available:
            raise ValueError(self.error)
        result = await super().probe(invocation)
        if self.runtime_changed:
            result = result.model_copy(update={'interpreter': {
                **result.interpreter, 'binary_sha256': '9' * 64}})
        return result

    async def plan(self, invocation: Any) -> PythonInputSupplyResponse:
        self.plan_ids.append(invocation.execution_id)
        declaration = parse_python_project_declaration(MANIFEST, LOCK, max_control_bytes=256 * 1024,
                                                       max_requirements=128)
        plan = PythonInputSupplyPlan.model_validate({
            'scope': {name: getattr(invocation, name) for name in (
                'tenant_id', 'run_id', 'project_id', 'session_id', 'original_reference')},
            'declaration_sha256': declaration.sha256, 'manifest_sha256': declaration.manifest_sha256,
            'lock_sha256': declaration.lock_sha256, 'catalog_sha256': '6' * 64,
            'interpreter': interpreter().to_payload(),
            'artifacts': tuple({**wheel.to_payload(), 'size_bytes': 123} for wheel in selected()),
        }, strict=True)
        if self.corrupt:
            plan = plan.model_copy(update={'lock_sha256': '0' * 64})
        if self.on_io is not None:
            self.on_io('plan')
        return PythonInputSupplyResponse(action='plan_python_project_inputs',
            execution_id=invocation.execution_id, ok=True, plan=plan,
            missing_wheels=tuple(sorted(w.filename for w in plan.artifacts)) if not self.available else ())

    async def supply(self, invocation: Any, *, expected_plan_sha256: str) -> PythonInputSupplyReceipt:
        self.supply_ids.append(invocation.execution_id)
        # Durable intent must be visible before any fake publication side effect.
        response = await self.plan(invocation)
        assert response.plan is not None and response.plan.sha256 == expected_plan_sha256
        self.last = PythonInputSupplyReceipt(plan=response.plan, supply_execution_id=invocation.execution_id,
            published=tuple(sorted(w.filename for w in response.plan.artifacts)), reused=())
        self.available = True
        if self.on_io is not None:
            self.on_io('supply')
        if self.interrupt is not None:
            raise self.interrupt
        return self.last

    async def inspect(self, invocation: Any, *, expected_plan_sha256: str,
                      supply_execution_id: str) -> PythonInputSupplyStatus:
        self.inspect_ids.append(invocation.execution_id)
        return PythonInputSupplyStatus(state=self.inspect_state, plan_sha256=expected_plan_sha256,
            supply_execution_id=supply_execution_id,
            receipt=self.last if self.inspect_state == 'supplied' else None)


class RecipeConsent:
    """Only the actual captured recipe can receive this fixture's exact grant."""

    def __init__(self, context: TaskContext, current_recipe: Callable[[], PythonProjectInputs]) -> None:
        assert isinstance(context.actor_id, UUID)
        assert context.actor_role is Role.OPERATOR
        self.context = context
        self.actor_id = context.actor_id
        self.current_recipe = current_recipe
        self.allowed = False
        self.requests: list[CapabilityRequest] = []
        self.grants: set[str] = set()

    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        expected = self.current_recipe().approval_request(
            user_id=self.actor_id, agent_id='main_agent',
            idempotency_key='original-project-admission-' + self.context.run_id.hex)
        if (request != expected or role is not self.context.actor_role
                or role is not Role.OPERATOR or not self.allowed):
            return CapabilityResult(CapabilityStatus.DENIED, request.run_id)
        scope = capability_approval_scope(request)
        assert isinstance(scope, str)
        self.requests.append(request)
        self.grants.add(scope)
        return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)

    async def scope_approved(self, tenant: UUID, run: UUID, scope: str) -> bool:
        return (tenant, run) == (self.context.tenant_id, self.context.run_id) and scope in self.grants


async def configured(tmp_path: Path) -> tuple[Any, ...]:
    preparer, repository, context, _, calls, root, invocation = await setup(tmp_path)
    assert isinstance(context, TaskContext)
    source = root / str(invocation.tenant_id) / 'projects' / invocation.project_id / 'sessions' / invocation.session_id
    python_controls(source)
    client = Root(root)

    async def get(tenant: Any, run: Any) -> Any:
        assert (tenant, run) == (repository.row.tenant_id, repository.row.id)
        return RunRepository._record(repository.row)

    def current_recipe() -> PythonProjectInputs:
        reference = ProjectBaselineReference.from_payload(
            repository.row.routing_decision['project_input_preparation']['reference'])
        stored = ProjectBaselineStore(root).load(invocation, reference)
        return _capture(stored, interpreter=interpreter(), wheels=selected(),
                        max_control_bytes=256 * 1024, max_requirements=128)

    recipe_consent = RecipeConsent(context, current_recipe)
    # Explicit test consent for this recipe; supply approval remains independent.
    recipe_consent.allowed = True

    repository.get = get
    repository.is_capability_approval_scope_approved = recipe_consent.scope_approved
    preparer.verifier = ProjectVerificationService(root=root, repository=repository,
                                                  executor=None, python_executor=client)
    preparer.input_supply = client

    class Admission:
        _policy_gateway = recipe_consent
        allowed = False
        resolved: str | None = None

        async def authorize_project_inputs(self, tenant: Any, request: Any, **kwargs: Any) -> Any:
            from agent_hub.capabilities.python_input_preparation import input_supply_request
            calls.append('supply-authorize')
            plan = kwargs['plan']
            assert repository.row.routing_decision['python_input_supply']['phase'] == 'planned'
            if self.allowed:
                return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                    status='succeeded', payload={'authorized': True})
            route = repository.row.routing_decision
            route.update(approval_kind='capability_tool', approval_id='approval_input_supply',
                approval_checkpoint_required=False, approval_scope=capability_approval_scope(input_supply_request(
                    plan, user_id=context.actor_id, idempotency_key=request.idempotency_key)))
            repository.row.status = RunStatus.WAITING_APPROVAL.value
            RunRepository.clear_worker_lease(repository.row)
            if self.resolved is not None:
                repository.row.status = self.resolved
                repository.row.routing_decision = {
                    key: value for key, value in repository.row.routing_decision.items()
                    if not key.startswith('approval_')}
                if self.resolved == 'running':
                    repository.row.worker_id = 'replacement-worker'
                    repository.row.worker_lease_token = uuid4()
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status='failed', payload={'approval_id': 'approval_input_supply'},
                failure_reason='capability requires approval')

        async def authorize(self, tenant: UUID, request: HarnessToolCallRequest, *,
                            user_id: UUID, role: Role) -> HarnessToolCallResult:
            assert (tenant, request.run_id, user_id, role) == (
                context.tenant_id, context.run_id, context.actor_id, context.actor_role)
            assert request.tool_name == 'project.verify' and not request.arguments
            exact = current_recipe().approval_request(user_id=user_id, agent_id=request.actor,
                                                     idempotency_key=request.idempotency_key)
            scope = capability_approval_scope(exact)
            assert isinstance(scope, str)
            assert await recipe_consent.scope_approved(tenant, request.run_id, scope)
            calls.append('recipe-authorize')
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status='succeeded', payload={'authorized': True})

    admission = Admission()
    preparer.authorizer = admission
    return preparer, repository, context, client, admission, calls, root


def resume(repository: Any, context: Any) -> Any:
    token = uuid4()
    repository.row.status = RunStatus.RUNNING.value
    repository.row.worker_id, repository.row.worker_lease_token = 'new-worker', token
    repository.row.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    return context.model_copy(update={'routing_decision': deepcopy(repository.row.routing_decision),
                                      'execution_token': token})


async def test_missing_inputs_wait_supply_then_fresh_probe_without_recapture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparer, repo, context, client, admission, calls, _ = await configured(tmp_path)
    captured: list[object] = []
    original = ProjectBaselineStore.capture_once

    async def count_capture(self: Any, *args: Any, **kwargs: Any) -> Any:
        captured.append(args)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ProjectBaselineStore, 'capture_once', count_capture)
    with pytest.raises(OriginalProjectApprovalPending) as pending:
        await preparer(context)
    state = deepcopy(repo.row.routing_decision)
    assert state['project_input_preparation']['schema_version'] == 1
    assert state['project_input_preparation']['phase'] == 'captured'
    assert state['python_input_supply']['phase'] == 'planned'
    assert pending.value.matches(RunRepository._record(repo.row), context)
    assert len(captured) == 1 and not client.supply_ids and not client.calls
    admission.allowed = True
    prepared = await preparer(resume(repo, context))
    assert len(captured) == 1 and len(client.supply_ids) == 1
    assert len(set(client.probe_ids)) == len(client.probe_ids) >= 2
    assert client.supply_ids[0].startswith('input_supply_')
    assert calls[-2:] == ['supply-authorize', 'recipe-authorize']
    assert repo.row.routing_decision['python_input_supply']['phase'] == 'supplied'
    assert prepared.routing_decision['project_input_preparation']['schema_version'] == 2
    assert prepared.routing_decision['project_input_preparation']['phase'] == 'observed'
    assert json.loads(json.dumps(prepared.routing_decision['python_input_supply']['receipt']['plan'],
                                 default=dict)) == state['python_input_supply']['plan']
    assert any(a.type == 'python_project_input_supply' for a in prepared.artifacts)
    assert len(admission._policy_gateway.requests) == 1


@pytest.mark.parametrize('code', ['python_project_wheel_integrity', 'python_project_runtime_missing',
    'python_project_service_unavailable', 'python_project_lock_invalid'])
async def test_only_exact_wheel_missing_can_start_supply(tmp_path: Path, code: str) -> None:
    preparer, repo, context, client, _, _, _ = await configured(tmp_path)
    client.error = code
    with pytest.raises(ProjectVerificationError):
        await preparer(context)
    assert 'python_input_supply' not in repo.row.routing_decision and not client.plan_ids


@pytest.mark.parametrize('status', ['supplied', 'unknown', 'absent', 'in_progress'])
async def test_lost_supply_reply_only_inspects_old_execution(tmp_path: Path, status: str) -> None:
    preparer, repo, context, client, admission, _, _ = await configured(tmp_path)
    admission.allowed = True
    client.interrupt = OSError('input_supply_timeout')
    with pytest.raises((OSError, ValueError, ProjectVerificationError)):
        await preparer(context)
    state = deepcopy(repo.row.routing_decision['python_input_supply'])
    assert state['phase'] == 'executing' and len(client.supply_ids) == 1
    client.interrupt = None
    client.inspect_state = status
    resumed = resume(repo, context)
    if status == 'supplied':
        prepared = await preparer(resumed)
        assert prepared.routing_decision['python_input_supply']['phase'] == 'supplied'
    else:
        with pytest.raises((RuntimeError, ValueError)):
            await preparer(resumed)
        assert repo.row.routing_decision['python_input_supply'] == state
    assert len(client.supply_ids) == 1 and len(client.inspect_ids) == 1
    assert client.inspect_ids[0] != state['execution_id']
    assert len(admission._policy_gateway.requests) == (1 if status == 'supplied' else 0)


async def test_recipe_consent_double_requires_exact_request_role_and_grant(tmp_path: Path) -> None:
    preparer, _, context, client, admission, _, _ = await configured(tmp_path)
    with pytest.raises(OriginalProjectApprovalPending):
        await preparer(context)
    policy = admission._policy_gateway
    assert isinstance(policy, RecipeConsent)
    exact = policy.current_recipe().approval_request(user_id=context.actor_id, agent_id='main_agent',
        idempotency_key='original-project-admission-' + context.run_id.hex)
    scope = capability_approval_scope(exact)
    assert isinstance(scope, str)
    assert not await policy.scope_approved(context.tenant_id, context.run_id, scope)
    policy.allowed = False
    assert (await policy.invoke(exact, role=Role.OPERATOR)).status is CapabilityStatus.DENIED
    policy.allowed = True
    for changed in (
        exact.model_copy(update={'tenant_id': uuid4()}),
        exact.model_copy(update={'run_id': uuid4()}),
        exact.model_copy(update={'user_id': uuid4()}),
        exact.model_copy(update={'agent_id': 'other_agent'}),
        exact.model_copy(update={'idempotency_key': 'other-action'}),
        exact.model_copy(update={'resource': 'workspace/current/project-verification'}),
        exact.model_copy(update={'arguments': {**exact.arguments, 'recipe_sha256': '0' * 64}}),
    ):
        assert (await policy.invoke(changed, role=Role.OPERATOR)).status is CapabilityStatus.DENIED
    assert (await policy.invoke(exact, role=Role.VIEWER)).status is CapabilityStatus.DENIED
    assert not policy.grants and not policy.requests
    assert not await policy.scope_approved(context.tenant_id, context.run_id, scope)
    assert (await policy.invoke(exact, role=Role.OPERATOR)).status is CapabilityStatus.ALLOWED
    assert policy.requests == [exact] and policy.grants == {scope}
    assert await policy.scope_approved(context.tenant_id, context.run_id, scope)
    assert not await policy.scope_approved(uuid4(), context.run_id, scope)
    assert not await policy.scope_approved(context.tenant_id, uuid4(), scope)
    assert not await policy.scope_approved(context.tenant_id, context.run_id, 'generic-command-grant')
    assert not client.supply_ids and not client.calls


@pytest.mark.parametrize('point', ['plan', 'supply'])
async def test_old_worker_cannot_persist_after_root_io(tmp_path: Path, point: str) -> None:
    preparer, repo, context, client, admission, _, _ = await configured(tmp_path)
    admission.allowed = True

    def lose_lease(at: str) -> None:
        if at == point:
            repo.row.worker_lease_token = uuid4()

    client.on_io = lose_lease
    with pytest.raises(RunConflict):
        await preparer(context)
    assert repo.row.routing_decision['project_input_preparation']['schema_version'] == 1
    assert repo.row.routing_decision.get('python_input_supply', {}).get('phase') != 'supplied'
    assert not client.calls


async def test_cleanup_uncertainty_is_not_downgraded(tmp_path: Path) -> None:
    preparer, repo, context, client, admission, _, _ = await configured(tmp_path)
    admission.allowed = True
    error = ProcessCleanupIncomplete('input_supply_cleanup_incomplete')
    client.interrupt = error
    with pytest.raises(ProcessCleanupIncomplete) as result:
        await preparer(context)
    assert result.value is error and repo.row.routing_decision['python_input_supply']['phase'] == 'executing'


async def test_root_plan_must_match_actual_captured_lock(tmp_path: Path) -> None:
    preparer, repo, context, client, admission, _, _ = await configured(tmp_path)
    client.corrupt = True
    admission.allowed = True
    with pytest.raises((ValueError, ProjectVerificationError), match='input_supply'):
        await preparer(context)
    assert 'python_input_supply' not in repo.row.routing_decision and not client.supply_ids


async def test_fresh_probe_cannot_substitute_the_planned_interpreter(tmp_path: Path) -> None:
    preparer, repo, context, client, admission, _, _ = await configured(tmp_path)
    admission.allowed = True
    client.runtime_changed = True
    with pytest.raises((ValueError, ProjectVerificationError), match='input_supply'):
        await preparer(context)
    assert repo.row.routing_decision['python_input_supply']['phase'] == 'supplied'
    assert repo.row.routing_decision['project_input_preparation']['schema_version'] == 1
    assert not client.calls


@pytest.mark.parametrize('resolved', [None, 'queued', 'running', 'cancelled'])
async def test_real_service_preserves_fast_supply_decision(
    tmp_path: Path, resolved: str | None, caplog: pytest.LogCaptureFixture,
) -> None:
    from agent_hub.runs.service import RunService
    from agent_hub.runtime.registry import RuntimeRegistry
    from tests.unit.runs.test_terminal_hooks import RuntimeCompletes

    preparer, repo, _, client, admission, _, _ = await configured(tmp_path)
    repo.row.status = RunStatus.QUEUED.value
    RunRepository.clear_worker_lease(repo.row)
    admission.resolved = resolved
    service = RunService(cast(RunRepository, repo), runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None, task_queue=cast(Any, object()), project_input_preparer=preparer)
    result = await service.execute(repo.run_id)
    assert result.status is RunStatus(resolved or 'waiting_approval')
    assert not client.supply_ids and not client.calls
    assert repo.row.routing_decision['project_input_preparation']['schema_version'] == 1
    assert repo.row.routing_decision['python_input_supply']['phase'] == 'planned'
    assert 'run_execute_failed' not in caplog.text
