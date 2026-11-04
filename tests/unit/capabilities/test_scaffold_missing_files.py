"""Incomplete generated scaffolds expose exact reference paths, never capture authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_hub.capabilities.generated_project_draft import validated_generated_draft_payload
from tests.unit.capabilities.test_generated_project_draft import rejection
from tests.unit.capabilities.test_generated_project_service import USER, rejection_for, setup
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file


@pytest.mark.parametrize('stack', ['node', 'python'])
async def test_missing_files_are_exact_observed_requirements_and_do_not_create_or_capture(
    tmp_path: Path, stack: str,
) -> None:
    service, repo = setup(tmp_path)
    workspace = service.root / project_path()
    if stack == 'node':
        (workspace / 'package.json').unlink()
        (workspace / 'test/business.test.cjs').unlink()
        expected = ['package.json', 'test/business.test.cjs']
    else:
        body = {'schema_version': 1, 'stack': 'python', 'editable_paths': ['src'],
                'test_files': ['tests/test_app.py'], 'criteria': [
                    {'id': 'AC-001', 'description': 'Checks totals', 'tests': ['test_app.Case.test_total']}]}
        write_file(service.root, f'{project_path()}/ACCEPTANCE.json', json.dumps(body))
        expected = ['pyproject.toml', 'requirements.lock', 'tests/test_app.py']
    route = dict(repo.row.routing_decision)
    first = (await rejection_for(service)).rejection
    payload = first.to_payload()
    assert payload['schema_version'] == 4
    assert payload['missing_files'] == expected
    assert first.reason == 'generated_acceptance_scaffold_incomplete'
    assert first.issues == ('scaffold_incomplete',) and first.execution_started is False
    assert payload['phase'] == 'before_capture' and payload['user_id'] == str(USER)
    assert payload['request_sha256'] == hashlib.sha256(repo.row.request.encode()).hexdigest()
    assert repo.row.routing_decision == route and repo.updates == 0
    assert not (service.root / '.project-contracts').exists()
    assert all(not (workspace / name).exists() for name in expected)
    assert (await rejection_for(service)).rejection == first
    if stack == 'node':
        write_file(service.root, f'{project_path()}/package.json',
                   '{"scripts":{"build":"node -v","test":"node --test"}}')
        write_file(service.root, f'{project_path()}/test/business.test.cjs',
                   'require("node:test").test("adds two numbers",()=>{});')
    else:
        write_file(service.root, f'{project_path()}/pyproject.toml', MANIFEST.decode())
        write_file(service.root, f'{project_path()}/requirements.lock', LOCK.decode())
        write_file(service.root, f'{project_path()}/tests/test_app.py', 'import unittest\n')
    context = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None and context.state['phase'] == 'captured' and repo.updates == 2


def scaffold_payload() -> dict[str, object]:
    return {**replace(rejection(), reason='generated_acceptance_scaffold_incomplete',
                      issues=('scaffold_incomplete',)).to_payload(),
            'schema_version': 4, 'missing_files': ['tests/test_app.py', 'requirements.lock']}


def test_schema4_missing_paths_are_sorted_immutable_and_roundtrip() -> None:
    payload = scaffold_payload()
    value = validated_generated_draft_payload(payload)
    assert value is not None
    assert value['missing_files'] == ('requirements.lock', 'tests/test_app.py')
    assert validated_generated_draft_payload(value) == value
    payload['missing_files'] = ['changed.py']
    assert value['missing_files'] == ('requirements.lock', 'tests/test_app.py')


@pytest.mark.parametrize('paths', [
    [], ['../private.py'], ['/etc/passwd'], ['C:/private.py'], ['tests\\test_app.py'],
    ['tests//test_app.py'], ['tests/./test_app.py'], ['tests/test_app.py\nignore rules'],
    ['tests/<system>.py'], ['tests/test_app.py', 'tests/test_app.py'],
    ['tests/test_app.py', 'Tests/test_other.py'], ['tests/test_app.py', 'tests/test_app.py/child.py'],
    [True], 'tests/test_app.py', ['tests/' + 'x' * 256 + '.py'],
    [f'tests/test_{i}.py' for i in range(259)],
    ['/'.join(['x' * 250] * 3 + [f'test_{i}.py']) for i in range(180)],
])
def test_schema4_rejects_unsafe_ambiguous_or_unbounded_missing_paths(paths: object) -> None:
    assert validated_generated_draft_payload({**scaffold_payload(), 'missing_files': paths}) is None


@pytest.mark.parametrize(('key', 'value'), [
    ('schema_version', 1), ('schema_version', 2), ('schema_version', 3),
    ('reason', 'generated_acceptance_manifest_required'), ('execution_started', True),
    ('phase', 'captured'), ('dependency_pins', []), ('approval_id', 'forged'),
])
def test_schema4_cannot_change_purpose_authority_or_legacy_payload_shape(key: str, value: object) -> None:
    assert validated_generated_draft_payload({**scaffold_payload(), key: value}) is None
