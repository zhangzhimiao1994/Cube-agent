from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.defaults import (
    _project_input_supply_effect,
    _project_verification_effect,
)
from agent_hub.capabilities.types import PolicyEffect
from agent_hub.harness.tool_gateway import _project_verification_authorization
from agent_hub.runs.repository import (
    _python_input_supply_approval_binding,
    _python_project_approval_binding,
)
from tests.unit.capabilities.test_python_generated_service import configured, request


@pytest.fixture
def api() -> Any:
    from agent_hub.capabilities import python_generated_service
    return python_generated_service


async def test_generated_python_recipe_requires_exact_approval(api: Any, tmp_path: Path) -> None:
    service, _, repo, _, _, context = await configured(api, tmp_path)
    selected = await service.authorization_request(request(), context)
    assert _project_verification_authorization(selected, expected=request()) == selected
    assert await _project_verification_effect(repo, selected) is PolicyEffect.REQUIRE_APPROVAL
    assert await _project_verification_effect(repo, request()) is PolicyEffect.DENY
    assert _python_project_approval_binding(repo.row, capability_approval_scope(selected)) == selected.arguments
    wrong = selected.model_copy(update={'arguments': {**selected.arguments, 'project_id': 'other'}})
    assert await _project_verification_effect(repo, wrong) is PolicyEffect.DENY


async def test_generated_supply_is_separate_from_recipe_approval(api: Any, tmp_path: Path) -> None:
    service, _, repo, executor, _, context = await configured(api, tmp_path)
    executor.probe_error = 'python_project_wheel_missing'
    selected = await service.authorization_request(request(), context)
    assert _project_verification_authorization(selected, expected=request()) == selected
    assert await _project_input_supply_effect(repo, selected, role=Role.OPERATOR) is PolicyEffect.REQUIRE_APPROVAL
    assert _python_input_supply_approval_binding(repo.row, capability_approval_scope(selected)) == selected.arguments
    assert await _project_verification_effect(repo, request()) is PolicyEffect.DENY
    wrong = selected.model_copy(update={'arguments': {**selected.arguments, 'plan_sha256': 'f' * 64}})
    assert await _project_input_supply_effect(repo, wrong, role=Role.OPERATOR) is PolicyEffect.DENY
    with pytest.raises(ValueError):
        _project_verification_authorization(wrong, expected=request())
    with pytest.raises(ValueError):
        _project_verification_authorization(selected.model_copy(update={'agent_id': 'other'}), expected=request())
