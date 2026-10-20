"""A Python recipe is captured from the original, never a later model rewrite."""

import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity
from agent_hub.capabilities.python_project_recipe import PythonProjectRecipeError
from agent_hub.capabilities.python_project_wheels import PythonWheelBinding
from tests.unit.capabilities.test_project_baselines import source_tree
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST


@pytest.fixture
def module() -> Any:
    name = 'agent_hub.capabilities.python_project_input_store'
    assert importlib.util.find_spec(name) is not None, 'Python original recipe store missing'
    return importlib.import_module(name)


def selected() -> tuple[PythonWheelBinding, ...]:
    return tuple(PythonWheelBinding(f'{name}-{version}-py3-none-any.whl', name, version, char * 64)
        for name, version, char in [('packaging', '25.0', 'a'), ('setuptools', '80.9.0', 'b'), ('wheel', '0.45.1', 'c')])


def interpreter() -> PythonInterpreterIdentity:
    return PythonInterpreterIdentity('cpython', '3.12.14', 'cp312', 'linux_x86_64', '1' * 64, '2' * 64)


def python_controls(source: Path) -> None:
    (source / 'pyproject.toml').write_bytes(MANIFEST)
    (source / 'requirements.lock').write_bytes(LOCK)
    (source / 'src/billing').mkdir()
    (source / 'src/billing/__init__.py').write_bytes(b'def total(): return 0\n')
    (source / 'tests/test_billing.py').write_bytes(b'import unittest\n')


async def test_recipe_persists_once_and_restart_uses_original_controls(module: Any, tmp_path: Path) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    python_controls(source)
    baselines = ProjectBaselineStore(root)
    reference = await baselines.capture_once(invocation, ManagedProjectInput(('src',)))
    before = (root / '.project-contracts' / str(invocation.tenant_id) / str(invocation.run_id) / 'baseline.json').read_bytes()
    store = module.PythonProjectInputStore(root)
    result = store.capture_once(invocation, reference, interpreter=interpreter(), wheels=selected(),
        max_control_bytes=256 * 1024, max_requirements=128)
    (source / 'pyproject.toml').write_bytes(b'changed after original')
    restarted = module.PythonProjectInputStore(root).load(invocation, reference, expected_sha256=result.sha256)
    assert restarted == result
    assert result.declaration.manifest_sha256 == hashlib.sha256(MANIFEST).hexdigest()
    assert (root / '.project-contracts' / str(invocation.tenant_id) / str(invocation.run_id) / 'baseline.json').read_bytes() == before
    with pytest.raises(PythonProjectRecipeError):
        store.capture_once(invocation, reference, interpreter=interpreter(), wheels=selected(),
            max_control_bytes=256 * 1024, max_requirements=128)
    with pytest.raises(PythonProjectRecipeError):
        store.load(invocation, reference, expected_sha256='a' * 64)
    with pytest.raises(PythonProjectRecipeError):
        store.load(invocation.model_copy(update={'run_id': UUID(int=99)}), reference, expected_sha256=result.sha256)


async def test_serialized_recipe_cannot_replace_original_manifest_even_if_resigned(module: Any, tmp_path: Path) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    python_controls(source)
    reference = await ProjectBaselineStore(root).capture_once(invocation, ManagedProjectInput(('src',)))
    store = module.PythonProjectInputStore(root)
    result = store.capture_once(invocation, reference, interpreter=interpreter(), wheels=selected(),
        max_control_bytes=256 * 1024, max_requirements=128)
    path = root / '.project-contracts' / str(invocation.tenant_id) / str(invocation.run_id) / 'python-recipe.json'
    payload = result.to_payload()
    cast(dict[str, object], payload['declaration'])['import_module'] = 'replacement'
    body = {key: value for key, value in payload.items() if key not in {'recipe_sha256', 'environment_input_sha256'}}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    payload['recipe_sha256'] = digest
    path.write_text(json.dumps(payload, sort_keys=True, separators=(',', ':')), encoding='ascii')
    with pytest.raises(PythonProjectRecipeError):
        store.load(invocation, reference, expected_sha256=digest)


async def test_partial_recipe_is_a_consumed_fence_not_permission_to_recreate(module: Any, tmp_path: Path) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    python_controls(source)
    reference = await ProjectBaselineStore(root).capture_once(invocation, ManagedProjectInput(('src',)))
    path = root / '.project-contracts' / str(invocation.tenant_id) / str(invocation.run_id) / 'python-recipe.json'
    path.write_bytes(b'{partial')
    path.chmod(0o600)
    store = module.PythonProjectInputStore(root)
    with pytest.raises(PythonProjectRecipeError):
        store.load(invocation, reference, expected_sha256='a' * 64)
    with pytest.raises(PythonProjectRecipeError):
        store.capture_once(invocation, reference, interpreter=interpreter(), wheels=selected(),
            max_control_bytes=256 * 1024, max_requirements=128)
    assert path.read_bytes() == b'{partial'


@pytest.mark.parametrize('defect', ['missing_package', 'single_module', 'setup.py', 'setup.cfg',
    'editable_tests', 'skipped_directory', 'skipped_filename'])
async def test_rejects_layouts_not_bound_to_fixed_packaged_build_and_original_tests(
    module: Any, tmp_path: Path, defect: str,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    python_controls(source)
    if defect in {'missing_package', 'single_module'}:
        (source / 'src/billing/__init__.py').unlink()
        if defect == 'single_module':
            (source / 'src/billing.py').write_bytes(b'pass')
    if defect.startswith('setup.'):
        (source / defect).write_bytes(b'unsupported hook')
    if defect in {'skipped_directory', 'skipped_filename'}:
        (source / 'tests/test_billing.py').unlink()
        if defect == 'skipped_directory':
            (source / 'tests/unit').mkdir()
            (source / 'tests/unit/test_billing.py').write_bytes(b'import unittest\n')
        else:
            (source / 'tests/test-billing.py').write_bytes(b'import unittest\n')
    selection = ('src', 'tests') if defect == 'editable_tests' else ('src',)
    reference = await ProjectBaselineStore(root).capture_once(invocation, ManagedProjectInput(selection))
    with pytest.raises(PythonProjectRecipeError):
        module.PythonProjectInputStore(root).capture_once(invocation, reference,
            interpreter=interpreter(), wheels=selected(), max_control_bytes=256 * 1024, max_requirements=128)


async def test_dependency_count_is_not_confused_with_source_file_count(module: Any, tmp_path: Path) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    python_controls(source)
    invocation = invocation.model_copy(update={'file_limit': 7})
    extras = tuple(PythonWheelBinding(f'helper{i}-1.0-py3-none-any.whl', f'helper{i}', '1.0', 'd' * 64)
        for i in range(5))
    lock = LOCK + b''.join(f'{item.name}==1.0 --hash=sha256:{item.sha256}\n'.encode() for item in extras)
    (source / 'requirements.lock').write_bytes(lock)
    reference = await ProjectBaselineStore(root).capture_once(invocation, ManagedProjectInput(('src',)))
    store = module.PythonProjectInputStore(root)
    result = store.capture_once(invocation, reference, interpreter=interpreter(), wheels=(*selected(), *extras),
        max_control_bytes=256 * 1024, max_requirements=128)
    assert len(result.declaration.requirements) > invocation.file_limit
    assert store.load(invocation, reference, expected_sha256=result.sha256) == result


async def test_nested_original_test_package_is_supported(module: Any, tmp_path: Path) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    python_controls(source)
    (source / 'tests/test_billing.py').unlink()
    (source / 'tests/unit').mkdir()
    (source / 'tests/unit/__init__.py').write_bytes(b'')
    (source / 'tests/unit/test_billing.py').write_bytes(b'import unittest\n')
    reference = await ProjectBaselineStore(root).capture_once(invocation, ManagedProjectInput(('src',)))
    result = module.PythonProjectInputStore(root).capture_once(invocation, reference,
        interpreter=interpreter(), wheels=selected(), max_control_bytes=256 * 1024, max_requirements=128)
    assert result.original_reference == reference


async def test_capture_or_load_requires_independently_rebuilt_matching_recipe(
    module: Any, tmp_path: Path,
) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    python_controls(source)
    reference = await ProjectBaselineStore(root).capture_once(invocation, ManagedProjectInput(('src',)))
    store = module.PythonProjectInputStore(root)
    kwargs = {'interpreter': interpreter(), 'wheels': selected(), 'max_control_bytes': 256 * 1024,
              'max_requirements': 128}
    first = store.capture_or_load_matching(invocation, reference, **kwargs)
    path = root / '.project-contracts' / str(invocation.tenant_id) / str(invocation.run_id) / 'python-recipe.json'
    before = path.read_bytes()
    (source / 'pyproject.toml').write_bytes(b'current is not recipe scope')
    assert store.capture_or_load_matching(invocation, reference, **kwargs) == first
    assert path.read_bytes() == before
    kwargs['interpreter'] = PythonInterpreterIdentity(
        'cpython', '3.12.15', 'cp312', 'linux_x86_64', '3' * 64, '2' * 64)
    with pytest.raises(PythonProjectRecipeError):
        store.capture_or_load_matching(invocation, reference, **kwargs)
    assert path.read_bytes() == before


async def test_capture_or_load_never_rewrites_partial_recipe(module: Any, tmp_path: Path) -> None:
    root = tmp_path / 'workspaces'
    invocation, source = source_tree(root)
    python_controls(source)
    reference = await ProjectBaselineStore(root).capture_once(invocation, ManagedProjectInput(('src',)))
    path = root / '.project-contracts' / str(invocation.tenant_id) / str(invocation.run_id) / 'python-recipe.json'
    path.write_bytes(b'{partial')
    path.chmod(0o600)
    with pytest.raises(PythonProjectRecipeError):
        module.PythonProjectInputStore(root).capture_or_load_matching(
            invocation, reference, interpreter=interpreter(), wheels=selected(),
            max_control_bytes=256 * 1024, max_requirements=128)
    assert path.read_bytes() == b'{partial'
