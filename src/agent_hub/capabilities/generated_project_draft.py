"""Bounded pre-capture observations, never execution receipts or authority."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from agent_hub.capabilities.project_contracts import _scope, _sha
from agent_hub.runtime.contracts import JsonValue, _freeze_object

_ISSUES = {
    'generated_acceptance_manifest_required': frozenset({'manifest_missing'}),
    'generated_acceptance_manifest_invalid': frozenset({
        'manifest_invalid', 'protected_editable_paths', 'protected_test_files'}),
    'generated_acceptance_scaffold_incomplete': frozenset({'scaffold_incomplete'}),
    'generated_acceptance_node_declaration_invalid': frozenset({'node_declaration_invalid'}),
    'generated_acceptance_python_declaration_invalid': frozenset({'python_declaration_invalid'}),
    'generated_acceptance_python_dependency_incomplete': frozenset({'python_dependency_incomplete'}),
}
_KEYS = frozenset({'schema_version', 'kind', 'phase', 'execution_started', 'tenant_id', 'user_id',
    'run_id', 'project_id', 'session_id', 'request_sha256', 'proposer_actor', 'observation_sha256',
    'reason', 'issues'})
_ERROR = 'invalid generated draft rejection'


def dependency_observation_payload(value: object, *, complete: bool = False) -> dict[str, object]:
    from agent_hub.skills.sandbox.python_dependency_protocol import (
        PythonDependencyClosureObservation,
    )

    if not isinstance(value, Mapping) or type(complete) is not bool:
        raise ValueError(_ERROR)
    raw = bytearray()
    encoder = json.JSONEncoder(default=dict, ensure_ascii=True, allow_nan=False,
                               sort_keys=True, separators=(',', ':'))
    for chunk in encoder.iterencode(value):
        if len(raw) + len(chunk) > 32 * 1024:
            raise ValueError(_ERROR)
        raw.extend(chunk.encode('ascii'))
    observed = PythonDependencyClosureObservation.model_validate_json(bytes(raw), strict=True)
    if (observed.closure['status'] == 'complete') is not complete:
        raise ValueError(_ERROR)
    return observed.model_dump(mode='json')


def _checked(value: object) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping) or 'schema_version' not in value:
        raise ValueError(_ERROR)
    version = value.get('schema_version')
    if type(version) is not int or version not in (1, 2):
        raise ValueError(_ERROR)
    keys = _KEYS | {'dependency_closure'} if version == 2 else _KEYS
    if len(value) != len(keys) or set(value) != keys:
        raise ValueError(_ERROR)
    item = dict(value)
    if (type(item['execution_started']) is not bool or item['execution_started'] is not False
            or type(item['kind']) is not str or item['kind'] != 'generated_project_draft_rejection'
            or type(item['phase']) is not str or item['phase'] != 'before_capture'):
        raise ValueError(_ERROR)
    for key in ('tenant_id', 'user_id', 'run_id'):
        if type(item[key]) is not str or len(item[key]) != 36 or str(UUID(item[key])) != item[key]:
            raise ValueError(_ERROR)
    for key in ('project_id', 'session_id'):
        _scope(item[key])
    for key in ('request_sha256', 'observation_sha256'):
        _sha(item[key])
    actor, reason, issues = item['proposer_actor'], item['reason'], item['issues']
    if (type(actor) is not str or re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,127}', actor) is None
            or type(reason) is not str or reason not in _ISSUES
            or type(issues) not in (list, tuple) or not 1 <= len(issues) <= 3
            or any(type(issue) is not str or issue not in _ISSUES[reason] for issue in issues)
            or len(set(issues)) != len(issues)):
        raise ValueError(_ERROR)
    dependency_reason = 'generated_acceptance_python_dependency_incomplete'
    if version == 2:
        if reason != dependency_reason:
            raise ValueError(_ERROR)
        item['dependency_closure'] = dependency_observation_payload(item['dependency_closure'])
    elif reason == dependency_reason:
        raise ValueError(_ERROR)
    return _freeze_object(cast(Mapping[str, JsonValue], item), name='generated draft rejection')


def validated_generated_draft_payload(value: object) -> Mapping[str, JsonValue] | None:
    """Return a bounded immutable copy, or reject malformed/untrusted data."""
    try:
        return _checked(value)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return None


@dataclass(frozen=True, slots=True)
class GeneratedDraftRejection:
    tenant_id: str
    user_id: str
    run_id: str
    project_id: str
    session_id: str
    request_sha256: str
    proposer_actor: str
    observation_sha256: str
    reason: str
    issues: tuple[str, ...]
    schema_version: int = 1
    kind: str = 'generated_project_draft_rejection'
    phase: str = 'before_capture'
    execution_started: bool = False
    dependency_closure: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        payload = self.to_payload()
        if self.dependency_closure is not None:
            object.__setattr__(self, 'dependency_closure', _freeze_object(
                cast(Mapping[str, JsonValue], payload['dependency_closure']), name='dependency observation'))

    def to_payload(self) -> dict[str, object]:
        # Revalidate even frozen instances: object.__setattr__ must not forge evidence.
        try:
            if type(self.issues) is not tuple or not 1 <= len(self.issues) <= 3:
                raise ValueError(_ERROR)
            item: dict[str, object] = {
                'schema_version': self.schema_version, 'kind': self.kind, 'phase': self.phase,
                'execution_started': self.execution_started, 'tenant_id': self.tenant_id,
                'user_id': self.user_id, 'run_id': self.run_id, 'project_id': self.project_id,
                'session_id': self.session_id, 'request_sha256': self.request_sha256,
                'proposer_actor': self.proposer_actor, 'observation_sha256': self.observation_sha256,
                'reason': self.reason, 'issues': list(self.issues),
            }
            if self.dependency_closure is not None:
                item['dependency_closure'] = dependency_observation_payload(self.dependency_closure)
            _checked(item)
            return item
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            raise ValueError(_ERROR) from None
