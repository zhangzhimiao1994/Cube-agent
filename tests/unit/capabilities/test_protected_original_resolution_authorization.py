"""Protected originals use a separate approval binding over the captured snapshot."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities import python_resolution_authorization as authorization
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.gateway import (
    ApprovalReviewDecision,
    ApprovalReviewOutcome,
    CapabilityResult,
    CapabilityStatus,
)
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runs.repository import RunRecord
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.skills.sandbox.python_registry_protocol import digest
from agent_hub.skills.sandbox.python_resolution_protocol import ProtectedOriginalResolutionSource
from tests.unit.capabilities.test_standard_python_private_recipe import ENVIRONMENT

REFERENCE = {
    "schema_version": 1, "contract_sha256": "a" * 64, "input_source_sha256": "b" * 64,
    "file_count": 6,
}
SOURCE = {
    "original_reference": REFERENCE, "protected_sha256": "c" * 64, "manifest_sha256": "d" * 64,
}
PERMISSIONS = ["workspace.read", "workspace.write", "command.run"]


def original() -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        id=UUID(int=2), tenant_id=UUID(int=1), actor_id=UUID(int=3), request="repair original",
        mode=TaskMode.DISCUSS, status=RunStatus.RUNNING, version=2, created_at=now,
        actor_role=Role.OPERATOR, worker_id="worker", worker_lease_token=UUID(int=4),
        worker_lease_expires_at=now + timedelta(minutes=1), worker_heartbeat_at=now,
        routing_decision={
            "project_id": "billing", "workspace_session_id": "session-one",
            "sandbox_profile": "workspace_write", "execution_backend": "systemd",
            "requested_permissions": PERMISSIONS,
            "project_input_contract_version": 1,
            "project_input": {"schema_version": 1, "kind": "managed_workspace",
                              "editable_paths": ["src/billing/cli.py"]},
            "project_input_preparation": {"schema_version": 1, "phase": "captured",
                                          "reference": REFERENCE},
        },
    )


def action(record: RunRecord, *, generated: bool = False, actor: str = "main_agent",
           changes: dict[str, object] | None = None) -> CapabilityRequest:
    arguments = {
        "purpose": "python_dependency_resolution", "key": "1" * 64,
        "source_sha256": REFERENCE["input_source_sha256"], "root_sha256": "2" * 64,
        "catalog_sha256": "3" * 64, "permissions_sha256": digest({
            "sandbox_profile": "workspace_write", "execution_backend": "systemd",
            "permissions": sorted(PERMISSIONS),
        }),
        "original_deadline": (record.created_at + timedelta(minutes=5)).isoformat(),
        "registry": "https://pypi.org", "artifact_host": "files.pythonhosted.org",
        "install_allowed": False, "requirements": ["click>=8.1,<9"],
        "environment": ENVIRONMENT, "max_wheel_bytes": 64 * 1024**2,
    }
    if not generated:
        arguments["protected_original"] = SOURCE
    arguments.update(changes or {})
    return CapabilityRequest(
        tenant_id=record.tenant_id, run_id=record.id, user_id=UUID(int=3), agent_id=actor,
        capability="command", operation="run", resource=authorization.PREFIX + digest(arguments),
        arguments=arguments, idempotency_key="resolution-" + "1" * 64,
    )


def waiting(record: RunRecord) -> RunRecord:
    return replace(record, status=RunStatus.WAITING_APPROVAL, worker_id=None,
                   worker_lease_token=None, worker_lease_expires_at=None, worker_heartbeat_at=None)


def test_original_binding_matches_active_and_approval_read() -> None:
    record = original()
    source = ProtectedOriginalResolutionSource.model_validate(SOURCE, strict=True)
    scope = authorization.original_resolution_scope(record, source=source, actor="main_agent")
    request = action(record)
    bound = authorization.resolution_approval_binding(record, request)
    approval = capability_approval_scope(request)

    assert scope.actor_role == "operator" and scope.project_id == "billing"
    assert scope.session_id == "session-one" and scope.user_id == record.actor_id
    assert bound["scope"] == scope.model_dump(mode="json")
    assert authorization.resolution_binding_matches(record, bound, approval)
    assert authorization.resolution_binding_matches(waiting(record), bound, approval)
    assert not authorization.resolution_binding_matches(
        replace(waiting(record), worker_lease_token=UUID(int=9)), bound, approval,
    )
    with pytest.raises(authorization.InvalidResolutionAuthorization):
        authorization.resolution_approval_binding(waiting(record), request)


@pytest.mark.parametrize("change", ["reference", "phase", "schema", "session", "role", "lease", "actor"])
def test_original_approval_cannot_cross_capture_identity_or_lease(change: str) -> None:
    record = original()
    request = action(record)
    bound = authorization.resolution_approval_binding(record, request)
    approval = capability_approval_scope(request)
    assert record.routing_decision is not None
    route = dict(record.routing_decision)
    if change == "reference":
        route["project_input_preparation"] = {
            "schema_version": 1, "phase": "captured",
            "reference": {**REFERENCE, "contract_sha256": "f" * 64},
        }
    elif change == "phase":
        route["project_input_preparation"] = {
            "schema_version": 1, "phase": "executing", "reference": REFERENCE,
        }
    elif change == "schema":
        route["project_input_preparation"] = {
            "schema_version": 2, "phase": "captured", "reference": REFERENCE,
            "python_recipe": {"schema_version": 1, "recipe_sha256": "1" * 64,
                              "environment_input_sha256": "2" * 64},
        }
    elif change == "session":
        route["workspace_session_id"] = "sibling"
    elif change == "role":
        record = replace(record, actor_role=Role.VIEWER)
    elif change == "lease":
        record = replace(record, worker_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    else:
        record = replace(record, actor_id=UUID(int=9))
    assert not authorization.resolution_binding_matches(
        replace(record, routing_decision=route), bound, approval,
    )


def test_original_payload_is_part_of_the_exact_approval_digest() -> None:
    record = original()
    request = action(record)
    other = action(record, changes={"protected_original": {**SOURCE, "manifest_sha256": "f" * 64}})
    assert request.resource != other.resource
    first = authorization.resolution_approval_binding(record, request)
    changed = authorization.resolution_approval_binding(record, other)
    assert not authorization.resolution_binding_matches(record, changed, capability_approval_scope(request))
    assert not authorization.resolution_binding_matches(
        record, {**changed, "resource": first["resource"]}, capability_approval_scope(request),
    )
    for bad in (
        action(record, changes={"source_sha256": "f" * 64}),
        action(record, changes={"protected_original": {**SOURCE, "unknown": "f" * 64}}),
        action(record, actor="engineer"),
    ):
        with pytest.raises(authorization.InvalidResolutionAuthorization):
            authorization.validate_resolution_authorization(bad)


def test_generated_binding_keeps_legacy_shape_and_cannot_use_original_origin() -> None:
    record = original()
    assert record.routing_decision is not None
    route = {key: value for key, value in record.routing_decision.items()
             if not key.startswith("project_input")}
    route.update(generated_project_contract_version=1, python_dependency_preflight_version=1,
                 python_dependency_preflight_submission_version=1)
    generated = replace(record, routing_decision=route)
    legacy = action(generated, generated=True, actor="engineer")
    bound = authorization.resolution_approval_binding(generated, legacy)
    assert set(bound) == {"schema_version", "scope", "resource"} and bound["schema_version"] == 1
    assert authorization.resolution_binding_matches(generated, bound, capability_approval_scope(legacy))
    assert not authorization.resolution_binding_matches(record, bound, capability_approval_scope(legacy))
    with pytest.raises(authorization.InvalidResolutionAuthorization):
        authorization.resolution_approval_binding(generated, action(record))


def test_publication_keeps_separate_plan_approval_for_the_same_original() -> None:
    record = original()
    resolve = action(record)
    publish = action(record, changes={
        "purpose": "python_dependency_publication", "plan_sha256": "4" * 64,
        "artifacts": [["click-8.1.8-py3-none-any.whl", "5" * 64, 1024]],
    })
    binding = authorization.resolution_approval_binding(record, publish)
    approval = capability_approval_scope(publish)
    assert authorization.resolution_binding_matches(waiting(record), binding, approval)
    assert not authorization.resolution_binding_matches(record, binding, capability_approval_scope(resolve))
    assert not authorization.resolution_binding_matches(
        replace(waiting(record), status=RunStatus.FAILED), binding, approval,
    )


def test_manual_requeue_uses_fresh_worker_and_same_protected_approval() -> None:
    first = original()
    request = action(first)
    binding = authorization.resolution_approval_binding(first, request)
    approval = capability_approval_scope(request)
    queued = replace(waiting(first), status=RunStatus.QUEUED)
    assert authorization.resolution_binding_matches(queued, binding, approval)
    with pytest.raises(authorization.InvalidResolutionAuthorization):
        authorization.resolution_approval_binding(queued, request)

    now = datetime.now(UTC)
    current = replace(queued, status=RunStatus.RUNNING, worker_id="replacement-worker",
                      worker_lease_token=UUID(int=8), worker_lease_expires_at=now + timedelta(minutes=1),
                      worker_heartbeat_at=now)
    source = ProtectedOriginalResolutionSource.model_validate(SOURCE, strict=True)
    scope = authorization.original_resolution_scope(current, actor="main_agent", source=source)
    assert scope.model_dump(mode="json") == binding["scope"]
    assert authorization.resolution_approval_binding(current, request) == binding
    assert authorization.resolution_binding_matches(current, binding, approval)

    sibling_reference = {**REFERENCE, "input_source_sha256": "e" * 64}
    sibling = action(current, changes={
        "source_sha256": sibling_reference["input_source_sha256"],
        "protected_original": {**SOURCE, "original_reference": sibling_reference},
    })
    with pytest.raises(authorization.InvalidResolutionAuthorization):
        authorization.resolution_approval_binding(current, sibling)
    malformed = action(current, changes={"key": 5})
    with pytest.raises(authorization.InvalidResolutionAuthorization):
        authorization.validate_resolution_authorization(malformed)


@pytest.mark.parametrize("publication", [False, True])
@pytest.mark.parametrize("automatic", [False, True])
async def test_protected_original_gateway_requires_exact_approval(publication: bool, automatic: bool) -> None:
    record = original()
    assert record.worker_id is not None and record.actor_id is not None and record.mode is not None
    begin = AsyncMock()
    audit = AsyncMock()
    repository = SimpleNamespace(
        get=AsyncMock(return_value=record), begin_capability_approval=begin,
        record_capability_approval_review=audit,
        supports_conversation_capability_approval=True,
    )
    review = AsyncMock(return_value=ApprovalReviewDecision(
        ApprovalReviewOutcome.ALLOW, "auto_review", "fixture allows",
    ))
    policy = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repository,
        approval_reviewer=SimpleNamespace(review=review) if automatic else None,
    )
    gateway = HarnessToolGateway(RuntimeCapabilityGateway(skill_store_dir=Path(".")), policy_gateway=policy)
    context = TaskContext(
        tenant_id=record.tenant_id, run_id=record.id, actor_id=record.actor_id, actor_role=Role.OPERATOR,
        execution_token=record.worker_lease_token, mode=record.mode, request=record.request,
        routing_decision=cast(dict[str, JsonValue], record.routing_decision or {}), timeout_seconds=300.0,
    )
    changes: dict[str, object] = {}
    if publication:
        changes.update(purpose="python_dependency_publication", plan_sha256="4" * 64,
                       artifacts=[["click-8.1.8-py3-none-any.whl", "5" * 64, 1024]])
    exact = action(record, changes=changes)
    drift = action(record, changes={
        **changes,
        "protected_original": {
            **SOURCE, "original_reference": {**REFERENCE, "contract_sha256": "f" * 64},
        },
    })
    assert authorization.validate_resolution_authorization(drift) == drift
    assert drift.resource != exact.resource

    async def authorize(request: CapabilityRequest) -> CapabilityResult:
        assert record.actor_id is not None
        return await gateway.authorize_original_python_resolution(
            request, tenant_id=record.tenant_id, run_id=record.id, user_id=record.actor_id, role=Role.OPERATOR,
        )
    with bind_execution_authority(context, worker_id=record.worker_id):
        assert (await authorize(drift)).status is (
            CapabilityStatus.DENIED
        )
        begin.assert_not_awaited()
        audit.assert_not_awaited()
        review.assert_not_awaited()
        result = await authorize(exact)

    fingerprint = fingerprint_capability_request(exact)
    scope = capability_approval_scope(exact)
    if automatic:
        assert result.status is CapabilityStatus.ALLOWED
        review.assert_awaited_once_with(exact)
        begin.assert_not_awaited()
        audit.assert_awaited_once_with(
            record.tenant_id, record.id,
            approval_id="auto_review_" + hashlib.sha256(fingerprint.encode()).hexdigest()[:24],
            approval_fingerprint=fingerprint, reviewer="auto_review", reason="fixture allows", status="approved",
            approval_scope=scope, approval_request=exact,
        )
    else:
        assert result.status is CapabilityStatus.WAITING_APPROVAL and result.approval_id is not None
        begin.assert_awaited_once_with(
            record.tenant_id, record.id, approval_id=result.approval_id, approval_fingerprint=fingerprint,
            approval_scope=scope, actor_id=record.actor_id, approval_request=exact,
        )
        audit.assert_not_awaited()
