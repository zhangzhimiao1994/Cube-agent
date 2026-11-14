"""Finite reported failure observations; no logs, execution proof, or authorization.

Callers must establish trusted typed-failure/native-preview provenance themselves.
This pure projection independently checks schema and exact diagnostic metadata;
matching data alone never establishes that a command or test really ran.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import cast

from agent_hub.runtime.contracts import JsonValue, _freeze_object


def _failure_hint(stdout: str, *, phase: str) -> Mapping[str, JsonValue] | None:
    """Finite observations from failed stdout, never native provenance or PASS."""
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate failure hint key')
            value[key] = item
        return value

    def nonfinite(_: str) -> object:
        raise ValueError('nonfinite failure hint')

    try:
        if not 0 < len(stdout.encode('utf-8')) <= 1024:
            return None
        value = json.loads(stdout, object_pairs_hook=pairs, parse_constant=nonfinite)
        if (type(value) is not dict or set(value) != {'status', 'error', 'failure_hint'}
                or value['status'] != 'failed' or value['error'] != 'phase_failed'):
            return None
        hint = value['failure_hint']
        if type(hint) is not dict or set(hint) not in (
            {'phase', 'stage', 'cause', 'exception_type'},
            {'phase', 'stage', 'cause', 'exception_type', 'counts'},
        ):
            return None
        if any(type(hint[key]) is not str for key in ('phase', 'stage', 'cause', 'exception_type')):
            return None
        if hint['phase'] != phase:
            return None
        allowed = {
            ('phase_execution', 'syntax_error', 'SyntaxError'),
            ('phase_execution', 'assertion_error', 'AssertionError'),
            ('phase_execution', 'type_error', 'TypeError'),
            ('phase_execution', 'value_error', 'ValueError'),
            ('phase_execution', 'runtime_error', 'RuntimeError'),
            ('phase_execution', 'os_error', 'FileNotFoundError'),
            ('phase_execution', 'os_error', 'PermissionError'),
            ('phase_execution', 'os_error', 'OSError'),
            ('phase_guard', 'phase_guard', 'PhaseError'),
        }
        allowed |= ({('test_import', 'import_error', 'ImportError'),
                     ('test_import', 'import_error', 'ModuleNotFoundError'),
                     ('test_execution', 'test_result_failed', 'PhaseError')} if phase == 'test'
                    else {('phase_execution', 'import_error', 'ImportError'),
                          ('phase_execution', 'import_error', 'ModuleNotFoundError'),
                          ('phase_execution', 'test_result_failed', 'PhaseError')})
        if (hint['stage'], hint['cause'], hint['exception_type']) not in allowed:
            return None
        if 'counts' in hint:
            counts = hint['counts']
            if ((hint['phase'], hint['stage'], hint['cause'], hint['exception_type'])
                    != ('test', 'test_execution', 'test_result_failed', 'PhaseError')
                    or type(counts) is not dict or set(counts) != {'tests', 'failures', 'errors', 'skipped'}
                    or any(type(item) is not int or not 0 <= item <= 2**31 - 1 for item in counts.values())
                    or counts['tests'] <= 0 or counts['failures'] + counts['errors'] <= 0):
                return None
        return _freeze_object(hint, name='finite phase failure hint')
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None


def _unittest_summary_stream(text: str) -> str | None:
    """Recognize only the two exact native trailers following unittest.run.

    These are observation framing, never execution proof. Other native causes,
    arbitrary JSON, duplicate markers or unknown tails cannot supply a summary.
    """
    marker = 'PYTHON_PHASE_FAILURE '
    if marker not in text:
        return text
    if text.count(marker) != 1:
        return None
    for trailer in (
        'PYTHON_PHASE_FAILURE {"cause": "test_result_failed", "exception_type": "PhaseError", "phase": "test", "stage": "test_execution"}\n',
        'PYTHON_PHASE_FAILURE {"cause": "phase_guard", "exception_type": "PhaseError", "phase": "test", "stage": "phase_guard"}\n',
    ):
        if text.endswith('\n' + trailer):
            return text[:-len(trailer)]
    return None


def verification_failure_diagnosis(
    report: Mapping[str, JsonValue], preview: Mapping[str, JsonValue] | None,
) -> Mapping[str, JsonValue]:
    """Return only finite labels/numbers, never preview text or exception details."""
    from agent_hub.capabilities.project_verification import _validate_original_diagnostic
    from agent_hub.capabilities.python_project_reports import _failure_shape

    result: dict[str, JsonValue] = {'category': 'unknown', 'reported_test_count': None,
        'reported_failures': None, 'reported_errors': None, 'preview_truncated': None,
        'observed_summary_only': False}
    try:
        safe = _failure_shape(report)
        checks = cast(tuple[Mapping[str, JsonValue], ...], safe['checks'])
        failed = checks[-1]
        metadata = cast(Mapping[str, JsonValue], failed['execution_diagnostic'])
        if preview is None:
            if metadata['timed_out'] is True:
                result['category'] = 'timeout'
            return _freeze_object(result, name='failure diagnosis')
        _validate_original_diagnostic(preview, retain_tail=True)
        if {k:v for k,v in preview.items() if k not in {'stdout', 'stderr'}} != metadata:
            return _freeze_object(result, name='failure diagnosis')
        truncated = any(cast(Mapping[str, JsonValue], preview[name])['truncated'] is True
                        for name in ('stdout_metadata', 'stderr_metadata'))
        result['preview_truncated'] = truncated
        if metadata['timed_out'] is True:
            result['category'] = 'timeout'
            return _freeze_object(result, name='failure diagnosis')
        # Raw stderr remains withheld. Only complete, bound stdout from an
        # actually failed phase may supply finite, untrusted failure metadata.
        from agent_hub.skills.sandbox.python_project_process import PYTHON_PHASE_OUTPUT_WITHHELD

        if (preview['stderr'] == PYTHON_PHASE_OUTPUT_WITHHELD
                and metadata['exit_code'] not in (None, 0)
                and all(cast(Mapping[str, JsonValue], preview[name])['redacted'] is False
                        for name in ('stdout_metadata', 'stderr_metadata'))
                and cast(Mapping[str, JsonValue], preview['stdout_metadata'])['truncated'] is False):
            dependency = cast(Mapping[str, JsonValue], safe['dependency_install'])
            phase = ('check_environment' if failed['phase'] == 'install'
                     and dependency['mode'] == 'reused' else cast(str, failed['phase']))
            hint = _failure_hint(cast(str, preview['stdout']), phase=phase)
            if hint is not None:
                result.update(phase_failure_hint=hint, preview_truncated=True, observed_summary_only=True)
                if hint['cause'] in ('syntax_error', 'import_error'):
                    result['category'] = hint['cause']
                hint_counts = hint.get('counts')
                if isinstance(hint_counts, Mapping):
                    result.update(reported_test_count=hint_counts['tests'],
                                  reported_failures=hint_counts['failures'], reported_errors=hint_counts['errors'])
                return _freeze_object(result, name='failure diagnosis')
        if (failed['phase'] != 'test' or any(
                cast(Mapping[str, JsonValue], preview[name])['redacted'] is True
                for name in ('stdout_metadata', 'stderr_metadata'))):
            return _freeze_object(result, name='failure diagnosis')
        texts = tuple(cast(str, preview[name]) for name in ('stdout', 'stderr'))
        # The native producer writes the finite trailer to stderr only. Do not
        # alter bound metadata, disclosed diagnostics or legacy summary parsing.
        summary_stderr = _unittest_summary_stream(texts[1])
        summary_texts = (() if 'PYTHON_PHASE_FAILURE ' in texts[0] or summary_stderr is None
                         else (texts[0], summary_stderr))
        summaries = [match for text in summary_texts if (match := re.search(
            r'(?m)^Ran ([0-9]{1,10}) tests? in [0-9]+(?:\.[0-9]+)?s\n\n?FAILED \(([^()\n]{1,128})\)\s*\Z', text))]
        if len(summaries) == 1:
            summary = summaries[0]
            counts: dict[str, int] = {}
            for item in summary[2].split(', '):
                match = re.fullmatch(r'(failures|errors|skipped)=([0-9]{1,10})', item)
                if match is None or match[1] in counts:
                    break
                counts[match[1]] = int(match[2])
            else:
                total, failures, errors = int(summary[1]), counts.get('failures', 0), counts.get('errors', 0)
                if (0 < total <= 2**31 - 1 and failures + errors > 0
                        and all(v <= 2**31 - 1 for v in counts.values())):
                    result.update(reported_test_count=total, reported_failures=failures, reported_errors=errors,
                                  observed_summary_only=truncated)
        if not truncated:
            text = '\n'.join(texts)
            if re.search(r'(?m)^(SyntaxError|IndentationError|TabError)(:|$)', text):
                result['category'] = 'syntax_error'
            elif re.search(r'(?m)^(ImportError|ModuleNotFoundError)(:|$)', text):
                result['category'] = 'import_error'
            elif re.search(r'(?m)^(?:_+\s*)?ERROR collecting [^\n]+', text):
                result['category'] = 'collection_error'
            elif (result['reported_errors'] == 0 and type(result['reported_failures']) is int
                    and result['reported_failures'] == len(re.findall(r'(?m)^AssertionError(?::|$)', text))
                    == len(re.findall(r'(?m)^FAIL: ', text))):
                result['category'] = 'functional_assertion'
        return _freeze_object(result, name='failure diagnosis')
    except Exception:  # noqa: BLE001 - malformed data yields no observations or private errors.
        return _freeze_object({'category': 'unknown', 'reported_test_count': None,
            'reported_failures': None, 'reported_errors': None, 'preview_truncated': None,
            'observed_summary_only': False}, name='failure diagnosis')
