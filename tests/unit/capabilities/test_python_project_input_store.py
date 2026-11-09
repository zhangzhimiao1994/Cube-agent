"""A Python recipe is captured from the original, never a later model rewrite."""

import hashlib
import importlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity, PythonProjectInputs
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


@pytest.fixture
def private_capture(module: Any, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Any:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
    from agent_hub.capabilities.project_contracts import build_project_check_contract
    from agent_hub.capabilities.python_original_metadata import standard_python_intent
    from agent_hub.capabilities.python_original_private_recipe import (
        derive_private_original_recipe, original_dependency_needs,
    )
    from agent_hub.skills.sandbox.project import ProjectInvocation
    from tests.unit.capabilities.test_standard_python_original_intent import (
        FILES, MANIFEST as STANDARD_MANIFEST, PROTECTED,
    )
    from tests.unit.capabilities.test_standard_python_private_recipe import ENVIRONMENT, _descriptor

    invocation = ProjectInvocation(
        execution_id='original', tenant_id=UUID(int=1), run_id=UUID(int=2),
        project_id='project', session_id='session', sandbox_profile='workspace_write',
        timeout_seconds=30,
    )
    legacy = getattr(request, 'param', None) == 'declared'
    if legacy:
        source = {'pyproject.toml': MANIFEST, 'requirements.lock': LOCK,
                  'src/billing/__init__.py': b'', 'tests/test_billing.py': b'import unittest\n'}
        editable = ('src',)
    else:
        source = dict.fromkeys(FILES, b'pass\n')
        source['pyproject.toml'] = STANDARD_MANIFEST
        editable = ('src/expense_cli/cli.py',)
    source_files = {name: hashlib.sha256(b'f\0' + data).hexdigest() for name, data in source.items()}
    source_sha = hashlib.sha256(module._encoded(source_files)).hexdigest()
    reference = ProjectBaselineReference('a' * 64, source_sha, len(source_files))
    contract = build_project_check_contract(
        tenant_id=invocation.tenant_id, run_id=invocation.run_id,
        project_id=invocation.project_id, session_id=invocation.session_id,
        input_source_sha256=source_sha, editable_paths=editable,
        source_files=source_files,
    )
    snapshot = Path('original-snapshot')
    stored = StoredProjectBaseline(reference, contract, snapshot, source_files)
    kwargs: dict[str, object] = dict(interpreter=interpreter(), wheels=selected(),
        max_control_bytes=256 * 1024, max_requirements=128)
    measured = None
    if not legacy:
        intent = standard_python_intent(STANDARD_MANIFEST, paths=FILES, protected=PROTECTED)
        assert intent is not None
        descriptor = _descriptor()
        scope = replace(descriptor.scope, source_sha256=source_sha)
        descriptor = replace(descriptor, scope=scope, needs=original_dependency_needs(intent, ENVIRONMENT))
        measured = derive_private_original_recipe(
            intent, descriptor, expected_scope=scope, verified_descriptor_sha256=descriptor.sha256,
        )
        kwargs.update(interpreter=replace(interpreter(), version='3.12.8'), wheels=measured.wheels,
                      private_declaration=measured.declaration)
    records: dict[str, bytes] = {}

    class Controls:
        def info(self, name: str) -> None:
            if name not in records:
                raise FileNotFoundError(name)

        def read(self, name: str, maximum: int) -> bytes:
            self.info(name)
            assert len(records[name]) <= maximum
            return records[name]

        def check(self) -> None:
            pass

    controls = Controls()

    def read_original(path: Path, parts: tuple[str, ...], *, max_bytes: int) -> bytes:
        assert path == snapshot and len(parts) == 1
        return source[parts[0]][:max_bytes + 1]

    def write_once(parent: Controls, name: str, data: bytes) -> None:
        assert parent is controls
        if name in records:
            raise FileExistsError(name)
        records[name] = data

    monkeypatch.setattr(module, '_read_no_links', read_original)
    monkeypatch.setattr(ProjectBaselineStore, '_invocation', lambda _self, value, **_kw: value)
    monkeypatch.setattr(ProjectBaselineStore, 'load', lambda _self, _inv, _ref: stored)
    monkeypatch.setattr(ProjectBaselineStore, '_controls', lambda _self, _inv, stack, **_kw:
                        stack.enter_context(nullcontext(controls)))
    monkeypatch.setattr(ProjectBaselineStore, '_write_once', staticmethod(write_once))
    return SimpleNamespace(
        root=Path('workspaces'), invocation=invocation, reference=reference, records=records,
        source=source, measured=measured, kwargs=kwargs,
    )


def test_private_original_captures_and_reloads_without_source_lock(module: Any, private_capture: Any) -> None:
    fixture = private_capture
    store = module.PythonProjectInputStore(fixture.root)
    source_before = dict(fixture.source)
    result = store.capture_or_load_matching(fixture.invocation, fixture.reference, **fixture.kwargs)
    assert result.declaration == fixture.measured.declaration
    persisted = json.loads(fixture.records['python-recipe.json'])
    assert type(result) is PythonProjectInputs
    assert PythonProjectInputs.from_payload(persisted) == result
    restarted = module.PythonProjectInputStore(fixture.root)
    assert restarted.load(fixture.invocation, fixture.reference, expected_sha256=result.sha256) == result
    assert restarted.capture_or_load_matching(fixture.invocation, fixture.reference, **fixture.kwargs) == result
    assert fixture.source == source_before and 'requirements.lock' not in result.contract.protected_files


@pytest.mark.parametrize('drift', ['project', 'manifest', 'lock', 'requirements', 'manifest_bytes'])
def test_private_original_rejects_inconsistent_measured_recipe(
    module: Any, private_capture: Any, drift: str,
) -> None:
    fixture = private_capture
    declaration = fixture.measured.declaration
    if drift == 'project':
        declaration = replace(declaration, project_name='other')
    elif drift == 'manifest':
        declaration = replace(declaration, manifest_sha256='9' * 64)
    elif drift == 'lock':
        declaration = replace(declaration, lock_sha256='9' * 64)
    elif drift == 'requirements':
        declaration = replace(declaration, runtime_dependencies=())
    else:
        fixture.source['pyproject.toml'] += b'\n# changed after baseline'
    kwargs = fixture.kwargs | {'private_declaration': declaration}
    with pytest.raises(PythonProjectRecipeError):
        module.PythonProjectInputStore(fixture.root).capture_or_load_matching(
            fixture.invocation, fixture.reference, **kwargs,
        )
    assert fixture.records == {}


def test_private_original_does_not_rewrite_partial_recipe(module: Any, private_capture: Any) -> None:
    fixture = private_capture
    fixture.records['python-recipe.json'] = b'{partial'
    with pytest.raises(PythonProjectRecipeError):
        module.PythonProjectInputStore(fixture.root).capture_or_load_matching(
            fixture.invocation, fixture.reference, **fixture.kwargs,
        )
    assert fixture.records == {'python-recipe.json': b'{partial'}


def test_private_original_rejects_resigned_recipe_on_reload(module: Any, private_capture: Any) -> None:
    fixture = private_capture
    store = module.PythonProjectInputStore(fixture.root)
    result = store.capture_or_load_matching(fixture.invocation, fixture.reference, **fixture.kwargs)
    payload = result.to_payload()
    cast(dict[str, object], payload['declaration'])['lock_sha256'] = '9' * 64
    binding = {key: value for key, value in payload.items()
               if key not in {'recipe_sha256', 'environment_input_sha256'}}
    digest = hashlib.sha256(module._encoded(binding)).hexdigest()
    payload['recipe_sha256'] = digest
    fixture.records['python-recipe.json'] = module._encoded(payload)
    with pytest.raises(PythonProjectRecipeError, match='python_project_lock_invalid'):
        store.load(fixture.invocation, fixture.reference, expected_sha256=digest)


@pytest.mark.parametrize('private_capture', ['declared'], indirect=True)
def test_declared_original_recipe_keeps_legacy_payload(module: Any, private_capture: Any) -> None:
    fixture = private_capture
    store = module.PythonProjectInputStore(fixture.root)
    result = store.capture_or_load_matching(fixture.invocation, fixture.reference, **fixture.kwargs)
    assert result.to_payload() == json.loads(fixture.records['python-recipe.json'])
    assert 'requirements.lock' in result.contract.protected_files
    assert store.load(fixture.invocation, fixture.reference, expected_sha256=result.sha256) == result
