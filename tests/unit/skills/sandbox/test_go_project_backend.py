"""Root Go backend contracts; injected OS boundaries are not native Go proof."""

from __future__ import annotations

import asyncio
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
from tests.unit.capabilities.test_go_project_inputs import case

MODULE = 'agent_hub.skills.sandbox.go_project_backend'


def test_backend_exists() -> None:
    assert importlib.util.find_spec(MODULE) is not None, 'Root Go diagnostic backend missing'


@pytest.fixture
def api() -> Any:
    if importlib.util.find_spec(MODULE) is None:
        pytest.skip('backend missing; existence test is RED')
    return importlib.import_module(MODULE)


@pytest.fixture
def prepared(tmp_path: Path) -> Any:
    fixture = case(tmp_path / 'original', editable=('main.go', 'assets'), changes={
        'assets/readme.txt': b'full source, including inactive and non-Go files\n',
        'inactive_windows.go': b'package main\n',
    })
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
    request = GoProjectRequest(action='run_go_project', invocation=inv)
    return SimpleNamespace(fixture=fixture, inputs=inputs, invocation=inv, request=request,
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
    assert api._report(process(payload), prepared.invocation, prepared.inputs) == payload
    payload.update(status='failed', failed_phase='build',
        checks=[{'phase': 'build', 'status': 'failed', 'error': 'go_project_build_failed'}])
    assert api._report(process(payload), prepared.invocation, prepared.inputs) == payload


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
        api._report(process(report(prepared) | {field: value}), prepared.invocation, prepared.inputs)


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
        api._report(process(raw), prepared.invocation, prepared.inputs)


@pytest.mark.parametrize('changes', [
    {'stdout_truncated': True}, {'stderr_truncated': True}, {'timed_out': True},
    {'stdout_observed_bytes': 0}, {'stderr_observed_bytes': 1}, {'exit_code': None},
    {'exit_code': 1}, {'exit_code': False}, {'stdout_observed_bytes': True},
    {'stderr': '\ud800'}, {'stdout_observed_bytes': 32 * 1024**2 + 1},
])
def test_report_requires_complete_actual_utf8_output(api: Any, prepared: Any, changes: Any) -> None:
    with pytest.raises(ValueError):
        api._report(process(report(prepared), **changes), prepared.invocation, prepared.inputs)


def test_nested_cleanup_uncertainty_is_not_a_normal_failed_report(api: Any, prepared: Any) -> None:
    payload = report(prepared) | {'status': 'failed', 'failed_phase': 'test',
        'checks': [{'phase': 'test', 'status': 'failed', 'error': 'go_project_cleanup_incomplete'}]}
    with pytest.raises(ProcessCleanupIncomplete):
        api._report(process(payload), prepared.invocation, prepared.inputs)


@pytest.fixture
def flow(api: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace only native IO/process boundaries, retain actual recipe/plan/report code."""
    state = SimpleNamespace(events=[], authority_calls=0, fault=None, pins=0)
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
        return prepared.fixture.files | ({'extra.txt': 'f' * 64} if state.fault == 'source' else {})

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
        return target / 'plan.json'

    def export(fd: int, target: Path, gid: int) -> Path:
        assert gid == prepared.policy.allowed_gid
        return target / 'export'

    def launch(command: Any, **kwargs: Any) -> Any:
        state.events.append('launch')
        assert state.events[-2] == 'authorize'
        assert kwargs['capture_limit_bytes'] == 32 * 1024**2
        assert kwargs.get('pass_fds', ()) == () and kwargs['cwd'] == prepared.stage
        joined = '\n'.join(command)
        assert f'{prepared.artifact.artifact_sha256}/payload/go' in joined.replace('\\', '/')
        assert f'{prepared.artifact.sha256}/payload/go' not in joined.replace('\\', '/')
        assert '/run/agent-hub-go-work' in joined
        state.command = command
        if state.fault == 'launch':
            raise OSError('uncertain launcher')
        return process(report(prepared))

    def quiesce(policy: Any, job: Any) -> None:
        state.events.append('quiet')
        assert job.invocation.output_limit_bytes == prepared.invocation.output_limit_bytes
        if state.fault == 'quiet':
            raise ProcessCleanupIncomplete('go_project_cleanup_incomplete')

    monkeypatch.setattr(api, '_roots', roots)
    monkeypatch.setattr(api, '_admission', admission)
    monkeypatch.setattr(api, '_disk_budget', lambda *a: state.events.append('budget'))
    monkeypatch.setattr(api, 'stage_original_contract', stage)
    monkeypatch.setattr(api, '_source_files', measured)
    monkeypatch.setattr(api, 'observe_published_toolchain', observe)
    monkeypatch.setattr(api, 'toolchain_reference', lambda value: value)
    monkeypatch.setattr(api, '_write_plan', write)
    monkeypatch.setattr(api, '_make_export', export)
    monkeypatch.setattr(api, '_export_empty', lambda *a: state.events.append('export-empty'))
    native_command = api.build_go_project_systemd_command

    def command(inv: Any, policy: Any, **locations: Any) -> Any:
        # Synthetic staging reads a real Windows fixture; the real command builder
        # still validates the POSIX paths a native Root stage would supply.
        assert locations['snapshot'] == locations['original'] == prepared.fixture.stored.snapshot
        return native_command(inv, policy, **(locations | {
            'snapshot': prepared.stage / 'original_snapshot',
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
        api._report(process(payload), prepared.invocation, prepared.inputs)


@pytest.mark.parametrize('fault', ['sdk', 'source', 'pins'])
def test_late_changes_cannot_return_success(flow: Any, fault: str) -> None:
    def authority(inputs: Any, inv: Any) -> None:
        flow.authorize(inputs, inv)
        if flow.authority_calls == 2:
            flow.fault = fault
    response = flow.execute(authority=authority)
    assert not response.ok and 'quiet' in flow.events


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
            prepared.invocation, prepared.inputs)


def test_generated_capture_cannot_become_original_v1(flow: Any, prepared: Any) -> None:
    prepared.fixture.stored = replace(prepared.fixture.stored, generated_metadata={
        'schema_version': 1, 'purpose': 'generated_acceptance', 'capture_phase': 'post_model',
        'user_id': str(prepared.inputs.scope.user_id), 'request_sha256': '1' * 64,
        'manifest_sha256': '2' * 64})
    response = flow.execute()
    assert not response.ok and 'launch' not in flow.events and flow.authority_calls == 0


def test_candidate_is_explicitly_unsupported(flow: Any, prepared: Any) -> None:
    request = prepared.request.model_copy(update={
        'invocation': prepared.invocation.model_copy(update={'source_kind': 'candidate'})})
    response = flow.execute(request)
    assert not response.ok and response.error == 'go_project_unsupported'
    assert 'stage' not in flow.events and 'launch' not in flow.events


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


def test_disk_budget_includes_rounding_entries_controls_and_reserve(
    api: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = 4096
    required = (prepared.invocation.source_limit_bytes + prepared.invocation.file_limit * 10 * block
                + 64 * 1024**2 + 1024**2 + prepared.invocation.workspace_limit_bytes)
    available = required + prepared.catalog.min_free_bytes
    monkeypatch.setattr(api.os, 'fstatvfs', lambda fd: SimpleNamespace(
        f_bavail=available // block, f_frsize=block), raising=False)
    api._disk_budget(20, prepared.invocation, prepared.catalog)
    available -= block
    with pytest.raises(ValueError, match='go_project_capacity'):
        api._disk_budget(20, prepared.invocation, prepared.catalog)


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
