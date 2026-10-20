"""Python declarations are data, never host installation or execution authority."""
import hashlib
import importlib
import importlib.util
from typing import Any

import pytest

MANIFEST = b'''[project]
name = "billing-demo"
version = "1.0.0"
requires-python = ">=3.12,<3.13"
dependencies = ["packaging==25.0"]

[build-system]
requires = ["setuptools==80.9.0", "wheel==0.45.1"]
build-backend = "setuptools.build_meta"

[tool.agent-hub.python-project]
recipe = "python_offline_wheel_v1"
import-module = "billing"
test-directory = "tests"

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.packages.find]
where = ["src"]
'''
LOCK = (b'packaging==25.0 --hash=sha256:' + b'a' * 64 + b'\n'
    b'setuptools==80.9.0 --hash=sha256:' + b'b' * 64 + b'\n'
    b'wheel==0.45.1 --hash=sha256:' + b'c' * 64 + b'\n')


@pytest.fixture
def recipe() -> Any:
    name = 'agent_hub.capabilities.python_project_recipe'
    assert importlib.util.find_spec(name) is not None, 'Python project recipe parser missing'
    return importlib.import_module(name)


def parse(recipe: Any, manifest: bytes = MANIFEST, lock: bytes = LOCK) -> Any:
    return recipe.parse_python_project_declaration(manifest, lock,
        max_control_bytes=256 * 1024, max_requirements=128)


def test_captures_raw_manifest_and_lock_without_execution(recipe: Any) -> None:
    result = parse(recipe)
    assert result.manifest_sha256 == hashlib.sha256(MANIFEST).hexdigest()
    assert result.lock_sha256 == hashlib.sha256(LOCK).hexdigest()
    assert result.project_name == 'billing-demo'
    assert result.import_module == 'billing'
    assert result.kind == 'python_offline_wheel_v1'
    assert [(item.name, item.version) for item in result.requirements] == [
        ('packaging', '25.0'), ('setuptools', '80.9.0'), ('wheel', '0.45.1')]
    assert result.to_payload()['test_recipe'] == 'unittest_discover_v1'


def test_raw_line_endings_are_bound_without_changing_requirement_meaning(recipe: Any) -> None:
    first, second = parse(recipe), parse(recipe, lock=LOCK.replace(b'\n', b'\r\n'))
    assert first.lock_sha256 != second.lock_sha256
    assert first.requirements == second.requirements
    assert first.sha256 != second.sha256


@pytest.mark.parametrize('lock', [
    b'', b'packaging>=25 --hash=sha256:' + b'a' * 64,
    b'packaging==25.0', b'packaging @ https://untrusted.invalid/package.whl',
    b'-e .', b'--index-url https://untrusted.invalid\n' + LOCK,
    LOCK + b'packaging==24.0 --hash=sha256:' + b'd' * 64,
    LOCK.replace(b'packaging==', b'packaging[extra]=='),
    LOCK.replace(b'packaging==25.0', b'packaging==25.0;python_version>"3"'),
])
def test_rejects_ambiguous_or_unbound_dependency_inputs(recipe: Any, lock: bytes) -> None:
    with pytest.raises(recipe.PythonProjectRecipeError): parse(recipe, lock=lock)


@pytest.mark.parametrize('before,after', [
    (b'setuptools.build_meta', b'untrusted_backend'),
    (b'recipe = "python_offline_wheel_v1"', b'recipe = "shell"'),
    (b'import-module = "billing"', b'import-module = "../outside"'),
    (b'test-directory = "tests"', b'test-directory = "../../outside"'),
    (b'packaging==25.0', b'packaging==24.0'),
    (b'requires = [', b'backend-path = ["."]\nrequires = ['),
    (b'version = "1.0.0"', b'dynamic = ["version"]'),
])
def test_requires_explicit_fixed_source_recipe(recipe: Any, before: bytes, after: bytes) -> None:
    with pytest.raises(recipe.PythonProjectRecipeError): parse(recipe, manifest=MANIFEST.replace(before, after))


def test_authoritative_input_budgets_are_not_silently_expanded(recipe: Any) -> None:
    with pytest.raises(recipe.PythonProjectRecipeError):
        recipe.parse_python_project_declaration(MANIFEST, LOCK, max_control_bytes=10, max_requirements=128)
    with pytest.raises(recipe.PythonProjectRecipeError):
        recipe.parse_python_project_declaration(MANIFEST, LOCK, max_control_bytes=256 * 1024, max_requirements=2)


def test_accepts_explicit_hash_locked_build_dependency_closure(recipe: Any) -> None:
    manifest = MANIFEST.replace(b'dependencies = ["packaging==25.0"]', b'dependencies = []')
    manifest = manifest.replace(b'"wheel==0.45.1"]', b'"wheel==0.45.1", "packaging==25.0"]')
    result = parse(recipe, manifest=manifest)
    assert result.runtime_dependencies == ()
    assert result.build_dependencies == ('packaging', 'setuptools', 'wheel')
    assert recipe.PythonProjectDeclaration.from_payload(result.to_payload()) == result
    with pytest.raises(recipe.PythonProjectRecipeError):
        parse(recipe, manifest=manifest, lock=LOCK.split(b'\n', 1)[1])


@pytest.mark.parametrize('manifest,lock,code', [
    (b'not toml', LOCK, 'python_project_recipe_invalid'),
    (b'\xff', LOCK, 'python_project_recipe_invalid'),
    (MANIFEST, b'\xff', 'python_project_lock_invalid'),
    (MANIFEST, b'unpinned', 'python_project_lock_invalid'),
    (b'huge = ' + b'[' * 1200 + b']' * 1200, LOCK, 'python_project_declaration_limit'),
])
def test_parser_reports_the_actual_failed_input(recipe: Any, manifest: bytes, lock: bytes, code: str) -> None:
    with pytest.raises(recipe.PythonProjectRecipeError) as error:
        parse(recipe, manifest=manifest, lock=lock)
    assert error.value.code == code


@pytest.mark.parametrize('extra', [b'dynamic = false\n', b'optional-dependencies = []\n'])
def test_empty_values_do_not_bypass_metadata_types(recipe: Any, extra: bytes) -> None:
    with pytest.raises(recipe.PythonProjectRecipeError):
        parse(recipe, manifest=MANIFEST.replace(b'[project]\n', b'[project]\n' + extra))


def test_constraint_whitespace_cannot_change_version_digits(recipe: Any) -> None:
    with pytest.raises(recipe.PythonProjectRecipeError):
        parse(recipe, manifest=MANIFEST.replace(b'>=3.12,<3.13', b'>=3.1 2,<3.13'))
    result = parse(recipe, manifest=MANIFEST.replace(b'>=3.12,<3.13', b'>= 3.12, < 3.13'))
    assert result.requires_python == '>=3.12,<3.13'
    parse(recipe, manifest=MANIFEST.replace(b'[project]\n',
        b'[project]\ndynamic = []\noptional-dependencies = {}\n'))


@pytest.mark.parametrize('module', ['json', 'typing', 'unittest', 'hashlib', 'importlib'])
def test_recipe_rejects_reserved_runtime_module_names_before_build(recipe: Any, module: str) -> None:
    with pytest.raises(recipe.PythonProjectRecipeError) as error:
        parse(recipe, manifest=MANIFEST.replace(b'import-module = "billing"',
            f'import-module = "{module}"'.encode()))
    assert error.value.code == 'python_project_recipe_unsupported'


def test_private_package_name_is_a_valid_python_identifier(recipe: Any) -> None:
    result = parse(recipe, manifest=MANIFEST.replace(b'import-module = "billing"', b'import-module = "_billing"'))
    assert result.import_module == '_billing'
