"""Synthetic native metadata for component tests, never real execution evidence."""
from __future__ import annotations

import json
from collections.abc import Mapping

from agent_hub.capabilities.project_verification import ProjectVerificationError, _validate_report
from agent_hub.runtime.contracts import JsonValue


def failed_observation(source: str, protected: str) -> Mapping[str, JsonValue]:
    try:
        _validate_report(json.dumps({'schema_version': 1, 'status': 'failed', 'source_sha256': source,
            'runtime': {'node': 'v22.12.0', 'npm': '10.9.0'}, 'checks': [
                {'phase': phase, 'status': 'failed' if phase == 'test' else 'passed',
                 'exit_code': 1 if phase == 'test' else 0, 'timed_out': False,
                 'stdout': '', 'stderr': 'component fixture only' if phase == 'test' else ''}
                for phase in ('install', 'build', 'test')]}), expected_source=source)
    except ProjectVerificationError as error:
        assert error.project_failure is not None and error.diagnostic is not None
        return {'schema_version': 1, 'status': 'failed', 'verification': error.project_failure.to_payload(),
                'execution_diagnostic': error.diagnostic.to_payload(),
                'original_check_contract': {'schema_version': 1, 'contract_sha256': protected,
                                            'protected_sha256': protected}}
    raise AssertionError('original fixture must fail')
