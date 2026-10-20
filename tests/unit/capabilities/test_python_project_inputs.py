"""Recipe identity binds original checks; environment input identity is stable."""

import hashlib
import importlib
import importlib.util
import json
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.project_baselines import ProjectBaselineReference
from agent_hub.capabilities.project_contracts import build_project_check_contract
from agent_hub.capabilities.python_project_recipe import parse_python_project_declaration
from agent_hub.capabilities.types import CapabilityRequest
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST


@pytest.fixture
def inputs() -> Any:
    name = 'agent_hub.capabilities.python_project_inputs'
    assert importlib.util.find_spec(name) is not None, 'Python project input binding missing'
    return importlib.import_module(name)


def runtime(inputs: Any, **changes: object) -> Any:
    values: dict[str, object] = {'version': '3.12.10', 'implementation': 'cpython', 'abi': 'cp312',
        'platform': 'linux_x86_64', 'binary_sha256': '1' * 64, 'base_runtime_sha256': '2' * 64}
    return inputs.PythonInterpreterIdentity(**(values | changes))


def bind(inputs: Any, *, manifest: bytes = MANIFEST, lock: bytes = LOCK,
         tenant: UUID | None = None, run: UUID | None = None, **changes: object) -> Any:
    wheels = importlib.import_module('agent_hub.capabilities.python_project_wheels')
    declaration = parse_python_project_declaration(manifest, lock,
        max_control_bytes=256 * 1024, max_requirements=128)
    files = {path: hashlib.sha256(b'f\0' + content).hexdigest() for path, content in {
        'pyproject.toml': manifest, 'requirements.lock': lock,
        'src/billing/__init__.py': b'pass', 'tests/test_billing.py': b'pass'}.items()}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    contract = build_project_check_contract(tenant or UUID(int=1), run or UUID(int=2),
        'billing', 'session-one', digest, ('src/billing',), files)
    reference = ProjectBaselineReference('3' * 64, digest, 4)
    selected = tuple(wheels.PythonWheelBinding(
        filename=f'{item.name}-{item.version}-py3-none-any.whl', name=item.name,
        version=item.version, sha256=item.hashes[0]) for item in declaration.requirements)
    values: dict[str, object] = {'contract': contract, 'original_reference': reference,
        'declaration': declaration, 'interpreter': runtime(inputs), 'wheels': selected,
        'manifest': manifest, 'lock': lock}
    return inputs.bind_python_project_inputs(**(values | changes))


def test_recipe_is_scoped_but_environment_inputs_are_not_issuance_identity(inputs: Any) -> None:
    first = bind(inputs)
    second = bind(inputs, run=uuid4(), tenant=uuid4())
    assert first.sha256 != second.sha256
    assert first.environment_input_sha256 == second.environment_input_sha256
    assert first.environment_input_sha256 != first.sha256
    assert first.to_payload()['kind'] == 'python_offline_wheel_v1'
    assert inputs.PythonProjectInputs.from_payload(first.to_payload()) == first


def test_raw_control_changes_do_not_reset_identical_environment_inputs(inputs: Any) -> None:
    first = bind(inputs)
    reformatted = bind(inputs, lock=LOCK.replace(b'\n', b'\r\n'), manifest=MANIFEST + b'\n')
    assert first.sha256 != reformatted.sha256
    assert first.environment_input_sha256 == reformatted.environment_input_sha256
    changed = bind(inputs, interpreter=runtime(inputs, binary_sha256='a' * 64))
    assert first.environment_input_sha256 != changed.environment_input_sha256


@pytest.mark.parametrize('changes', [
    {'implementation': 'pypy'}, {'abi': 'cp311'}, {'version': '3.11.9'},
    {'version': True}, {'platform': '../../host'}, {'binary_sha256': 'unmeasured'},
    {'base_runtime_sha256': ''},
])
def test_runtime_identity_is_measured_data_not_a_model_selected_executable(inputs: Any, changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        runtime(inputs, **changes)


def test_changed_original_control_bytes_cannot_be_bound(inputs: Any) -> None:
    first = bind(inputs)
    with pytest.raises(ValueError):
        bind(inputs, manifest=MANIFEST + b'\n', contract=first.contract)
    with pytest.raises(ValueError):
        bind(inputs, original_reference=replace(first.original_reference, input_source_sha256='a' * 64))


@pytest.mark.parametrize('field,value', [
    ('schema_version', True), ('kind', 'node'), ('unexpected', 'ignored'),
    ('environment_input_sha256', 'a' * 64), ('recipe_sha256', 'a' * 64),
])
def test_persisted_inputs_reject_schema_downgrade_and_unbound_fields(inputs: Any, field: str, value: object) -> None:
    payload = bind(inputs).to_payload() | {field: value}
    with pytest.raises(ValueError):
        inputs.PythonProjectInputs.from_payload(payload)


def test_install_approval_is_recipe_specific_and_never_old_generic_scope(inputs: Any) -> None:
    first = bind(inputs)
    request = first.approval_request(user_id=UUID(int=3), agent_id='implementer', idempotency_key='install')
    assert request.resource == f'workspace/current/project-verification/python/{first.sha256}'
    assert request.arguments['recipe_sha256'] == first.sha256
    assert request.arguments['environment_input_sha256'] == first.environment_input_sha256
    scope = capability_approval_scope(request)
    assert scope is not None
    legacy = CapabilityRequest(**(request.model_dump() | {'resource': 'workspace/current/project-verification'}))
    assert scope != capability_approval_scope(legacy)
    changed = bind(inputs, interpreter=runtime(inputs, binary_sha256='a' * 64))
    next_request = changed.approval_request(user_id=UUID(int=3), agent_id='implementer', idempotency_key='install')
    assert scope != capability_approval_scope(next_request)
    assert first.matches_approval_request(request)
    assert not first.matches_approval_request(legacy)
    assert not first.matches_approval_request(next_request)
    altered = request.model_copy(update={'arguments': request.arguments | {'project_id': 'sibling'}})
    assert not first.matches_approval_request(altered)
