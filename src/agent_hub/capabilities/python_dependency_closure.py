"""Bounded dependency observations over supplied values, never Root/install authority.

Wheel hashes are declarations, not attestations. Callers must establish metadata
provenance separately. This module performs no filesystem, network or process IO.
Candidates are local observations, not a dependency solution or lock mutation.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast, get_args

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from agent_hub.capabilities.python_project_recipe import PythonLockedRequirement
from agent_hub.capabilities.python_project_wheels import PythonWheelBinding

PythonDependencyClosureStatus = Literal[
    'complete', 'incomplete', 'metadata_unavailable', 'unsupported',
]
PythonDependencyClosureIssueCode = Literal[
    'missing_dependency', 'version_conflict', 'requires_python', 'metadata_missing',
    'metadata_mismatch', 'metadata_ambiguous', 'invalid_metadata',
    'unsupported_dependency', 'invalid_environment', 'edge_limit', 'issue_limit',
]
_CORRECTABLE = frozenset({'missing_dependency', 'version_conflict', 'requires_python'})
_NAME = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?')
_ENVIRONMENT = frozenset({
    'implementation_name', 'implementation_version', 'os_name', 'platform_machine',
    'platform_release', 'platform_system', 'platform_version',
    'platform_python_implementation', 'python_full_version', 'python_version', 'sys_platform',
})
_MAX_BYTES = 8192
_PARSE_ERRORS = (ValueError, KeyError, RecursionError)


def _need(condition: object) -> None:
    if not condition:
        raise ValueError('python_dependency_closure_invalid')


def _object(value: object, fields: set[str]) -> dict[str, object]:
    _need(type(value) is dict)
    item = cast(dict[str, object], value)
    _need(set(item) == fields and all(type(key) is str for key in item))
    return item


def _text(value: object, maximum: int = 4096) -> None:
    _need(type(value) is str)
    text = cast(str, value)
    _need(len(text) <= maximum and text.isascii()
        and all(32 <= ord(char) < 127 for char in text))


def _wheel(value: object) -> PythonWheelBinding:
    _need(type(value) is PythonWheelBinding)
    wheel = cast(PythonWheelBinding, value)
    _text(wheel.filename, 300)
    _text(wheel.version, 128)
    PythonWheelBinding(wheel.filename, wheel.name, wheel.version, wheel.sha256)
    return wheel


def _wheel_from_payload(value: object) -> PythonWheelBinding:
    item = _object(value, {'filename', 'name', 'version', 'sha256'})
    for field, maximum in (('filename', 300), ('name', 128), ('version', 128), ('sha256', 64)):
        _text(item[field], maximum)
    try:
        return _wheel(PythonWheelBinding(
            cast(str, item['filename']), cast(str, item['name']),
            cast(str, item['version']), cast(str, item['sha256'])))
    except ValueError:
        raise ValueError('python_dependency_closure_invalid') from None


@dataclass(frozen=True, slots=True)
class PythonWheelMetadata:
    wheel: PythonWheelBinding
    requires_python: str | None
    requires_dist: tuple[str, ...]

    def __post_init__(self) -> None:
        _wheel(self.wheel)
        if self.requires_python is not None:
            _text(self.requires_python)
        _need(type(self.requires_dist) is tuple and len(self.requires_dist) <= 4096)
        for item in self.requires_dist:
            _text(item)
        _need(sum(len(item) for item in self.requires_dist) <= 65536)

    def to_payload(self) -> dict[str, object]:
        return {'wheel': self.wheel.to_payload(), 'requires_python': self.requires_python,
            'requires_dist': list(self.requires_dist)}

    @classmethod
    def from_payload(cls, value: object) -> PythonWheelMetadata:
        item = _object(value, {'wheel', 'requires_python', 'requires_dist'})
        deps = item['requires_dist']
        _need(type(deps) is list and len(cast(list[object], deps)) <= 4096)
        return cls(_wheel_from_payload(item['wheel']), cast(str | None, item['requires_python']),
            tuple(cast(list[str], deps)))


@dataclass(frozen=True, slots=True)
class PythonDependencyClosureIssue:
    code: PythonDependencyClosureIssueCode
    source: str
    dependency: str
    candidates: tuple[PythonWheelBinding, ...] = ()
    constraint: str = ''
    locked_version: str | None = None

    def __post_init__(self) -> None:
        _need(type(self.code) is str and self.code in get_args(PythonDependencyClosureIssueCode))
        for name in (self.source, self.dependency):
            _need(type(name) is str and (name == '' or (
                _NAME.fullmatch(name) is not None and canonicalize_name(name) == name)))
        if self.code in _CORRECTABLE:
            _need(self.source and self.dependency)
        elif self.code in ('invalid_environment', 'issue_limit'):
            _need(self.source == self.dependency == '')
        else:
            _need(self.source and self.dependency == '')
        _text(self.constraint, 1024)
        if self.code in _CORRECTABLE:
            try:
                specifier = SpecifierSet(self.constraint)
                _need(str(specifier) == self.constraint
                    and all(spec.operator != '===' for spec in specifier))
            except ValueError:
                raise ValueError('python_dependency_closure_invalid') from None
            if self.code == 'missing_dependency':
                _need(self.locked_version is None)
            else:
                _need(self.constraint and self.locked_version is not None)
                _text(self.locked_version, 128)
                try:
                    # Reuse the lock version grammar; this sentinel is never a candidate.
                    PythonLockedRequirement(self.dependency, cast(str, self.locked_version),
                        ('0' * 64,))
                except ValueError:
                    raise ValueError('python_dependency_closure_invalid') from None
        else:
            _need(self.constraint == '' and self.locked_version is None)
        _need(type(self.candidates) is tuple and len(self.candidates) <= 3)
        _need(not self.candidates or self.code in _CORRECTABLE)
        for wheel in self.candidates:
            _wheel(wheel)
            _need(wheel.name == self.dependency)
        _need(len(set(self.candidates)) == len(self.candidates))

    def to_payload(self) -> dict[str, object]:
        return {'code': self.code, 'source': self.source, 'dependency': self.dependency,
            'candidates': [wheel.to_payload() for wheel in self.candidates],
            'constraint': self.constraint, 'locked_version': self.locked_version}

    @classmethod
    def from_payload(cls, value: object) -> PythonDependencyClosureIssue:
        item = _object(value, {'code', 'source', 'dependency', 'candidates',
            'constraint', 'locked_version'})
        candidates = item['candidates']
        _need(type(candidates) is list and len(cast(list[object], candidates)) <= 3)
        return cls(cast(PythonDependencyClosureIssueCode, item['code']),
            cast(str, item['source']), cast(str, item['dependency']),
            tuple(_wheel_from_payload(wheel) for wheel in cast(list[object], candidates)),
            cast(str, item['constraint']), cast(str | None, item['locked_version']))


def _status(issues: tuple[PythonDependencyClosureIssue, ...]) -> PythonDependencyClosureStatus:
    codes = {issue.code for issue in issues}
    if codes - _CORRECTABLE - {'metadata_missing'}:
        return 'unsupported'
    if 'metadata_missing' in codes:
        return 'metadata_unavailable'
    return 'incomplete' if codes else 'complete'


@dataclass(frozen=True, slots=True)
class PythonDependencyClosure:
    status: PythonDependencyClosureStatus
    issues: tuple[PythonDependencyClosureIssue, ...]

    def __post_init__(self) -> None:
        _need(type(self.status) is str and self.status in get_args(PythonDependencyClosureStatus))
        _need(type(self.issues) is tuple and len(self.issues) <= 32
            and all(type(issue) is PythonDependencyClosureIssue for issue in self.issues))
        _need(self.status == _status(self.issues))
        _need(_payload_bytes(self.to_payload()) <= _MAX_BYTES)

    def to_payload(self) -> dict[str, object]:
        return {'status': self.status, 'issues': [issue.to_payload() for issue in self.issues]}

    @classmethod
    def from_payload(cls, value: object) -> PythonDependencyClosure:
        item = _object(value, {'status', 'issues'})
        issues = item['issues']
        _need(type(issues) is list and len(cast(list[object], issues)) <= 32)
        return cls(cast(PythonDependencyClosureStatus, item['status']),
            tuple(PythonDependencyClosureIssue.from_payload(issue)
                for issue in cast(list[object], issues)))


def _payload_bytes(payload: dict[str, object]) -> int:
    return len(json.dumps(payload, ensure_ascii=True, allow_nan=False).encode('ascii'))


def _environment(value: Mapping[str, str]) -> dict[str, str]:
    _need(isinstance(value, Mapping) and len(value) == len(_ENVIRONMENT)
        and set(value) == _ENVIRONMENT)
    result = dict(value)
    for field in _ENVIRONMENT:
        _text(result[field], 1024)
    full = Version(result['python_full_version'])
    _need(len(full.release) >= 3 and result['python_version'] ==
        f'{full.release[0]}.{full.release[1]}')
    Version(result['implementation_version'])
    return result


def _requirement(text: str) -> Requirement:
    # Bound parser recursion as well as input bytes; never echo parser exceptions.
    _need(text.count('(') <= 32)
    result = Requirement(text)
    _need(result.url is None and all(spec.operator != '===' for spec in result.specifier))
    _need(len(result.name) <= 128 and len(result.extras) <= 128
        and all(len(extra) <= 128 for extra in result.extras))
    return result


def _python_spec(item: PythonWheelMetadata) -> SpecifierSet:
    result = SpecifierSet(item.requires_python or '')
    _need(all(spec.operator != '===' for spec in result))
    return result


def _single_issue(code: PythonDependencyClosureIssueCode) -> PythonDependencyClosure:
    issues = (PythonDependencyClosureIssue(code, '', ''),)
    return PythonDependencyClosure(_status(issues), issues)


def check_python_dependency_closure(
    requirements: tuple[PythonLockedRequirement, ...],
    metadata: tuple[PythonWheelMetadata, ...],
    target_environment: Mapping[str, str],
    *, max_edges: int = 4096, max_issues: int = 32,
) -> PythonDependencyClosure:
    """Check every lock and activated extra; return bounded, non-authorizing values.

    Budgets are integers in 1..4096 / 1..32. Each Requires-Dist examination
    (including inactive markers and extra revisits) consumes an edge. Input shape
    errors raise a fixed ValueError; unsupported semantics become safe issues.
    All eleven standard target marker fields are required; extra is supplied
    explicitly for each evaluation. No host value can fill a missing target field.
    """
    _need(type(max_edges) is int and 1 <= max_edges <= 4096
        and type(max_issues) is int and 1 <= max_issues <= 32)
    _need(type(requirements) is tuple and len(requirements) <= 128
        and type(metadata) is tuple and len(metadata) <= 1000)
    locked: dict[str, PythonLockedRequirement] = {}
    for pin in requirements:
        _need(type(pin) is PythonLockedRequirement and pin.name not in locked)
        _text(pin.version, 128)
        _need(len(pin.hashes) <= 128)
        PythonLockedRequirement(pin.name, pin.version, pin.hashes)
        locked[pin.name] = pin
    catalog: dict[str, list[PythonWheelMetadata]] = {}
    for item in metadata:
        _need(type(item) is PythonWheelMetadata)
        item.__post_init__()
        catalog.setdefault(item.wheel.name, []).append(item)
    try:
        environment = _environment(target_environment)
        python_version = Version(environment['python_full_version'])
    except _PARSE_ERRORS:
        return _single_issue('invalid_environment')
    return _check(locked, catalog, environment, python_version, max_edges, max_issues)


def _check(
    locked: dict[str, PythonLockedRequirement], catalog: dict[str, list[PythonWheelMetadata]],
    environment: dict[str, str], python_version: Version, max_edges: int, max_issues: int,
) -> PythonDependencyClosure:
    issues: list[PythonDependencyClosureIssue] = []
    selected: dict[str, PythonWheelMetadata] = {}
    constraints: dict[str, list[SpecifierSet]] = {}

    def report(
        code: PythonDependencyClosureIssueCode, source: str, dependency: str = '', *,
        constraint: str = '', locked_version: str | None = None,
    ) -> bool:
        issue = PythonDependencyClosureIssue(code, source, dependency,
            constraint=constraint, locked_version=locked_version)
        if issue not in issues:
            if len(issues) >= max_issues:
                issues[-1] = PythonDependencyClosureIssue('issue_limit', '', '')
                return False
            issues.append(issue)
        return True

    def finish() -> PythonDependencyClosure:
        return _finish(issues, catalog, constraints, environment, python_version)

    for name, pin in sorted(locked.items()):
        entries = catalog.get(name, [])
        matches = [entry for entry in entries if entry.wheel.version == pin.version
            and entry.wheel.sha256 in pin.hashes]
        if not matches:
            if not report('metadata_mismatch' if entries else 'metadata_missing', name):
                return finish()
            continue
        if len(matches) != 1:
            if not report('metadata_ambiguous', name):
                return finish()
            continue
        selected[name] = matches[0]
        try:
            python_spec = _python_spec(matches[0])
            if (not python_spec.contains(python_version, prereleases=True)
                and not report('requires_python', name, name,
                    constraint=str(python_spec), locked_version=pin.version)):
                return finish()
        except _PARSE_ERRORS:
            if not report('invalid_metadata', name):
                return finish()

    # Each (locked package, extra) is visited once. Empty extra is always a root.
    pending = deque((name, '') for name in sorted(selected))
    seen = set(pending)
    scheduled_edges = sum(len(item.requires_dist) for item in selected.values())
    edges = 0
    while pending:
        name, extra = pending.popleft()
        for text in selected[name].requires_dist:
            if edges >= max_edges:
                report('edge_limit', name)
                return finish()
            edges += 1
            try:
                dependency = _requirement(text)
                active = dependency.marker is None or dependency.marker.evaluate(
                    environment={**environment, 'extra': extra}, context='requirement')
                if not active:
                    continue
                target = str(canonicalize_name(dependency.name))
                constraints.setdefault(target, []).append(dependency.specifier)
                target_pin = locked.get(target)
                if target_pin is None:
                    if not report('missing_dependency', name, target,
                        constraint=str(dependency.specifier)):
                        return finish()
                    continue
                if (not dependency.specifier.contains(Version(target_pin.version), prereleases=True)
                    and not report('version_conflict', name, target,
                        constraint=str(dependency.specifier), locked_version=target_pin.version)):
                    return finish()
                if target in selected and selected[target].requires_dist:
                    for activated in sorted(dependency.extras):
                        context = (target, str(canonicalize_name(activated)))
                        if context not in seen:
                            scheduled_edges += len(selected[target].requires_dist)
                            if scheduled_edges > max_edges:
                                report('edge_limit', target)
                                return finish()
                            seen.add(context)
                            pending.append(context)
            except _PARSE_ERRORS:
                if not report('unsupported_dependency', name):
                    return finish()
    return finish()


def _candidates(
    name: str, catalog: dict[str, list[PythonWheelMetadata]],
    constraints: dict[str, list[SpecifierSet]], environment: dict[str, str],
    python_version: Version, remaining: int,
) -> tuple[tuple[PythonWheelBinding, ...], int]:
    entries = catalog.get(name, [])
    counts: dict[PythonWheelBinding, int] = {}
    for item in entries:
        counts[item.wheel] = counts.get(item.wheel, 0) + 1
    result: list[PythonWheelBinding] = []
    for item in sorted(entries, key=lambda entry: (
        entry.wheel.version, entry.wheel.filename, entry.wheel.sha256)):
        if counts[item.wheel] != 1:
            continue
        cost = 1 + len(item.requires_dist) + len(constraints.get(name, []))
        if cost > remaining:
            break
        remaining -= cost
        try:
            version = Version(item.wheel.version)
            if not _python_spec(item).contains(python_version, prereleases=True):
                continue
            if not all(spec.contains(version, prereleases=True) for spec in constraints.get(name, [])):
                continue
            for text in item.requires_dist:
                dependency = _requirement(text)
                if dependency.marker is not None:
                    dependency.marker.evaluate(
                        environment={**environment, 'extra': ''}, context='requirement')
        except _PARSE_ERRORS:
            continue
        result.append(item.wheel)
    result.sort(key=lambda wheel: (Version(wheel.version), wheel.version, wheel.filename, wheel.sha256))
    return tuple(result[:3]), remaining


def _finish(
    issues: list[PythonDependencyClosureIssue], catalog: dict[str, list[PythonWheelMetadata]],
    constraints: dict[str, list[SpecifierSet]], environment: dict[str, str], python_version: Version,
) -> PythonDependencyClosure:
    bounded: list[PythonDependencyClosureIssue] = []
    suggestions: dict[str, tuple[PythonWheelBinding, ...]] = {}
    remaining = 4096
    for issue in issues:
        candidates: tuple[PythonWheelBinding, ...] = ()
        if issue.code in _CORRECTABLE:
            if issue.dependency not in suggestions:
                suggestions[issue.dependency], remaining = _candidates(
                    issue.dependency, catalog, constraints, environment, python_version, remaining)
            candidates = suggestions[issue.dependency]
        bounded.append(PythonDependencyClosureIssue(
            issue.code, issue.source, issue.dependency, candidates,
            issue.constraint, issue.locked_version))
    # Reserve room for a terminal limit issue; keep ordinary JSON output <=8192B.
    while _payload_bytes({'status': 'metadata_unavailable',
        'issues': [issue.to_payload() for issue in bounded]}) > _MAX_BYTES - 100:
        if bounded[-1].code == 'issue_limit':
            bounded.pop()
        bounded.pop()
        bounded.append(PythonDependencyClosureIssue('issue_limit', '', ''))
    result = tuple(bounded)
    return PythonDependencyClosure(_status(result), result)
