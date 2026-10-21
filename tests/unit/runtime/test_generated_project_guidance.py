from types import SimpleNamespace
from typing import cast

import pytest

from agent_hub.runtime.contracts import TaskContext


def test_generated_engineering_guidance_defines_executable_acceptance_without_grants() -> None:
    from agent_hub.runtime.generated_project_guidance import generated_project_guidance
    context = SimpleNamespace(routing_decision={'generated_project_contract_version': 1,
                                               'project_delivery': 'workspace'})
    text = generated_project_guidance(cast(TaskContext, context))
    assert all(word in text for word in ('ACCEPTANCE.json', 'node:test', 'unittest', 'project.inspect',
                                        'project.verify', 'workspace.bundle', 'requirements.lock'))
    assert 'never invent' in text and 'approval' in text and 'frozen' in text


@pytest.mark.parametrize('route', [{}, {'generated_project_contract_version': 1},
    {'generated_project_contract_version': True, 'project_delivery': 'workspace'},
    {'project_input_contract_version': 1, 'project_delivery': 'workspace'}])
def test_chat_and_original_projects_do_not_receive_generated_instructions(route: dict[str, object]) -> None:
    from agent_hub.runtime.generated_project_guidance import generated_project_guidance
    assert generated_project_guidance(cast(TaskContext, SimpleNamespace(routing_decision=route))) == ''
