"""Wheel bindings check declared identities and hashes, never archive contents."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import FrozenInstanceError
from pathlib import PurePath
from typing import Any

import pytest

from agent_hub.capabilities.python_project_recipe import (
    PythonLockedRequirement,
    PythonProjectRecipeError,
)

SHA = 'a' * 64
OTHER_SHA = 'b' * 64
INVALID = 'python_project_recipe_invalid'
UNSUPPORTED = 'python_project_recipe_unsupported'
LOCK_INVALID = 'python_project_lock_invalid'


class StringSubclass(str):
    pass


class TupleSubclass(tuple[Any, ...]):
    pass


@pytest.fixture
def wheels() -> Any:
    name = 'agent_hub.capabilities.python_project_wheels'
    assert importlib.util.find_spec(name) is not None, 'Python wheel binding module missing'
    return importlib.import_module(name)


def binding(wheels: Any, **overrides: Any) -> Any:
    fields = {
        'filename': 'demo_pkg-1.0-py3-none-any.whl',
        'name': 'demo-pkg',
        'version': '1.0',
        'sha256': SHA,
    }
    fields.update(overrides)
    return wheels.PythonWheelBinding(**fields)


def locked(
    name: str = 'demo-pkg', version: str = '1.0', hashes: tuple[str, ...] = (SHA,),
) -> PythonLockedRequirement:
    return PythonLockedRequirement(name, version, hashes)


def test_binds_universal_wheel_and_serializes_only_declared_data() -> None:
    name = 'agent_hub.capabilities.python_project_wheels'
    assert importlib.util.find_spec(name) is not None, 'Python wheel binding module missing'
    wheels = importlib.import_module(name)
    wheel = binding(wheels, sha256=OTHER_SHA)
    result = wheels.bind_python_project_wheels((locked(hashes=(SHA, OTHER_SHA)),), (wheel,))
    assert type(result) is tuple
    assert result == (wheel,)
    assert result[0].to_payload() == {
        'filename': 'demo_pkg-1.0-py3-none-any.whl',
        'name': 'demo-pkg',
        'version': '1.0',
        'sha256': OTHER_SHA,
    }
    payload = result[0].to_payload()
    payload['sha256'] = SHA
    assert result[0].sha256 == OTHER_SHA


def test_binding_identity_cannot_be_mutated_after_validation(wheels: Any) -> None:
    wheel = binding(wheels)
    with pytest.raises(FrozenInstanceError):
        wheel.name = 'another'
    assert not hasattr(wheel, '__dict__')


@pytest.mark.parametrize('distribution', ['demo_pkg', 'Demo_Pkg', 'demo.pkg', 'demo__pkg'])
def test_filename_distribution_matches_canonical_name(wheels: Any, distribution: str) -> None:
    wheel = binding(wheels, filename=f'{distribution}-1.0-py3-none-any.whl')
    assert wheels.bind_python_project_wheels((locked(),), (wheel,)) == (wheel,)


@pytest.mark.parametrize('version', [
    '0', '1.2.3', '1.0a1', '1.0b2', '1.0rc3', '1.0.post1', '1.0.dev2',
    '1.0rc1.post2.dev3+cpu.1',
])
def test_accepts_unambiguous_recipe_versions(wheels: Any, version: str) -> None:
    wheel = binding(wheels, version=version, filename=f'demo_pkg-{version}-py3-none-any.whl')
    assert wheels.bind_python_project_wheels((locked(version=version),), (wheel,)) == (wheel,)


@pytest.mark.parametrize('version', ['1!1.0', '1.0+cpu_1', '1.0+cpu-1'])
def test_explicitly_rejects_versions_needing_filename_normalization(
    wheels: Any, version: str,
) -> None:
    with pytest.raises(PythonProjectRecipeError) as exc:
        binding(wheels, version=version, filename=f'demo_pkg-{version}-py3-none-any.whl')
    assert exc.value.code == UNSUPPORTED


@pytest.mark.parametrize('field', ['filename', 'name', 'version', 'sha256'])
@pytest.mark.parametrize('value', [None, True, False, 1, b'demo', [], {}, PurePath('demo')])
def test_rejects_non_plain_string_fields(wheels: Any, field: str, value: object) -> None:
    with pytest.raises(PythonProjectRecipeError) as exc:
        binding(wheels, **{field: value})
    assert exc.value.code == INVALID


@pytest.mark.parametrize(('field', 'value'), [
    ('filename', 'demo_pkg-1.0-py3-none-any.whl'), ('name', 'demo-pkg'),
    ('version', '1.0'), ('sha256', SHA),
])
def test_rejects_string_subclasses_even_with_valid_contents(
    wheels: Any, field: str, value: str,
) -> None:
    with pytest.raises(PythonProjectRecipeError) as exc:
        binding(wheels, **{field: StringSubclass(value)})
    assert exc.value.code == INVALID


@pytest.mark.parametrize('filename', [
    '../demo_pkg-1.0-py3-none-any.whl', 'folder/demo_pkg-1.0-py3-none-any.whl',
    r'folder\demo_pkg-1.0-py3-none-any.whl', '/demo_pkg-1.0-py3-none-any.whl',
    r'C:\demo_pkg-1.0-py3-none-any.whl', '//host/demo_pkg-1.0-py3-none-any.whl',
    'https://host/demo_pkg-1.0-py3-none-any.whl', 'file:demo_pkg-1.0-py3-none-any.whl',
    'demo_pkg-1.0-py3-none-any.whl:stream', 'demo_pkg-1.0-py3-none-any.whl?x=1',
    'demo_pkg-1.0-py3-none-any.whl#fragment', 'demo_pkg-1.0-py3-none-any.whl\x00',
    'demo_pkg-1.0-py3-none-any.whl\n', ' demo_pkg-1.0-py3-none-any.whl',
    'demo_pkg-1.0-py3-none-any.whl ', 'demo_pkg-1.0-py3-none-any.whl\u202e',
    '', '.', '..',
])
def test_rejects_paths_urls_and_unsafe_filename_symbols(wheels: Any, filename: str) -> None:
    with pytest.raises(PythonProjectRecipeError) as exc:
        binding(wheels, filename=filename)
    assert exc.value.code == INVALID


@pytest.mark.parametrize('filename', [
    'demo_pkg-1.0-cp312-none-any.whl', 'demo_pkg-1.0-py2.py3-none-any.whl',
    'demo_pkg-1.0-py3-abi3-any.whl', 'demo_pkg-1.0-py3-none-win_amd64.whl',
    'demo_pkg-1.0-1-py3-none-any.whl', 'demo_pkg-1.0-py3-none-any.WHL',
    'demo_pkg-1.0.tar.gz', 'demo_pkg-1.0-py3-none.whl',
])
def test_rejects_non_universal_tags_build_tags_and_non_wheels(wheels: Any, filename: str) -> None:
    with pytest.raises(PythonProjectRecipeError) as exc:
        binding(wheels, filename=filename)
    assert exc.value.code == UNSUPPORTED


@pytest.mark.parametrize('filename', [
    'other-1.0-py3-none-any.whl', 'demo_pkg-2.0-py3-none-any.whl',
    'demo_pkg-1.0.0-py3-none-any.whl', 'demo_pkg_-1.0-py3-none-any.whl',
])
def test_rejects_filename_identity_misrepresentation(wheels: Any, filename: str) -> None:
    with pytest.raises(PythonProjectRecipeError) as exc:
        binding(wheels, filename=filename)
    assert exc.value.code == INVALID


@pytest.mark.parametrize(('field', 'value'), [
    ('name', 'Demo_Pkg'), ('name', 'demo_pkg'), ('name', ''), ('name', '../demo'),
    ('version', 'v1.0'), ('version', '>=1.0'), ('version', '1.0RC1'),
    ('version', '1.0\n'), ('sha256', 'A' * 64), ('sha256', 'a' * 63),
    ('sha256', 'g' * 64), ('sha256', 'sha256:' + SHA), ('sha256', SHA + '\n'),
])
def test_rejects_identity_or_hash_outside_recipe_contract(
    wheels: Any, field: str, value: str,
) -> None:
    with pytest.raises(PythonProjectRecipeError) as exc:
        binding(wheels, **{field: value})
    assert exc.value.code == INVALID


@pytest.mark.parametrize(('version', 'sha256'), [('1.0.0', SHA), ('1.0', OTHER_SHA)])
def test_requires_exact_locked_version_and_hash(wheels: Any, version: str, sha256: str) -> None:
    wheel = binding(wheels, version=version, sha256=sha256,
        filename=f'demo_pkg-{version}-py3-none-any.whl')
    with pytest.raises(PythonProjectRecipeError) as exc:
        wheels.bind_python_project_wheels((locked(),), (wheel,))
    assert exc.value.code == LOCK_INVALID


@pytest.mark.parametrize('scenario', [
    'missing', 'extra', 'duplicate-name', 'duplicate-filename', 'duplicate-lock',
    'conflicting-lock', 'no-lock', 'no-wheels',
])
def test_requires_one_wheel_per_unique_locked_name(wheels: Any, scenario: str) -> None:
    wheel = binding(wheels)
    requirements: tuple[PythonLockedRequirement, ...] = (locked(),)
    selected: tuple[Any, ...] = (wheel,)
    if scenario == 'missing':
        requirements += (locked('other'),)
    elif scenario == 'extra':
        selected += (binding(wheels, name='other', filename='other-1.0-py3-none-any.whl'),)
    elif scenario == 'duplicate-name':
        selected += (binding(wheels, filename='Demo_Pkg-1.0-py3-none-any.whl'),)
    elif scenario == 'duplicate-filename':
        selected += (wheel,)
    elif scenario == 'duplicate-lock':
        requirements += (locked(),)
    elif scenario == 'conflicting-lock':
        requirements += (locked(version='2.0'),)
    elif scenario == 'no-lock':
        requirements = ()
    elif scenario == 'no-wheels':
        selected = ()
    with pytest.raises(PythonProjectRecipeError) as exc:
        wheels.bind_python_project_wheels(requirements, selected)
    assert exc.value.code == LOCK_INVALID


def test_selection_is_sorted_independently_of_both_input_orders(wheels: Any) -> None:
    alpha = binding(wheels, name='alpha', filename='alpha-1.0-py3-none-any.whl')
    zeta = binding(wheels, name='zeta', filename='zeta-1.0-py3-none-any.whl')
    for requirements in [(locked('alpha'), locked('zeta')), (locked('zeta'), locked('alpha'))]:
        for selected in [(alpha, zeta), (zeta, alpha)]:
            result = wheels.bind_python_project_wheels(requirements, selected)
            assert tuple(item.name for item in result) == ('alpha', 'zeta')
            assert result == (alpha, zeta)


def test_empty_selection_matches_empty_requirements(wheels: Any) -> None:
    assert wheels.bind_python_project_wheels((), ()) == ()


@pytest.mark.parametrize('value', [None, True, [], {}, 'demo', iter(()), TupleSubclass(())])
@pytest.mark.parametrize('argument', ['requirements', 'wheels'])
def test_rejects_non_plain_tuple_arguments(wheels: Any, argument: str, value: object) -> None:
    args: dict[str, object] = {'requirements': (locked(),), 'wheels': (binding(wheels),)}
    args[argument] = value
    with pytest.raises(PythonProjectRecipeError) as exc:
        wheels.bind_python_project_wheels(**args)
    assert exc.value.code == INVALID


@pytest.mark.parametrize('value', [None, True, 'demo', {}, object()])
@pytest.mark.parametrize('argument', ['requirements', 'wheels'])
def test_rejects_non_binding_or_non_requirement_items(
    wheels: Any, argument: str, value: object,
) -> None:
    args: dict[str, object] = {'requirements': (locked(),), 'wheels': (binding(wheels),)}
    args[argument] = (value,)
    with pytest.raises(PythonProjectRecipeError) as exc:
        wheels.bind_python_project_wheels(**args)
    assert exc.value.code == INVALID


@pytest.mark.parametrize('scenario', ['swapped', 'requirement-subclass', 'wheel-subclass'])
def test_rejects_wrong_declaration_classes(wheels: Any, scenario: str) -> None:
    requirement: Any = locked()
    wheel = binding(wheels)
    if scenario == 'swapped':
        requirement, wheel = wheel, requirement
    elif scenario == 'requirement-subclass':
        subclass = type('RequirementSubclass', (PythonLockedRequirement,), {})
        requirement = subclass('demo-pkg', '1.0', (SHA,))
    elif scenario == 'wheel-subclass':
        subclass = type('WheelSubclass', (wheels.PythonWheelBinding,), {})
        wheel = subclass('demo_pkg-1.0-py3-none-any.whl', 'demo-pkg', '1.0', SHA)
    with pytest.raises(PythonProjectRecipeError) as exc:
        wheels.bind_python_project_wheels((requirement,), (wheel,))
    assert exc.value.code == INVALID
