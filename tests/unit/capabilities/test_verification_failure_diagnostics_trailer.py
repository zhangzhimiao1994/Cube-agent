"""Real unittest/native-trailer diagnostic contract; no sandbox or model execution."""
from __future__ import annotations

import importlib.util
import io
import json
import unittest
from collections.abc import Callable, Mapping
from contextlib import redirect_stderr
from pathlib import Path
from typing import cast

import pytest

from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.python_project_phase import (
    PhaseError,
    _TestResultFailed,
    _write_phase_failure,
)


@pytest.fixture
def diagnose() -> Callable[..., Mapping[str, JsonValue]]:
    candidate = Path(__file__).with_name('verification_failure_diagnostics.py')
    if candidate.is_file():
        spec = importlib.util.spec_from_file_location('_diagnostics_trailer_candidate', candidate)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return cast(Callable[..., Mapping[str, JsonValue]], module.verification_failure_diagnosis)
    from agent_hub.capabilities.verification_failure_diagnostics import (
        verification_failure_diagnosis,
    )

    return verification_failure_diagnosis


def native_failure(*, guard: bool = False) -> tuple[str, str]:
    class BrokenImplementation(unittest.TestCase):
        def test_actual_assertion(self) -> None:
            self.assertEqual(1, 2)

    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(BrokenImplementation))
    assert result.testsRun == 1 and len(result.failures) == 1 and not result.errors
    summary = stream.getvalue()
    with redirect_stderr(stream):
        _write_phase_failure('test', PhaseError() if guard else _TestResultFailed())
    return summary, stream.getvalue()[len(summary):]


def evidence(text: str, *, stdout: str = '') -> tuple[dict[str, JsonValue], Mapping[str, JsonValue]]:
    preview = ExecutionDiagnostic('systemd', SkillResult(
        exit_code=1, timed_out=False, stdout=stdout, stderr=text), retain_tail=True).to_payload()
    passed = ExecutionDiagnostic('systemd', SkillResult(
        exit_code=0, timed_out=False, stdout='', stderr='')).to_payload()

    def metadata(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        return {key: item for key, item in value.items() if key not in {'stdout', 'stderr'}}

    return {
        'schema_version': 2, 'kind': 'python_project', 'status': 'failed',
        'execution_id': 'native_trailer_case', 'source_sha256': 'a' * 64,
        'recipe_sha256': 'b' * 64, 'environment_input_sha256': 'c' * 64,
        'environment_sha256': 'd' * 64, 'original_check_contract': 'e' * 64,
        'runtime': {'implementation': 'cpython', 'version': '3.12.10', 'abi': 'cp312',
                    'platform': 'linux_x86_64', 'binary_sha256': 'f' * 64,
                    'base_runtime_sha256': '0' * 64},
        'dependency_install': {'mode': 'created', 'origin_execution_id': 'native_trailer_case'},
        'wheel': {'filename': 'example-1.0-py3-none-any.whl', 'sha256': '1' * 64, 'size': 100},
        'test_count': None,
        'checks': (
            {'phase': 'install', 'status': 'passed', 'execution_diagnostic': metadata(passed)},
            {'phase': 'build', 'status': 'passed', 'execution_diagnostic': metadata(passed)},
            {'phase': 'test', 'status': 'failed', 'failure_reason': 'process_failed',
             'execution_diagnostic': metadata(preview)},
        ),
    }, preview


def test_real_unittest_failure_with_original_native_trailer(diagnose: Callable[..., object]) -> None:
    summary, trailer = native_failure()
    report, preview = evidence(summary + trailer)
    assert diagnose(report, preview) == {
        'category': 'functional_assertion', 'reported_test_count': 1,
        'reported_failures': 1, 'reported_errors': 0, 'preview_truncated': False,
        'observed_summary_only': False,
    }


def test_legacy_summary_and_generated_guard_trailer(diagnose: Callable[..., Mapping[str, JsonValue]]) -> None:
    summary, trailer = native_failure(guard=True)
    for text in (summary, summary + trailer):
        result = diagnose(*evidence(text))
        assert result['category'] == 'functional_assertion'
        assert result['reported_test_count'] == result['reported_failures'] == 1
        assert result['reported_errors'] == 0


@pytest.mark.parametrize('fault', [
    'wrong_phase', 'unknown_cause', 'inconsistent_kind', 'extra_field', 'duplicate_key',
    'malformed_json', 'missing_newline', 'extra_tail', 'duplicate_trailer', 'stdout_trailer',
])
def test_non_native_or_ambiguous_trailer_stays_unknown(
    diagnose: Callable[..., Mapping[str, JsonValue]], fault: str,
) -> None:
    summary, trailer = native_failure()
    marker = 'PYTHON_PHASE_FAILURE '
    record = json.loads(trailer.removeprefix(marker))
    if fault in {'wrong_phase', 'unknown_cause', 'inconsistent_kind', 'extra_field'}:
        record.update({
            'wrong_phase': {'phase': 'build'}, 'unknown_cause': {'cause': 'unknown'},
            'inconsistent_kind': {'exception_type': 'AssertionError'}, 'extra_field': {'detail': 'extra'},
        }[fault])
        trailer = marker + json.dumps(record, sort_keys=True) + '\n'
    elif fault == 'duplicate_key':
        trailer = trailer.replace('{', '{"phase": "test", ', 1)
    elif fault == 'malformed_json':
        trailer = marker + '{broken}\n'
    elif fault == 'missing_newline':
        trailer = trailer.rstrip('\n')
    elif fault == 'extra_tail':
        trailer += 'unrecognized tail\n'
    elif fault == 'duplicate_trailer':
        trailer += trailer
    text, stdout = summary + trailer, ''
    if fault == 'stdout_trailer':
        text, stdout = summary, trailer
    result = diagnose(*evidence(text, stdout=stdout))
    assert result['category'] == 'unknown'
    assert all(result[key] is None for key in (
        'reported_test_count', 'reported_failures', 'reported_errors'))


def test_redaction_and_metadata_binding_are_not_bypassed(
    diagnose: Callable[..., Mapping[str, JsonValue]],
) -> None:
    summary, trailer = native_failure()
    report, preview = evidence('password=DO_NOT_DISCLOSE\n' + summary + trailer)
    result = diagnose(report, preview)
    assert result['category'] == 'unknown' and result['reported_test_count'] is None
    assert 'DO_NOT_DISCLOSE' not in json.dumps(dict(result))
    report, preview = evidence(summary + trailer)
    altered = {**preview, 'exit_code': 2}
    result = diagnose(report, altered)
    assert result['category'] == 'unknown' and result['reported_test_count'] is None


@pytest.mark.parametrize('error,category', [
    (SyntaxError, 'syntax_error'), (ImportError, 'import_error'),
])
def test_other_native_trailers_preserve_existing_error_classification(
    diagnose: Callable[..., Mapping[str, JsonValue]], error: type[Exception], category: str,
) -> None:
    stream = io.StringIO()
    stream.write(error.__name__ + ': example failure\n')
    with redirect_stderr(stream):
        _write_phase_failure('test', error())
    result = diagnose(*evidence(stream.getvalue()))
    assert result['category'] == category
    assert all(result[key] is None for key in (
        'reported_test_count', 'reported_failures', 'reported_errors'))
