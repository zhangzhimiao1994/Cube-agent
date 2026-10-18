from __future__ import annotations

from uuid import uuid4

import pytest

from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
)
from agent_hub.capabilities.types import CapabilityRequest


def request() -> CapabilityRequest:
    return CapabilityRequest(
        tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4(), agent_id="main_agent",
        capability="file", operation="create", resource="workspace/bundle",
        arguments={}, idempotency_key="admission-bundle",
    )


async def test_bundle_admission_and_actual_bundle_share_one_pending_approval() -> None:
    admission = request()
    service = ApprovalService(InMemoryApprovalStore())
    first = await service.create(admission)
    actual = admission.model_copy(update={
        "arguments": {"title": "PRIVATE_SOURCE", "filename": "project.zip"},
        "idempotency_key": "actual-bundle",
    })
    second = await service.create(actual)
    assert first.id == second.id
    assert first.request_scope == (
        f"capability-scope:v1:tenant={admission.tenant_id}:user={admission.user_id}:"
        f"run={admission.run_id}:capability=file:operation=create:resource=workspace/bundle"
    )
    assert "PRIVATE_SOURCE" not in first.request_scope


@pytest.mark.parametrize("field", ["tenant_id", "user_id", "run_id"])
async def test_bundle_approval_cannot_cross_tenant_actor_or_run(field: str) -> None:
    original = request()
    service = ApprovalService(InMemoryApprovalStore())
    first = await service.create(original)
    second = await service.create(original.model_copy(update={field: uuid4()}))
    assert first.id != second.id and first.request_scope != second.request_scope


@pytest.mark.parametrize(("capability", "operation", "resource"), [
    ("file", "write", "workspace/bundle"),
    ("file", "create", "workspace/bundle/other"),
    ("file", "create", "workspace/bundles"),
    ("command", "create", "workspace/bundle"),
])
def test_bundle_scope_is_an_exact_static_tuple(
    capability: str, operation: str, resource: str,
) -> None:
    assert capability_approval_scope(request().model_copy(update={
        "capability": capability, "operation": operation, "resource": resource,
    })) is None


def test_bundle_scope_stays_distinct_from_verification_and_workspace_write() -> None:
    original = request()
    scopes = [capability_approval_scope(original)]
    for parts in [("file", "write", "workspace/current"),
                  ("command", "run", "workspace/current/project-verification")]:
        scopes.append(capability_approval_scope(original.model_copy(update={
            "capability": parts[0], "operation": parts[1], "resource": parts[2],
        })))
    assert all(scope is not None for scope in scopes) and len(set(scopes)) == 3
