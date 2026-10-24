"""Root Go backend contracts; injected OS boundaries are not native Go proof."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.capabilities.go_project_inputs import bind_go_project_inputs
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.go_project_protocol import GoProjectInvocation, GoProjectRequest
from agent_hub.skills.sandbox.project import OriginalProjectReference
from agent_hub.skills.sandbox.python_project_process import PythonPhaseProcessResult
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainArtifact, ToolchainCatalog
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.capabilities.test_go_project_acceptance import manifest
from tests.unit.capabilities.test_go_project_inputs import case
from tests.unit.capabilities.test_go_project_receipts import _receipt

MODULE = 'agent_hub.skills.sandbox.go_project_backend'


def test_backend_exists() -> None:
    assert importlib.util.find_spec(MODULE) is not None, 'Root Go diagnostic backend missing'


@pytest.fixture
def api() -> Any:
    if importlib.util.find_spec(MODULE) is None:
        pytest.skip('backend missing; existence test is RED')
    return importlib.import_module(MODULE)


@pytest.fixture
def prepared(tmp_path: Path, request: pytest.FixtureRequest) -> Any:
    changes = {
        'assets/readme.txt': b'full source, including inactive and non-Go files\n',
        'inactive_windows.go': b'package main\n',
    }
    if getattr(request, 'param', False):
        acceptance = replace(manifest(), editable_paths=('main.go', 'assets'))
        changes['ACCEPTANCE.json'] = json.dumps(acceptance.to_payload()).encode()
    fixture = case(tmp_path / 'original', editable=('main.go', 'assets'), changes=changes)
    artifact = ToolchainArtifact(stack='go', version='1.27.3', platform='linux_amd64',
        url='https://dl.google.com/go/go1.27.3.linux-amd64.tar.gz', sha256='b' * 64,
        archive_bytes=1024, unpacked_bytes_limit=8192, archive_entries_limit=100,
        directory_entries_limit=100, tar_bytes_limit=20480)
    catalog = ToolchainCatalog(runtime_root='/opt/go-runtime', stage_root='/var/lib/go-stages',
        state_root='/var/lib/go-state', min_free_bytes=3 * 1024**3, artifacts=(artifact,))
    sdk = fixture.sdk.model_copy(update={'artifact_sha256': artifact.artifact_sha256})
    inputs = bind_go_project_inputs(stored=fixture.stored, scope=fixture.scope,
        toolchain=sdk, source_limit_bytes=1024**2, file_limit=100)
    inv = GoProjectInvocation(execution_id='go_backend', tenant_id=inputs.scope.tenant_id,
        run_id=inputs.scope.run_id, project_id=inputs.scope.project_id,
        session_id=inputs.scope.session_id, sandbox_profile='workspace_write',
        source_kind='original', timeout_seconds=60, scope=inputs.scope,
        original_reference=OriginalProjectReference.model_validate(inputs.original_reference.to_payload()),
        toolchain=sdk, recipe_sha256=inputs.sha256,
        environment_input_sha256=inputs.environment_input_sha256,
        source_limit_bytes=inputs.source_limit_bytes, file_limit=inputs.file_limit,
        output_limit_bytes=2 * 1024**2)
    policy = replace(BrokerPolicy(), toolchain_runtime_root=Path(catalog.stage_root))
    broker_request = GoProjectRequest(action='run_go_project', invocation=inv)
    return SimpleNamespace(fixture=fixture, inputs=inputs, invocation=inv, request=broker_request,
        catalog=catalog, artifact=artifact, policy=policy,
        stage=Path(catalog.stage_root) / 'one')


def report(value: Any) -> dict[str, Any]:
    inv, inputs = value.invocation, value.inputs
    return {'schema_version': 1, 'kind': 'go_project', 'evidence': 'native_process_only',
        'status': 'incomplete', 'execution_id': inv.execution_id, 'scope': inv.scope.to_payload(),
        'source_kind': inv.source_kind, 'source_sha256': inv.scope.source_sha256,
        'recipe_sha256': inputs.sha256, 'environment_input_sha256': inputs.environment_input_sha256,
        'original_reference': inputs.original_reference.to_payload(),
        'original_check_contract': inputs.contract.protected_sha256,
        'toolchain': inputs.toolchain.model_dump(mode='json'),
        'toolchain_measurement': 'not_run', 'checks': [], 'canary': 'not_run',
        'business_gate': 'not_run', 'cache_reuse': 'none'}


def process(payload: dict[str, Any] | str, **changes: Any) -> PythonPhaseProcessResult:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=True) + '\n'
    return replace(PythonPhaseProcessResult(0, False, text, '', False, False,
        len(text.encode('utf-8')), 0), **changes)


def test_authority_is_required_and_no_cli_is_exposed(api: Any, prepared: Any) -> None:
    with pytest.raises(TypeError, match='authorize'):
        api.execute_go_project_request(prepared.request, policy=prepared.policy,
            catalog=prepared.catalog, stage=prepared.stage)
    assert not hasattr(api, 'main')


def test_non_native_fails_before_io(api: Any, prepared: Any) -> None:
    if sys.platform == 'linux':
        pytest.skip('non-Linux prerequisite only')
    response = api.execute_go_project_request(prepared.request, policy=prepared.policy,
        catalog=prepared.catalog, stage=prepared.stage, authorize=lambda *_: None)
    assert not response.ok and response.error == 'go_project_unsupported'
    response.require_for(prepared.request)


def test_wrong_envelope_cannot_reach_authority(api: Any, prepared: Any) -> None:
    with pytest.raises(ValueError, match='go_project_request_invalid'):
        api.execute_go_project_request({'version': 7}, policy=prepared.policy,
            catalog=prepared.catalog, stage=prepared.stage, authorize=lambda *_: pytest.fail())


def test_report_keeps_diagnostic_status_and_runner_measurement(api: Any, prepared: Any) -> None:
    payload = report(prepared)
    assert api._report(process(payload), prepared.invocation, prepared.inputs,
                       prepared.inputs.scope.source_sha256) == payload
    payload.update(status='failed', failed_phase='build',
        checks=[{'phase': 'build', 'status': 'failed', 'error': 'go_project_build_failed'}])
    assert api._report(process(payload), prepared.invocation, prepared.inputs,
                       prepared.inputs.scope.source_sha256) == payload


@pytest.mark.parametrize('field,value', [
    ('status', 'passed'), ('status', 'completed'), ('business_gate', 'passed'),
    ('toolchain_measurement', 'completed'), ('cache_reuse', 'hit'),
    ('schema_version', True), ('source_kind', 'candidate'), ('kind', 'python_project'),
    ('evidence', 'business_receipt'), ('source_sha256', '9' * 64),
    ('recipe_sha256', '9' * 64), ('environment_input_sha256', '9' * 64),
    ('original_check_contract', '9' * 64), ('execution_id', 'other'),
    ('scope', {}), ('toolchain', {}), ('original_reference', {}),
])
def test_report_rejects_identity_or_business_rebinding(
    api: Any, prepared: Any, field: str, value: object,
) -> None:
    with pytest.raises(ValueError):
        api._report(process(report(prepared) | {field: value}), prepared.invocation, prepared.inputs,
                    prepared.inputs.scope.source_sha256)


@pytest.mark.parametrize('damage', ['duplicate', 'nested', 'nan', 'infinity', 'exponent'])
def test_report_rejects_ambiguous_json(api: Any, prepared: Any, damage: str) -> None:
    raw = json.dumps(report(prepared))
    if damage == 'duplicate':
        raw = raw[:-1] + ',"status":"incomplete"}'
    elif damage == 'nested':
        raw = raw.replace('"checks": []', '"checks": [{"phase":"build","phase":"test"}]')
    else:
        raw = raw[:-1] + ',"unused":' + {'nan': 'NaN', 'infinity': 'Infinity',
                                        'exponent': '1e9999'}[damage] + '}'
    with pytest.raises(ValueError):
        api._report(process(raw), prepared.invocation, prepared.inputs,
                    prepared.inputs.scope.source_sha256)


@pytest.mark.parametrize('changes', [
    {'stdout_truncated': True}, {'stderr_truncated': True}, {'timed_out': True},
    {'stdout_observed_bytes': 0}, {'stderr_observed_bytes': 1}, {'exit_code': None},
    {'exit_code': 1}, {'exit_code': False}, {'stdout_observed_bytes': True},
    {'stderr': '\ud800'}, {'stdout_observed_bytes': 32 * 1024**2 + 1},
])
def test_report_requires_complete_actual_utf8_output(api: Any, prepared: Any, changes: Any) -> None:
    with pytest.raises(ValueError):
        api._report(process(report(prepared), **changes), prepared.invocation, prepared.inputs,
                    prepared.inputs.scope.source_sha256)


def test_nested_cleanup_uncertainty_is_not_a_normal_failed_report(api: Any, prepared: Any) -> None:
    payload = report(prepared) | {'status': 'failed', 'failed_phase': 'test',
        'checks': [{'phase': 'test', 'status': 'failed', 'error': 'go_project_cleanup_incomplete'}]}
    with pytest.raises(ProcessCleanupIncomplete):
        api._report(process(payload), prepared.invocation, prepared.inputs,
                    prepared.inputs.scope.source_sha256)


@pytest.fixture
def flow(api: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace only native IO/process boundaries, retain actual recipe/plan/report code."""
    state = SimpleNamespace(events=[], authority_calls=0, fault=None, pins=0,
                            candidate=None, candidate_digest=None, payload=None)
    monkeypatch.setattr(api, '_native', lambda: None)

    def check() -> None:
        state.pins += 1
        if state.fault == 'pins' and state.pins >= 3:
            raise ValueError('private pin changed')

    @contextmanager
    def roots(*_: Any) -> Any:
        state.events.append('roots')
        yield SimpleNamespace(stage_fd=20, admission_fd=21, pins=[SimpleNamespace(check=check)])
        check()

    @contextmanager
    def admission(fd: int, deadline: float) -> Any:
        assert fd == 21 and deadline > time.monotonic()
        state.events.append('admit')
        try:
            yield
        finally:
            state.events.append('release')

    def stage(workspace: Path, inv: Any, ref: Any, target: Path, uid: int) -> Any:
        assert inv.output_limit_bytes == 1024**2  # projection for source helper only
        assert workspace == prepared.policy.project_workspace_root and target == prepared.stage
        assert ref == prepared.inputs.original_reference and uid == prepared.policy.allowed_uid
        state.events.append('stage')
        return prepared.fixture.stored

    def measured(root: Path, inv: Any, deadline: float) -> Any:
        state.events.append('source')
        assert inv == prepared.invocation
        if root == prepared.stage / 'snapshot':
            state.events.append('candidate')
            return state.candidate | ({'assets/late.txt': 'f' * 64}
                                      if state.fault == 'candidate' else {})
        return prepared.fixture.files | ({'extra.txt': 'f' * 64} if state.fault == 'source' else {})

    async def snapshot(workspace: Path, inv: Any, *, destination: Path,
                       _manifest: dict[str, str]) -> str:
        assert workspace == prepared.policy.project_workspace_root
        assert inv.output_limit_bytes == 1024**2
        assert inv.tenant_id == prepared.invocation.tenant_id
        assert destination == prepared.stage / 'snapshot'
        state.events.append('snapshot')
        _manifest.update(state.candidate)
        assert type(state.candidate_digest) is str
        return state.candidate_digest

    def observe(root: Path, *, artifact: Any, deadline: float) -> Any:
        state.events.append('sdk')
        assert root == Path(prepared.catalog.runtime_root) and artifact == prepared.artifact
        return prepared.inputs.toolchain.model_copy(update={'tree_sha256': '9' * 64}) \
            if state.fault == 'sdk' else prepared.inputs.toolchain

    def authorize(inputs: Any, inv: Any) -> None:
        state.authority_calls += 1
        state.events.append('authorize')
        assert inputs == prepared.inputs and inv == prepared.invocation
        if state.fault == f'authority-{state.authority_calls}':
            raise PermissionError('private revoked grant')

    def write(plan: Any, fd: int, target: Path) -> Path:
        state.events.append('plan')
        assert plan.invocation == prepared.invocation and plan.inputs == prepared.inputs.to_payload()
        state.plan = plan
        return target / 'plan.json'

    def export(fd: int, target: Path, gid: int) -> Path:
        assert gid == prepared.policy.allowed_gid
        return target / 'export'

    def launch(command: Any, **kwargs: Any) -> Any:
        state.events.append('launch')
        assert 'authorize' in state.events[:state.events.index('launch')]
        assert kwargs['capture_limit_bytes'] == 32 * 1024**2
        assert kwargs.get('pass_fds', ()) == () and kwargs['cwd'] == prepared.stage
        joined = '\n'.join(command)
        assert f'{prepared.artifact.artifact_sha256}/payload/go' in joined.replace('\\', '/')
        assert f'{prepared.artifact.sha256}/payload/go' not in joined.replace('\\', '/')
        assert '/run/agent-hub-go-work' in joined
        state.command = command
        if state.fault == 'launch':
            raise OSError('uncertain launcher')
        payload = report(prepared)
        if state.candidate is not None:
            payload['source_sha256'] = state.candidate_digest
        return process(payload | (state.payload or {}))

    def quiesce(policy: Any, job: Any) -> None:
        state.events.append('quiet')
        assert job.invocation.output_limit_bytes == prepared.invocation.output_limit_bytes
        assert job.source_sha256 == (state.candidate_digest or prepared.inputs.scope.source_sha256)
        if state.fault == 'quiet':
            raise ProcessCleanupIncomplete('go_project_cleanup_incomplete')

    monkeypatch.setattr(api, '_roots', roots)
    monkeypatch.setattr(api, '_admission', admission)
    monkeypatch.setattr(api, '_disk_budget', lambda *a: state.events.append('budget'))
    monkeypatch.setattr(api, 'stage_original_contract', stage)
    monkeypatch.setattr(api, '_source_files', measured)
    monkeypatch.setattr(api, 'snapshot_project_source', snapshot, raising=False)
    monkeypatch.setattr(api, 'observe_published_toolchain', observe)
    monkeypatch.setattr(api, 'toolchain_reference', lambda value: value)
    monkeypatch.setattr(api, '_write_plan', write)
    monkeypatch.setattr(api, '_make_export', export)
    monkeypatch.setattr(api, '_export_empty', lambda *a: state.events.append('export-empty'))
    native_command = api.build_go_project_systemd_command

    def command(inv: Any, policy: Any, **locations: Any) -> Any:
        # Synthetic staging reads a real Windows fixture; the real command builder
        # still validates the POSIX paths a native Root stage would supply.
        assert locations['original'] == prepared.fixture.stored.snapshot
        assert locations['snapshot'] == (prepared.stage / 'snapshot' if state.candidate is not None
                                        else prepared.fixture.stored.snapshot)
        return native_command(inv, policy, **(locations | {
            'snapshot': prepared.stage / ('snapshot' if state.candidate is not None
                                          else 'original_snapshot'),
            'original': prepared.stage / 'original_snapshot'}))

    monkeypatch.setattr(api, 'build_go_project_systemd_command', command)
    monkeypatch.setattr(api, 'run_python_phase', launch)
    monkeypatch.setattr(api, '_quiesce', quiesce)
    state.authorize = authorize
    state.execute = lambda request=prepared.request, authority=authorize: api.execute_go_project_request(
        request, policy=prepared.policy, catalog=prepared.catalog, stage=prepared.stage,
        authorize=authority)
    return state


def test_run_rebuilds_then_authorizes_and_remeasures_before_return(flow: Any, prepared: Any) -> None:
    response = flow.execute()
    assert response.ok and response.result.exit_code == 0
    payload = json.loads(response.result.stdout)
    assert payload['status'] == 'incomplete' and payload['business_gate'] == 'not_run'
    assert payload['toolchain_measurement'] == 'not_run'
    assert flow.authority_calls == 3
    assert flow.events.index('budget') < flow.events.index('stage')
    assert flow.events.count('sdk') >= 2 and flow.events.count('source') >= 2
    assert 'export-empty' in flow.events[flow.events.index('quiet') + 1:]
    assert flow.events[-1] == 'release'
    response.require_for(prepared.request)


def test_failure_diagnostic_retains_actual_nonzero_process_without_changing_response(
    api: Any, flow: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stderr = '{"go_project_runner_diagnostic":{"stage":"run","exception_type":"KeyError",' \
        '"identifier":"inventory"}}\n'
    actual = process('safe compiler output\n', exit_code=1, stderr=stderr,
                     stderr_observed_bytes=len(stderr.encode()))
    launch = api.run_python_phase

    def collect(*args: Any, **kwargs: Any) -> Any:
        launch(*args, **kwargs)
        return actual

    monkeypatch.setattr(api, 'run_python_phase', collect)
    response = flow.execute()
    assert not response.ok and response.error == 'go_project_execution_unavailable'
    assert response.result is response.receipt is None
    response.require_for(prepared.request)
    output = capsys.readouterr()
    assert output.out == '' and len(output.err.splitlines()) == 1
    detail = json.loads(output.err)['go_project_diagnostic']
    assert detail['stage'] == 'report' and detail['exception_type'] == 'GoProjectBackendError'
    evidence = detail['process']
    assert evidence['exit_code'] == 1 and evidence['timed_out'] is False
    assert evidence['stdout'] == actual.stdout and evidence['stderr'] == stderr
    assert evidence['stdout_observed_bytes'] == actual.stdout_observed_bytes
    assert evidence['stderr_observed_bytes'] == actual.stderr_observed_bytes
    assert evidence['stdout_truncated'] is evidence['stderr_truncated'] is False
    assert flow.events.count('launch') == flow.events.count('quiet') == 1
    assert flow.events[-1] == 'release'


@pytest.mark.parametrize('truncated', [False, True])
def test_failure_diagnostic_redacts_and_bounds_previews_with_separate_native_counters(
    api: Any, flow: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], truncated: bool,
) -> None:
    stdout = 'begin\n' + 'x' * 10000 + '\nend\napi_key=private-secret\n'
    stderr = 'token=private-secret\nuseful failure\n'
    actual = process(stdout, exit_code=1, stderr=stderr, stdout_truncated=truncated,
        stdout_observed_bytes=987654 if truncated else len(stdout.encode()),
        stderr_observed_bytes=len(stderr.encode()))
    monkeypatch.setattr(api, 'run_python_phase', lambda *a, **k: actual)
    assert not flow.execute().ok
    raw = capsys.readouterr().err
    evidence = json.loads(raw)['go_project_diagnostic']['process']
    assert 'private-secret' not in raw and len(raw.encode()) < 64 * 1024
    assert evidence['stdout_observed_bytes'] == actual.stdout_observed_bytes
    assert evidence['stderr_observed_bytes'] == actual.stderr_observed_bytes
    assert evidence['stdout_truncated'] is truncated and evidence['stderr_truncated'] is False
    assert evidence['stderr_metadata']['redacted'] is True
    assert len(evidence['stdout'].encode()) <= 4096
    if truncated:
        assert 'begin' not in evidence['stdout'] and 'end' not in evidence['stdout']
    else:
        assert evidence['stdout'].startswith('begin\n') and 'end\n' in evidence['stdout']
        assert evidence['stdout_metadata']['truncated'] is True
        assert evidence['stdout_metadata']['redacted'] is True


@pytest.mark.parametrize('target,stage', [
    ('_native', 'native'), ('_disk_budget', 'admission'),
    ('stage_original_contract', 'baseline'), ('_measured_inputs', 'inputs'),
    ('_source_snapshot', 'source'), ('_write_plan', 'plan'),
    ('build_go_project_systemd_command', 'command'), ('_report', 'report'),
])
def test_backend_failure_diagnostic_tracks_stage_and_safe_identifier(
    api: Any, flow: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    target: str, stage: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise KeyError('inventory')

    monkeypatch.setattr(api, target, fail)
    assert not flow.execute().ok
    detail = json.loads(capsys.readouterr().err)['go_project_diagnostic']
    assert detail['stage'] == stage and detail['exception_type'] == 'KeyError'
    assert detail['identifier'] == 'inventory'
    assert ('process' in detail) == (stage == 'report')
    assert ('quiet' in flow.events) == (stage == 'report')


@pytest.mark.parametrize('fault', ['launch', 'quiet', 'nested'])
@pytest.mark.parametrize('broken', [False, True])
def test_failure_diagnostic_cannot_mask_cleanup_unknown_or_invent_process(
    api: Any, flow: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    fault: str, broken: bool,
) -> None:
    flow.fault = fault
    if fault == 'nested':
        flow.payload = {'status': 'failed', 'failed_phase': 'test', 'checks': [
            {'phase': 'test', 'status': 'failed', 'error': 'go_project_cleanup_incomplete'}]}

    class Broken:
        def write(self, value: str) -> None:
            raise SystemExit('private stderr failure')

    if broken:
        monkeypatch.setattr(api, 'sys', SimpleNamespace(stderr=Broken()))
    with pytest.raises(ProcessCleanupIncomplete, match='go_project_cleanup_incomplete'):
        flow.execute()
    raw = capsys.readouterr().err
    if not broken:
        detail = json.loads(raw)['go_project_diagnostic']
        assert detail['stage'] == {'launch': 'run', 'quiet': 'quiesce', 'nested': 'report'}[fault]
        assert detail['exception_type'] == 'ProcessCleanupIncomplete'
        assert ('process' in detail) == (fault != 'launch')
    assert flow.events.count('launch') == flow.events.count('quiet') == 1
    assert flow.events[-1] == 'release'


def test_backend_exception_details_and_stderr_failure_do_not_change_error(
    api: Any, flow: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AttributeError('/root/private token=private-secret', name='/root/private')

    class Broken:
        def write(self, value: str) -> None:
            raise BrokenPipeError('private stderr failure')

    monkeypatch.setattr(api, '_write_plan', fail)
    response = flow.execute()
    assert not response.ok and response.error == 'go_project_execution_unavailable'
    assert json.loads(capsys.readouterr().err) == {'go_project_diagnostic': {
        'stage': 'plan', 'exception_type': 'AttributeError'}}
    monkeypatch.setattr(api, 'sys', SimpleNamespace(stderr=Broken()))
    assert flow.execute() == response


@pytest.mark.parametrize('changes', [
    {'stdout_observed_bytes': 'private-counter'}, {'stderr_truncated': 'private-flag'},
    {'stdout_observed_bytes': True}, {'stdout_observed_bytes': -1},
    {'exit_code': 2**1000},
])
def test_failure_diagnostic_withholds_malformed_native_metadata(
    api: Any, flow: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    changes: dict[str, Any],
) -> None:
    actual = process('safe output', exit_code=1)
    actual = replace(actual, **changes)
    monkeypatch.setattr(api, 'run_python_phase', lambda *a, **k: actual)
    assert not flow.execute().ok
    raw = capsys.readouterr().err
    detail = json.loads(raw)['go_project_diagnostic']
    assert detail['stage'] == 'report' and detail['process_withheld'] is True
    assert 'process' not in detail and 'private' not in raw


@pytest.mark.parametrize('exit_code,timed_out', [(None, True), (-9, True), (None, False)])
def test_failure_diagnostic_keeps_actual_exit_and_timeout_without_inventing_completion(
    api: Any, flow: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    exit_code: int | None, timed_out: bool,
) -> None:
    actual = process('safe output', exit_code=exit_code, timed_out=timed_out)
    monkeypatch.setattr(api, 'run_python_phase', lambda *a, **k: actual)
    assert not flow.execute().ok
    detail = json.loads(capsys.readouterr().err)['go_project_diagnostic']['process']
    assert detail['exit_code'] == exit_code and detail['timed_out'] is timed_out
    assert detail['stdout_observed_bytes'] == len(actual.stdout.encode())
    if exit_code is None and not timed_out:
        assert 'stdout' not in detail


@pytest.mark.parametrize('prepared', [True], indirect=True)
def test_nonzero_process_diagnostic_cannot_be_used_as_passed_business_receipt(
    api: Any, business_flow: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launch = api.run_python_phase

    def nonzero(*args: Any, **kwargs: Any) -> Any:
        return replace(launch(*args, **kwargs), exit_code=1)

    class Sink:
        def write(self, value: str) -> None:
            assert business_flow.events.count('quiet') == 1
            sys.stderr.write(value)

    monkeypatch.setattr(api, 'run_python_phase', nonzero)
    monkeypatch.setattr(api, 'sys', SimpleNamespace(stderr=Sink()))
    response = business_flow.execute()
    assert not response.ok and response.result is response.receipt is None
    assert 'binary' not in business_flow.events
    detail = json.loads(capsys.readouterr().err)['go_project_diagnostic']
    assert detail['process']['exit_code'] == 1 and 'receipt' not in detail


@pytest.fixture
def business_flow(api: Any, flow: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    receipt = _receipt(prepared.inputs)
    binary = {key: receipt['binary'][key] for key in ('filename', 'sha256', 'size')}
    flow.payload = {
        'checks': [{'phase': phase, 'status': 'completed'}
                   for phase in ('inspect', 'build', 'test', 'canary', 'business')],
        'canary': 'completed', 'business_gate': 'completed', 'binary': binary,
        'named_tests': receipt['named_tests'], 'business_evidence': receipt['business_evidence'],
        'acceptance_sha256': receipt['acceptance_sha256'],
        'business_suite_sha256': receipt['business_suite_sha256'],
        'inventory': {'module_path': prepared.inputs.declaration.module_path,
                      'package_count': 1, 'test_count': 1, 'source_go_file_count': 2,
                      'report_sha256': '1' * 64},
        'test_evidence': {'status': 'completed', 'report_sha256': '1' * 64,
            'expected_sha256': '2' * 64, 'required_test_count': 1, 'required_passed_count': 1,
            'failed_count': 0, 'test_count': 1, 'package_count': 1},
        'canary_evidence': {'execution_id': 'go_canary_123',
            'project_execution_id': prepared.invocation.execution_id, 'report_sha256': '3' * 64,
            'expected_sha256': '4' * 64, 'observed_expected_failure': True},
    }
    flow.binary = binary

    def observe(fd: int, **kwargs: Any) -> Any:
        assert fd == 20
        assert kwargs['gid'] == prepared.policy.allowed_gid
        assert kwargs['service_uid'] == prepared.policy.allowed_uid
        assert flow.events.index('quiet') < len(flow.events)
        flow.events.append('binary')
        if flow.fault == 'binary-missing':
            raise ValueError('private missing binary')
        return binary | ({'sha256': 'f' * 64} if flow.fault == 'binary-mismatch' else {})

    monkeypatch.setattr(api, 'observe_go_project_binary', observe, raising=False)
    return flow


@pytest.mark.parametrize('prepared', [True], indirect=True)
def test_root_business_receipt_requires_quiet_measurement_and_final_rechecks(
    business_flow: Any, prepared: Any,
) -> None:
    flow = business_flow
    response = flow.execute()
    assert response.ok and response.receipt is not None
    receipt = response.receipt.model_dump(mode='json')
    assert receipt['status'] == 'passed' and receipt['execution_id'] == prepared.invocation.execution_id
    assert receipt['binary']['sha256'] == flow.binary['sha256']
    assert json.loads(response.result.stdout)['status'] == 'incomplete'
    assert flow.events.count('binary') >= 2
    assert flow.events.index('quiet') < flow.events.index('binary')
    assert flow.events[-1] == 'release'
    response.require_for(prepared.request)


@pytest.mark.parametrize('prepared', [True], indirect=True)
@pytest.mark.parametrize('fault', ['binary-missing', 'binary-mismatch', 'authority-3'])
def test_business_report_without_matching_actual_export_or_live_grant_cannot_pass(
    business_flow: Any, fault: str,
) -> None:
    business_flow.fault = fault
    response = business_flow.execute()
    assert not response.ok and response.receipt is None
    assert 'quiet' in business_flow.events


@pytest.mark.parametrize('prepared', [True], indirect=True)
@pytest.mark.parametrize('damage', ['observation', 'tests', 'canary', 'phase', 'acceptance'])
def test_root_rejects_forged_incomplete_or_rebound_business_evidence(
    business_flow: Any, damage: str,
) -> None:
    payload = business_flow.payload
    if damage == 'observation':
        payload['business_evidence'][0]['stdout_truncated'] = True
    elif damage == 'tests':
        payload['named_tests'] = []
    elif damage == 'canary':
        payload['canary_evidence']['observed_expected_failure'] = False
    elif damage == 'phase':
        payload['checks'].pop(2)
    else:
        payload['acceptance_sha256'] = '9' * 64
    response = business_flow.execute()
    assert not response.ok and response.receipt is None


@pytest.mark.parametrize('prepared', [True], indirect=True)
def test_binary_changed_after_receipt_authority_is_rejected(
    api: Any, business_flow: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = api.observe_go_project_binary

    def late(fd: int, **kwargs: Any) -> Any:
        result = real(fd, **kwargs)
        return result | ({'sha256': 'f' * 64} if business_flow.authority_calls == 3 else {})

    monkeypatch.setattr(api, 'observe_go_project_binary', late)
    response = business_flow.execute()
    assert not response.ok and response.receipt is None


@pytest.mark.parametrize('prepared', [True], indirect=True)
def test_business_candidate_receipt_keeps_original_recipe_and_actual_candidate_digest(
    business_flow: Any, candidate: Any, prepared: Any,
) -> None:
    response = candidate.execute(prepared.request)
    assert response.ok and response.receipt is not None
    receipt = response.receipt.model_dump(mode='json')
    assert receipt['source_kind'] == 'candidate'
    assert receipt['source_sha256'] == candidate.candidate_digest
    assert receipt['source_sha256'] != prepared.inputs.scope.source_sha256
    assert receipt['recipe_sha256'] == prepared.inputs.sha256
    assert receipt['original_reference'] == prepared.inputs.original_reference.to_payload()
    response.require_for(prepared.request)


@pytest.mark.parametrize('prepared', [True], indirect=True)
def test_failure_after_completed_business_preserves_diagnostics_without_receipt(
    business_flow: Any,
) -> None:
    business_flow.payload.update(status='failed', failed_phase='toolchain')
    business_flow.payload['checks'].append(
        {'phase': 'toolchain', 'status': 'failed', 'error': 'go_project_toolchain_changed'})
    response = business_flow.execute()
    assert response.ok and response.receipt is None
    assert json.loads(response.result.stdout)['failed_phase'] == 'toolchain'
    assert 'binary' not in business_flow.events


def test_inspect_is_live_measured_and_never_launches(flow: Any, prepared: Any) -> None:
    request = prepared.request.model_copy(update={'action': 'inspect_go_project_environment'})
    response = flow.execute(request)
    assert response.ok and response.environment == prepared.inputs.toolchain
    assert response.result is None and 'launch' not in flow.events and 'plan' not in flow.events
    assert flow.authority_calls >= 2 and flow.events.count('sdk') >= 2
    response.require_for(request)


@pytest.mark.parametrize('phase', [1, 2, 3])
def test_revoked_authority_never_returns_success(flow: Any, phase: int) -> None:
    flow.fault = f'authority-{phase}'
    response = flow.execute()
    assert not response.ok and response.error == 'go_project_recipe_approval_required'
    assert ('launch' in flow.events) == (phase == 3)


@pytest.mark.parametrize('phase', [2, 3])
def test_live_authority_cannot_outlive_launch_or_return_deadline(
    api: Any, flow: Any, phase: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = SimpleNamespace(now=time.monotonic())
    monkeypatch.setattr(api, 'time', SimpleNamespace(monotonic=lambda: clock.now))

    def authority(inputs: Any, inv: Any) -> None:
        flow.authorize(inputs, inv)
        if flow.authority_calls == phase:
            clock.now += inv.timeout_seconds + 1

    response = flow.execute(authority=authority)
    assert not response.ok and response.error == 'go_project_timeout'
    assert ('launch' in flow.events) == (phase == 3)


@pytest.mark.parametrize('value', [True, False, None])
def test_approval_boolean_is_never_an_authority(flow: Any, value: Any) -> None:
    response = flow.execute(authority=value)
    assert not response.ok and response.error == 'go_project_recipe_approval_required'
    assert 'launch' not in flow.events


@pytest.mark.parametrize('value', [True, False, 'approved'])
def test_authority_must_raise_or_return_none(flow: Any, value: Any) -> None:
    response = flow.execute(authority=lambda *_: value)
    assert not response.ok and 'launch' not in flow.events


def test_async_authority_is_rejected_without_running_it(flow: Any) -> None:
    async def authorize(*_: Any) -> None:
        pytest.fail('async authority ran')
    assert not flow.execute(authority=authorize).ok
    assert 'launch' not in flow.events


@pytest.mark.parametrize('fault', ['source', 'sdk'])
def test_actual_source_and_independent_sdk_are_required(flow: Any, fault: str) -> None:
    flow.fault = fault
    response = flow.execute()
    assert not response.ok and 'launch' not in flow.events and flow.authority_calls == 0


@pytest.mark.parametrize('field', ['recipe_sha256', 'environment_input_sha256'])
def test_claimed_binding_is_compared_to_rebuilt_inputs(flow: Any, prepared: Any, field: str) -> None:
    request = prepared.request.model_copy(update={
        'invocation': prepared.invocation.model_copy(update={field: '9' * 64})})
    response = flow.execute(request)
    assert not response.ok and 'launch' not in flow.events


@pytest.mark.parametrize('field', ['scope', 'toolchain', 'original_reference'])
def test_report_nested_types_are_exact(api: Any, prepared: Any, field: str) -> None:
    payload = report(prepared)
    if field == 'scope':
        payload[field]['user_id'] = str(prepared.inputs.scope.run_id)
    elif field == 'toolchain':
        payload[field]['total_bytes'] = float(payload[field]['total_bytes'])
    else:
        payload[field]['file_count'] = float(payload[field]['file_count'])
    with pytest.raises(ValueError):
        api._report(process(payload), prepared.invocation, prepared.inputs,
                    prepared.inputs.scope.source_sha256)


@pytest.mark.parametrize('fault', ['sdk', 'source', 'pins'])
def test_late_changes_cannot_return_success(flow: Any, fault: str) -> None:
    def authority(inputs: Any, inv: Any) -> None:
        flow.authorize(inputs, inv)
        if flow.authority_calls == 2:
            flow.fault = fault
    response = flow.execute(authority=authority)
    assert not response.ok and 'launch' not in flow.events


def test_launch_receives_exact_go_budget_not_source_projection(flow: Any, prepared: Any) -> None:
    assert flow.execute().ok
    # Root report capture is fixed, phase diagnostics are allocated by the Go plan.
    assert prepared.invocation.output_limit_bytes > 1024**2
    assert flow.events.count('launch') == 1


@pytest.mark.parametrize('changes', [
    {'phase': 'shell'}, {'status': 'passed'}, {'stdout': '\ud800'},
    {'exit_code': False}, {'stdout_observed_bytes': 2}, {'stderr_truncated': 'false'},
])
def test_report_process_rows_are_strict(api: Any, prepared: Any, changes: Any) -> None:
    row = {'phase': 'build', 'status': 'completed', 'exit_code': 0, 'timed_out': False,
        'stdout': '', 'stderr': '', 'stdout_truncated': False, 'stderr_truncated': False,
        'stdout_observed_bytes': 0, 'stderr_observed_bytes': 0}
    with pytest.raises(ValueError):
        api._report(process(report(prepared) | {'checks': [row | changes]}),
            prepared.invocation, prepared.inputs, prepared.inputs.scope.source_sha256)


def test_generated_capture_cannot_become_original_v1(flow: Any, prepared: Any) -> None:
    prepared.fixture.stored = replace(prepared.fixture.stored, generated_metadata={
        'schema_version': 1, 'purpose': 'generated_acceptance', 'capture_phase': 'post_model',
        'user_id': str(prepared.inputs.scope.user_id), 'request_sha256': '1' * 64,
        'manifest_sha256': '2' * 64})
    response = flow.execute()
    assert not response.ok and 'launch' not in flow.events and flow.authority_calls == 0


@pytest.fixture
def candidate(flow: Any, prepared: Any) -> Any:
    prepared.invocation = prepared.invocation.model_copy(update={'source_kind': 'candidate'})
    prepared.request = prepared.request.model_copy(update={'invocation': prepared.invocation})
    flow.candidate = prepared.fixture.files | {'main.go': hashlib.sha256(
        b'f\0package main\nfunc main() { println("repair") }\n').hexdigest(),
        'assets/added.txt': hashlib.sha256(b'f\0new inactive file').hexdigest()}
    flow.candidate_digest = hashlib.sha256(json.dumps(flow.candidate, sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
    return flow


def test_candidate_binds_measured_source_without_rebinding_original_recipe(
    candidate: Any, prepared: Any,
) -> None:
    response = candidate.execute(prepared.request)
    assert response.ok and response.result is not None
    payload = json.loads(response.result.stdout)
    assert payload['source_sha256'] == candidate.candidate_digest != prepared.inputs.scope.source_sha256
    assert payload['scope'] == prepared.inputs.scope.to_payload()
    assert payload['recipe_sha256'] == prepared.inputs.sha256
    assert payload['toolchain'] == prepared.inputs.toolchain.model_dump(mode='json')
    assert payload['status'] == 'incomplete' and payload['business_gate'] == 'not_run'
    assert payload['toolchain_measurement'] == 'not_run'
    assert candidate.plan.source_sha256 == candidate.candidate_digest
    assert candidate.events.count('snapshot') == 1 and candidate.events.count('candidate') >= 3
    assert candidate.authority_calls == 3
    response.require_for(prepared.request)


@pytest.mark.parametrize('path', ['go.mod', 'main_test.go', 'assets/new_test.go',
    'assets/go.mod', 'assets/setup.py', 'assets/native.s', 'assets/native.S',
    'assets/native.syso', 'assets/native.SYSO', 'assets/native.c', 'assets/native.cpp',
    'assets/native.h', 'assets/native.swigcxx'])
def test_candidate_cannot_change_protected_controls_tests_or_add_native_inputs(
    candidate: Any, prepared: Any, path: str,
) -> None:
    candidate.candidate[path] = '1' * 64
    candidate.candidate_digest = hashlib.sha256(json.dumps(candidate.candidate, sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
    response = candidate.execute(prepared.request)
    assert not response.ok and 'launch' not in candidate.events and 'plan' not in candidate.events


@pytest.mark.parametrize('path', ['go.mod', 'main_test.go', 'assets/readme.txt', 'inactive_windows.go'])
def test_candidate_deletions_follow_captured_editable_scope(
    candidate: Any, prepared: Any, path: str,
) -> None:
    del candidate.candidate[path]
    candidate.candidate_digest = hashlib.sha256(json.dumps(candidate.candidate, sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
    response = candidate.execute(prepared.request)
    if path == 'assets/readme.txt':
        # assets is explicitly editable in the captured contract.
        assert response.ok
    else:
        assert not response.ok and 'plan' not in candidate.events


@pytest.mark.parametrize('boundary', ['manifest', 'current', 'after-process'])
@pytest.mark.parametrize('damage,code', [
    ('go.mod', 'go_project_protected_input_changed'),
    ('main_test.go', 'go_project_protected_input_changed'),
    ('inactive_windows.go', 'go_project_protected_input_changed'),
    ('outside.go', 'go_project_protected_input_changed'),
    ('assets/new_test.go', 'go_project_protected_input_changed'),
    ('assets/go.mod', 'go_project_protected_input_changed'),
    ('assets/native.S', 'go_project_unsupported'),
    ('assets/bad path.S', 'go_project_source_unavailable'),
    ('invalid-digest', 'go_project_source_unavailable'),
    ('case-alias', 'go_project_source_unavailable'),
    ('delete-protected', 'go_project_protected_input_changed'),
    ('files-overbudget', 'go_project_capacity'),
])
def test_candidate_validation_errors_are_finite_at_each_boundary(
    api: Any, candidate: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch,
    boundary: str, damage: str, code: str,
) -> None:
    bad = dict(candidate.candidate)
    if damage == 'invalid-digest':
        bad['main.go'] = 'not-a-digest'
    elif damage == 'case-alias':
        bad['Assets/other.txt'] = '1' * 64
    elif damage == 'delete-protected':
        del bad['inactive_windows.go']
    elif damage == 'files-overbudget':
        bad.update({f'assets/extra{i}.txt': '1' * 64 for i in range(prepared.inputs.file_limit)})
    else:
        bad[damage] = '1' * 64
    if boundary == 'manifest':
        candidate.candidate = bad
        candidate.candidate_digest = hashlib.sha256(json.dumps(bad, sort_keys=True,
            separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
    else:
        measured = api._source_files

        def current(root: Path, inv: Any, deadline: float) -> Any:
            files = measured(root, inv, deadline)
            if root == prepared.stage / 'snapshot' and (
                boundary == 'current' or 'quiet' in candidate.events
            ):
                return bad
            return files

        monkeypatch.setattr(api, '_source_files', current)
    response = candidate.execute(prepared.request)
    assert not response.ok and response.error == code
    assert ('launch' in candidate.events) == (boundary == 'after-process')
    if boundary == 'after-process':
        assert candidate.events.count('quiet') == 1
    response.require_for(prepared.request)


@pytest.mark.parametrize('boundary', ['manifest', 'current', 'after-process'])
@pytest.mark.parametrize('failure', ['unknown', 'cleanup', 'cancel'])
def test_candidate_validation_preserves_unknown_execution_errors_and_control_flow(
    api: Any, candidate: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch,
    boundary: str, failure: str,
) -> None:
    error = {'unknown': RuntimeError('go_project_protected_input_changed'),
             'cleanup': ProcessCleanupIncomplete('go_project_cleanup_incomplete'),
             'cancel': asyncio.CancelledError()}[failure]
    original = api.GoProjectInputs.require_current

    def validate(inputs: Any, files: Any) -> None:
        if files == candidate.candidate and (
            boundary != 'after-process' or 'quiet' in candidate.events
        ) and (boundary != 'current' or 'candidate' in candidate.events):
            raise error
        original(inputs, files)

    monkeypatch.setattr(api.GoProjectInputs, 'require_current', validate)
    if failure == 'unknown':
        response = candidate.execute(prepared.request)
        assert not response.ok and response.error == 'go_project_execution_unavailable'
    else:
        with pytest.raises(type(error)):
            candidate.execute(prepared.request)
    assert ('launch' in candidate.events) == (boundary == 'after-process')


@pytest.mark.parametrize('step', ['_write_plan', '_make_export', '_export_empty'])
@pytest.mark.parametrize('kind', [ValueError, RuntimeError])
def test_candidate_unknown_errors_outside_validators_stay_generic(
    api: Any, candidate: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch,
    step: str, kind: type[Exception],
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise kind('go_project_protected_input_changed')

    monkeypatch.setattr(api, step, fail)
    response = candidate.execute(prepared.request)
    assert not response.ok and response.error == 'go_project_execution_unavailable'
    assert 'launch' not in candidate.events


@pytest.mark.parametrize('fault', ['source', 'candidate', 'sdk'])
@pytest.mark.parametrize('phase', [2, 3])
def test_candidate_remeasures_both_sources_and_sdk_after_live_authority(
    candidate: Any, prepared: Any, fault: str, phase: int,
) -> None:
    def authority(inputs: Any, inv: Any) -> None:
        candidate.authorize(inputs, inv)
        if candidate.authority_calls == phase:
            candidate.fault = fault
    response = candidate.execute(prepared.request, authority=authority)
    assert not response.ok
    assert response.error == {'source': 'go_project_protected_input_changed',
        'candidate': 'go_project_source_unavailable', 'sdk': 'go_project_toolchain_changed'}[fault]
    assert ('launch' in candidate.events) == (phase == 3)


@pytest.mark.parametrize('field', ['source_sha256', 'source_kind', 'scope', 'toolchain'])
def test_candidate_report_cannot_substitute_original_digest_sdk_or_scope(
    candidate: Any, prepared: Any, field: str,
) -> None:
    candidate.payload = {field: {'source_sha256': prepared.inputs.scope.source_sha256,
        'source_kind': 'original', 'scope': {}, 'toolchain': {}}[field]}
    response = candidate.execute(prepared.request)
    assert not response.ok and candidate.events.count('quiet') == 1


@pytest.mark.parametrize('fault', ['launch', 'quiet'])
def test_candidate_cleanup_uncertainty_is_unknown_without_retry(
    candidate: Any, prepared: Any, fault: str,
) -> None:
    candidate.fault = fault
    with pytest.raises(ProcessCleanupIncomplete):
        candidate.execute(prepared.request)
    assert candidate.events.count('launch') == candidate.events.count('quiet') == 1


@pytest.mark.parametrize('phase', [1, 2, 3])
def test_candidate_live_revocation_is_preserved(candidate: Any, prepared: Any, phase: int) -> None:
    candidate.fault = f'authority-{phase}'
    response = candidate.execute(prepared.request)
    assert not response.ok and response.error == 'go_project_recipe_approval_required'
    assert ('launch' in candidate.events) == (phase == 3)


def test_candidate_snapshot_digest_is_checked_before_plan(candidate: Any, prepared: Any) -> None:
    candidate.candidate_digest = '9' * 64
    response = candidate.execute(prepared.request)
    assert not response.ok and 'plan' not in candidate.events and 'launch' not in candidate.events


def test_candidate_inspect_validates_snapshot_without_launch(candidate: Any, prepared: Any) -> None:
    request = prepared.request.model_copy(update={'action': 'inspect_go_project_environment'})
    response = candidate.execute(request)
    assert response.ok and response.environment == prepared.inputs.toolchain
    assert candidate.events.count('snapshot') == 1 and candidate.events.count('candidate') >= 3
    assert 'launch' not in candidate.events and 'plan' not in candidate.events


@pytest.mark.parametrize('fault', ['source', 'candidate', 'sdk'])
def test_candidate_process_changes_are_rejected_after_quiescence(
    api: Any, candidate: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    launch = api.run_python_phase

    def changed(*args: Any, **kwargs: Any) -> Any:
        result = launch(*args, **kwargs)
        candidate.fault = fault
        return result

    monkeypatch.setattr(api, 'run_python_phase', changed)
    response = candidate.execute(prepared.request)
    assert not response.ok and candidate.events.count('quiet') == 1
    assert response.error == {'source': 'go_project_protected_input_changed',
        'candidate': 'go_project_source_unavailable', 'sdk': 'go_project_toolchain_changed'}[fault]


@pytest.mark.parametrize('phase', [1, 2, 3])
def test_candidate_authority_unknown_is_never_converted_to_denial(
    candidate: Any, prepared: Any, phase: int,
) -> None:
    def authority(inputs: Any, inv: Any) -> None:
        candidate.authorize(inputs, inv)
        if candidate.authority_calls == phase:
            raise ProcessCleanupIncomplete('go_project_cleanup_incomplete')
    with pytest.raises(ProcessCleanupIncomplete):
        candidate.execute(prepared.request, authority=authority)
    assert ('launch' in candidate.events) == (phase == 3)


@pytest.mark.parametrize('phase', [1, 2, 3])
def test_candidate_cancellation_is_preserved(candidate: Any, prepared: Any, phase: int) -> None:
    def authority(inputs: Any, inv: Any) -> None:
        candidate.authorize(inputs, inv)
        if candidate.authority_calls == phase:
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        candidate.execute(prepared.request, authority=authority)
    assert ('launch' in candidate.events) == (phase == 3)


def test_candidate_unknown_admission_never_copies_or_executes(
    api: Any, candidate: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def unknown(*args: Any) -> Any:
        raise api.GoProjectBackendError('go_project_unknown')
        yield

    monkeypatch.setattr(api, '_admission', unknown)
    response = candidate.execute(prepared.request)
    assert not response.ok and response.error == 'go_project_unknown'
    assert 'snapshot' not in candidate.events and 'launch' not in candidate.events


def test_candidate_wrong_scope_is_rejected_by_rebuilt_recipe(
    api: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import UUID

    monkeypatch.setattr(api, '_source_files', lambda *_: prepared.fixture.files)
    monkeypatch.setattr(api, 'observe_published_toolchain', lambda *a, **k: prepared.inputs.toolchain)
    monkeypatch.setattr(api, 'toolchain_reference', lambda value: value)
    inv = prepared.invocation.model_copy(update={'source_kind': 'candidate',
        'scope': prepared.inputs.scope.model_copy(update={'user_id': UUID(int=99)})})
    with pytest.raises(ValueError, match='go_project_recipe_changed'):
        api._measured_inputs(prepared.fixture.stored, inv, prepared.catalog, time.monotonic() + 10)


@pytest.mark.parametrize('fault', ['launch', 'quiet'])
def test_cleanup_uncertainty_propagates_without_retry(flow: Any, fault: str) -> None:
    flow.fault = fault
    with pytest.raises(ProcessCleanupIncomplete):
        flow.execute()
    assert flow.events.count('launch') == 1 and flow.events.count('quiet') == 1
    assert flow.events[-1] == 'release'


def test_native_requires_root_single_thread_no_loop(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(api.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(api.os, 'getegid', lambda: 0, raising=False)
    monkeypatch.setattr(api.threading, 'active_count', lambda: 2)
    with pytest.raises(ValueError, match='go_project_unsupported'):
        api._native()
    monkeypatch.setattr(api.threading, 'active_count', lambda: 1)

    async def running() -> None:
        with pytest.raises(ValueError, match='go_project_unsupported'):
            api._native()
    asyncio.run(running())


@pytest.mark.parametrize('source_kind,copies', [('original', 1), ('candidate', 2)])
def test_disk_budget_includes_rounding_entries_controls_and_reserve(
    api: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch, source_kind: str, copies: int,
) -> None:
    block = 4096
    inv = prepared.invocation.model_copy(update={'source_kind': source_kind})
    required = (copies * (inv.source_limit_bytes + inv.file_limit * 10 * block + 32 * 1024**2)
                + 32 * 1024**2 + 1024**2 + inv.workspace_limit_bytes)
    available = required + prepared.catalog.min_free_bytes
    monkeypatch.setattr(api.os, 'fstatvfs', lambda fd: SimpleNamespace(
        f_bavail=available // block, f_frsize=block), raising=False)
    api._disk_budget(20, inv, prepared.catalog)
    available -= block
    with pytest.raises(ValueError, match='go_project_capacity'):
        api._disk_budget(20, inv, prepared.catalog)


@pytest.mark.parametrize('limit', ['bytes', 'files'])
def test_candidate_appended_files_cannot_exceed_source_budget(
    api: Any, prepared: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str,
) -> None:
    workspace = tmp_path / 'workspace'
    inv = prepared.invocation.model_copy(update={'source_kind': 'candidate'})
    source = workspace / str(inv.tenant_id) / 'projects' / inv.project_id / 'sessions' / inv.session_id
    source.mkdir(parents=True)
    for name, data in prepared.fixture.raw.items():
        path = source / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    (source / 'assets' / 'added.txt').write_bytes(b'additional source bytes')
    if limit == 'bytes':
        inv = inv.model_copy(update={'source_limit_bytes': sum(map(len, prepared.fixture.raw.values()))})
    else:
        inv = inv.model_copy(update={'file_limit': len(prepared.fixture.raw)})
    stage = tmp_path / 'stage'
    stage.mkdir()
    policy = replace(prepared.policy, project_workspace_root=workspace)
    monkeypatch.setattr(api, '_source_files', lambda *args: pytest.fail('overbudget copy admitted'))
    with pytest.raises(api.GoProjectBackendError) as failure:
        api._source_snapshot(prepared.fixture.stored, prepared.inputs, inv, policy,
                             stage, time.monotonic() + 10)
    assert failure.value.code == 'go_project_source_unavailable'


@pytest.mark.parametrize('kind', [RuntimeError, ProcessCleanupIncomplete, asyncio.CancelledError])
def test_candidate_snapshot_helper_rejection_preserves_control_flow(
    api: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch, kind: type[BaseException],
) -> None:
    error = kind('unrelated private helper error text')

    async def reject(*args: Any, **kwargs: Any) -> str:
        raise error

    monkeypatch.setattr(api, 'snapshot_project_source', reject)
    monkeypatch.setattr(api, '_source_files', lambda *a: pytest.fail('rejected copy remeasured'))
    inv = prepared.invocation.model_copy(update={'source_kind': 'candidate'})
    if kind is RuntimeError:
        with pytest.raises(api.GoProjectBackendError) as failure:
            api._source_snapshot(prepared.fixture.stored, prepared.inputs, inv, prepared.policy,
                                 prepared.stage, time.monotonic() + 10)
        assert failure.value.code == 'go_project_source_unavailable'
    else:
        with pytest.raises(kind) as failure:
            api._source_snapshot(prepared.fixture.stored, prepared.inputs, inv, prepared.policy,
                                 prepared.stage, time.monotonic() + 10)
        assert failure.value is error


def test_candidate_real_copy_binds_complete_live_scope_and_ignores_later_workspace_edits(
    api: Any, prepared: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, stage = tmp_path / 'workspace', tmp_path / 'stage'
    inv = prepared.invocation.model_copy(update={'source_kind': 'candidate'})
    source = workspace / str(inv.tenant_id) / 'projects' / inv.project_id / 'sessions' / inv.session_id
    source.mkdir(parents=True)
    raw = prepared.fixture.raw | {'main.go': b'package main\nfunc main() { println("fixed") }\n',
                                 'assets/extra.txt': b'non-Go candidate input\n'}
    expected = {name: hashlib.sha256(b'f\0' + data).hexdigest() for name, data in raw.items()}
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
    for name, data in raw.items():
        path = source / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    stage.mkdir()
    policy = replace(prepared.policy, project_workspace_root=workspace)
    observed = []

    def measured(root: Path, invocation: Any, deadline: float) -> Any:
        observed.append(root)
        # Only native ownership/dir_fd measurement is replaced on Windows.
        return {path.relative_to(root).as_posix(): hashlib.sha256(
            b'f\0' + path.read_bytes()).hexdigest() for path in root.rglob('*') if path.is_file()}

    monkeypatch.setattr(api, '_source_files', measured)
    try:
        assert api._source_snapshot(prepared.fixture.stored, prepared.inputs, inv,
                                    policy, stage, time.monotonic() + 10) == digest
        snapshot = stage / 'snapshot'
        assert {p.relative_to(snapshot).as_posix(): p.read_bytes()
                for p in snapshot.rglob('*') if p.is_file()} == raw
        (source / 'main.go').write_bytes(b'later workspace mutation')
        api._recheck_source(prepared.fixture.stored, prepared.inputs, inv, snapshot,
                            digest, time.monotonic() + 10)
        assert observed.count(prepared.fixture.stored.snapshot) == 2
        assert observed.count(snapshot) == 2
        with pytest.raises(api.GoProjectBackendError) as failure:
            api._source_snapshot(prepared.fixture.stored, prepared.inputs, inv,
                                 policy, stage, time.monotonic() + 10)
        assert failure.value.code == 'go_project_source_unavailable'
    finally:
        if (stage / 'snapshot').exists():
            for path in (stage / 'snapshot').rglob('*'):
                path.chmod(0o700 if path.is_dir() else 0o600)
            (stage / 'snapshot').chmod(0o700)


@pytest.mark.parametrize('change', ['stage-parent', 'policy-stage', 'uid', 'gid', 'group',
                                   'systemctl', 'shared-admission', 'source-overlap'])
def test_roots_reject_bad_configuration_before_opening_any_path(
    api: Any, prepared: Any, change: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, catalog, stage = prepared.policy, prepared.catalog, prepared.stage
    if change == 'stage-parent':
        stage = stage / 'nested'
    elif change == 'policy-stage':
        policy = replace(policy, toolchain_runtime_root=Path('/elsewhere'))
    elif change == 'uid':
        policy = replace(policy, allowed_uid=True)
    elif change == 'gid':
        policy = replace(policy, allowed_gid=0)
    elif change == 'group':
        policy = replace(policy, service_group='unsafe group')
    elif change == 'systemctl':
        policy = replace(policy, systemctl='/model/systemctl')
    elif change == 'shared-admission':
        policy = replace(policy, python_project_runtime_root=Path(catalog.stage_root))
    else:
        policy = replace(policy, source_path=Path(catalog.runtime_root) / 'code')
    monkeypatch.setattr(api.os, 'open', lambda *a, **k: pytest.fail('unsafe roots opened'))
    with pytest.raises(ValueError), api._roots(policy, catalog, stage):
        pytest.fail('unsafe roots admitted')


def test_quiesce_uses_existing_broker_and_preserves_go_invocation(
    api: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = []

    async def kill(broker: Any, job: Any) -> None:
        observed.append(job.invocation)

    monkeypatch.setattr(api._python.ProjectExecutionBroker, '_kill_and_confirm', kill)
    job = api._Execution(prepared.invocation, None, launched=True)
    api._quiesce(prepared.policy, job)
    assert observed == [prepared.invocation] and job.quiet


@pytest.mark.skipif(sys.platform != 'linux', reason='requires native Linux Root dir_fd/modes')
def test_native_plan_is_root_readonly_exclusive_and_never_removes_stage(
    api: Any, prepared: Any, tmp_path: Path,
) -> None:
    if sys.platform != 'linux':
        pytest.skip('requires native Linux Root dir_fd/modes')
    if os.geteuid() != 0 or os.getegid() != 0:
        pytest.skip('requires Root')
    plan = api.GoProjectRunnerPlan(invocation=prepared.invocation,
        inputs=prepared.inputs.to_payload(), source_sha256=prepared.inputs.scope.source_sha256)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        path = api._write_plan(plan, fd, tmp_path)
        assert path.stat().st_uid == 0 and path.stat().st_mode & 0o777 == 0o444
        assert json.loads(path.read_bytes()) == json.loads(plan.model_dump_json())
        with pytest.raises(FileExistsError):
            api._write_plan(plan, fd, tmp_path)
        assert path.exists() and tmp_path.exists()
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != 'linux', reason='requires native Linux Root sealed measurement')
def test_native_full_source_measurement_catches_non_go_extra_file(
    api: Any, prepared: Any,
) -> None:
    if sys.platform != 'linux':
        pytest.skip('requires native Linux Root sealed measurement')
    if os.geteuid() != 0 or os.getegid() != 0:
        pytest.skip('requires Root')
    root = prepared.fixture.stored.snapshot
    for path in root.rglob('*'):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)
    try:
        assert api._source_files(root, prepared.invocation, time.monotonic() + 10) == prepared.fixture.files
        root.chmod(0o755)
        (root / 'extra.txt').write_bytes(b'extra inactive input')
        (root / 'extra.txt').chmod(0o444)
        root.chmod(0o555)
        files = api._source_files(root, prepared.invocation, time.monotonic() + 10)
        assert files != prepared.fixture.files and 'extra.txt' in files
    finally:
        root.chmod(0o700)
        for path in root.rglob('*'):
            path.chmod(0o700 if path.is_dir() else 0o600)


@pytest.mark.skipif(sys.platform != 'linux', reason='requires native Linux Root dir_fd/modes')
def test_native_export_is_empty_root_group_writable_only_outer(
    api: Any, tmp_path: Path,
) -> None:
    if sys.platform != 'linux':
        pytest.skip('requires native Linux Root dir_fd/modes')
    if os.geteuid() != 0 or os.getegid() != 0:
        pytest.skip('requires Root')
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        export = api._make_export(fd, tmp_path, 10001)
        info = export.stat()
        assert info.st_uid == 0 and info.st_gid == 10001 and info.st_mode & 0o7777 == 0o770
        api._export_empty(fd, time.monotonic() + 10)
        (export / 'project').write_bytes(b'no binary export in first slice')
        with pytest.raises(ValueError):
            api._export_empty(fd, time.monotonic() + 10)
        assert export.exists()
    finally:
        os.close(fd)
