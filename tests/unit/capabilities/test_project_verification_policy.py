from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
)
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.types import CapabilityRequest
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, stored_run

USER = UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc')


def request() -> CapabilityRequest:
    return CapabilityRequest(tenant_id=TENANT, user_id=USER, agent_id='engineer',
        capability='command', operation='run', resource='workspace/current/project-verification',
        arguments={}, run_id=RUN, idempotency_key='verify-1')


class Repository:
    def __init__(self, *, network: bool, approved: bool = False) -> None:
        self.record = replace(stored_run(requested_permissions=[
            'workspace.read', 'workspace.write', 'command.run', *(['network.read'] if network else []),
        ]), actor_id=USER)
        self.approved = approved
        self.waits: list[Mapping[str, object]] = []

    async def get(self, tenant_id: UUID, run_id: UUID) -> object:
        return self.record

    async def is_capability_request_approved(self, *args: object) -> bool:
        return False

    async def is_capability_approval_scope_approved(self, *args: object) -> bool:
        return self.approved

    async def begin_capability_approval(self, *args: object, **kwargs: object) -> None:
        self.waits.append(kwargs)


@pytest.mark.parametrize('network,approved,expected', [
    (False, False, CapabilityStatus.WAITING_APPROVAL),
    (True, False, CapabilityStatus.ALLOWED), (False, True, CapabilityStatus.ALLOWED),
])
async def test_dependency_fetch_needs_run_network_grant_or_project_scope_approval(
    network: bool, approved: bool, expected: CapabilityStatus,
) -> None:
    repo = Repository(network=network, approved=approved)
    service = ApprovalService(InMemoryApprovalStore())
    gateway = DefaultRuntimeCapabilityPolicyGateway(service, repo)
    result = await gateway.invoke(request(), role=Role.OPERATOR)
    assert result.status is expected
    assert bool(repo.waits) is (expected is CapabilityStatus.WAITING_APPROVAL)


def test_project_approval_scope_is_run_user_bound_and_not_generic_write() -> None:
    scope = capability_approval_scope(request())
    assert scope is not None
    assert str(RUN) in scope and str(USER) in scope and 'project-verification' in scope
    assert scope != capability_approval_scope(request().model_copy(update={
        'capability': 'file', 'operation': 'write', 'resource': 'workspace/current',
    }))
