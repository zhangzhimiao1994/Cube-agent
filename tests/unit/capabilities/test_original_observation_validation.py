"""Persisted original observations must retain strict bounded native metadata."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, cast

import pytest

from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    _validate_report,
    validated_original_observation,
)
from tests.unit.capabilities.project_original_fixture import failed_observation

SOURCE = 'a' * 64
CONTRACT = 'b' * 64


def observation(failed: bool) -> dict[str, Any]:
    if failed:
        return cast(dict[str, Any], json.loads(json.dumps(failed_observation(SOURCE, CONTRACT), default=dict)))
    receipt = _validate_report(json.dumps({
        'schema_version': 1, 'status': 'passed', 'source_sha256': SOURCE,
        'runtime': {'node': 'v22.12.0', 'npm': '10.9.0'},
        'checks': [{'phase': phase, 'status': 'passed', 'exit_code': 0, 'timed_out': False,
                    'stdout': '', 'stderr': ''} for phase in ('install', 'build', 'test')],
    }), expected_source=SOURCE)
    return cast(dict[str, Any], json.loads(json.dumps({'schema_version': 1, 'status': 'passed', 'verification': receipt,
        'original_check_contract': {'schema_version': 1, 'contract_sha256': CONTRACT,
                                    'protected_sha256': CONTRACT}}, default=dict)))


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('mutation', ['missing_metadata', 'boolean_count', 'bad_sha',
                                      'mismatched_length', 'unknown_field', 'oversized_output',
                                      'fake_truncation'])
def test_original_observation_rejects_invalid_diagnostic(failed: bool, mutation: str) -> None:
    payload = observation(failed)
    diagnostic = (payload['execution_diagnostic'] if failed
                  else payload['verification']['checks'][0]['execution_diagnostic'])
    if mutation == 'missing_metadata':
        diagnostic.pop('stdout_metadata')
    elif mutation == 'boolean_count':
        diagnostic['stdout_metadata']['returned_bytes'] = False
    elif mutation == 'bad_sha':
        diagnostic['stdout_metadata']['sha256'] = 'not-a-sha'
    elif mutation == 'mismatched_length':
        diagnostic['stdout'] = 'unexpected bytes'
    elif mutation == 'unknown_field':
        diagnostic['untrusted_extra'] = 'must not survive'
    elif mutation == 'fake_truncation':
        diagnostic['stdout_metadata']['truncated'] = True
        diagnostic['stdout_metadata']['sha256'] = '0' * 64
    else:
        diagnostic['stdout'] = 'x' * 4097
    with pytest.raises(ProjectVerificationError, match='observation'):
        validated_original_observation(payload, expected_source=SOURCE, expected_contract=CONTRACT)


@pytest.mark.parametrize('failed', [False, True])
def test_valid_original_observation_is_not_rewritten(failed: bool) -> None:
    payload = observation(failed)
    before = deepcopy(payload)
    result = validated_original_observation(payload, expected_source=SOURCE, expected_contract=CONTRACT)
    assert json.loads(json.dumps(result, default=dict)) == before
    assert payload == before


def test_valid_truncated_original_output_keeps_full_stream_metadata() -> None:
    from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
    from agent_hub.skills.sandbox.base import SkillResult

    payload = observation(False)
    diagnostic = ExecutionDiagnostic(backend='systemd', result=SkillResult(
        exit_code=0, timed_out=False, stdout='x' * 5000, stderr='',
    )).to_payload()
    payload['verification']['checks'][0]['execution_diagnostic'] = diagnostic
    result = validated_original_observation(payload, expected_source=SOURCE, expected_contract=CONTRACT)
    assert json.loads(json.dumps(result, default=dict)) == json.loads(json.dumps(payload, default=dict))
