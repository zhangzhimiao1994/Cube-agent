"""Python installation always needs its exact recipe grant, not network.read."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.capabilities.defaults import _project_verification_effect
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect


def request(**changes: Any) -> CapabilityRequest:
    return CapabilityRequest.model_validate({'tenant_id': UUID(int=1), 'user_id': UUID(int=3),
        'run_id': UUID(int=2), 'agent_id': 'main_agent', 'capability': 'command', 'operation': 'run',
        'resource': 'workspace/current/project-verification/python/' + 'a' * 64,
        'arguments': {'schema_version': 1, 'recipe_sha256': 'a' * 64,
            'environment_input_sha256': 'b' * 64, 'project_id': 'billing', 'workspace_session_id': 'one'},
        'idempotency_key': 'scope'} | changes, strict=True)


def repository(**changes: Any) -> Any:
    state = {'schema_version': 2, 'phase': 'captured',
        'reference': {'schema_version': 1, 'contract_sha256': 'c' * 64,
            'input_source_sha256': 'd' * 64, 'file_count': 6},
        'python_recipe': {'schema_version': 1, 'recipe_sha256': 'a' * 64,
            'environment_input_sha256': 'b' * 64}}
    route = {'project_id': 'billing', 'workspace_session_id': 'one', 'sandbox_profile': 'workspace_write',
        'execution_backend': 'systemd', 'requested_permissions': ['workspace.read', 'workspace.write', 'command.run', 'network.read'],
        'project_input_contract_version': 1, 'project_input': ManagedProjectInput(('src/billing',)).to_payload(),
        'project_input_preparation': state}
    route.update(changes)
    record = SimpleNamespace(tenant_id=UUID(int=1), id=UUID(int=2), actor_id=UUID(int=3), routing_decision=route)
    async def get(tenant_id: UUID, run_id: UUID) -> Any:
        return record
    return SimpleNamespace(get=get, record=record)


@pytest.mark.asyncio
async def test_network_read_never_preapproves_python_installation() -> None:
    assert await _project_verification_effect(repository(), request()) is PolicyEffect.REQUIRE_APPROVAL


@pytest.mark.parametrize('change', [{'project_id': 'elsewhere'}, {'workspace_session_id': 'elsewhere'},
    {'execution_backend': 'docker'}, {'project_input_contract_version': True},
    {'project_input_preparation': None}, {'project_input_preparation': {'schema_version': 1}},
    {'requested_permissions': ['workspace.read', 'workspace.write']}])
@pytest.mark.asyncio
async def test_recipe_policy_rechecks_persisted_project_and_execution_scope(change: dict[str, Any]) -> None:
    assert await _project_verification_effect(repository(**change), request()) is PolicyEffect.DENY


@pytest.mark.parametrize('field,value', [('schema_version', True), ('recipe_sha256', 'e' * 64),
    ('environment_input_sha256', 'e' * 64), ('extra', 'not allowed')])
@pytest.mark.asyncio
async def test_stale_or_mutated_recipe_marker_is_not_a_grant(field: str, value: object) -> None:
    repo = repository()
    repo.record.routing_decision['project_input_preparation']['python_recipe'][field] = value
    assert await _project_verification_effect(repo, request()) is PolicyEffect.DENY


@pytest.mark.asyncio
async def test_generic_resource_is_not_an_alternative_for_a_captured_python_project() -> None:
    assert await _project_verification_effect(repository(), request(
        resource='workspace/current/project-verification', arguments={})) is PolicyEffect.DENY


@pytest.mark.asyncio
async def test_unchanged_node_policy_preserves_network_approval_behavior() -> None:
    repo = repository(project_input_preparation=None)
    assert await _project_verification_effect(repo, request(
        resource='workspace/current/project-verification', arguments={})) is PolicyEffect.ALLOW
