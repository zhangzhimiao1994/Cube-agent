from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from typing import Any
from uuid import uuid4

import pytest

from agent_hub.capabilities.generated_project_draft import (
    GeneratedDraftRejection,
    validated_generated_draft_payload,
)


def rejection() -> GeneratedDraftRejection:
    return GeneratedDraftRejection(
        tenant_id=str(uuid4()), user_id=str(uuid4()), run_id=str(uuid4()),
        project_id='project-a', session_id='session-a', request_sha256='a' * 64,
        proposer_actor='engineer', observation_sha256='b' * 64,
        reason='generated_acceptance_manifest_invalid', issues=('protected_editable_paths',),
    )


def test_payload_is_exact_frozen_and_roundtrips() -> None:
    value = rejection()
    payload = value.to_payload()
    assert set(payload) == {'schema_version', 'kind', 'phase', 'execution_started', 'tenant_id',
        'user_id', 'run_id', 'project_id', 'session_id', 'request_sha256', 'proposer_actor',
        'observation_sha256', 'reason', 'issues'}
    assert payload['execution_started'] is False
    validated = validated_generated_draft_payload(payload)
    assert validated is not None and validated['issues'] == value.issues
    assert validated_generated_draft_payload(validated) == validated
    with pytest.raises(FrozenInstanceError):
        value.reason = 'changed'  # type: ignore[misc]
    payload['issues'] = ['raw source']
    assert value.issues == ('protected_editable_paths',)


@pytest.mark.parametrize('field,value', [
    ('schema_version', True), ('schema_version', 1.0), ('execution_started', 0),
    ('execution_started', True), ('phase', 'captured'), ('kind', 'execution_receipt'),
    ('tenant_id', str(uuid4()).upper()), ('user_id', uuid4()), ('run_id', '../run'),
    ('project_id', '../project'), ('project_id', 'C:\\project'), ('session_id', 'a/b'),
    ('request_sha256', 'A' * 64), ('observation_sha256', 'b' * 65),
    ('proposer_actor', 'engineer\nsource'), ('reason', 'generated_acceptance_unavailable_or_changed'),
    ('issues', []), ('issues', ['protected_editable_paths'] * 2),
    ('issues', ['secret/path']), ('issues', ['manifest_missing']), ('issues', 'protected_editable_paths'),
])
def test_mutated_payload_is_rejected(field: str, value: object) -> None:
    payload = rejection().to_payload()
    payload[field] = value
    assert validated_generated_draft_payload(payload) is None


def test_extra_missing_and_oversize_payloads_are_rejected() -> None:
    value = rejection().to_payload()
    assert validated_generated_draft_payload({**value, 'path': 'private'}) is None
    assert validated_generated_draft_payload({k: v for k, v in value.items() if k != 'phase'}) is None
    assert validated_generated_draft_payload({**value, 'issues': ['a' * 100_000]}) is None
    assert validated_generated_draft_payload(None) is None


@pytest.mark.parametrize('field,value', [('tenant_id', uuid4()), ('session_id', '../session'),
    ('reason', 'anything'), ('issues', ['protected_editable_paths']), ('request_sha256', 5)])
def test_construction_is_strict(field: str, value: Any) -> None:
    with pytest.raises(ValueError, match='invalid generated draft rejection'):
        replace(rejection(), **{field: value})


def test_to_payload_revalidates_mutated_dataclass() -> None:
    value = rejection()
    object.__setattr__(value, 'issues', ('source/secret.py',))
    with pytest.raises(ValueError):
        value.to_payload()


@pytest.mark.parametrize('field,value', [('schema_version', True), ('execution_started', 0),
    ('phase', 'captured'), ('kind', 'receipt'), ('proposer_actor', 'raw\nsource')])
def test_frozen_constant_mutation_is_rejected(field: str, value: object) -> None:
    item = rejection()
    object.__setattr__(item, field, value)
    with pytest.raises(ValueError):
        item.to_payload()


@pytest.mark.parametrize('reason,issue', [
    ('manifest_required', 'manifest_missing'), ('manifest_invalid', 'manifest_invalid'),
    ('scaffold_incomplete', 'scaffold_incomplete'), ('node_declaration_invalid', 'node_declaration_invalid'),
    ('python_declaration_invalid', 'python_declaration_invalid'),
])
def test_each_finite_reason_has_bound_issue(reason: str, issue: str) -> None:
    item = replace(rejection(), reason='generated_acceptance_' + reason, issues=(issue,))
    assert validated_generated_draft_payload(item.to_payload()) is not None
