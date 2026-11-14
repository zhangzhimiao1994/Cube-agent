"""Finite failure hints survive discarded stderr; never execution/PASS authority."""
from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from agent_hub.capabilities.python_project_reports import _check
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.python_project_process import (
    PYTHON_PHASE_OUTPUT_WITHHELD,
    _Capture,
)
from tests.unit.capabilities.test_verification_failure_diagnostics_trailer import evidence


def load(name: str, filename: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    if not path.is_file():
        target = {
            'python_project_phase.py': 'agent_hub.skills.sandbox.python_project_phase',
            'verification_failure_diagnostics.py': 'agent_hub.capabilities.verification_failure_diagnostics',
        }[filename]
        return importlib.import_module(target)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def phase() -> ModuleType:
    return load('_failure_hint_phase', 'python_project_phase.py')


@pytest.fixture
def diagnosis() -> ModuleType:
    return load('_failure_hint_diagnosis', 'verification_failure_diagnostics.py')


def actual_failure_stdout(
    phase: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> tuple[str, str]:
    output, workspace = tmp_path / 'output', tmp_path / 'workspace'
    site = output / 'project-site'
    site.mkdir(parents=True)
    tests = workspace / 'tests'
    tests.mkdir(parents=True)
    (site / 'example_failure_hint.py').write_text('VALUE = 1\n')
    (tests / 'test_failure_hint.py').write_text(
        'import unittest\nclass Broken(unittest.TestCase):\n'
        '    def test_failure(self):\n        self.assertEqual(1, 2)\n')
    # Execute real _test, unittest, run_phase and main; only Linux isolation is
    # replaced for this owned unit fixture. No native launcher is invoked.
    monkeypatch.setattr(phase, '_isolation_guard', lambda: None)
    monkeypatch.setattr(phase, '_mounts', lambda: None)
    monkeypatch.setattr(phase, '_environment_interpreter', lambda: None)
    monkeypatch.setattr(phase, '_directory', lambda path: path.stat())
    monkeypatch.setattr(phase, '_OUTPUT', output)
    monkeypatch.setattr(phase, '_WORKSPACE', workspace)
    monkeypatch.setattr(phase, '_phase_output', nullcontext)
    try:
        assert phase.main(['test', '--import-module', 'example_failure_hint']) == 1
    finally:
        sys.modules.pop('example_failure_hint', None)
        sys.modules.pop('test_failure_hint', None)
    captured = capsys.readouterr()
    assert 'Ran 1 test' in captured.err and 'FAILED (failures=1)' in captured.err
    return captured.out, captured.err


def discarded_stderr_evidence(stdout: str, stderr: str) -> tuple[dict[str, JsonValue], Mapping[str, JsonValue]]:
    capture = _Capture(4096)
    capture.feed(('PRIVATE_OVERSIZE_' + 'x' * 8192 + '\n' + stderr).encode())
    assert capture.truncated and capture.observed > 8192
    assert capture.text() == PYTHON_PHASE_OUTPUT_WITHHELD
    _, _, diagnostic = _check({
        'phase': 'test', 'status': 'failed', 'exit_code': 1, 'timed_out': False,
        'stdout': stdout, 'stderr': capture.text(), 'stdout_truncated': False,
        'stderr_truncated': True, 'stdout_observed_bytes': len(stdout.encode()),
        'stderr_observed_bytes': capture.observed,
    }, phase='test', actual_phase='test', import_module='example', limit=4096)
    preview = diagnostic.to_payload()
    report, _ = evidence('')
    checks = cast(tuple[dict[str, JsonValue], ...], report['checks'])
    checks[-1]['execution_diagnostic'] = {
        key: value for key, value in preview.items() if key not in {'stdout', 'stderr'}}
    return report, preview


def test_actual_unittest_failure_hint_survives_native_stderr_overflow(
    phase: ModuleType, diagnosis: ModuleType, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    stdout, stderr = actual_failure_stdout(phase, monkeypatch, capsys, tmp_path)
    report, preview = discarded_stderr_evidence(stdout, stderr)
    assert preview['stderr'] == PYTHON_PHASE_OUTPUT_WITHHELD
    result = diagnosis.verification_failure_diagnosis(report, preview)
    assert result['phase_failure_hint'] == {
        'phase': 'test', 'stage': 'test_execution', 'cause': 'test_result_failed',
        'exception_type': 'PhaseError', 'counts': {'tests': 1, 'failures': 1, 'errors': 0, 'skipped': 0},
    }
    assert result['reported_test_count'] == result['reported_failures'] == 1
    assert result['reported_errors'] == 0 and result['observed_summary_only'] is True
    assert result['preview_truncated'] is True
    assert 'PRIVATE_OVERSIZE' not in json.dumps(dict(result), default=dict)
    assert report['status'] == 'failed' and report['test_count'] is None


def envelope() -> dict[str, object]:
    return {'status': 'failed', 'error': 'phase_failed', 'failure_hint': {
        'phase': 'test', 'stage': 'test_execution', 'cause': 'test_result_failed',
        'exception_type': 'PhaseError', 'counts': {'tests': 1, 'failures': 1, 'errors': 0, 'skipped': 0},
    }}


@pytest.mark.parametrize('fault', [
    'extra_envelope', 'extra_hint', 'extra_counts', 'duplicate_envelope', 'duplicate_hint',
    'duplicate_counts', 'wrong_phase', 'wrong_stage', 'unknown_cause', 'wrong_kind',
    'bool_count', 'negative_count', 'oversize_count', 'zero_failures', 'malformed',
    'nonfinite', 'oversize_stdout', 'pass_status',
])
def test_unknown_or_unbound_hint_never_supplies_counts(diagnosis: ModuleType, fault: str) -> None:
    value = envelope()
    hint = cast(dict[str, object], value['failure_hint'])
    counts = cast(dict[str, object], hint['counts'])
    if fault == 'extra_envelope':
        value['additional'] = 'private'
    elif fault == 'extra_hint':
        hint['message'] = 'private'
    elif fault == 'extra_counts':
        counts['additional'] = 1
    elif fault == 'wrong_phase':
        hint['phase'] = 'build'
    elif fault == 'wrong_stage':
        hint['stage'] = 'phase_execution'
    elif fault == 'unknown_cause':
        hint['cause'] = 'unknown'
    elif fault == 'wrong_kind':
        hint['exception_type'] = 'AssertionError'
    elif fault == 'bool_count':
        counts['tests'] = True
    elif fault == 'negative_count':
        counts['errors'] = -1
    elif fault == 'oversize_count':
        counts['tests'] = 2**31
    elif fault == 'zero_failures':
        counts['failures'] = 0
    elif fault == 'pass_status':
        value['status'] = 'completed'
    raw = json.dumps(value)
    if fault == 'duplicate_envelope':
        raw = raw.replace('{', '{"status": "failed", ', 1)
    elif fault == 'duplicate_hint':
        raw = raw.replace('"phase": "test"', '"phase": "test", "phase": "test"')
    elif fault == 'duplicate_counts':
        raw = raw.replace('"tests": 1', '"tests": 1, "tests": 1')
    elif fault == 'malformed':
        raw = '{invalid}'
    elif fault == 'nonfinite':
        raw = raw.replace('"tests": 1', '"tests": NaN')
    elif fault == 'oversize_stdout':
        raw += ' ' * 1025
    result = diagnosis.verification_failure_diagnosis(*discarded_stderr_evidence(raw, ''))
    assert result['category'] == 'unknown' and 'phase_failure_hint' not in result
    assert all(result[key] is None for key in (
        'reported_test_count', 'reported_failures', 'reported_errors'))


def test_stdout_overflow_and_metadata_conflict_still_reject_hint(diagnosis: ModuleType) -> None:
    raw = json.dumps(envelope())
    report, preview = discarded_stderr_evidence(raw, '')
    altered = {**preview, 'exit_code': 2}
    assert 'phase_failure_hint' not in diagnosis.verification_failure_diagnosis(report, altered)
    capture = _Capture(4096)
    capture.feed((raw + ' ' * 8192).encode())
    _, _, diagnostic = _check({
        'phase': 'test', 'status': 'failed', 'exit_code': 1, 'timed_out': False,
        'stdout': capture.text(), 'stderr': PYTHON_PHASE_OUTPUT_WITHHELD,
        'stdout_truncated': True, 'stderr_truncated': True,
        'stdout_observed_bytes': capture.observed, 'stderr_observed_bytes': 9000,
    }, phase='test', actual_phase='test', import_module='example', limit=4096)
    preview = diagnostic.to_payload()
    checks = cast(tuple[dict[str, JsonValue], ...], report['checks'])
    checks[-1]['execution_diagnostic'] = {
        key: value for key, value in preview.items() if key not in {'stdout', 'stderr'}}
    result = diagnosis.verification_failure_diagnosis(report, preview)
    assert result['category'] == 'unknown' and 'phase_failure_hint' not in result
    assert result['reported_test_count'] is None


@pytest.mark.parametrize('kind,category', [(SyntaxError, 'syntax_error'), (ImportError, 'import_error')])
def test_finite_producer_never_reflects_exception_message(
    phase: ModuleType, diagnosis: ModuleType, kind: type[Exception], category: str,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise kind('password=PRIVATE_EXCEPTION_CONTENT')

    monkeypatch.setattr(phase, '_isolation_guard', lambda: None)
    monkeypatch.setattr(phase, '_mounts', lambda: None)
    monkeypatch.setattr(phase, '_phase_output', nullcontext)
    monkeypatch.setattr(phase, '_test', fail)
    assert phase.main(['test', '--import-module', 'example']) == 1
    captured = capsys.readouterr()
    assert 'PRIVATE_EXCEPTION_CONTENT' not in captured.out + captured.err
    result = diagnosis.verification_failure_diagnosis(
        *discarded_stderr_evidence(captured.out, captured.err))
    assert result['category'] == category
    assert 'phase_failure_hint' in result and result['reported_test_count'] is None
