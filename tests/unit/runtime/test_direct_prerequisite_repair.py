from __future__ import annotations

import json
import time
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_hub.capabilities.project_prerequisites import validate_project_prerequisite_report
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.runtime.action_direct import _binding
from agent_hub.runtime.contracts import EventKind, TaskContext
from agent_hub.runtime.direct import RuntimeExecutionError, _manifest_prerequisite_repair
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_fingerprint
from agent_hub.skills.sandbox.project_runner import Blocked, _manifest
from tests.unit.capabilities.test_project_prerequisites import raw_report
from tests.unit.runtime.test_direct_project_verification import (
    BROKEN,
    FIXED,
    Model,
    batch,
    diagnostic_context,
    diagnostic_read_batch,
    runner,
)


def repair_context(**route: Any) -> TaskContext:
    ctx = diagnostic_context()
    return ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'engineering_prerequisite_repair_version': 1, **route,
    }})


def manifest_backend(backend: Any, ctx: TaskContext, monkeypatch: pytest.MonkeyPatch,
                     *, reason: str | None = None) -> None:
    original = backend.execute

    async def execute(*, name: str, **kwargs: Any) -> Any:
        if name == 'project.verify':
            try:
                if reason is not None:
                    raise Blocked(reason)
                _manifest(backend.root, time.monotonic() + 5)
            except Blocked as error:
                backend.calls.append(name)
                source = await project_source_fingerprint(backend.workspace_root, ProjectInvocation(
                    execution_id='fixture', tenant_id=ctx.tenant_id, run_id=ctx.run_id,
                    project_id='direct-verification', session_id='same-task',
                    sandbox_profile='workspace_write', timeout_seconds=60,
                ))
                raw = raw_report(reason=str(error))
                raw['source_sha256'] = source
                raise RuntimeCapabilityError(
                    'project verification blocked by execution prerequisites',
                    project_prerequisite=validate_project_prerequisite_report(raw, source),
                ) from None
        return await original(name=name, **kwargs)

    monkeypatch.setattr(backend, 'execute', execute)


async def test_direct_repairs_manifest_after_source_read_before_real_child_and_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = repair_context()
    fixed_manifest = json.dumps({'scripts': {'build': 'node -v', 'test': 'node --test'}})
    model = Model(batch({'package.json': '{broken', 'main.py': FIXED}),
                  diagnostic_read_batch(path='package.json'), batch({'package.json': fixed_manifest}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    manifest_backend(backend, ctx, monkeypatch)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert backend.calls.count('project.verify') == 2
    assert backend.calls.index('read_context') > backend.calls.index('project.verify')
    assert len(backend.process_results) == 1 and backend.process_results[0].exit_code == 0
    assert len(model.requests) == 3
    for request in model.requests[1:]:
        user = '\n'.join(message.content for message in request.messages if message.role == 'user')
        assert 'UNTRUSTED_PROJECT_PREREQUISITE_JSON' in user
        assert 'invalid_package_manifest' in user
        assert 'no installation or successful verification' in user
        assert not any('verification_blocked' in message.content for message in request.messages
                       if message.role == 'system')
    assert shared.results[0].status == 'failed'
    assert 'verification_blocked' in shared.results[0].payload
    archives = list((tmp_path / 'generated').rglob('*.zip'))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.read('package.json').decode() == fixed_manifest


async def test_unchanged_manifest_observation_cannot_retry_verify_or_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = repair_context()
    model = Model(batch({'package.json': '{broken', 'main.py': FIXED}),
                  diagnostic_read_batch(path='package.json'), batch({'package.json': '{broken'}))
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    manifest_backend(backend, ctx, monkeypatch)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 3
    assert backend.calls.count('project.verify') == 1
    assert 'workspace.bundle' not in backend.calls


@pytest.mark.parametrize('marker', [None, True, '1', 2])
async def test_old_or_malformed_policy_keeps_original_blocked_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: Any,
) -> None:
    ctx = repair_context(engineering_prerequisite_repair_version=marker)
    model = Model(batch({'package.json': '{broken', 'main.py': FIXED}))
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    manifest_backend(backend, ctx, monkeypatch)
    with pytest.raises(RuntimeExecutionError) as caught:
        _ = [event async for event in runtime.run(ctx)]
    assert caught.value.project_prerequisite_payload['reason'] == 'invalid_package_manifest'
    assert len(model.requests) == 1 and backend.calls.count('project.verify') == 1


@pytest.mark.parametrize('reason', [
    'node_runtime_missing', 'install_authorization_required', 'runtime_access_denied',
    'node_runtime_untrusted', 'cleanup_deadline_exceeded', 'deadline_exceeded',
])
async def test_other_prerequisites_do_not_gain_generic_retry_or_install_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    ctx = repair_context()
    model = Model(batch({'package.json': '{broken', 'main.py': FIXED}))
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    manifest_backend(backend, ctx, monkeypatch, reason=reason)
    with pytest.raises(RuntimeExecutionError) as caught:
        _ = [event async for event in runtime.run(ctx)]
    report = caught.value.project_prerequisite_payload
    assert isinstance(report, Mapping) and report['reason'] == reason
    assert len(model.requests) == 1 and 'workspace.bundle' not in backend.calls


@pytest.mark.parametrize('marker', [None, True, '1', 2, 0])
def test_action_binding_freezes_manifest_remediation_policy(marker: Any) -> None:
    ctx = repair_context()
    route = dict(ctx.routing_decision)
    if marker is None:
        route.pop('engineering_prerequisite_repair_version')
    else:
        route['engineering_prerequisite_repair_version'] = marker
    assert _binding(ctx, 'main') != _binding(ctx.model_copy(update={'routing_decision': route}), 'main')


@pytest.mark.parametrize('key', [
    'project_input', 'project_input_contract_version', 'project_input_preparation',
    'generated_project_contract_version', 'generated_project_preparation',
    'python_project_history', 'python_input_supply', 'toolchain_supply',
])
def test_presence_of_any_frozen_contract_excludes_plain_manifest_repair(key: str) -> None:
    assert _manifest_prerequisite_repair(repair_context())
    assert not _manifest_prerequisite_repair(repair_context(**{key: None}))


@pytest.mark.parametrize('fault', ['no_read', 'truncated', 'changed_read', 'other_file'])
async def test_manifest_repair_needs_complete_current_read_and_cannot_change_other_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    ctx = repair_context()
    fixed_manifest = '{"scripts":{"build":"node -v","test":"node --test"}}'
    texts = [batch({'package.json': '{broken', 'main.py': FIXED})]
    if fault != 'no_read':
        texts.append(diagnostic_read_batch(path='package.json'))
    texts.append(batch({'main.py': 'changed'} if fault == 'other_file'
                       else {'package.json': fixed_manifest}))
    model = Model(*texts)
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    manifest_backend(backend, ctx, monkeypatch)
    execute = backend.execute

    async def read(*, name: str, **kwargs: Any) -> Any:
        result = await execute(name=name, **kwargs)
        if name == 'read_context' and fault in {'truncated', 'changed_read'}:
            return {**result, **({'truncated': True} if fault == 'truncated' else {'text': 'different'})}
        return result

    monkeypatch.setattr(backend, 'execute', read)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(ctx)]
    assert backend.calls.count('project.verify') == 1
    assert (backend.root / 'package.json').read_text() == '{broken'
    assert (backend.root / 'main.py').read_text() == FIXED
    assert 'workspace.bundle' not in backend.calls


async def test_second_manifest_rejection_stops_even_after_a_different_invalid_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = repair_context()
    model = Model(batch({'package.json': '{broken', 'main.py': FIXED}),
                  diagnostic_read_batch(path='package.json'), batch({'package.json': '{still-broken'}))
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    manifest_backend(backend, ctx, monkeypatch)
    with pytest.raises(RuntimeExecutionError) as caught:
        _ = [event async for event in runtime.run(ctx)]
    assert caught.value.project_prerequisite_payload['reason'] == 'invalid_package_manifest'
    assert backend.calls.count('project.verify') == 2 and len(model.requests) == 3
    assert not backend.process_results and 'workspace.bundle' not in backend.calls


async def test_execution_failure_cannot_reopen_manifest_repair_using_an_old_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = repair_context()
    valid = '{"scripts":{"build":"node -v","test":"node --test"}}'
    model = Model(batch({'package.json': '{broken', 'main.py': BROKEN}),
                  diagnostic_read_batch(path='package.json'), batch({'package.json': valid}),
                  batch({'package.json': '{different-broken'}), batch({'package.json': valid}))
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    manifest_backend(backend, ctx, monkeypatch)
    with pytest.raises(RuntimeExecutionError) as caught:
        _ = [event async for event in runtime.run(ctx)]
    report = getattr(caught.value, 'project_prerequisite_payload', None)
    assert isinstance(report, Mapping) and report['reason'] == 'invalid_package_manifest'
    assert len(model.requests) == 4 and backend.calls.count('project.verify') == 3
    assert len(backend.process_results) == 1 and backend.process_results[0].exit_code == 3
    assert (backend.root / 'package.json').read_text() == '{different-broken'
    assert 'workspace.bundle' not in backend.calls
