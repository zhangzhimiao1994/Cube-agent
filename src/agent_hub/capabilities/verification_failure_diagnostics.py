"""Finite reported failure observations; no logs, execution proof, or authorization.

Callers must establish trusted typed-failure/native-preview provenance themselves.
This pure projection independently checks schema and exact diagnostic metadata;
matching data alone never establishes that a command or test really ran.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

from agent_hub.runtime.contracts import JsonValue, _freeze_object


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
        if (failed['phase'] != 'test' or any(
                cast(Mapping[str, JsonValue], preview[name])['redacted'] is True
                for name in ('stdout_metadata', 'stderr_metadata'))):
            return _freeze_object(result, name='failure diagnosis')
        texts = tuple(cast(str, preview[name]) for name in ('stdout', 'stderr'))
        summaries = [match for text in texts if (match := re.search(
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
