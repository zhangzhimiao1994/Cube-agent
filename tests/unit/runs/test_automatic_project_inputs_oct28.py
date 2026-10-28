"""Fresh ordinary maintenance selection: real store, memory SQL boundaries, no execution."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.domain.runs import TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.runs.conversations import ConversationRecord
from agent_hub.runs.project_inputs import OriginalProjectPreparer
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_conversation_mode import (
    ConversationMetadataRepository,
    ConversationModeRepository,
)
from tests.unit.runs.test_harness_decision import ACTOR_ID, TENANT_ID, RecordingQueue
from tests.unit.runs.test_terminal_hooks import ExecutableFakeRepository, FakeTransaction

VERSION = 'automatic_project_input_version'
STATE = 'automatic_project_input_selection'
ACTUAL = ('请继续维护当前已经交付的 ticket_summary 项目，在同一项目和会话中增加 NDJSON 输出。'
          '由你自己首先只新增 tests/test_ndjson_acceptance.py，通过真实失败再修实现并交付 ZIP。')


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    try:
        for _ in range(10000):
            suspended = coroutine.send(None)
            assert suspended is None, 'fixture must not schedule external IO'
    except StopIteration as completed:
        return cast(T, completed.value)
    finally:
        coroutine.close()
    raise AssertionError('unbounded fixture coroutine')


@pytest.fixture(autouse=True)
def memory_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    async def immediate(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', immediate)


def submission(request: str, **options: Any) -> Mapping[str, Any]:
    now = datetime.now(UTC)
    conversation = ConversationRecord(id=uuid4(), tenant_id=TENANT_ID, conversation_id='conv-maintain',
        title='Existing', project_id='existing', project_label='Existing', workspace_path='session-a',
        archived_at=None, created_at=now, updated_at=now)
    repository = ConversationModeRepository(TaskMode.DIRECT)
    class Preparer:
        async def __call__(self, context: TaskContext) -> TaskContext:
            raise AssertionError('submit must not prepare or execute')
        async def select_existing(self, context: TaskContext) -> TaskContext:
            raise AssertionError('submit must not read workspace')
    snapshot_repository = options.pop('conversation_artifact_repository', None)
    submission_role = options.pop('submission_actor_role', Role.OPERATOR)
    service = RunService(cast(RunRepository, repository),
        conversation_artifact_repository=snapshot_repository,
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)), router=None,
        task_queue=RecordingQueue(), project_input_preparer=Preparer(),
        conversation_repository=ConversationMetadataRepository(conversation))
    values = {'project_id': 'existing', 'workspace_session_id': 'session-a', 'conversation_id': 'conv-maintain',
        'sandbox_profile': 'workspace_write', 'execution_backend': 'systemd', 'permission_mode': 'default',
        'requested_permissions': ('workspace.read', 'workspace.write', 'command.run'),
        'skip_evolution_proposal': True, 'allow_scale_mode_upgrade': False}
    values.update(options)
    run(service.submit(tenant_id=TENANT_ID, actor_id=ACTOR_ID, actor_role=submission_role,
        message=request, mode=TaskMode.DIRECT, **values))
    return cast(Mapping[str, Any], repository.created[-1]['routing_decision'])


@pytest.mark.parametrize('message', [ACTUAL, '继续维护当前项目，新增NDJSON并交付ZIP',
    '加一个导出功能', 'Please add NDJSON output to the current project.'])
def test_ordinary_modification_submission_marks_only_no_hidden_input(message: str) -> None:
    route = submission(message)
    assert route[VERSION] == 1
    assert 'project_input' not in route and STATE not in route
    assert route['generated_project_contract_version'] == 1
    assert route['requested_permissions'] == ['workspace.read', 'workspace.write', 'command.run']


@pytest.mark.parametrize('message', ['完成了吗？', '如何使用当前项目？', '请只分析新增NDJSON的方案',
    '如果以后要加一个导出功能，应该怎么做？', '不要修改当前项目，仅规划新增NDJSON',
    '"继续维护当前项目，新增NDJSON并交付ZIP"', '> 加一个导出功能',
    'UNTRUSTED_DATA_JSON={"request":"加一个导出功能"}', '请解释如何新增NDJSON'])
def test_questions_quoted_negative_conditional_planning_never_preverify(message: str) -> None:
    assert VERSION not in submission(message)


@pytest.mark.parametrize('options', [
    {'sandbox_profile': 'read_only', 'permission_mode': 'read_only', 'requested_permissions': ('workspace.read',)},
    {'requested_permissions': ('workspace.read', 'workspace.write')},
    {'execution_backend': 'docker'}, {'replace_workspace_files': True}, {'conversation_id': 'conv-missing'},
    {'project_input': ManagedProjectInput(('src',))},
])
def test_ineligible_and_explicit_requests_do_not_acquire_automatic_marker(options: dict[str, Any]) -> None:
    assert VERSION not in submission(ACTUAL, **options)


def selector(tmp_path: Path, *, files: bool = True) -> tuple[Any, Any, TaskContext, Path]:
    root = tmp_path / 'workspaces'
    route = {'project_id': 'existing', 'workspace_session_id': 'session-a', 'conversation_id': 'conv-maintain',
        'sandbox_profile': 'workspace_write', 'permission_mode': 'default', 'execution_backend': 'systemd',
        'requested_permissions': ['workspace.read', 'workspace.write', 'command.run'],
        'generated_project_contract_version': 1, VERSION: 1}
    repository = ExecutableFakeRepository(routing_decision=route)
    repository.row.actor_role = 'operator'
    repository.row.request = ACTUAL
    token = uuid4()
    run(repository.claim_for_execution(FakeTransaction(), repository.run_id, allow_running_recovery=False,
        worker_id='test-worker', worker_lease_token=token,
        worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5)))
    context = TaskContext(run_id=repository.run_id, tenant_id=repository.row.tenant_id,
        actor_id=repository.row.actor_id, actor_role=Role.OPERATOR, mode=TaskMode.DISPATCH,
        request=ACTUAL, execution_token=token, routing_decision=cast(Mapping[str, JsonValue], route))
    store = ProjectWorkspaceStore(root)
    path = store.session_root(context.tenant_id, 'existing', 'session-a')
    path.mkdir(parents=True)
    if files:
        for name, text in {'pyproject.toml': '[project]\nname="example"\n',
            'src/ticket_summary/cli.py': 'print("old")\n', 'src/ticket_summary/core.py': 'value = 1\n',
            'src/ticket_summary/__init__.py': '', 'tests/test_original.py': 'assert True\n',
            'README.md': '# Existing\n', 'requirements.lock': 'locked\n',
            'ACCEPTANCE.json': '{}', 'MANIFEST.in': 'include README.md\n',
            'src/settings.json': '{}', 'src/config.py': 'SETTING = 1\n'}.items():
            store.write_bytes(context.tenant_id, 'existing', 'session-a', name, text.encode(), 'text/plain')
    preparer = OriginalProjectPreparer(root=root, repository=cast(RunRepository, repository),
        authorizer=cast(Any, object()), verifier=cast(Any, object()))
    return preparer, repository, context, path


def test_real_store_auto_selects_exact_production_and_absent_new_test(tmp_path: Path) -> None:
    preparer, repository, context, _ = selector(tmp_path)
    selected = run(preparer.select_existing(context))
    route = selected.routing_decision
    editable = ManagedProjectInput.from_payload(route['project_input']).editable_paths
    assert set(editable) == {'src/ticket_summary/cli.py', 'src/ticket_summary/core.py',
                             'README.md', 'tests/test_ndjson_acceptance.py'}
    assert route['project_input_contract_version'] == 1
    assert 'generated_project_contract_version' not in route
    assert repository.row.routing_decision[STATE]['kind'] == 'original'
    assert repository.row.routing_decision[STATE]['source_sha256']
    assert selected.token_budget == context.token_budget and selected.timeout_seconds == context.timeout_seconds
    assert selected.execution_token == context.execution_token and selected.artifacts == context.artifacts
    assert route['requested_permissions'] == context.routing_decision['requested_permissions']
    assert 'project_input_preparation' not in route


def test_positive_selection_restore_without_rescanning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = False
    preparer, repository, context, path = selector(tmp_path, files=not empty)
    selected = run(preparer.select_existing(context))
    version = repository.row.version
    (path / 'later.py').write_text('changed after selection')
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('recovery must not reselect')
    monkeypatch.setattr(ProjectWorkspaceStore, 'list_files', forbidden)
    restored = run(preparer.select_existing(selected))
    assert restored.routing_decision == selected.routing_decision and repository.row.version == version
    assert repository.row.routing_decision[STATE]['kind'] == ('generated' if empty else 'original')


@pytest.mark.parametrize('defect', ['actor', 'tenant', 'lease', 'expired', 'scope', 'cancelled', 'permissions'])
def test_invalid_current_identity_never_reads_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str) -> None:
    preparer, repository, context, _ = selector(tmp_path)
    if defect == 'actor': repository.row.actor_id = uuid4()
    elif defect == 'tenant': repository.row.tenant_id = uuid4()
    elif defect == 'lease': repository.row.worker_lease_token = uuid4()
    elif defect == 'expired': repository.row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif defect == 'scope': repository.row.routing_decision['workspace_session_id'] = 'other'
    elif defect == 'cancelled': repository.row.status = 'cancelled'
    else: repository.row.routing_decision['requested_permissions'] = ['workspace.read']
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('unauthorized filesystem read')
    monkeypatch.setattr(ProjectWorkspaceStore, 'list_files', forbidden)
    with pytest.raises(RuntimeError):
        run(preparer.select_existing(context))
    assert STATE not in repository.row.routing_decision


def test_old_unmarked_context_is_not_selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preparer, repository, context, _ = selector(tmp_path)
    repository.row.routing_decision.pop(VERSION)
    context = context.model_copy(update={'routing_decision': repository.row.routing_decision})
    monkeypatch.setattr(ProjectWorkspaceStore, 'list_files', lambda *a, **k: pytest.fail('old run read'))
    assert run(preparer.select_existing(context)) is context


def test_larger_scope_is_selected_exactly_never_truncated_or_generated(tmp_path: Path) -> None:
    preparer, repository, context, path = selector(tmp_path)
    for index in range(130):
        (path / 'src' / f'module_{index}.py').write_text('value = 1')
    selected = run(preparer.select_existing(context))
    assert repository.row.routing_decision[STATE]['kind'] == 'original'
    editable = ManagedProjectInput.from_payload(selected.routing_decision['project_input']).editable_paths
    assert len(editable) == 134 and 'src' not in editable
    assert all(f'src/module_{index}.py' in editable for index in range(130))


@pytest.mark.parametrize('message', ['Please add NDJSON if possible', '请增加NDJSON，但先只做计划',
    '请修改当前项目，如果有空再执行', '请支持NDJSON？'])
def test_uncertain_leading_action_is_not_automatic(message: str) -> None:
    assert VERSION not in submission(message)


def test_generated_absent_test_path_is_unique_and_existing_tests_stay_protected(tmp_path: Path) -> None:
    preparer, repository, context, _ = selector(tmp_path)
    repository.row.request = '加一个导出功能'
    context = context.model_copy(update={'request': repository.row.request})
    selected = run(preparer.select_existing(context))
    paths = ManagedProjectInput.from_payload(selected.routing_decision['project_input']).editable_paths
    assert f'tests/test_automatic_{context.run_id.hex}.py' in paths
    assert 'tests' not in paths and 'tests/test_original.py' not in paths


def test_interrupted_selection_is_not_restarted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preparer, repository, context, _ = selector(tmp_path)
    def interrupted(*args: Any, **kwargs: Any) -> Any:
        raise OSError('read interrupted')
    monkeypatch.setattr(ProjectWorkspaceStore, 'list_files', interrupted)
    with pytest.raises(OSError):
        run(preparer.select_existing(context))
    assert repository.row.routing_decision[STATE]['phase'] == 'selecting'
    monkeypatch.setattr(ProjectWorkspaceStore, 'list_files', lambda *a, **k: pytest.fail('reselection'))
    with pytest.raises(RuntimeError):
        run(preparer.select_existing(context))


def test_source_change_during_selection_cannot_commit_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.runs import automatic_project_inputs as automatic
    preparer, repository, context, _ = selector(tmp_path)
    original = automatic.project_source_manifest
    count = 0
    async def changed(*args: Any, **kwargs: Any) -> Any:
        nonlocal count
        result = await original(*args, **kwargs)
        count += 1
        if count == 2:
            result = {**result, 'README.md': 'a' * 64}
        return result
    monkeypatch.setattr(automatic, 'project_source_manifest', changed)
    with pytest.raises(RuntimeError):
        run(preparer.select_existing(context))
    assert 'project_input' not in repository.row.routing_decision
    assert repository.row.routing_decision[STATE]['phase'] == 'selecting'


def test_selected_source_cannot_be_recaptured_after_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preparer, repository, context, path = selector(tmp_path)
    selected = run(preparer.select_existing(context))
    (path / 'src/ticket_summary/cli.py').write_text('changed before capture')
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: SimpleNamespace(time=time.monotonic))
    with pytest.raises(RuntimeError, match='original project'):
        run(preparer(selected))
    assert 'project_input_preparation' not in repository.row.routing_decision
    assert not (preparer.root / '.project-contracts').exists()


async def test_actual_worker_selects_and_observes_original_before_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.domain.runs import RunStatus
    from tests.unit.runs.test_original_project_preparation import setup
    from tests.unit.runs.test_project_input_lifecycle import ObservingRuntime
    preparer, repository, _, _, calls, _, _ = await setup(tmp_path)
    monkeypatch.setattr('tests.unit.runs.test_terminal_hooks.TENANT_ID', repository.row.tenant_id)
    route = repository.row.routing_decision
    route.pop('project_input')
    route.pop('project_input_contract_version')
    route.update({VERSION: 1, 'generated_project_contract_version': 1,
                  'permission_mode': 'default', 'conversation_id': 'conv-maintain'})
    repository.row.request = '继续维护当前项目，修复并交付ZIP'
    service = RunService(cast(RunRepository, repository), router=None, task_queue=cast(Any, object()),
        runtime_registry=RuntimeRegistry((ObservingRuntime(TaskMode.DISPATCH, calls),)),
        project_input_preparer=preparer, worker_id='test-worker')
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.COMPLETED
    assert calls == ['authorize', 'execute-original', 'model']
    assert repository.row.routing_decision[STATE]['kind'] == 'original'
    assert repository.row.routing_decision['project_input_preparation']['phase'] == 'observed'
    assert 'generated_project_contract_version' not in repository.row.routing_decision


@pytest.mark.parametrize('defect', [None, 'budget', 'permissions', 'lease_after_select', 'state_not_persisted'])
def test_service_selection_boundary_revalidates_real_record_and_no_authority_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str | None,
) -> None:
    preparer, repository, context, _ = selector(tmp_path)
    class Timeout:
        async def __aenter__(self) -> None: pass
        async def __aexit__(self, *args: object) -> None: pass
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: SimpleNamespace(time=time.monotonic))
    monkeypatch.setattr(asyncio, 'timeout', lambda seconds: Timeout())
    class Boundary:
        async def __call__(self, ctx: TaskContext) -> TaskContext:
            raise AssertionError('selector boundary must not verify')
        async def select_existing(self, ctx: TaskContext) -> TaskContext:
            result = await preparer.select_existing(ctx)
            if defect == 'budget':
                return result.model_copy(update={'token_budget': result.token_budget + 1})
            if defect == 'permissions':
                return result.model_copy(update={'routing_decision': {
                    **result.routing_decision, 'requested_permissions': ('workspace.read', 'workspace.write',
                        'command.run', 'network.read')}})
            if defect == 'lease_after_select':
                repository.row.worker_lease_token = uuid4()
            if defect == 'state_not_persisted':
                repository.row.routing_decision.pop(STATE)
            return result
    service = RunService(cast(RunRepository, repository), router=None, task_queue=RecordingQueue(),
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DISPATCH),)),
        project_input_preparer=Boundary(), worker_id='test-worker')
    if defect is None:
        selected = run(service._select_automatic_project_context(context))
        assert selected.routing_decision['project_input_contract_version'] == 1
        assert selected.token_budget == context.token_budget
        assert 0 < selected.timeout_seconds <= context.timeout_seconds
    else:
        with pytest.raises((RuntimeError, TypeError)):
            run(service._select_automatic_project_context(context))


async def test_worker_automatic_original_pending_approval_keeps_selected_context(tmp_path: Path) -> None:
    from agent_hub.domain.runs import RunStatus
    from agent_hub.runs.project_inputs import OriginalProjectApprovalPending
    from tests.unit.runs.test_project_input_lifecycle import ObservingRuntime
    preparer, repository, _, _ = selector(tmp_path)
    calls: list[str] = []
    class Waiting:
        async def select_existing(self, ctx: TaskContext) -> TaskContext:
            return await preparer.select_existing(ctx)
        async def __call__(self, ctx: TaskContext) -> TaskContext:
            assert ctx.routing_decision['project_input_contract_version'] == 1
            assert 'generated_project_contract_version' not in ctx.routing_decision
            calls.append('approval')
            state = {'schema_version': 1, 'phase': 'captured', 'reference': {
                'schema_version': 1, 'contract_sha256': 'a' * 64,
                'input_source_sha256': 'b' * 64, 'file_count': 11}}
            row = repository.row
            row.routing_decision = {**row.routing_decision, 'project_input_preparation': state,
                'approval_kind': 'capability_tool', 'approval_id': 'approval_auto1',
                'approval_checkpoint_required': False, 'approval_scope': (
                    f'capability-scope:v1:tenant={row.tenant_id}:user={row.actor_id}:run={row.id}:'
                    'capability=command:operation=run:resource=workspace/current/project-verification')}
            row.status = 'waiting_approval'
            RunRepository.clear_worker_lease(cast(Any, row))
            raise OriginalProjectApprovalPending('approval_auto1', cast(Mapping[str, JsonValue], state))
    service = RunService(cast(RunRepository, repository), router=None, task_queue=RecordingQueue(),
        runtime_registry=RuntimeRegistry((ObservingRuntime(TaskMode.DISPATCH, calls),)),
        project_input_preparer=Waiting(), worker_id='test-worker')
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.WAITING_APPROVAL and calls == ['approval']
    assert repository.row.worker_lease_token is None
    assert not any(e.kind in ('runtime.failed', 'model.started') for e in repository.event_log)


@pytest.mark.parametrize('message', ['如何开发当前项目？', '"请构建一个软件项目并交付ZIP"',
    '请新增当前项目的功能，但先只做计划'])
def test_non_action_existing_conversation_does_not_force_generated_delivery(message: str) -> None:
    route = submission(message)
    assert VERSION not in route
    assert route.get('project_delivery') != 'workspace'
    assert route.get('artifact_strategy') != 'workspace_bundle'



def test_negative_selection_detects_external_files_without_reselect_or_capture(tmp_path: Path) -> None:
    preparer, repository, context, path = selector(tmp_path, files=False)
    selected = run(preparer.select_existing(context))
    choice = dict(repository.row.routing_decision[STATE])
    (path / 'foreign.py').write_text('external = True')
    with pytest.raises(RuntimeError, match='automatic project'):
        run(preparer.select_existing(selected))
    assert repository.row.routing_decision[STATE] == choice and choice['kind'] == 'generated'
    assert 'project_input' not in repository.row.routing_decision
    assert not (preparer.root / '.project-contracts').exists()


def test_negative_unchanged_empty_selection_is_reused(tmp_path: Path) -> None:
    preparer, repository, context, _ = selector(tmp_path, files=False)
    selected = run(preparer.select_existing(context))
    version = repository.row.version
    restored = run(preparer.select_existing(selected))
    assert restored.routing_decision == selected.routing_decision and repository.row.version == version


def test_missing_readme_is_exactly_editable_without_alias_or_ancestor_conflict(tmp_path: Path) -> None:
    preparer, _, context, path = selector(tmp_path)
    (path / 'README.md').unlink()
    selected = run(preparer.select_existing(context))
    assert 'README.md' in ManagedProjectInput.from_payload(selected.routing_decision['project_input']).editable_paths


@pytest.mark.parametrize('conflict', ['readme.md', 'README.md/child.py'])
def test_missing_readme_with_case_or_ancestor_conflict_is_not_added(tmp_path: Path, conflict: str) -> None:
    preparer, _, context, path = selector(tmp_path)
    (path / 'README.md').unlink()
    target = path / conflict
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('protected')
    selected = run(preparer.select_existing(context))
    assert 'README.md' not in ManagedProjectInput.from_payload(selected.routing_decision['project_input']).editable_paths



def test_arbitrary_checkpoint_is_not_proof_of_negative_selection_ownership(tmp_path: Path) -> None:
    from agent_hub.runtime.contracts import RuntimeCheckpoint
    preparer, repository, context, path = selector(tmp_path, files=False)
    selected = run(preparer.select_existing(context))
    checkpoint = RuntimeCheckpoint(id=uuid4(), runtime_type='unproven', runtime_version='1',
        tenant_id=context.tenant_id, run_id=context.run_id, mode=context.mode, state={'claimed': True})
    selected = selected.model_copy(update={'checkpoint': checkpoint})
    (path / 'foreign.py').write_text('external = True')
    with pytest.raises(RuntimeError, match='automatic project'):
        run(preparer.select_existing(selected))
    assert repository.row.routing_decision[STATE]['kind'] == 'generated'


@pytest.mark.parametrize('root_result', ['valid', 'absent', 'rejected'])
def test_negative_drift_uses_existing_readonly_root_generated_validator(
    tmp_path: Path, root_result: str,
) -> None:
    preparer, repository, context, path = selector(tmp_path, files=False)
    selected = run(preparer.select_existing(context))
    (path / 'owned.py').write_text('this run wrote a candidate')
    repository.row.routing_decision['generated_project_preparation'] = {'opaque': 'root-validator-input'}
    selected = selected.model_copy(update={'routing_decision': repository.row.routing_decision})
    calls = []
    class RootGenerated:
        async def load(self, tenant: Any, rid: Any, actor: Any, *, readonly: bool) -> Any:
            calls.append((tenant, rid, actor, readonly))
            if root_result == 'rejected':
                raise RuntimeError('root validation rejected')
            return object() if root_result == 'valid' else None
    preparer.verifier = SimpleNamespace(generated=RootGenerated())
    if root_result == 'valid':
        restored = run(preparer.select_existing(selected))
        assert restored.routing_decision[STATE]['kind'] == 'generated'
        assert 'project_input' not in restored.routing_decision
    else:
        with pytest.raises(RuntimeError):
            run(preparer.select_existing(selected))
    assert calls == [(context.tenant_id, context.run_id, 'main_agent', True)]
    assert repository.row.routing_decision[STATE]['kind'] == 'generated'


def test_nonempty_unsupported_project_is_durable_blocker_not_generated_rebuild(tmp_path: Path) -> None:
    preparer, repository, context, path = selector(tmp_path, files=False)
    (path / 'Cargo.toml').write_text('[package]\nname="example"')
    with pytest.raises(RuntimeError, match='automatic project'):
        run(preparer.select_existing(context))
    assert repository.row.routing_decision[STATE]['kind'] == 'blocked'
    assert repository.row.routing_decision[STATE]['reason'] == 'unsupported'
    assert 'project_input' not in repository.row.routing_decision



def test_node_control_and_test_runners_are_never_production_editables(tmp_path: Path) -> None:
    preparer, _, context, path = selector(tmp_path, files=False)
    for relative in ('package.json', 'src/app.js', 'config.js', 'settings.js', 'profile.js',
                     'scripts/build.js', 'src/custom_test_runner.js', 'tests/original.js', 'README.md'):
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{}' if relative == 'package.json' else '// existing')
    selected = run(preparer.select_existing(context))
    assert set(ManagedProjectInput.from_payload(selected.routing_decision['project_input']).editable_paths) == {
        'src/app.js', 'README.md'}


def test_bare_preparer_private_repository_binding_is_metadata_only(tmp_path: Path) -> None:
    from agent_hub.capabilities.defaults import build_runtime_capability_stack
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository
    stack = build_runtime_capability_stack(tenant_id=TENANT_ID, run_repository=object(),
        skill_store_dir=tmp_path / 'skills', workspace_root=tmp_path / 'attachments',
        project_workspace_dir=tmp_path / 'workspaces')
    # Windows has no systemd executor; constructing the preparer must remain metadata-only.
    stack.runtime_gateway._project_executor_configured = True
    preparer = stack.runtime_gateway.original_project_preparer(stack.harness_tool_gateway)
    assert isinstance(preparer, OriginalProjectPreparer)
    repository = InMemoryArtifactRepository()
    preparer.bind_artifact_repository(repository)
    assert preparer.artifact_repository is repository
    assert stack.runtime_gateway._artifact_repository is None
    assert preparer.verifier._durable is None
    preparer.bind_artifact_repository(repository)
    with pytest.raises(RuntimeError, match='already bound'):
        preparer.bind_artifact_repository(InMemoryArtifactRepository())


def test_app_binds_only_preparer_before_run_service() -> None:
    import ast

    from agent_hub import app
    text = Path(app.__file__).read_text(encoding='utf-8')
    tree = ast.parse(text)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == 'bind_artifact_repository']
    assert len(calls) == 1
    call = calls[0]
    assert ast.unparse(call.func.value) == 'runtime_capability_stack.project_input_preparer'
    assert [ast.unparse(arg) for arg in call.args] == ['artifact_repository']
    assert call.lineno < text[:text.index('application.state.run_service = RunService(')].count('\n') + 1
    assert 'runtime_capability_stack = capability_stack_builder()' in text


def test_fresh_owned_read_submission_marks_snapshot_without_changing_authority() -> None:
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository
    baseline = submission('如何使用当前项目？')
    selected = submission('如何使用当前项目？',
        conversation_artifact_repository=InMemoryArtifactRepository())
    assert selected.pop('conversation_input_snapshot_version') == 1
    assert selected == baseline


@pytest.mark.parametrize('options', [
    {'conversation_artifact_repository': None},
    {'requested_permissions': ('workspace.write', 'command.run')},
    {'submission_actor_role': None},
])
def test_snapshot_marker_requires_repository_owned_actor_and_read_permission(options: dict[str, Any]) -> None:
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository
    selected = submission(ACTUAL, **{'conversation_artifact_repository': InMemoryArtifactRepository(), **options})
    assert 'conversation_input_snapshot_version' not in selected


def test_app_external_registry_still_injects_snapshot_repository(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from tests.unit.test_action_capability_stack_wiring import _settings

    from agent_hub.app import create_app
    from agent_hub.runs.artifacts import PostgresArtifactRepository
    from tests.unit.test_app_wiring import (
        FakeDatabase,
        FakeRedis,
        InMemoryAdminResourceService,
        NoopDirectRuntime,
        StubAuthService,
        StubConfigService,
        StubRateLimiter,
    )
    database = FakeDatabase()
    application = create_app(settings=_settings(tmp_path), database=database, redis_client=FakeRedis(),
        auth_service=StubAuthService(), rate_limiter=StubRateLimiter(), config_service=StubConfigService(),
        admin_resource_service=InMemoryAdminResourceService(), user_admin_service=object(),
        runtime_registry=RuntimeRegistry((NoopDirectRuntime(),)))
    with TestClient(application):
        repository = application.state.run_service._conversation_artifact_repository
        assert isinstance(repository, PostgresArtifactRepository)
        assert repository._session_factory is database.session_factory


def test_app_internal_registry_binds_metadata_only_to_real_preparer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient
    from tests.unit.test_action_capability_stack_wiring import (
        RuntimeAdapter,
        _assert_isolated_stacks,
        _capture_builds,
        _settings,
    )

    from agent_hub import app as app_module
    from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
    from tests.unit.test_app_wiring import (
        FakeDatabase,
        FakeRedis,
        InMemoryAdminResourceService,
        StubAuthService,
        StubConfigService,
        StubRateLimiter,
    )
    original = RuntimeCapabilityGateway.original_project_preparer
    def available_preparer(gateway: RuntimeCapabilityGateway, authorizer: Any) -> Any:
        # Only enable construction on Windows; no executor is invoked.
        gateway._project_executor_configured = True
        return original(gateway, authorizer)
    async def adapters(**kwargs: Any) -> RuntimeAdapter:
        return RuntimeAdapter()
    monkeypatch.setattr(RuntimeCapabilityGateway, 'original_project_preparer', available_preparer)
    captured = _capture_builds(monkeypatch, app_module)
    monkeypatch.setattr(app_module, 'build_runtime_mcp_service', adapters)
    monkeypatch.setattr(app_module, 'build_runtime_plugin_service', adapters)
    database, settings = FakeDatabase(), _settings(tmp_path)
    application = app_module.create_app(settings=settings, database=database, redis_client=FakeRedis(),
        auth_service=StubAuthService(), rate_limiter=StubRateLimiter(), config_service=StubConfigService(),
        admin_resource_service=InMemoryAdminResourceService(), user_admin_service=object())
    with TestClient(application):
        bare, _ = _assert_isolated_stacks(captured, settings, database.session_factory)
        repository = captured['artifacts'][0]
        assert isinstance(bare.project_input_preparer, OriginalProjectPreparer)
        assert bare.project_input_preparer.artifact_repository is repository
        service = application.state.run_service
        assert service._project_input_preparer is bare.project_input_preparer
        assert service._conversation_artifact_repository is repository


@pytest.mark.parametrize('preparer_available', [False, True])
def test_production_worker_bootstrap_shares_private_repository_without_durable_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preparer_available: bool,
) -> None:
    from tests.unit.test_action_capability_stack_wiring import (
        _assert_isolated_stacks,
        _build_worker,
    )

    from agent_hub.capabilities.runtime import RuntimeCapabilityGateway

    original = RuntimeCapabilityGateway.original_project_preparer
    def available_preparer(gateway: RuntimeCapabilityGateway, authorizer: Any) -> Any:
        gateway._project_executor_configured = preparer_available
        return original(gateway, authorizer)
    monkeypatch.setattr(RuntimeCapabilityGateway, 'original_project_preparer', available_preparer)
    resources, captured, settings, database = _build_worker(tmp_path, monkeypatch)
    assert isinstance(resources.service, RunService)
    bare, action = _assert_isolated_stacks(captured, settings, database.session_factory)
    repository = captured['artifacts'][0]
    assert resources.service._conversation_artifact_repository is repository
    assert resources.service._project_input_preparer is bare.project_input_preparer
    if preparer_available:
        assert isinstance(bare.project_input_preparer, OriginalProjectPreparer)
        assert bare.project_input_preparer.artifact_repository is repository
        assert isinstance(action.project_input_preparer, OriginalProjectPreparer)
        assert action.project_input_preparer.artifact_repository is repository
    else:
        assert bare.project_input_preparer is None
    assert bare.runtime_gateway._artifact_repository is None
    assert bare.runtime_gateway._project_verification._durable is None


@pytest.mark.parametrize('python', [True, False])
def test_configuration_prefixes_stay_protected_in_real_automatic_selection(tmp_path: Path, python: bool) -> None:
    preparer, _, context, path = selector(tmp_path, files=python)
    suffix = '.py' if python else '.js'
    if not python:
        (path / 'package.json').write_text('{}')
        (path / 'src').mkdir()
        (path / 'src/app.js').write_text('// production')
    protected = set()
    for prefix in ('config', 'settings', 'profile'):
        for separator in ('_', '-'):
            relative = ('src/' if separator == '_' else '') + prefix + separator + (
                'prod' if prefix == 'settings' else 'local') + suffix
            (path / relative).write_text('# protected' if python else '// protected')
            protected.add(relative)
    safe = 'src/configuration' + suffix
    (path / safe).write_text('# production' if python else '// production')
    selected = run(preparer.select_existing(context))
    editable = set(ManagedProjectInput.from_payload(selected.routing_decision['project_input']).editable_paths)
    assert not protected.intersection(editable)
    assert safe in editable and 'README.md' in editable
