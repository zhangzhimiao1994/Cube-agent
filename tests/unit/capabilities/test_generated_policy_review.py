"""DB authority must override broad policy grants on generated execution paths."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.types import PolicyEffect
from tests.unit.capabilities.test_generated_project_service import Repository
from tests.unit.capabilities.test_python_generated_service import request


@pytest.mark.parametrize('role', [Role.ADMIN, Role.SUPER_ADMIN])
async def test_generated_generic_grant_cannot_use_a_different_db_actor_role(role: Role) -> None:
    repo = Repository()
    repo.row.routing_decision['requested_permissions'].append('network.read')
    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repo)
    decision = await gateway.invoke(request(), role=role)
    assert decision.status is CapabilityStatus.DENIED


@pytest.mark.parametrize('resource', [
    'workspace/./current/project-inputs/python/' + 'a' * 64,
    'workspace/current/unused/../project-inputs/python/' + 'a' * 64,
    'workspace\\current\\project-inputs\\python\\' + 'a' * 64,
])
async def test_supply_resource_alias_cannot_fall_through_to_broad_plugin_allow(resource: str) -> None:
    repo = Repository()

    class Plugins:
        def capability_policy_rules(self, tenant: Any) -> tuple[CapabilityRule, ...]:
            return (CapabilityRule(tenant_id=tenant, role=Role.OPERATOR, agent_id=None,
                capability='command', operation='run', resource_prefix='workspace/current', effect=PolicyEffect.ALLOW),)

    gateway = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repo,
                                                    plugin_policy_rules=Plugins())
    decision = await gateway.invoke(request().model_copy(update={'resource': resource}), role=Role.OPERATOR)
    assert decision.status is CapabilityStatus.DENIED
