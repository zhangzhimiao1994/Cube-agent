from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_hub.skills.sandbox import project


def base() -> project.ProjectInvocation:
    return project.ProjectInvocation(execution_id='original_test', tenant_id=uuid4(), run_id=uuid4(),
        project_id='project-a', session_id='session-a', sandbox_profile='workspace_write', timeout_seconds=30)


def reference() -> dict[str, object]:
    return {'schema_version': 1, 'contract_sha256': 'a' * 64,
            'input_source_sha256': 'b' * 64, 'file_count': 3}


@pytest.mark.parametrize('kind', ['original', 'candidate'])
def test_contract_protocol_has_explicit_source_kind_and_version(kind: str) -> None:
    invocation_type = project.ContractProjectInvocation
    request_type = project.ContractProjectBrokerRequest
    invocation = invocation_type.model_validate({**base().model_dump(),
        'source_kind': kind, 'original_reference': reference()}, strict=True)
    request = request_type(invocation=invocation)
    assert request.version == 3
    assert request_type.model_validate_json(request.model_dump_json(), strict=True) == request
    with pytest.raises(ValidationError):
        project.ProjectBrokerRequest.model_validate(request.model_dump(), strict=True)
    with pytest.raises(ValidationError):
        project.ProjectInvocation.model_validate(invocation.model_dump(), strict=True)


@pytest.mark.parametrize('field,value', [('file_count', True), ('schema_version', True),
    ('contract_sha256', 'invalid'), ('input_source_sha256', 'B' * 64), ('extra', 'unknown')])
def test_contract_reference_rejects_weak_or_unknown_fields(field: str, value: object) -> None:
    invocation_type = project.ContractProjectInvocation
    with pytest.raises(ValidationError):
        invocation_type.model_validate({**base().model_dump(), 'source_kind': 'original',
            'original_reference': {**reference(), field: value}}, strict=True)


def test_legacy_request_is_still_exact_v2() -> None:
    assert project.ProjectBrokerRequest(invocation=base()).version == 2


def test_contract_command_requires_all_trusted_paths() -> None:
    from agent_hub.skills.sandbox.broker import BrokerPolicy

    with pytest.raises(ValueError):
        project.build_project_systemd_command(base(), BrokerPolicy(allowed_uid=123), Path('/private/snapshot'),
            contract_path=Path('/private/check_contract.json'))


@pytest.mark.parametrize('schema', [True, '1', 1.0, None])
def test_broker_rejects_weak_native_binding_versions(schema: object) -> None:
    from agent_hub.skills.sandbox.base import SkillResult

    report = {'schema_version': 1, 'status': 'passed', 'source_sha256': 'b' * 64,
        'checks': [], 'runtime': {}, 'original_check_contract': {'schema_version': schema,
            'contract_sha256': 'c' * 64, 'protected_sha256': 'c' * 64}}
    with pytest.raises(RuntimeError, match='contract'):
        project.ProjectExecutionBroker._validate_result(
            SkillResult(exit_code=0, stdout=json.dumps(report), stderr='', timed_out=False),
            'b' * 64, expected_contract_sha256='c' * 64)
