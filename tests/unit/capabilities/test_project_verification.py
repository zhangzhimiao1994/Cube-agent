from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from agent_hub.capabilities.project_verification import ProjectVerificationError, _validate_report
from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ProjectInvocation
from tests.unit.capabilities.test_scoped_read import (
    RUN,
    TENANT,
    FakeRunRepository,
    project_path,
    stored_run,
    write_file,
)


class Executor:
    def __init__(self, *, failed: bool = False, mutate: Path | None = None) -> None:
        self.failed = failed
        self.mutate = mutate
        self.calls: list[ProjectInvocation] = []
        self.root = Path('.')

    async def run(self, invocation: ProjectInvocation) -> SkillResult:
        from agent_hub.skills.sandbox.project import project_source_fingerprint
        self.calls.append(invocation)
        fingerprint = await project_source_fingerprint(self.root, invocation)
        if self.mutate:
            self.mutate.write_text('changed', encoding='utf-8')
        report = {
            'schema_version': 1, 'status': 'failed' if self.failed else 'passed',
            'source_sha256': fingerprint, 'runtime': {'node': 'v22.12.0', 'npm': '10.9.0'},
            'checks': [
                {'phase': phase, 'status': 'failed' if self.failed and phase == 'test' else 'passed',
                 'exit_code': 1 if self.failed and phase == 'test' else 0, 'timed_out': False,
                 'stdout': '', 'stderr': 'AssertionError: expected total 12, got 10\nAPI_KEY=SECRET'
                 if self.failed and phase == 'test' else ''}
                for phase in ('install', 'build', 'test')
            ],
        }
        return SkillResult(exit_code=0, timed_out=False, stdout=json.dumps(report), stderr='')


def gateway(tmp_path: Path, executor: Executor, **routing: object) -> RuntimeCapabilityGateway:
    root = tmp_path / 'projects'
    executor.root = root
    write_file(root, f'{project_path()}/package.json', '{"scripts":{"build":"node -v","test":"node --test"}}')
    record = stored_run(
        requested_permissions=['workspace.read', 'workspace.write', 'command.run', 'network.read'],
        execution_backend='systemd', runtime_timeout_seconds=180,
    )
    record = replace(record, routing_decision={**(record.routing_decision or {}), **routing})
    return RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / 'skills', project_workspace_dir=root,
        generated_artifact_dir=tmp_path / 'artifacts',
        run_repository=FakeRunRepository(record),
        project_executor=executor,
    )


async def verify(backend: RuntimeCapabilityGateway) -> Mapping[str, JsonValue]:
    return await backend.execute(tenant_id=TENANT, run_id=RUN, actor='engineer',
                                 name='project.verify', arguments={}, idempotency_key='verify-project')


async def test_verify_binds_authoritative_scope_and_returns_real_phase_receipts(tmp_path: Path) -> None:
    executor = Executor()
    result = await verify(gateway(tmp_path, executor))
    receipt = cast(Mapping[str, JsonValue], result['verification'])
    assert receipt['status'] == 'passed'
    assert len(executor.calls) == 1
    invocation = executor.calls[0]
    assert invocation.tenant_id == TENANT
    assert invocation.run_id == RUN
    assert invocation.project_id == 'project-a'
    assert invocation.session_id == 'session-a'
    assert invocation.install_allowed is True
    assert invocation.timeout_seconds <= 180
    assert backend_check_names(receipt) == ('install', 'build', 'test')


def backend_check_names(receipt: Mapping[str, JsonValue]) -> tuple[JsonValue, ...]:
    return tuple(item['phase'] for item in cast(tuple[Mapping[str, JsonValue], ...], receipt['checks']))


async def test_failed_check_is_typed_redacted_process_evidence_not_success(tmp_path: Path) -> None:
    executor = Executor(failed=True)
    with pytest.raises(RuntimeCapabilityError) as caught:
        await verify(gateway(tmp_path, executor))
    diagnostic = caught.value.execution_diagnostic
    assert diagnostic is not None
    payload = diagnostic.to_payload()
    assert payload['exit_code'] == 1
    assert 'AssertionError' in str(payload)
    assert 'SECRET' not in str(payload)
    assert len(executor.calls) == 1


@pytest.mark.parametrize('field,value', [
    ('requested_permissions', ['workspace.read']), ('sandbox_profile', 'read_only'),
    ('execution_backend', 'unknown'), ('project_id', '../escape'),
])
async def test_verify_rejects_scope_and_authority_before_executor(
    tmp_path: Path, field: str, value: object,
) -> None:
    executor = Executor()
    backend = gateway(tmp_path, executor, **{field: value})
    with pytest.raises(RuntimeCapabilityError):
        await verify(backend)
    assert executor.calls == []


async def test_concurrent_source_change_invalidates_verification(tmp_path: Path) -> None:
    executor = Executor()
    backend = gateway(tmp_path, executor)
    executor.mutate = tmp_path / 'projects' / project_path() / 'package.json'
    with pytest.raises(RuntimeCapabilityError, match='project source changed during verification'):
        await verify(backend)


async def test_bundle_requires_fresh_successful_receipt(tmp_path: Path) -> None:
    executor = Executor()
    backend = gateway(tmp_path, executor)

    async def bundle() -> Mapping[str, JsonValue]:
        return await backend.execute(tenant_id=TENANT, run_id=RUN, actor='engineer',
            name='workspace.bundle', arguments={}, idempotency_key='bundle-verified')

    with pytest.raises(RuntimeCapabilityError, match='project verification required'):
        await bundle()
    await verify(backend)
    delivered = await bundle()
    assert 'artifact_id' in delivered
    assert cast(Mapping[str, JsonValue], delivered['file'])['mime_type'] == 'application/zip'
    write_file(tmp_path / 'projects', f'{project_path()}/changed.js', 'invalid program')
    with pytest.raises(RuntimeCapabilityError, match='project verification required'):
        await bundle()


async def test_inline_node_zip_cannot_bypass_workspace_verification(tmp_path: Path) -> None:
    backend = gateway(tmp_path, Executor())
    with pytest.raises(RuntimeCapabilityError, match='workspace.write_text'):
        await backend.execute(tenant_id=TENANT, run_id=RUN, actor='engineer',
            name='project.generate_zip', arguments={
                'title': 'Unverified', 'files': {'package.json': '{}'},
            }, idempotency_key='unverified-inline')


async def test_zip_bytes_are_verified_even_if_source_changes_after_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.files.workspace import ProjectWorkspaceStore
    backend = gateway(tmp_path, Executor())
    await verify(backend)
    original = ProjectWorkspaceStore.create_session_zip

    def changed(store: ProjectWorkspaceStore, *args: object, **kwargs: object) -> object:
        write_file(tmp_path / 'projects', f'{project_path()}/late.js', 'changed after receipt check')
        return original(store, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ProjectWorkspaceStore, 'create_session_zip', changed)
    with pytest.raises(RuntimeCapabilityError, match='bundle does not match verified source'):
        await backend.execute(tenant_id=TENANT, run_id=RUN, actor='engineer', name='workspace.bundle',
            arguments={}, idempotency_key='raced-bundle')
    assert not list((tmp_path / 'artifacts').rglob('*.zip'))


@pytest.mark.parametrize('failed_phase', ['install', 'build'])
def test_early_failure_preserves_diagnostic_when_later_phases_were_not_run(failed_phase: str) -> None:
    stopped = False
    checks = []
    for phase in ('install', 'build', 'test'):
        failed = phase == failed_phase
        checks.append({'phase': phase, 'status': 'not_run' if stopped else 'failed' if failed else 'passed',
            'exit_code': None if stopped else 2 if failed else 0, 'timed_out': False,
            'stdout': '', 'stderr': 'actual diagnostic' if failed else ''})
        stopped = stopped or failed
    with pytest.raises(ProjectVerificationError, match=f'project {failed_phase} failed') as caught:
        _validate_report(json.dumps({'schema_version': 1, 'source_sha256': 'a' * 64,
            'status': 'failed', 'runtime': {}, 'checks': checks}), expected_source='a' * 64)
    assert caught.value.diagnostic is not None
    assert caught.value.diagnostic.to_payload()['exit_code'] == 2
