"""Root staging must bind a service recipe to its preserved original and scope."""

import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.capabilities.project_baselines import StoredProjectBaseline
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectInvocation
from tests.unit.capabilities.test_python_project_inputs import bind
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST


@pytest.fixture
def staging() -> Any:
    name = 'agent_hub.skills.sandbox.python_project_staging'
    assert importlib.util.find_spec(name) is not None, 'Python root recipe staging missing'
    return importlib.import_module(name)


def fixture() -> tuple[Any, PythonProjectInvocation, StoredProjectBaseline, bytes]:
    inputs = importlib.import_module('agent_hub.capabilities.python_project_inputs')
    recipe = bind(inputs)
    invocation = PythonProjectInvocation(execution_id='python_original', tenant_id=recipe.contract.tenant_id,
        run_id=recipe.contract.run_id, project_id=recipe.contract.project_id, session_id=recipe.contract.session_id,
        sandbox_profile='workspace_write', timeout_seconds=30, install_allowed=True,
        original_reference=recipe.original_reference.to_payload(), source_kind='original',
        recipe_sha256=recipe.sha256, environment_input_sha256=recipe.environment_input_sha256)
    # Decoder does not read the tree: byte/scope relations are independent of a path.
    files = {name: hashlib.sha256(b'f\0' + raw).hexdigest() for name, raw in {
        'pyproject.toml': MANIFEST, 'requirements.lock': LOCK,
        'src/billing/__init__.py': b'pass', 'tests/test_billing.py': b'pass'}.items()}
    original = StoredProjectBaseline(recipe.original_reference, recipe.contract, Path('/root/private-original'), files)
    raw = json.dumps(recipe.to_payload(), sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return recipe, invocation, original, raw


def test_native_recipe_decoder_keeps_baseline_and_environment_inputs_distinct(staging: Any) -> None:
    expected, invocation, original, raw = fixture()
    result = staging.decode_python_project_inputs(raw, invocation=invocation, original=original)
    assert result == expected
    assert result.sha256 != result.environment_input_sha256
    assert result.original_reference.contract_sha256 != result.contract.protected_sha256


@pytest.mark.parametrize('change', ['tenant', 'run', 'project', 'session', 'recipe', 'environment', 'original'])
def test_native_recipe_rejects_changed_trusted_invocation_binding(staging: Any, change: str) -> None:
    _expected, invocation, original, raw = fixture()
    updates = {'tenant': {'tenant_id': UUID(int=99)}, 'run': {'run_id': UUID(int=99)},
        'project': {'project_id': 'sibling'}, 'session': {'session_id': 'sibling'},
        'recipe': {'recipe_sha256': 'a' * 64}, 'environment': {'environment_input_sha256': 'a' * 64},
        'original': {'original_reference': invocation.original_reference.model_copy(update={'contract_sha256': 'a' * 64})}}
    invocation = invocation.model_copy(update=updates[change])
    with pytest.raises(ValueError):
        staging.decode_python_project_inputs(raw, invocation=invocation, original=original)


@pytest.mark.parametrize('change', ['noncanonical', 'duplicate', 'weak_version', 'authority', 'node'])
def test_native_recipe_rejects_ambiguous_or_extra_authority_data(staging: Any, change: str) -> None:
    _expected, invocation, original, raw = fixture()
    if change == 'noncanonical':
        raw += b'\n'
    elif change == 'duplicate':
        raw = raw.replace(b'{"contract":', b'{"schema_version":1,"contract":', 1)
    else:
        payload = json.loads(raw)
        if change == 'weak_version':
            payload['schema_version'] = True
        elif change == 'authority':
            payload['install_allowed'] = True
        else:
            payload['kind'] = 'node'
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    with pytest.raises(ValueError):
        staging.decode_python_project_inputs(raw, invocation=invocation, original=original)


@pytest.mark.parametrize('uid', [0, True, -1, '10001'])
def test_root_staging_does_not_touch_any_path_for_invalid_service_uid(staging: Any, tmp_path: Path, uid: object) -> None:
    _expected, invocation, _original, _raw = fixture()
    stage = tmp_path / 'stage'
    stage.mkdir()
    sentinel = stage / 'sentinel'
    sentinel.write_bytes(b'keep')
    with pytest.raises(staging.PythonProjectStageError):
        staging.stage_python_project_inputs(workspace_root=tmp_path / 'absent', invocation=invocation,
            stage=stage, allowed_uid=uid)
    assert list(stage.iterdir()) == [sentinel]
