from __future__ import annotations

import hashlib
import json
import os
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectDraftRejected,
    GeneratedProjectError,
    GeneratedProjectService,
    generated_authority,
)
from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.capabilities.scoped_read import _read_no_links
from agent_hub.domain.runs import RunStatus
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file

USER = UUID('66666666-6666-4666-8666-666666666666')


def manifest() -> bytes:
    return json.dumps({'schema_version': 1, 'stack': 'node', 'editable_paths': ['src'],
        'test_files': ['test/business.test.cjs'], 'criteria': [
            {'id': 'AC-001', 'description': 'Adds two numbers', 'tests': ['adds two numbers']}]}).encode()


class Repository:
    def __init__(self) -> None:
        self.row = SimpleNamespace(tenant_id=TENANT, id=RUN, actor_id=USER, actor_role=Role.OPERATOR,
            request='Build a working addition project.', status=RunStatus.RUNNING,
            worker_id='worker-a', worker_lease_token=uuid4(),
            worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
            routing_decision={'generated_project_contract_version': 1, 'project_id': 'project-a',
                'workspace_session_id': 'session-a', 'sandbox_profile': 'workspace_write',
                'execution_backend': 'systemd', 'runtime_timeout_seconds': 120,
                'requested_permissions': ['workspace.read', 'workspace.write', 'command.run']})
        self.updates = 0

    async def get(self, tenant_id: UUID, run_id: UUID) -> object:
        assert (tenant_id, run_id) == (TENANT, RUN)
        return self.row

    async def compare_and_set_generated_preparation(self, tenant_id: UUID, run_id: UUID, **kw: object) -> object:
        assert (tenant_id, run_id) == (TENANT, RUN)
        assert kw['actor_id'] == self.row.actor_id
        assert kw['lease_token'] == self.row.worker_lease_token
        assert kw['request_sha256'] == hashlib.sha256(self.row.request.encode()).hexdigest()
        assert self.row.routing_decision.get('generated_project_preparation') == kw['expected']
        self.row.routing_decision = {**self.row.routing_decision, 'generated_project_preparation': kw['replacement']}
        self.updates += 1
        return self.row


def setup(tmp_path: Path) -> tuple[GeneratedProjectService, Repository]:
    root = tmp_path / 'projects'
    root.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        from agent_hub.plugins.handover_snapshots import _windows_security
        _windows_security(root, private=True, create=True)
    else:
        root.mkdir(mode=0o700)
    for name, body in {'ACCEPTANCE.json': manifest().decode(), 'src/index.js': 'exports.add=(a,b)=>a+b;',
        'package.json': '{"scripts":{"build":"node -v","test":"node --test"}}',
        'test/business.test.cjs': 'require("node:test").test("adds two numbers",()=>{});'}.items():
        write_file(root, f'{project_path()}/{name}', body)
    repo = Repository()
    return GeneratedProjectService(root=root, repository=repo), repo


async def test_generated_capture_is_post_model_and_reuses_frozen_scaffold(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    assert await service.load(TENANT, RUN, 'engineer') is None
    context = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None
    assert context.envelope.capture_phase == 'post_model'
    assert context.envelope.proposer_actor == 'engineer'
    assert context.stored.generated_metadata is not None
    assert 'project_input_preparation' not in repo.row.routing_decision
    assert 'project_input' not in repo.row.routing_decision
    assert repo.updates == 2
    second = await service.prepare(TENANT, RUN, 'tester', user_id=USER)
    assert second is not None and second.envelope.sha256 == context.envelope.sha256
    assert repo.updates == 2


async def test_missing_manifest_can_be_authored_before_reserving_capture(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    (service.root / project_path() / 'ACCEPTANCE.json').unlink()
    with pytest.raises(GeneratedProjectError, match='generated_acceptance_manifest_required'):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert repo.updates == 0


@pytest.mark.parametrize('field,value', [('actor_id', uuid4()), ('actor_role', Role.VIEWER),
    ('status', RunStatus.COMPLETED), ('worker_lease_token', None),
    ('worker_lease_expires_at', datetime(2000, 1, 1, tzinfo=UTC))])
async def test_capture_requires_live_authoritative_actor_and_scope(tmp_path: Path, field: str, value: object) -> None:
    service, repo = setup(tmp_path)
    setattr(repo.row, field, value)
    with pytest.raises(GeneratedProjectError):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert repo.updates == 0


async def test_frozen_tests_and_request_cannot_be_replaced(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    context = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None
    (service.root / project_path() / 'test/business.test.cjs').write_text('echo success', encoding='utf-8')
    with pytest.raises(GeneratedProjectError):
        await service.load(TENANT, RUN, 'engineer')
    repo.row.request = 'A different request.'
    with pytest.raises(GeneratedProjectError):
        await service.load(TENANT, RUN, 'engineer')


async def test_implementation_changes_do_not_redefine_acceptance(tmp_path: Path) -> None:
    service, _ = setup(tmp_path)
    first = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert first is not None
    (service.root / project_path() / 'src/index.js').write_text('exports.add=(a,b)=>Number(a)+Number(b);', encoding='utf-8')
    current = await service.load(TENANT, RUN, 'tester')
    assert current is not None and current.envelope.sha256 == first.envelope.sha256


async def test_uncertain_capture_is_never_recaptured(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    first = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert first is not None
    repo.row.routing_decision['generated_project_preparation'] = {'schema_version': 1, 'phase': 'capturing'}
    with pytest.raises(GeneratedProjectError):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert repo.updates == 2


async def test_invalid_python_recipe_can_be_repaired_before_freeze(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    original = json.loads(manifest())
    original.update(stack='python', test_files=['tests/test_app.py'], criteria=[
        {'id': 'AC-001', 'description': 'Adds numbers', 'tests': ['test_app.Case.test_add']}])
    for name, body in {'ACCEPTANCE.json': json.dumps(original), 'pyproject.toml': '[project]\nname="app"\n',
                       'requirements.lock': 'not-a-hash-lock', 'tests/test_app.py': 'import unittest\n'}.items():
        write_file(service.root, f'{project_path()}/{name}', body)
    with pytest.raises(GeneratedProjectDraftRejected, match='generated_acceptance_python_declaration_invalid') as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert caught.value.rejection.issues == ('python_lock_fixed_versions_required',)
    assert repo.updates == 0


@pytest.mark.parametrize('package', ['not json', '{}', '{"scripts":{"build":"","test":"node --test"}}',
    '{"scripts":{"build":"node -v","test":"node --test"},"workspaces":[]}',
    '{"scripts":{"build":"node -v","test":"node --test"},"devEngines":{}}'])
async def test_invalid_node_declaration_remains_repairable_before_freeze(tmp_path: Path, package: str) -> None:
    service, repo = setup(tmp_path)
    write_file(service.root, f'{project_path()}/package.json', package)
    with pytest.raises(GeneratedProjectError, match='generated_acceptance_node_declaration_invalid'):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert repo.updates == 0
    write_file(service.root, f'{project_path()}/package.json', '{"scripts":{"build":"node -v","test":"node --test"}}')
    assert await service.prepare(TENANT, RUN, 'engineer', user_id=USER) is not None


async def test_protected_editable_manifest_is_typed_before_any_cas(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    body = json.loads(manifest())
    body['editable_paths'] = ['src', 'package.json']
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', json.dumps(body))
    with pytest.raises(GeneratedProjectDraftRejected) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    value = caught.value.rejection
    assert value.reason == 'generated_acceptance_manifest_invalid'
    assert value.issues == ('protected_editable_paths',)
    assert value.tenant_id == str(TENANT) and value.user_id == str(USER) and value.run_id == str(RUN)
    assert value.proposer_actor == 'engineer'
    assert len(value.observation_sha256) == 64 and repo.updates == 0
    assert not (service.root / '.project-contracts').exists()


async def rejection_for(service: GeneratedProjectService) -> GeneratedProjectDraftRejected:
    with pytest.raises(GeneratedProjectDraftRejected) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    return caught.value


@pytest.mark.parametrize('change,reason,issue', [
    ('missing', 'manifest_required', 'manifest_missing'),
    ('json', 'manifest_invalid', 'manifest_invalid'),
    ('schema', 'manifest_invalid', 'manifest_invalid'),
    ('test', 'scaffold_incomplete', 'scaffold_incomplete'),
    ('node', 'node_declaration_invalid', 'node_declaration_invalid'),
    ('protected_test', 'manifest_invalid', 'protected_test_files'),
])
async def test_known_draft_rejections_are_bound_and_do_not_capture(
    tmp_path: Path, change: str, reason: str, issue: str,
) -> None:
    service, repo = setup(tmp_path)
    if change == 'missing':
        (service.root / project_path() / 'ACCEPTANCE.json').unlink()
    elif change in {'json', 'schema'}:
        write_file(service.root, f'{project_path()}/ACCEPTANCE.json',
                   'raw private invalid JSON' if change == 'json' else '{"schema_version":true}')
    elif change == 'test':
        (service.root / project_path() / 'test/business.test.cjs').unlink()
    elif change == 'node':
        write_file(service.root, f'{project_path()}/package.json', '{"scripts":{}}')
    else:
        body = json.loads(manifest())
        body['test_files'] = ['package.json']
        write_file(service.root, f'{project_path()}/ACCEPTANCE.json', json.dumps(body))
    first = (await rejection_for(service)).rejection
    second = (await rejection_for(service)).rejection
    assert first == second
    assert first.reason == 'generated_acceptance_' + reason and first.issues == (issue,)
    assert first.project_id == 'project-a' and first.session_id == 'session-a'
    assert first.request_sha256 == hashlib.sha256(repo.row.request.encode()).hexdigest()
    assert repo.updates == 0 and 'generated_project_preparation' not in repo.row.routing_decision
    assert not (service.root / '.project-contracts').exists()
    assert 'private' not in json.dumps(first.to_payload())
    write_file(service.root, f'{project_path()}/src/index.js', 'changed observation')
    assert (await rejection_for(service)).rejection.observation_sha256 != first.observation_sha256


async def test_manifest_bytes_change_digest_even_when_both_invalid(tmp_path: Path) -> None:
    service, _ = setup(tmp_path)
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', 'invalid one')
    first = (await rejection_for(service)).rejection
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', 'invalid two')
    assert (await rejection_for(service)).rejection.observation_sha256 != first.observation_sha256


@pytest.mark.parametrize('control', ['', 'capture-intent.json', 'baseline-partial.json',
    'baseline.json', 'generated-acceptance.json', 'unknown-control'])
async def test_any_existing_run_control_namespace_is_fatal(tmp_path: Path, control: str) -> None:
    service, repo = setup(tmp_path)
    invocation = generated_authority(repo.row, TENANT, RUN).invocation
    with ExitStack() as stack:
        controls = ProjectBaselineStore(service.root)._controls(invocation, stack, create=True)
        if control:
            ProjectBaselineStore._write_once(controls, control, b'{}')
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', 'invalid')
    with pytest.raises(GeneratedProjectError) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert type(caught.value) is GeneratedProjectError and repo.updates == 0


@pytest.mark.parametrize('phase', ['capturing', 'captured', 'python_ready', 'UNKNOWN', None])
async def test_existing_persisted_state_never_becomes_draft_feedback(tmp_path: Path, phase: str | None) -> None:
    service, repo = setup(tmp_path)
    repo.row.routing_decision['generated_project_preparation'] = (
        {'schema_version': 1, 'phase': phase} if phase is not None else None)
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', 'invalid')
    with pytest.raises(GeneratedProjectError) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert type(caught.value) is GeneratedProjectError and repo.updates == 0


@pytest.mark.parametrize('change', ['lease', 'worker', 'expired', 'status', 'user', 'request',
    'scope', 'state', 'unknown', 'controls', 'input'])
async def test_rejection_rechecks_authority_and_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    service, repo = setup(tmp_path)
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', 'invalid')
    original = service._draft_observation
    calls = 0

    async def changed_observation(invocation: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == 'input':
                write_file(service.root, f'{project_path()}/src/index.js', 'changed')
            elif change == 'lease':
                repo.row.worker_lease_token = uuid4()
            elif change == 'worker':
                repo.row.worker_id = 'different-worker'
            elif change == 'expired':
                repo.row.worker_lease_expires_at = datetime(2000, 1, 1, tzinfo=UTC)
            elif change == 'status':
                repo.row.status = RunStatus.CANCELLED
            elif change == 'user':
                repo.row.actor_id = uuid4()
            elif change == 'request':
                repo.row.request = 'different request'
            elif change == 'scope':
                repo.row.routing_decision['project_id'] = 'different-project'
            elif change == 'state':
                repo.row.routing_decision['generated_project_preparation'] = {'phase': 'capturing'}
            elif change == 'unknown':
                repo.row.routing_decision['checkpoint'] = {'outcome': 'UNKNOWN'}
            elif change == 'controls':
                with ExitStack() as stack:
                    ProjectBaselineStore(service.root)._controls(
                        generated_authority(repo.row, TENANT, RUN).invocation, stack, create=True)
        return await original(invocation)  # type: ignore[arg-type]

    monkeypatch.setattr(service, '_draft_observation', changed_observation)
    with pytest.raises(GeneratedProjectError) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert type(caught.value) is GeneratedProjectError and repo.updates == 0


@pytest.mark.parametrize('failure', [PermissionError('secret'), OSError('secret'),
    ValueError('filesystem validation failed'), RuntimeError('secret')])
@pytest.mark.parametrize('site', ['manifest', 'source', 'controls', 'database'])
async def test_arbitrary_observation_errors_are_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception, site: str,
) -> None:
    from agent_hub.capabilities import generated_project_service as module

    service, repo = setup(tmp_path)
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', 'invalid')

    def broken(*args: object, **kwargs: object) -> object:
        raise failure

    async def broken_async(*args: object, **kwargs: object) -> object:
        raise failure

    if site == 'manifest':
        monkeypatch.setattr(service, '_manifest', broken)
    elif site == 'source':
        monkeypatch.setattr(module, 'project_source_manifest', broken_async)
    elif site == 'controls':
        monkeypatch.setattr(service, '_controls_absent', broken)
    else:
        monkeypatch.setattr(repo, 'get', broken_async)
    with pytest.raises(GeneratedProjectError) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert type(caught.value) is GeneratedProjectError and repo.updates == 0
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('failure', [ValueError('secret'), OSError('secret'), RuntimeError('secret')])
async def test_cas_errors_remain_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    service, repo = setup(tmp_path)

    async def broken(*args: object, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(repo, 'compare_and_set_generated_preparation', broken)
    with pytest.raises(GeneratedProjectError) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert type(caught.value) is GeneratedProjectError


async def test_capture_failure_after_cas_never_becomes_draft_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo = setup(tmp_path)

    async def broken(*args: object, **kwargs: object) -> object:
        raise ValueError('capture is uncertain')

    monkeypatch.setattr(ProjectBaselineStore, 'capture_generated_once', broken)
    with pytest.raises(GeneratedProjectError) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert type(caught.value) is GeneratedProjectError
    assert repo.updates == 1 and repo.row.routing_decision['generated_project_preparation']['phase'] == 'capturing'


@pytest.mark.parametrize('target', ['ACCEPTANCE.json', 'package.json', 'src/index.js'])
async def test_hardlinked_source_is_not_correctable(tmp_path: Path, target: str) -> None:
    service, repo = setup(tmp_path)
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', 'invalid')
    os.link(service.root / project_path() / target, tmp_path / 'alias')
    with pytest.raises(GeneratedProjectError) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert type(caught.value) is GeneratedProjectError and repo.updates == 0


async def test_symlinked_control_namespace_is_not_absence(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', 'invalid')
    try:
        os.symlink(tmp_path / 'missing-target', service.root / '.project-contracts', target_is_directory=True)
    except OSError:
        pytest.skip('native symlink privilege unavailable')
    with pytest.raises(GeneratedProjectError) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert type(caught.value) is GeneratedProjectError and repo.updates == 0


async def test_other_run_controls_do_not_block_new_draft(tmp_path: Path) -> None:
    from dataclasses import replace

    service, repo = setup(tmp_path)
    authority = replace(generated_authority(repo.row, TENANT, RUN), run_id=uuid4())
    with ExitStack() as stack:
        ProjectBaselineStore(service.root)._controls(authority.invocation, stack, create=True)
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', 'invalid')
    assert (await rejection_for(service)).rejection.reason == 'generated_acceptance_manifest_invalid'
    assert repo.updates == 0


async def test_typed_error_revalidates_exact_rejection() -> None:
    from tests.unit.capabilities.test_generated_project_draft import rejection

    item = rejection()
    object.__setattr__(item, 'execution_started', True)
    with pytest.raises(ValueError, match='invalid generated draft rejection'):
        GeneratedProjectDraftRejected(item)
    with pytest.raises(ValueError, match='invalid generated draft rejection'):
        GeneratedProjectDraftRejected(rejection().to_payload())  # type: ignore[arg-type]


@pytest.mark.parametrize('failure', [PermissionError('secret'), FileNotFoundError('secret'), OSError('secret'),
    ValueError('untrusted filesystem')])
async def test_declaration_file_read_errors_are_not_pure_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception,
) -> None:
    from agent_hub.capabilities import generated_project_service as module

    service, repo = setup(tmp_path)
    original = _read_no_links

    def broken(root: Path, parts: tuple[str, ...], *, max_bytes: int) -> bytes:
        if parts[-1] == 'package.json':
            raise failure
        return original(root, parts, max_bytes=max_bytes)

    monkeypatch.setattr(module, '_read_no_links', broken)
    with pytest.raises(GeneratedProjectError) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert type(caught.value) is GeneratedProjectError and repo.updates == 0


async def test_missing_manifest_digest_binds_actual_source_manifest(tmp_path: Path) -> None:
    from agent_hub.skills.sandbox.project import project_source_manifest

    service, repo = setup(tmp_path)
    (service.root / project_path() / 'ACCEPTANCE.json').unlink()
    files = await project_source_manifest(service.root, generated_authority(repo.row, TENANT, RUN).invocation)
    expected = hashlib.sha256(json.dumps({'schema_version': 1, 'manifest': {'missing': True},
        'source_files': files}, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
    assert (await rejection_for(service)).rejection.observation_sha256 == expected


async def test_corrected_protected_editable_draft_gets_full_normal_capture(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    body = json.loads(manifest())
    body['editable_paths'] = ['src', 'package.json']
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', json.dumps(body))
    rejected = (await rejection_for(service)).rejection
    assert repo.updates == 0
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', manifest().decode())
    captured = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert captured is not None and repo.updates == 2
    assert captured.envelope.manifest.editable_paths == ('src',)
    assert captured.envelope.request_sha256 == rejected.request_sha256
    assert 'package.json' in captured.stored.contract.protected_files
