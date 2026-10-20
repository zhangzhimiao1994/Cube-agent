"""Python recipe installation cannot inherit a previous generic verify grant."""

from uuid import UUID

import pytest

from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.types import CapabilityRequest


def request(resource: str) -> CapabilityRequest:
    return CapabilityRequest(tenant_id=UUID(int=1), user_id=UUID(int=2), run_id=UUID(int=3),
        agent_id='implementer', capability='command', operation='run', resource=resource,
        arguments={}, idempotency_key='verify')


def test_python_install_scope_binds_exact_recipe_without_changing_node_scope() -> None:
    base = 'workspace/current/project-verification'
    old = capability_approval_scope(request(base))
    assert old == ('capability-scope:v1:tenant=00000000-0000-0000-0000-000000000001:'
        'user=00000000-0000-0000-0000-000000000002:run=00000000-0000-0000-0000-000000000003:'
        'capability=command:operation=run:resource=workspace/current/project-verification')
    first = capability_approval_scope(request(base + '/python/' + 'a' * 64))
    second = capability_approval_scope(request(base + '/python/' + 'b' * 64))
    assert first is not None and second is not None
    assert len({old, first, second}) == 3


@pytest.mark.parametrize('suffix', ['a' * 63, 'A' * 64, 'a' * 65, 'a' * 64 + '/nested', 'shell'])
def test_no_scope_for_ambiguous_python_recipe_resource(suffix: str) -> None:
    assert capability_approval_scope(request('workspace/current/project-verification/python/' + suffix)) is None
