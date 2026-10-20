"""Python native previews preserve actual final errors without changing Node defaults."""

import hashlib
import importlib

import pytest

from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.skills.sandbox.base import SkillResult
from tests.unit.capabilities.test_python_project_reports import decode, fail, fixture


def test_actual_python_failure_preview_keeps_beginning_and_final_error() -> None:
    api = importlib.import_module('agent_hub.capabilities.python_project_reports')
    context = fixture()
    stderr = 'BUILD START\n' + 'progress line\n' * 1500 + 'ActualFailure: dependency behavior wrong\n'
    fail(context[3], 2, stderr=stderr, stderr_observed_bytes=len(stderr.encode()))
    observed = decode(api, context)
    preview = observed.diagnostic.to_payload()
    assert preview['stderr'].startswith('BUILD START\n')
    assert preview['stderr'].endswith('ActualFailure: dependency behavior wrong\n')
    assert len(preview['stderr'].encode()) <= 4096
    assert preview['stderr_metadata']['sha256'] == hashlib.sha256(stderr.encode()).hexdigest()


@pytest.mark.parametrize('character', ['a', '\u754c', '\U0001f600'])
def test_tail_preview_preserves_full_sanitized_hash_and_existing_metadata_budget(character: str) -> None:
    raw = character * 10000
    result = SkillResult(exit_code=1, timed_out=False, stdout='', stderr=raw)
    legacy = ExecutionDiagnostic('systemd', result).to_payload()
    tail = ExecutionDiagnostic('systemd', result, retain_tail=True).to_payload()
    assert legacy['stderr_metadata']['sha256'] == tail['stderr_metadata']['sha256']
    assert 4093 <= tail['stderr_metadata']['returned_bytes'] <= 4096
    assert tail['stderr'].endswith(character)
    assert legacy['stderr'].endswith('[truncated]')


def test_tail_is_sanitized_before_clipping_even_when_credential_is_after_budget() -> None:
    raw = 'safe\n' * 2000 + 'API_KEY=secret-sentinel\n' + 'RealError: value mismatch\n'
    payload = ExecutionDiagnostic('systemd', SkillResult(exit_code=1, timed_out=False,
        stdout='', stderr=raw), retain_tail=True).to_payload()
    assert 'secret-sentinel' not in repr(payload)
    assert payload['stderr'].endswith('RealError: value mismatch\n')
    assert payload['stderr_metadata']['redacted_lines'] == 1
