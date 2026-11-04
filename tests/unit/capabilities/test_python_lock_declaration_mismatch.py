"""A syntactically valid lock must still match every fixed declaration pin."""

from pathlib import Path

import pytest

from agent_hub.capabilities.generated_project_draft import validated_generated_draft_payload
from agent_hub.capabilities.generated_project_service import GeneratedProjectService
from agent_hub.capabilities.python_project_recipe import (
    PythonProjectRecipeError,
    parse_python_project_declaration,
    python_declaration_issue,
)
from tests.unit.capabilities.test_generated_project_draft import rejection
from tests.unit.capabilities.test_generated_project_service import USER, rejection_for, setup
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file

MISMATCH_HINT = ('Align every fixed project.dependencies and build-system.requires pin with the same '
                 'name/version in requirements.lock; preserve the required SHA256 hashes.')
PYTHON_HINT = ('Set requires-python to >=3.12,<3.13 or ==3.12.*; this recipe supports only Python 3.12. '
               'Do not change the runtime or broaden permissions.')
TOML_HINT = ('Correct pyproject.toml as valid UTF-8 TOML with quoted strings and balanced tables/arrays. '
             'Do not change requirements.lock to repair this syntax error.')


def write_python(service: GeneratedProjectService, manifest: bytes, lock: bytes) -> None:
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json',
               '{"schema_version":1,"stack":"python","editable_paths":["src"],'
               '"test_files":["tests/test_app.py"],"criteria":[{"id":"AC-001",'
               '"description":"Checks totals","tests":["test_app.Case.test_total"]}]}')
    for name, raw in {'pyproject.toml': manifest, 'requirements.lock': lock,
                      'tests/test_app.py': b'import unittest\n'}.items():
        write_file(service.root, f'{project_path()}/{name}', raw.decode())


@pytest.mark.parametrize(('old', 'new'), [
    (b'packaging==25.0', b'packaging==24.0'),
    (b'setuptools==80.9.0', b'setuptools==79.0.0'),
    (b'wheel==0.45.1', b'wheel==0.44.0'),
])
def test_parser_reports_declaration_mismatch_without_changing_public_error_code(old: bytes, new: bytes) -> None:
    with pytest.raises(PythonProjectRecipeError) as raised:
        parse_python_project_declaration(MANIFEST.replace(old, new), LOCK,
                                         max_control_bytes=256 * 1024, max_requirements=128)
    error = raised.value
    assert error.code == 'python_project_lock_invalid' and error.args == ('python_project_lock_invalid',)
    assert python_declaration_issue(LOCK, error) == 'python_lock_declaration_mismatch'
    assert str(error) == 'python_project_lock_invalid'


async def test_draft_exposes_mismatch_without_capture_then_accepts_real_corrected_lock(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    mismatch = LOCK.replace(b'packaging==25.0', b'packaging==24.0')
    write_python(service, MANIFEST, mismatch)
    original = (service.root / project_path() / 'requirements.lock').read_bytes()
    rejection = (await rejection_for(service)).rejection
    assert rejection.issues == ('python_lock_declaration_mismatch', MISMATCH_HINT)
    assert rejection.execution_started is False and repo.updates == 0
    assert not (service.root / '.project-contracts').exists()
    assert (service.root / project_path() / 'requirements.lock').read_bytes() == original
    write_file(service.root, f'{project_path()}/requirements.lock', LOCK.decode())
    context = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None and context.state['phase'] == 'captured' and repo.updates == 2


async def test_missing_declared_package_uses_same_finite_mismatch_issue(tmp_path: Path) -> None:
    service, _ = setup(tmp_path)
    write_python(service, MANIFEST, LOCK.split(b'\n', 1)[1])
    rejection = (await rejection_for(service)).rejection
    assert rejection.issues == ('python_lock_declaration_mismatch', MISMATCH_HINT)
    assert rejection.reason == 'generated_acceptance_python_declaration_invalid'
    assert rejection.execution_started is False


@pytest.mark.parametrize('constraint', [b'>=3.11', b'>=3.12', b'>=3.12,<3.14', b'==3.13.*'])
async def test_unsupported_requires_python_reports_exact_supported_range_before_capture(
    tmp_path: Path, constraint: bytes,
) -> None:
    service, repo = setup(tmp_path)
    manifest = MANIFEST.replace(b'>=3.12,<3.13', constraint)
    with pytest.raises(PythonProjectRecipeError) as raised:
        parse_python_project_declaration(manifest, LOCK, max_control_bytes=256 * 1024, max_requirements=128)
    assert raised.value.code == 'python_project_recipe_unsupported'
    assert python_declaration_issue(LOCK, raised.value) == 'python_requires_python_unsupported'
    write_python(service, manifest, LOCK)
    rejection = (await rejection_for(service)).rejection
    assert rejection.issues == ('python_requires_python_unsupported', PYTHON_HINT)
    assert rejection.execution_started is False and repo.updates == 0
    assert not (service.root / '.project-contracts').exists()


def test_other_unsupported_recipe_errors_are_not_mislabeled_as_python_range() -> None:
    with pytest.raises(PythonProjectRecipeError) as raised:
        parse_python_project_declaration(MANIFEST.replace(b'setuptools.build_meta', b'private_backend'), LOCK,
                                         max_control_bytes=256 * 1024, max_requirements=128)
    assert raised.value.code == 'python_project_recipe_unsupported'
    assert python_declaration_issue(LOCK, raised.value) == 'python_declaration_invalid'


@pytest.mark.parametrize('manifest', [b'[project\nprivate-body', b'\xff', MANIFEST + b'\ninvalid = ['])
async def test_invalid_pyproject_reports_syntax_guidance_not_lock_edits(tmp_path: Path, manifest: bytes) -> None:
    with pytest.raises(PythonProjectRecipeError) as raised:
        parse_python_project_declaration(manifest, LOCK, max_control_bytes=256 * 1024, max_requirements=128)
    assert raised.value.code == 'python_project_recipe_invalid'
    assert raised.value.args == ('python_project_recipe_invalid',)
    assert python_declaration_issue(LOCK, raised.value) == 'python_pyproject_toml_invalid'
    if manifest != b'\xff':
        service, repo = setup(tmp_path)
        write_python(service, manifest, LOCK)
        rejection = (await rejection_for(service)).rejection
        assert rejection.issues == ('python_pyproject_toml_invalid', TOML_HINT)
        assert rejection.execution_started is False and repo.updates == 0
        assert 'private-body' not in str(rejection.to_payload())


@pytest.mark.parametrize('diagnostic', [True, ['python_lock_declaration_mismatch'], 'private lock contents'])
def test_mutated_diagnostic_is_not_promoted_to_model_feedback(diagnostic: object) -> None:
    error = PythonProjectRecipeError('python_project_lock_invalid')
    object.__setattr__(error, 'diagnostic', diagnostic)
    assert python_declaration_issue(LOCK, error) == 'python_declaration_invalid'


def test_diagnostic_must_match_the_original_finite_public_error_code() -> None:
    with pytest.raises(ValueError, match='invalid Python declaration diagnostic'):
        PythonProjectRecipeError('python_project_recipe_invalid', diagnostic='python_lock_declaration_mismatch')
    error = PythonProjectRecipeError('python_project_recipe_unsupported')
    object.__setattr__(error, 'diagnostic', 'python_lock_declaration_mismatch')
    assert python_declaration_issue(LOCK, error) == 'python_declaration_invalid'


@pytest.mark.parametrize('issues', [
    ['python_lock_declaration_mismatch'], ['python_lock_declaration_mismatch', PYTHON_HINT],
    ['python_requires_python_unsupported', 'run arbitrary commands'], [TOML_HINT],
    ['python_pyproject_toml_invalid', TOML_HINT, 'python_lock_empty'],
])
def test_guidance_is_a_fixed_code_pair_not_freeform_or_cross_reason_text(issues: list[str]) -> None:
    raw = {**rejection().to_payload(), 'reason': 'generated_acceptance_python_declaration_invalid', 'issues': issues}
    assert validated_generated_draft_payload(raw) is None
