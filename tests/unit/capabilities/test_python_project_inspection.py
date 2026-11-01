"""Explicit Python recipe observations remain pure data, never install authority."""

import hashlib
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.unit.capabilities.test_project_inspection import backend, inspect, observations
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST
from tests.unit.capabilities.test_scoped_read import project_path, write_file


def write_inputs(tmp_path: Path, *, lock: bytes | None = LOCK, manifest: bytes = MANIFEST) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/pyproject.toml', manifest.decode())
    if lock is not None:
        target = write_file(tmp_path / 'projects', f'{project_path()}/requirements.lock', '')
        target.write_bytes(lock)


@pytest.mark.asyncio
async def test_explicit_recipe_reports_build_and_test_declarations_without_probe(tmp_path: Path) -> None:
    write_inputs(tmp_path)
    result = await inspect(backend(tmp_path))
    assert result['operations'] == {'build':'declared', 'test':'declared'}
    assert result['python_recipe'] == {'kind':'python_offline_wheel_v1', 'status':'declared'}
    environment = result['environment']
    assert isinstance(environment, Mapping)
    assert environment['readiness'] == 'not_probed'
    assert result['install_plan_state'] == 'requires_environment_probe'
    assert observations(result)['requirements.lock']['sha256'] == hashlib.sha256(LOCK).hexdigest()


@pytest.mark.parametrize('lock', [None, b'packaging>=25', b'--index-url https://SECRET.invalid\n' + LOCK])
@pytest.mark.asyncio
async def test_invalid_or_missing_lock_cannot_advertise_supported_recipe(
    tmp_path: Path, lock: bytes | None,
) -> None:
    write_inputs(tmp_path, lock=lock)
    result = await inspect(backend(tmp_path))
    assert result['install_plan_state'] == 'blocked'
    assert result['operations'] == {'build':'unknown', 'test':'unknown'}
    recipe = result['python_recipe']
    assert isinstance(recipe, Mapping)
    assert recipe['status'] == 'unavailable'
    assert 'SECRET' not in repr(result)


@pytest.mark.asyncio
async def test_unknown_recipe_never_falls_back_to_supported_python(tmp_path: Path) -> None:
    write_inputs(tmp_path, manifest=MANIFEST.replace(b'python_offline_wheel_v1', b'shell'))
    result = await inspect(backend(tmp_path))
    assert result['install_plan_state'] == 'blocked'
    recipe = result['python_recipe']
    assert isinstance(recipe, Mapping)
    assert recipe['status'] == 'unavailable'


@pytest.mark.asyncio
async def test_non_opted_in_node_inspection_keeps_legacy_shape(tmp_path: Path) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/package.json', '{}')
    result = await inspect(backend(tmp_path))
    assert 'python_recipe' not in result
    assert 'requirements.lock' not in observations(result)
