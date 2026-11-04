"""Bounded pre-capture observations, never execution receipts or authority."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from agent_hub.capabilities.project_contracts import _path, _scope, _sha, _spellings, _tree
from agent_hub.runtime.contracts import JsonValue, _freeze_object

_PYTHON_DECLARATION_GUIDANCE = {
    'python_lock_declaration_mismatch': (
        'Align every fixed project.dependencies and build-system.requires pin with the same '
        'name/version in requirements.lock; preserve the required SHA256 hashes.'),
    'python_requires_python_unsupported': (
        'Set requires-python to >=3.12,<3.13 or ==3.12.*; this recipe supports only Python 3.12. '
        'Do not change the runtime or broaden permissions.'),
    'python_pyproject_toml_invalid': (
        'Correct pyproject.toml as valid UTF-8 TOML with quoted strings and balanced tables/arrays. '
        'Do not change requirements.lock to repair this syntax error.'),
}
_ISSUES = {
    'generated_acceptance_manifest_required': frozenset({'manifest_missing'}),
    'generated_acceptance_manifest_invalid': frozenset({
        'manifest_invalid', 'protected_editable_paths', 'protected_test_files'}),
    'generated_acceptance_scaffold_incomplete': frozenset({'scaffold_incomplete'}),
    'generated_acceptance_node_declaration_invalid': frozenset({'node_declaration_invalid'}),
    'generated_acceptance_python_declaration_invalid': frozenset({
        'python_declaration_invalid', 'python_lock_empty', 'python_lock_hashes_required',
        'python_lock_fixed_versions_required', 'python_lock_external_source_forbidden',
        *_PYTHON_DECLARATION_GUIDANCE, *_PYTHON_DECLARATION_GUIDANCE.values()}),
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


def dependency_pins_payload(value: object) -> list[dict[str, JsonValue]]:
    """Reference facts only: no Root claim, action plan, URL or execution authority."""
    from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileWheel

    if type(value) not in (tuple, list):
        raise ValueError(_ERROR)
    selected = cast(list[object] | tuple[object, ...], value)
    if not 1 <= len(selected) <= 128:
        raise ValueError(_ERROR)
    raw = bytearray()
    encoder = json.JSONEncoder(default=dict, ensure_ascii=True, allow_nan=False,
                               sort_keys=True, separators=(',', ':'))
    for chunk in encoder.iterencode(selected):
        if len(raw) + len(chunk) > 32 * 1024:
            raise ValueError(_ERROR)
        raw.extend(chunk.encode('ascii'))
    rows = json.loads(raw)
    result = [cast(dict[str, JsonValue], PythonProjectProfileWheel.model_validate_json(
        json.dumps(row, allow_nan=False), strict=True).model_dump(mode='json')) for row in rows]
    names = [cast(str, row['name']) for row in result]
    if len(set(names)) != len(names):
        raise ValueError(_ERROR)
    return sorted(result, key=lambda row: str(row['name']))


def python_declaration_issues(issue: str) -> tuple[str, ...]:
    """Fixed, code-paired guidance; no parser input or exception text is included."""
    guidance = _PYTHON_DECLARATION_GUIDANCE.get(issue)
    return (issue,) if guidance is None else (issue, guidance)


def scaffold_missing_files_payload(value: object) -> list[str]:
    """Bounded path facts from a manifest observation, never file-write authority."""
    if not isinstance(value, tuple | list) or type(value) not in (tuple, list) or not 1 <= len(value) <= 258:
        raise ValueError(_ERROR)
    paths = [_path(item) for item in value]
    if len(set(paths)) != len(paths):
        raise ValueError(_ERROR)
    _spellings(paths)
    _tree(paths)
    paths.sort()
    if len(json.dumps(paths, ensure_ascii=True, separators=(',', ':')).encode('ascii')) > 128 * 1024:
        raise ValueError(_ERROR)
    return paths


def _checked(value: object) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping) or 'schema_version' not in value:
        raise ValueError(_ERROR)
    version = value.get('schema_version')
    if type(version) is not int or version not in (1, 2, 3, 4):
        raise ValueError(_ERROR)
    keys = _KEYS | ({'dependency_closure'} if version == 2 else {'dependency_pins'} if version == 3
                    else {'missing_files'} if version == 4 else set())
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
    for code, guidance in _PYTHON_DECLARATION_GUIDANCE.items():
        if (code in issues or guidance in issues) and tuple(issues) != (code, guidance):
            raise ValueError(_ERROR)
    dependency_reason = 'generated_acceptance_python_dependency_incomplete'
    if version == 2:
        if reason != dependency_reason:
            raise ValueError(_ERROR)
        item['dependency_closure'] = dependency_observation_payload(item['dependency_closure'])
    elif version == 3:
        if reason != 'generated_acceptance_python_declaration_invalid':
            raise ValueError(_ERROR)
        item['dependency_pins'] = dependency_pins_payload(item['dependency_pins'])
    elif version == 4:
        if reason != 'generated_acceptance_scaffold_incomplete':
            raise ValueError(_ERROR)
        item['missing_files'] = scaffold_missing_files_payload(item['missing_files'])
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
    dependency_pins: tuple[Mapping[str, JsonValue], ...] | None = None
    missing_files: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        payload = self.to_payload()
        if self.dependency_closure is not None:
            object.__setattr__(self, 'dependency_closure', _freeze_object(
                cast(Mapping[str, JsonValue], payload['dependency_closure']), name='dependency observation'))
        if self.dependency_pins is not None:
            object.__setattr__(self, 'dependency_pins', tuple(_freeze_object(
                cast(Mapping[str, JsonValue], row), name='dependency pin')
                for row in cast(list[object], payload['dependency_pins'])))
        if self.missing_files is not None:
            object.__setattr__(self, 'missing_files', tuple(scaffold_missing_files_payload(self.missing_files)))

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
            if self.dependency_pins is not None:
                item['dependency_pins'] = dependency_pins_payload(self.dependency_pins)
            if self.missing_files is not None:
                item['missing_files'] = scaffold_missing_files_payload(self.missing_files)
            _checked(item)
            return item
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            raise ValueError(_ERROR) from None
