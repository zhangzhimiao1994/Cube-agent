"""Initial draft guidance exposes the strict recipe before a failed verify."""

from uuid import UUID

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.generated_project_guidance import generated_project_guidance

MODES = (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.HYBRID, TaskMode.DISCUSS)


def context(mode: TaskMode) -> TaskContext:
    return TaskContext(
        run_id=UUID('00000000-0000-4000-8000-000000000011'),
        tenant_id=UUID('00000000-0000-4000-8000-000000000012'),
        mode=mode, request='Build an expense CLI with real tests.',
        routing_decision={
            'generated_project_contract_version': 1,
            'project_delivery': 'workspace',
            'python_dependency_preflight_version': 1,
        },
        token_budget=10_000, timeout_seconds=120,
    )


@pytest.mark.parametrize('mode', MODES)
def test_initial_guidance_states_both_exact_supported_python_ranges(mode: TaskMode) -> None:
    text = generated_project_guidance(context(mode))

    assert 'requires-python' in text
    assert '>=3.12,<3.13' in text
    assert '==3.12.*' in text
    assert 'Python 3.12' in text


@pytest.mark.parametrize('mode', MODES)
def test_initial_guidance_requires_manifest_and_build_pins_to_match_lock(mode: TaskMode) -> None:
    text = generated_project_guidance(context(mode))

    assert 'project.dependencies' in text
    assert 'build-system.requires' in text
    assert 'same name/version' in text
    assert 'requirements.lock' in text


@pytest.mark.parametrize('mode', MODES)
def test_initial_guidance_locates_verified_hashes_without_granting_authority(mode: TaskMode) -> None:
    ctx = context(mode)
    before = ctx.to_payload()
    text = generated_project_guidance(ctx)

    assert 'same-scope verified dependency pin feedback' in text
    assert 'dependency_pins' not in text
    assert 'project.inspect' in text and 'python_build_profile' in text
    assert 'name==version --hash=sha256:' in text
    assert 'matching name/version' in text
    assert 'never invent' in text
    assert 'configured_not_probed is not an installation receipt' in text
    assert 'UNKNOWN operation is inspect-only' in text
    assert 'do not retry a download or extend the original budget/deadline' in text
    assert ctx.to_payload() == before


@pytest.mark.parametrize('mode', MODES)
def test_unselected_project_contract_does_not_gain_recipe_instructions(mode: TaskMode) -> None:
    ctx = context(mode).model_copy(update={'routing_decision': {}})

    assert generated_project_guidance(ctx) == ''
