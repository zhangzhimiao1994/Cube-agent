from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from uuid import UUID, uuid4

from agent_hub.auth.models import Role
from agent_hub.capabilities.defaults import (
    build_runtime_capability_stack,
    default_capability_policy,
)
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.harness.types import HarnessToolCallRequest

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")


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
