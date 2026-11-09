"""Local regression for original-only offline authorization and receipt consumption."""

from __future__ import annotations

from asyncio import Event, Lock, create_task, current_task, gather, wait_for
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.api.routers import admin
from agent_hub.auth.models import Role
from agent_hub.capabilities import python_project_service
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.project_baselines import (
    ProjectBaselineReference,
    ProjectBaselineStore,
    StoredProjectBaseline,
    _fingerprint,
)
from agent_hub.capabilities.project_contracts import (
    ManagedProjectInput,
    build_project_check_contract,
)
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.capabilities.python_original_metadata import standard_python_intent
from agent_hub.capabilities.python_original_private_recipe import (
    declaration_for_original_pins,
    original_pin_environment,
)
from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
from agent_hub.capabilities.python_project_recipe import parse_python_project_declaration
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.db.models import AdminResourceRow, RunApprovalRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runs import project_inputs, project_source_context
from agent_hub.runs.project_inputs import OriginalProjectApprovalPending, OriginalProjectPreparer
from agent_hub.runs.repository import RunConflict, RunRepository, _public_plugin_approval_events
from agent_hub.runtime.contracts import JsonValue, TaskContext
from tests.unit.capabilities import test_python_project_service as original
from tests.unit.capabilities.test_python_ephemeral_services_nov08 import (
    configure_ephemeral_executor,
)
from tests.unit.capabilities.test_python_project_input_store import interpreter, selected
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST
from tests.unit.runs.test_terminal_hooks import ExecutableFakeRepository, FakeTransaction


class LocalSession(FakeTransaction):
    def __init__(self, repository: LocalRepository) -> None:
        self.repository = repository
        self.bind = repository.bind
        self.active = False
        self.setting_locked = False
        self.lock_order: list[str] = []

    def in_transaction(self) -> bool:
        return self.active

    @asynccontextmanager
    async def begin(self):  # type: ignore[override,no-untyped-def]
        async with self.repository.lock:
            self.active = True
            task = current_task()
            self.repository.transaction_tasks.add(task)
            try:
                yield
            finally:
                if self.setting_locked:
                    self.repository.setting_lock.release()
                self.repository.transaction_tasks.remove(task)
                self.active = False

    async def scalar(self, statement: Any) -> Any:
        repo = self.repository
        table = statement.get_final_froms()[0].name
        if table == 'agent_hub_runs':
            if statement._for_update_arg is not None:
                self.lock_order.append('run')
            return repo.row
        if table == 'agent_hub_run_checkpoints':
            return 1 if repo.has_checkpoint else None
        if table == 'agent_hub_run_events':
            return 1 if repo.has_model_event else None
        assert statement._for_update_arg is not None
        if table == 'agent_hub_users':
            self.lock_order.append('user')
            return SimpleNamespace(tenant_id=repo.row.tenant_id, id=repo.row.actor_id,
                                   disabled=repo.user_disabled, role=repo.row.actor_role)
        if table == 'agent_hub_admin_resources':
            assert self.active and self.lock_order == ['run', 'user']
            arg = statement._for_update_arg
            assert arg.read and not arg.key_share
            assert statement.get_execution_options()['populate_existing'] is True
            params = statement.compile().params
            assert repo.row.tenant_id in params.values() and {'setting', 'system'} <= set(params.values())
            await repo.setting_lock.acquire()
            self.setting_locked = True
            self.lock_order.append('settings')
            return repo.setting_row()
        assert table == 'agent_hub_run_approvals'
        assert self.lock_order == ['run', 'user', 'settings']
        self.lock_order.append('receipt')
        approval_id = statement.compile().params['approval_id_1']
        return next((r for r in repo.receipts if r.approval_id == approval_id), None)

    async def scalars(self, statement: Any) -> Any:
        assert statement.get_final_froms()[0].name == 'agent_hub_run_approvals'
        params = statement.compile().params
        rows = self.repository.receipts
        if 'status_1' in params:
            rows = [r for r in rows if r.status == params['status_1']]
        if 'approval_id_1' in params:
            scope = next(v for v in params.values() if isinstance(v, str) and v.startswith('capability-scope:'))
            fingerprint = next(v for v in params.values() if isinstance(v, str) and len(v) == 64)
            rows = [r for r in rows if r.approval_id == params['approval_id_1']
                    or (r.action == 'capability_tool' and (r.payload.get('approval_scope') == scope
                        or r.payload.get('approval_fingerprint') == fingerprint))]
        return SimpleNamespace(all=lambda: rows)

    def add(self, row: RunApprovalRow) -> None:
        self.repository.receipts.append(row)

    async def execute(self, statement: Any) -> Any:
        assert statement.get_final_froms()[0].name == 'agent_hub_admin_resources'
        assert not self.active
        return SimpleNamespace(scalar_one_or_none=self.repository.setting_row)

    async def flush(self) -> None:
        if self.repository.on_flush is not None:
            await self.repository.on_flush(self)


class LocalSessionFactory:
    def __init__(self, repository: LocalRepository) -> None:
        self.repository = repository
        self.kw = {'bind': repository.bind}

    def __call__(self) -> LocalSession:
        assert current_task() not in self.repository.transaction_tasks, 'attempted a second checkout'
        return LocalSession(self.repository)


class LocalRepository(RunRepository):
    def __init__(self, row: Any) -> None:
        self.row = row
        self.receipts: list[RunApprovalRow] = []
        self.waited = 0
        self.has_checkpoint = self.has_model_event = self.user_disabled = False
        self.lock = Lock()
        self.setting_lock = Lock()
        self.transaction_tasks: set[object] = set()
        self.tenant_mode = 'auto_review'
        self.on_flush: Callable[[LocalSession], Awaitable[None]] | None = None
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
        super().__init__(cast(Any, LocalSessionFactory(self)))

    def setting_row(self) -> AdminResourceRow | None:
        if self.tenant_mode == 'unavailable':
            return None
        return AdminResourceRow(tenant_id=self.row.tenant_id, kind='setting', resource_id='system',
                                payload={'tool_approval_mode': self.tenant_mode})

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._record(self.row)

    async def begin_capability_approval(self, *args: Any, **kwargs: Any) -> Any:
        self.waited += 1


async def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
                run_mode: str = 'auto_review', tenant_mode: str = 'auto_review',
                standard: bool = False, mode: TaskMode = TaskMode.DISCUSS) -> tuple[Any, ...]:
    tenant, run = uuid4(), uuid4()
    sources = {'pyproject.toml': MANIFEST, 'requirements.lock': LOCK,
               'src/billing/__init__.py': b'', 'tests/test_billing.py': b'import unittest\n'}
    if standard:
        sources['pyproject.toml'] = MANIFEST.split(b'[tool.agent-hub.python-project]')[0]
        del sources['requirements.lock']
    manifest = {name: sha256(b'f\0' + data).hexdigest() for name, data in sources.items()}
    editables = ('src/billing/__init__.py',)
    contract = build_project_check_contract(tenant, run, 'project-a', 'session-a', _fingerprint(manifest),
                                            editables, manifest)
    reference = ProjectBaselineReference('a' * 64, contract.input_source_sha256, len(manifest))
    stored = StoredProjectBaseline(reference, contract, tmp_path, manifest)
    declaration = parse_python_project_declaration(
        MANIFEST, LOCK, max_control_bytes=256 * 1024, max_requirements=128)
    runtime = interpreter()
    if standard:
        intent = standard_python_intent(sources['pyproject.toml'], paths=manifest, protected=contract.protected_files)
        assert intent is not None
        declaration = declaration_for_original_pins(
            intent, declaration.requirements, environment=original_pin_environment(runtime))
    inputs = PythonProjectInputs(contract, reference, declaration, runtime, selected())
    row = ExecutableFakeRepository(routing_decision={
        'project_id': 'project-a', 'workspace_session_id': 'session-a',
        'sandbox_profile': 'workspace_write', 'execution_backend': 'systemd',
        'permission_mode': run_mode, 'requested_permissions': ['workspace.read', 'workspace.write', 'command.run'],
        'engineering_concrete_approval_version': 1,
        'project_input_contract_version': 1, 'project_input': ManagedProjectInput(editables).to_payload(),
        'project_input_preparation': {'schema_version': 2, 'phase': 'captured', 'reference': reference.to_payload(),
                                      'python_recipe': python_project_service.python_recipe_marker(inputs)},
    }).row
    row.id, row.tenant_id, row.actor_id = run, tenant, UUID(int=456)
    row.mode, row.status, row.actor_role = mode.value, RunStatus.RUNNING.value, Role.OPERATOR.value
    row.worker_id, row.worker_lease_token = 'local-worker', uuid4()
    row.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
    repository = LocalRepository(row)
    repository.tenant_mode = tenant_mode
    configure_ephemeral_executor(monkeypatch)
    executor = original.PythonExecutor(tmp_path)
    monkeypatch.setattr(executor, 'inputs', lambda _: inputs)
    verifier = ProjectVerificationService(root=tmp_path, repository=repository, executor=None,
                                          python_executor=executor)

    async def supported(*_args: object) -> None:
        pass

    async def fingerprint(*_args: object) -> str:
        return contract.input_source_sha256

    async def source_manifest(*_args: object) -> dict[str, str]:
        return manifest

    monkeypatch.setattr(verifier, '_require_supported_source', supported)
    monkeypatch.setattr(verifier._python, '_read_references', lambda _context: ())
    monkeypatch.setattr(ProjectBaselineStore, 'load', lambda *_args: stored)
    for module in (python_project_service, project_inputs, project_source_context):
        monkeypatch.setattr(module, '_read_no_links', lambda _root, path, **_kwargs: sources['/'.join(path)])
    monkeypatch.setattr(python_project_service, 'load_python_original_inputs', lambda *_args: inputs)
    monkeypatch.setattr(project_inputs, 'load_python_original_inputs', lambda *_args: inputs)
    monkeypatch.setattr(python_project_service, 'project_source_fingerprint', fingerprint)
    monkeypatch.setattr(python_project_service, 'project_source_manifest', source_manifest)
    monkeypatch.setattr(original, 'project_source_fingerprint', fingerprint)

    settings_service = admin.PersistentAdminResourceService.__new__(admin.PersistentAdminResourceService)
    settings_service._tenant_id = settings_service._actor_id = tenant
    settings_service._session_factory = repository._session_factory
    policy = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repository,
        tool_approval_mode=admin.PersistentToolApprovalMode(settings_service),
    )

    class Backend:
        project_verification_static_authorization = verifier._python.authorization_request

        def is_available(self, _tenant: object, name: str) -> bool:
            return name == 'project.verify'

    harness = HarnessToolGateway(cast(Any, Backend()), policy_gateway=policy)
    preparer = OriginalProjectPreparer(root=verifier.root, repository=repository, authorizer=harness,
                                      verifier=verifier)
    context = TaskContext(run_id=row.id, tenant_id=row.tenant_id, mode=mode,
                          request=row.request, actor_id=row.actor_id, actor_role=Role.OPERATOR,
                          execution_token=row.worker_lease_token,
                          routing_decision=cast(dict[str, JsonValue], row.routing_decision))
    return preparer, verifier, repository, executor, context, policy


@pytest.mark.parametrize('standard', [False, True], ids=['source-lock', 'standard-private-lock'])
@pytest.mark.parametrize('mode', [TaskMode.DISCUSS, TaskMode.DIRECT, TaskMode.HYBRID, TaskMode.DISPATCH])
async def test_auto_review_original_consumes_private_receipt_before_native_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, standard: bool, mode: TaskMode,
) -> None:
    preparer, verifier, repository, executor, context, _ = await setup(
        tmp_path, monkeypatch, standard=standard, mode=mode)
    assert not executor.calls and not repository.receipts
    previous = []
    if standard:
        for purpose in ('resolve', 'publish'):
            previous.append(RunApprovalRow(tenant_id=context.tenant_id, run_id=context.run_id,
                approval_id=f'approval_{purpose}', action='capability_tool', status='approved',
                payload={'approval_scope': f'capability-scope:v1:resource=workspace/current/project-resolution/{purpose}'}))
        repository.receipts.extend(previous)
    run = executor.run

    async def recorded(invocation: Any) -> Any:
        assert repository.receipts[-1].status == 'consumed'
        return await run(invocation)

    monkeypatch.setattr(executor, 'run', recorded)
    with bind_execution_authority(context, worker_id='local-worker'):
        prepared = await preparer(context)
    assert not repository.waited
    assert len(executor.calls) == 1 and repository.receipts[:-1] == previous
    invocation = executor.calls[0]
    assert invocation.install_allowed and invocation.source_kind == 'original'
    assert invocation.execution_id == f'original_{context.run_id.hex}'
    assert repository.receipts[-1].status == 'consumed'
    assert prepared.routing_decision['project_input_preparation']['phase'] == 'observed'
    assert _public_plugin_approval_events(repository.receipts, repository.row) == ()
    with pytest.raises(ProjectVerificationError, match='install_approval_required'):
        await verifier._python.select(context.tenant_id, context.run_id, 'main_agent', approve=True)
    assert len(executor.calls) == 1


@pytest.mark.parametrize('run_mode,tenant_mode', [
    ('default', 'auto_review'), ('auto_review', 'ask'), ('auto_review', 'unavailable'),
])
async def test_manual_modes_still_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                      run_mode: str, tenant_mode: str) -> None:
    preparer, _, repository, executor, context, _ = await setup(
        tmp_path, monkeypatch, run_mode=run_mode, tenant_mode=tenant_mode)
    with bind_execution_authority(context, worker_id='local-worker'), pytest.raises(OriginalProjectApprovalPending):
        await preparer(context)
    assert repository.waited == 1 and not repository.receipts and not executor.calls


@pytest.mark.parametrize('effect', [PolicyEffect.REQUIRE_APPROVAL, PolicyEffect.DENY])
async def test_explicit_policy_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effect: PolicyEffect) -> None:
    preparer, _, repository, executor, context, policy = await setup(tmp_path, monkeypatch)
    policy._plugin_policy_rules = SimpleNamespace(capability_policy_rules=lambda tenant: (
        CapabilityRule(tenant, Role.OPERATOR, 'main_agent', 'command', 'run',
                       'workspace/current/project-verification/python', effect),))
    failure = OriginalProjectApprovalPending if effect is PolicyEffect.REQUIRE_APPROVAL else RuntimeError
    with bind_execution_authority(context, worker_id='local-worker'), pytest.raises(failure):
        await preparer(context)
    assert not repository.receipts and not executor.calls


@pytest.mark.parametrize('defect', ['has_model_event', 'has_checkpoint', 'user_disabled'])
async def test_unsafe_reservation_never_creates_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       defect: str) -> None:
    preparer, _, repository, executor, context, _ = await setup(tmp_path, monkeypatch)
    setattr(repository, defect, True)
    failure = OriginalProjectApprovalPending if defect == 'user_disabled' else RunConflict
    with bind_execution_authority(context, worker_id='local-worker'), pytest.raises(failure):
        await preparer(context)
    assert not repository.receipts and not executor.calls


@pytest.mark.parametrize('defect', [
    'missing', 'consumed', 'lease', 'expired', 'mode', 'version', 'user', 'policy', 'setting', 'recipe',
])
async def test_second_check_fails_before_native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                defect: str) -> None:
    preparer, verifier, repository, executor, context, policy = await setup(tmp_path, monkeypatch)
    observe = verifier.observe_original

    async def damaged(**kwargs: Any) -> Any:
        assert repository.receipts[0].status == 'reserved'
        if defect == 'missing':
            repository.receipts.clear()
        elif defect == 'consumed':
            repository.receipts[0].status = 'consumed'
        elif defect == 'lease':
            repository.row.worker_lease_token = uuid4()
        elif defect == 'expired':
            repository.row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif defect == 'mode':
            repository.row.mode = TaskMode.HYBRID.value
        elif defect == 'version':
            repository.row.version += 1
        elif defect == 'user':
            repository.user_disabled = True
        elif defect == 'policy':
            policy._tool_approval_mode = None
        elif defect == 'setting':
            repository.tenant_mode = 'ask'
        else:
            repository.receipts[0].payload['recipe_sha256'] = 'b' * 64
        return await observe(**kwargs)

    monkeypatch.setattr(verifier, 'observe_original', damaged)
    with bind_execution_authority(context, worker_id='local-worker'), pytest.raises(ProjectVerificationError):
        await preparer(context)
    assert not executor.calls


async def test_same_execution_can_consume_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preparer, verifier, repository, executor, context, _ = await setup(tmp_path, monkeypatch)
    observe = verifier.observe_original

    async def race(**kwargs: Any) -> Any:
        outcomes = await gather(observe(**kwargs), observe(**kwargs), return_exceptions=True)
        assert sum(isinstance(outcome, ProjectVerificationError) for outcome in outcomes) == 1
        return next(outcome for outcome in outcomes if not isinstance(outcome, BaseException))

    monkeypatch.setattr(verifier, 'observe_original', race)
    with bind_execution_authority(context, worker_id='local-worker'):
        await preparer(context)
    assert len(executor.calls) == 1 and repository.receipts[0].status == 'consumed'


async def test_auto_review_never_rereads_run_through_another_checkout_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparer, _, repository, executor, context, _ = await setup(tmp_path, monkeypatch)
    get = repository.get

    async def reject_nested_read(*args: Any, **kwargs: Any) -> Any:
        assert not repository.lock.locked(), 'run policy read tried a second checkout'
        return await get(*args, **kwargs)

    monkeypatch.setattr(repository, 'get', reject_nested_read)
    with bind_execution_authority(context, worker_id='local-worker'):
        await preparer(context)
    assert len(executor.calls) == 1 and repository.receipts[0].status == 'consumed'


@pytest.mark.parametrize('unproven', ['callback', 'different_factory', 'sqlite'])
async def test_unproven_settings_callback_does_not_authorize_offline_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unproven: str,
) -> None:
    preparer, _, repository, executor, context, policy = await setup(tmp_path, monkeypatch)

    async def injected(_tenant: object) -> str:
        return 'auto_review'

    reader = policy._tool_approval_mode
    assert type(reader) is admin.PersistentToolApprovalMode
    if unproven == 'callback':
        policy._tool_approval_mode = injected
    elif unproven == 'different_factory':
        reader.service._session_factory = cast(Any, LocalSessionFactory(repository))
    else:
        repository.bind.dialect.name = 'sqlite'
    with bind_execution_authority(context, worker_id='local-worker'), pytest.raises(OriginalProjectApprovalPending):
        await preparer(context)
    assert repository.waited == 1 and not repository.receipts and not executor.calls


async def test_locked_setting_survives_consume_flush_until_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparer, _, repository, executor, context, _ = await setup(tmp_path, monkeypatch)
    flushed, commit, writer_attempted = Event(), Event(), Event()

    async def pause_consume(session: LocalSession) -> None:
        if repository.receipts[-1].status == 'consumed':
            assert session.lock_order == ['run', 'user', 'settings', 'receipt']
            flushed.set()
            await commit.wait()

    async def revoke() -> None:
        writer_attempted.set()
        async with repository.setting_lock:
            repository.tenant_mode = 'ask'

    repository.on_flush = pause_consume
    native = executor.run
    with bind_execution_authority(context, worker_id='local-worker'):
        execution = create_task(preparer(context))
        writer = None
        try:
            await wait_for(flushed.wait(), 2)
            writer = create_task(revoke())
            await wait_for(writer_attempted.wait(), 2)
            assert not writer.done() and not executor.calls

            async def run_after_revoke(invocation: Any) -> Any:
                assert writer is not None
                await writer
                return await native(invocation)

            monkeypatch.setattr(executor, 'run', run_after_revoke)
            commit.set()
            with pytest.raises(ProjectVerificationError, match='install_approval_required'):
                await wait_for(execution, 2)
            assert len(executor.calls) == 1 and repository.receipts[-1].status == 'consumed'
        finally:
            commit.set()
            tasks = [task for task in (execution, writer) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('status', ['pending', 'approved'])
async def test_prior_exact_human_ledger_is_never_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                          status: str) -> None:
    preparer, verifier, repository, executor, context, _ = await setup(tmp_path, monkeypatch)
    selected = await verifier._python.select(context.tenant_id, context.run_id, 'main_agent', phases=('captured',))
    request = selected.inputs.approval_request(user_id=context.actor_id, agent_id='main_agent',
        idempotency_key='original-project-admission-' + context.run_id.hex)
    manual = RunApprovalRow(tenant_id=context.tenant_id, run_id=context.run_id, approval_id='approval_old',
        action='capability_tool', status=status, payload={'approval_scope': capability_approval_scope(request),
            'approval_fingerprint': fingerprint_capability_request(request)})
    repository.receipts.append(manual)
    with bind_execution_authority(context, worker_id='local-worker'):
        if status == 'pending':
            with pytest.raises(OriginalProjectApprovalPending):
                await preparer(context)
        else:
            await preparer(context)
    assert repository.receipts == [manual] and manual.status == status
    assert len(executor.calls) == (status == 'approved')


@pytest.mark.parametrize('variant', ['exact', 'read_only', 'plugin', 'generic', 'resolve', 'publish'])
async def test_builtin_exact_recipe_can_enter_private_rules_review(variant: str) -> None:
    row = ExecutableFakeRepository(routing_decision={
        'project_id': 'project-a', 'workspace_session_id': 'session-a',
        'sandbox_profile': 'workspace_write', 'permission_mode': 'auto_review',
        'requested_permissions': ['workspace.read', 'workspace.write', 'command.run'],
    }).row
    assert row.actor_id is not None and row.routing_decision is not None
    row.mode, row.status, row.actor_role = TaskMode.DISCUSS.value, RunStatus.RUNNING.value, Role.OPERATOR.value
    repository = LocalRepository(row)

    async def enabled(_tenant: object) -> str:
        return 'auto_review'

    gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repository, tool_approval_mode=enabled,
    )
    request = CapabilityRequest(tenant_id=row.tenant_id, user_id=row.actor_id, run_id=row.id,
        agent_id='main_agent', capability='command', operation='run',
        resource='workspace/current/project-verification/python/' + 'a' * 64,
        arguments={'schema_version': 1, 'recipe_sha256': 'a' * 64, 'environment_input_sha256': 'b' * 64,
                   'project_id': 'project-a', 'workspace_session_id': 'session-a'},
        idempotency_key='original-project-admission-' + row.id.hex)
    if variant == 'read_only':
        row.routing_decision['permission_mode'] = 'read_only'
    elif variant == 'plugin':
        request = request.model_copy(update={'tool_origin': 'plugin'})
    elif variant == 'generic':
        request = request.model_copy(update={'resource': 'workspace/current/project-verification', 'arguments': {}})
    elif variant in {'resolve', 'publish'}:
        request = request.model_copy(update={'resource': f'workspace/current/project-resolution/{variant}/' + 'a' * 64})
    allowed = gateway._original_python_offline_action_allowed(
        request, role=Role.OPERATOR, permission_mode=row.routing_decision['permission_mode'],
    )
    assert allowed is (variant == 'exact')
