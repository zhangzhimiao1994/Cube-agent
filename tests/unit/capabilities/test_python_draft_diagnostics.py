"""A model must learn which control failed before changing unrelated project files."""
import json
from pathlib import Path

import pytest

from agent_hub.capabilities.generated_project_service import GeneratedProjectDraftRejected
from tests.unit.capabilities.test_generated_project_service import USER, setup
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file


@pytest.mark.parametrize(('lock', 'issue'), [
    (b'', 'python_lock_empty'),
    (b'packaging==25.0\n', 'python_lock_hashes_required'),
    (b'packaging>=25.0\n', 'python_lock_fixed_versions_required'),
    (b'--index-url https://untrusted.invalid\n', 'python_lock_external_source_forbidden'),
])
async def test_draft_reports_actionable_lock_fault_before_capture_and_accepts_corrected_controls(
    tmp_path: Path, lock: bytes, issue: str,
) -> None:
    service, repo = setup(tmp_path)
    acceptance = {'schema_version': 1, 'stack': 'python', 'editable_paths': ['src'],
        'test_files': ['tests/test_app.py'], 'criteria': [
            {'id': 'AC-001', 'description': 'Checks totals', 'tests': ['test_app.Case.test_total']}]}
    for name, body in {'ACCEPTANCE.json': json.dumps(acceptance),
                       'pyproject.toml': MANIFEST.decode(), 'requirements.lock': lock.decode(),
                       'src/billing/__init__.py': 'def total(): return 12\n',
                       'tests/test_app.py': 'import unittest\n'}.items():
        write_file(service.root, f'{project_path()}/{name}', body)
    original_lock = (service.root / project_path() / 'requirements.lock').read_bytes()
    with pytest.raises(GeneratedProjectDraftRejected) as raised:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    rejection = raised.value.rejection
    assert rejection.reason == 'generated_acceptance_python_declaration_invalid'
    assert rejection.issues == (issue,)
    assert rejection.execution_started is False and repo.updates == 0
    assert not (service.root / '.project-contracts').exists()
    assert (service.root / project_path() / 'requirements.lock').read_bytes() == original_lock
    write_file(service.root, f'{project_path()}/requirements.lock', LOCK.decode())
    captured = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert captured is not None and captured.state['phase'] == 'captured'
    assert repo.updates == 2


def test_unrecognized_or_mutated_errors_cannot_become_specific_repair_data() -> None:
    from agent_hub.capabilities.python_project_recipe import (
        PythonProjectRecipeError,
        python_declaration_issue,
    )

    malformed = PythonProjectRecipeError('python_project_lock_invalid')
    malformed.code = []  # type: ignore[assignment]
    unknown = ValueError('sensitive diagnostic content')
    for error in (malformed, unknown):
        assert python_declaration_issue(b'', error) == 'python_declaration_invalid'


@pytest.mark.parametrize(('field', 'value'), [
    ('filename', '../click-8.1.8-py3-none-any.whl'), ('sha256', 'not-a-digest'),
    ('size_bytes', True), ('size_bytes', 0), ('claim', {'permission': 'allow'}),
])
def test_repair_pin_facts_reject_paths_unknown_fields_and_invalid_download_metadata(
    field: str, value: object,
) -> None:
    from dataclasses import replace

    from agent_hub.capabilities.generated_project_draft import validated_generated_draft_payload
    from tests.unit.capabilities.test_generated_project_draft import rejection

    item = replace(rejection(), schema_version=3,
        reason='generated_acceptance_python_declaration_invalid', issues=('python_lock_hashes_required',),
        dependency_pins=({'name': 'click', 'version': '8.1.8',
                          'filename': 'click-8.1.8-py3-none-any.whl', 'sha256': 'd' * 64,
                          'size_bytes': 100},))
    payload = item.to_payload()
    pins = payload['dependency_pins']
    assert isinstance(pins, list) and isinstance(pins[0], dict)
    pins[0][field] = value
    assert validated_generated_draft_payload(payload) is None
