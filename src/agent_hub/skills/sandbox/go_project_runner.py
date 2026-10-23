"""DynamicUser outer diagnostic chain; project code only runs in fixed bwrap.

Root must independently rebuild inputs, authorize execution and measure the
mounted SDK. Plan identity and the local executable hash checks below do not
attest the whole SDK. A typed overlay observes one negative test in a separate
capture; it is not business proof. Complete diagnostics remain incomplete.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Literal, Self, TextIO, cast

from pydantic import BaseModel, ConfigDict, model_validator

from agent_hub.capabilities.go_project_inputs import GoProjectInputs
from agent_hub.observability.logging import SENSITIVE_KEY_PATTERN, SENSITIVE_VALUE_PATTERN
from agent_hub.skills.sandbox.go_project_binary_export import (
    export_go_project_binary,
    measure_go_project_binary,
    seal_go_project_binary,
)
from agent_hub.skills.sandbox.go_project_business_runner import run_go_business_case
from agent_hub.skills.sandbox.go_project_canary import GoOverlayCanary, create_go_overlay_canary
from agent_hub.skills.sandbox.go_project_evidence import (
    GoEvidenceLimits,
    GoTestProcess,
    parse_go_overlay_canary_evidence,
    parse_go_test_evidence,
)
from agent_hub.skills.sandbox.go_project_inventory import (
    GoSourceInventory,
    parse_go_source_inventory,
)
from agent_hub.skills.sandbox.go_project_mounts import (
    GoInspectionParameters,
    build_go_phase_mounts,
    go_phase_command,
)
from agent_hub.skills.sandbox.go_project_protocol import GoProjectInvocation, _json
from agent_hub.skills.sandbox.project_contract_mounts import ContractWork, validate_contract_work
from agent_hub.skills.sandbox.project_runner import _cleanup_work, _copy_snapshot
from agent_hub.skills.sandbox.python_project_mounts import _file_stamp, _pin_directory, _Pinned
from agent_hub.skills.sandbox.python_project_process import (
    PYTHON_PHASE_OUTPUT_WITHHELD,
    PythonPhaseCleanupError,
    PythonPhaseProcessResult,
    run_python_phase,
)
from agent_hub.skills.sandbox.toolchain_protocol import Digest, ToolchainReference
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

_INPUT = Path('/run/agent-hub-go-source')
_ORIGINAL = Path('/run/agent-hub-go-original')
_SDK = Path('/run/agent-hub-go-sdk')
_PLAN = Path('/run/agent-hub-go-plan.json')
_EXPORT = Path('/run/agent-hub-go-export')
_WORK = Path('/run/agent-hub-go-work/owned')
_INSPECTION_HELPER = Path(__file__).with_name('go_project_inspect.go')
_MAX_PLAN_BYTES = 32 * 1024**2
_MAX_REPORT_BYTES = 32 * 1024**2
_DiagnosticStage = Literal['native', 'arguments', 'plan', 'run', 'encode', 'write']
_RUNNER_ERROR_CODES = frozenset({
    'go_project_runner_rejected', 'go_project_timeout', 'go_project_toolchain_changed',
    'go_project_build_output_invalid', 'go_project_output_incomplete',
    'go_project_build_output_changed', 'go_project_source_changed', 'go_project_business_failed',
    'go_project_inspection_helper_unavailable', 'go_project_inspection_failed',
    'go_project_acceptance_tests_changed', 'go_project_build_failed', 'go_project_test_failed',
    'go_project_canary_failed', 'go_project_cleanup_incomplete',
})


class GoProjectRunnerError(ValueError):
    """Runner-owned finite diagnostics, never copied from an OS exception."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _exception_diagnostic(error: BaseException) -> dict[str, str]:
    """Finite class names and optional identifiers; never format exception objects."""
    kinds = (GoProjectRunnerError, ProcessCleanupIncomplete, PythonPhaseCleanupError,
             FileNotFoundError, PermissionError, OSError, KeyError, AttributeError,
             OverflowError, ValueError, RuntimeError, TypeError, AssertionError, SystemExit)
    detail = {'exception_type': next(
        (kind.__name__ for kind in kinds if isinstance(error, kind)), 'Exception')}
    if (type(error) is GoProjectRunnerError and type(error.code) is str
            and error.code in _RUNNER_ERROR_CODES):
        detail['code'] = error.code
    identifier = None
    if type(error) is KeyError and len(error.args) == 1:
        identifier = error.args[0]
    elif type(error) is AttributeError:
        identifier = error.name
    if (type(identifier) is str and len(identifier) <= 64
            and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', identifier) is not None
            and not SENSITIVE_KEY_PATTERN.search(identifier)
            and not SENSITIVE_VALUE_PATTERN.search(identifier)):
        detail['identifier'] = identifier
    return detail


def _write_diagnostic(stream: TextIO, payload: dict[str, Any]) -> None:
    """Best effort private stderr, including when the sink raises a control exception."""
    try:
        raw = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n'
        if len(raw) <= 64 * 1024:
            stream.write(raw)
    except BaseException:  # noqa: BLE001 - diagnostics must never replace the original failure
        return


def _diagnostic(stage: _DiagnosticStage, error: BaseException) -> None:
    try:
        _write_diagnostic(sys.stderr, {'go_project_runner_diagnostic': {
            'stage': stage, **_exception_diagnostic(error)}})
    except BaseException:  # noqa: BLE001 - preserve the original CLI outcome even without stderr
        return


def _need(value: object, code: str = 'go_project_runner_rejected') -> None:
    if not value:
        raise GoProjectRunnerError(code)


def _native() -> None:
    _need(sys.platform == 'linux'
          and os.environ.get('AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX') == 'systemd')


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    _need(remaining > 0, 'go_project_timeout')
    return remaining


class GoProjectRunnerPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)

    invocation: GoProjectInvocation
    inputs: dict[str, object]
    source_sha256: Digest

    @model_validator(mode='after')
    def bound_plan(self) -> Self:
        inputs = GoProjectInputs.from_payload(self.inputs)
        invocation = GoProjectInvocation.model_validate_json(
            self.invocation.model_dump_json(warnings='error'), strict=True,
        )
        _need(inputs.scope == invocation.scope
              and inputs.original_reference.to_payload() == invocation.original_reference.model_dump()
              and inputs.toolchain == invocation.toolchain
              and inputs.sha256 == invocation.recipe_sha256
              and inputs.environment_input_sha256 == invocation.environment_input_sha256)
        _need(invocation.file_limit <= inputs.file_limit
              and invocation.source_limit_bytes <= inputs.source_limit_bytes)
        if invocation.source_kind == 'original':
            _need(self.source_sha256 == inputs.original_reference.input_source_sha256)
        return self


def parse_go_runner_plan(raw: bytes) -> GoProjectRunnerPlan:
    try:
        _json(raw, _MAX_PLAN_BYTES)
        return GoProjectRunnerPlan.model_validate_json(raw, strict=True)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError('go_project_runner_rejected') from None


def _read_plan() -> bytes:
    if sys.platform != 'linux':
        raise GoProjectRunnerError('go_project_runner_rejected')
    deadline = time.monotonic() + 10
    with ExitStack() as stack:
        pins: list[_Pinned] = []
        parent = _pin_directory(_PLAN.parent, stack, pins)
        fd = os.open(_PLAN.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent)
        stack.callback(os.close, fd)
        before = os.fstat(fd)
        _need(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
              and not before.st_mode & 0o022 and 0 < before.st_size <= _MAX_PLAN_BYTES)
        raw = bytearray()
        while True:
            _remaining(deadline)
            chunk = os.read(fd, min(1024**2, before.st_size - len(raw) + 1))
            if not chunk:
                break
            raw.extend(chunk)
            _need(len(raw) <= before.st_size)
        _need(len(raw) == before.st_size
              and _file_stamp(os.fstat(fd)) == _file_stamp(before)
              and _file_stamp(os.stat(_PLAN.name, dir_fd=parent, follow_symlinks=False))
              == _file_stamp(before))
        for pin in pins:
            pin.check()
        return bytes(raw)


def _binary(root: Path, name: str, *, owner: int, maximum: int,
            deadline: float, readonly: bool) -> tuple[str, int, bytes]:
    """No-follow bounded file observation, not a toolchain attestation."""
    if sys.platform != 'linux':
        raise GoProjectRunnerError('go_project_runner_rejected')
    with ExitStack() as stack:
        pins: list[_Pinned] = []
        parent = _pin_directory(root, stack, pins)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent)
        stack.callback(os.close, fd)
        before = os.fstat(fd)
        _need(stat.S_ISREG(before.st_mode) and before.st_uid == owner and before.st_nlink == 1
              and before.st_mode & 0o111 and not before.st_mode & 0o022
              and (not readonly or not before.st_mode & 0o222)
              and 0 < before.st_size <= maximum)
        digest, size, header = hashlib.sha256(), 0, b''
        while True:
            _remaining(deadline)
            chunk = os.read(fd, min(1024**2, before.st_size - size + 1))
            if not chunk:
                break
            size += len(chunk)
            _need(size <= before.st_size)
            digest.update(chunk)
            if len(header) < 20:
                header += chunk[:20 - len(header)]
        _need(size == before.st_size and _file_stamp(os.fstat(fd)) == _file_stamp(before)
              and _file_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) == _file_stamp(before))
        for pin in pins:
            pin.check()
        return digest.hexdigest(), size, header


def _check_sdk(reference: ToolchainReference, *, deadline: float) -> None:
    for name, expected in (('go', reference.binary_sha256), ('gofmt', reference.gofmt_sha256)):
        digest, _, _ = _binary(_SDK / 'bin', name, owner=0, maximum=reference.total_bytes,
                               deadline=deadline, readonly=True)
        _need(digest == expected, 'go_project_toolchain_changed')


def _built_binary(output: Path, *, maximum: int, deadline: float) -> dict[str, object]:
    if sys.platform != 'linux':
        raise GoProjectRunnerError('go_project_runner_rejected')
    digest, size, header = _binary(output, 'project', owner=os.getuid(), maximum=maximum,
                                   deadline=deadline, readonly=False)
    _need(header.startswith(b'\x7fELF\x02\x01') and len(header) == 20
          and int.from_bytes(header[18:20], 'little') == 62, 'go_project_build_output_invalid')
    return {'filename': 'project', 'sha256': digest, 'size': size}


def _source_view(inputs: GoProjectInputs, arguments: dict[str, Any]) -> ContractWork:
    view = validate_contract_work(**arguments)
    # Include every active/inactive/non-Go file and its executable-mode tag.
    inputs.require_current({file.path: file.sha256 for file in view._files})
    return view


def _complete_stdout(result: PythonPhaseProcessResult, *, maximum: int) -> bytes:
    raw = result.stdout.encode('utf-8')
    _need(not result.timed_out and result.exit_code is not None and not result.stdout_truncated
          and type(result.stdout_observed_bytes) is int
          and result.stdout_observed_bytes == len(raw) <= maximum,
          'go_project_output_incomplete')
    return raw


def _check_detail(phase: str, result: PythonPhaseProcessResult) -> dict[str, Any]:
    return {'phase': phase, 'status': 'failed', 'exit_code': result.exit_code,
            'timed_out': result.timed_out, 'stdout': result.stdout, 'stderr': result.stderr,
            'stdout_truncated': result.stdout_truncated, 'stderr_truncated': result.stderr_truncated,
            'stdout_observed_bytes': result.stdout_observed_bytes,
            'stderr_observed_bytes': result.stderr_observed_bytes}


def _business_cases(
    inputs: GoProjectInputs, invocation: GoProjectInvocation, report: dict[str, Any],
    arguments: dict[str, Any], output: Path, deadline: float,
) -> None:
    assert inputs.acceptance is not None and inputs.business is not None
    binary = report['binary']
    rows: list[dict[str, object]] = []
    report.update(acceptance_sha256=inputs.acceptance.sha256,
                  business_suite_sha256=inputs.business.sha256, business_evidence=rows)
    # Bound retained observations, including arbitrary-length original case IDs.
    remaining = _MAX_REPORT_BYTES // 2

    def collected(result: PythonPhaseProcessResult) -> None:
        report['checks'][-1].update(_check_detail('business', result))
        _trim_report_streams(report)

    for index, case in enumerate(inputs.business.cases):
        report['checks'][-1].clear()
        report['checks'][-1].update(phase='business', status='failed')
        _remaining(deadline)
        _source_view(inputs, arguments)
        _need(measure_go_project_binary(output, maximum=invocation.workspace_limit_bytes,
                                    deadline=deadline) == binary, 'go_project_build_output_changed')
        work = _WORK / ('business-' + str(index))
        digest = _copy_snapshot(_INPUT, work, deadline)
        _need(digest == report['source_sha256'], 'go_project_source_changed')
        case_arguments = {**arguments, 'work_root': work,
                          'source_timeout_seconds': min(3600.0, _remaining(deadline))}
        observation = run_go_business_case(case, work_root=work, binary_root=output,
            expected_binary_sha256=binary['sha256'], expected_binary_bytes=binary['size'],
            contract_arguments=case_arguments, deadline=deadline,
            output_limit_bytes=invocation.output_limit_bytes, on_collected=collected)
        # No cleanup on uncertain child drain: the outer owner must first quiesce.
        _source_view(inputs, case_arguments)
        _need(measure_go_project_binary(output, maximum=invocation.workspace_limit_bytes,
                                    deadline=deadline) == binary, 'go_project_build_output_changed')
        payload = observation.to_payload()
        remaining -= len(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True, allow_nan=False).encode('ascii')) + 1
        _need(remaining >= 0, 'go_project_output_incomplete')
        rows.append(payload)
        _cleanup_work(work, deadline)
        _need(observation.passed, 'go_project_business_failed')
    report['business_gate'] = 'completed'


def run_go_project(plan: GoProjectRunnerPlan) -> dict[str, Any]:
    _native()
    _need(type(plan) is GoProjectRunnerPlan)
    plan = parse_go_runner_plan(plan.model_dump_json(warnings='error').encode())
    invocation = plan.invocation
    inputs = GoProjectInputs.from_payload(plan.inputs)
    deadline = time.monotonic() + max(0.1, invocation.timeout_seconds - 2)
    # Sixteen bytes is a deliberately conservative per-Test source allocation;
    # event/subtest allocation is also explicit, bounded by captured output.
    packages, tests = invocation.file_limit, max(1, invocation.source_limit_bytes // 16)
    limits = GoEvidenceLimits(max_output_bytes=invocation.output_limit_bytes,
        max_events=tests * 8 + packages * 4, max_packages=packages, max_tests=tests)
    report: dict[str, Any] = {
        'schema_version': 1, 'kind': 'go_project', 'evidence': 'native_process_only',
        'status': 'incomplete', 'execution_id': invocation.execution_id,
        'scope': invocation.scope.to_payload(), 'source_kind': invocation.source_kind,
        'source_sha256': plan.source_sha256, 'recipe_sha256': inputs.sha256,
        'environment_input_sha256': inputs.environment_input_sha256,
        'original_reference': inputs.original_reference.to_payload(),
        'original_check_contract': inputs.contract.protected_sha256,
        'toolchain': inputs.toolchain.model_dump(mode='json'), 'toolchain_measurement': 'not_run',
        'checks': [], 'canary': 'not_run', 'business_gate': 'not_run', 'cache_reuse': 'none',
    }
    checks: list[dict[str, Any]] = report['checks']
    phase = 'source'
    inventory: GoSourceInventory | None = None
    try:
        _WORK.mkdir(mode=0o700)
        source, output = (_WORK / name for name in ('source', 'output'))
        output.mkdir(mode=0o700)
        digest = _copy_snapshot(_INPUT, source, deadline)
        _need(digest == plan.source_sha256, 'go_project_source_changed')
        arguments: dict[str, Any] = {
            'contract_payload': inputs.contract.to_payload(),
            'expected_contract_sha256': inputs.contract.protected_sha256,
            'original_root': _ORIGINAL, 'work_root': source, 'expected_source_sha256': digest,
            'ephemeral_dirs': (), 'source_file_limit': invocation.file_limit,
            'source_byte_limit': invocation.source_limit_bytes,
            'source_timeout_seconds': min(3600.0, _remaining(deadline)),
        }
        view = _source_view(inputs, arguments)
        expected_source = {file.path: (hashlib.sha256(file.data).hexdigest(), len(file.data))
                           for file in view._files}
        phase = 'toolchain'
        _check_sdk(inputs.toolchain, deadline=deadline)
        for selected in ('inspect', 'build', 'test', 'canary'):
            phase = 'source_integrity'
            arguments['source_timeout_seconds'] = min(3600.0, _remaining(deadline))
            view = _source_view(inputs, arguments)
            phase = selected
            cache = _WORK / ('cache-' + phase)
            cache.mkdir(mode=0o700)
            inspection = None
            canary: GoOverlayCanary | None = None
            if phase == 'inspect':
                _need(_INSPECTION_HELPER.is_file(), 'go_project_inspection_helper_unavailable')
                inspection = GoInspectionParameters(inputs.declaration.module_path,
                    invocation.file_limit, invocation.source_limit_bytes,
                    max(1, int(_remaining(deadline) * 1000)), packages, tests)
            elif phase == 'canary':
                canary = create_go_overlay_canary(project_execution_id=invocation.execution_id,
                    execution_id='go_canary_' + secrets.token_hex(16),
                    module_path=inputs.declaration.module_path, source_sha256=digest,
                    protected_sha256=inputs.contract.protected_sha256,
                    source_paths=tuple(file.path for file in view._files))
            with build_go_phase_mounts(phase, work_root=source, toolchain_root=_SDK,
                    output_root=output, cache_root=cache, contract_arguments=arguments,
                    inspection=inspection, canary=canary,
                    parallelism=max(1, (invocation.cpu_quota_percent + 99) // 100)) as mounts:
                _need(mounts.argv[0] == '/usr/bin/bwrap')
                result = run_python_phase(mounts.argv, cwd=source, deadline=deadline,
                    capture_limit_bytes=invocation.output_limit_bytes, pass_fds=mounts.pass_fds)
                check = _check_detail(phase, result)
                checks.append(check)
                _trim_report_streams(report)
            if phase == 'inspect':
                raw = _complete_stdout(result, maximum=limits.max_output_bytes)
                _need(result.exit_code == 0, 'go_project_inspection_failed')
                inventory = parse_go_source_inventory(raw,
                    expected_module=inputs.declaration.module_path, expected_source=expected_source,
                    max_files=invocation.file_limit, max_output_bytes=limits.max_output_bytes,
                    max_packages=packages, max_tests=tests)
                report['inventory'] = {'module_path': inventory.module_path,
                    'package_count': len(inventory.packages), 'test_count': len(inventory.expected_tests),
                    'source_go_file_count': len(inventory.source_go_files),
                    'report_sha256': hashlib.sha256(raw).hexdigest()}
                if inputs.acceptance is not None:
                    names = tuple(sorted(package + '::' + name
                                         for package, name in inventory.expected_tests))
                    _need(names == inputs.acceptance.test_ids, 'go_project_acceptance_tests_changed')
                    report['named_tests'] = list(names)
            elif phase == 'build':
                _need(result.exit_code == 0 and not result.timed_out, 'go_project_build_failed')
                report['binary'] = _built_binary(output, maximum=invocation.workspace_limit_bytes,
                                                  deadline=deadline)
                if inputs.business is not None:
                    _need(seal_go_project_binary(output, maximum=invocation.workspace_limit_bytes,
                        deadline=deadline) == report['binary'], 'go_project_build_output_changed')
            elif phase == 'test':
                _need(inventory is not None)
                assert inventory is not None
                raw = _complete_stdout(result, maximum=limits.max_output_bytes)
                expected = inventory.to_test_expectation(source_sha256=digest,
                    protected_sha256=inputs.contract.protected_sha256,
                    max_files=invocation.file_limit, max_packages=packages, max_tests=tests)
                assert result.exit_code is not None
                evidence = parse_go_test_evidence(raw, expected=expected,
                    process=GoTestProcess(invocation.execution_id, 'project_tests',
                        go_phase_command('test'), result.exit_code, result.timed_out,
                        result.stdout_truncated), limits=limits)
                report['test_evidence'] = {'status': 'completed' if evidence.status == 'passed'
                    else evidence.status, 'report_sha256': evidence.report_sha256,
                    'expected_sha256': evidence.expected_sha256,
                    'required_test_count': len(inventory.expected_tests),
                    'required_passed_count': len(evidence.required_passed),
                    'failed_count': sum(item.status == 'fail' for item in evidence.tests),
                    'test_count': len(evidence.tests), 'package_count': len(evidence.packages)}
                _need(evidence.status == 'passed', 'go_project_test_failed')
            else:
                assert canary is not None
                raw = _complete_stdout(result, maximum=limits.max_output_bytes)
                assert result.exit_code is not None
                negative = parse_go_overlay_canary_evidence(raw, canary=canary,
                    process=GoTestProcess(canary.execution_id, 'negative_canary', canary.command,
                        result.exit_code, result.timed_out, result.stdout_truncated), limits=limits)
                report['canary_evidence'] = {'execution_id': canary.execution_id,
                    'project_execution_id': invocation.execution_id,
                    'report_sha256': negative.evidence.report_sha256,
                    'expected_sha256': negative.evidence.expected_sha256,
                    'observed_expected_failure': negative.observed_expected_failure}
                _need(negative.observed_expected_failure, 'go_project_canary_failed')
                report['canary'] = 'completed'
            check['status'] = 'completed'
        if inputs.business is not None:
            phase = 'business'
            checks.append({'phase': phase, 'status': 'failed'})
            _business_cases(inputs, invocation, report, arguments, output, deadline)
            checks[-1]['status'] = 'completed'
        phase = 'source_integrity'
        arguments['source_timeout_seconds'] = min(3600.0, _remaining(deadline))
        _source_view(inputs, arguments)
        phase = 'toolchain'
        _check_sdk(inputs.toolchain, deadline=deadline)
        if inputs.business is not None:
            phase = 'export'
            binary = report['binary']
            _need(export_go_project_binary(output, _EXPORT,
                expected_sha256=binary['sha256'], expected_bytes=binary['size'],
                maximum=invocation.workspace_limit_bytes, deadline=deadline) == binary,
                'go_project_build_output_changed')
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        # Preserve collector diagnostics; exception messages may contain host paths.
        code = ('go_project_cleanup_incomplete' if isinstance(error, (PythonPhaseCleanupError, ProcessCleanupIncomplete))
                else 'go_project_timeout' if time.monotonic() >= deadline
                else error.code if isinstance(error, GoProjectRunnerError)
                else 'go_project_' + phase + '_failed')
        if not checks or checks[-1]['phase'] != phase:
            checks.append({'phase': phase, 'status': 'failed', 'error': code})
        else:
            checks[-1].update(status='failed', error=code)
        report.update(status='failed', failed_phase=phase)
        if phase == 'canary':
            report['canary'] = 'failed'
        if phase == 'business':
            report['business_gate'] = 'failed'
    return cast(dict[str, Any], json.loads(encode_go_report(report)))


def _trim_report_streams(report: dict[str, Any]) -> None:
    # Reserve bounded identity/summary metadata and worst-case JSON escaping.
    budget = (_MAX_REPORT_BYTES - min(1024**2, _MAX_REPORT_BYTES // 2)) // 6
    for row in report.get('checks', []):
        for name in ('stdout', 'stderr'):
            if name not in row:
                continue
            size = len(row[name].encode('utf-8'))
            if size > budget:
                row[name] = PYTHON_PHASE_OUTPUT_WITHHELD[:budget]
                row[name + '_preview_withheld'] = True
                report['diagnostics_withheld'] = True
                size = len(row[name].encode('utf-8'))
            budget -= size


def encode_go_report(report: dict[str, Any]) -> bytes:
    """One cap for all metadata and streams, below the v8 SkillResult limit."""
    _need(report.get('status') in {'incomplete', 'failed'})
    report = {**report, 'checks': [dict(row) for row in report.get('checks', [])]}
    _trim_report_streams(report)
    encoded = json.dumps(report, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                         allow_nan=False).encode('ascii')
    if len(encoded) + 1 > _MAX_REPORT_BYTES:
        report = {**report, 'diagnostics_withheld': True,
                  'checks': [{**row, **{name: PYTHON_PHASE_OUTPUT_WITHHELD
                              for name in ('stdout', 'stderr') if name in row}}
                             for row in report.get('checks', [])]}
        encoded = json.dumps(report, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                             allow_nan=False).encode('ascii')
    _need(len(encoded) + 1 <= _MAX_REPORT_BYTES)
    return encoded


def main() -> int:
    phase: _DiagnosticStage = 'native'
    try:
        _native()
        phase = 'arguments'
        _need(len(sys.argv) == 1)
        phase = 'plan'
        plan = parse_go_runner_plan(_read_plan())
        phase = 'run'
        report = run_go_project(plan)
        phase = 'encode'
        encoded = encode_go_report(report).decode('ascii') + '\n'
        phase = 'write'
        sys.stdout.write(encoded)
        return 0
    except Exception as error:  # noqa: BLE001 - never a raw traceback or host path
        _diagnostic(phase, error)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
