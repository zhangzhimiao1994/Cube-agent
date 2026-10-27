"""Trusted Python failure phase projection; no model or native command execution."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.runtime.direct import _WorkspaceVerificationTrace
from agent_hub.skills.sandbox.base import SkillResult
from tests.unit.runtime.test_python_project_history import history_case


def result(original: Any, *, diagnostic: object | None = None, report: object | None = None) -> HarnessToolCallResult:
    return HarnessToolCallResult(call_id='failed-native-phase', tool_name='project.verify', status='failed',
        failure_reason='python_project_checks_failed', payload={
            'python_verification_failure': original.observation['verification'] if report is None else report,
            'execution_diagnostic': original.observation['execution_diagnostic'] if diagnostic is None else diagnostic,
        })


@pytest.mark.parametrize('index,phase', [(0, 'install'), (1, 'build'), (2, 'test')])
async def test_trace_preserves_bound_native_python_failure_phase(tmp_path: Path, index: int, phase: str) -> None:
    _, _, _, original, context = await history_case(tmp_path, failure_index=index)
    trace = _WorkspaceVerificationTrace(context=context)
    trace.record(result(original), known_files={}, attempt=1)
    check = trace.attempts[0]['checks'][0]
    assert check['phase'] == phase and check['status'] == 'failed'
    assert check['execution_diagnostic']['exit_code'] != 0
    assert trace.attempts[0]['source_sha256'] == original.observation['verification']['source_sha256']
    public = json.dumps(trace.to_payload())
    assert str(tmp_path) not in public and 'stdout"' not in public and 'stderr"' not in public


@pytest.mark.parametrize('defect', ['bad_shape', 'different_diagnostic'])
async def test_trace_does_not_credit_unvalidated_or_conflicting_python_phase(tmp_path: Path, defect: str) -> None:
    _, _, _, original, context = await history_case(tmp_path)
    report = dict(original.observation['verification'])
    diagnostic = dict(original.observation['execution_diagnostic'])
    if defect == 'bad_shape':
        report['checks'] = [{'phase': 'test', 'status': 'failed'}]
    else:
        diagnostic['exit_code'] = 97
    trace = _WorkspaceVerificationTrace(context=context)
    trace.record(result(original, diagnostic=diagnostic, report=report), known_files={}, attempt=1)
    assert trace.attempts[0]['checks'][0]['phase'] == 'execution'


async def test_python_report_without_matching_context_does_not_claim_native_phase(tmp_path: Path) -> None:
    _, _, _, original, _ = await history_case(tmp_path)
    trace = _WorkspaceVerificationTrace()
    trace.record(result(original), known_files={}, attempt=1)
    assert trace.attempts[0]['checks'][0]['phase'] == 'execution'


@pytest.mark.parametrize('clipped', [False, True])
async def test_public_trace_carries_safe_diagnosis_without_native_preview(tmp_path: Path, clipped: bool) -> None:
    _, _, _, original, context = await history_case(tmp_path)
    text = ('SOURCE_SNIPPET_' + 'x' * 5000 + '\n') if clipped else (
        'FAIL: case_one\nAssertionError: SOURCE_SNIPPET_ONE\n'
        'FAIL: case_two\nAssertionError: SOURCE_SNIPPET_TWO\n')
    text += 'Ran 4 tests in 0.001s\n\nFAILED (failures=2)\n'
    diagnostic = ExecutionDiagnostic('systemd', SkillResult(exit_code=1, timed_out=False,
        stdout='', stderr=text), retain_tail=True).to_payload()
    report = json.loads(json.dumps(original.observation['verification'], default=dict))
    report['checks'][-1]['execution_diagnostic'] = {
        key: value for key, value in diagnostic.items() if key not in {'stdout', 'stderr'}}
    trace = _WorkspaceVerificationTrace(context=context)
    trace.record(result(original, report=report, diagnostic=diagnostic), known_files={}, attempt=1)
    check = trace.attempts[0]['checks'][0]
    assert check['phase'] == 'test'
    assert check['diagnosis'] == {'category': 'unknown' if clipped else 'functional_assertion',
        'reported_test_count': 4, 'reported_failures': 2, 'reported_errors': 0,
        'preview_truncated': clipped, 'observed_summary_only': clipped}
    public = json.dumps(trace.to_payload())
    assert 'SOURCE_SNIPPET' not in public and text not in public


@pytest.mark.parametrize('reason,phase', [('project test failed', 'test'), ('project build failed', 'build')])
def test_legacy_non_python_phase_is_unchanged(reason: str, phase: str) -> None:
    trace = _WorkspaceVerificationTrace()
    trace.record(HarnessToolCallResult(call_id='legacy-node-phase', tool_name='project.verify', status='failed',
        failure_reason=reason, payload={'execution_diagnostic': {'exit_code': 1, 'timed_out': False}}),
        known_files={}, attempt=1)
    check = trace.attempts[0]['checks'][0]
    assert isinstance(check, Mapping) and check['phase'] == phase
