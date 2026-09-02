from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from uuid import UUID, uuid4

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import (
    DefaultRuntimeCapabilityPolicyGateway,
    build_runtime_capability_stack,
    default_capability_policy,
)
from agent_hub.capabilities.gateway import CapabilityGateway, CapabilityStatus
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.harness.types import HarnessToolCallRequest

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
GENERATED_ZIP_SCOPE = (
    "capability-scope:v1:"
    "tenant=11111111-1111-4111-8111-111111111111:"
    "user=22222222-2222-4222-8222-222222222222:"
    "run=33333333-3333-4333-8333-333333333333:"
    "capability=file:"
    "operation=create:"
    "resource=generated/project.generate_zip"
)


def request(capability: str, operation: str, resource: str) -> CapabilityRequest:
    return CapabilityRequest(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        agent_id="researcher",
        capability=capability,
        operation=operation,
        resource=resource,
        idempotency_key=f"{capability}:{operation}:{resource}",
        run_id=RUN_ID,
    )


def capability_request(
    *,
    agent_id: str,
    resource: str = "generated/project.generate_zip",
    run_id: UUID = RUN_ID,
    arguments: dict[str, object] | None = None,
) -> CapabilityRequest:
    return CapabilityRequest(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        agent_id=agent_id,
        capability="file",
        operation="create",
        resource=resource,
        arguments={"filename": f"{agent_id}.zip"} if arguments is None else arguments,
        idempotency_key=f"{run_id}:{agent_id}:{resource}",
        run_id=run_id,
    )


class ScopeApprovedRepository:
    def __init__(self) -> None:
        self.pending_approvals = 0
        self.approved_scopes: set[str] = set()

    async def is_capability_approval_approved(
        self,
        _tenant_id: UUID,
        _run_id: UUID,
        _approval_fingerprint: str,
    ) -> bool:
        return False

    async def is_capability_approval_scope_approved(
        self,
        _tenant_id: UUID,
        _run_id: UUID,
        approval_scope: str,
    ) -> bool:
        return approval_scope in self.approved_scopes

    async def begin_capability_approval(
        self,
        _tenant_id: UUID,
        _run_id: UUID,
        *,
        approval_id: str,
        approval_fingerprint: str,
        approval_scope: str | None = None,
    ) -> object:
        del approval_id, approval_fingerprint, approval_scope
        self.pending_approvals += 1
        return object()


async def approval_required(_tenant_id: UUID) -> bool:
    return True


def test_default_capability_policy_allows_safe_runtime_tools_for_operators() -> None:
    policy = default_capability_policy(TENANT_ID)

    calculator = policy.evaluate(request("calculator", "evaluate", "calculator"), Role.OPERATOR)
    read = policy.evaluate(request("file", "read", "workspace/docs/a.md"), Role.OPERATOR)
    create = policy.evaluate(
        request("file", "create", "generated/document.generate_docx"), Role.OPERATOR
    )
    project = policy.evaluate(
        request("file", "create", "generated/project.generate_zip"), Role.OPERATOR
    )
    skill = policy.evaluate(request("skill", "use", "skill/docx"), Role.OPERATOR)

    assert calculator.effect is PolicyEffect.ALLOW
    assert read.effect is PolicyEffect.ALLOW
    assert create.effect is PolicyEffect.ALLOW
    assert project.effect is PolicyEffect.ALLOW
    assert skill.effect is PolicyEffect.ALLOW


def test_default_capability_policy_denies_viewer_runtime_tools() -> None:
    policy = default_capability_policy(TENANT_ID)

    decision = policy.evaluate(request("calculator", "evaluate", "calculator"), Role.VIEWER)

    assert decision.effect is PolicyEffect.DENY


async def test_default_runtime_capability_stack_authorizes_request_tenant(
    tmp_path: Path,
) -> None:
    stack = build_runtime_capability_stack(
        tenant_id=TENANT_ID,
        run_repository=object(),
        skill_store_dir=tmp_path,
        workspace_root=tmp_path,
    )
    request_tenant = UUID("44444444-4444-4444-8444-444444444444")

    result = await stack.harness_tool_gateway.invoke(
        request_tenant,
        HarnessToolCallRequest(
            run_id=RUN_ID,
            actor="researcher",
            tool_name="calculator",
            arguments={"expression": "2+3"},
            approval_required=False,
            sandbox="none",
            idempotency_key="tenant-default-policy",
        ),
        user_id=uuid4(),
        role=Role.OPERATOR,
    )

    assert result.status == "succeeded"
    assert result.payload == {"value": "5"}


async def test_default_runtime_capability_stack_wires_generated_artifact_store(
    tmp_path: Path,
) -> None:
    generated_dir = tmp_path / "generated"
    stack = build_runtime_capability_stack(
        tenant_id=TENANT_ID,
        run_repository=object(),
        skill_store_dir=tmp_path / "skills",
        workspace_root=tmp_path / "workspace",
        generated_artifact_dir=generated_dir,
    )

    result = await stack.harness_tool_gateway.invoke(
        TENANT_ID,
        HarnessToolCallRequest(
            run_id=RUN_ID,
            actor="document_writer",
            tool_name="document.generate_docx",
            arguments={"title": "Delivery Plan"},
            approval_required=False,
            sandbox="restricted",
            idempotency_key="docx-stack",
        ),
        user_id=uuid4(),
        role=Role.OPERATOR,
    )

    assert result.status == "succeeded"
    file_payload = result.payload["file"]
    assert isinstance(file_payload, Mapping)
    assert file_payload["filename"] == "delivery-plan.docx"
    assert "storage_key" not in file_payload
    metadata = result.payload["metadata"]
    assert isinstance(metadata, Mapping)
    assert isinstance(metadata["storage_key"], str)
    assert (generated_dir / metadata["storage_key"]).is_file()


async def test_generated_file_scope_approval_reuses_run_level_tool_consent() -> None:
    repository = ScopeApprovedRepository()
    first_request = capability_request(agent_id="engineer")
    second_request = capability_request(agent_id="reviewer", arguments={"filename": "final.zip"})
    repository.approved_scopes.add(GENERATED_ZIP_SCOPE)
    gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()),
        repository,
        require_approval_for_tools=approval_required,
    )

    result = await gateway.invoke(second_request, role=Role.OPERATOR)

    assert result.status is CapabilityStatus.ALLOWED
    assert repository.pending_approvals == 0
    assert first_request.run_id == second_request.run_id


async def test_generated_file_scope_approval_does_not_authorize_other_tools_or_runs() -> None:
    repository = ScopeApprovedRepository()
    repository.approved_scopes.add(GENERATED_ZIP_SCOPE)
    gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()),
        repository,
        require_approval_for_tools=approval_required,
    )

    other_run = await gateway.invoke(
        capability_request(
            agent_id="reviewer",
            run_id=UUID("44444444-4444-4444-8444-444444444444"),
        ),
        role=Role.OPERATOR,
    )
    skill = await gateway.invoke(
        CapabilityRequest(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            agent_id="reviewer",
            capability="skill",
            operation="use",
            resource="skill/project-packager",
            arguments={"name": "project-packager"},
            idempotency_key="skill:project-packager",
            run_id=RUN_ID,
        ),
        role=Role.OPERATOR,
    )

    assert other_run.status is CapabilityStatus.WAITING_APPROVAL
    assert skill.status is CapabilityStatus.WAITING_APPROVAL


async def test_pending_generated_file_scope_reuses_one_approval_id_before_user_decides() -> None:
    repository = ScopeApprovedRepository()
    policy = CapabilityPolicy(
        (
            CapabilityRule(
                tenant_id=TENANT_ID,
                role=Role.OPERATOR,
                agent_id=None,
                capability="file",
                operation="create",
                resource_prefix="generated",
                effect=PolicyEffect.REQUIRE_APPROVAL,
            ),
        )
    )
    gateway = CapabilityGateway(policy, ApprovalService(InMemoryApprovalStore()), repository)

    first = await gateway.invoke(capability_request(agent_id="engineer"), role=Role.OPERATOR)
    second = await gateway.invoke(
        capability_request(agent_id="reviewer", arguments={"filename": "review.zip"}),
        role=Role.OPERATOR,
    )

    assert first.status is CapabilityStatus.WAITING_APPROVAL
    assert second.status is CapabilityStatus.WAITING_APPROVAL
    assert second.approval_id == first.approval_id
