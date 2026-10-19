from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_hub.api.routers.runs import CreateRunRequest


def test_project_input_survives_public_request_validation() -> None:
    request = CreateRunRequest.model_validate({
        'message': 'Repair the selected project using its original checks.',
        'project_input': {
            'schema_version': 1, 'kind': 'managed_workspace', 'editable_paths': ['src', 'lib'],
        },
    })
    assert request.model_dump(mode='json').get('project_input') == {
        'schema_version': 1, 'kind': 'managed_workspace', 'editable_paths': ['lib', 'src'],
    }


@pytest.mark.parametrize('project_input', [
    {'kind': 'managed_workspace', 'editable_paths': ['..']},
    {'kind': 'managed_workspace', 'editable_paths': ['/tmp/host']},
    {'kind': 'managed_workspace', 'editable_paths': ['src'], 'install_allowed': True},
    {'kind': 'attachment', 'editable_paths': ['src']},
    {'kind': 'managed_workspace', 'editable_paths': ['package.json']},
    {'kind': 'managed_workspace', 'editable_paths': []},
    {'schema_version': True, 'kind': 'managed_workspace', 'editable_paths': ['src']},
])
def test_invalid_project_input_is_rejected_not_silently_ignored(project_input: object) -> None:
    with pytest.raises(ValidationError):
        CreateRunRequest.model_validate({'message': 'Repair project.', 'project_input': project_input})


def test_absent_project_input_does_not_implicitly_select_current_workspace() -> None:
    request = CreateRunRequest(message='Explain this code.', project_id='project-a')
    assert request.model_dump(mode='json').get('project_input') is None
