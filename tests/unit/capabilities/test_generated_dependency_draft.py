"""Typed dependency correction values are observations, not execution proof."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from pydantic import JsonValue, TypeAdapter

from agent_hub.capabilities.generated_project_draft import (
    GeneratedDraftRejection,
    validated_generated_draft_payload,
)
from agent_hub.capabilities.python_dependency_closure import (
    PythonDependencyClosure,
    PythonDependencyClosureIssue,
    PythonDependencyClosureIssueCode,
    PythonDependencyClosureStatus,
)
from agent_hub.skills.sandbox.python_dependency_protocol import PythonDependencyClosureObservation
from tests.unit.capabilities.capability_lifecycle_typed_fixture import mutate_mapping
from tests.unit.capabilities.test_generated_project_draft import rejection
from tests.unit.runs.project_auto_repair_typed_fixture import json_map


def json_payload(value: object) -> dict[str, JsonValue]:
    return TypeAdapter(dict[str, JsonValue]).validate_python(value, strict=True)


def observation(status: PythonDependencyClosureStatus = 'incomplete') -> dict[str, object]:
    environment = {
        'implementation_name':'cpython', 'implementation_version':'3.12.14',
        'os_name':'posix', 'platform_machine':'x86_64', 'platform_release':'6.1',
        'platform_system':'Linux', 'platform_version':'1',
        'platform_python_implementation':'CPython', 'python_full_version':'3.12.14',
        'python_version':'3.12', 'sys_platform':'linux',
    }
    issues = () if status == 'complete' else (
        PythonDependencyClosureIssue('missing_dependency', 'wheel', 'packaging'),)
    closure = PythonDependencyClosure(status, issues)
    data = PythonDependencyClosureObservation(
        source_sha256='a'*64, manifest_sha256='b'*64, lock_sha256='c'*64,
        catalog_sha256='d'*64, metadata_index_sha256='e'*64,
        target_environment=environment,
        target_environment_sha256=hashlib.sha256(json.dumps(environment, sort_keys=True,
            separators=(',', ':'), ensure_ascii=True).encode()).hexdigest(),
        closure=json_payload(closure.to_payload()),
    )
    return data.model_dump(mode='json')


def dependency_rejection() -> GeneratedDraftRejection:
    return replace(rejection(), schema_version=2,
        reason='generated_acceptance_python_dependency_incomplete',
        issues=('python_dependency_incomplete',), dependency_closure=json_map(observation()))


def test_dependency_rejection_roundtrips_and_freezes_observation() -> None:
    item = dependency_rejection()
    payload = item.to_payload()
    assert payload['schema_version'] == 2 and payload['execution_started'] is False
    assert 'packaging' in json.dumps(payload)
    validated = validated_generated_draft_payload(payload)
    assert validated is not None and validated_generated_draft_payload(validated) == validated
    with pytest.raises(TypeError):
        mutate_mapping(item.dependency_closure, 'source_sha256', 'f'*64)


@pytest.mark.parametrize('field,value', [
    ('schema_version',1), ('schema_version',True), ('dependency_closure',None),
    ('dependency_closure',{}), ('dependency_closure',{'host_path':'secret'}),
    ('reason','generated_acceptance_manifest_invalid'), ('issues',['manifest_invalid']),
])
def test_dependency_rejection_cannot_fall_back_to_legacy_or_unknown_fields(field: str, value: object) -> None:
    payload = dependency_rejection().to_payload()
    payload[field] = value
    assert validated_generated_draft_payload(payload) is None


def test_complete_observation_is_not_a_draft_rejection_or_success_receipt() -> None:
    payload = dependency_rejection().to_payload()
    payload['dependency_closure'] = observation('complete')
    assert validated_generated_draft_payload(payload) is None


@pytest.mark.parametrize('status,code', [
    ('metadata_unavailable','metadata_missing'), ('unsupported','unsupported_dependency'),
])
def test_noncomplete_known_catalog_feedback_can_select_a_supported_alternative(status: PythonDependencyClosureStatus, code: PythonDependencyClosureIssueCode) -> None:
    payload = dependency_rejection().to_payload()
    data = observation()
    data['closure'] = PythonDependencyClosure(status, (
        PythonDependencyClosureIssue(code, 'proposed-package', ''),)).to_payload()
    payload['dependency_closure'] = data
    assert validated_generated_draft_payload(payload) is not None


def test_legacy_payload_stays_byte_shape_compatible() -> None:
    payload = rejection().to_payload()
    assert payload['schema_version'] == 1 and 'dependency_closure' not in payload
    assert validated_generated_draft_payload(payload) is not None
