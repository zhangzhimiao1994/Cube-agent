"""Pure execution observations preserve bounded failure evidence, never authority."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from collections.abc import Mapping
from typing import Any, Protocol, cast

import pytest

from agent_hub.runtime.contracts import JsonValue


class _Validator(Protocol):
    def __call__(
        self, payload: object, *, tool_name: object = None,
    ) -> Mapping[str, JsonValue] | None: ...


def _validate(payload: object, *, tool_name: object = None) -> Mapping[str, JsonValue] | None:
    name = 'agent_hub.runtime.execution_failure'
    assert importlib.util.find_spec(name) is not None, 'pure execution failure validator is missing'
    validator = cast(_Validator, importlib.import_module(name).execution_failure_result)
    return validator(payload, tool_name=tool_name)


def _diagnostic() -> dict[str, Any]:
    streams: dict[str, Any] = {}
    for stream, text in (('stdout', 'build failed\n'), ('stderr', '')):
        streams[stream] = text
        streams[f'{stream}_metadata'] = {
            'sanitized_bytes': len(text.encode()), 'returned_bytes': len(text.encode()),
            'sha256': hashlib.sha256(text.encode()).hexdigest(), 'truncated': False,
            'redacted': False, 'redacted_lines': 0,
        }
    return {'schema_version': 1, 'backend': 'systemd', 'exit_code': 1, 'timed_out': False, **streams}


@pytest.mark.parametrize('timed_out', [False, True])
def test_failed_process_receipt_and_replay_are_preserved(timed_out: bool) -> None:
    diagnostic = _diagnostic()
    if timed_out:
        diagnostic.update(exit_code=None, timed_out=True)
    payload = {'execution_diagnostic': diagnostic}
    before = json.dumps(payload, sort_keys=True)
    expected = {'status': 'failed', 'untrusted': True, 'execution_diagnostic': diagnostic}
    result = _validate(payload, tool_name='project.verify')
    assert result == expected
    assert _validate(result, tool_name='project.verify') == expected
    assert json.dumps(payload, sort_keys=True) == before


@pytest.mark.parametrize('defect', [
    'schema_bool', 'schema_version', 'zero_exit', 'missing_exit', 'bool_exit', 'timeout_type',
    'backend', 'extra_diagnostic', 'oversized_output', 'missing_metadata', 'extra_metadata',
    'negative_bytes', 'bool_bytes', 'returned_mismatch', 'sanitized_short', 'redacted_type',
    'truncated_type', 'digest', 'negative_lines',
])
def test_malformed_execution_diagnostic_is_rejected_without_mutation(defect: str) -> None:
    diagnostic = _diagnostic()
    metadata = diagnostic['stdout_metadata']
    if defect == 'schema_bool':
        diagnostic['schema_version'] = True
    elif defect == 'schema_version':
        diagnostic['schema_version'] = 2
    elif defect == 'zero_exit':
        diagnostic['exit_code'] = 0
    elif defect == 'missing_exit':
        diagnostic['exit_code'] = None
    elif defect == 'bool_exit':
        diagnostic['exit_code'] = True
    elif defect == 'timeout_type':
        diagnostic['timed_out'] = 1
    elif defect == 'backend':
        diagnostic['backend'] = 'untrusted backend!'
    elif defect == 'extra_diagnostic':
        diagnostic['command'] = 'not permitted'
    elif defect == 'oversized_output':
        diagnostic['stdout'] = 'x' * 4097
        metadata.update(sanitized_bytes=4097, returned_bytes=4097)
    elif defect == 'missing_metadata':
        del diagnostic['stdout_metadata']
    elif defect == 'extra_metadata':
        metadata['raw_output'] = 'not permitted'
    elif defect == 'negative_bytes':
        metadata['returned_bytes'] = -1
    elif defect == 'bool_bytes':
        metadata['sanitized_bytes'] = True
    elif defect == 'returned_mismatch':
        metadata['returned_bytes'] += 1
    elif defect == 'sanitized_short':
        metadata['sanitized_bytes'] = 0
    elif defect == 'redacted_type':
        metadata['redacted'] = 0
    elif defect == 'truncated_type':
        metadata['truncated'] = 0
    elif defect == 'digest':
        metadata['sha256'] = 'not-a-digest'
    else:
        metadata['redacted_lines'] = -1
    payload = {'execution_diagnostic': diagnostic}
    before = json.dumps(payload, sort_keys=True)
    assert _validate(payload, tool_name='project.verify') is None
    assert json.dumps(payload, sort_keys=True) == before


@pytest.mark.parametrize('key', ['approval_id', 'generated_draft_rejection'])
def test_authority_or_draft_markers_cannot_be_normalized_as_execution_failure(key: str) -> None:
    assert _validate({'execution_diagnostic': _diagnostic(), key: 'untrusted'}) is None


@pytest.mark.parametrize('payload', [None, [], 'failed', {}, {'execution_diagnostic': {}}])
def test_missing_or_unstructured_receipts_are_rejected(payload: object) -> None:
    assert _validate(payload) is None


def test_invalid_project_failure_report_cannot_be_dropped_to_accept_outer_diagnostic() -> None:
    assert _validate({'execution_diagnostic': _diagnostic(), 'verification_failure': {}},
                     tool_name='project.verify') is None


@pytest.mark.parametrize('key', ['python_verification_failure', 'verification_blocked'])
def test_specialized_failures_keep_their_tool_scope_and_validation(key: str) -> None:
    payload = {key: {}, 'execution_diagnostic': _diagnostic()}
    assert _validate(payload, tool_name='run_safe_command') is None
    assert _validate(payload, tool_name='project.verify') is None
