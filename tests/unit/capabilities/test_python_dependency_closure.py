"""Pure closure observations never authorize installation or rewrite locks."""

from __future__ import annotations

import importlib
import importlib.util
import json
from dataclasses import FrozenInstanceError
from types import MappingProxyType
from typing import Any

import pytest

from agent_hub.capabilities.python_project_recipe import PythonLockedRequirement
from agent_hub.capabilities.python_project_wheels import PythonWheelBinding

SHA = 'a' * 64
OTHER_SHA = 'b' * 64
ENV = {
    'implementation_name': 'cpython',
    'implementation_version': '3.12.4',
    'os_name': 'posix',
    'platform_machine': 'x86_64',
    'platform_release': '6.8',
    'platform_system': 'Linux',
    'platform_version': '#1',
    'platform_python_implementation': 'CPython',
    'python_full_version': '3.12.4',
    'python_version': '3.12',
    'sys_platform': 'linux',
}


def test_pure_closure_api_is_available() -> None:
    assert importlib.util.find_spec(
        'agent_hub.capabilities.python_dependency_closure') is not None, (
        'Pure dependency closure API missing')


@pytest.fixture
def api() -> Any:
    name = 'agent_hub.capabilities.python_dependency_closure'
    assert importlib.util.find_spec(name) is not None, 'Pure dependency closure API missing'
    return importlib.import_module(name)


def lock(name: str = 'demo', version: str = '1.0') -> PythonLockedRequirement:
    return PythonLockedRequirement(name, version, (SHA,))


def wheel(name: str = 'demo', version: str = '1.0', sha: str = SHA) -> PythonWheelBinding:
    return PythonWheelBinding(f'{name.replace("-", "_")}-{version}-py3-none-any.whl',
        name, version, sha)


def meta(
    api: Any, name: str = 'demo', deps: tuple[str, ...] = (), *,
    version: str = '1.0', python: str | None = None, sha: str = SHA,
) -> Any:
    return api.PythonWheelMetadata(wheel(name, version, sha), python, deps)


def codes(result: Any) -> set[str]:
    return {issue.code for issue in result.issues}


def test_metadata_round_trip_is_strict_detached_and_frozen(api: Any) -> None:
    item = meta(api, deps=('helper>=2; python_version >= "3.12"',), python='>=3.12')
    payload = item.to_payload()
    assert payload == {
        'wheel': {'filename': 'demo-1.0-py3-none-any.whl', 'name': 'demo',
            'version': '1.0', 'sha256': SHA},
        'requires_python': '>=3.12',
        'requires_dist': ['helper>=2; python_version >= "3.12"'],
    }
    assert api.PythonWheelMetadata.from_payload(json.loads(json.dumps(payload))) == item
    payload['requires_dist'].clear()
    assert item.requires_dist
    with pytest.raises(FrozenInstanceError):
        item.requires_python = '<3'
    assert not hasattr(item, '__dict__')


@pytest.mark.parametrize('change', [
    {'extra': True}, {'requires_python': True}, {'requires_dist': ('helper',)},
    {'requires_dist': [False]}, {'requires_dist': 'helper'}, {'wheel': {}},
    {'wheel': {'filename': 'https://secret/w.whl', 'name': 'demo',
        'version': '1.0', 'sha256': SHA}},
])
def test_metadata_payload_rejects_wrong_shapes_without_echo(api: Any, change: dict[str, Any]) -> None:
    payload = meta(api).to_payload() | change
    with pytest.raises(ValueError) as exc:
        api.PythonWheelMetadata.from_payload(payload)
    assert 'secret' not in str(exc.value)


@pytest.mark.parametrize('value', [None, [], True, {'wheel': None}])
def test_metadata_payload_rejects_missing_fields(api: Any, value: object) -> None:
    with pytest.raises(ValueError):
        api.PythonWheelMetadata.from_payload(value)


@pytest.mark.parametrize('field,value', [
    ('wheel', {}), ('requires_python', 12), ('requires_dist', []),
    ('requires_dist', (None,)), ('requires_dist', ('x' * 4097,)),
    ('requires_dist', ('helper',) * 4097), ('requires_python', 'x' * 4097),
])
def test_metadata_constructor_enforces_finite_plain_values(
    api: Any, field: str, value: object,
) -> None:
    fields: dict[str, object] = {'wheel': wheel(), 'requires_python': None, 'requires_dist': ()}
    fields[field] = value
    with pytest.raises(ValueError):
        api.PythonWheelMetadata(**fields)


def test_all_locked_items_including_build_and_unreferenced_items_are_roots(api: Any) -> None:
    locks = (lock(), lock('setuptools'), lock('unused'))
    metadata = (meta(api), meta(api, 'setuptools', ('build-helper>=2',)),
        meta(api, 'unused', ('runtime-helper',)), meta(api, 'build-helper', version='2.0'))
    result = api.check_python_dependency_closure(locks, metadata, ENV)
    assert result.status == 'incomplete'
    assert codes(result) == {'missing_dependency'}
    assert {issue.dependency for issue in result.issues} == {'build-helper', 'runtime-helper'}
    build = next(issue for issue in result.issues if issue.dependency == 'build-helper')
    assert build.candidates == (wheel('build-helper', '2.0'),)
    assert locks == (lock(), lock('setuptools'), lock('unused'))


def test_complete_is_a_frozen_json_value_without_authority(api: Any) -> None:
    result = api.check_python_dependency_closure((lock(),), (meta(api),), MappingProxyType(ENV))
    assert result.status == 'complete'
    assert result.issues == ()
    assert result.to_payload() == {'status': 'complete', 'issues': []}
    assert json.loads(json.dumps(result.to_payload())) == result.to_payload()
    with pytest.raises(FrozenInstanceError):
        result.status = 'incomplete'
    assert not hasattr(result, '__dict__')


def test_closure_round_trip_revalidates_nested_values(api: Any) -> None:
    result = api.check_python_dependency_closure(
        (lock(),), (meta(api, deps=('helper>=2',)), meta(api, 'helper', version='2.0')), ENV)
    payload = json.loads(json.dumps(result.to_payload()))
    assert payload == {'status': 'incomplete', 'issues': [{
        'code': 'missing_dependency', 'source': 'demo', 'dependency': 'helper',
        'candidates': [wheel('helper', '2.0').to_payload()],
        'constraint': '>=2', 'locked_version': None,
    }]}
    assert api.PythonDependencyClosure.from_payload(payload) == result
    payload['issues'][0]['candidates'][0]['sha256'] = 'bad'
    with pytest.raises(ValueError):
        api.PythonDependencyClosure.from_payload(payload)
    assert result.issues[0].candidates == (wheel('helper', '2.0'),)
    with pytest.raises(FrozenInstanceError):
        result.issues[0].code = 'invalid_metadata'


@pytest.mark.parametrize('payload', [
    None, [], {'status': 'complete'}, {'status': 'complete', 'issues': (),},
    {'status': True, 'issues': []}, {'status': 'unknown', 'issues': []},
    {'status': 'complete', 'issues': [], 'install_authorized': True},
    {'status': 'incomplete', 'issues': []},
    {'status': 'complete', 'issues': [{'code': 'metadata_missing', 'source': 'demo',
        'dependency': '', 'candidates': []}]},
    {'status': 'incomplete', 'issues': [{'code': 'unknown', 'source': '',
        'dependency': '', 'candidates': []}]},
    {'status': 'incomplete', 'issues': [{'code': 'missing_dependency',
        'source': 'https://secret', 'dependency': 'helper', 'candidates': []}]},
    {'status': 'incomplete', 'issues': [{'code': 'missing_dependency',
        'source': 'demo', 'dependency': 'helper', 'candidates': [], 'raw': 'secret'}]},
    {'status': 'incomplete', 'issues': [{'code': 'missing_dependency',
        'source': 'demo', 'dependency': 'helper', 'candidates': []}] * 33},
    {'status': 'incomplete', 'issues': [{'code': 'metadata_missing', 'source': 'demo',
        'dependency': '', 'candidates': []}]},
])
def test_closure_payload_rejects_unsafe_or_inconsistent_values(api: Any, payload: object) -> None:
    if isinstance(payload, dict) and isinstance(payload.get('issues'), list):
        for issue in payload['issues']:
            if isinstance(issue, dict):
                issue.update(constraint='', locked_version=None)
    with pytest.raises(ValueError) as exc:
        api.PythonDependencyClosure.from_payload(payload)
    assert 'secret' not in str(exc.value)


@pytest.mark.parametrize('code,source,dependency', [
    ('missing_dependency', '', 'helper'), ('missing_dependency', 'demo', ''),
    ('version_conflict', '', ''), ('requires_python', 'demo', ''),
    ('metadata_missing', '', ''), ('metadata_missing', 'demo', 'helper'),
    ('invalid_environment', 'demo', ''), ('issue_limit', '', 'helper'),
])
def test_issue_payload_requires_code_appropriate_package_fields(
    api: Any, code: str, source: str, dependency: str,
) -> None:
    with pytest.raises(ValueError):
        api.PythonDependencyClosureIssue.from_payload({
            'code': code, 'source': source, 'dependency': dependency, 'candidates': [],
            'constraint': '', 'locked_version': None})


def test_closure_payload_rejects_oversized_safe_summary(api: Any) -> None:
    name = 'x' * 128
    payload = {'status': 'incomplete', 'issues': [{
        'code': 'missing_dependency', 'source': name, 'dependency': name,
        'candidates': [], 'constraint': '', 'locked_version': None,
    }] * 32}
    assert len(json.dumps(payload).encode()) > 8192
    with pytest.raises(ValueError):
        api.PythonDependencyClosure.from_payload(payload)


def test_candidates_with_unsupported_markers_are_not_recommended(api: Any) -> None:
    items = (meta(api, deps=('helper',)),
        meta(api, 'helper', ('leaf; "dev" in dependency_groups',)))
    result = api.check_python_dependency_closure((lock(),), items, ENV)
    assert result.status == 'incomplete'
    assert result.issues[0].candidates == ()


def test_missing_dependency_can_be_corrected_using_only_supplied_binding(api: Any) -> None:
    metadata = (meta(api, deps=('Helper_Pkg>=2,<3',)),
        meta(api, 'helper-pkg', version='2.0'), meta(api, 'helper-pkg', version='3.0'))
    before = api.check_python_dependency_closure((lock(),), metadata, ENV)
    assert before.status == 'incomplete'
    assert before.issues[0].dependency == 'helper-pkg'
    assert before.issues[0].candidates == (wheel('helper-pkg', '2.0'),)
    after = api.check_python_dependency_closure((lock(), lock('helper-pkg', '2.0')), metadata, ENV)
    assert after.status == 'complete'


def test_version_conflict_and_requires_python_can_be_corrected(api: Any) -> None:
    metadata = (meta(api, deps=('helper>=2',)),
        meta(api, 'helper', python='>=3.13'), meta(api, 'helper', version='2.0', python='>=3.12'))
    result = api.check_python_dependency_closure((lock(), lock('helper')), metadata, ENV)
    assert result.status == 'incomplete'
    assert codes(result) == {'version_conflict', 'requires_python'}
    assert all(issue.candidates == (wheel('helper', '2.0'),) for issue in result.issues)
    assert api.check_python_dependency_closure(
        (lock(), lock('helper', '2.0')), metadata, ENV).status == 'complete'


@pytest.mark.parametrize('key', tuple(ENV))
def test_missing_any_target_field_cannot_fall_back_to_host(api: Any, key: str) -> None:
    env = dict(ENV)
    del env[key]
    result = api.check_python_dependency_closure((lock(),), (meta(api),), env)
    assert result.status == 'unsupported'
    assert codes(result) == {'invalid_environment'}


@pytest.mark.parametrize('change', [
    {'extra': 'accidental'}, {'python_version': '3.11'}, {'python_full_version': 'bad'},
    {'implementation_version': ''}, {'os_name': True}, {'extras': 'foo'},
])
def test_invalid_target_environment_is_unsupported(api: Any, change: dict[str, object]) -> None:
    result = api.check_python_dependency_closure((lock(),), (meta(api),), ENV | change)
    assert result.status == 'unsupported'


@pytest.mark.parametrize('marker,expected', [
    ('sys_platform == "win32"', 'complete'), ('sys_platform == "linux"', 'incomplete'),
    ('python_full_version < "3.12.5"', 'incomplete'),
    ('os_name == "nt" or platform_system == "Windows"', 'complete'),
    ('extra == "feature"', 'complete'),
    ('"feature" in extras', 'unsupported'),
    ('"dev" in dependency_groups', 'unsupported'),
    ('os_name ~= "posix"', 'unsupported'),
])
def test_markers_use_explicit_target_and_base_extra(
    api: Any, marker: str, expected: str,
) -> None:
    result = api.check_python_dependency_closure(
        (lock(),), (meta(api, deps=(f'helper; {marker}',)),), ENV)
    assert result.status == expected


def test_transitive_extras_activate_revisit_and_cycles_terminate(api: Any) -> None:
    metadata = (
        meta(api, 'alpha', ('beta[Fast_Mode]',)),
        meta(api, 'beta', ('gamma[docs]; extra == "fast-mode"',)),
        meta(api, 'gamma', ('alpha[more]; extra == "docs"',)),
    )
    locks = tuple(lock(name) for name in ('alpha', 'beta', 'gamma'))
    assert api.check_python_dependency_closure(locks, metadata, ENV).status == 'complete'
    metadata = (meta(api, 'alpha', ('beta[Fast_Mode]', 'leaf; extra == "more"')),) + metadata[1:]
    result = api.check_python_dependency_closure(locks, metadata, ENV)
    assert result.status == 'incomplete'
    assert {issue.dependency for issue in result.issues} == {'leaf'}


def test_empty_extra_remains_active_when_other_extras_arrive(api: Any) -> None:
    metadata = (meta(api, deps=('helper[x]',)),
        meta(api, 'helper', ('base-only; extra != "x"', 'optional; extra == "x"')))
    result = api.check_python_dependency_closure((lock(), lock('helper')), metadata, ENV)
    assert {issue.dependency for issue in result.issues} == {'base-only', 'optional'}


def test_missing_metadata_has_its_own_status_and_no_invented_candidates(api: Any) -> None:
    result = api.check_python_dependency_closure((lock(), lock('helper')), (meta(api),), ENV)
    assert result.status == 'metadata_unavailable'
    assert codes(result) == {'metadata_missing'}
    assert result.issues[0].candidates == ()


@pytest.mark.parametrize('field', ['name', 'version', 'sha'])
def test_metadata_cannot_substitute_wrong_locked_identity(api: Any, field: str) -> None:
    kwargs: dict[str, str] = {'name': 'wrong'} if field == 'name' else {
        field: '2.0' if field == 'version' else OTHER_SHA}
    result = api.check_python_dependency_closure((lock(),), (meta(api, name=kwargs.get('name', 'demo'), version=kwargs.get('version', '1.0'), sha=kwargs.get('sha', SHA)),), ENV)
    assert result.status != 'complete'
    assert not any(issue.candidates for issue in result.issues)
    if field != 'name':
        assert result.status == 'unsupported'
        assert 'metadata_mismatch' in codes(result)


def test_ambiguous_exact_metadata_is_not_arbitrarily_selected(api: Any) -> None:
    first = meta(api)
    second = meta(api, deps=('hidden',))
    result = api.check_python_dependency_closure((lock(),), (first, second), ENV)
    assert result.status == 'unsupported'
    assert 'metadata_ambiguous' in codes(result)


@pytest.mark.parametrize('dep', [
    'helper @ https://user:secret@invalid/private.whl?token=secret',
    'helper @ file:///private/secret.whl',
    'helper @ https://invalid/secret.whl ; sys_platform == "win32"',
    'helper===secret', 'not a valid requirement /secret',
    'helper; (' + '(' * 100 + 'os_name == "nt"' + ')' * 101,
])
def test_unsupported_or_url_dependencies_are_safe_and_never_complete(api: Any, dep: str) -> None:
    result = api.check_python_dependency_closure((lock(),), (meta(api, deps=(dep,)),), ENV)
    assert result.status == 'unsupported'
    payload = json.dumps(result.to_payload())
    assert all(text not in payload for text in ('secret', 'https:', 'file:', '/private'))


def test_invalid_requires_python_is_safe_unsupported(api: Any) -> None:
    result = api.check_python_dependency_closure(
        (lock(),), (meta(api, python='https://secret.invalid'),), ENV)
    assert result.status == 'unsupported'
    assert 'secret' not in json.dumps(result.to_payload())


@pytest.mark.parametrize('field', ['max_edges', 'max_issues'])
@pytest.mark.parametrize('value', [True, False, 0, -1, 1.5, '2', 1000000])
def test_budgets_reject_boolean_noninteger_and_unbounded_values(
    api: Any, field: str, value: object,
) -> None:
    with pytest.raises(ValueError):
        api.check_python_dependency_closure((lock(),), (meta(api),), ENV, **{field: value})


def test_edge_budget_counts_inactive_edges_and_extra_evaluations(api: Any) -> None:
    item = meta(api, deps=('helper; sys_platform == "win32"',) * 2)
    assert api.check_python_dependency_closure(
        (lock(),), (item,), ENV, max_edges=2).status == 'complete'
    result = api.check_python_dependency_closure((lock(),), (item,), ENV, max_edges=1)
    assert result.status == 'unsupported'
    assert 'edge_limit' in codes(result)
    metadata = (meta(api, deps=('helper[x]',)), meta(api, 'helper', ('demo',)))
    assert api.check_python_dependency_closure(
        (lock(), lock('helper')), metadata, ENV, max_edges=2).status == 'unsupported'


def test_issue_count_and_safe_summary_bytes_are_bounded(api: Any) -> None:
    deps = tuple('missing-' + str(i) + '-' + 'x' * 110 for i in range(80))
    result = api.check_python_dependency_closure((lock(),), (meta(api, deps=deps),), ENV)
    assert result.status == 'unsupported'
    assert 0 < len(result.issues) <= 32
    assert len(json.dumps(result.to_payload()).encode()) <= 8192
    small = api.check_python_dependency_closure(
        (lock(),), (meta(api, deps=deps),), ENV, max_issues=1)
    assert small.status == 'unsupported'
    assert len(small.issues) == 1


@pytest.mark.parametrize('kind', ['requirements', 'metadata', 'duplicate_lock', 'list_lock'])
def test_input_cardinality_and_shapes_are_finite(api: Any, kind: str) -> None:
    locks: Any = (lock(),)
    items: tuple[object, ...] = (meta(api),)
    if kind == 'requirements':
        locks = tuple(lock(f'pkg-{i}') for i in range(129))
    elif kind == 'metadata':
        items *= 1001
    elif kind == 'duplicate_lock':
        locks *= 2
    else:
        locks = list(locks)
    with pytest.raises(ValueError):
        api.check_python_dependency_closure(locks, items, ENV)


def test_catalog_and_input_order_cannot_change_result(api: Any) -> None:
    items = (meta(api, deps=('helper>=2',)), meta(api, 'unused'),
        meta(api, 'helper', version='3.0'), meta(api, 'helper', version='2.0'))
    locks = (lock(), lock('unused'))
    first = api.check_python_dependency_closure(locks, items, ENV)
    assert first == api.check_python_dependency_closure(locks[::-1], items[::-1], ENV)
    assert first.issues[0].candidates == (wheel('helper', '2.0'), wheel('helper', '3.0'))


def test_candidates_respect_all_observed_constraints_and_python(api: Any) -> None:
    items = (meta(api, 'alpha', ('helper>=2',)), meta(api, 'beta', ('helper<3',)),
        meta(api, 'helper', version='2.0'), meta(api, 'helper', version='3.0'),
        meta(api, 'helper', version='2.1', python='>=3.13'))
    result = api.check_python_dependency_closure((lock('alpha'), lock('beta')), items, ENV)
    assert result.status == 'incomplete'
    assert all(issue.candidates == (wheel('helper', '2.0'),) for issue in result.issues)


def test_conflicting_candidate_metadata_is_not_a_repair_suggestion(api: Any) -> None:
    items = (meta(api, deps=('helper',)), meta(api, 'helper'),
        meta(api, 'helper', ('unknown',)))
    result = api.check_python_dependency_closure((lock(),), items, ENV)
    assert result.status == 'incomplete'
    assert result.issues[0].candidates == ()


def test_no_candidates_are_fabricated_and_no_dependencies_are_auto_locked(api: Any) -> None:
    original = (lock(),)
    result = api.check_python_dependency_closure(original, (meta(api, deps=('helper>=2',)),), ENV)
    assert result.status == 'incomplete'
    assert result.issues[0].candidates == ()
    assert original == (lock(),)


def test_semantic_issues_report_canonical_constraints_and_actual_locked_version(api: Any) -> None:
    items = (meta(api, deps=(
        'helper ( >= 2, < 3 ); python_version >= "3.12"',
        'missing >= 4, != 5', 'unconstrained',
    )), meta(api, 'helper', python=' >= 3.13, < 4 '))
    result = api.check_python_dependency_closure((lock(), lock('helper')), items, ENV)
    actual = {(issue.code, issue.dependency): (issue.constraint, issue.locked_version)
        for issue in result.issues}
    assert actual == {
        ('version_conflict', 'helper'): ('<3,>=2', '1.0'),
        ('requires_python', 'helper'): ('<4,>=3.13', '1.0'),
        ('missing_dependency', 'missing'): ('!=5,>=4', None),
        ('missing_dependency', 'unconstrained'): ('', None),
    }
    assert api.PythonDependencyClosure.from_payload(result.to_payload()) == result
    payload = json.dumps(result.to_payload())
    assert 'python_version' not in payload


def test_distinct_constraints_for_same_edge_are_not_silently_collapsed(api: Any) -> None:
    result = api.check_python_dependency_closure(
        (lock(),), (meta(api, deps=('helper>=2', 'helper<3')),), ENV)
    assert result.status == 'incomplete'
    assert {issue.constraint for issue in result.issues} == {'>=2', '<3'}


@pytest.mark.parametrize('change', [
    {'constraint': True}, {'constraint': None}, {'constraint': '\u4e2d'},
    {'constraint': '>=2\n'}, {'constraint': 'x' * 1025},
    {'constraint': 'https://secret.invalid'}, {'constraint': '===secret'},
    {'constraint': '==secret'}, {'constraint': '>=2; os_name == "posix"'},
    {'constraint': '>=2, <3'}, {'constraint': '>=2,<3'},
    {'locked_version': True}, {'locked_version': 1}, {'locked_version': ''},
    {'locked_version': 'https://secret.invalid'}, {'locked_version': '1.0\n'},
    {'locked_version': '1.0+\u4e2d'}, {'locked_version': '1' * 129},
    {'locked_version': 'v1.0'}, {'locked_version': '1.0RC1'},
])
def test_new_issue_fields_reject_unsafe_noncanonical_and_invalid_versions(
    api: Any, change: dict[str, object],
) -> None:
    fields = {'code': 'version_conflict', 'source': 'demo', 'dependency': 'helper',
        'candidates': [], 'constraint': '>=2', 'locked_version': '1.0'} | change
    with pytest.raises(ValueError) as exc:
        api.PythonDependencyClosureIssue.from_payload(fields)
    assert 'secret' not in str(exc.value)


@pytest.mark.parametrize('code,constraint,version', [
    ('metadata_missing', '>=2', None), ('metadata_missing', '', '1.0'),
    ('unsupported_dependency', '>=2', None), ('missing_dependency', '>=2', '1.0'),
    ('version_conflict', '>=2', None), ('version_conflict', '', '1.0'),
    ('requires_python', '>=3.13', None), ('requires_python', '', '1.0'),
])
def test_issue_semantics_restrict_new_fields_to_meaningful_codes(
    api: Any, code: str, constraint: str, version: str | None,
) -> None:
    dependency = '' if code in ('metadata_missing', 'unsupported_dependency') else 'helper'
    with pytest.raises(ValueError):
        api.PythonDependencyClosureIssue.from_payload({
            'code': code, 'source': 'demo', 'dependency': dependency, 'candidates': [],
            'constraint': constraint, 'locked_version': version})


@pytest.mark.parametrize('field', ['constraint', 'locked_version'])
def test_issue_payload_requires_new_fields_even_when_defaulted(api: Any, field: str) -> None:
    payload: dict[str, object] = {'code': 'missing_dependency', 'source': 'demo', 'dependency': 'helper',
        'candidates': [], 'constraint': '>=2', 'locked_version': None}
    del payload[field]
    with pytest.raises(ValueError):
        api.PythonDependencyClosureIssue.from_payload(payload)


def test_nonsemantic_issues_keep_new_fields_empty_and_do_not_leak_urls(api: Any) -> None:
    items = (meta(api, deps=('helper @ https://user:secret@invalid/private.whl',)),)
    result = api.check_python_dependency_closure((lock(), lock('absent')), items, ENV)
    assert result.status == 'unsupported'
    assert all(issue.constraint == '' and issue.locked_version is None for issue in result.issues)
    assert 'secret' not in json.dumps(result.to_payload())
    assert api.PythonDependencyClosure.from_payload(result.to_payload()) == result


def test_constraint_overflow_is_unsupported_without_raw_metadata_echo(api: Any) -> None:
    spec = ','.join(f'!={i}' for i in range(220))
    assert len(spec) > 1024
    result = api.check_python_dependency_closure(
        (lock(),), (meta(api, deps=(f'helper{spec}',)),), ENV)
    assert result.status == 'unsupported'
    assert all(issue.constraint == '' and issue.locked_version is None for issue in result.issues)
    assert len(json.dumps(result.to_payload()).encode()) <= 8192
