"""Bound runner orchestration tests; synthetic phases are NOT native Go proof."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.capabilities.go_project_inputs import bind_go_project_inputs
from agent_hub.skills.sandbox.go_project_mounts import go_phase_command
from agent_hub.skills.sandbox.go_project_protocol import GoProjectInvocation
from agent_hub.skills.sandbox.project import OriginalProjectReference
from agent_hub.skills.sandbox.project_runner import _copy_snapshot
from agent_hub.skills.sandbox.python_project_process import (
    PYTHON_PHASE_OUTPUT_WITHHELD,
    PythonPhaseCleanupError,
    PythonPhaseProcessResult,
)
from tests.unit.capabilities.test_go_project_acceptance import manifest
from tests.unit.capabilities.test_go_project_inputs import case
from tests.unit.skills.sandbox.test_go_project_evidence import event, happy, stream


@pytest.fixture
def api() -> Any:
    name = 'agent_hub.skills.sandbox.go_project_runner'
    assert importlib.util.find_spec(name) is not None, 'Go bound diagnostic runner missing'
    return importlib.import_module(name)


@pytest.fixture
def prepared(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Any]:
    original = tmp_path / 'original'
    original.mkdir()
    changes = {
        'assets/readme.txt': b'non-Go input also requires full source binding\n',
        'inactive_windows.go': b'package main\n',
    }
    if getattr(request, 'param', False):
        acceptance = replace(manifest(), editable_paths=('main.go', 'assets'))
        changes['ACCEPTANCE.json'] = json.dumps(acceptance.to_payload()).encode()
    fixture = case(original, editable=('main.go', 'assets'), changes=changes)
    inputs = bind_go_project_inputs(stored=fixture.stored, scope=fixture.scope,
        toolchain=fixture.sdk, source_limit_bytes=1024**2, file_limit=100)
    invocation = GoProjectInvocation(
        execution_id='go_runner', tenant_id=inputs.scope.tenant_id, run_id=inputs.scope.run_id,
        project_id=inputs.scope.project_id, session_id=inputs.scope.session_id,
        sandbox_profile='workspace_write', timeout_seconds=60, source_kind='original',
        original_reference=OriginalProjectReference.model_validate(
            inputs.original_reference.to_payload(), strict=True), scope=inputs.scope,
        toolchain=inputs.toolchain, recipe_sha256=inputs.sha256,
        environment_input_sha256=inputs.environment_input_sha256,
        source_limit_bytes=inputs.source_limit_bytes, file_limit=inputs.file_limit,
    )
    source = tmp_path / 'input'
    source.mkdir()
    for name, data in fixture.raw.items():
        target = source / name
        target.parent.mkdir(exist_ok=True, parents=True)
        target.write_bytes(data)
    for path in original.rglob('*'):
        path.chmod(0o555 if path.is_dir() else 0o444)
    original.chmod(0o555)
    try:
        yield SimpleNamespace(inputs=inputs, invocation=invocation, raw=fixture.raw,
            source=source, original=original,
            payload={'invocation': invocation, 'inputs': inputs.to_payload(),
                     'source_sha256': inputs.original_reference.input_source_sha256})
    finally:
        original.chmod(0o700)
        for path in original.rglob('*'):
            path.chmod(0o700 if path.is_dir() else 0o600)


def test_plan_binds_real_go_inputs_and_original_candidate_identity(api: Any, prepared: Any) -> None:
    plan = api.GoProjectRunnerPlan.model_validate(prepared.payload, strict=True)
    assert api.parse_go_runner_plan(plan.model_dump_json().encode()) == plan
    with pytest.raises(ValueError):
        api.GoProjectRunnerPlan.model_validate(prepared.payload | {'source_sha256': '9' * 64})
    payload = prepared.payload | {'source_sha256': '9' * 64,
        'invocation': prepared.invocation.model_copy(update={'source_kind': 'candidate'})}
    assert api.GoProjectRunnerPlan.model_validate(payload).source_sha256 == '9' * 64


@pytest.mark.parametrize('field,kind', [
    ('scope', 'user'), ('toolchain', 'binary'), ('original_reference', 'count'),
    ('recipe_sha256', 'digest'), ('environment_input_sha256', 'digest'),
    ('file_limit', 'files'), ('source_limit_bytes', 'bytes'),
])
def test_plan_rejects_rebinding_or_budget_escalation(
    api: Any, prepared: Any, field: str, kind: str,
) -> None:
    inv = prepared.invocation
    changes: dict[str, Any] = {'digest': '9' * 64, 'files': 101, 'bytes': 1024**2 + 1}
    changes['user'] = inv.scope.model_copy(update={'user_id': inv.scope.run_id})
    changes['binary'] = inv.toolchain.model_copy(update={'binary_sha256': '9' * 64})
    changes['count'] = inv.original_reference.model_copy(update={'file_count': 99})
    with pytest.raises(ValueError):
        api.GoProjectRunnerPlan.model_validate(prepared.payload | {
            'invocation': inv.model_copy(update={field: changes[kind]}),
        }, strict=True)


@pytest.mark.parametrize('level', ['plan', 'inputs', 'invocation'])
def test_unknown_execution_paths_commands_and_flags_never_enter_plan(
    api: Any, prepared: Any, level: str,
) -> None:
    plan = api.GoProjectRunnerPlan.model_validate(prepared.payload).model_dump(mode='json')
    target = plan if level == 'plan' else plan[level]
    target['command'] = '/host/go -race'
    with pytest.raises(ValueError):
        api.parse_go_runner_plan(json.dumps(plan).encode())


@pytest.mark.parametrize('damage', ['duplicate', 'nested', 'nan', 'infinity', 'exponent', 'utf8', 'size'])
def test_plan_json_rejects_ambiguous_nonfinite_and_oversized_payloads(
    api: Any, prepared: Any, damage: str,
) -> None:
    raw = api.GoProjectRunnerPlan.model_validate(prepared.payload).model_dump_json().encode()
    if damage == 'duplicate':
        raw = raw[:-1] + b',"source_sha256":"' + b'9' * 64 + b'"}'
    elif damage == 'nested':
        raw = raw.replace(b'"file_limit":100', b'"file_limit":100,"file_limit":100', 1)
    elif damage == 'utf8':
        raw = b'\xff'
    elif damage == 'size':
        raw = b' ' * (32 * 1024**2 + 1)
    else:
        number = {'nan': b'NaN', 'infinity': b'Infinity', 'exponent': b'1e9999'}[damage]
        raw = raw.replace(b'"file_limit":100', b'"file_limit":' + number, 1)
    with pytest.raises(ValueError, match='go_project_runner_rejected'):
        api.parse_go_runner_plan(raw)


def test_runner_and_main_fail_closed_without_native_outer_context(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX', raising=False)
    with pytest.raises(ValueError):
        api.run_go_project(None)
    assert api.main() == 1


def collected(stdout: str = '', **changes: Any) -> PythonPhaseProcessResult:
    return replace(PythonPhaseProcessResult(0, False, stdout, '', False, False,
                                           len(stdout.encode()), 0), **changes)


@pytest.fixture
def orchestration(api: Any, prepared: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(api, 'sys', SimpleNamespace(platform='linux', argv=['go_project_runner']))
    monkeypatch.setenv('AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX', 'systemd')
    paths = {'INPUT': prepared.source, 'ORIGINAL': prepared.original,
             'SDK': tmp_path / 'sdk', 'WORK': tmp_path / 'work' / 'owned',
             'EXPORT': tmp_path / 'export', 'PLAN': tmp_path / 'plan.json'}
    paths['WORK'].parent.mkdir()
    paths['EXPORT'].mkdir()
    for name, path in paths.items():
        monkeypatch.setattr(api, '_' + name, path)
    state = SimpleNamespace(plan=api.GoProjectRunnerPlan.model_validate(prepared.payload),
        paths=paths, prepared=prepared, phases=[], mounts=[], open=False, fault=None,
        process_changes={}, source_checks=[], sdk_checks=0, canary=None)
    original_validate = api.validate_contract_work

    def validate(**arguments: Any) -> Any:
        state.source_checks.append(arguments)
        assert arguments['source_file_limit'] == 100
        assert arguments['source_byte_limit'] == 1024**2
        assert 0 < arguments['source_timeout_seconds'] <= 60
        return original_validate(**arguments)

    def sdk(*args: Any, **kwargs: Any) -> None:
        state.sdk_checks += 1
        if state.fault == 'sdk':
            raise ValueError('private SDK failure not for the report')

    @contextmanager
    def mounts(phase: str, **arguments: Any) -> Iterator[Any]:
        assert not state.open
        assert arguments['toolchain_root'] == paths['SDK']
        assert arguments['work_root'] == paths['WORK'] / 'source'
        assert arguments['cache_root'] == paths['WORK'] / ('cache-' + phase)
        assert arguments['cache_root'].is_dir()
        assert arguments['output_root'] == paths['WORK'] / 'output'
        assert arguments['contract_arguments']['ephemeral_dirs'] == ()
        assert arguments['parallelism'] == max(1, (state.plan.invocation.cpu_quota_percent + 99) // 100)
        assert paths['EXPORT'] not in arguments.values()
        state.mounts.append(arguments)
        if phase == 'inspect':
            parameters = arguments['inspection']
            assert parameters.module_path == 'example.org/demo'
            assert (parameters.file_limit, parameters.byte_limit) == (100, 1024**2)
            assert (parameters.max_packages, parameters.max_tests) == (100, 65536)
            assert 0 < parameters.timeout_ms <= 60000
        if phase == 'canary':
            canary = arguments['canary']
            state.canary = canary
            assert canary.project_execution_id == state.plan.invocation.execution_id
            assert canary.execution_id != canary.project_execution_id
            assert canary.source_sha256 == state.plan.source_sha256
            assert canary.protected_sha256 == prepared.inputs.contract.protected_sha256
        state.open = True
        try:
            yield SimpleNamespace(argv=('/usr/bin/bwrap', phase), pass_fds=(20, 21))
        finally:
            state.open = False
            if state.fault == phase + '-mount-exit':
                raise ValueError('private late pin check failed')

    def phase(command: tuple[str, ...], **arguments: Any) -> Any:
        assert state.open and command[0] == '/usr/bin/bwrap'
        name = command[1]
        state.phases.append(name)
        assert arguments['pass_fds'] == (20, 21)
        assert arguments['cwd'] == paths['WORK'] / 'source'
        assert arguments['capture_limit_bytes'] == state.plan.invocation.output_limit_bytes
        if state.fault == name + '-cleanup':
            raise PythonPhaseCleanupError('sensitive cleanup detail')
        if name == 'inspect':
            rows = [{'path': p, 'raw_sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)}
                    for p, data in sorted(prepared.raw.items())
                    if p.endswith('.go') and not p.endswith('_windows.go')]
            payload: dict[str, Any] = {'schema_version': 1, 'kind': 'go_source_inventory_v1',
                'module_path': 'example.org/demo', 'project_main_package': 'example.org/demo',
                'packages': ['example.org/demo'],
                'expected_tests': [{'package': 'example.org/demo', 'name': 'TestMainLogic'}],
                'source_go_files': rows}
            if state.fault == 'inventory':
                payload['source_go_files'][0]['raw_sha256'] = '9' * 64
            result = collected(json.dumps(payload))
        elif name == 'canary':
            canary = state.canary
            events = [event('start', package=canary.module_path),
                event('run', canary.test_name, canary.module_path),
                event('fail', canary.test_name, canary.module_path),
                event('fail', package=canary.module_path)]
            if state.fault == 'canary-passed':
                events = happy(canary.module_path, canary.test_name)
            elif state.fault == 'canary-build-failed':
                events = [event('start', package=canary.module_path),
                          event('fail', package=canary.module_path, FailedBuild=canary.module_path)]
            result = collected(stream(*events).decode(), exit_code=0 if state.fault == 'canary-passed'
                               else 2 if state.fault == 'canary-exit-two' else 1)
        elif name == 'test':
            events = happy('example.org/demo', 'TestMainLogic')
            if state.fault == 'test-failed':
                events[2] = event('fail', 'TestMainLogic', 'example.org/demo')
                events[3] = event('fail', package='example.org/demo')
            result = collected(stream(*events).decode(), exit_code=1 if state.fault == 'test-failed' else 0)
        else:
            result = collected()
        if state.fault == name:
            result = replace(result, exit_code=2, stderr='bounded compiler diagnostic\n',
                             stderr_observed_bytes=28)
        if state.fault == 'source-after-test' and name == 'test':
            (paths['WORK'] / 'source' / 'main_test.go').write_bytes(b'changed protected source')
        return replace(result, **state.process_changes.get(name, {}))

    monkeypatch.setattr(api, 'validate_contract_work', validate)
    monkeypatch.setattr(api, '_check_sdk', sdk)
    monkeypatch.setattr(api, '_built_binary', lambda *args, **kwargs: {'filename': 'project', 'sha256': '8' * 64, 'size': 100})
    monkeypatch.setattr(api, 'build_go_phase_mounts', mounts)
    monkeypatch.setattr(api, 'run_python_phase', phase)
    return state


def test_complete_diagnostic_chain_never_claims_delivery_pass(api: Any, orchestration: Any) -> None:
    state = orchestration
    report = api.run_go_project(state.plan)
    assert state.phases == ['inspect', 'build', 'test', 'canary']
    assert not state.open and state.sdk_checks >= 1
    assert report['status'] == 'incomplete'
    assert report['canary'] == 'completed' and report['business_gate'] == 'not_run'
    assert [row['status'] for row in report['checks']] == ['completed'] * 4
    assert report['source_sha256'] == state.plan.source_sha256
    assert report['test_evidence']['required_test_count'] == 1
    assert report['inventory']['source_go_file_count'] == 2
    assert not list(state.paths['EXPORT'].iterdir())
    assert report['cache_reuse'] == 'none'
    caches = [row['cache_root'] for row in state.mounts]
    assert len(caches) == len(set(caches)) == 4
    assert '"passed"' not in json.dumps(report)
    assert len(api.encode_go_report(report)) <= 32 * 1024**2


@pytest.fixture
def business_orchestration(
    api: Any, orchestration: Any, monkeypatch: pytest.MonkeyPatch,
) -> Any:
    state = orchestration
    state.business_calls = []
    state.exports = []
    state.binary_checks = []
    state.business_fault = None
    state.binary = {'filename': 'project', 'sha256': '8' * 64, 'size': 100}

    def seal(output: Path, **arguments: Any) -> Any:
        assert output == state.paths['WORK'] / 'output'
        state.binary_checks.append(arguments)
        return dict(state.binary)

    def export(output: Path, destination: Path, **arguments: Any) -> Any:
        assert len(state.business_calls) == 2
        state.exports.append((output, destination, arguments))
        return dict(state.binary)

    def business(case: Any, **arguments: Any) -> Any:
        from agent_hub.skills.sandbox.go_project_business_runner import GoBusinessObservation

        state.business_calls.append((case, arguments))
        assert state.phases == ['inspect', 'build', 'test', 'canary']
        assert arguments['binary_root'] == state.paths['WORK'] / 'output'
        assert arguments['expected_binary_sha256'] == state.binary['sha256']
        assert arguments['expected_binary_bytes'] == state.binary['size']
        work = arguments['work_root']
        assert work.is_dir() and work != state.paths['WORK'] / 'source'
        assert (work / 'ACCEPTANCE.json').read_bytes() == state.prepared.raw['ACCEPTANCE.json']
        assert arguments['contract_arguments']['work_root'] == work
        assert state.paths['EXPORT'] not in arguments.values()
        if state.business_fault == 'cleanup' or (
                state.business_fault == 'second-cleanup' and len(state.business_calls) == 2):
            from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

            raise ProcessCleanupIncomplete('private cgroup detail')
        passed = state.business_fault != 'mismatch'
        callback = arguments.get('on_collected')
        if callback is not None:
            callback(collected(case.expected_stdout if passed else 'actual unexpected result\n',
                               exit_code=case.expected_exit))
        return GoBusinessObservation(case_id=case.id, criterion_id=case.criterion_id,
            case_sha256=hashlib.sha256(json.dumps(case.to_payload(), sort_keys=True,
                separators=(',', ':'), ensure_ascii=True).encode()).hexdigest(),
            expected_exit=case.expected_exit, exit_code=case.expected_exit, timed_out=False,
            stdout_sha256=hashlib.sha256(case.expected_stdout.encode()).hexdigest(),
            stderr_sha256=hashlib.sha256(case.expected_stderr.encode()).hexdigest(),
            stdout_observed_bytes=len(case.expected_stdout.encode()),
            stderr_observed_bytes=len(case.expected_stderr.encode()),
            stdout_truncated=False, stderr_truncated=False,
            stdout_utf8_complete=True, stderr_utf8_complete=True,
            stdout_matches=passed, stderr_matches=True, passed=passed,
            reason=None if passed else 'stdout_mismatch')

    monkeypatch.setattr(api, 'seal_go_project_binary', seal, raising=False)
    monkeypatch.setattr(api, 'measure_go_project_binary', seal, raising=False)
    monkeypatch.setattr(api, 'export_go_project_binary', export, raising=False)
    monkeypatch.setattr(api, 'run_go_business_case', business, raising=False)
    return state


@pytest.mark.parametrize('prepared', [True], indirect=True)
def test_business_profile_runs_actual_case_adapter_before_export_without_self_attesting(
    api: Any, business_orchestration: Any,
) -> None:
    state = business_orchestration
    report = api.run_go_project(state.plan)
    assert len(state.business_calls) == 2
    assert len(state.exports) == 1
    assert report['status'] == 'incomplete' and report['toolchain_measurement'] == 'not_run'
    assert report['business_gate'] == 'completed'
    assert report['acceptance_sha256'] == state.prepared.inputs.acceptance.sha256
    assert report['business_suite_sha256'] == state.prepared.inputs.business.sha256
    assert report['named_tests'] == ['example.org/demo::TestMainLogic']
    assert len(report['business_evidence']) == 2
    assert len(state.binary_checks) >= 3
    assert [row['phase'] for row in report['checks']] == ['inspect', 'build', 'test', 'canary', 'business']


@pytest.mark.parametrize('prepared', [True], indirect=True)
@pytest.mark.parametrize('fault,code', [
    ('mismatch', 'go_project_business_failed'), ('cleanup', 'go_project_cleanup_incomplete'),
])
def test_business_failure_or_cleanup_uncertainty_cannot_export_a_binary(
    api: Any, business_orchestration: Any, fault: str, code: str,
) -> None:
    state = business_orchestration
    state.business_fault = fault
    report = api.run_go_project(state.plan)
    assert report['status'] == 'failed' and report['failed_phase'] == 'business'
    assert report['business_gate'] == 'failed'
    assert report['checks'][-1]['error'] == code
    assert len(state.business_calls) == 1 and not state.exports
    assert 'private cgroup' not in json.dumps(report)


@pytest.mark.parametrize('prepared', [True], indirect=True)
def test_business_mismatch_keeps_actual_bounded_diagnostic_output_for_autonomous_repair(
    api: Any, business_orchestration: Any,
) -> None:
    state = business_orchestration
    state.business_fault = 'mismatch'
    report = api.run_go_project(state.plan)
    check = report['checks'][-1]
    assert check['phase'] == 'business' and check['status'] == 'failed'
    assert check['stdout'] == 'actual unexpected result\n'
    assert check['stdout_observed_bytes'] == len(check['stdout'].encode())
    assert report['business_evidence'][0]['reason'] == 'stdout_mismatch'
    assert 'stdout' not in report['business_evidence'][0]
    assert not state.exports


@pytest.mark.parametrize('prepared', [True], indirect=True)
def test_next_case_cleanup_does_not_retain_previous_case_diagnostics(
    api: Any, business_orchestration: Any,
) -> None:
    state = business_orchestration
    state.business_fault = 'second-cleanup'
    report = api.run_go_project(state.plan)
    assert len(state.business_calls) == 2
    assert report['checks'][-1]['error'] == 'go_project_cleanup_incomplete'
    assert 'stdout' not in report['checks'][-1]
    assert 'stderr' not in report['checks'][-1]
    assert not state.exports


@pytest.mark.parametrize('prepared', [True], indirect=True)
def test_manifest_named_tests_must_match_authenticated_inventory_before_build(
    api: Any, orchestration: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = api.parse_go_source_inventory

    def inventory(*args: Any, **kwargs: Any) -> Any:
        return replace(real(*args, **kwargs), expected_tests=(('example.org/demo', 'TestOther'),))

    monkeypatch.setattr(api, 'parse_go_source_inventory', inventory)
    report = api.run_go_project(orchestration.plan)
    assert report['status'] == 'failed' and report['failed_phase'] == 'inspect'
    assert report['checks'][-1]['error'] == 'go_project_acceptance_tests_changed'
    assert orchestration.phases == ['inspect']


@pytest.mark.parametrize('fault,failed_phase,executed', [
    ('inspect', 'inspect', ['inspect']), ('inventory', 'inspect', ['inspect']),
    ('build', 'build', ['inspect', 'build']), ('test', 'test', ['inspect', 'build', 'test']),
    ('test-failed', 'test', ['inspect', 'build', 'test']),
    ('build-cleanup', 'build', ['inspect', 'build']), ('sdk', 'toolchain', []),
    ('source-after-test', 'source_integrity', ['inspect', 'build', 'test']),
])
def test_failures_stop_at_actual_stage_and_keep_bounded_diagnostics(
    api: Any, orchestration: Any, fault: str, failed_phase: str, executed: list[str],
) -> None:
    state = orchestration
    state.fault = fault
    report = api.run_go_project(state.plan)
    assert report['status'] == 'failed' and report['failed_phase'] == failed_phase
    assert state.phases == executed and not state.open
    assert report['checks'][-1]['status'] == 'failed'
    assert report['canary'] == report['business_gate'] == 'not_run'
    assert 'sensitive cleanup detail' not in json.dumps(report)
    if fault in {'inspect', 'build', 'test'}:
        assert report['checks'][-1]['stderr'] == 'bounded compiler diagnostic\n'
    if fault == 'test-failed':
        assert report['test_evidence']['status'] == 'test_failed'
        assert report['test_evidence']['required_test_count'] == 1
        assert report['test_evidence']['required_passed_count'] == 0
        assert report['test_evidence']['failed_count'] == 1


@pytest.mark.parametrize('phase', ['inspect', 'test'])
@pytest.mark.parametrize('patch', [
    {'timed_out': True}, {'stdout_truncated': True, 'stdout': PYTHON_PHASE_OUTPUT_WITHHELD},
    {'stdout': '{}'}, {'exit_code': None},
])
def test_incomplete_capture_is_failed_not_diagnostic_success(
    api: Any, orchestration: Any, phase: str, patch: dict[str, Any],
) -> None:
    state = orchestration
    state.process_changes[phase] = patch
    report = api.run_go_project(state.plan)
    assert report['status'] == 'failed' and report['failed_phase'] == phase


def test_verbose_stderr_alone_is_not_a_build_failure(api: Any, orchestration: Any) -> None:
    orchestration.process_changes['build'] = {'stderr': PYTHON_PHASE_OUTPUT_WITHHELD,
        'stderr_truncated': True, 'stderr_observed_bytes': 9999999}
    assert api.run_go_project(orchestration.plan)['status'] == 'incomplete'


def test_late_mount_recheck_failure_keeps_already_collected_phase_diagnostics(
    api: Any, orchestration: Any,
) -> None:
    orchestration.fault = 'build-mount-exit'
    orchestration.process_changes['build'] = {'stderr': 'diagnostic before close\n',
                                            'stderr_observed_bytes': 24}
    report = api.run_go_project(orchestration.plan)
    assert report['status'] == 'failed' and report['failed_phase'] == 'build'
    assert report['checks'][-1]['stderr'] == 'diagnostic before close\n'
    assert 'private late pin' not in json.dumps(report)


def test_all_source_files_are_checked_even_when_inventory_omits_inactive_and_non_go(
    api: Any, orchestration: Any,
) -> None:
    state = orchestration
    inv = state.plan.invocation.model_copy(update={'source_kind': 'candidate'})
    # A protected inactive control file cannot be changed by binding a new whole-tree digest.
    (state.prepared.source / 'inactive_windows.go').write_bytes(b'package main\n//changed\n')
    temporary = state.paths['WORK'].parent / 'digest-copy'
    digest = _copy_snapshot(state.prepared.source, temporary, time.monotonic() + 5)
    plan = state.plan.model_copy(update={'invocation': inv, 'source_sha256': digest})
    assert api.run_go_project(plan)['status'] == 'failed'
    assert not state.phases


@pytest.mark.parametrize('quota', [1, 100, 101, 250, 400])
def test_parallelism_is_bounded_from_cpu_quota_without_new_argv_flags(
    api: Any, orchestration: Any, quota: int,
) -> None:
    state = orchestration
    state.plan = state.plan.model_copy(update={'invocation': state.plan.invocation.model_copy(
        update={'cpu_quota_percent': quota})})
    assert api.run_go_project(state.plan)['status'] == 'incomplete'
    assert len(state.mounts) == 4


@pytest.mark.parametrize('phase', ['inspect', 'test', 'canary'])
def test_lossy_utf8_capture_is_rejected_before_it_can_become_valid_evidence(
    api: Any, orchestration: Any, phase: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = api.run_python_phase

    def collect(command: tuple[str, ...], **arguments: Any) -> Any:
        result = real(command, **arguments)
        return (replace(result, stdout_observed_bytes=result.stdout_observed_bytes + 1)
                if command[1] == phase else result)

    monkeypatch.setattr(api, 'run_python_phase', collect)
    report = api.run_go_project(orchestration.plan)
    assert report['status'] == 'failed' and report['failed_phase'] == phase
    assert report['checks'][-1]['stdout_observed_bytes'] > len(report['checks'][-1]['stdout'].encode())


@pytest.mark.parametrize('fault', ['canary-passed', 'canary-build-failed', 'canary-exit-two', 'canary-cleanup'])
def test_canary_requires_its_single_exact_named_failure_with_exit_one(
    api: Any, orchestration: Any, fault: str,
) -> None:
    orchestration.fault = fault
    report = api.run_go_project(orchestration.plan)
    assert report['status'] == 'failed' and report['failed_phase'] == 'canary'
    assert report['canary'] == 'failed' and report['business_gate'] == 'not_run'
    assert orchestration.phases == ['inspect', 'build', 'test', 'canary']


def test_canary_binds_complete_source_and_uses_separate_capture_identity(
    api: Any, orchestration: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_create = api.create_go_overlay_canary
    real_parse = api.parse_go_overlay_canary_evidence
    seen: list[Any] = []

    def create(**arguments: Any) -> Any:
        assert set(arguments['source_paths']) == set(orchestration.prepared.raw)
        return real_create(**arguments)

    def parse(raw: bytes, **arguments: Any) -> Any:
        canary, process, limits = (arguments[name] for name in ('canary', 'process', 'limits'))
        assert process.execution_id == canary.execution_id != 'go_runner'
        assert process.purpose == 'negative_canary' and process.argv == canary.command
        assert (limits.max_packages, limits.max_tests) == (100, 65536)
        seen.append(canary)
        return real_parse(raw, **arguments)

    monkeypatch.setattr(api, 'create_go_overlay_canary', create)
    monkeypatch.setattr(api, 'parse_go_overlay_canary_evidence', parse)
    report = api.run_go_project(orchestration.plan)
    assert report['status'] == 'incomplete' and report['canary'] == 'completed'
    assert report['toolchain_measurement'] == 'not_run' and len(seen) == 1


def test_missing_trusted_helper_is_constant_inspect_failure_without_phase_launch(
    api: Any, orchestration: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, '_INSPECTION_HELPER', orchestration.paths['WORK'] / 'missing.go')
    report = api.run_go_project(orchestration.plan)
    assert report['status'] == 'failed' and report['failed_phase'] == 'inspect'
    assert report['checks'][-1]['error'] == 'go_project_inspection_helper_unavailable'
    assert not orchestration.phases


def test_one_report_cap_covers_every_phase_stream_and_metadata(
    api: Any, orchestration: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = api.run_go_project(orchestration.plan)
    for row in report['checks']:
        row['stderr'] = '\x01' * 2000
        row['stderr_observed_bytes'] = 2000
    monkeypatch.setattr(api, '_MAX_REPORT_BYTES', 6000)
    encoded = api.encode_go_report(report)
    assert len(encoded) + 1 <= 6000
    bounded = json.loads(encoded)
    assert bounded['status'] == 'incomplete' and bounded['diagnostics_withheld'] is True
    assert all(row['stderr_observed_bytes'] == 2000 for row in bounded['checks'])
    with pytest.raises(ValueError):
        api.encode_go_report(report | {'status': 'passed'})


def test_oversized_streams_are_withheld_before_json_expansion(
    api: Any, orchestration: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = api.run_go_project(orchestration.plan)
    for row in report['checks']:
        row['stderr'] = '\x01' * 2000
    monkeypatch.setattr(api, '_MAX_REPORT_BYTES', 6000)
    real = json.dumps

    def dumps(value: Any, **arguments: Any) -> str:
        if isinstance(value, dict) and value.get('kind') == 'go_project':
            retained = sum(len(row.get(name, '').encode('utf-8'))
                           for row in value['checks'] for name in ('stdout', 'stderr'))
            assert retained * 6 < 6000, 'unbounded diagnostic expansion reached the encoder'
        return real(value, **arguments)

    monkeypatch.setattr(api.json, 'dumps', dumps)
    assert len(api.encode_go_report(report)) < 6000


def test_evidence_uses_exact_fixed_go_argv_and_caller_limits(
    api: Any, orchestration: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = api.parse_go_test_evidence
    seen: list[Any] = []

    def parse(raw: bytes, **arguments: Any) -> Any:
        seen.append(arguments)
        assert arguments['process'].argv == go_phase_command('test')
        assert arguments['process'].execution_id == 'go_runner'
        limits = arguments['limits']
        assert (limits.max_packages, limits.max_tests) == (100, 65536)
        assert limits.max_output_bytes == orchestration.plan.invocation.output_limit_bytes
        assert limits.max_events > limits.max_tests
        return real(raw, **arguments)

    monkeypatch.setattr(api, 'parse_go_test_evidence', parse)
    assert api.run_go_project(orchestration.plan)['status'] == 'incomplete'
    assert len(seen) == 1


def test_main_prints_only_bounded_report_and_hides_invalid_plan_details(
    api: Any, prepared: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(api, 'sys', SimpleNamespace(platform='linux', argv=['go_project_runner'],
                                                   stdout=sys.stdout, stderr=sys.stderr))
    monkeypatch.setenv('AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX', 'systemd')
    raw = api.GoProjectRunnerPlan.model_validate(prepared.payload).model_dump_json().encode()
    monkeypatch.setattr(api, '_read_plan', lambda: raw)
    monkeypatch.setattr(api, 'run_go_project', lambda plan: {'status': 'incomplete', 'canary': 'not_run', 'business_gate': 'not_run'})
    assert api.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out)['status'] == 'incomplete' and output.err == ''
    monkeypatch.setattr(api, '_read_plan', lambda: b'{"private":"/root/secret","private":0}')
    assert api.main() == 1
    output = capsys.readouterr()
    assert output.out == '' and '/root/secret' not in output.err


@pytest.mark.skipif(sys.platform != 'linux', reason='real Linux FD tests; Windows is not native PASS')
def test_native_plan_reader_requires_root_owned_single_link_nofollow(
    api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != 'linux' or os.getuid() != 0:
        pytest.skip('root-owned fixture requires root; no native plan proof')
    path = tmp_path / 'plan.json'
    path.write_bytes(b'{}')
    path.chmod(0o444)
    monkeypatch.setattr(api, '_PLAN', path)
    assert api._read_plan() == b'{}'
    alias = tmp_path / 'alias'
    alias.symlink_to(path)
    monkeypatch.setattr(api, '_PLAN', alias)
    with pytest.raises((OSError, ValueError)):
        api._read_plan()
    alias.unlink()
    os.link(path, alias)
    monkeypatch.setattr(api, '_PLAN', path)
    with pytest.raises(ValueError):
        api._read_plan()
